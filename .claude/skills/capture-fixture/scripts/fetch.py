#!/usr/bin/env python3
"""Make ONE read-only GET against a real account and save the raw response outside the repository.

Only for capturing test fixtures, and only after the user approved exactly
this call (SPEC §11). The token comes from the Windows Credential Manager
entry the app uses (TaskHelper:github, TaskHelper:jira, TaskHelper:slack), so
it never appears in a command line, a transcript or a log. The response body
is written to disk and never printed - it holds real names and texts until
anonymize.py has run.

Usage (Windows only):
    uv run --no-project python fetch.py github "https://api.github.com/search/issues?q=..." OUT_DIR NAME
    uv run --no-project python fetch.py jira   "https://SITE.atlassian.net/rest/api/3/search/jql?..." OUT_DIR NAME --email ADDRESS
    uv run --no-project python fetch.py slack  "https://slack.com/api/conversations.info?channel=C123" OUT_DIR NAME

Outlook is not supported: Graph tokens live in the WAM broker, not in the
Credential Manager. Export the response with Graph Explorer instead.

Writes OUT_DIR/NAME.json and OUT_DIR/NAME.headers.json (only the headers the
adapters read). OUT_DIR must lie outside the repository.

Exit codes:
    0  saved
    2  usage error (host not allowed, OUT_DIR inside the repository, missing --email)
    3  HTTP error, redirect (refused, so the token never reaches another host)
       or Slack ok:false (status printed, body not)
    4  not on Windows, or no credential stored for the source
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "github": lambda host: host == "api.github.com",
    "jira": lambda host: host.endswith(".atlassian.net"),
    "slack": lambda host: host == "slack.com",
}
# Headers the adapters evaluate (paging, token expiry, rate limits); nothing else is kept.
KEEP_HEADERS = {"link", "github-authentication-token-expiration", "retry-after", "x-ratelimit-remaining",
                "x-ratelimit-reset", "x-ratelimit-limit", "content-type"}
TIMEOUT_SECONDS = 30  # the same request timeout the app uses (SPEC §5)


def read_credential(target: str) -> str | None:
    """Password of a generic Credential Manager entry, via CredReadW."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(CREDENTIAL)()
    CRED_TYPE_GENERIC = 1
    if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return None
    try:
        cred = pointer.contents
        return decode_blob(ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize))
    finally:
        advapi.CredFree(pointer)


def decode_blob(blob: bytes) -> str:
    """A credential blob as text: UTF-16LE (.NET/Windows default) or UTF-8.

    Tokens are ASCII, so a UTF-16LE blob has a zero in every odd byte; a UTF-8
    blob of even length would otherwise decode to printable CJK characters.
    """
    if blob and len(blob) % 2 == 0 and all(b == 0 for b in blob[1::2]):
        return blob.decode("utf-16-le")
    return blob.decode("utf-8")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would resend the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def inside_repo(path: Path) -> bool:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False)
    if out.returncode != 0:
        return False
    root = Path(out.stdout.strip()).resolve()
    return path.resolve() == root or root in path.resolve().parents


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", choices=sorted(ALLOWED_HOSTS))
    parser.add_argument("url")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("name")
    parser.add_argument("--email", help="Jira account e-mail (Basic auth)")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("error=windows_only detail=the token is read from the Windows Credential Manager", file=sys.stderr)
        return 4
    host = (urlparse(args.url).hostname or "").lower()
    if urlparse(args.url).scheme != "https" or not ALLOWED_HOSTS[args.source](host):
        print(f"error=host_not_allowed detail={host} for {args.source}", file=sys.stderr)
        return 2
    if inside_repo(args.out_dir):
        print("error=out_dir_in_repo detail=raw responses hold real data; use a directory outside the repository",
              file=sys.stderr)
        return 2
    if args.source == "jira" and not args.email:
        print("error=missing_email detail=jira needs --email for Basic auth", file=sys.stderr)
        return 2

    token = read_credential(f"TaskHelper:{args.source}")
    if not token:
        print(f"error=no_credential detail=TaskHelper:{args.source} not in Credential Manager; save it in the "
              "token dialog first", file=sys.stderr)
        return 4
    if args.source == "jira":
        auth = "Basic " + base64.b64encode(f"{args.email}:{token}".encode()).decode()
    else:
        auth = f"Bearer {token}"
    request = urllib.request.Request(args.url, method="GET", headers={
        "Authorization": auth, "Accept": "application/json", "User-Agent": "TaskHelper-fixture-capture"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status, headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        hint = " redirect refused; call the target URL directly" if 300 <= exc.code < 400 else ""
        print(f"error=http_status detail={exc.code}{hint}", file=sys.stderr)
        return 3

    data = json.loads(body.decode("utf-8"))
    if args.source == "slack" and isinstance(data, dict) and data.get("ok") is False:
        print(f"error=slack_not_ok detail={data.get('error')}", file=sys.stderr)
        return 3
    args.out_dir.mkdir(parents=True, exist_ok=True)
    body_path = args.out_dir / f"{args.name}.json"
    header_path = args.out_dir / f"{args.name}.headers.json"
    body_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kept = {k.lower(): v for k, v in headers.items() if k.lower() in KEEP_HEADERS}
    header_path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    print(f"status={status} raw={body_path.as_posix()} headers={header_path.as_posix()} bytes={len(body)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
