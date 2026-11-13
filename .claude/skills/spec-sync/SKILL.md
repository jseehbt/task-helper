---
name: spec-sync
description: Brings SPEC.md, AGENTS.md, README.md and the setup guides docs/setup-*.md of the TaskHelper repository in line with the code and spike results changed on the current branch - a script maps changed files to the spec sections and docs that describe them, docs-reviewer agents report the exact drift, the edits are applied after the user approves them, and open issues that link a changed spec section are named. Use it before a pull request, after an implementation deviated from the spec, after a spike result (docs/spike-*.md), when a config field, status, reason or default changed, or whenever someone asks whether spec and docs still match the code, even if they only say "Spec nachziehen" or "stimmt die Doku noch". Not for writing new setup guides from scratch (add-source-adapter does that with the adapter), not for code comments, and not for editing issue bodies.
---

# Spec Sync

## Goal

Every spec section and doc that describes a file changed on this branch states what the code now does, each edit approved by the user, and the
user knows which open issues are affected by a changed section.

## Workflow

`D` = `uv run --no-project python .claude/skills/spec-sync/scripts/doc_targets.py`; run from the repository root.

1. **Collect the targets.** `D --issues` (add `--base REF` if the branch did not start from `main`). It prints `target=` lines (doc, sections,
   changed sources), `changed_doc=`, `unmapped=`, `stale_rule=` and `issue=`. Exit 1 means nothing to do: say so and stop. A `stale_rule` or a
   surprising `unmapped` file means `RULES` in the script needs updating - do that in the same change.
2. **Review in parallel.** Start one `docs-reviewer` agent per target doc in a single message, each with: the doc path, its sections, the changed
   sources and the base ref so it can read `git diff BASE -- SOURCES`. One more for all `changed_doc` files together, to check their new claims
   against the code. Each returns "no change needed" or edits with the exact current text and the replacement.
3. **Decide what wins.** The spec is the contract: where code and spec disagree, the reviewer says which looks intended. A deviation the issue or
   the user decided goes into the spec; an unintended one is a bug in the code - report it, do not paper over it in the spec.
4. **Show the plan** grouped by file, then ask with the ask-user tool: apply all, apply some, or cancel. If every reviewer reported "no change
   needed", say "Spec und Doku passen zum Code." and stop.
5. **Apply** the approved edits exactly as reported. An anchor that is not found means the doc changed meanwhile: re-read that section and
   adapt, do not force it. If SPEC.md changed, update its "Stand:" line with today's date and a short reason.
6. **Name affected issues.** For each `issue=` line, say in one sentence what changes for that issue. Do not edit issue bodies; if an issue is
   now wrong, offer the user a comment on it.

## References and Scripts

* Run `scripts/doc_targets.py [--base REF] [--issues] [--json]` in step 1; exit 0 targets, 1 nothing to check, 2 not a repository or unknown
  base. Its `RULES` list is the mapping from code to spec sections - extend it when a new kind of file appears.

## Safety

* Edit only what the code made wrong or incomplete. The spec is hand-written and reviewed; rewording a correct sentence costs a review and gains
  nothing.
* Spec, AGENTS.md and the setup guides are German; keep each file's language.
* Do not create or delete doc files here, and do not touch issue bodies - say what is needed instead.

## Pitfalls

* Section numbers in issues link the spec as `§4.4` and as URL anchors; renumbering a spec section breaks those links in every issue. Prefer
  adding a subsection at the end over renumbering.

## Done When

* [ ] Every `target=` and `changed_doc=` from step 1 was reviewed, and every approved edit is applied.
* [ ] No `stale_rule=` remains; every `unmapped=` file was mapped or judged irrelevant to the docs.
* [ ] Every `issue=` line was named to the user with its consequence.
