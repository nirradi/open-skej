#!/usr/bin/env bash
#
# Bring the whole stack up from scratch for manual fiddling.
#
#   scripts/dev-sandbox.sh            wipe the database, migrate, seed, start both servers
#   scripts/dev-sandbox.sh --keep-data   same, but keep the Postgres volume (see the note below)
#   scripts/dev-sandbox.sh --force       kill whatever holds ports 8000/5173 instead of aborting
#   scripts/dev-sandbox.sh --stop        stop the servers this script started (Postgres stays up)
#   scripts/dev-sandbox.sh --stop --all  also `docker compose down` (add --wipe to drop the volume)
#
# ## Sandbox auth, not the real tenant
#
# The seeded identities (`app/backend/app/sandbox_seed.py`) carry `sandbox|*` subs that only exist
# in sandbox auth mode, so this script runs the stack with `SANDBOX_AUTH=true` /
# `VITE_SANDBOX_AUTH=true` and no hosted login page at all. Both sides refuse to start with a real
# Auth0 tenant configured alongside the sandbox switch — a process trusting either credential
# source is the bypass that guard exists to prevent — and `app/backend/.env` and
# `app/frontend/.env` in this checkout do configure one. So this script exports the `AUTH0_*` /
# `VITE_AUTH0_*` names as **empty** for the two child processes only: an environment variable
# outranks a `.env` entry on both sides, and both treat empty-after-trim as unset. Neither file is
# touched, and nothing outside these two processes sees the override.
#
# ## `--keep-data` keeps the volume, not your rows
#
# `app.sandbox_seed` resets its own fixtures before replanting them, by design — that is what makes
# it re-runnable. So `--keep-data` skips the Docker volume drop (a container restart, not a data
# guarantee) and every Space is still rebuilt with a **fresh `public_id`**: an old link stops
# resolving either way. The flag is for skipping the slow path, not for keeping bookings.
#
# ## Processes, not a foreground babysitter
#
# The servers are detached and their pids recorded under `.dev-sandbox/`, so the terminal is free
# once the script prints its summary. Logs stream to `.dev-sandbox/{backend,frontend}.log`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$REPO_ROOT/.dev-sandbox"

BACKEND_DIR="$REPO_ROOT/app/backend"
FRONTEND_DIR="$REPO_ROOT/app/frontend"
VENV_PY="$BACKEND_DIR/venv/bin/python"

BACKEND_PORT=8000
FRONTEND_PORT=5173
POSTGRES_PORT=5432

# Compose derives the project name from the directory, which is what stops two worktrees naming the
# same container — but they still publish the same host port, so a sibling checkout's Postgres is a
# real collision on this machine and one that would otherwise be found only after the wipe. Compose
# normalizes the basename to lowercase alphanumerics; mirror that here.
COMPOSE_PROJECT="$(basename "$REPO_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]_-')"

# The compose service's development-only credentials (docker-compose.yml).
DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://skej:skej@localhost:5432/skej}"

# Rule generation is off by default in the backend and its routes are registered
# conditionally, so a sandbox without this switch serves a genuine 404 and the
# rules page's authoring panel hides itself — absent, not broken, which is right
# in production and useless here. This stack exists to exercise the product, so
# it opts in. The client stays `stub` unless asked otherwise: it answers with a
# canned valid rule, deterministically, with no network call and no money spent.
# `RULE_GENERATION_CLIENT=google ./scripts/dev-sandbox.sh` drives a real model
# instead, and finds its key in `rules/.env` without this script handling it.
RULE_GENERATION_CLIENT="${RULE_GENERATION_CLIENT:-stub}"

# Empty means "whichever model the selected client defaults to", which is the
# only correct answer here: a model id belongs to one backend, so a value this
# script picked would be wrong for every client but the one it was chosen for.
RULE_GENERATION_MODEL="${RULE_GENERATION_MODEL:-}"

KEEP_DATA=false
FORCE=false
STOP=false
STOP_ALL=false
WIPE_VOLUME=false

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-data) KEEP_DATA=true ;;
    --force) FORCE=true ;;
    --stop) STOP=true ;;
    --all) STOP_ALL=true ;;
    --wipe) WIPE_VOLUME=true ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

mkdir -p "$RUN_DIR"

# --- process handling ---------------------------------------------------------

pid_file() { printf '%s/%s.pid' "$RUN_DIR" "$1"; }

