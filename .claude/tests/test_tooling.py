"""Tests for the scripts behind the project skills and hooks.

Run from the repository root:
    uv run --no-project python -m unittest discover -s .claude/tests
Only the pure parts are tested: no gh, no network, no Windows desktop.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".claude" / "skills"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


issue = load(SKILLS / "implement-issue" / "scripts" / "issue.py")
anonymize = load(SKILLS / "capture-fixture" / "scripts" / "anonymize.py")
fixture_check = load(SKILLS / "capture-fixture" / "scripts" / "fixture_check.py")
route = load(SKILLS / "review-branch" / "scripts" / "route_reviewers.py")
doc_targets = load(SKILLS / "spec-sync" / "scripts" / "doc_targets.py")
coverage = load(SKILLS / "add-source-adapter" / "scripts" / "adapter_coverage.py")
shots = load(SKILLS / "ui-shots" / "scripts" / "shots.py")
fetch = load(SKILLS / "capture-fixture" / "scripts" / "fetch.py")
guard = load(ROOT / ".claude" / "hooks" / "guard_commit.py")
stop_hook = load(ROOT / ".claude" / "hooks" / "checks_on_stop.py")
format_hook = load(ROOT / ".claude" / "hooks" / "dotnet_format_check.py")


class IssueParsing(unittest.TestCase):
    BODY = (
        "Spec: [§4.2](https://github.com/o/r/blob/main/SPEC.md#42-slack-web-api-user-token), §13 R3, R4 · "
        "Voraussetzung: R2 in #2\n\n"
        "**Fertig, wenn**\n- [x] Tests grün\n- [ ] Doku\n\n"
        "Abhängig von #2 (R1, R7), #10, #14\n"
    )

    def test_dependencies_ignore_url_anchors_and_merge_r_items(self):
        deps = {d["issue"]: d["r_items"] for d in issue.dependencies(self.BODY)}
        self.assertEqual(deps, {2: ["R1", "R2", "R7"], 10: [], 14: []})

    def test_done_criteria_only_below_marker(self):
        self.assertEqual([c["checked"] for c in issue.done_criteria(self.BODY)], [True, False])

    def test_spec_refs_in_order_without_duplicates(self):
        self.assertEqual(issue.spec_refs(self.BODY + " §4.2"), ["4.2", "13"])

    def test_slug_strips_step_prefix_and_umlauts(self):
        self.assertEqual(issue.slug("Schritt 1: Projektgerüst (Core, App, Tests)"), "projektgeruest-core-app-tests")
        self.assertLessEqual(len(issue.slug("Schritt 9: " + "sehr langer titel " * 10)), issue.SLUG_MAX)

    def test_r_item_state(self):
        body = "- [x] **R9 GitHub:** ok\n- [ ] **R7 Dependencies:** offen"
        self.assertEqual(issue.r_items(body), {"R9": True, "R7": False})

    def test_dependency_on_r_items_ignores_issue_state(self):
        issues = {2: {"state": "OPEN", "body": "- [x] **R7 Deps:** ok", "labels": []}}
        state = issue.dependency_state({"issue": 2, "r_items": ["R7"]}, issues)
        self.assertTrue(state["satisfied"])


class Anonymize(unittest.TestCase):
    def test_content_emails_and_mapping(self):
        a = anonymize.Anonymizer([("acme-org", "example-org"), ("acme-org-team", "example-team")])
        data = {
            "items": [
                {"title": "Geheimes Projekt", "user": {"login": "jdoe", "avatar_url": "https://x/y.png"},
                 "html_url": "https://github.com/acme-org/repo/pull/1", "state": "open", "id": 42},
                {"title": "Geheimes Projekt", "body": "Mail an jane.doe@acme.de", "draft": True},
            ],
            "note": "acme-org-team und jane.doe@acme.de",
        }
        out = a.walk(data)
        self.assertEqual(out["items"][0]["title"], out["items"][1]["title"])
        self.assertNotIn("Geheim", str(out))
        self.assertEqual(out["items"][0]["html_url"], "https://github.com/example-org/repo/pull/1")
        self.assertEqual(out["items"][0]["state"], "open")
        self.assertEqual(out["items"][0]["id"], 42)
        self.assertEqual(out["items"][0]["user"]["avatar_url"], anonymize.NEUTRAL_IMAGE)
        self.assertEqual(out["note"], "example-team und user1@example.com")

    def test_example_emails_unchanged(self):
        self.assertEqual(anonymize.Anonymizer([]).scrub("a@example.com"), "a@example.com")

    def test_graph_body_content(self):
        a = anonymize.Anonymizer([])
        out = a.walk({"body": {"contentType": "html", "content": "<p>Gehalt</p>"}, "uniqueBody": {"content": "x"}})
        self.assertNotIn("Gehalt", str(out))
        self.assertEqual(out["body"]["contentType"], "html")

    def test_logins_and_domains_are_replaced_in_urls(self):
        a = anonymize.Anonymizer([])
        data = {"html_url": "https://github.com/jdoe/repo/pull/1", "user": {"login": "jdoe"},
                "team": {"domain": "acmeteam"}, "permalink": "https://acmeteam.slack.com/archives/C1/p1",
                "other": "jdoe2 bleibt"}
        a.collect(data)
        out = a.walk(data)
        self.assertEqual(out["user"]["login"], "login1")
        self.assertEqual(out["html_url"], "https://github.com/login1/repo/pull/1")
        self.assertEqual(out["permalink"], "https://domain1.slack.com/archives/C1/p1")
        self.assertEqual(out["other"], "jdoe2 bleibt")

    def test_short_identities_and_keep_keys_stay_local(self):
        a = anonymize.Anonymizer([])
        data = {"user": {"login": "ab"}, "other": {"login": "open"}, "team": {"domain": "api"},
                "key": "AB-12", "state": "open", "ref": "ab-fix", "self": "https://x.example/rest/api/3/issue/1"}
        a.collect(data)
        out = a.walk(data)
        self.assertEqual(out["user"]["login"], "login1")
        self.assertEqual((out["key"], out["state"], out["ref"]), ("AB-12", "open", "ab-fix"))
        self.assertEqual(out["self"], "https://x.example/rest/api/3/issue/1")
        self.assertEqual(a.short_identities, {"ab", "api"})

    def test_every_spelling_of_a_login_is_replaced(self):
        a = anonymize.Anonymizer([])
        data = {"a": {"login": "JDoe"}, "b": {"login": "jdoe"}, "url": "https://github.com/jdoe/r"}
        a.collect(data)
        out = a.walk(data)
        self.assertEqual(out["url"], "https://github.com/login1/r")
        self.assertEqual(out["a"]["login"], out["b"]["login"])

    def test_keep_keys_follow_the_map(self):
        a = anonymize.Anonymizer([("ACME", "EX")])
        out = a.walk({"key": "ACME-12", "state": "open", "self": "https://x.example/browse/ACME-12"})
        self.assertEqual(out, {"key": "EX-12", "state": "open", "self": "https://x.example/browse/EX-12"})

    def test_mapped_login_keeps_the_mapping(self):
        a = anonymize.Anonymizer([("acme-org", "example-org")])
        self.assertEqual(a.walk({"login": "acme-org"})["login"], "example-org")


class FixtureCheck(unittest.TestCase):
    def kinds(self, text, denylist=(), secrets_only=False):
        return [kind for _, kind, _ in fixture_check.scan_text(text, list(denylist), secrets_only)]

    def test_tokens(self):
        self.assertIn("slack_token", self.kinds("xox" + "p-1234567890-abcdefghij"))
        self.assertIn("github_token", self.kinds("github_pat_" + "A" * 30))
        self.assertIn("atlassian_token", self.kinds("ATATT" + "x" * 30))

    def test_emails_and_tenant_hosts(self):
        self.assertIn("email", self.kinds("jane@acme.de"))
        self.assertIn("tenant_host", self.kinds("https://acme.atlassian.net/browse/X-1"))
        self.assertEqual(self.kinds("user1@example.com https://example.atlassian.net https://slack.com/api"), [])

    def test_denylist_case_insensitive_and_secrets_only(self):
        self.assertEqual(self.kinds("Hallo ACME", ["acme"]), ["denylist"])
        self.assertEqual(self.kinds("jane@acme.de", secrets_only=True), [])

    def test_more_tenant_hosts(self):
        self.assertIn("tenant_host", self.kinds("https://acme.enterprise.slack.com/x"))
        self.assertIn("tenant_host", self.kinds("siehe acme.slack.com/archives/C1"))
        self.assertIn("tenant_host", self.kinds("https://acme.jira.com/browse/X-1"))
        self.assertEqual(self.kinds("https://app.slack.com/client https://example.atlassian.net"), [])
        self.assertNotIn("tenant_host", self.kinds("bot@acme.slack.com"))

    def test_denylist_encodings(self):
        for data in ["AcmeCorp\r\n# Kommentar\r\nJane".encode("utf-8-sig"), "AcmeCorp\nJane".encode("utf-16"),
                     "AcmeCorp\nJane".encode("utf-16-le"), b"AcmeCorp\nJane"]:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / fixture_check.DENYLIST
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
                self.assertEqual(fixture_check.load_denylist(Path(tmp)), ["AcmeCorp", "Jane"], data)

    def test_values_are_masked(self):
        _, _, value = fixture_check.scan_text("xox" + "p-1234567890-abcdefghij", [])[0]
        self.assertNotIn("1234567890", value)


class Routing(unittest.TestCase):
    def test_route(self):
        result = route.route([
            "src/TaskHelper.Core/Sources/GitHubAdapter.cs",
            "src/TaskHelper.App/Flyout.xaml",
            "tests/TaskHelper.Core.Tests/Fixtures/github/search.json",
            "SPEC.md",
            ".gitignore",
        ])
        self.assertEqual(result["reviewers"]["spec-reviewer"],
                         ["src/TaskHelper.Core/Sources/GitHubAdapter.cs", "src/TaskHelper.App/Flyout.xaml"])
        self.assertIn("SPEC.md", result["reviewers"]["docs-reviewer"])
        self.assertIn("fixture_check", result["hints"])
        self.assertIn("ui-shots", result["hints"])
        self.assertEqual(result["unrouted"], [".gitignore"])


class DocTargets(unittest.TestCase):
    def test_rule_sections_exist_in_spec(self):
        known = doc_targets.spec_sections(ROOT)
        used = {s for _, doc, sections in doc_targets.RULES if doc == doc_targets.SPEC for s in sections}
        self.assertEqual(used - known, set())

    def test_map_files(self):
        result = doc_targets.map_files(["src/TaskHelper.Core/JiraAdapter.cs", "SPEC.md", "x/Unknown.cs"], {"4.3"})
        self.assertEqual(result["targets"]["SPEC.md"]["sections"], ["4.3"])
        self.assertIn("docs/setup-jira.md", result["targets"])
        self.assertEqual(result["changed_docs"], ["SPEC.md"])
        self.assertEqual(result["unmapped"], ["x/Unknown.cs"])


class AdapterCoverage(unittest.TestCase):
    def test_reasons_match_spec(self):
        spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
        for source in coverage.SOURCES.values():
            for reason in source["reasons"]:
                self.assertIn(f"`{reason}`", spec)

    def test_checks_on_minimal_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "src/TaskHelper.Core/GitHubAdapter.cs":
                    'class GitHubAdapter { Task PollAsync() { var k = $"github:{org}"; IsComplete = false; } }',
                "docs/setup-github.md": "x",
            }
            for path, text in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text, encoding="utf-8")
            result = dict(coverage.checks("github", coverage.Repo(root, list(files))))
            self.assertEqual(result["adapter_with_PollAsync"], "src/TaskHelper.Core/GitHubAdapter.cs")
            self.assertIsNotNone(result["item_key_prefix"])
            self.assertIsNotNone(result["setup_doc"])
            self.assertIsNone(result["auth_required_mapping"])


def git_repo(tmp: str, branch: str = "42-feature") -> Path:
    root = Path(tmp)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=root, check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("core.autocrlf", "false")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)
    return root


def commit_all(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


class GuardCommit(unittest.TestCase):
    TOKEN = "ghp_" + "a1B2" * 9

    def commits(self, command):
        return guard.calls(command, Path("."))[0]

    def test_detects_commit_commands(self):
        for command in ['git commit -m "x"', 'git add a && git commit -am "x"', "git -c core.x=y commit -m x",
                        "git --no-pager commit", 'git -C "a b" commit -m x', "cd x; GIT_EDITOR=true git commit",
                        "git status | cat\ngit commit -m x", "git commit -m x 2>&1"]:
            self.assertTrue(self.commits(command), command)

    def test_detects_commits_the_parser_used_to_miss(self):
        for command in ["cat > /tmp/msg <<'EOF'\nfix: don't break\nEOF\ngit commit -F /tmp/msg",
                        "# Claude's check\ngit commit -m x", "env A=1 git commit -m x",
                        "if true; then git commit -m x; fi", "for f in a; do git commit -m x; done",
                        "! git commit -m x"]:
            self.assertTrue(self.commits(command), command)

    def test_quoted_angle_bracket_is_a_message(self):
        (commit,) = self.commits('git commit -m "<feat>" a.json')
        self.assertEqual(commit.messages, ["<feat>"])
        self.assertEqual(commit.pathspecs, ["a.json"])

    def test_nested_and_continued_commits(self):
        for command in ["bash -c 'git commit -am x'", "echo $(git commit -m x)", "git \\\ncommit -m x",
                        'git -C sub \\\n  commit -m x', "if git commit -F m.txt; then echo ok; fi",
                        "sudo -u me git commit -m x", "env -i git commit -m x", 'cat <<< "hi"\ngit commit -m x',
                        "cat <<'END-OF'\nit's\nEND-OF\ngit commit -m x"]:
            self.assertTrue(self.commits(command), command)

    def test_mentions_of_commit_pass_on_main(self):
        footer = "Generated with [Claude" + " Code](https://claude.com/claude-code)"
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp, "main")
            (root / "a.json").write_text("{}", encoding="utf-8")
            commit_all(root)
            (root / "a.json").write_text(f'{{"t": "{self.TOKEN}"}}', encoding="utf-8")
            for command in ["git log --oneline | grep commit", "git help commit", "git config commit.gpgsign",
                            "git log -1 --format=%B  # last commit message", 'echo "git status, then commit"',
                            'gh issue comment 5 --body "siehe git commit abc"',
                            f"gh pr create --body \"$(cat <<'EOF'\nblocks git commit on main\n\n{footer}\nEOF\n)\""]:
                self.assertEqual(guard.problems(command, root, fixture_check, []), [], command)
            found = guard.problems("bash -c 'git commit -am x'", root, fixture_check, [])
            self.assertTrue(any("'main'" in f for f in found), found)
            self.assertTrue(any("github_token" in f for f in found), found)

    def test_trailer_with_equals_and_missing_message_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)
            found = guard.problems('git commit -m x --trailer "Co-authored-by=Claude <c@x>"', root, fixture_check, [])
            self.assertTrue(any("Footer" in f for f in found), found)
            found = guard.problems("git commit -F missing.txt", root, fixture_check, [])
            self.assertTrue(any("missing.txt" in f for f in found), found)

    def test_forced_add_of_ignored_file_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)
            (root / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
            (root / "secret.txt").write_text(self.TOKEN, encoding="utf-8")
            self.assertEqual(guard.problems("git add secret.txt && git commit -m x", root, fixture_check, []), [])
            found = guard.problems("git add -f secret.txt && git commit -m x", root, fixture_check, [])
            self.assertTrue(any("secret.txt" in f for f in found), found)

    @unittest.skipUnless(sys.platform == "win32", "Git Bash paths exist only on Windows")
    def test_git_bash_paths(self):
        self.assertEqual(guard.native("/c/Git/x", Path(".")), Path("C:/Git/x"))

    def test_ignores_commit_as_text(self):
        for command in ["git log --grep commit", 'echo "remember to git commit later"', "git commit-tree abc",
                        'gh issue comment 1 --body "dann git commit"', "git show HEAD:commit"]:
            self.assertFalse(self.commits(command), command)

    def test_parses_options(self):
        (commit,) = self.commits('git -C sub commit -am "msg" --trailer "Co-Authored-By: x" -F f.txt -- a.json')
        self.assertTrue(commit.all)
        self.assertEqual(commit.workdir, Path("sub"))
        self.assertEqual(commit.messages, ["msg", "Co-Authored-By: x"])
        self.assertEqual(commit.message_files, ["f.txt"])
        self.assertEqual(commit.pathspecs, ["a.json"])
        (commit,) = self.commits("git commit -mall")
        self.assertFalse(commit.all)

    def test_footer(self):
        self.assertTrue(guard.FOOTER.search("x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"))

    def test_blocks_main_and_footer_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp, "main")
            (root / "msg.txt").write_text("#1 feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
                                          encoding="utf-8")
            found = guard.problems("git commit -F msg.txt", root, fixture_check, [])
            self.assertTrue(any("'main'" in f for f in found))
            self.assertTrue(any("Footer" in f for f in found))

    def test_scans_files_added_in_the_same_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)
            (root / "fixtures").mkdir()
            (root / "fixtures" / "a.json").write_text('{"org": "AcmeCorp"}', encoding="utf-8")
            found = guard.problems("git add fixtures/a.json && git commit -m x", root, fixture_check, ["acmecorp"])
            self.assertTrue(any("fixtures/a.json" in f and "denylist" in f for f in found), found)
            self.assertEqual(guard.problems("git add other.txt && git commit -m x", root, fixture_check,
                                            ["acmecorp"]), [])

    def test_commit_all_reads_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)
            (root / "config.json").write_text("{}", encoding="utf-8")
            commit_all(root)
            (root / "config.json").write_text(f'{{"token": "{self.TOKEN}"}}', encoding="utf-8")
            self.assertEqual(guard.problems("git commit -m x", root, fixture_check, []), [])
            found = guard.problems("git commit -am x", root, fixture_check, [])
            self.assertTrue(any("github_token" in f for f in found), found)
            found = guard.problems("git commit -m x config.json", root, fixture_check, [])
            self.assertTrue(any("github_token" in f for f in found), found)

    def test_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)  # no fixture_check.py here: the checker cannot load
            payload = json.dumps({"tool_input": {"command": "git commit -m x"}, "cwd": str(root)})
            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(root)}), \
                    mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                    mock.patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertEqual(guard.main(), 2)
            self.assertIn("nicht prüfen", err.getvalue())

    def test_other_commands_pass_without_checking(self):
        payload = json.dumps({"tool_input": {"command": "ls -la"}, "cwd": "."})
        with mock.patch.object(sys, "stdin", io.StringIO(payload)):
            self.assertEqual(guard.main(), 0)


class StopHook(unittest.TestCase):
    def test_tooling_files_include_docs_the_tests_read(self):
        for path in [".claude/hooks/x.py", ".claude/skills/a/SKILL.md", ".claude/settings.json", "SPEC.md"]:
            self.assertTrue(stop_hook.TOOLING_FILES.search(path), path)
        self.assertFalse(stop_hook.TOOLING_FILES.search("README.md"))
        self.assertFalse(stop_hook.DOTNET_FILES.search(".claude/settings.json"))
        self.assertTrue(stop_hook.DOTNET_FILES.search("src/A/appsettings.json"))
        self.assertTrue(stop_hook.DOTNET_FILES.search(".editorconfig"))

    def test_build_output_is_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)
            for path in ["src/A/obj/A.props", "src/A/bin/Debug/A.deps.json", "src/A/A.csproj"]:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text("x", encoding="utf-8")
            cwd = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(stop_hook.changed(stop_hook.DOTNET_FILES), ["src/A/A.csproj"])
            finally:
                os.chdir(cwd)

    def test_hook_timeout_covers_all_steps(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        timeout = settings["hooks"]["Stop"][0]["hooks"][0]["timeout"]
        self.assertGreater(timeout, stop_hook.BUILD_TIMEOUT + stop_hook.TEST_TIMEOUT + stop_hook.TOOLING_TIMEOUT)

    def test_stop_hook_active_ends_quietly(self):
        with mock.patch.object(sys, "stdin", io.StringIO('{"stop_hook_active": true}')):
            self.assertEqual(stop_hook.main(), 0)

    def test_missing_sdk_is_a_note(self):
        messages, notes = [], []
        with mock.patch.object(stop_hook, "changed", return_value=["a.cs"]), \
                mock.patch.object(stop_hook, "is_green", return_value=False), \
                mock.patch.object(stop_hook.Path, "glob", return_value=iter([Path("x.slnx")])), \
                mock.patch.object(stop_hook, "has_dotnet_sdk", return_value=False):
            stop_hook.check_dotnet(messages, notes)
        self.assertEqual(messages, [])
        self.assertEqual(len(notes), 1)


class FormatHook(unittest.TestCase):
    def run_hook(self, returncode):
        payload = json.dumps({"tool_input": {"file_path": str(ROOT / "src" / "A.cs")}})
        result = subprocess.CompletedProcess([], returncode, stdout="out", stderr="")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(ROOT)}), \
                mock.patch.object(format_hook, "has_dotnet_sdk", return_value=True), \
                mock.patch.object(format_hook.subprocess, "run", return_value=result), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            return format_hook.main(), out.getvalue()

    def test_only_exit_2_is_a_finding(self):
        self.assertEqual(self.run_hook(0), (0, ""))
        self.assertEqual(self.run_hook(2)[0], 2)
        code, out = self.run_hook(1)  # e.g. "No .NET SDKs were found"
        self.assertEqual(code, 0)
        self.assertIn("systemMessage", out)


class Fetch(unittest.TestCase):
    def test_decode_blob(self):
        self.assertEqual(fetch.decode_blob("ghp_abc".encode("utf-16-le")), "ghp_abc")
        self.assertEqual(fetch.decode_blob(b"ghp_abcdefgh"), "ghp_abcdefgh")  # even length, still UTF-8

    def test_redirects_are_refused(self):
        handler = fetch.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))


class Encoding(unittest.TestCase):
    def test_scripts_print_non_cp1252_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.json"
            raw.write_text("{}", encoding="utf-8")
            out = Path(tmp) / "Łódź → out.json"
            env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
            result = subprocess.run([sys.executable, str(SKILLS / "capture-fixture" / "scripts" / "anonymize.py"),
                                     str(raw), str(out)], capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertIn("Łódź →", result.stdout.decode("utf-8"))


class Png(unittest.TestCase):
    def test_png_roundtrip(self):
        width, height = 3, 2
        bgra = bytes([10, 20, 30, 0] * (width * height))
        data = shots.png_bytes(width, height, bgra)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", data[16:24]), (width, height))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        self.assertEqual(raw[:5], bytes([0, 30, 20, 10, 255]))
        self.assertEqual(len(raw), height * (1 + width * 4))


class SkillCalls(unittest.TestCase):
    def test_skills_call_scripts_through_uv(self):
        for skill in SKILLS.glob("*/SKILL.md"):
            text = skill.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\bpython3\b", text), f"{skill}: use 'uv run --no-project python'")


if __name__ == "__main__":
    unittest.main()
