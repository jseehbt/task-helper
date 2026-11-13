#!/usr/bin/env python3
"""Map the files changed on this branch to the reviewer agents and checks that cover them.

Changed means: committed since the merge base with BASE, plus staged,
unstaged and untracked files. Each file goes to every reviewer whose pattern
matches; HINTS name checks a script or another skill performs instead of an
agent.

Usage:
    uv run --no-project python route_reviewers.py [--base REF] [--json]

Output (key=value):
    reviewer=NAME files=a,b
    hint=NAME files=a,b detail=...
    unrouted=PATH

Exit codes:
    0  files to review
    1  no changes
    2  not a git repository or unknown base
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# (agent, pattern on the repo-relative POSIX path). Order is the output order.
REVIEWERS = [
    # Domain rules: lifecycle, completeness, ChangeKey, status, principles. Not for tests - they are checked
    # against the spec through the code they exercise.
    ("spec-reviewer", re.compile(r"(^|/)TaskHelper\.(Core|App)/.*\.(cs|xaml)$")),
    ("dotnet-reviewer", re.compile(r"\.(cs|xaml|csproj|props|targets|slnx?)$|(^|/)\.editorconfig$")),
    ("docs-reviewer", re.compile(r"^(SPEC|README|AGENTS)\.md$|^docs/.*\.md$")),
]
HINTS = [
    ("fixture_check", re.compile(r"(^|/)fixtures?/", re.IGNORECASE),
     "uv run --no-project python .claude/skills/capture-fixture/scripts/fixture_check.py FILES"),
    ("ui-shots", re.compile(r"\.xaml$|(^|/)TaskHelper\.App/.*(Tray|Flyout|Icon|Theme|Dialog)[^/]*\.cs$"),
     "UI changed: run the ui-shots skill, then the ui-reviewer agent with the screenshots"),
    ("check_skill", re.compile(r"^\.claude/(skills|agents)/"),
     "skills/agents changed: hbt-skill-creator check_skill.py --strict on .claude/skills"),
    ("tooling_tests", re.compile(r"^\.claude/.*\.py$"),
     "uvx ruff check .claude && uv run --no-project python -m unittest discover -s .claude/tests"),
    ("github_actions", re.compile(r"^\.github/workflows/"),
     "workflow changed: review with the hbt-github-action-creator skill (actionlint, zizmor)"),
    ("spec_sync", re.compile(r"(^|/)TaskHelper\.(Core|App)/.*\.cs$|^docs/spike-"),
     "code or spike result changed: run the spec-sync skill before the pull request"),
]
# Files no reviewer needs: generated or binary.
IGNORED = re.compile(r"(^|/)(bin|obj)/|\.(png|ico|jpg|gif|dll|exe)$", re.IGNORECASE)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=False)


def changed_files(base: str) -> list[str]:
    merge_base = git("merge-base", base, "HEAD")
    if merge_base.returncode != 0:
        print(f"error=unknown_base detail={base}", file=sys.stderr)
        sys.exit(2)
    files: set[str] = set()
    for args in (("diff", "--name-only", merge_base.stdout.strip(), "HEAD"),
                 ("diff", "--name-only", "--cached"),
                 ("diff", "--name-only"),
                 ("ls-files", "--others", "--exclude-standard")):
        files.update(line for line in git(*args).stdout.splitlines() if line)
    return sorted(f for f in files if not IGNORED.search(f))


def route(files: list[str]) -> dict:
    reviewers = {name: [f for f in files if pattern.search(f)] for name, pattern in REVIEWERS}
    hints = {name: ([f for f in files if pattern.search(f)], detail) for name, pattern, detail in HINTS}
    covered = {f for fs in reviewers.values() for f in fs} | {f for fs, _ in hints.values() for f in fs}
    return {
        "reviewers": {k: v for k, v in reviewers.items() if v},
        "hints": {k: {"files": fs, "detail": d} for k, (fs, d) in hints.items() if fs},
        "unrouted": [f for f in files if f not in covered],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="main")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if git("rev-parse", "--git-dir").returncode != 0:
        print("error=not_a_repository", file=sys.stderr)
        return 2
    files = changed_files(args.base)
    if not files:
        print("status=no_changes")
        return 1
    result = route(files)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for name, fs in result["reviewers"].items():
        print(f"reviewer={name} files={','.join(fs)}")
    for name, hint in result["hints"].items():
        print(f"hint={name} files={','.join(hint['files'])} detail={hint['detail']}")
    for f in result["unrouted"]:
        print(f"unrouted={f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
