#!/usr/bin/env python
"""Configure the Auth0 tenant Open-Skej logs in against.

Everything this script creates could be clicked together in the Auth0 dashboard.
Doing it in code buys two things the dashboard cannot: the configuration is
reviewable in a diff, and a fresh tenant can be brought to a known state in one
command instead of a wiki page nobody kept current.

**Idempotency is the whole design constraint.** The Management API's create
endpoints are not idempotent — ``POST /clients`` twice yields two applications
with the same name and different client ids, and the second one is silently
wrong: it has no connections enabled, so logins fail with an error that points
nowhere near the cause. Every object below is therefore handled as *read, then
create or update*: list the collection, match on the object's natural key, then
``POST`` only when there is genuinely nothing to update. The natural keys are
the API's ``identifier`` (immutable in Auth0, so it is the reliable one), the
application's ``name``, and a connection's ``strategy``.

**Google may legitimately be missing.** Attaching the tenant's ``google-oauth2``
connection to our client needs ``update:connections``, which we have; *creating*
that connection needs ``create:connections``, which we deliberately do not have.
A tenant without it is an expected outcome, not a failure — the script says so
loudly and exits 0, leaving email/password login working. Provisioning Google is
then a one-line scope grant in the dashboard followed by a re-run.

**The M2M client secret must never leave this process.** It is read from the
environment, used once to mint a Management token, and never printed — not in
output, not in an error message (see :func:`_redact`), and never in a test
fixture. The Management token itself is equally sensitive and equally unprinted.

Usage::

    ./venv/bin/python scripts/auth0_provision.py            # apply
    ./venv/bin/python scripts/auth0_provision.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import httpx
from dotenv import load_dotenv

# --- What we provision -----------------------------------------------------

# The audience our access tokens are minted for. `app/auth/jwt.py` checks the
# `aud` claim against AUTH0_API_AUDIENCE, so this value and the .env line this
# script prints have to agree exactly or every token is rejected.
API_IDENTIFIER = "https://api.open-skej.dev"
API_NAME = "Open-Skej API"

# The SPA's name is its natural key: Auth0 assigns the client id, so the name is
# the only stable handle we control across runs.
SPA_NAME = "open-skej-web"

# Vite's dev-server origin. Local-only by decision — nothing is deployed yet, so
# there is no staging or production callback to add.
SPA_ORIGIN = "http://localhost:5173"

# `auth0` is the strategy of a username/password database connection; every
# tenant is created with one. `google-oauth2` is the social connection that may
# or may not be present.
DATABASE_STRATEGY = "auth0"
GOOGLE_STRATEGY = "google-oauth2"

# Auth0 caps `per_page` at 100. A tenant of our size fits in one page, but the
# pagination loop is here so a shared tenant does not silently truncate a list
# and make an existing object look absent — which would blind-create a duplicate.
PAGE_SIZE = 100

# Requested instead of the full client record so a client secret is never even
# fetched, let alone printed. Belt and braces alongside `_redact`.
CLIENT_FIELDS = "client_id,name,app_type,callbacks,web_origins,allowed_logout_urls"

# The connections list endpoint accepts a fixed vocabulary of field names and
# 400s on anything outside it, so this stays minimal — everything we match on
# and nothing else.
CONNECTION_FIELDS = "id,name,strategy"

# `/connections/{id}/clients` uses checkpoint pagination (`take` plus a `next`
# cursor) rather than the page/per_page style the rest of the API uses, and
# rejects per_page outright. 50 is the endpoint's maximum.
CONNECTION_CLIENTS_PAGE_SIZE = 50

# Stands in for the client id a POST would have returned, so --dry-run can print
# a complete .env block. Visibly not a real id, so it cannot be pasted by mistake.
DRY_RUN_CLIENT_ID = "<created-on-a-real-run>"


class ProvisioningError(Exception):
    """A failure that should stop the run and exit non-zero.

    A missing Google connection is deliberately *not* one of these.
    """


@dataclass(frozen=True)
class Credentials:
    domain: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class ProvisionResult:
    domain: str
    audience: str
    spa_client_id: str
    google_connection_found: bool


# --- Management API access -------------------------------------------------


class ManagementClient(Protocol):
    """The three verbs this script needs from the Management API.

    Narrow on purpose: the tests implement it with an in-memory tenant, and
    ``--dry-run`` implements it by wrapping another one, both without touching
    HTTP.
    """

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...

    def post(self, path: str, body: Any) -> Any: ...

    def patch(self, path: str, body: Any) -> Any: ...


def _redact(text: str, secret: str) -> str:
    """Blank a secret out of text that is about to be shown to a human.

    Auth0's error bodies do not echo credentials back, but an error path is
    exactly where a value gets printed without anyone thinking about it, so this
    does not rely on that staying true.
    """
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


class HttpManagementClient:
    """Talks to the real Management API with a bearer token."""

    def __init__(self, domain: str, token: str, *, timeout: float = 30.0) -> None:
        self._base_url = f"https://{domain}/api/v2"
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._send("GET", path, params=params)

    def post(self, path: str, body: Any) -> Any:
        return self._send("POST", path, json=body)

    def patch(self, path: str, body: Any) -> Any:
        return self._send("PATCH", path, json=body)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        response = self._client.request(method, f"{self._base_url}{path}", params=params, json=json)
        if response.is_error:
            # The body carries Auth0's own explanation — an insufficient scope,
            # a rejected field — which is far more useful than the status alone.
            raise ProvisioningError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text}"
            )
        return response.json() if response.content else {}

    def close(self) -> None:
        self._client.close()


class DryRunClient:
    """Reads through to the real tenant; prints writes instead of issuing them.

    Dry-run is read-only, not call-free: without the ``GET`` half the script
    could not tell you whether a run would create or update, which is the single
    most useful thing a dry run has to say. Nothing that mutates the tenant is
    ever sent.
    """

    def __init__(self, inner: ManagementClient, out: Callable[[str], None] = print) -> None:
        self._inner = inner
        self._out = out

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._inner.get(path, params)

    def post(self, path: str, body: Any) -> Any:
        self._out(f"  [dry-run] POST {path} {_summarize(body)}")
        # Enough of a stand-in for the created object that the caller can carry
        # on and report what the rest of the run would do.
        return {**body, "client_id": DRY_RUN_CLIENT_ID, "id": DRY_RUN_CLIENT_ID}

    def patch(self, path: str, body: Any) -> Any:
        self._out(f"  [dry-run] PATCH {path} {_summarize(body)}")
        return body


def _summarize(body: Any) -> str:
    """A one-line rendering of a request body, for the dry-run log."""
    if isinstance(body, list):
        return "[" + ", ".join(_summarize(item) for item in body) + "]"
    parts = []
    for key, value in body.items():
        if isinstance(value, list):
            parts.append(f"{key}=[{', '.join(str(item) for item in value)}]")
        else:
            parts.append(f"{key}={value}")
    return "{" + ", ".join(parts) + "}"


def load_credentials(env_file: Path | None = None) -> Credentials:
    """Read the M2M credentials from the environment, falling back to ``.env``.

    Real environment variables win, so CI or a shell export can override the
    file without editing it.
    """
    if env_file is None:
        env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)

    values = {
        name: os.environ.get(name, "").strip()
        for name in ("AUTH0_DOMAIN", "AUTH0_M2M_CLIENT_ID", "AUTH0_M2M_CLIENT_SECRET")
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise ProvisioningError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Set them in {env_file} or the environment."
        )
    return Credentials(
        domain=values["AUTH0_DOMAIN"],
        client_id=values["AUTH0_M2M_CLIENT_ID"],
        client_secret=values["AUTH0_M2M_CLIENT_SECRET"],
    )


def fetch_management_token(credentials: Credentials, *, timeout: float = 30.0) -> str:
    """Exchange the M2M credentials for a Management API access token."""
    response = httpx.post(
        f"https://{credentials.domain}/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "audience": f"https://{credentials.domain}/api/v2/",
        },
        timeout=timeout,
    )
    if response.is_error:
        raise ProvisioningError(
            "Could not obtain a Management API token "
            f"(HTTP {response.status_code}): "
            f"{_redact(response.text, credentials.client_secret)}"
        )
    token = response.json().get("access_token")
    if not token:
        raise ProvisioningError("Auth0 returned no access_token for the M2M credentials.")
    return token


# --- Provisioning steps ----------------------------------------------------


def _list_all(
    client: ManagementClient,
    path: str,
    key: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Every object in a paginated collection.

    ``include_totals`` makes Auth0 return an envelope rather than a bare array,
    which is the only shape that reports where the collection ends.
    """
    items: list[dict[str, Any]] = []
    page = 0
    while True:
        query: dict[str, Any] = {"per_page": PAGE_SIZE, "page": page, "include_totals": "true"}
        if params:
            query.update(params)
        payload = client.get(path, query)
        batch = payload.get(key, []) if isinstance(payload, dict) else list(payload)
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        page += 1


