#!/usr/bin/env bash
#
# Build the project and run it, including the browser interface.
#
# Anything already running is stopped first, so this can be run repeatedly and
# always leaves one API and one web server, both freshly built.
#
#   ./run.sh                 build everything and serve the built interface
#   ./run.sh --dev           serve the interface from the development server
#   ./run.sh --stop          stop whatever is running and exit
#   ./run.sh --rebuild-index discard the cached index and build it again
#   ./run.sh --skip-install  trust the installed dependencies
#   ./run.sh --help
#
# The command line, python main.py, needs none of this.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

API_PORT=8000
DEV_PORT=5173
PREVIEW_PORT=4173
LOG_DIR="$ROOT/.run"

MODE="serve"       # serve | dev | stop
REBUILD_INDEX=0
SKIP_INSTALL=0

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1m==>\033[0m %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
fail() { printf '\n\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# The comment block at the top of this file is the help text, printed by
# stripping the leading "#" up to the first line that is not a comment.
usage() {
  awk 'NR > 2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) MODE="dev" ;;
    --stop) MODE="stop" ;;
    --rebuild-index) REBUILD_INDEX=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------- stopping ---

# Stop by port rather than by process name: it finds whatever is actually
# holding the port, including a server started by hand in another terminal.
stop_port() {
  local port="$1" what="$2" pids
  pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0

  info "stopping ${what} on port ${port} (pid $(echo "$pids" | tr '\n' ' '))"
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 30); do
    sleep 0.2
    lsof -ti "tcp:${port}" >/dev/null 2>&1 || return 0
  done

  info "  it did not stop, forcing"
  kill -9 $pids 2>/dev/null || true
  sleep 0.3
}

stop_everything() {
  stop_port "$API_PORT" "the API"
  stop_port "$DEV_PORT" "the development server"
  stop_port "$PREVIEW_PORT" "the web server"
}

if [[ "$MODE" == "stop" ]]; then
  step "Stopping"
  stop_everything
  info "stopped"
  exit 0
fi

# Stop the servers this script started when it is interrupted, so Ctrl-C does
# not leave orphans holding the ports.
API_PID=""
WEB_PID=""
shutdown() {
  trap - INT TERM EXIT
  printf '\n'
  step "Stopping"
  [[ -n "$WEB_PID" ]] && kill "$WEB_PID" 2>/dev/null || true
  [[ -n "$API_PID" ]] && kill "$API_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  info "stopped"
  exit 0
}

# ------------------------------------------------------------ requirements ---

step "Checking what is needed"

command -v node >/dev/null || fail "node is not installed; the interface needs Node 20 or newer"
command -v npm  >/dev/null || fail "npm is not installed"
info "node $(node --version)"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null; then
  step "Creating a virtual environment in .venv"
  python3 -m venv "$ROOT/.venv"
  PYTHON="$ROOT/.venv/bin/python"
  SKIP_INSTALL=0
else
  fail "python3 is not installed"
fi
info "python $("$PYTHON" --version | cut -d' ' -f2) from ${PYTHON#$ROOT/}"

CORPUS="$("$PYTHON" - <<'PY'
from autocomplete.config import load_default_config
print(load_default_config().corpus_root)
PY
)" || fail "config.yaml could not be read"

[[ -d "$CORPUS" ]] || fail "the corpus directory does not exist: ${CORPUS}
       Set corpus_root in config.yaml to the extracted text files."
info "corpus ${CORPUS}"

# ------------------------------------------------------------------ build ---

step "Stopping anything already running"
stop_everything

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  step "Installing dependencies"
  "$PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
  info "python packages ready"
  (cd web && npm install --silent --no-fund --no-audit)
  info "node packages ready"
else
  step "Skipping dependency installation"
fi

step "Preparing the search index"
# PYTHONUNBUFFERED keeps the progress lines coming as the build makes them:
# through a pipe Python would otherwise hold them until it exits.
if [[ "$REBUILD_INDEX" -eq 1 ]]; then
  PYTHONUNBUFFERED=1 "$PYTHON" main.py --rebuild | sed 's/^/    /'
else
  # build_or_load reuses a cached index only while the corpus fingerprint still
  # matches, so this is a full build whenever anything has changed.
  PYTHONUNBUFFERED=1 "$PYTHON" main.py --build | sed 's/^/    /'
fi

mkdir -p "$LOG_DIR"

if [[ "$MODE" == "serve" ]]; then
  step "Building the interface"
  rm -rf web/dist
  (cd web && npm run build) | sed 's/^/    /'
  WEB_PORT="$PREVIEW_PORT"
  WEB_COMMAND=(npm run preview -- --port "$PREVIEW_PORT" --strictPort)
  WEB_WHAT="the built interface"
else
  WEB_PORT="$DEV_PORT"
  WEB_COMMAND=(npm run dev -- --port "$DEV_PORT" --strictPort)
  WEB_WHAT="the development server"
fi

# ----------------------------------------------------------------- running ---

step "Starting"
trap shutdown INT TERM EXIT

"$PYTHON" -m uvicorn autocomplete.web:create_app --factory \
  --port "$API_PORT" --log-level warning >"$LOG_DIR/api.log" 2>&1 &
API_PID=$!

wait_for() {
  local url="$1" what="$2" pid="$3" tries="$4"
  for _ in $(seq 1 "$tries"); do
    kill -0 "$pid" 2>/dev/null || { cat "$LOG_DIR"/*.log >&2; fail "${what} exited while starting"; }
    curl -sf "$url" >/dev/null 2>&1 && return 0
    sleep 0.4
  done
  fail "${what} did not come up; see ${LOG_DIR#$ROOT/}"
}

wait_for "http://127.0.0.1:${API_PORT}/api/health" "the API" "$API_PID" 60
info "API on http://127.0.0.1:${API_PORT}"

(cd web && "${WEB_COMMAND[@]}") >"$LOG_DIR/web.log" 2>&1 &
WEB_PID=$!
wait_for "http://127.0.0.1:${WEB_PORT}/" "${WEB_WHAT}" "$WEB_PID" 60

# The index is prepared in the background, so say whether it is usable yet
# rather than implying the first search will answer at once.
READY="$(curl -sf "http://127.0.0.1:${API_PORT}/api/health" | tr -d ' ' | grep -o '"ready":[a-z]*' | cut -d: -f2)"

printf '\n'
bold "Ready."
info "open        http://localhost:${WEB_PORT}"
info "API         http://127.0.0.1:${API_PORT}/api/health"
info "logs        ${LOG_DIR#$ROOT/}/api.log, ${LOG_DIR#$ROOT/}/web.log"
if [[ "$READY" != "true" ]]; then
  info "the index is still being prepared; the page will start working on its own"
fi
info "press Ctrl-C to stop, or run ./run.sh --stop from another terminal"

wait
