# Demo recordings

The GIFs referenced from the top-level README are generated from the `.tape`
scripts in this directory using [vhs](https://github.com/charmbracelet/vhs).

## Why scripts rather than checked-in recordings alone

A screenshot or hand-recorded GIF is a claim about behaviour with no way to
re-check it. It ages silently: the tool changes, the output changes, and the
recording keeps showing what the project used to do. That is the same failure
this repository keeps finding in its own instruments — evidence that cannot be
re-derived is not evidence.

Keeping the script in git means any demo can be regenerated on demand, and a
reviewer can read exactly what commands produced the picture.

## Producing the GIFs

```bash
go install github.com/charmbracelet/vhs@latest    # or: brew install vhs
cd docs/media

vhs ava-pipeline.tape        # -> ava-pipeline.gif
vhs bug-detect.tape          # -> bug-detect.gif
vhs voe-eligibility.tape     # -> voe-eligibility.gif
```

Run them from this directory; each tape `cd`s to the repository root itself.

## Known issue: vhs can exit 0 and write nothing

On at least one WSL2 setup, `vhs` printed `Creating <name>.gif...`, exited with
status **0**, and produced no file — with `ttyd 1.7.7` and `ffmpeg` both present
and on `PATH`. Writing to a Linux-native path instead of `/mnt/<drive>` made no
difference, so it is not a filesystem-permission problem.

The remaining suspect is `vhs`'s headless browser dependency: it renders the
terminal through go-rod, which needs a Chrome/Chromium it can drive. A headless
environment without one appears to fail without saying so.

If you hit this:

```bash
vhs ava-pipeline.tape >/dev/null; echo "exit=$?"   # 0 with no output file == this bug
ls -la *.gif
```

Try installing Chromium (`sudo apt install -y chromium-browser`) or run `vhs`
with `--debug`. If it still produces nothing, the demos are not worth blocking
on — the reproducibility tables in the top-level README carry the substance, and
they can be *checked* rather than watched.

Worth naming plainly, because it is the same defect class this repository exists
to study: **a tool reporting success while producing nothing.** An exit code of
0 is a claim, not evidence. The evidence is the file, and here there wasn't one.

## Tool requirements per tape

| Tape | Needs | Runtime |
|---|---|---|
| `ava-pipeline.tape` | nothing (pure Python path) | ~30 s |
| `bug-detect.tape` | nothing (uses the checked-in mutant fixtures) | ~20 s |
| `voe-eligibility.tape` | nothing — deliberately runs `--mock` | ~15 s |

All three avoid EDA tools on purpose, so anyone who clones the repository can
regenerate them. Demos of the Verilator and SymbiYosys paths would need the OSS
CAD Suite and are better shown as the reproducibility tables in the main README,
where the numbers can be checked rather than watched.

## Committing the output

GIFs are binary and they do bloat a repository's history. Keep them small:

```bash
# vhs already limits size, but if a GIF exceeds ~2 MB, trim it
vhs ava-pipeline.tape --output ava-pipeline.gif
ls -lh *.gif
```

If they grow past a few megabytes, host them in a release asset or a
`gh-pages` branch and link by URL instead.
