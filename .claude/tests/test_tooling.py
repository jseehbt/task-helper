"""Tests for the scripts behind the project skills and hooks.

Run from the repository root:
    uv run --no-project python -m unittest discover -s .claude/tests
Only the pure parts are tested: no gh, no network, no Windows desktop.
"""

from __future__ import annotations

import importlib.util
import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

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
guard = load(ROOT / ".claude" / "hooks" / "guard_commit.py")


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


class GuardCommit(unittest.TestCase):
    def test_detects_commit_commands(self):
        self.assertTrue(guard.COMMIT.search('git commit -m "x"'))
        self.assertTrue(guard.COMMIT.search('git add a && git commit -am "x"'))
        self.assertFalse(guard.COMMIT.search("git log --grep commit"))
        self.assertTrue(guard.ALL_FLAG.search(' commit -am "x"'))

    def test_footer(self):
        self.assertTrue(guard.FOOTER.search("x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"))


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
