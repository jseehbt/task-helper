#!/usr/bin/env python3
"""Turn a raw API response into an anonymized test fixture.

Content fields (subjects, titles, message texts incl. Graph `body.content`,
names) become numbered placeholders - the same real value always gets the same
placeholder, so references between objects stay intact. E-mail addresses
become userN@example.com everywhere, also inside other strings. Logins,
usernames and Slack team domains become loginN/usernameN/domainN, also inside
URLs (`https://github.com/<login>`) and every other string. `--map REAL=FAKE`
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
    "name", "real_name", "display_name", "displayname", "first_name", "last_name",
    "fullname", "full_name", "topic", "purpose", "value", "real_name_normalized", "display_name_normalized",
    "title_html", "body_html", "body_text", "fallback", "pretext", "content",
}
# Keys whose values are account handles that also appear inside URLs and other
# strings; they are replaced everywhere, not only in their own field.
IDENTITY_KEYS = {"login", "username", "domain"}
# Shorter identities ("ab", "api") are replaced in their own field only: inside
# other strings they would hit unrelated words. main() warns about them.
MIN_GLOBAL_IDENTITY = 4
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
        self.identities: dict[str, str] = {}
        self.originals: dict[str, str] = {}
        self.short_identities: set[str] = set()
        self.identity_counts: dict[str, int] = {}
        self.identity_pattern: re.Pattern | None = None

    def identity(self, key: str, value: str) -> str:
        low = value.lower()
        if low not in self.identities:
            mapped = next((fake for real, fake in self.mapping if real.lower() == low), None)
            if mapped is None:
                self.identity_counts[key] = self.identity_counts.get(key, 0) + 1
                mapped = f"{key}{self.identity_counts[key]}"
            self.identities[low] = mapped
        if value not in self.originals:  # every spelling ("JDoe", "jdoe") of one identity
            self.originals[value] = self.identities[low]
            if len(value) < MIN_GLOBAL_IDENTITY:
                self.short_identities.add(value)
            else:
                # Case-sensitive, whole path segment or host label only: "ab" must not turn "ab-fix" or "AB-12" over.
                alternatives = "|".join(re.escape(real) for real in sorted(
                    (o for o in self.originals if len(o) >= MIN_GLOBAL_IDENTITY), key=len, reverse=True))
                self.identity_pattern = re.compile(rf"(?<![A-Za-z0-9_.-])(?:{alternatives})(?![A-Za-z0-9_-])")
        return self.identities[low]

    def collect(self, node, key: str = "") -> None:
        """Register every identity first, so a URL earlier in the document is covered too."""
        if isinstance(node, dict):
            for k, v in node.items():
                self.collect(v, k)
        elif isinstance(node, list):
            for item in node:
                self.collect(item, key)
        elif isinstance(node, str) and node and key.lower() in IDENTITY_KEYS and "/" not in node:
            self.identity(key.lower(), node)

    def email(self, match: re.Match) -> str:
        real = match.group(0).lower()
        if real.endswith(("@example.com", "@example.org", "@example.net")):
            return match.group(0)
        if real not in self.emails:
            self.emails[real] = f"user{len(self.emails) + 1}@example.com"
        return self.emails[real]

    def apply_map(self, value: str) -> str:
        for real, fake in self.mapping:
            value = value.replace(real, fake)
        return value

    def scrub(self, value: str) -> str:
        value = EMAIL.sub(self.email, self.apply_map(value))
        if self.identity_pattern is not None:
            value = self.identity_pattern.sub(lambda m: self.originals[m.group(0)], value)
        return value

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
        if low in KEEP_KEYS:
            # Enum-like values and issue keys the adapters switch on: only --map, so "ACME-12" follows ".../EX-12".
            return self.apply_map(node)
        if IMAGE_KEY.match(low):
            return NEUTRAL_IMAGE if node else node
        if low in IDENTITY_KEYS and node and "/" not in node:
            return self.identity(low, node)
        if low in CONTENT_KEYS and not node.startswith(("http://", "https://")):
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
    raw = load(args.raw)
    headers = load(sidecar(args.raw)) if sidecar(args.raw).is_file() else None
    anonymizer.collect(raw)
    anonymizer.collect(headers)
    write(args.out, anonymizer.walk(raw))
    print(f"written={args.out.as_posix()}")
    if headers is not None:
        write(sidecar(args.out), anonymizer.walk(headers))
        print(f"written={sidecar(args.out).as_posix()}")
    print(f"emails={len(anonymizer.emails)} identities={len(anonymizer.identities)} "
          f"content_values={len(anonymizer.content)}")
    if anonymizer.short_identities:
        print(f"warning=short_identities count={len(anonymizer.short_identities)} detail=replaced only in their own "
              "field; check URLs and texts in the output, or pass --map REAL=FAKE")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
