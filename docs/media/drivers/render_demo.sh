#!/usr/bin/env bash
# Usage: render_demo.sh <name> <driver.sh> [out_dir]
# Records <driver.sh> in a 100x40 pty and renders <out_dir>/<name>.gif with agg.
# Requires: a python with asciinema (set ASCIINEMA_PYTHON if `python3` lacks it),
# and `agg` on PATH. Verify output with `file`, never the exit code.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="$1"; drv="$2"; out="${3:-/tmp}"
cast="$out/$name.cast"; gif="$out/$name.gif"
mkdir -p "$out"; rm -f "$cast" "$gif"
DRIVER="$drv" CAST="$cast" "${ASCIINEMA_PYTHON:-python3}" "$HERE/recsize.py" >/dev/null 2>&1
agg --font-size 16 --theme asciinema "$cast" "$gif" >/dev/null 2>&1
printf '%-18s ' "$name"; file "$gif"
