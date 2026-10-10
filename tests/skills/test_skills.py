import re
import unittest

from tests.shipped import ROOT, frontmatter, violations

SKILLS = ROOT / "claude" / "skills"
GODOT_SKILL = ROOT / "templates" / "stacks" / "godot" / ".claude" / "skills"
EXPECTED = {"backlog-ops", "branch-review", "branch-sync", "coordinator-dispatch", "doc-claims-discipline",
            "flaky-test-triage", "land-branch", "machine-lock-run", "mutation-testing", "session-handoff",
            "shell-and-worktree-traps", "subagent-dispatch"}
# reference files a skill carries beside its SKILL.md
EXTRA_FILES = {"coordinator-dispatch": ["roster.md"]}
# one repo's own: never shipped in the plugin (docs/shipping.md, "Stays repo-local")
REPO_LOCAL = {"worktree-start"}
REPO_LOCAL_SUFFIX = "-train-eval"     # the training-and-evaluation workflow skill


class SkillFilesTest(unittest.TestCase):
    def test_set_is_complete(self):
        self.assertEqual({p.name for p in SKILLS.iterdir() if p.is_dir()}, EXPECTED)
        self.assertFalse(EXPECTED & REPO_LOCAL)
        self.assertFalse([n for n in EXPECTED if n.endswith(REPO_LOCAL_SUFFIX)])

    def test_each_skill_is_one_skill_md_plus_listed_references(self):
        for name in EXPECTED:
            self.assertEqual(sorted(p.name for p in (SKILLS / name).iterdir()),
                             sorted(["SKILL.md"] + EXTRA_FILES.get(name, [])), name)

    def test_frontmatter_parses(self):
        files = [SKILLS / n / "SKILL.md" for n in EXPECTED] + list(GODOT_SKILL.glob("*/SKILL.md"))
        self.assertEqual(len(files), len(EXPECTED) + 1)
        for p in files:
            fm = frontmatter(p.read_text(encoding="utf-8"))
            self.assertIsNotNone(fm, p)
            self.assertEqual(fm.get("name"), p.parent.name, p)
            self.assertTrue(fm.get("description", "").startswith("Use "), p)

    def test_no_repo_or_owner_specifics(self):
        self.assertEqual(violations(SKILLS), [])
        self.assertEqual(violations(GODOT_SKILL), [])

    def test_kit_tools_named_exist(self):
        """A skill names a kit tool by path: every `tools/<name>.py`, `optional/..` and kitlib path must exist."""
        rx = re.compile(r"((?:tools|kitlib|optional)/[\w./-]+?\.(?:py|json))")
        # files a repo supplies itself, not the kit (the skills say so beside each)
        repo_files = {"tools/doc_index.py", "tools/doc_budget.py", "tools/gate.json"}
        for p in list(SKILLS.glob("*/SKILL.md")) + list(GODOT_SKILL.glob("*/SKILL.md")):
            for rel in rx.findall(p.read_text(encoding="utf-8")):
                if rel in repo_files:
                    continue
                self.assertTrue((ROOT / rel).exists(), f"{p.parent.name} names {rel}")


if __name__ == "__main__":
    unittest.main()
