---
name: implement-issue
description: Implements one GitHub issue of the TaskHelper repository end to end - checks that the issues and clarifications it depends on are done, reads the SPEC.md sections it links, has the requirements-reviewer agent judge it, builds it on its own branch, runs build, format and tests, reviews the change, commits, ticks the verified acceptance criteria in the issue and in the overview issue, and comments the result on the issue. Without an argument it picks the next issue whose dependencies are met. Use it whenever someone wants to work on, continue or finish an issue or a step of the implementation plan, e.g. "Issue 11 umsetzen", "nächstes Issue", "mach mit Schritt 5 weiter", "what can be built next", even if the word issue is not used. Not for the clarification and spike issues labelled question, not for writing or restructuring issues, and not for reviewing a branch or pull request that is not tied to an issue.
---

# Implement an Issue

## Goal

The issue is implemented on its own branch with a green build, format check and test run, every verified acceptance criterion is ticked, and the
issue carries a comment with the result - or, if it cannot be built as written, a comment with the concrete open questions.

## Workflow

`I` = `uv run --no-project python .claude/skills/implement-issue/scripts/issue.py`, run from the repository root. Write every comment to a temporary file first and
pass its path: quotes, umlauts and line breaks then survive on Windows.

1. **Pick the issue.** With a number, use it. Without one, run `I next`: it walks the overview issue in order and prints one `issue=` line per open
   issue (`ready`, `in_progress`, `blocked by=…`, `human_only`) and `next=`. Name the chosen issue to the user and continue. `next=none` → show the
   `blocked` lines, say which clarification or issue unblocks the most, and stop.
2. **Collect the facts.** `I show N` returns JSON with dependencies, `spec_refs`, `done_criteria`, the branch name, body and comments.
   - `state=CLOSED` → say so and stop.
   - `human_only=true` (label `question`) → this is a clarification or a spike that needs the user's decisions or real-account calls; say so and
     stop.
   - A dependency with `satisfied=false` → stop and report it (`r_items_open` names the open clarifications in #2). Exception `done_unmerged`: the
     dependency is implemented on a local branch that is not merged yet - ask the user whether to stack on that branch (`--base` in step 5) or wait.
3. **Read the spec.** `I spec <spec_refs…>` prints the linked sections. Read them plus `AGENTS.md`, if it exists. The spec wins over the issue text
   where they differ; note the difference for the comment in step 10.
4. **Judge the issue.** Start the `requirements-reviewer` agent with the issue body, all comments (an earlier question may already be answered),
   the spec sections and the dependency state. Take its verdict. **Nicht umsetzbar** → comment with `I comment N FILE` in this form, report, stop:

   ```
   Ich setze dieses Issue noch nicht um.

   Grund: <ein bis drei Sätze, konkret, mit Bezug auf Issue, Spec oder Code>

   Offene Fragen:
   - <Frage, die eine kurze Antwort klärt>?
   ```

   If the same question is already open in the thread, refer to it instead of asking again.
5. **Branch.** `I branch N` (or `I branch N --base <branch>` after step 2's stacking decision) creates `N-slug` from `main` or switches to the
   existing one. Exit 5 (dirty working tree) → tell the user and stop; never stash or discard changes that are not yours.
6. **Implement.** Use the reviewer's "Hinweise für die Umsetzung". Change only the lines the issue needs. Ask the user before adding any NuGet
   package - SPEC §2 lists which ones are still unconfirmed. Unclear API behaviour of Slack, Graph, Jira or GitHub → start the `api-researcher` agent
   rather than guessing. Calls against real accounts only read-only and only after asking the user, every time (SPEC §11). Follow the skill for
   the kind of work:
   - a source adapter or a new query of one (issues 11, 12, 16-19) → `add-source-adapter`;
   - test fixtures from API responses → `capture-fixture`;
   - anything visible in tray, flyout or dialogs → `ui-shots` after step 7, before the review.
7. **Check.** Start the `test-runner` agent (build without warnings, `dotnet format --verify-no-changes`, `dotnet test`). Fix failures and run it
   again. If it stays red at reasonable effort, do not commit: comment the error state and the branch on the issue, report, stop.
8. **Review.** Run the `review-branch` skill; it fixes Critical and Major findings and re-runs the `test-runner` agent. If it names the
   `spec_sync` hint and the implementation deviates from the spec, run `spec-sync` before committing, so the spec change lands in the same commit.
9. **Commit.** Stage the changed files by path (no `git add -A`, so nothing unrelated slips in). Subject: `#N <type>: <imperative summary>`, max. 150
   characters, type `feat` for label `enhancement`, `docs` for `documentation`, `fix` for a bug. No tool or co-author footer. No push.
10. **Tick and comment.** Go through `done_criteria` one by one. Tick only what was actually verified: `I tick N --match "<unique part of the
    text>"`. A criterion that needs a look at the running app on Windows (tray, flyout, DPI, dark mode, publish EXE starts) counts only if it was
    really checked in this run; otherwise leave it open and say why. When all criteria are ticked, also `I overview N`. Then post a German comment
    with `I comment N FILE`:

    ```
    Umgesetzt auf Branch `N-slug`, Commit `abc1234` (lokal, noch nicht gepusht).

    Änderungen: <zwei bis fünf Punkte>
    Prüfung: Build ohne Warnungen, format, <x> Tests grün; Review: <Ergebnis>
    Abgehakt: <Kriterien>
    Offen: <Kriterium – warum, was der Nutzer prüfen muss>   (nur wenn etwas offen ist)
    Abweichungen von Spec/Issue: <…>                          (nur wenn es welche gibt)
    ```
11. **Report** to the user: issue link, branch, commit, check and review results, ticked and open criteria. Offer push and pull request; the user
    decides.

## References and Scripts

* Run `scripts/issue.py next|show|spec|branch|tick|overview|comment` as described above. Exit codes: 0 ok, 2 usage, 3 gh/git call failed or
  issue/section not found, 4 gh or git missing, 5 dirty tree or unknown base, 6 `tick` matched no or several checkboxes - make `--match` more
  specific instead of editing the body by hand.

## Safety

* Never commit on `main`, never push or open a pull request without the user's go - a pushed branch is visible to everyone with repository access.
* Change issues only through `tick`, `overview` and `comment`: never untick, rewrite a body, close an issue or edit the R items in #2. Those are the
  user's decisions.
* No tokens, real names, mail subjects or message texts in comments, commits or fixtures - issues are readable by everyone with access, and
  SPEC §3 forbids content outside the local database.

## Pitfalls

* #2 stays open until every clarification is settled, so a dependency on it names R items (`#2 (R7)`, `R2 in #2`). The script checks exactly
  those checkboxes; a plain "is #2 closed" would block everything.
* Spec links carry anchors such as `SPEC.md#42-…`; the script strips URLs before reading dependencies, so they do not count as issue #42.

## Done When

* [ ] The issue carries a new comment: either the result (branch, commit, checks, ticked and open criteria) or the open questions.
* [ ] When implemented: `test-runner` green after the last change, commit on `N-slug`, working tree clean.
* [ ] Every ticked criterion was verified in this run; every open one is named with its reason in the comment and to the user.
