---
name: add-source-adapter
description: Walks a TaskHelper source adapter (GitHub, Jira, Outlook/Graph, Slack) or a new partial query of one through every place it has to reach - adapter with PollAsync, item key with instance, reasons, ChangeKey, completeness and paging limits, error mapping to AuthRequired and Error, Retry-After, config section with validation and template, Credential Manager entry, token check in the token dialog, fixture tests and the setup doc - and checks the result with a coverage script. Use it whenever an adapter is built or extended, e.g. for issues 11, 12, 16, 17, 18 and 19, when a source gets a new query, reason or config option, or when someone asks what an adapter is still missing, even if they only say "GitHub-Adapter bauen" or "Slack-DMs ergänzen". Not for the scheduler, status priority or tray/flyout UI (no source-specific code), and not for API research without code (api-researcher answers that).
---

# Add or Extend a Source Adapter

## Goal

The adapter delivers items exactly as SPEC §3 and its §4.x section describe, `adapter_coverage.py <source>` reports `result=complete`, and the
`test-runner` agent is green.

## Workflow

`C` = `uv run --no-project python .claude/skills/add-source-adapter/scripts/adapter_coverage.py`; run from the repository root.

1. **Read the contract.** SPEC §3 (item, key format with instance, reasons, flags), §4 introduction (completeness), the source's §4.x, §7 (status
   mapping), §8.3 (token check) and §9 (config). For Slack also `docs/spike-slack.md`. Unclear API behaviour → `api-researcher` agent, not a guess.
2. **Run `C <source>`** to see what already exists - an extension starts from the existing adapter, not from scratch.
3. **Settle the decisions before code**, and ask the user if one is open: which partial queries make up `IsComplete`, which limit truncates
   (and therefore makes the result incomplete), which part is throttled (keeps its last state without making the result incomplete), the
   ChangeKey field, and how each error response maps (`AuthRequired` vs. `Error`).
4. **Core.** Adapter in `TaskHelper.Core` with `Task<SourceResult> PollAsync(CancellationToken)`; HTTP only through the injected
   `HttpMessageHandler`, time only through `TimeProvider`. Key `{source}:{instance}:{externalId}`; one item per key with all reasons (dedup);
   paging until the end or the limit; `IsComplete = false` on truncation, `incomplete_results`, a failed partial query or a channel over
   `maxUnreadPerChannel`. Logs only keys, counts, status codes and error kinds - never titles, subjects, texts or tokens (SPEC §10).
5. **Config.** Section model with validation (required fields when `enabled`, URL format, interval ≥ 1, errors with JSON path), the commented
   template, and a default for every optional field. No tenant, org, site or person in code (SPEC §12).
6. **App.** Credential entry `TaskHelper:<source>` (not Outlook), the "Prüfen" call in the token dialog, `NotConfigured` when the source is enabled
   but has no token.
7. **Tests and fixtures.** Mapping, dedup, paging, completeness and error mapping with fixtures through the fake handler; fixtures via the
   `capture-fixture` skill.
8. **Setup doc** `docs/setup-<source>.md` in German: how to create the app or token, which scopes or permissions, which config fields.
9. **Check.** `C <source>` until `result=complete`; then the `test-runner` agent. A `missing` that is deliberately not needed (e.g. a literal the
   adapter builds differently) is fine - name it to the user instead of adding the literal for the script's sake.

## References and Scripts

* Run `scripts/adapter_coverage.py <source> [--json]` in steps 2 and 9; exit 0 complete, 3 something missing, 2 usage error. Its `SOURCES` table
  holds the per-source reasons, token check and required literals - extend it when the spec gains a query or reason.
* SPEC.md §3-§9 is the contract; the issue text only selects the slice.

## Safety

* Read-only towards every source: only GET (and Graph's `translateExchangeIds` POST, which changes nothing). No mark-as-read, reaction or
  transition, also not "just to test".
* Calls against real accounts only via `capture-fixture`, after the user's yes.

## Pitfalls

* An error or an incomplete result must never set `GoneAt` (SPEC §3/§4). A test that only covers the happy path does not show this - add one
  where a partial query fails and the items of the other queries must still be there.
* Without `per_page=100`, GitHub returns 30 hits and the rest would count as gone.

## Done When

* [ ] `adapter_coverage.py <source>` exits 0, or every `missing` is explained to the user.
* [ ] Tests cover mapping, dedup, paging, incompleteness and the error mapping, with anonymized fixtures.
* [ ] `docs/setup-<source>.md` exists and matches the config fields; `test-runner` is green.
