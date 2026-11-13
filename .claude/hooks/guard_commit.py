#!/usr/bin/env python3
"""PreToolUse hook (Bash): stop a `git commit` that breaks a project rule.

Blocks (exit 2, reason fed back to Claude) when
* the current branch is main or master - work happens on issue branches;
* the message carries a Co-Authored-By or tool footer - the user's commit rules forbid it;
* a file about to be committed contains a token or a deny-listed real name,
  or a fixture contains a real e-mail address or tenant host
  (capture-fixture/scripts/fixture_check.py does the scanning).

The command is split at unquoted `;`, `&&`, `||`, `|` and newlines (comments
and here-document bodies dropped); a segment counts as a commit when it runs
`git [global options] commit`, also behind `env`, `then`, `if`, `!` and the
like. Words that hold a command themselves - `bash -c '…'`, `$(…)`, backticks -
are parsed the same way, two levels deep. Text that merely mentions
`git commit` (a PR body, `git log | grep commit`) is no commit. Files about to
be committed are the staged ones (read from the index) plus, read from the
working tree, the tracked changes for -a/--all or pathspecs and the files a
`git add` in the same command picks up - the hook runs before that add.

Fails closed: once the command is a commit, any error while checking blocks
it. A heuristic against accidents, not a sandbox: a commit through another
tool, a script or a `cd` into another repository is not seen.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

FOOTER = re.compile(r"Co-Authored-By\s*[:=]|Generated with \[?Claude", re.IGNORECASE)
FIXTURE = re.compile(r"(^|/)fixtures?/", re.IGNORECASE)
CHECKER = Path(".claude") / "skills" / "capture-fixture" / "scripts" / "fixture_check.py"
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GIT_EXE = re.compile(r"(^|[/\\])git(\.exe)?$", re.IGNORECASE)
REDIRECT = re.compile(r"^\d*(>>?|<<?<?|>&|<&)(.*)$")
# Global git options that take the next token as their value.
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}
# git commit options that take a value, attached or as the next token.
COMMIT_SHORT_VALUE = set("mFCct")
COMMIT_LONG_VALUE = {"--message", "--file", "--reuse-message", "--reedit-message", "--fixup", "--squash",
                     "--author", "--date", "--template", "--cleanup", "--trailer", "--pathspec-from-file"}
ADD_ALL = {"-A", "--all", "-u", "--update", "--no-ignore-removal"}
# Words in front of a command that still run it: `then git commit`, `env A=1 git commit`.
SHELL_PREFIXES = {"then", "do", "else", "if", "elif", "while", "until", "!", "time", "command", "env", "nohup",
                  "exec", "{", "(", "sudo", "xargs"}
# Prefix options that take the next word as their value (`sudo -u x`).
PREFIX_VALUE_OPTIONS = {"-u", "-g", "-C", "-n", "-I", "-L"}
# Nested command text inside a word: `$(…)`, backticks, and the parentheses closing them.
SUBSTITUTION = re.compile(r"\$\(|`|\)")
MAX_NESTING = 2


@dataclass
class GitCall:
    workdir: Path
    subcommand: str
    args: list[str]


@dataclass
class Commit:
    workdir: Path
    messages: list[str] = field(default_factory=list)
    message_files: list[str] = field(default_factory=list)
    all: bool = False
    pathspecs: list[str] = field(default_factory=list)


HEREDOC = re.compile(r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s;&|<>()'\"]+))")


def segments(command: str) -> list[str]:
    """Split at unquoted ;, &&, ||, |, & and newlines.

    `2>&1` stays whole, `#` comments and here-document bodies are dropped, so
    an apostrophe in either cannot hide the rest of the command.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    heredocs: list[str] = []
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and command[i + 1:i + 2] == "\n":
                i += 2
                continue
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == "\\" and command[i + 1:i + 2] == "\n":  # line continuation: `git \` + newline + `commit`
            i += 2
            continue
        elif ch == "\\" and i + 1 < len(command):
            buf.append(command[i:i + 2])
            i += 1
        elif ch == "#" and (not buf or buf[-1].isspace()):
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        elif command.startswith("<<<", i):  # here-string, no body to skip
            buf.append("<<<")
            i += 3
            continue
        elif command.startswith("<<", i):
            match = HEREDOC.match(command, i)
            if match:
                heredocs.append(next(g for g in match.groups() if g))
                buf.append(match.group(0))
                i = match.end()
                continue
            buf.append(ch)
        elif ch == "&" and ((buf and buf[-1].endswith(">")) or command[i + 1:i + 2] == ">"):
            buf.append(ch)
        elif ch in ";|&\n":
            parts.append("".join(buf))
            buf = []
            if ch in "|&" and command[i + 1:i + 2] == ch:
                i += 1
            if ch == "\n" and heredocs:
                i = skip_heredoc_bodies(command, i + 1, heredocs)
                heredocs = []
                continue
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def skip_heredoc_bodies(command: str, i: int, delimiters: list[str]) -> int:
    """Index after the last delimiter line, starting at the line after the `<<`."""
    for delimiter in delimiters:
        while i < len(command):
            end = command.find("\n", i)
            line = command[i:] if end < 0 else command[i:end]
            i = len(command) if end < 0 else end + 1
            if line.strip() == delimiter:
                break
    return i