# The pid of whatever is listening on a port, or nothing. `-sTCP:LISTEN` matters:
# without it this also matches the browser tab connected to the dev server.
port_pid() { lsof -ti :"$1" -sTCP:LISTEN 2>/dev/null || true; }

stop_recorded() {
  local name pid
  for name in backend frontend; do
    pid="$(cat "$(pid_file "$name")" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      info "stopping $name (pid $pid)"
      # Negative pid: uvicorn --reload and vite both fork children, and killing
      # only the parent leaves the child holding the port.
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
    rm -f "$(pid_file "$name")"
  done
}

# Shutdown is not instant: uvicorn drains, and vite's child takes a beat to release the socket.
# Both the restart path and `--stop` have to outlast that, or the next thing either does — rebinding
# the port, or the user's own command — meets a port that is already on its way out.
wait_port_free() {
  local port="$1" deadline
  deadline=$(( $(date +%s) + 10 ))
  while [ -n "$(port_pid "$port")" ] && [ "$(date +%s)" -lt "$deadline" ]; do sleep 0.5; done
}

# Wait for a port to fall silent, then take it by force if asked to.
claim_port() {
  local port="$1" label="$2" pid
  wait_port_free "$port"

  pid="$(port_pid "$port")"
  [ -z "$pid" ] && return 0

  if [ "$FORCE" = true ]; then
    info "killing $(ps -p "$pid" -o comm= 2>/dev/null || echo pid) on port $port (--force)"
    kill -TERM $pid 2>/dev/null || true
    sleep 2
    pid="$(port_pid "$port")"
    [ -n "$pid" ] && kill -KILL $pid 2>/dev/null || true
    return 0
  fi

  die "port $port ($label) is held by pid $pid ($(ps -p "$pid" -o command= 2>/dev/null | cut -c1-70)).
       That is not a process this script started, so it is left alone — it is most likely your own
       dev server pointed at your own data. Stop it, or re-run with --force to kill it."
}

