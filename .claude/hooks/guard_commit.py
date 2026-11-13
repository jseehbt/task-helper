#!/usr/bin/env python3
"""PreToolUse hook (Bash): stop a `git commit` that breaks a project rule.

Blocks (exit 2, reason fed back to Claude) when
* the current branch is main or master - work happens on issue branches;
* the message carries a Co-Authored-By or tool footer - the user's commit rules forbid it;
* a file about to be committed contains a token or a deny-listed real name,
  or a fixture contains a real e-mail address or tenant host
  (capture-fixture/scripts/fixture_check.py does the scanning).

Files about to be committed are the staged ones, plus every modified tracked
file when the command uses -a/--all. A heuristic against accidents, not a
sandbox: a commit through another tool is not seen.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

COMMIT = re.compile(r"(^|[;&|]\s*|\s)git(\s+-C\s+\S+)?\s+commit\b")
ALL_FLAG = re.compile(r"\s(-[a-zA-Z]*a[a-zA-Z]*|--all)\b")
FOOTER = re.compile(r"Co-Authored-By:|Generated with \[?Claude", re.IGNORECASE)
FIXTURE = re.compile(r"(^|/)fixtures?/", re.IGNORECASE)
CHECKER = Path(".claude") / "skills" / "capture-fixture" / "scripts" / "fixture_check.py"


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         cwd=root, check=False)
    return out.stdout


def load_checker(root: Path):
    spec = importlib.util.spec_from_file_location("fixture_check", root / CHECKER)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def problems(command: str, root: Path) -> list[str]:
    found: list[str] = []
    branch = git(root, "branch", "--show-current").strip()
    if branch in {"main", "master"}:
        found.append(f"Commit auf '{branch}' ist nicht erlaubt - erst einen Issue-Branch anlegen (implement-issue: issue.py branch N).")
    if FOOTER.search(command):
        found.append("Commit-Nachricht ohne Co-Authored-By- oder Tool-Footer schreiben (Nutzerregel).")
    files = set(git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines())
    if ALL_FLAG.search(command):
        files |= set(git(root, "diff", "--name-only", "--diff-filter=ACMR").splitlines())
    checker = load_checker(root) if (root / CHECKER).is_file() else None
    if checker is None:
        return found
    denylist = checker.load_denylist(root)
    for path in sorted(f for f in files if f):
        if Path(path).suffix.lower() not in checker.TEXT_SUFFIXES:
            continue
        # The staged blob, not the working tree: that is what gets committed.
        text = git(root, "show", f":{path}")
        if not text and (root / path).is_file():
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        for line, kind, value in checker.scan_text(text, denylist, secrets_only=not FIXTURE.search(path)):
            found.append(f"{path}:{line} {kind} ({value})")
    return found


def main() -> int:
    payload = json.load(sys.stdin)
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not COMMIT.search(command):
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    found = problems(command, root)
    if not found:
        return 0
    print("Commit blockiert:\n- " + "\n- ".join(found[:20]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