def words(segment: str) -> list[tuple[str, str]]:
    """Shell words as (raw text, value without quotes); unbalanced quotes run to the end."""
    result: list[tuple[str, str]] = []
    raw: list[str] = []
    value: list[str] = []
    quote = ""
    i = 0
    while i < len(segment):
        ch = segment[i]
        if quote:
            if ch == quote:
                quote = ""
                raw.append(ch)
            elif ch == "\\" and quote == '"' and i + 1 < len(segment) and segment[i + 1] in '"\\$`':
                raw.append(segment[i:i + 2])
                value.append(segment[i + 1])
                i += 1
            else:
                raw.append(ch)
                value.append(ch)
        elif ch in "'\"":
            quote = ch
            raw.append(ch)
        elif ch == "\\" and i + 1 < len(segment):
            raw.append(segment[i:i + 2])
            value.append(segment[i + 1])
            i += 1
        elif ch.isspace():
            if raw:
                result.append(("".join(raw), "".join(value)))
            raw, value = [], []
        else:
            raw.append(ch)
            value.append(ch)
        i += 1
    if raw:
        result.append(("".join(raw), "".join(value)))
    return result


def tokens(segment: str) -> list[str]:
    """Word values without redirections; a quoted `"<x>"` is a word, not a redirect."""
    result: list[str] = []
    skip_target = False
    for raw, value in words(segment):
        if skip_target:
            skip_target = False
            continue
        redirect = REDIRECT.match(raw)
        if redirect:
            skip_target = not redirect.group(2)
            continue
        result.append(value)
    return result


def native(path: str, base: Path) -> Path:
    """A path as Git Bash means it: `/c/x` and `/tmp/x` are not Windows paths."""
    if sys.platform == "win32" and path.startswith("/"):
        drive = re.match(r"^/([A-Za-z])(/|$)(.*)$", path)
        if drive:
            return Path(f"{drive.group(1).upper()}:/{drive.group(3)}")
        if shutil.which("cygpath"):
            out = subprocess.run(["cygpath", "-w", path], capture_output=True, check=False)
            if out.returncode == 0 and out.stdout.strip():
                return Path(out.stdout.decode("utf-8", errors="replace").strip())
    return base / path


