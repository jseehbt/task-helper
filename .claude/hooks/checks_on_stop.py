#!/usr/bin/env python3
"""Stop hook: build and test before Claude ends a turn with changed files.

Two independent checks, each only when its files changed and only once per
state of those changes (a hash is stored in the git directory after a green
run, so a turn that changed nothing costs nothing):

* .NET: `dotnet build` (warnings are errors) and `dotnet test`, when C#,
  XAML, project or fixture files differ from HEAD and a solution exists.
* Tooling: the unittest suite in .claude/tests, when a Python file under
  .claude, a SKILL.md, .claude/settings.json or SPEC.md changed (the tests
  read them).

Build output (bin/, obj/, TestResults/) never counts as a change, otherwise
every build would invalidate the stored hash.

A failure exits 2, which makes Claude continue and fix it; stop_hook_active
prevents a loop. A missing tool or .NET SDK is reported to the user, not to
Claude - it is not a test failure. The step timeouts add up to 960 s; the
hook timeout in .claude/settings.json must stay above that.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Everything under .claude/ is agent tooling, never part of the .NET build.
DOTNET_FILES = re.compile(r"^(?!\.claude/)(.*\.(cs|xaml|csproj|props|targets|slnx?|json|resx)|(.*/)?\.editorconfig)$",
                          re.IGNORECASE)
TOOLING_FILES = re.compile(r"^(\.claude/.*\.py|\.claude/skills/.*\.md|\.claude/settings\.json|SPEC\.md)$")
BUILD_OUTPUT = re.compile(r"(^|/)(bin|obj|TestResults)/", re.IGNORECASE)
BUILD_TIMEOUT = 420
TEST_TIMEOUT = 420
TOOLING_TIMEOUT = 120


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], capture_output=True, check=False)


def has_dotnet_sdk() -> bool:
    """`which dotnet` also finds the bare host, which cannot build anything."""
    if shutil.which("dotnet") is None:
        return False
    out = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, check=False)
    return out.returncode == 0 and bool(out.stdout.strip())


def changed(pattern: re.Pattern) -> list[str]:
    tracked = git("diff", "--name-only", "HEAD").stdout.decode("utf-8", errors="replace").splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").stdout.decode("utf-8", errors="replace").splitlines()
    return sorted(p for p in set(tracked) | set(untracked)
                  if p and pattern.search(p) and not BUILD_OUTPUT.search(p))


def fingerprint(files: list[str]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.encode("utf-8"))
        file = Path(path)
        if file.is_file():
            digest.update(file.read_bytes())
    return digest.hexdigest()


def marker(name: str) -> Path:
    return Path(git("rev-parse", "--git-path", name).stdout.decode("utf-8").strip())


def is_green(name: str, files: list[str]) -> bool:
    path = marker(name)
    return path.is_file() and path.read_text(encoding="utf-8").strip() == fingerprint(files)


def remember_green(name: str, files: list[str]) -> None:
    marker(name).write_text(fingerprint(files), encoding="utf-8")


def run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd)}: timeout after {timeout}s"
    return result.returncode == 0, (result.stdout + result.stderr)[-3000:]


def check_dotnet(messages: list[str], notes: list[str]) -> None:
    files = changed(DOTNET_FILES)
    if not files or not any(Path(".").glob("*.sln*")) or is_green("claude-dotnet-green", files):
        return
    if not has_dotnet_sdk():
        notes.append("Build/Tests nicht gelaufen: kein .NET SDK gefunden (dotnet --list-sdks).")
        return
    ok, output = run(["dotnet", "build", "--nologo", "-v", "q"], BUILD_TIMEOUT)
    if not ok:
        messages.append(f"dotnet build schlägt fehl (Warnungen sind Fehler):\n{output}")
        return
    ok, output = run(["dotnet", "test", "--nologo", "--no-build", "-v", "q"], TEST_TIMEOUT)
    if not ok:
        messages.append(f"dotnet test schlägt fehl:\n{output}")
        return
    remember_green("claude-dotnet-green", files)


def check_tooling(messages: list[str], notes: list[str]) -> None:
    files = changed(TOOLING_FILES)
    if not files or is_green("claude-tooling-green", files):
        return
    if shutil.which("uv") is None:
        notes.append("Tooling-Tests nicht gelaufen: uv nicht gefunden.")
        return
    ok, output = run(["uv", "run", "--no-project", "python", "-m", "unittest", "discover", "-s", ".claude/tests",
                      "-q"], TOOLING_TIMEOUT)
    if not ok:
        messages.append(f"Tests der Agent-Skripte (.claude/tests) schlagen fehl:\n{output}")
        return
    remember_green("claude-tooling-green", files)


def main() -> int:
    payload = json.load(sys.stdin)
    if payload.get("stop_hook_active") or git("rev-parse", "--show-toplevel").returncode != 0:
        return 0
    messages: list[str] = []
    notes: list[str] = []
    check_dotnet(messages, notes)
    check_tooling(messages, notes)
    if notes and not messages:
        print(json.dumps({"systemMessage": " ".join(notes)}))
    if messages:
        print("\n\n".join(messages) + "\n\nBeheben oder dem Nutzer sagen, warum es fehlschlägt.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # hook JSON is UTF-8; Windows pipes default to cp1252
    sys.exit(main())
