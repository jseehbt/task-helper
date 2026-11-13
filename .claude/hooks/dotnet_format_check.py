#!/usr/bin/env python3
"""PostToolUse hook: check the whitespace formatting of an edited C# file.

`dotnet format --verify-no-changes` is part of the definition of done (SPEC
§11); catching a violation right after the edit is cheaper than at the end.
`whitespace --folder` needs no build and no solution, so it stays fast. It
only reports (exit 2 feeds the output back to Claude) and never rewrites the
file, so no line the edit did not touch changes behind Claude's back.
Only exit code 2 of `dotnet format` means a formatting finding; any other
failure is a broken tool and goes to the user as a note. Without a .NET SDK
the hook stays silent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SUFFIXES = {".cs"}
TIMEOUT_SECONDS = 60
FORMAT_FINDINGS = 2  # dotnet format --verify-no-changes: files would change


def has_dotnet_sdk() -> bool:
    """`which dotnet` also finds the bare host, which cannot run `dotnet format`."""
    if shutil.which("dotnet") is None:
        return False
    out = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, check=False)
    return out.returncode == 0 and bool(out.stdout.strip())


def main() -> int:
    payload = json.load(sys.stdin)
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    if Path(file_path).suffix.lower() not in SUFFIXES or not has_dotnet_sdk():
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    try:
        relative = Path(file_path).resolve().relative_to(root).as_posix()
    except ValueError:
        return 0
    try:
        result = subprocess.run(
            ["dotnet", "format", "whitespace", str(root), "--folder", "--verify-no-changes", "--include", relative],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS, cwd=root,
            check=False)
    except subprocess.TimeoutExpired:
        return 0
    if result.returncode == 0:
        return 0
    if result.returncode != FORMAT_FINDINGS:
        print(json.dumps({"systemMessage": f"dotnet format konnte {relative} nicht prüfen "
                                           f"(Exit {result.returncode})."}, ensure_ascii=False))
        return 0
    print(f"dotnet format meldet Formatierungsfehler in {relative} (beheben, nur die eigenen Zeilen):\n"
          f"{(result.stdout + result.stderr)[-2000:]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # hook JSON is UTF-8; Windows pipes default to cp1252
    sys.exit(main())
