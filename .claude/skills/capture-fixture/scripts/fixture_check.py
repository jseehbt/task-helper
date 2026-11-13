#!/usr/bin/env python3
"""Scan test fixtures (or any text files) for data that must not be committed.

SPEC §11 requires anonymized fixtures, and nothing in the repository may hold
a token. This finds tokens, e-mail addresses outside example domains, hosts
of real tenants (a Slack workspace, a Jira site) and every term of the local,
untracked deny list - the real org, site, workspace and people names, which
no generic pattern can know.

Usage:
    uv run --no-project python fixture_check.py                 # all fixtures in the repository
    uv run --no-project python fixture_check.py PATH...         # these files or directories
    uv run --no-project python fixture_check.py --secrets-only PATH...

Exit codes:
    0  nothing found
    2  usage error (path not found, deny list neither UTF-8 nor UTF-16)
    3  findings, one `finding=` line each

The guard_commit hook imports `scan_text` from here.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Untracked on purpose (.gitignore): it lists exactly the real names that must
# not appear in the repository, so committing it would be the leak.
DENYLIST = Path(".claude") / "fixture-denylist.txt"

SECRET_PATTERNS = {
    "slack_token": re.compile(r"xox[abeoprs]-[A-Za-z0-9-]{10,}"),
    "github_token": re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,})"),
    "atlassian_token": re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer_header": re.compile(r"(?i)\b(?:authorization|bearer)\b[\"']?\s*[:=]?\s*[\"']?(?:bearer|basic)\s+[A-Za-z0-9+/=._-]{16,}"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# Tenant-looking host names, with or without a scheme (an e-mail domain is not a host).
HOST = re.compile(r"(?<![A-Za-z0-9@.-])((?:[A-Za-z0-9-]+\.)+(?:slack\.com|atlassian\.net|jira\.com|sharepoint\.com))"
                  r"(?![A-Za-z0-9-])", re.IGNORECASE)
# Domains reserved for examples (RFC 2606) plus the vendors' own service
# addresses, which identify no customer.
EXAMPLE_DOMAIN = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|invalid|example|test|localhost)$", re.IGNORECASE)
SERVICE_EMAIL_DOMAINS = {"users.noreply.github.com", "noreply.github.com"}
# Hosts whose first label names a real tenant unless it is the placeholder.
# The tenant is the label in front of the vendor domain (Slack Enterprise Grid: in front of `enterprise.`).
TENANT_HOST = re.compile(r"^(?:[A-Za-z0-9-]+\.)*?([A-Za-z0-9-]+)\.(?:enterprise\.)?"
                         r"(slack\.com|atlassian\.net|jira\.com|sharepoint\.com)$", re.IGNORECASE)
TENANT_PLACEHOLDERS = {"example", "app", "api", "files", "hooks", "slack", "www", "enterprise"}
FIXTURE_DIR = re.compile(r"(^|/)(fixtures?)(/|$)", re.IGNORECASE)
TEXT_SUFFIXES = {".json", ".jsonc", ".txt", ".md", ".cs", ".xml", ".xaml", ".http", ".yml", ".yaml", ".config",
                 ".csproj", ".props", ".py", ".ps1", ".cmd", ".sh"}


def load_denylist(root: Path) -> list[str]:
    path = root / DENYLIST
    if not path.is_file():
        return []
    terms = []
    for line in decode_denylist(path.read_bytes(), path).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def decode_denylist(data: bytes, path: Path) -> str:
    """UTF-8 with or without BOM, or UTF-16 as PowerShell 5 writes it (`>`, Out-File)."""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        if b"\x00" in data:
            return data.decode("utf-16-le")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.as_posix()} is neither UTF-8 nor UTF-16; save it as UTF-8") from exc


def mask(value: str) -> str:
    """Show enough to locate a finding without repeating a secret."""
    return value if len(value) <= 8 else f"{value[:4]}…{value[-2:]}"


def scan_text(text: str, denylist: list[str], secrets_only: bool = False) -> list[tuple[int, str, str]]:
    """Findings as (line, kind, masked value)."""
    findings: list[tuple[int, str, str]] = []
    lowered_terms = [(t, t.lower()) for t in denylist]
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS.items():
            for m in pattern.finditer(line):
                findings.append((number, kind, mask(m.group(0))))
        lowered = line.lower()
        for term, low in lowered_terms:
            if low in lowered:
                findings.append((number, "denylist", mask(term)))
        if secrets_only:
            continue
        for m in EMAIL.finditer(line):
            domain = m.group(1).lower()
            if not EXAMPLE_DOMAIN.search(domain) and domain not in SERVICE_EMAIL_DOMAINS:
                findings.append((number, "email", mask(m.group(0))))
        for m in HOST.finditer(line):
            tenant = TENANT_HOST.match(m.group(1))
            if tenant and tenant.group(1).lower() not in TENANT_PLACEHOLDERS:
                findings.append((number, "tenant_host", m.group(1)))
    return findings


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False)
    return Path(out.stdout.strip()) if out.returncode == 0 else Path.cwd()


def default_targets(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root, check=False)
    return [root / p for p in out.stdout.splitlines() if FIXTURE_DIR.search(p)]


def expand(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(p for p in sorted(path.rglob("*")) if p.is_file())
        elif path.is_file():
            files.append(path)
        else:
            print(f"error=path_not_found detail={path.as_posix()}", file=sys.stderr)
            sys.exit(2)
    return [f for f in files if f.suffix.lower() in TEXT_SUFFIXES]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--secrets-only", action="store_true",
                        help="only tokens and deny-list terms, not e-mails or hosts")
    args = parser.parse_args(argv)
    root = repo_root()
    try:
        denylist = load_denylist(root)
    except ValueError as exc:
        print(f"error=denylist_encoding detail={exc}", file=sys.stderr)
        return 2
    files = expand(args.paths) if args.paths else expand(default_targets(root))
    total = 0
    for file in files:
        text = file.read_text(encoding="utf-8", errors="replace")
        for line, kind, value in scan_text(text, denylist, args.secrets_only):
            total += 1
            print(f"finding={kind} file={file.as_posix()} line={line} value={value}")
    if not denylist:
        print(f"warning=no_denylist detail=create {DENYLIST.as_posix()} with the real org, site, workspace "
              "and people names, one per line")
    print(f"files={len(files)} findings={total}")
    return 3 if total else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
