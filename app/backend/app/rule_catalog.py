"""The backend-owned catalog of rule types: `rules.REGISTRY` plus generated types, hoisted.

`rules.REGISTRY` holds the eight hand-written types and is a **module-level constant in a package
the backend installs**. Writing a generated type into it would invert that relationship — the
engine's behaviour would then depend on which HTTP requests a process happened to have served,
rather than on what was installed. This module is the alternative: it reads `REGISTRY` and never
mutates it, and it owns the one thing `REGISTRY` cannot hold — the `generated_rule_types` rows a
successful generation run has written — as its own, separate map. `_build_canon` in
`app.rules_stub` resolves through it instead of through `REGISTRY.get` directly, but takes it as an
argument rather than importing this module: `rules_stub` is deliberately ORM-free, and this module
is nothing but ORM.

**Hoisting** is the load path: turning one `generated_rule_types` row's stored bytecode back into a
constructible `RuleType`, re-proving at every load the safety properties that were true when the
row was written (`.claude/rules/rule-engine.md`, "AI generation loop"). A row that fails any step of
`hoist` is not a state this catalog can serve — it is logged loudly and left out of the map, so a
`space_rules` row naming it denies with `RULE_ERROR_MESSAGE` through the fail-closed path
`_build_canon` already has for an unregistered type, rather than 500ing the process that tried to
load it.

`catalog` is a module-level singleton, matching `REGISTRY` — there is exactly one live view of
"every rule type this process currently knows", read by every request the process serves.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import logging
import marshal
import time
from collections.abc import Callable, Mapping
from typing import Any

from generation.harness import ENGINE_NAMES
from rules import REGISTRY, BaseRule, ParamKind, RuleParam, RuleType, UnsafeRuleError, interfaces
from rules import validate_source as validate_rule_source
from rules.safety import ALLOWED_IMPORTS, SAFE_BUILTINS
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.identity.models import GeneratedRuleType, GeneratedRuleTypeStatus

logger = logging.getLogger(__name__)

#: A rule type by its stable string id, or `None` if this process knows no such type. What
#: `app.rules_stub.SpaceRuleConfig.lookup` is typed as — the adapter resolves through this
#: shape without ever importing this module (see the module docstring).
RuleTypeLookup = Callable[[str], "RuleType | None"]

#: How long a miss on `lookup` suppresses a further reload. A `space_rules` row naming a type this
#: process has never hoisted is either a genuinely unknown id (which reloading again cannot fix) or
#: a type hoisted by another uvicorn worker moments ago (which reloading fixes exactly once) —
#: either way, a booking request is not the place to pay a database round trip more than once per
#: window.
_MISS_RELOAD_COOLDOWN_SECONDS = 5.0


#: The module name a hoisted rule's globals carry. `__build_class__` reads `__name__` out of the
#: executing module's globals to set every class's `__module__`, and raises `NameError` if it is
#: absent — so a namespace with no `__name__` cannot execute a `class` statement at all. The value
#: is never imported and names no real module; it exists to be what a hoisted rule's traceback says.
_HOISTED_MODULE_NAME = "app.rule_catalog.<generated>"


def _guarded_import(
    name: str,
    globals: Any = None,
    locals: Any = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """`__import__` restricted to `rules.safety.ALLOWED_IMPORTS`, for a hoisted rule's namespace.

    The validator already enforces this allowlist statically, over the source, at write time and
    again at every load — so on the happy path this guard never fires. It is here for the one gap
    that re-validating the source cannot close: **`source_sha256` proves `human_code` has not
    changed since it was hashed, but nothing proves `executable_bytecode` was compiled from that
    source.** The blob is the artifact that actually runs, and it is the only one no check upstream
    of this line covers. A runtime import guard is therefore the one place the *executed* bytecode,
    rather than the source standing in for it, is held to the import allowlist.

    `level != 0` — a relative import — is refused outright rather than resolved: a hoisted rule has
    no package to be relative to, so the only thing a relative import could express here is an
    attempt to reach one this namespace was built not to have.
    """
    root = name.split(".")[0]
    if level != 0 or root not in ALLOWED_IMPORTS:
        raise ImportError(
            f"a generated rule may import only {', '.join(sorted(ALLOWED_IMPORTS))}; "
            f"{name!r} is not one of them"
        )
    return builtins.__import__(name, globals, locals, fromlist, level)


def _restricted_namespace() -> dict[str, Any]:
    """The globals a hoisted generated rule's module is executed in — never the real builtins.

    Three layers, and the second is the one that is easy to get wrong:

    1. **`rules.safety.SAFE_BUILTINS`**, and nothing else from `builtins` that a rule can *name*.
       That frozenset is the validator's own list of pure builtins — they return data, never a
       namespace or a route to one — so a rule reaching any other builtin (`repr`, `type`,
       `print`, `ValueError`) raises `NameError` when it runs and the controller contains it as a
       denial. That asymmetry with the sandbox, which runs a candidate under the real builtins, is
       deliberate: the sandbox bounds what execution can *cost* while this namespace bounds what it
       can *reach*, and a rule that passes there and `NameError`s here fails closed, which is the
       direction that is safe to be wrong in.
    2. **The execution machinery a module needs but a rule never writes.** `SAFE_BUILTINS` is a
       list of names appearing in *source*; a compiled module also calls builtins the source never
       mentions. Every `class` statement calls `__build_class__`, and every `import` calls
       `__import__` — including the `from datetime import timedelta` the Generator's system prompt
       explicitly requires, since only the seven engine names are free here. Omitting the two does
       not harden anything: it makes *every* generated rule unhoistable, since a rule is a class by
       definition. `__import__` is `_guarded_import` rather than the real one, so the allowlist
       still holds at runtime; `__build_class__` is handed over as-is, because building a class is
       what the source was validated as doing.
    3. **The seven engine free names** `generation.harness.ENGINE_NAMES` binds in the sandbox
       prelude, from `rules.interfaces`. Reusing that tuple rather than restating it here is the
       whole point of its existing: it and the Generator's system prompt are one promise written in
       two languages, and a third copy would be a third thing to drift.
    """
    namespace: dict[str, Any] = {
        "__name__": _HOISTED_MODULE_NAME,
        "__builtins__": {
            **{name: getattr(builtins, name) for name in sorted(SAFE_BUILTINS)},
            "__build_class__": builtins.__build_class__,
            "__import__": _guarded_import,
        },
    }
    namespace.update({name: getattr(interfaces, name) for name in ENGINE_NAMES})
    return namespace


class HoistError(Exception):
    """One `generated_rule_types` row could not be turned into a `RuleType`.

    Raised by every check `hoist` performs and caught only by `reload`, which logs it at ERROR with
    the row's id and the reason and moves on to the next row — never allowed to escape `reload`
    itself, because one bad row must not stop the other rows in the table from loading, and must
    not stop the process booting (`app.main`'s lifespan handler calls `reload` once at startup).
    """


class RuleCatalog:
    """Every rule type this process currently knows: `REGISTRY` plus hoisted generated types.

    `REGISTRY` is authoritative and this catalog never copies it; `_hoisted` is this instance's own
    map, rebuilt wholesale by `reload` and read by `get`. The two are disjoint in practice —
    `rule_type` is unique across the whole product because a generated type's id is minted at write
    time from its label, never reused (`app.identity.models.GeneratedRuleType`) — so there is no
    precedence question to resolve between them.
    """

    def __init__(self) -> None:
        self._hoisted: dict[str, RuleType] = {}
        self._last_reload_at: float | None = None

    def get(self, rule_type: str) -> RuleType | None:
        """`REGISTRY` first, then the hoisted generated types. No database access, ever.

        This is the fast path every booking evaluation actually takes: by the time a request
        arrives, either `reload` has already run (at startup, or after an earlier miss) and this is
        a dict lookup, or the type genuinely does not exist here and no query would change that
        without a reload. `lookup` below is what performs the reload; this method never does.
        """
        found = REGISTRY.get(rule_type)
        if found is not None:
            return found
        return self._hoisted.get(rule_type)

    def lookup(self, rule_type: str) -> RuleType | None:
        """`get`, and on a miss, one self-healing reload before giving up.

        Multi-worker uvicorn gives each process its own catalog, so a type hoisted by the worker
        that served a successful generation run is invisible to every other worker until it
        reloads. Reloading on every `get` would cost a query on the hot path for every booking this
        process ever evaluates; reloading only on a miss costs one only on a path that was about to
        deny anyway, and it is what makes the cache self-healing across workers.

        Throttled by `_last_reload_at`: a miss within `_MISS_RELOAD_COOLDOWN_SECONDS` of the last
        reload *attempt* does not trigger another one, so a genuinely unknown `rule_type` — the
        common case, a typo or a retired type — costs one query per window rather than one per
        booking.

        The stamp is taken **before** the attempt, not after a successful one, and the difference
        is the whole storm guard rather than a detail of where the line sits. Stamping only on
        success would leave the cooldown unarmed for exactly the failure it most needs to cover: a
        database that is down or refusing connections makes every miss re-enter this branch and
        open a fresh connection, so the moment Postgres is least able to absorb load is the moment
        this path stops throttling itself. What that costs is bounded and small — a reload that
        fails for a transient reason delays self-healing by at most one cooldown window — and
        `reload` stamps again on the way out, so a *successful* reload still resets the clock from
        its own completion.

        `app.db.session.get_session_factory` is imported lazily, inside this method, so importing
        this module in a process with no `DATABASE_URL` configured stays harmless; only actually
        reaching this branch requires a database. Any exception opening the session or reloading is
        logged and swallowed — the miss stands, `lookup` returns `None`, and the caller
        (`_build_canon`) fails closed exactly as it would for any other unregistered type.
        """
        found = self.get(rule_type)
        if found is not None:
            return found

        now = time.monotonic()
        if (
            self._last_reload_at is not None
            and now - self._last_reload_at < _MISS_RELOAD_COOLDOWN_SECONDS
        ):
            return None
        self._last_reload_at = now

        try:
            from app.db.session import get_session_factory

            session = get_session_factory()()
            try:
                self.reload(session)
            finally:
                session.close()
        except Exception:
            logger.exception(
                "Could not reload the rule catalog after a miss on rule_type %r; denying.",
                rule_type,
            )
            return None

        return self.get(rule_type)

    def all(self) -> tuple[RuleType, ...]:
        """Every registered type — `REGISTRY` plus every hoisted generated one — in canon order.

        Sorted by `(priority, rule_type)`. The secondary key is not cosmetic: every generated type
        shares priority 100, so without a tie-breaker two reloads of the same rows could return
        them in a different order (dict iteration order follows insertion, and `reload` rebuilds
        from a database query with no `ORDER BY` on `rule_type`), which would make
        `GET /rule-types` — and the canon order it implies — flap for no reason a caller could see.
        """
        return tuple(
            sorted(
                (*REGISTRY.values(), *self._hoisted.values()),
                key=lambda rule_type: (rule_type.priority, rule_type.rule_type),
            )
        )

    def reload(self, session: Session) -> None:
        """Rebuild the hoisted map from every `active` `generated_rule_types` row.

        Built into a fresh `dict` and assigned to `self._hoisted` in one statement — never mutated
        in place — so a concurrent `get`/`lookup` from another request in flight on this process
        always sees either the old map or the complete new one, never a half-built mix of both. A
        row that fails `hoist` is logged at ERROR with its id and the reason and otherwise skipped:
        see `HoistError`'s docstring for why the loop continues rather than propagating.

        The monotonic clock is stamped once the new map is in place, so a reload triggered from
        anywhere — this method called directly at startup, or reached through `lookup` — resets
        `lookup`'s cooldown from the moment the catalog actually became fresh. `lookup` stamps
        again on its own way *in*, before it calls this; see its docstring for why the attempt and
        not only the success is what arms the throttle.
        """
        rows = (
            session.execute(
                select(GeneratedRuleType).where(
                    GeneratedRuleType.status == GeneratedRuleTypeStatus.ACTIVE
                )
            )
            .scalars()
            .all()
        )

        hoisted: dict[str, RuleType] = {}
        for row in rows:
            try:
                hoisted[row.rule_type] = self.hoist(row)
            except HoistError:
                logger.error(
                    "Could not hoist generated_rule_types row %s (rule_type=%r); it stays "
                    "unavailable until this is fixed and the catalog reloads.",
                    row.id,
                    row.rule_type,
                    exc_info=True,
                )

        self._hoisted = hoisted
        self._last_reload_at = time.monotonic()

    def hoist(self, row: GeneratedRuleType) -> RuleType:
        """Turn one row into a constructible `RuleType`, re-proving every safety property as it
        goes rather than trusting that they still hold.

        In order, each failure raising `HoistError`:

        1. **`validate_source(row.human_code)` again**, at load, not only at write time. Storing
           bytecode gives up the property that the executed artifact is provably the validated
           one; re-parsing the source here — about a millisecond — is what is left of that
           guarantee once bytecode, not source, is what actually runs.
        2. **`sha256(row.human_code) == row.source_sha256`.** If the stored source has been edited
           since it was compiled, the bytecode about to run is not bytecode for the source that was
           just re-validated in step 1 — the two must describe the same artifact.
        3. **`row.bytecode_magic == importlib.util.MAGIC_NUMBER.hex()`.** `marshal`'s format is not
           stable across interpreter versions; a mismatch means this row was compiled by a Python
           this process is not running, and `deferred/generated-rule-bytecode-recompile.md` is the
           hand-run repair for it, not a fallback here.
        4. **`marshal.loads` the bytecode, then `exec` it** in `_restricted_namespace()` — never
           the real builtins. See that function for what it binds and why the list is not simply
           `SAFE_BUILTINS`.
        5. **Exactly one `BaseRule` subclass in the resulting namespace.** Zero means the source
           defined no rule at all; two or more means this catalog cannot know which one a
           `space_rules` row naming this type would mean.
        6. **Build a `RuleType`** from the row's own columns. `needs_local_resolution=False`
           always: a generated rule reads `context.local` directly and never needs a constructor
           argument resolved against the Space's zone — that is the entire point of `LocalFrame`
           existing before this feature did. `is_single=False`: advisory only, and neither reading
           is knowable for a type nobody has reasoned about — `False` is the one that shows an
           admin no warning this catalog cannot justify. `build` is a real named function, not a
           lambda, so a traceback from a constructor that rejects its own params names it.
        """
        try:
            validate_rule_source(row.human_code)
        except UnsafeRuleError as exc:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}) failed validate_source: "
                f"{exc}"
            ) from exc

        if hashlib.sha256(row.human_code.encode("utf-8")).hexdigest() != row.source_sha256:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): source_sha256 does not "
                "match human_code — the stored source has changed since it was compiled."
            )

        running_magic = importlib.util.MAGIC_NUMBER.hex()
        if row.bytecode_magic != running_magic:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): bytecode_magic "
                f"{row.bytecode_magic!r} does not match the running interpreter's "
                f"{running_magic!r} (see deferred/generated-rule-bytecode-recompile.md)."
            )

        try:
            code = marshal.loads(row.executable_bytecode)
        except (ValueError, EOFError, TypeError) as exc:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): executable_bytecode did "
                f"not unmarshal: {exc!r}"
            ) from exc

        namespace = _restricted_namespace()

        try:
            exec(code, namespace)  # noqa: S102 - the whole point; namespace is the sandbox.
        except Exception as exc:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): executing the compiled "
                f"module raised {exc!r}"
            ) from exc

        rule_classes = [
            obj
            for obj in namespace.values()
            if isinstance(obj, type) and issubclass(obj, BaseRule) and obj is not BaseRule
        ]
        if len(rule_classes) != 1:
            found = ", ".join(sorted(cls.__name__ for cls in rule_classes)) or "none"
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): expected exactly one "
                f"BaseRule subclass in the hoisted module, found {len(rule_classes)}: {found}"
            )
        rule_class = rule_classes[0]

        try:
            # `schema_params`, not `params`: these are the RuleParam *declarations* the admin form
            # and the write-boundary validator read, while `build` below is handed the validated
            # *values*. The registry's own build functions call that argument `params`, so naming
            # the declarations the same thing here would shadow it inside the closure.
            schema_params = tuple(
                RuleParam(**{**entry, "kind": ParamKind(entry["kind"])})
                for entry in row.param_schema
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): malformed param_schema "
                f"entry: {exc!r}"
            ) from exc

        def build(params: Mapping[str, Any], resolved: Mapping[str, Any] | None = None) -> BaseRule:
            return rule_class(**params)

        try:
            return RuleType(
                rule_type=row.rule_type,
                label=row.label,
                description=row.description,
                priority=row.priority,
                params=schema_params,
                reads_history=row.reads_history,
                needs_local_resolution=False,
                is_single=False,
                build=build,
            )
        except (TypeError, ValueError) as exc:
            raise HoistError(
                f"generated_rule_types row {row.id} ({row.rule_type!r}): could not build a "
                f"RuleType from it: {exc!r}"
            ) from exc


#: The process-wide catalog, matching `REGISTRY`'s own module-level singleton. `app.main`'s
#: lifespan handler reloads it once at startup; `app.identity.service` and `app.identity.router`
#: read through it instead of `REGISTRY` directly, so a booking or a rule-types listing sees
#: generated types the moment this process has hoisted them.
catalog = RuleCatalog()