def git_call(words_: list[str], cwd: Path) -> GitCall | None:
    i = 0
    while i < len(words_):
        if ENV_ASSIGNMENT.match(words_[i]):
            i += 1
        elif words_[i] in SHELL_PREFIXES:
            i += 1
            while i < len(words_) and words_[i].startswith("-") and words_[i] != "-":  # `sudo -u x`, `env -i`
                i += 2 if words_[i] in PREFIX_VALUE_OPTIONS else 1
        else:
            break
    if i >= len(words_) or not GIT_EXE.search(words_[i]):
        return None
    i += 1
    workdir = cwd
    while i < len(words_) and words_[i].startswith("-"):
        word = words_[i]
        if word in GIT_VALUE_OPTIONS:
            if word == "-C" and i + 1 < len(words_):
                workdir = native(words_[i + 1], workdir)
            i += 2
            continue
        i += 1
    if i >= len(words_):
        return None
    return GitCall(workdir, words_[i], words_[i + 1:])


def parse_commit(call: GitCall) -> Commit:
    commit = Commit(call.workdir)
    args = call.args
    i = 0
    after_dashes = False
    while i < len(args):
        word = args[i]
        if after_dashes or not word.startswith("-") or word == "-":
            commit.pathspecs.append(word)
        elif word == "--":
            after_dashes = True
        elif word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in COMMIT_LONG_VALUE:
                if not eq:
                    value = args[i + 1] if i + 1 < len(args) else ""
                    i += 1
                if name in {"--message", "--trailer"}:
                    commit.messages.append(value)
                elif name == "--file":
                    commit.message_files.append(value)
            elif name == "--all":
                commit.all = True
        else:
            cluster = word[1:]
            for j, ch in enumerate(cluster):
                if ch == "a":
                    commit.all = True
                elif ch in COMMIT_SHORT_VALUE:
                    value = cluster[j + 1:]
                    if not value:
                        value = args[i + 1] if i + 1 < len(args) else ""
                        i += 1
                    if ch == "m":
                        commit.messages.append(value)
                    elif ch == "F":
                        commit.message_files.append(value)
                    break
                elif ch in "Su":  # optional value, only attached
                    break
        i += 1
    return commit


def add_pathspecs(call: GitCall) -> tuple[list[str], bool]:
    """Pathspecs of a `git add` (empty means everything) and whether it forces ignored files in."""
    specs: list[str] = []
    everything = False
    force = False
    after_dashes = False
    for word in call.args:
        if after_dashes or not word.startswith("-"):
            specs.append(word)
        elif word == "--":
            after_dashes = True
        elif word in ADD_ALL:
            everything = True
        elif word == "--force" or (not word.startswith("--") and "f" in word[1:]):
            force = True
    return ([] if everything else specs), force


def calls(command: str, cwd: Path, depth: int = 0) -> tuple[list[Commit], list[GitCall]]:
    commits: list[Commit] = []
    adds: list[GitCall] = []
    for segment in segments(command):
        words_ = tokens(segment)
        call = git_call(words_, cwd)
        if call is not None:
            if call.subcommand == "commit":
                commits.append(parse_commit(call))
            elif call.subcommand == "add":
                adds.append(call)
            continue
        if depth >= MAX_NESTING or "git" not in segment:
            continue
        # `bash -c 'git commit …'` and `eval "$(…)"` hold the command in one word; an unquoted
        # `echo $(git commit …)` spreads it over several, so the segment itself is parsed again too.
        nested = [word for word in words_[1:]
                  if "git" in word and (any(c.isspace() for c in word) or SUBSTITUTION.search(word))]
        if SUBSTITUTION.search(segment):
            nested.append(segment)
        for text in nested:
            nested_commits, nested_adds = calls(SUBSTITUTION.sub("\n", text), cwd, depth + 1)
            commits += nested_commits
            adds += nested_adds
    return commits, adds


def git(workdir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], capture_output=True, cwd=workdir, check=False)


def git_text(workdir: Path, *args: str) -> str:
    out = git(workdir, *args)
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""


def names(workdir: Path, *args: str) -> set[str]:
    return {line for line in git_text(workdir, *args).splitlines() if line}


