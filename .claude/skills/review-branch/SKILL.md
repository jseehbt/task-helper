---
name: review-branch
description: Reviews everything changed on a branch of the TaskHelper repository with the project's reviewer agents - spec-reviewer for the domain rules of SPEC.md, dotnet-reviewer for C#/WPF code and project files, docs-reviewer for SPEC, AGENTS and docs - picked per changed file by a script, plus the checks a script names (fixture leak check, UI screenshots, skill check). Then it fixes the approved findings and reviews the fixes again, in at most three rounds. Use it before a commit or pull request, at the end of implement-issue, and whenever someone asks for a review of local changes, a branch or a pull request of this repository, even if they only say "schau mal drüber", "check my changes" or "ist das so fertig". Not for pull requests of other repositories, not for a pure security review (security-review does that), and not for a single file where starting the one matching agent directly is enough.
---

# Review the Branch

## Goal

Every changed file was reviewed by the agent for its kind, every Critical and Major finding is fixed or explicitly deferred by the user, every
hint was handled, and the `test-runner` agent is green after the last round.

## Workflow

`R` = `uv run --no-project python .claude/skills/review-branch/scripts/route_reviewers.py`; run from the repository root.

0. **Pull request only.** Given a PR number or URL, read `gh pr view N --json headRefName,baseRefName`. If `headRefName` is not the checked-out
   branch, run `gh pr checkout N` - only with a clean working tree, otherwise ask the user first. Use `--base origin/<baseRefName>` in step 1.
1. **Route.** `R` (add `--base REF` if the branch did not start from `main`). Exit 1 means no changes: say so and stop. Show the user the
   `reviewer=` and `hint=` lines in one short list.
2. **Review in parallel.** Start every routed agent in a single message. Give each: its files, the base ref, the instruction to read
   `git diff BASE -- FILES` plus as much surrounding code as it needs, the issue text if the branch belongs to an issue, and "report findings
   only, no edits". Round 2 and later add: "Focus on the fixes of the previous round: are they correct, did they break something, is anything
   left."
3. **Run the hints** at the same time: `fixture_check`, `check_skill` and `tooling_tests` are commands (the `detail=` field). `ui-shots` means: run
   that skill, then start the `ui-reviewer` agent with the screenshots. `spec_sync` and `github_actions` are recommendations for step 7.
4. **Merge.** Deduplicate by file and line, keep the agent's severity (Critical, Major, Minor, Suggestion). Present the findings grouped by
   severity, each with file:line, the problem in one sentence and the proposed fix.
5. **Ask** with the ask-user tool which to fix: all, Critical and Major only, a selection, or none. A finding the user defers is not raised again in
   later rounds. For a pull request, also offer to post the findings as inline review comments (`gh api` on the PR's review comments); post only
   what the user picked. Inside `implement-issue` the user already asked for the implementation: fix Critical and Major without asking, and list
   the rest in the result.
6. **Fix** the approved findings in the main session, then start the `test-runner` agent. A failing check after a fix is a finding of the next
   round, not a reason to stop. **Next round** with only the agents whose files the fixes touched, until a round has no Critical or Major finding
   or three rounds are done - later rounds mostly trade one opinion for another; whatever is still open then goes to the user as a list.
7. **Finish.** Recommend `spec-sync` if the hint appeared, and say which hints could not be run (e.g. `ui-shots` without Windows desktop).

## References and Scripts

* Run `scripts/route_reviewers.py [--base REF] [--json]` in step 1; exit 0 = files to review, 1 = no changes, 2 = not a repository or unknown
  base. Its `REVIEWERS` and `HINTS` lists decide who reviews what - extend them when a new agent, project or file kind appears.
* The reviewer agents live in `.claude/agents/`; AGENTS.md lists them.

## Safety

* Reviewers report, the main session edits: two agents editing in parallel produce conflicting changes nobody reviewed.
* Do not commit or push in this skill; `implement-issue` or the user decides that.
* PR comments are visible to everyone with repository access: post them only after the user chose that in step 5.
* No secrets or fixture contents in findings - a reviewer that finds one reports its location, not its value.

## Pitfalls

* A file a reviewer was not given is out of scope for it; findings on unchanged code are noise unless the change makes them reachable.
* `unrouted=` files (`.gitignore`, images, config templates) get no agent. Look at them yourself if they matter for the change.

## Done When

* [ ] Every `reviewer=` agent from step 1 reported in round 1, and every `hint=` was run or named as not run.
* [ ] No Critical or Major finding is open, or the user deferred it explicitly.
* [ ] `test-runner` is green after the last fix.
