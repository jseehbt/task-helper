#!/usr/bin/env python3
"""GitHub issue helper for the implement-issue skill.

Collects the facts the skill needs to decide (dependencies, referenced SPEC
sections, done criteria) and performs the fixed GitHub/git steps (branch,
tick a checkbox, post a comment), so the skill text only keeps what needs
judgment. Standard library only; uses the gh and git CLIs.

Usage (from anywhere inside the repository):
    uv run --no-project python issue.py next                     # next issue whose dependencies are met
    uv run --no-project python issue.py show N                   # issue facts as JSON
    uv run --no-project python issue.py spec 4.4 11              # print SPEC.md sections
    uv run --no-project python issue.py branch N [--base REF]    # create or switch to the issue branch
    uv run --no-project python issue.py tick N --match TEXT      # tick the one open checkbox containing TEXT
    uv run --no-project python issue.py overview N               # tick issue N in the overview issue
    uv run --no-project python issue.py comment N FILE           # post FILE as a comment on issue N

Exit codes:
    0  success
    2  usage error
    3  gh/git call failed or issue/section not found
    4  gh or git not installed
    5  git precondition failed (dirty working tree, unknown base)
    6  tick: no matching or more than one matching checkbox
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

# The overview issue lists the implementation order as a task list (SPEC §14).
OVERVIEW_ISSUE = 22
# Issues labelled like this are clarifications or spikes that need a human or
# real-account calls; the skill does not implement them.
HUMAN_LABELS = {"question"}
# Enough for every issue of v1 plus follow-ups; gh list defaults to 30.
LIST_LIMIT = 500
# Branch slugs stay short so `git branch` output stays readable.
SLUG_MAX = 40

CHECKBOX = re.compile(r"^(\s*[-*] \[)([ xX])(\] )(.*)$")
DEP_LINE = re.compile(r"Abhängig von|Voraussetzung", re.IGNORECASE)
DEP_REF = re.compile(r"#(\d+)(?:\s*\(([^)]*)\))?")
R_IN_ISSUE = re.compile(r"((?:R\d+\s*(?:,|und)?\s*)+)\s+in\s+#(\d+)")
R_ITEM = re.compile(r"R\d+")
R_CHECKBOX = re.compile(r"^\s*[-*] \[([ xX])\] \*\*(R\d+)\b")
SPEC_REF = re.compile(r"§\s?(\d+(?:\.\d+)?)")
URL = re.compile(r"https?://[^\s)]+")
DONE_MARKER = re.compile(r"Fertig,? wenn", re.IGNORECASE)


def die(code: int, reason: str, detail: str = "") -> None:
    print(f"error={reason}" + (f" detail={detail}" if detail else ""), file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], check: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if check and result.returncode != 0:
        die(3, f"{cmd[0]}_failed", f"{' '.join(cmd[:4])}: {result.stderr.strip()}")
    return result.stdout


def require_tools() -> None:
    for tool in ("gh", "git"):
        if shutil.which(tool) is None:
            die(4, "tool_missing", f"{tool} not found on PATH")


def repo_root() -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"]).strip())


# --- GitHub data -----------------------------------------------------------

def view_issue(number: int) -> dict:
    out = run(["gh", "issue", "view", str(number), "--json",
               "number,title,state,body,labels,comments,url"])
    return json.loads(out)


def list_issues() -> dict[int, dict]:
    out = run(["gh", "issue", "list", "--state", "all", "--limit", str(LIST_LIMIT),
               "--json", "number,title,state,body,labels"])
    return {i["number"]: i for i in json.loads(out)}


def labels(issue: dict) -> set[str]:
    return {label["name"] for label in issue.get("labels", [])}


# --- Parsing ---------------------------------------------------------------

def checkboxes(body: str) -> list[dict]:
    return [{"checked": m.group(2) != " ", "text": m.group(4).strip()}
            for line in body.splitlines() if (m := CHECKBOX.match(line))]


def done_criteria(body: str) -> list[dict]:
    """Checkboxes below the 'Fertig, wenn' marker - the issue's acceptance criteria."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if DONE_MARKER.search(line):
            return checkboxes("\n".join(lines[index + 1:]))
    return []


def r_items(body: str) -> dict[str, bool]:
    return {m.group(2): m.group(1) != " "
            for line in body.splitlines() if (m := R_CHECKBOX.match(line))}