def worktree_files(commit: Commit, adds: list[GitCall]) -> set[str]:
    """Repository-relative paths whose working-tree content may get committed."""
    files: set[str] = set()
    changed = ("diff", "--name-only", "--diff-filter=ACMR")
    if commit.all:
        files |= names(commit.workdir, *changed)
    if commit.pathspecs:
        files |= names(commit.workdir, *changed, "--", *commit.pathspecs)
    for add in adds:
        specs, force = add_pathspecs(add)
        exclude = [] if force else ["--exclude-standard"]
        files |= names(add.workdir, *changed, "--", *specs)
        files |= names(add.workdir, "ls-files", "--others", *exclude, "--full-name", "--", *specs)
    return files


def footer_problems(command: str, commit: Commit) -> list[str]:
    found: list[str] = []
    texts = list(commit.messages)
    for name in commit.message_files:
        if name == "-":
            texts.append(command)  # stdin: the message is a here-document somewhere in the command
            continue
        path = native(name, commit.workdir)
        if path.is_file():
            texts.append(path.read_bytes().decode("utf-8", errors="replace"))
        elif command.count(name) < 2:  # not written earlier in the same command either
            found.append(f"Nachrichtendatei '{name}' nicht gefunden - Footer nicht prüfbar.")
    if any(FOOTER.search(text) for text in texts):
        found.append("Commit-Nachricht ohne Co-Authored-By- oder Tool-Footer schreiben (Nutzerregel).")
    return found


def problems(command: str, cwd: Path, checker, denylist: list[str]) -> list[str]:
    commits, adds = calls(command, cwd)
    found: list[str] = []
    for commit in commits:
        top = git_text(commit.workdir, "rev-parse", "--show-toplevel").strip()
        if not top:
            continue
        root = Path(top)
        branch = git_text(root, "branch", "--show-current").strip()
        if branch in {"main", "master"}:
            found.append(f"Commit auf '{branch}' ist nicht erlaubt - erst einen Issue-Branch anlegen "
                         "(implement-issue: issue.py branch N).")
        found += footer_problems(command, commit)
        staged = names(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
        worktree = worktree_files(commit, adds)
        for path in sorted(staged | worktree):
            if Path(path).suffix.lower() not in checker.TEXT_SUFFIXES:
                continue
            texts = []
            if path in staged:
                texts.append(git_text(root, "show", f":{path}"))
            if path in worktree and (root / path).is_file():
                texts.append((root / path).read_bytes().decode("utf-8", errors="replace"))
            hits = {hit for text in texts
                    for hit in checker.scan_text(text, denylist, secrets_only=not FIXTURE.search(path))}
            found += [f"{path}:{line} {kind} ({value})" for line, kind, value in sorted(hits)]
    return list(dict.fromkeys(found))


def load_checker(project: Path):
    path = project / CHECKER
    if not path.is_file():
        raise FileNotFoundError(f"{CHECKER.as_posix()} fehlt")
    spec = importlib.util.spec_from_file_location("fixture_check", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{CHECKER.as_posix()} nicht ladbar")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError as exc:
        print(f"guard_commit.py: Hook-Eingabe ist kein JSON ({exc})", file=sys.stderr)
        return 1
    command = (payload.get("tool_input") or {}).get("command") or ""
    try:
        cwd = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".")
        if not calls(command, cwd)[0]:
            return 0
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)
        checker = load_checker(project)
        found = problems(command, cwd, checker, checker.load_denylist(project))
    except Exception as exc:  # noqa: BLE001 - fail closed: an unchecked commit is worse than a blocked one
        if "commit" not in command:
            return 0
        print(f"Commit blockiert: guard_commit.py konnte nicht prüfen ({type(exc).__name__}: {str(exc)[:200]}). "
              "Ursache beheben und erneut committen.", file=sys.stderr)
        return 2
    if not found:
        return 0
    print("Commit blockiert:\n- " + "\n- ".join(found[:20]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # hook JSON is UTF-8; Windows pipes default to cp1252
    sys.exit(main())