def ensure_resource_server(client: ManagementClient, out: Callable[[str], None]) -> None:
    """Create or update the API that our access tokens are audienced to.

    ``identifier`` is immutable once set, which makes it the one natural key
    Auth0 guarantees will still match on the next run — so it is what we look
    up by, and it is omitted from the PATCH body.
    """
    desired = {
        "name": API_NAME,
        "signing_alg": "RS256",
        # RBAC. `enforce_policies` turns it on; `access_token_authz` is what
        # puts the granted permissions into the token, which is the only way a
        # resource server can act on them.
        "enforce_policies": True,
        "token_dialect": "access_token_authz",
        # Without this, Auth0 shows a consent screen for localhost callbacks
        # even though this is a first-party app.
        "skip_consent_for_verifiable_first_party_clients": True,
    }

    existing = _find(
        _list_all(client, "/resource-servers", "resource_servers"),
        lambda item: item.get("identifier") == API_IDENTIFIER,
    )
    if existing is None:
        out(f"API {API_IDENTIFIER}: not found, creating")
        client.post("/resource-servers", {**desired, "identifier": API_IDENTIFIER})
    else:
        out(f"API {API_IDENTIFIER}: found, updating")
        client.patch(f"/resource-servers/{existing['id']}", desired)


def ensure_spa_client(client: ManagementClient, out: Callable[[str], None]) -> str:
    """Create or update the browser application, returning its client id."""
    desired = {
        "name": SPA_NAME,
        "app_type": "spa",
        "callbacks": [SPA_ORIGIN],
        "allowed_logout_urls": [SPA_ORIGIN],
        "web_origins": [SPA_ORIGIN],
        "oidc_conformant": True,
        # Authorization code with PKCE plus refresh tokens: the current
        # recommendation for a SPA. `implicit` is deliberately absent.
        "grant_types": ["authorization_code", "refresh_token"],
        # A public client has nowhere to keep a secret, so it authenticates with
        # none. This is also why nothing below ever handles one.
        "token_endpoint_auth_method": "none",
    }

    existing = _find(
        _list_all(
            client,
            "/clients",
            "clients",
            {"fields": CLIENT_FIELDS, "include_fields": "true"},
        ),
        lambda item: item.get("name") == SPA_NAME,
    )
    if existing is None:
        out(f"Application {SPA_NAME}: not found, creating")
        created = client.post("/clients", desired)
        return str(created.get("client_id", DRY_RUN_CLIENT_ID))

    client_id = str(existing["client_id"])
    out(f"Application {SPA_NAME}: found ({client_id}), updating")
    client.patch(f"/clients/{client_id}", desired)
    return client_id


