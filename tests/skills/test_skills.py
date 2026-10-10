import re
import unittest

from kitlib.stubs import SHARED
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


# kit-only paths, written for the plugin's root (substituted at load); and repo-relative kit paths
PLUGIN_RX = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]*[\w-])")
REPO_RX = re.compile(r"(?<![\w/}.~-])((?:tools|kitlib|optional)/[\w./-]+?\.(?:py|json))")
# files a repo supplies itself, not the kit (the skills say so beside each)
REPO_FILES = {"tools/gate.json"}


def _shipped_text_files():
    return (list(SKILLS.glob("*/*.md")) + list(GODOT_SKILL.glob("*/SKILL.md"))
            + list((ROOT / "claude" / "agents").glob("*.md")))


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
        """Kit-only paths are written `${CLAUDE_PLUGIN_ROOT}/<path>` and exist in the kit; a repo-relative
        `tools/<name>.py` is one a repo has through a stub (kitlib.stubs.SHARED) or supplies itself."""
        for p in _shipped_text_files():
            text = p.read_text(encoding="utf-8")
            for rel in PLUGIN_RX.findall(text):
                self.assertTrue((ROOT / rel).exists(), f"{p.name} names ${{CLAUDE_PLUGIN_ROOT}}/{rel}")
            for rel in REPO_RX.findall(text):
                if GODOT_SKILL in p.parents:     # a repo-local template, not loaded as a plugin: no root variable
                    self.assertTrue(rel in REPO_FILES or (ROOT / rel).exists(), f"{p.parent.name} names {rel}")
                    continue
                self.assertTrue(rel in REPO_FILES or (rel.startswith("tools/") and
                                                      rel[len("tools/"):-len(".py")] in SHARED),
                                f"{p.parent.name}/{p.name} names {rel}: not a stub tool; write ${{CLAUDE_PLUGIN_ROOT}}/{rel}")


if __name__ == "__main__":
    unittest.main()
