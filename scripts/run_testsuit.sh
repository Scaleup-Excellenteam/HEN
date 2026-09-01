#!/usr/bin/env bash
#
# Run the full Python test suite (pytest, everything under tests/).
#
#   ./scripts/run_testsuit.sh            run the suite
#   ./scripts/run_testsuit.sh -k corpus  forward extra arguments to pytest
#
# Installs requirements-dev.txt first unless --skip-install is given. Exits
# non-zero if any test fails, so it can gate a change the way CI does.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKIP_INSTALL=0
ARGS=()

# The project's own interpreter when there is one, so the script works against
# the environment run.sh and the README set up rather than whatever python3
# happens to be first on PATH.
if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
else
    PYTHON="python3"
fi

for arg in "$@"; do
    case "$arg" in
        --skip-install)
            SKIP_INSTALL=1
            ;;
        *)
            ARGS+=("$arg")
            ;;
    esac
done

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
    echo "Installing test dependencies..."
    "$PYTHON" -m pip install -q -r requirements-dev.txt
fi

echo "Running pytest..."
# ${ARGS[@]+...} rather than a bare "${ARGS[@]}": under `set -u`, bash 3.2 —
# which is what macOS ships — treats an empty array as unset and aborts, so
# running this with no arguments at all would fail before reaching pytest.
"$PYTHON" -m pytest ${ARGS[@]+"${ARGS[@]}"}