def ensure_connections(
    client: ManagementClient,
    spa_client_id: str,
    out: Callable[[str], None],
) -> bool:
    """Enable the database and Google connections for the SPA.

    Returns whether a ``google-oauth2`` connection existed. A tenant without one
    is reported, not treated as an error — see the module docstring.
    """
    connections = _list_all(
        client,
        "/connections",
        "connections",
        {"fields": CONNECTION_FIELDS, "include_fields": "true"},
    )

    database = _find(connections, lambda item: item.get("strategy") == DATABASE_STRATEGY)
    if database is None:
        # Every tenant ships with one, so its absence means someone deleted it.
        # Worth saying out loud, but it does not stop the rest of the run.
        out("WARNING: no database connection found — email/password login is unavailable.")
    else:
        _enable_client_on_connection(client, database, spa_client_id, out)

    google = _find(connections, lambda item: item.get("strategy") == GOOGLE_STRATEGY)
    if google is None:
        _warn_google_missing(out)
        return False

    _enable_client_on_connection(client, google, spa_client_id, out)
    return True


def _enabled_client_ids(client: ManagementClient, connection_id: str) -> list[str]:
    """The clients currently enabled on a connection.

    A connection's enabled clients are their own subresource on this tenant's
    API version — the ``connections`` records themselves no longer carry an
    ``enabled_clients`` array, and asking for that field is a 400. This endpoint
    also paginates by checkpoint (``take`` and a ``next`` cursor) rather than by
    page number, so it does not go through :func:`_list_all`.
    """
    ids: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"take": CONNECTION_CLIENTS_PAGE_SIZE}
        if cursor:
            params["from"] = cursor
        payload = client.get(f"/connections/{connection_id}/clients", params)
        ids.extend(entry["client_id"] for entry in payload.get("clients", []))
        cursor = payload.get("next")
        if not cursor:
            return ids


