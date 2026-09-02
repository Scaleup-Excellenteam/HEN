#!/usr/bin/env bash
#
# Tear the project back down to a fresh checkout.
#
# Stops anything running, then removes every generated artifact: the virtual
# environment, the installed node packages, the built interface, the search
# index cache, test and build caches, and the logs. Nothing under version
# control is touched, and neither is the corpus, so the next ./run.sh does the
# whole build from scratch in front of you.
#
#   ./teardown.sh            stop everything and remove all generated files
#   ./teardown.sh --dry-run  list what would be removed, remove nothing
#   ./teardown.sh --deep     also purge the pip and npm download caches
#   ./teardown.sh --help
#
# --deep makes the next build re-download every package rather than unpacking
# it from the local download cache. It is slower, and it affects other projects
# on this machine, so it is not the default.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

API_PORT=8000
DEV_PORT=5173
PREVIEW_PORT=4173

DRY_RUN=0
DEEP=0
REMOVED_KB=0

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
    --dry-run|-n) DRY_RUN=1 ;;
    --deep) DEEP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# Refuse to run anywhere but this project: everything below is rm -rf, and a
# script that deletes .venv and node_modules must never run in a stray tree.
[[ -f "$ROOT/main.py" && -d "$ROOT/autocomplete" ]] ||
  fail "this does not look like the project directory: $ROOT"

# --------------------------------------------------------------- removing ---

# Read a path setting straight out of config.yaml. The virtual environment is
# about to be deleted, so this cannot go through the Python config loader; the
# two keys it reads are plain scalars, and a missing one falls back to the same
# default the loader uses.
config_path() {
  local key="$1" fallback="$2" value
  value="$(sed -n "s/^${key}:[[:space:]]*//p" "$ROOT/config.yaml" 2>/dev/null |
           head -1 | sed 's/[[:space:]]*#.*$//' | tr -d '"'"'"' \r')"
  [[ -z "$value" ]] && value="$fallback"
  [[ "$value" = /* ]] || value="$ROOT/$value"
  # Collapse any ".." so the guard below compares the same path the Python
  # config loader would report.
  [[ -d "$value" ]] && value="$(cd "$value" && pwd)"
  printf '%s\n' "$value"
}

CACHE_DIR="$(config_path cache_dir .cache)"
CORPUS_DIR="$(config_path corpus_root corpus)"

# Removing the corpus would mean re-extracting Archive.zip by hand, which is not
# what a teardown is for. Anything resolving to it, or to the project root, is a
# misconfiguration rather than something to delete.
guarded() {
  local target
  target="$(cd "$(dirname "$1")" 2>/dev/null && pwd)/$(basename "$1")" || return 1
  [[ "$target" == "$ROOT" || "$target" == "$CORPUS_DIR" ]]
}

remove() {
  local target="$1" label="${2:-}" kb shown
  [[ -e "$target" ]] || return 0
  if guarded "$target"; then
    info "refusing to remove ${target} (the corpus or the project root)"
    return 0
  fi

  kb="$(du -sk "$target" 2>/dev/null | cut -f1)"
  kb="${kb:-0}"
  REMOVED_KB=$((REMOVED_KB + kb))
  shown="${target#$ROOT/}"
  [[ -n "$label" ]] && shown="${shown}  (${label})"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "would remove ${shown}"
  else
    rm -rf "$target"
    info "removed ${shown}"
  fi
}

# Remove every match of a find expression, skipping trees already removed above.
remove_matching() {
  local description="$1"; shift
  local found=0 path
  while IFS= read -r path; do
    remove "$path"
    found=1
  done < <(find "$ROOT" \
             \( -path "$ROOT/.venv" -o -path "$ROOT/web/node_modules" -o -path "$ROOT/.git" \) -prune \
             -o "$@" -print 2>/dev/null | sort)
  [[ "$found" -eq 0 ]] && info "no ${description} to remove"
  return 0
}

# --------------------------------------------------------------- stopping ---

stop_port() {
  local port="$1" what="$2" pids
  pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0

  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "would stop ${what} on port ${port} (pid $(echo "$pids" | tr '\n' ' '))"
    return 0
  fi

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

if [[ "$DRY_RUN" -eq 1 ]]; then
  bold "Dry run: nothing will be stopped or deleted."
fi

step "Stopping anything running"
stop_port "$API_PORT" "the API"
stop_port "$DEV_PORT" "the development server"
stop_port "$PREVIEW_PORT" "the web server"

# ------------------------------------------------------------------ python ---

step "Removing the Python environment"
remove "$ROOT/.venv" "virtual environment"
remove "$ROOT/venv"

step "Removing the search index cache"
remove "$CACHE_DIR" "built index, rebuilt on the next run"
remove "$ROOT/cache"
remove "$ROOT/artifacts"
remove "$ROOT/prototypes/artifacts"

step "Removing Python caches"
remove "$ROOT/.pytest_cache"
remove "$ROOT/.hypothesis"
remove_matching "bytecode caches" -type d -name __pycache__
remove_matching "packaging metadata" -type d -name '*.egg-info'

# -------------------------------------------------------------------- node ---

step "Removing the web build and its packages"
remove "$ROOT/web/node_modules" "installed node packages"
remove "$ROOT/web/dist" "built interface"
remove "$ROOT/web/coverage"
remove "$ROOT/web/.vite"
remove_matching "TypeScript build info" -type f -name '*.tsbuildinfo'

# ------------------------------------------------------- logs and results ---

step "Removing logs and generated results"
remove "$ROOT/.run" "run.sh logs"
remove "$ROOT/benchmarks/results" "benchmark output"

# ----------------------------------------------------------- download caches ---

if [[ "$DEEP" -eq 1 ]]; then
  step "Purging download caches"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "would purge the pip cache and the npm cache"
  else
    if command -v python3 >/dev/null; then
      python3 -m pip cache purge >/dev/null 2>&1 && info "pip cache purged" ||
        info "pip cache not purged (nothing cached, or pip unavailable)"
    fi
    if command -v npm >/dev/null; then
      npm cache clean --force >/dev/null 2>&1 && info "npm cache cleaned" ||
        info "npm cache not cleaned"
    fi
  fi
else
  step "Keeping the pip and npm download caches"
  info "packages will be unpacked from the local cache instead of downloaded"
  info "pass --deep to purge those too"
fi

# ------------------------------------------------------------------- done ---

printf '\n'
if [[ "$DRY_RUN" -eq 1 ]]; then
  bold "Dry run finished. $((REMOVED_KB / 1024)) MB would be freed."
  info "run ./teardown.sh to actually remove it"
else
  bold "Torn down. $((REMOVED_KB / 1024)) MB freed."
  info "the corpus at ${CORPUS_DIR} was left alone"
  info "run ./run.sh to build everything again from scratch"
fi
