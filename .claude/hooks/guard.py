#!/usr/bin/env python3
"""PreToolUse guard for Claude Code in this repository.

CLAUDE.md is guidance; this is enforcement. Each rule below exists because the
thing it blocks actually happened on this project and cost real work:

  * pushing/committing to main      -> PRs carried unrelated commits for days
  * git add -A / . / --renormalize  -> one "add a test" commit swept in 11 files,
                                       including unrelated VOE research
  * git push --force (bare)         -> use --force-with-lease, which refuses to
                                       overwrite work you have not seen
  * editing a committed pre-registration -> the VOE method rests on criteria
                                       being fixed before the data

Contract: Claude Code sends the pending tool call as JSON on stdin. Exit 0 lets
it run. Exit 2 blocks it, and stderr is shown to Claude as the reason.
"""
import json
import re
import subprocess
import sys


def block(reason: str) -> None:
    print(f"BLOCKED by .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def current_branch() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, timeout=10
                              ).stdout.strip()
    except Exception:
        return ""


def check_bash(cmd: str) -> None:
    # split on shell separators so `cd x && git add -A` is still caught
    for part in re.split(r"&&|\|\||;|\n", cmd):
        p = " ".join(part.split())

        if re.match(r"^git add\b", p):
            if re.search(r"(^| )(-A|--all|\.|--renormalize)( |$)", p):
                block("bulk `git add` is not allowed here. Add exact paths, then "
                      "check `git diff --cached --stat`. The working tree often "
                      "contains unrelated uncommitted work.")

        if re.match(r"^git push\b", p):
            args = p.split()[2:]
            if any(a in ("-f", "--force") for a in args):
                block("bare --force push. Use --force-with-lease.")
            if any(re.fullmatch(r"(\S+:)?(main|master)", a) for a in args):
                block("pushing to main is not allowed. Push a branch and open a PR.")
            positional = [a for a in args if not a.startswith("-")]
            if len(positional) < 2 and current_branch() in ("main", "master"):
                block("you are on main; this push would push main. "
                      "Create a branch first.")

        if re.match(r"^git commit\b", p) and current_branch() in ("main", "master"):
            block("committing directly on main. Create a branch: "
                  "`git checkout -b fix/<topic>`.")


def check_write(path: str) -> None:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if re.match(r"^(commit_.*|prereg_.*)\.json$", name):
        block(f"{name} is a pre-registration. Committed criteria are never "
              "edited; if the commitment was wrong, record that and register "
              "a new one with fresh seeds.")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)                    # never wedge the session on a parse error
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}
    if tool == "Bash":
        check_bash(inp.get("command", ""))
    elif tool in ("Edit", "Write", "MultiEdit"):
        check_write(inp.get("file_path", ""))
    sys.exit(0)


if __name__ == "__main__":
    main()