def _enable_client_on_connection(
    client: ManagementClient,
    connection: dict[str, Any],
    spa_client_id: str,
    out: Callable[[str], None],
) -> None:
    """Enable our client on a connection, if it is not already enabled.

    The PATCH body is a *delta* — only the clients whose status is changing —
    so unlike the older whole-array ``enabled_clients`` field there is no way to
    accidentally disable every other application on the connection.
    """
    name = connection.get("name", connection.get("strategy", "?"))
    connection_id = connection["id"]
    if spa_client_id in _enabled_client_ids(client, connection_id):
        out(f"Connection {name}: already enabled for {SPA_NAME}")
        return

    out(f"Connection {name}: enabling {SPA_NAME}")
    client.patch(
        f"/connections/{connection_id}/clients",
        [{"client_id": spa_client_id, "status": True}],
    )


def _warn_google_missing(out: Callable[[str], None]) -> None:
    out("")
    out("!" * 78)
    out("! WARNING: no 'google-oauth2' connection exists in this tenant.")
    out("! Google login is UNAVAILABLE. Email/password login is unaffected.")
    out("!")
    out("! This script cannot create the connection: doing so needs the")
    out("! 'create:connections' scope, which this M2M application has not been")
    out("! granted (it holds read:connections and update:connections only).")
    out("!")
    out("! To enable Google login: add the Google social connection in the Auth0")
    out("! dashboard (Authentication > Social), or grant 'create:connections' to")
    out("! the M2M application, then re-run this script.")
    out("!" * 78)
    out("")


def _find(
    items: Sequence[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    for item in items:
        if predicate(item):
            return item
    return None


def provision(
    client: ManagementClient,
    domain: str,
    *,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
) -> ProvisionResult:
    """Bring the tenant to the desired state and report what it now looks like.

    ``dry_run`` only changes the wording — whether writes actually happen is the
    client's business, so a caller cannot get a real run by forgetting the flag.
    """
    out(f"Provisioning Auth0 tenant {domain}" + (" (dry run — no changes)" if dry_run else ""))
    out("")

    ensure_resource_server(client, out)
    spa_client_id = ensure_spa_client(client, out)
    google_found = ensure_connections(client, spa_client_id, out)

    return ProvisionResult(
        domain=domain,
        audience=API_IDENTIFIER,
        spa_client_id=spa_client_id,
        google_connection_found=google_found,
    )


def print_env(result: ProvisionResult, out: Callable[[str], None] = print) -> None:
    """Print the configuration as paste-ready ``.env`` lines.

    The backend names are exactly the ones ``app/settings.py`` reads, so a
    mismatch between what we provisioned and what we verify against is not
    possible by transcription error.
    """
    out("")
    out("--- app/backend/.env " + "-" * 55)
    out(f"AUTH0_DOMAIN={result.domain}")
    out(f"AUTH0_API_AUDIENCE={result.audience}")
    out("")
    out("--- app/frontend/.env (task 2.8) " + "-" * 43)
    out(f"VITE_AUTH0_DOMAIN={result.domain}")
    out(f"VITE_AUTH0_CLIENT_ID={result.spa_client_id}")
    out(f"VITE_AUTH0_AUDIENCE={result.audience}")
    out("-" * 76)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently provision the Auth0 tenant for Open-Skej.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report what would change without changing it. Reads the tenant so "
            "it can tell you create from update; issues no POST or PATCH."
        ),
    )
    args = parser.parse_args(argv)

    http_client: HttpManagementClient | None = None
    try:
        credentials = load_credentials()
        token = fetch_management_token(credentials)
        http_client = HttpManagementClient(credentials.domain, token)
        client: ManagementClient = DryRunClient(http_client) if args.dry_run else http_client

        result = provision(client, credentials.domain, dry_run=args.dry_run)
        print_env(result)
    except ProvisioningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if http_client is not None:
            http_client.close()

    # A missing Google connection is reported, never fatal.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
