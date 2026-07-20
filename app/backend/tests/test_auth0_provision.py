"""Provisioning-script tests, run against an in-memory Auth0 tenant.

No network and no credentials, so this suite passes in CI exactly as it passes
locally. The fake below is not a stub that returns canned values: it *keeps
state*, so a run genuinely sees what the previous run left behind. That is the
only way to test the property this script exists for.

**The headline is idempotency, and it is asserted on the HTTP verbs.** A test
that ran the routine twice and checked it did not crash would pass against a
script that created a second application on every run — which is the precise
bug the script is written to avoid, and one that fails silently in production
(the duplicate application has no connections enabled, so logins break with an
error that points nowhere near the cause). So the assertion is: the second run
issues ``PATCH`` and **zero** ``POST``.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

import pytest

from app.settings import Settings
from scripts import auth0_provision
from scripts.auth0_provision import (
    API_IDENTIFIER,
    SPA_NAME,
    SPA_ORIGIN,
    DryRunClient,
    ProvisioningError,
    load_credentials,
    main,
    print_env,
    provision,
)

DOMAIN = "test-tenant.us.auth0.com"

# A value that must never reach stdout. Distinctive enough that a substring
# search for it cannot match anything else the script prints.
SECRET_SENTINEL = "s3cr3t-m2m-value-must-never-be-printed"


class FakeTenant:
    """An in-memory Auth0 Management API that remembers what was done to it.

    Implements the same three verbs as ``HttpManagementClient`` and records
    every call, so a test can assert on the verbs rather than on the end state.
    Two different implementations can reach the same end state; only one of them
    got there without creating a duplicate.
    """

    def __init__(self, *, connections: list[dict[str, Any]] | None = None) -> None:
        self.resource_servers: list[dict[str, Any]] = []
        self.clients: list[dict[str, Any]] = []
        self.connections: list[dict[str, Any]] = connections if connections is not None else []
        self.calls: list[tuple[str, str]] = []
        self._next_id = 1

    # -- helpers -----------------------------------------------------------

    def _mint_id(self, prefix: str) -> str:
        value = f"{prefix}{self._next_id}"
        self._next_id += 1
        return value

    def _connection(self, connection_id: str) -> dict[str, Any]:
        for connection in self.connections:
            if connection["id"] == connection_id:
                return connection
        raise AssertionError(f"no connection {connection_id}")

    def _collection(self, name: str) -> list[dict[str, Any]]:
        return {
            "resource-servers": self.resource_servers,
            "clients": self.clients,
            "connections": self.connections,
        }[name]

    def verbs(self, method: str) -> list[str]:
        """Paths hit with ``method``, in order."""
        return [path for verb, path in self.calls if verb == method]

    # -- the ManagementClient interface ------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path))
        if path.endswith("/clients") and path.startswith("/connections/"):
            # A connection's enabled clients are their own subresource on the
            # current Management API: the connection record does not carry an
            # `enabled_clients` array, and asking for that field is a 400.
            connection = self._connection(path.split("/")[2])
            return {"clients": [{"client_id": cid} for cid in connection["enabled_clients"]]}

        name = path.strip("/")
        items = self._collection(name)
        # Mirror Auth0's `include_totals` envelope, which is the shape the
        # script's pagination helper reads.
        page = int((params or {}).get("page", 0))
        per_page = int((params or {}).get("per_page", 50))
        window = items[page * per_page : (page + 1) * per_page]
        return {name.replace("-", "_"): window, "total": len(items)}

    def post(self, path: str, body: Any) -> Any:
        self.calls.append(("POST", path))
        name = path.strip("/")
        record = dict(body)
        record["id"] = self._mint_id("id_")
        if name == "clients":
            record["client_id"] = self._mint_id("client_")
            # A real tenant returns a secret here for confidential apps. Kept
            # out of the fake entirely: the script must never have one to leak.
        self._collection(name).append(record)
        return record

    def patch(self, path: str, body: Any) -> Any:
        self.calls.append(("PATCH", path))
        if path.endswith("/clients") and path.startswith("/connections/"):
            # A delta of {client_id, status} entries, not a replacement array.
            connection = self._connection(path.split("/")[2])
            enabled = connection["enabled_clients"]
            for entry in body:
                if entry["status"] and entry["client_id"] not in enabled:
                    enabled.append(entry["client_id"])
                elif not entry["status"] and entry["client_id"] in enabled:
                    enabled.remove(entry["client_id"])
            return {}

        name, _, identifier = path.strip("/").partition("/")
        key = "client_id" if name == "clients" else "id"
        for record in self._collection(name):
            if record.get(key) == identifier:
                record.update(body)
                return record
        raise AssertionError(f"PATCH {path} targeted an object that does not exist")

    def close(self) -> None:  # pragma: no cover - parity with the real client
        pass


def _database_connection() -> dict[str, Any]:
    return {
        "id": "con_db",
        "name": "Username-Password-Authentication",
        "strategy": "auth0",
        "enabled_clients": [],
    }


def _google_connection(enabled_clients: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "con_google",
        "name": "google-oauth2",
        "strategy": "google-oauth2",
        "enabled_clients": enabled_clients if enabled_clients is not None else [],
    }


@pytest.fixture
def tenant() -> FakeTenant:
    """A tenant with both connections present — the fully-configured case."""
    return FakeTenant(connections=[_database_connection(), _google_connection()])


def _run(tenant: FakeTenant, **kwargs: Any) -> tuple[Any, str]:
    """Provision against ``tenant``, returning the result and captured output."""
    buffer = io.StringIO()
    result = provision(tenant, DOMAIN, out=lambda line: print(line, file=buffer), **kwargs)
    return result, buffer.getvalue()


# --- Idempotency -----------------------------------------------------------


def test_first_run_creates_the_api_and_the_application(tenant: FakeTenant) -> None:
    """The baseline the idempotency test is measured against.

    Without this, "the second run issues no POST" would also pass for a script
    that never created anything at all.
    """
    _run(tenant)

    assert tenant.verbs("POST") == ["/resource-servers", "/clients"]
    assert len(tenant.resource_servers) == 1
    assert len(tenant.clients) == 1


def test_second_run_updates_and_creates_nothing(tenant: FakeTenant) -> None:
    """THE test: re-running must update in place, never duplicate.

    Asserted on the verbs, not just the object count, because a script could
    also reach "one client" by creating a second and deleting the first —
    which would hand every already-configured frontend a dead client id.
    """
    first, _ = _run(tenant)
    calls_before = len(tenant.calls)

    second, _ = _run(tenant)
    second_run_calls = tenant.calls[calls_before:]
    posts = [path for verb, path in second_run_calls if verb == "POST"]
    patches = [path for verb, path in second_run_calls if verb == "PATCH"]

    assert posts == [], f"second run created objects that already existed: {posts}"
    assert patches, "second run updated nothing at all"
    assert len(tenant.resource_servers) == 1
    assert len(tenant.clients) == 1
    # The client id is the value pasted into the frontend's .env, so it staying
    # put across runs is the whole point of not re-creating the application.
    assert second.spa_client_id == first.spa_client_id


def test_third_run_still_creates_nothing(tenant: FakeTenant) -> None:
    """Idempotency is a property of every subsequent run, not just the second."""
    _run(tenant)
    _run(tenant)
    calls_before = len(tenant.calls)
    _run(tenant)

    assert [verb for verb, _ in tenant.calls[calls_before:] if verb == "POST"] == []


def test_matching_is_by_identifier_not_by_position(tenant: FakeTenant) -> None:
    """An unrelated API in the tenant must not be mistaken for ours.

    Matching on anything but the natural key — taking the first element, say —
    would update a stranger's resource server and then create a duplicate of
    ours on the next run.
    """
    tenant.resource_servers.append(
        {"id": "id_other", "identifier": "https://someone-elses.example", "name": "Other"}
    )
    _run(tenant)
    _run(tenant)

    ours = [rs for rs in tenant.resource_servers if rs["identifier"] == API_IDENTIFIER]
    assert len(ours) == 1
    assert tenant.resource_servers[0]["name"] == "Other", "an unrelated API was modified"


def test_resource_server_update_omits_the_immutable_identifier(tenant: FakeTenant) -> None:
    """Auth0 rejects a PATCH that tries to change ``identifier``."""
    _run(tenant)
    patched: dict[str, Any] = {}

    original_patch = tenant.patch

    def capture(path: str, body: dict[str, Any]) -> Any:
        if path.startswith("/resource-servers/"):
            patched.update(body)
        return original_patch(path, body)

    tenant.patch = capture  # type: ignore[method-assign]
    _run(tenant)

    assert patched, "the resource server was never updated"
    assert "identifier" not in patched


# --- Configured values -----------------------------------------------------


def test_the_api_is_rs256_with_rbac_enforced(tenant: FakeTenant) -> None:
    _run(tenant)
    api = tenant.resource_servers[0]

    assert api["identifier"] == API_IDENTIFIER
    assert api["signing_alg"] == "RS256"
    assert api["enforce_policies"] is True


def test_the_spa_is_configured_for_the_vite_dev_server(tenant: FakeTenant) -> None:
    _run(tenant)
    spa = tenant.clients[0]

    assert spa["name"] == SPA_NAME
    assert spa["app_type"] == "spa"
    assert spa["callbacks"] == [SPA_ORIGIN]
    assert spa["allowed_logout_urls"] == [SPA_ORIGIN]
    assert spa["web_origins"] == [SPA_ORIGIN]


def test_both_connections_are_enabled_for_the_spa(tenant: FakeTenant) -> None:
    result, _ = _run(tenant)

    for connection in tenant.connections:
        assert result.spa_client_id in connection["enabled_clients"]
    assert result.google_connection_found is True


def test_enabling_our_client_preserves_other_clients(tenant: FakeTenant) -> None:
    """Other applications on a connection must survive a provisioning run.

    The PATCH body is asserted to be a delta naming only our client. A body that
    listed the full desired set would disable every application missing from it
    — a tenant-wide login outage caused by provisioning an unrelated app.
    """
    tenant.connections = [_database_connection(), _google_connection(["someone_elses_client"])]
    bodies: list[Any] = []
    original_patch = tenant.patch

    def capture(path: str, body: Any) -> Any:
        if path.startswith("/connections/"):
            bodies.append(body)
        return original_patch(path, body)

    tenant.patch = capture  # type: ignore[method-assign]
    result, _ = _run(tenant)

    google = tenant.connections[1]
    assert "someone_elses_client" in google["enabled_clients"]
    assert result.spa_client_id in google["enabled_clients"]
    assert bodies, "the connection was never updated"
    for body in bodies:
        assert [entry["client_id"] for entry in body] == [result.spa_client_id]
        assert all(entry["status"] is True for entry in body)


def test_an_already_enabled_connection_is_not_patched_again(tenant: FakeTenant) -> None:
    _run(tenant)
    calls_before = len(tenant.calls)
    _run(tenant)

    connection_patches = [
        path
        for verb, path in tenant.calls[calls_before:]
        if verb == "PATCH" and path.startswith("/connections/")
    ]
    assert connection_patches == []


# --- A tenant without Google ----------------------------------------------


def test_missing_google_warns_but_completes_the_rest() -> None:
    """The expected outcome on a bare tenant: report it, finish, exit 0.

    Creating the connection would need ``create:connections``, which is not
    granted. Failing the run would leave the API and the SPA unprovisioned over
    a social login that is a nice-to-have.
    """
    tenant = FakeTenant(connections=[_database_connection()])
    result, output = _run(tenant)

    assert result.google_connection_found is False
    assert "google-oauth2" in output
    assert "create:connections" in output
    assert "UNAVAILABLE" in output

    # Everything else still happened.
    assert len(tenant.resource_servers) == 1
    assert len(tenant.clients) == 1
    assert result.spa_client_id in tenant.connections[0]["enabled_clients"]


def test_missing_google_never_attempts_to_create_a_connection() -> None:
    tenant = FakeTenant(connections=[_database_connection()])
    _run(tenant)

    assert "/connections" not in tenant.verbs("POST")


def test_missing_google_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = FakeTenant(connections=[_database_connection()])
    _patch_main_dependencies(monkeypatch, tenant)

    with redirect_stdout(io.StringIO()):
        assert main([]) == 0


# --- Dry run ---------------------------------------------------------------


def test_dry_run_issues_no_mutating_requests(tenant: FakeTenant) -> None:
    """Side-effect free is asserted against the tenant, not against the log.

    Reads still happen — without them a dry run could not tell create from
    update, which is the most useful thing it has to say.
    """
    buffer = io.StringIO()
    provision(
        DryRunClient(tenant, out=lambda line: print(line, file=buffer)),
        DOMAIN,
        dry_run=True,
        out=lambda line: print(line, file=buffer),
    )

    assert tenant.verbs("POST") == []
    assert tenant.verbs("PATCH") == []
    assert tenant.verbs("GET"), "a dry run should still read the tenant"
    assert tenant.resource_servers == []
    assert tenant.clients == []


def test_dry_run_reports_the_calls_it_would_have_made(tenant: FakeTenant) -> None:
    buffer = io.StringIO()
    provision(
        DryRunClient(tenant, out=lambda line: print(line, file=buffer)),
        DOMAIN,
        dry_run=True,
        out=lambda line: print(line, file=buffer),
    )
    output = buffer.getvalue()

    assert "POST /resource-servers" in output
    assert "POST /clients" in output
    # A summary of the body, not just the path.
    assert SPA_ORIGIN in output


def test_dry_run_against_a_provisioned_tenant_reports_updates(tenant: FakeTenant) -> None:
    _run(tenant)
    buffer = io.StringIO()
    calls_before = len(tenant.calls)

    provision(
        DryRunClient(tenant, out=lambda line: print(line, file=buffer)),
        DOMAIN,
        dry_run=True,
        out=lambda line: print(line, file=buffer),
    )

    assert [verb for verb, _ in tenant.calls[calls_before:] if verb in {"POST", "PATCH"}] == []
    assert "PATCH /clients/" in buffer.getvalue()


# --- Output ----------------------------------------------------------------


def test_env_lines_use_the_names_settings_actually_reads(tenant: FakeTenant) -> None:
    """Guards the seam between this script and ``app/settings.py``.

    The names are derived from ``Settings`` rather than typed out again, so
    renaming a setting fails this test instead of silently producing an .env
    block the backend ignores.
    """
    result, _ = _run(tenant)
    buffer = io.StringIO()
    print_env(result, out=lambda line: print(line, file=buffer))
    output = buffer.getvalue()

    for field in ("auth0_domain", "auth0_api_audience"):
        assert field in Settings.model_fields
        assert f"{field.upper()}=" in output

    assert f"AUTH0_DOMAIN={DOMAIN}" in output
    assert f"AUTH0_API_AUDIENCE={API_IDENTIFIER}" in output
    assert f"VITE_AUTH0_CLIENT_ID={result.spa_client_id}" in output


def test_the_client_secret_never_reaches_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted against real captured stdout, not against a review of the code."""
    tenant = FakeTenant(connections=[_database_connection(), _google_connection()])
    _patch_main_dependencies(monkeypatch, tenant)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main([]) == 0

    assert SECRET_SENTINEL not in buffer.getvalue()


