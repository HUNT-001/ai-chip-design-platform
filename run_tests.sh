#!/usr/bin/env bash
# Deterministic test entry point — refuses to run partially.
#
# Two independent problems made `python3 -m pytest` unreliable here:
#
#  1. pytest auto-loads every plugin advertised by any package on the
#     interpreter's path. Sourcing the OSS CAD Suite environment puts its Python
#     first, which picks up ROS plugins from /opt/ros; one needs `yaml` (absent
#     there) and another registers a hook this pytest does not know. Blocking
#     them by name is a guessing game — the entry-point name differs from the
#     module name, and two attempts missed.
#
#  2. That same interpreter does NOT have pytest-asyncio. tests/test_agents.py
#     contains async tests, and without the plugin they do not fail — they
#     degrade to warnings and the suite still reports success. A green run
#     meaning "nothing was checked" is the exact failure this project keeps
#     finding in its own instruments.
#
# So: disable autoload entirely, ALLOW-LIST what is needed, pick an interpreter
# that actually has it, and EXIT NON-ZERO if none does. Refusing to run is a
# correct outcome; running a subset and calling it green is not.
set -euo pipefail
cd "$(dirname "$0")"

need() {   # does this interpreter have both pytest and pytest_asyncio?
    "$1" -c 'import pytest, pytest_asyncio' >/dev/null 2>&1
}

PY=""
for cand in /usr/bin/python3 python3 python3.12 python3.11; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if need "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
    echo "REFUSING TO RUN: no interpreter found with both pytest and pytest-asyncio." >&2
    echo >&2
    echo "  tests/test_agents.py contains async tests. Without pytest-asyncio they" >&2
    echo "  do not fail — they become warnings, and the suite reports green while" >&2
    echo "  silently skipping them. Running anyway would be worse than not running." >&2
    echo >&2
    echo "  Install it for the interpreter you intend to use, e.g.:" >&2
    echo "      /usr/bin/python3 -m pip install --break-system-packages pytest pytest-asyncio" >&2
    echo >&2
    echo "  (The OSS CAD Suite Python is only needed for sby/yosys/verilator," >&2
    echo "   not for this suite.)" >&2
    exit 1
fi

echo "test interpreter: $PY  ($("$PY" -c 'import sys;print(sys.version.split()[0])'))"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
exec "$PY" -m pytest -p pytest_asyncio.plugin "${@:-tests/}"