# Refuse to run against a Postgres that belongs to another checkout.
#
# This is checked *before* the wipe, not after: `docker compose up` fails on the bind anyway, but by
# then this script has already dropped its own volume — and if the sibling container were stopped
# and a native Postgres held 5432 instead, compose would fail while every later step happily seeded
# whatever database answers on `localhost:5432`. Neither outcome is one a fiddling session should
# discover by reading a stack trace.
claim_postgres_port() {
  local holder project pid
  holder="$(docker ps --filter "publish=$POSTGRES_PORT" --format '{{.Names}}	{{.Label "com.docker.compose.project"}}' | head -n 1)"
  project="${holder#*	}"
  holder="${holder%%	*}"

  if [ -n "$holder" ]; then
    [ "$project" = "$COMPOSE_PROJECT" ] && return 0
    if [ "$FORCE" = true ]; then
      info "stopping container $holder (compose project '$project') to free port $POSTGRES_PORT (--force)"
      docker stop "$holder" >/dev/null
      return 0
    fi
    die "port $POSTGRES_PORT is published by container '$holder', from compose project '$project' —
       another checkout of this repository, not this one ('$COMPOSE_PROJECT'). Its database is not
       the one this script would seed, and nothing here has been changed yet. Stop it with
       \`docker stop $holder\`, or re-run with --force to have this script stop it for you."
  fi

  pid="$(port_pid "$POSTGRES_PORT")"
  if [ -n "$pid" ]; then
    die "port $POSTGRES_PORT is held by pid $pid ($(ps -p "$pid" -o command= 2>/dev/null | cut -c1-70)),
       which is not a Docker container. Compose cannot bind the port, and every step after it would
       seed whatever database that process serves. Stop it before re-running."
  fi
}

# --- --stop -------------------------------------------------------------------

if [ "$STOP" = true ]; then
  bold "Stopping the sandbox stack"
  stop_recorded
  wait_port_free "$BACKEND_PORT"
  wait_port_free "$FRONTEND_PORT"
  if [ "$STOP_ALL" = true ]; then
    if [ "$WIPE_VOLUME" = true ]; then
      info "docker compose down -v"
      (cd "$REPO_ROOT" && docker compose down -v)
    else
      info "docker compose down"
      (cd "$REPO_ROOT" && docker compose down)
    fi
  else
    info "Postgres left running (--all to stop it too, --all --wipe to drop its data)"
  fi
  exit 0
fi

# --- preflight ----------------------------------------------------------------

bold "Preflight"
command -v docker >/dev/null || die "docker not found on PATH"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running — start Docker Desktop first"
[ -x "$VENV_PY" ] || die "backend venv missing. Create it:
       python3 -m venv app/backend/venv && app/backend/venv/bin/pip install -r app/backend/requirements.txt"
[ -d "$FRONTEND_DIR/node_modules" ] || die "frontend dependencies missing. Run: npm ci --prefix app/frontend"
# The rule engine is an editable install of the sibling tree (`-e ../../rules` in
# requirements.txt), so it is the one dependency a venv can be missing while looking complete —
# and `app.routers.resource_bookings` imports it, so the failure lands as a uvicorn traceback
# 60 seconds into the run rather than here.
"$VENV_PY" -c 'import rules' >/dev/null 2>&1 || die "the backend venv cannot import the \`rules\` engine.
       It is installed from the sibling tree, not an index. Run:
         (cd app/backend && ./venv/bin/pip install -e ../../rules)"
info "docker, backend venv and frontend deps all present"

stop_recorded
claim_port "$BACKEND_PORT" backend
claim_port "$FRONTEND_PORT" frontend
claim_postgres_port

# --- database -----------------------------------------------------------------

cd "$REPO_ROOT"
if [ "$KEEP_DATA" = true ]; then
  bold "Postgres (keeping existing data)"
  docker compose up -d
else
  bold "Postgres (fresh — the named volume is dropped)"
  docker compose down -v
  docker compose up -d
fi

printf '  waiting for postgres to accept connections'
deadline=$(( $(date +%s) + 90 ))
until docker compose exec -T postgres pg_isready -U skej -d skej >/dev/null 2>&1; do
  [ "$(date +%s)" -lt "$deadline" ] || { printf '\n'; die "postgres did not become ready within 90s"; }
  printf '.'
  sleep 1
done
printf ' ready\n'

bold "Schema and seed"
export DATABASE_URL
(cd "$BACKEND_DIR" && "$VENV_PY" -m alembic upgrade head)
# The explicit-id-1 booking defaults, then the deterministic sandbox fixtures:
# two Spaces on different schedules, four identities, a pending access request,
# a pending invitation, an archived Space and a couple of future bookings. Both
# are idempotent, so --keep-data re-seeds rather than duplicating.
(cd "$BACKEND_DIR" && "$VENV_PY" -m app.db.bootstrap)
(cd "$BACKEND_DIR" && "$VENV_PY" -m app.sandbox_seed)
info "migrated and seeded"

# --- servers ------------------------------------------------------------------

bold "Servers"

# setsid-equivalent: `set -m` puts each background job in its own process group,
# which is what lets stop_recorded kill a whole tree by negated pid.
set -m

(
  cd "$BACKEND_DIR"
  exec env \
    DATABASE_URL="$DATABASE_URL" \
    SANDBOX_AUTH=true \
    AUTH0_DOMAIN= \
    AUTH0_API_AUDIENCE= \
    RULE_GENERATION_ENABLED=true \
    RULE_GENERATION_CLIENT="$RULE_GENERATION_CLIENT" \
    RULE_GENERATION_MODEL="$RULE_GENERATION_MODEL" \
    "$VENV_PY" -m uvicorn app.main:app --reload --port "$BACKEND_PORT"
) >"$RUN_DIR/backend.log" 2>&1 &
echo $! >"$(pid_file backend)"

(
  cd "$FRONTEND_DIR"
  exec env \
    VITE_SANDBOX_AUTH=true \
    VITE_AUTH0_DOMAIN= \
    VITE_AUTH0_CLIENT_ID= \
    VITE_AUTH0_AUDIENCE= \
    npm run dev -- --port "$FRONTEND_PORT" --strictPort
) >"$RUN_DIR/frontend.log" 2>&1 &
echo $! >"$(pid_file frontend)"

set +m

wait_for_http() {
  local url="$1" label="$2" log="$3" deadline
  printf '  waiting for %s' "$label"
  deadline=$(( $(date +%s) + 60 ))
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      printf '\n'
      tail -n 30 "$log" >&2
      die "$label did not answer within 60s — full log at $log"
    fi
    printf '.'
    sleep 1
  done
  printf ' up\n'
}

wait_for_http "http://localhost:$BACKEND_PORT/" backend "$RUN_DIR/backend.log"
wait_for_http "http://localhost:$FRONTEND_PORT/" frontend "$RUN_DIR/frontend.log"

# --- what got seeded ----------------------------------------------------------
#
# Read back rather than restated: a `public_id` is generated per run, so the only
# way to hand out a working link is to ask the database that was just seeded.

bold "Seeded Spaces"
(cd "$BACKEND_DIR" && "$VENV_PY" - <<'PY'
from app.db.bootstrap import DEFAULT_SPACE_PUBLIC_ID
from app.db.session import get_session_factory
from app.identity.models import Resource, Space, SpaceRule


def clock(minutes):
    """Render minutes-from-local-midnight as a local wall clock.

    Both availability bounds are minutes from the Space's own local midnight, so
    a venue closing after midnight is an ordinary value above 1440 rather than an
    inverted pair. That gets a day marker rather than wrapping silently, which
    would print a venue open 09:00-01:30 as if it shut before it opened.
    """
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}" + (
        "+1d" if minutes >= 1440 else ""
    )


with get_session_factory()() as session:
    spaces = (
        session.query(Space)
        # The bootstrap Space is the explicit-id-1 booking target `app.db.bootstrap`
        # replants: nobody is a member of it and nothing books through it since the
        # unscoped POST /bookings was deleted. Listing it would offer a link that
        # only ever renders the cold-link door.
        .filter(Space.public_id != DEFAULT_SPACE_PUBLIC_ID)
        .order_by(Space.id)
        .all()
    )
    for space in spaces:
        state = " [archived]" if space.archived_at else ""
        # A Space's schedule is `space_rules` rows, so it is read back from
        # them. Only the unscoped rows are summarised here: an `applies_to`
        # row is per-weekday or per-date and has no one line to print, and
        # this listing exists to hand out working links, not to render the
        # rules page.
        rules = {
            rule.rule_type: rule.params
            for rule in session.query(SpaceRule)
            .filter(
                SpaceRule.space_id == space.id,
                SpaceRule.applies_to.is_(None),
                SpaceRule.enabled.is_(True),
            )
            .order_by(SpaceRule.id)
            .all()
        }
        availability = rules.get("availability_hours")
        hours = (
            f"{clock(availability['opens_at_minutes'])}-"
            f"{clock(availability['closes_at_minutes'])} local"
            if availability
            else "no availability window"
        )
        alignment = rules.get("slot_alignment")
        slots = f"{alignment['slot_minutes']}m slots" if alignment else "default slot size"
        print(f"\n  {space.name}{state}")
        print(f"    {space.timezone} · {hours} · {slots}")
        print(f"    http://localhost:5173/s/{space.public_id}")
        resources = (
            session.query(Resource)
            .filter(Resource.space_id == space.id, Resource.archived_at.is_(None))
            .order_by(Resource.id)
            .all()
        )
        for resource in resources:
            print(
                f"      {resource.name}: "
                f"http://localhost:5173/s/{space.public_id}/resources/{resource.id}"
            )
PY
)

cat <<EOF

$(bold "Signing in")
  There is no hosted login page in sandbox mode. Pick an identity in the browser console at
  http://localhost:5173, then reload and press the sign-in button:

    localStorage.setItem('skej.sandbox.sub', 'sandbox|owner')   // owner of both Spaces
    localStorage.setItem('skej.sandbox.sub', 'sandbox|admin')   // admin of Space A only
    localStorage.setItem('skej.sandbox.sub', 'sandbox|member')  // member of Space A only
    localStorage.setItem('skej.sandbox.sub', 'sandbox|stranger')// no membership; pending request

  Unset it and the owner is assumed. Sign out with the app's own control, or:

    localStorage.removeItem('skej.sandbox.signedIn')

  A tab left open across a restart lands on that same sign-in card: the backend generates its
  sandbox keypair fresh on every start and never persists it, so the token the tab was holding is
  no longer signed by anything. Press sign-in again and reload.

$(bold "URLs")
  App        http://localhost:$FRONTEND_PORT/
  API        http://localhost:$BACKEND_PORT/
  Swagger    http://localhost:$BACKEND_PORT/docs

$(bold "Logs and shutdown")
  tail -f .dev-sandbox/backend.log
  tail -f .dev-sandbox/frontend.log
  scripts/dev-sandbox.sh --stop              # servers down, Postgres up
  scripts/dev-sandbox.sh --stop --all --wipe # everything down, data dropped
EOF
