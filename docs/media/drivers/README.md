# Demo rendering (asciinema + agg)

The `.tape` files in `docs/media/` are written for [vhs], but vhs drives a
headless Chromium and cannot render in every environment (e.g. a WSL box with
only a snap Chromium and no root). These driver scripts reproduce the same
demos with a **browser-free** toolchain — [asciinema] to record a terminal
session and [agg] to turn the recording into a GIF.

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

**Always verify a render with `file <gif>` (expect "GIF image data … WxH"), not
the exit code** — vhs in particular can exit 0 while writing nothing.

[vhs]: https://github.com/charmbracelet/vhs
[asciinema]: https://asciinema.org
[agg]: https://github.com/asciinema/agg