def dependencies(body: str) -> list[dict]:
    """Issues (optionally restricted to R items) named on 'Abhängig von' lines."""
    deps: dict[int, set[str]] = {}
    for line in body.splitlines():
        if not DEP_LINE.search(line):
            continue
        # Spec links carry anchors such as SPEC.md#42-slack-..., which would
        # otherwise read as a dependency on issue #42.
        line = URL.sub("", line)
        covered: set[int] = set()
        for m in R_IN_ISSUE.finditer(line):
            number = int(m.group(2))
            deps.setdefault(number, set()).update(R_ITEM.findall(m.group(1)))
            covered.add(m.start(2))
        for m in DEP_REF.finditer(line):
            if m.start(1) in covered:
                continue
            number = int(m.group(1))
            items = set(R_ITEM.findall(m.group(2) or ""))
            deps.setdefault(number, set()).update(items)
    return [{"issue": n, "r_items": sorted(items)} for n, items in sorted(deps.items())]


def spec_refs(body: str) -> list[str]:
    seen: list[str] = []
    for ref in SPEC_REF.findall(body):
        if ref not in seen:
            seen.append(ref)
    return seen


def local_branches(number: int) -> list[str]:
    out = run(["git", "branch", "--list", f"{number}-*", "--format=%(refname:short)"])
    return [b for b in out.splitlines() if b.strip()]


def dependency_state(dep: dict, issues: dict[int, dict]) -> dict:
    number = dep["issue"]
    issue = issues.get(number)
    result = {"issue": number, "r_items": dep["r_items"]}
    if issue is None:
        return {**result, "state": "unknown", "satisfied": False}
    if dep["r_items"]:
        # Only the named clarifications count; the clarification issue itself
        # stays open until every R item is settled.
        ticked = r_items(issue["body"])
        missing = [r for r in dep["r_items"] if not ticked.get(r, False)]
        return {**result, "state": "r_items_open" if missing else "r_items_done",
                "missing": missing, "satisfied": not missing}
    if issue["state"] == "CLOSED":
        return {**result, "state": "closed", "satisfied": True}
    criteria = done_criteria(issue["body"])
    if criteria and all(c["checked"] for c in criteria):
        # Implemented on a branch but not merged yet: building on it means
        # stacking branches, which the user decides.
        return {**result, "state": "done_unmerged", "branches": local_branches(number),
                "satisfied": False}
    return {**result, "state": "open", "satisfied": False}


def slug(title: str) -> str:
    title = re.sub(r"^Schritt\s+\d+:\s*", "", title)
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"),
                     ("Ü", "Ue"), ("ß", "ss")):
        title = title.replace(src, dst)
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    out = ""
    for word in words:
        candidate = f"{out}-{word}" if out else word
        if len(candidate) > SLUG_MAX:
            break
        out = candidate
    return out or "issue"


# --- Commands --------------------------------------------------------------

def cmd_show(number: int) -> int:
    issue = view_issue(number)
    issues = list_issues()
    deps = [dependency_state(d, issues) for d in dependencies(issue["body"])]
    data = {
        "number": issue["number"],
        "title": issue["title"],
        "url": issue["url"],
        "state": issue["state"],
        "labels": sorted(labels(issue)),
        "human_only": bool(labels(issue) & HUMAN_LABELS),
        "dependencies": deps,
        "dependencies_met": all(d["satisfied"] for d in deps),
        "spec_refs": spec_refs(issue["body"]),
        "done_criteria": done_criteria(issue["body"]),
        "branch": f"{number}-{slug(issue['title'])}",
        "existing_branches": local_branches(number),
        "body": issue["body"],
        "comments": [{"author": c["author"]["login"], "body": c["body"]}
                     for c in issue.get("comments", [])],
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_next(overview: int) -> int:
    issues = list_issues()
    if overview not in issues:
        die(3, "overview_not_found", f"issue #{overview}")
    order = [int(m.group(1)) for line in issues[overview]["body"].splitlines()
             if (cb := CHECKBOX.match(line)) and cb.group(2) == " "
             and (m := re.match(r"#(\d+)\b", cb.group(4)))]
    chosen = None
    for number in order:
        issue = issues.get(number)
        if issue is None:
            print(f"issue={number} status=unknown")
            continue
        if issue["state"] == "CLOSED":
            continue
        if labels(issue) & HUMAN_LABELS:
            print(f"issue={number} status=human_only")
            continue
        deps = [dependency_state(d, issues) for d in dependencies(issue["body"])]
        blocking = [d for d in deps if not d["satisfied"]]
        branches = local_branches(number)
        if blocking:
            reason = ",".join(
                f"#{d['issue']}:{d['state']}"
                + (f"({'+'.join(d['missing'])})" if d.get("missing") else "")
                for d in blocking)
            print(f"issue={number} status=blocked by={reason}")
        elif branches:
            print(f"issue={number} status=in_progress branch={branches[0]}")
            chosen = chosen or number
        else:
            print(f"issue={number} status=ready")
            chosen = chosen or number
    if chosen is None:
        print("next=none")
        return 0
    print(f"next={chosen} title={issues[chosen]['title']}")
    return 0


def cmd_spec(sections: list[str]) -> int:
    spec = repo_root() / "SPEC.md"
    if not spec.is_file():
        die(3, "spec_not_found", spec.as_posix())
    lines = spec.read_text(encoding="utf-8").splitlines()
    status = 0
    for section in sections:
        level = "##" if "." not in section else "###"
        start = next((i for i, line in enumerate(lines)
                      if re.match(rf"^{level} {re.escape(section)}\.?\s", line)), None)
        if start is None:
            print(f"section={section} status=not_found", file=sys.stderr)
            status = 3
            continue
        end = len(lines)
        for i in range(start + 1, len(lines)):
            heading = re.match(r"^(#{2,3}) ", lines[i])
            if heading and len(heading.group(1)) <= len(level):
                end = i
                break
        print("\n".join(lines[start:end]).rstrip() + "\n")
    return status


def cmd_branch(number: int, base: str) -> int:
    if run(["git", "status", "--porcelain"]).strip():
        die(5, "dirty_working_tree", "commit or stash your changes first; the skill never stashes")
    existing = local_branches(number)
    if existing:
        run(["git", "switch", existing[0]])
        print(f"branch={existing[0]} created=false")
        return 0
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
                      capture_output=True, check=False).returncode != 0:
        die(5, "unknown_base", base)
    name = f"{number}-{slug(view_issue(number)['title'])}"
    run(["git", "switch", "-c", name, base])
    print(f"branch={name} created=true base={base}")
    return 0


