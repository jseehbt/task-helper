#!/usr/bin/env python3
"""Map files changed on this branch to the documentation sections that describe them.

RULES maps path patterns to (document, sections). SPEC.md sections are given
by their number (`4.4`) and verified against the headings, so a renumbered
spec shows up as `stale_rule=` instead of silently pointing nowhere.

Usage:
    uv run --no-project python doc_targets.py [--base REF] [--issues] [--json]

Output:
    target=DOC sections=4.4,9 sources=a.cs,b.cs
    changed_doc=PATH                 (docs edited directly - check their claims too)
    unmapped=PATH                    (code no rule covers)
    stale_rule=DOC#SECTION           (a rule names a section that no longer exists)
    issue=N sections=4.4 title=...   (with --issues: open issues that link a target section)

Exit codes:
    0  targets found
    1  nothing to check
    2  not a git repository or unknown base
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SPEC = "SPEC.md"
# (pattern on repo-relative path, document, sections). A file may match several rules.
RULES = [
    (r"GitHub", SPEC, ["4.4"]), (r"GitHub", "docs/setup-github.md", []),
    (r"Jira", SPEC, ["4.3"]), (r"Jira", "docs/setup-jira.md", []),
    (r"Outlook|Graph|Msal", SPEC, ["4.1"]), (r"Outlook|Graph|Msal", "docs/setup-outlook.md", []),
    (r"Slack", SPEC, ["4.2"]), (r"Slack", "docs/setup-slack.md", []),
    (r"(Item|Reason|Key|Lifecycle|State|Store|Repository|Sqlite|Schema|Migration)[^/]*\.cs$", SPEC, ["3"]),
    (r"(Dismiss|Pin|Seen|Action|Undo)[^/]*\.cs$", SPEC, ["6"]),
    (r"(Scheduler|Poll|Backoff|Pause|WorkingHours|Session|Power)[^/]*\.cs$", SPEC, ["5"]),
    (r"(Status|Expir|Warning)[^/]*\.cs$", SPEC, ["7"]),
    (r"(Tray|Icon|Badge)[^/]*\.(cs|xaml)$", SPEC, ["8.1"]),
    (r"(Flyout|ItemRow|Filter)[^/]*\.(cs|xaml)$", SPEC, ["8.2"]),
    (r"(Token|Credential)[^/]*\.(cs|xaml)$", SPEC, ["8.3"]),
    (r"(Config|Options|Settings)[^/]*\.(cs|jsonc?)$", SPEC, ["9"]),
    (r"(Log|Logger)[^/]*\.cs$", SPEC, ["10"]),
    (r"(Autostart|SingleInstance|Mutex|Pipe)[^/]*\.cs$", SPEC, ["2"]),
    (r"\.(csproj|props|targets|slnx?)$|(^|/)\.editorconfig$|(^|/)global\.json$", SPEC, ["2"]),
    (r"\.(csproj|props|targets|slnx?)$|(^|/)\.editorconfig$|(^|/)global\.json$", "AGENTS.md", []),
    (r"Tests?/", SPEC, ["11"]),
    (r"^\.claude/", "AGENTS.md", []),
    (r"^docs/spike-", SPEC, ["13", "14"]),
]
DOC = re.compile(r"^(SPEC|README|AGENTS)\.md$|^docs/.*\.md$")
CODE = re.compile(r"\.(cs|xaml|csproj|props|targets|slnx?|jsonc?)$")
IGNORED = re.compile(r"(^|/)(bin|obj)/")
SPEC_REF = re.compile(r"§\s?(\d+(?:\.\d+)?)")
URL = re.compile(r"https?://[^\s)]+")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=False)


def changed_files(base: str) -> list[str]:
    merge_base = git("merge-base", base, "HEAD")
    if merge_base.returncode != 0:
        print(f"error=unknown_base detail={base}", file=sys.stderr)
        sys.exit(2)
    files: set[str] = set()
    for args in (("diff", "--name-only", merge_base.stdout.strip(), "HEAD"), ("diff", "--name-only", "--cached"),
                 ("diff", "--name-only"), ("ls-files", "--others", "--exclude-standard")):
        files.update(line for line in git(*args).stdout.splitlines() if line)
    return sorted(f for f in files if not IGNORED.search(f))


def spec_sections(root: Path) -> set[str]:
    path = root / SPEC
    if not path.is_file():
        return set()
    heads = re.findall(r"^#{2,3} (\d+(?:\.\d+)?)\.?\s", path.read_text(encoding="utf-8"), re.MULTILINE)
    return set(heads)


def map_files(files: list[str], known_sections: set[str]) -> dict:
    targets: dict[str, dict] = {}
    unmapped, changed_docs, stale = [], [], set()
    for f in files:
        if DOC.search(f):
            changed_docs.append(f)
        hit = False
        for pattern, doc, sections in RULES:
            if not re.search(pattern, f):
                continue
            hit = True
            entry = targets.setdefault(doc, {"sections": set(), "sources": set()})
            entry["sources"].add(f)
            for section in sections:
                if doc == SPEC and known_sections and section not in known_sections:
                    stale.add(f"{doc}#{section}")
                entry["sections"].add(section)
        if not hit and CODE.search(f) and not DOC.search(f):
            unmapped.append(f)
    for doc in list(targets):
        # A source change never needs a review of the very doc it is; changed docs are reviewed via changed_doc.
        targets[doc]["sources"] -= {doc}
        if not targets[doc]["sources"]:
            del targets[doc]
    return {
        "targets": {d: {"sections": sorted(v["sections"], key=lambda s: [int(x) for x in s.split(".")]),
                        "sources": sorted(v["sources"])} for d, v in targets.items()},
        "changed_docs": changed_docs, "unmapped": unmapped, "stale": sorted(stale),
    }


def affected_issues(sections: set[str]) -> list[dict]:
    out = subprocess.run(["gh", "issue", "list", "--state", "open", "--limit", "500", "--json", "number,title,body"],
                         capture_output=True, text=True, encoding="utf-8", check=False)
    if out.returncode != 0:
        print(f"warning=gh_failed detail={out.stderr.strip()[:200]}", file=sys.stderr)
        return []
    result = []
    for issue in json.loads(out.stdout):
        refs = set(SPEC_REF.findall(URL.sub("", issue["body"])))
        # §4 in an issue covers 4.4 in a target and vice versa.
        hits = {s for s in sections if any(s == r or s.startswith(r + ".") or r.startswith(s + ".") for r in refs)}
        if hits:
            result.append({"number": issue["number"], "title": issue["title"], "sections": sorted(hits)})
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="main")
    parser.add_argument("--issues", action="store_true", help="also list open issues linking a target section")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print("error=not_a_repository", file=sys.stderr)
        return 2
    result = map_files(changed_files(args.base), spec_sections(Path(top.stdout.strip())))
    if args.issues:
        spec_targets = set(result["targets"].get(SPEC, {}).get("sections", []))
        result["issues"] = affected_issues(spec_targets) if spec_targets else []
    if not result["targets"] and not result["changed_docs"]:
        print("status=nothing_to_check")
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    for doc, entry in result["targets"].items():
        print(f"target={doc} sections={','.join(entry['sections']) or '-'} sources={','.join(entry['sources'])}")
    for f in result["changed_docs"]:
        print(f"changed_doc={f}")
    for f in result["unmapped"]:
        print(f"unmapped={f}")
    for s in result["stale"]:
        print(f"stale_rule={s}")
    for issue in result.get("issues", []):
        print(f"issue={issue['number']} sections={','.join(issue['sections'])} title={issue['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
