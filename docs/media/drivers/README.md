# Demo rendering (asciinema + agg)

The `.tape` files in `docs/media/` are the declarative source of record for
each demo, written for [vhs]. vhs is Chromium-bound, and in this WSL
environment it does not work at all: it **exits 0 and writes a zero-byte
GIF** — a silent failure, not an error. These driver scripts reproduce the
same demos with a **browser-free** toolchain — [asciinema] to record a
terminal session and [agg] to turn the recording into a GIF.

They are the scripts that actually produced the published GIFs.

## Requirements
- `asciinema` available to some Python (`python3 -m asciinema`); set
  `ASCIINEMA_PYTHON` to that interpreter if plain `python3` lacks it.
- `agg` on `PATH`.

## Render one
```bash
ASCIINEMA_PYTHON=python3 bash docs/media/drivers/render_demo.sh \
    verdict-exit-code docs/media/drivers/driver_verdict.sh /tmp
```
Produces `/tmp/verdict-exit-code.gif`. Render all four by repeating with
`driver_ava.sh` → `ava-pipeline`, `driver_bug.sh` → `bug-detect`,
`driver_voe.sh` → `voe-eligibility`.

## How it works
- `recsize.py` records the driver inside a fixed **100x40** pseudo-terminal, so
  the cast size is stable even with no controlling tty (asciinema otherwise
  defaults to 80x24 and the longer demos scroll).
- `render_demo.sh` records then runs `agg --font-size 16 --theme asciinema`, the
  shared look for the whole set.
- Each driver runs the **real** commands only — same binaries, same output as a
  normal run. They derive the repo root from their own location and regenerate
  their own scratch inputs, so they work from a clean clone with no EDA tools.

**Verify every render with `file <gif>` — expect `GIF image data, version 89a,
WxH`. Never trust the exit code**: vhs exits 0 while writing a zero-byte GIF, so
a "successful" run tells you nothing about whether a usable GIF exists.

[vhs]: https://github.com/charmbracelet/vhs
[asciinema]: https://asciinema.org
[agg]: https://github.com/asciinema/agg
