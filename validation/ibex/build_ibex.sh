#!/usr/bin/env bash
# Clone lowRISC Ibex at the pinned commit and build simple_system with
# Verilator.  Run from the repo root.  The clone and the build land under
# work/ (gitignored).  The Python venv for fusesoc lives outside the repo:
# tests/test_atomic_publish.py scans every .py under the tree, ignored or not.
set -euo pipefail
IBEX_COMMIT=041098fe2380124dea536a0985cb85469fb206c4
WORK=${WORK:-work}
IBEX=$WORK/ibex
VENV=${VENV:-$HOME/.cache/ava-ibex-venv}

if [ ! -d "$IBEX/.git" ]; then
  git clone --quiet https://github.com/lowRISC/ibex.git "$IBEX"
fi
git -C "$IBEX" checkout --quiet "$IBEX_COMMIT"
test "$(git -C "$IBEX" rev-parse HEAD)" = "$IBEX_COMMIT"

# The subset of Ibex's python-requirements.txt the simple_system build needs:
# fusesoc (which brings edalize) and what check_tool_requirements.py imports.
[ -x "$VENV/bin/python3" ] || /usr/bin/python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet "fusesoc==2.4.3" packaging pyyaml mako

# Ibex's pre-build check runs a bare `python3` and imports edalize, so the
# venv must come first on PATH, not just provide fusesoc.
export PATH="$(realpath "$VENV")/bin:$PATH"
cd "$IBEX"
# shellcheck disable=SC2046
time fusesoc --cores-root=. run --target=sim --setup --build \
  lowrisc:ibex:ibex_simple_system $(python3 util/ibex_config.py small fusesoc_opts)
ls -l build/lowrisc_ibex_ibex_simple_system_0/sim-verilator/Vibex_simple_system