def edit_body(number: int, body: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        run(["gh", "issue", "edit", str(number), "--body-file", path])
    finally:
        Path(path).unlink(missing_ok=True)


def tick(number: int, matches) -> int:
    """Tick exactly one checkbox line selected by `matches(text)`; idempotent."""
    body = view_issue(number)["body"]
    lines = body.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines)
            if (m := CHECKBOX.match(line.rstrip("\r\n"))) and matches(m.group(4))]
    if len(hits) != 1:
        texts = " | ".join(lines[i].strip() for i in hits)
        die(6, "no_match" if not hits else "ambiguous_match", texts)
    line = lines[hits[0]]
    m = CHECKBOX.match(line.rstrip("\r\n"))
    if m.group(2) != " ":
        print(f"issue={number} ticked=false already=true item={m.group(4).strip()}")
        return 0
    ending = line[len(line.rstrip("\r\n")):]
    lines[hits[0]] = f"{m.group(1)}x{m.group(3)}{m.group(4)}{ending}"
    edit_body(number, "".join(lines))
    print(f"issue={number} ticked=true item={m.group(4).strip()}")
    return 0


def cmd_comment(number: int, file: Path) -> int:
    if not file.is_file():
        die(2, "file_not_found", file.as_posix())
    url = run(["gh", "issue", "comment", str(number), "--body-file", str(file)]).strip()
    print(f"issue={number} commented=true url={url}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next")
    p.add_argument("--overview", type=int, default=OVERVIEW_ISSUE)
    sub.add_parser("show").add_argument("number", type=int)
    sub.add_parser("spec").add_argument("sections", nargs="+")
    p = sub.add_parser("branch")
    p.add_argument("number", type=int)
    p.add_argument("--base", default="main")
    p = sub.add_parser("tick")
    p.add_argument("number", type=int)
    p.add_argument("--match", required=True)
    p = sub.add_parser("overview")
    p.add_argument("number", type=int)
    p.add_argument("--overview", type=int, default=OVERVIEW_ISSUE)
    p = sub.add_parser("comment")
    p.add_argument("number", type=int)
    p.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    require_tools()
    if args.cmd == "next":
        return cmd_next(args.overview)
    if args.cmd == "show":
        return cmd_show(args.number)
    if args.cmd == "spec":
        return cmd_spec(args.sections)
    if args.cmd == "branch":
        return cmd_branch(args.number, args.base)
    if args.cmd == "tick":
        return tick(args.number, lambda text: args.match in text)
    if args.cmd == "overview":
        # Exact reference, so that #1 never ticks #11.
        ref = re.compile(rf"^#{args.number}\b")
        return tick(args.overview, lambda text: bool(ref.match(text)))
    if args.cmd == "comment":
        return cmd_comment(args.number, args.file)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
