---
name: capture-fixture
description: Creates JSON test fixtures for the TaskHelper source adapters (GitHub, Jira, Slack, Outlook/Graph) - either written by hand from the documented response shape or captured with one approved read-only call against a real account, then anonymized by script and checked for tokens, real e-mail addresses, tenant hosts and deny-listed names before anything is committed. Use it whenever an adapter test needs a response fixture, a fixture must be refreshed after an API change, a test needs a paging, draft, incomplete or error case, or someone asks to check fixtures for leaked data, even if they only say "ich brauche Testdaten für den Jira-Adapter" or "is the fixture clean". Not for the fake HttpMessageHandler code itself, not for exploratory API calls without a fixture as result (api-researcher answers API questions), and not for the Slack spike in issue 3.
---

# Capture a Fixture

## Goal

The adapter test has the fixture it needs under the test project's `Fixtures/` folder, anonymized, and `fixture_check.py` reports `findings=0`
for it.

## Workflow

`F` = `uv run --no-project python .claude/skills/capture-fixture/scripts`; run from the repository root.

1. **Name the case.** Source, endpoint, and what the test checks (e.g. "GitHub search, second page via `Link`", "Jira 400 on invalid JQL",
   "Slack `ok:false` with `token_revoked`"). The case decides which fields matter.
2. **Prefer writing it by hand.** Error bodies, `ok:false`, empty results, a single item with a draft flag - write these from the documented
   response shape (ask the `api-researcher` agent if the shape is unclear). No real account is touched and nothing needs anonymizing. Capture
   only when the structure is large or the documentation is vague; then continue with step 3.
3. **Ask the user first, every time.** Show the exact call: source, full URL with query, which stored token is used, and what the response will
   contain. Wait for an explicit yes - SPEC §11 allows calls against real accounts only read-only and only after asking in each single case.
4. **Fetch outside the repository.** `F/fetch.py <github|jira|slack> "<url>" <dir outside the repo, e.g. %TEMP%\taskhelper-raw> <name>`
   (`--email` for Jira). It reads the token from the Credential Manager entry `TaskHelper:<source>` and refuses other hosts and in-repo targets.
   Outlook: the user exports the response from Graph Explorer into that directory instead. **Do not read the raw file** - it holds real
   names and texts, and reading it sends them into the conversation.
5. **Anonymize.** `F/anonymize.py <raw>.json <test project>/Fixtures/<source>/<name>.json --map <real org>=example-org --map <real host>=example.atlassian.net …`
   Map every tenant value: GitHub org, Jira site host and project key if it is telling, Slack team ID and workspace host. The script replaces
   content fields and e-mails itself and keeps IDs and timestamps. A `<name>.headers.json` sidecar is anonymized alongside.
6. **Shape it for the test.** Now read the anonymized file. Trim it to what the test needs (two items instead of 100), and write synthetic
   values where the test checks content, e.g. a Slack `text` with `<@U00000001>` and `<!here>` for mention resolution.
7. **Check.** `F/fixture_check.py <file or Fixtures folder>`. Every `finding=` is a leak or a false alarm; fix leaks in the fixture, never by
   weakening the check. The guard_commit hook runs the same check on every commit.

## References and Scripts

* `scripts/fetch.py` - one GET, token from the Credential Manager, raw body and filtered headers to disk. Exit 2 refused (host, target dir),
  3 HTTP error or Slack `ok:false`, 4 not Windows or no credential.
* `scripts/anonymize.py RAW OUT [--map REAL=FAKE …]` - exit 2 on bad input.
* `scripts/fixture_check.py [PATH …] [--secrets-only]` - without a path all fixture folders of the repository; exit 3 on findings.
* `.claude/fixture-denylist.txt` - untracked, one real name per line (org, site, workspace, people). The check warns `no_denylist` while it is
  missing: create it with the user once, it is what catches real names no pattern knows.

## Safety

* No call without the user's yes for exactly that call; never a write method - the app and its tests are strictly read-only towards every source
  (SPEC §1).
* Raw responses stay outside the repository and are deleted after step 7; tell the user where they are if deletion is not possible.
* Never print, log or paste a token. `fetch.py` reads it itself so that it does not appear in a command line.

## Pitfalls

* `anonymize.py` replaces whole content fields, so mention tokens, links and `@here` inside a Slack `text` are gone afterwards - re-add them
  synthetically in step 6 when the test needs them.
* Jira `status.name` and similar enum-like names are replaced too (`Name 3`); set them back to a realistic value such as `In Review` when the
  test asserts the context line.

## Done When

* [ ] The fixture lives under the test project's `Fixtures/<source>/` and is used by a test.
* [ ] `fixture_check.py` reports `findings=0` for it, with a deny list present.
* [ ] No raw response is left in the repository; the user knows where raw files outside it are, if any remain.
