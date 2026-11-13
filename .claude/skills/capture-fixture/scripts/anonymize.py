#!/usr/bin/env python3
"""Turn a raw API response into an anonymized test fixture.

Content fields (subjects, titles, message texts, names, logins) become
numbered placeholders - the same real value always gets the same placeholder,
so references between objects stay intact. E-mail addresses become
userN@example.com everywhere, also inside other strings. `--map REAL=FAKE`
replaces tenant values (org, site host, workspace, team ID) in every string,
URLs included. IDs, timestamps, states and flags stay as they are: the
adapter logic under test depends on them and they identify no person.

Usage:
    uv run --no-project python anonymize.py RAW.json OUT.json [--map REAL=FAKE ...]

A sidecar RAW.headers.json, if present, is anonymized the same way into
OUT.headers.json (Link headers carry the org name).

Exit codes:
    0  written
    2  usage error, input not found or not JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Keys whose string values are content or personal data. Lower-case; matched
# against the key name only, so the same key inside any object is covered.
CONTENT_KEYS = {
    "subject", "title", "summary", "body", "bodypreview", "preview", "text", "description", "comment",
    "name", "real_name", "display_name", "displayname", "first_name", "last_name", "login", "username",
    "fullname", "full_name", "topic", "purpose", "value", "real_name_normalized", "display_name_normalized",
    "title_html", "body_html", "body_text", "fallback", "pretext",
}
# Keys whose values are pictures of people; replaced by one neutral URL.
IMAGE_KEY = re.compile(r"^(avatar_url|gravatar_id|image_\w+|avatarurls|\d+x\d+)$", re.IGNORECASE)
# Keys that must keep their value even though their name looks like content:
# enum-like names the adapters switch on.
KEEP_KEYS = {"type", "subtype", "state", "state_reason", "status", "statuscategory", "flagstatus",
             "inferenceclassification", "importance", "key", "kind"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
NEUTRAL_IMAGE = "https://example.invalid/avatar.png"


class Anonymizer:
    def __init__(self, mapping: list[tuple[str, str]]):
        # Longest first, so "example-org-team" is not half-replaced by "example-org".
        self.mapping = sorted(mapping, key=lambda pair: len(pair[0]), reverse=True)
        self.emails: dict[str, str] = {}
        self.content: dict[str, str] = {}

    def email(self, match: re.Match) -> str:
        real = match.group(0).lower()
        if real.endswith(("@example.com", "@example.org", "@example.net")):
            return match.group(0)
        if real not in self.emails:
            self.emails[real] = f"user{len(self.emails) + 1}@example.com"
        return self.emails[real]

    def scrub(self, value: str) -> str:
        for real, fake in self.mapping:
            value = value.replace(real, fake)
        return EMAIL.sub(self.email, value)

    def placeholder(self, key: str, value: str) -> str:
        if not value:
            return value
        if value not in self.content:
            self.content[value] = f"{key.capitalize()} {len(self.content) + 1}"
        return self.content[value]

    def walk(self, node, key: str = ""):
        if isinstance(node, dict):
            return {k: self.walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(item, key) for item in node]
        if not isinstance(node, str):
            return node
        low = key.lower()
        if IMAGE_KEY.match(low):
            return NEUTRAL_IMAGE if node else node
        if low in CONTENT_KEYS and low not in KEEP_KEYS and not node.startswith(("http://", "https://")):
            return self.placeholder(low, node)
        return self.scrub(node)


def parse_map(pairs: list[str]) -> list[tuple[str, str]]:
    result = []
    for pair in pairs:
        if "=" not in pair or pair.startswith("="):
            print(f"error=bad_map detail={pair!r} expected REAL=FAKE", file=sys.stderr)
            sys.exit(2)
        real, fake = pair.split("=", 1)
        result.append((real, fake))
    return result


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"error=input_not_found detail={path.as_posix()}", file=sys.stderr)
    except json.JSONDecodeError as exc:
        print(f"error=not_json detail={path.as_posix()}:{exc.lineno}", file=sys.stderr)
    sys.exit(2)


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sidecar(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(".json") + ".headers.json")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("raw", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--map", action="append", default=[], metavar="REAL=FAKE")
    args = parser.parse_args(argv)
    anonymizer = Anonymizer(parse_map(args.map))
    write(args.out, anonymizer.walk(load(args.raw)))
    print(f"written={args.out.as_posix()}")
    if sidecar(args.raw).is_file():
        write(sidecar(args.out), anonymizer.walk(load(sidecar(args.raw))))
        print(f"written={sidecar(args.out).as_posix()}")
    print(f"emails={len(anonymizer.emails)} content_values={len(anonymizer.content)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