def test_the_client_secret_never_reaches_stdout_on_a_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = FakeTenant(connections=[_database_connection(), _google_connection()])
    _patch_main_dependencies(monkeypatch, tenant)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(["--dry-run"]) == 0

    assert SECRET_SENTINEL not in buffer.getvalue()
    assert tenant.verbs("POST") == []
    assert tenant.verbs("PATCH") == []


# --- Credentials -----------------------------------------------------------


def test_missing_credentials_are_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    for name in ("AUTH0_DOMAIN", "AUTH0_M2M_CLIENT_ID", "AUTH0_M2M_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ProvisioningError) as exc:
        load_credentials(env_file=tmp_path / "absent.env")

    assert "AUTH0_M2M_CLIENT_SECRET" in str(exc.value)


def _patch_main_dependencies(monkeypatch: pytest.MonkeyPatch, tenant: FakeTenant) -> None:
    """Point ``main`` at the fake tenant, with a secret that must not be printed."""
    monkeypatch.setattr(
        auth0_provision,
        "load_credentials",
        lambda *args, **kwargs: auth0_provision.Credentials(
            domain=DOMAIN,
            client_id="m2m_client_id",
            client_secret=SECRET_SENTINEL,
        ),
    )
    monkeypatch.setattr(
        auth0_provision, "fetch_management_token", lambda *args, **kwargs: "management-token"
    )
    monkeypatch.setattr(auth0_provision, "HttpManagementClient", lambda *args, **kwargs: tenant)
