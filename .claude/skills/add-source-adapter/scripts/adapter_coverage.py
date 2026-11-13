#!/usr/bin/env python3
"""Report which places a TaskHelper source adapter has to reach are still missing.

A source touches the adapter, the item key, reasons, completeness, error
mapping, config, credential, token check, tests, fixtures and the setup doc
(SPEC §3-§9). The checks search the repository by content instead of fixed
paths, so they keep working whatever the project layout of issue 4 turns out
to be; a hit in an unexpected file is reported with its path.

Usage:
    uv run --no-project python adapter_coverage.py github|jira|outlook|slack [--json]

Output: one `check=NAME status=ok|missing where=PATH` line per check, then
`result=complete|incomplete`.

Exit codes:
    0  complete
    2  usage error or not inside the repository
    3  something missing
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CORE = re.compile(r"(^|/)TaskHelper\.Core/.*\.cs$")
APP = re.compile(r"(^|/)TaskHelper\.App/.*\.(cs|xaml)$")
TESTS = re.compile(r"(^|/)TaskHelper\.[A-Za-z.]*Tests/.*\.cs$")
FIXTURES = re.compile(r"(^|/)TaskHelper\.[A-Za-z.]*Tests/.*fixtures?/.*\.json$", re.IGNORECASE)
CONFIG_TEMPLATE = re.compile(r"(^|/)TaskHelper\.(Core|App)/.*config[^/]*\.jsonc?$", re.IGNORECASE)

# Per source: the class-name fragment, reasons (SPEC §3), the token-check call
# (SPEC §8.3) and literals the spec requires the adapter to use (SPEC §4.x).
SOURCES = {
    "github": {
        "class": "GitHub",
        "reasons": ["IssueAssigned", "PrAssigned", "ReviewRequested"],
        "credential": True,
        "token_check": r"[\"']/?user[\"']",
        "literals": {
            "search_endpoint": r"search/issues",
            "per_page_100": r"per_page=100",
            "incomplete_results": r"incomplete_results",
            "token_expiration_header": r"github-authentication-token-expiration",
            "archived_false": r"archived:false",
            "draft_flag": r"IsDraft",
        },
    },
    "jira": {
        "class": "Jira",
        "reasons": ["JqlMatch"],
        "credential": True,
        "token_check": r"rest/api/3/myself",
        "literals": {
            "search_jql_endpoint": r"rest/api/3/search/jql",
            "next_page_token": r"nextPageToken",
            "fields": r"summary,status,updated,project,issuetype",
        },
    },
    "outlook": {
        "class": "Outlook",
        "reasons": ["Flagged", "UnreadFocused"],
        "credential": False,
        "token_check": r"[\"']/?me[\"'/?]",
        "literals": {
            "immutable_id": r"ImmutableId",
            "next_link": r"@odata\.nextLink",
            "inference_classification": r"inferenceClassification",
            "deleted_items": r"deleteditems",
            "junk_email": r"junkemail",
            "ui_required": r"MsalUiRequiredException",
            "parent_window": r"WithParentActivityOrWindow",
        },
    },
    "slack": {
        "class": "Slack",
        "reasons": ["Mention", "DirectMessage", "ChannelUnread"],
        "credential": True,
        "token_check": r"auth\.test",
        "literals": {
            "search_messages": r"search\.messages",
            "conversations_replies": r"conversations\.replies",
            "conversations_history": r"conversations\.history",
            "auth_error_codes": r"token_revoked",
            "overflow_key": r"overflow/",
            "highlight": r"<!here>|<!channel>|<!everyone>",
        },
    },
}


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         encoding="utf-8", check=False)
    if out.returncode != 0:
        print("error=not_a_repository", file=sys.stderr)
        sys.exit(2)
    return Path(out.stdout.strip())


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, encoding="utf-8", cwd=root, check=False)
    return [p for p in out.stdout.splitlines() if p]


class Repo:
    def __init__(self, root: Path, files: list[str]):
        self.root = root
        self.files = files
        self._text: dict[str, str] = {}

    def text(self, path: str) -> str:
        if path not in self._text:
            try:
                self._text[path] = (self.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[path] = ""
        return self._text[path]

    def grep(self, scope: re.Pattern, pattern: str, flags: int = 0) -> str | None:
        regex = re.compile(pattern, flags)
        return next((p for p in self.files if scope.search(p) and regex.search(self.text(p))), None)

    def find(self, scope: re.Pattern, name_pattern: str) -> str | None:
        regex = re.compile(name_pattern, re.IGNORECASE)
        return next((p for p in self.files if scope.search(p) and regex.search(Path(p).name)), None)


def checks(source: str, repo: Repo) -> list[tuple[str, str | None]]:
    spec = SOURCES[source]
    cls = spec["class"]
    adapter = next((p for p in repo.files if CORE.search(p) and cls.lower() in Path(p).name.lower()
                    and "PollAsync" in repo.text(p)), None)
    in_adapter = re.compile(re.escape(adapter) + "$") if adapter else re.compile(r"(?!)")
    results: list[tuple[str, str | None]] = [
        ("adapter_with_PollAsync", adapter),
        ("item_key_prefix", repo.grep(CORE, rf"[\"$]+{source}:")),
        ("is_complete", repo.grep(in_adapter, r"IsComplete")),
        ("auth_required_mapping", repo.grep(in_adapter, r"AuthRequired")),
        ("retry_after", repo.grep(CORE, r"Retry-?After", re.IGNORECASE)),
        ("config_section", repo.grep(CORE, rf"class\s+{cls}\w*(Config|Options|Settings)\b|\"{source}\"")),
        ("config_template_section", repo.grep(CONFIG_TEMPLATE, rf"\"{source}\"\s*:")),
        ("token_check_call", repo.grep(re.compile(r"(^|/)TaskHelper\.(Core|App)/.*\.cs$"), spec["token_check"])),
        ("tests", repo.find(TESTS, cls)),
        ("fixtures", next((p for p in repo.files if FIXTURES.search(p) and source in p.lower()), None)),
        ("setup_doc", f"docs/setup-{source}.md" if f"docs/setup-{source}.md" in repo.files else None),
    ]
    for reason in spec["reasons"]:
        results.append((f"reason_{reason}", repo.grep(CORE, rf"\b{reason}\b")))
    if spec["credential"]:
        results.append(("credential_name", repo.grep(APP, rf"TaskHelper:{source}")))
    for name, pattern in spec["literals"].items():
        results.append((name, repo.grep(CORE, pattern)))
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", choices=sorted(SOURCES))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = repo_root()
    results = checks(args.source, Repo(root, tracked_files(root)))
    missing = [name for name, where in results if where is None]
    if args.json:
        print(json.dumps({"source": args.source, "checks": dict(results), "missing": missing}, indent=2))
    else:
        for name, where in results:
            print(f"check={name} status={'ok' if where else 'missing'}" + (f" where={where}" if where else ""))
        print(f"result={'incomplete' if missing else 'complete'} missing={len(missing)}")
    return 3 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
