import re
import tempfile
import unittest
from pathlib import Path

from kitlib.stubs import SHARED
from tests.shipped import ROOT, frontmatter, violations

SKILLS = ROOT / "hosts" / "claude" / "shell" / "skills"
GODOT_SKILL = ROOT / "templates" / "stacks" / "godot" / ".claude" / "skills"
EXPECTED = {"backlog-ops", "branch-review", "branch-sync", "coordinator-dispatch", "doc-claims-discipline",
            "flaky-test-triage", "land-branch", "machine-lock-run", "mutation-testing", "session-handoff",
            "shell-and-worktree-traps", "subagent-dispatch"}
# reference files a skill carries beside its SKILL.md
EXTRA_FILES = {"coordinator-dispatch": ["roster.md"]}
# one repo's own: never shipped in the plugin (docs/shipping.md, "Stays repo-local")
REPO_LOCAL = {"worktree-start"}
REPO_LOCAL_SUFFIX = "-train-eval"     # the training-and-evaluation workflow skill


# kit-only paths: in an agent file, written for the plugin's root (substituted at load); in a skill,
# which every host ships byte for byte (docs/content.md), as `<kit>/...`, the kit's folder; and
# repo-relative kit paths
PLUGIN_RX = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]*[\w-])")
KIT_RX = re.compile(r"<kit>/([\w./-]*[\w-])")
# what only one host understands, which a shared skill never says (docs/multi-host-plan.md, section 5)
HOST_ONLY = (("${CLAUDE_PLUGIN_ROOT}", "write <kit>/..."), ("pm-kit:", "name the role bare"),
             ("{{", "skills are not rendered: no tokens"), ("CLAUDE.md", "say the repo's instructions"),
             (".claude/", "say the repo profile or name no host folder"))
REPO_RX = re.compile(r"(?<![\w/}.~-])((?:tools|kitlib|optional)/[\w./-]+?\.(?:py|json))")
DOC_RX = re.compile(r"(?<![\w/}.~-])docs/([\w-]+\.md)")
TEMPLATE_RX = re.compile(r"(?<![\w/}.~-])templates/[\w./-]+")
# files a repo supplies itself, not the kit (the skills say so beside each)
REPO_FILES = {"tools/gate.json"}


# docs every repo writes for itself (agents read the repo's own copy). The kit may also carry a file
# of the same name (e.g. its own docs/INDEX.md); that must not make `docs/INDEX.md` in an agent
# a "kit doc" that has to be written with the plugin root.
REPO_OWNED_DOCS = {"INDEX.md", "DECISIONS.md", "conventions-digest.md"}


def _repo_relative_kit_docs(text, kit_docs):
    """Names of kit docs (files in `kit_docs`) that `text` writes repo-relative, repo-owned names excepted."""
    return [n for n in DOC_RX.findall(text) if n not in REPO_OWNED_DOCS and (kit_docs / n).exists()]


def _kit_form(p):
    """How `p` writes the kit's root: `<kit>` in a skill, the plugin-root variable in an agent file."""
    return "<kit>" if SKILLS in p.parents else "${CLAUDE_PLUGIN_ROOT}"


def _shipped_text_files():
    return (list(SKILLS.glob("*/*.md")) + list(GODOT_SKILL.glob("*/SKILL.md"))
            + list((ROOT / "hosts" / "claude" / "shell" / "agents").glob("*.md")))


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
        """Kit-only paths are written `<kit>/<path>` in a skill, `${CLAUDE_PLUGIN_ROOT}/<path>` in an agent
        file, and exist in the kit; a repo-relative `tools/<name>.py` is one a repo has through a stub
        (kitlib.stubs.SHARED) or supplies itself."""
        for p in _shipped_text_files():
            text = p.read_text(encoding="utf-8")
            for rel in PLUGIN_RX.findall(text):
                self.assertTrue((ROOT / rel).exists(), f"{p.name} names ${{CLAUDE_PLUGIN_ROOT}}/{rel}")
            for rel in KIT_RX.findall(text):
                self.assertTrue((ROOT / rel).exists(), f"{p.parent.name}/{p.name} names <kit>/{rel}")
            for rel in REPO_RX.findall(text):
                if GODOT_SKILL in p.parents:     # a repo-local template, not loaded as a plugin: no root variable
                    self.assertTrue(rel in REPO_FILES or (ROOT / rel).exists(), f"{p.parent.name} names {rel}")
                    continue
                self.assertTrue(rel in REPO_FILES or (rel.startswith("tools/") and
                                                      rel[len("tools/"):-len(".py")] in SHARED),
                                f"{p.parent.name}/{p.name} names {rel}: not a stub tool; write {_kit_form(p)}/{rel}")

    def test_no_repo_relative_kit_doc_or_template(self):
        """A kit doc (`docs/<name>.md` that exists in the kit's docs/) or a `templates/` file is written
        from the kit's root (`<kit>/...` in a skill, `${CLAUDE_PLUGIN_ROOT}/...` in an agent file), never
        repo-relative, and no text says "kit checkout's"."""
        for p in _shipped_text_files():
            if GODOT_SKILL in p.parents:     # a repo-local template, not loaded as a plugin
                continue
            text = p.read_text(encoding="utf-8")
            where = f"{p.parent.name}/{p.name}"
            for name in _repo_relative_kit_docs(text, ROOT / "docs"):
                self.fail(f"{where} names docs/{name}: a kit doc; write {_kit_form(p)}/docs/{name}")
            self.assertEqual(TEMPLATE_RX.findall(text), [], f"{where}: a kit template; write {_kit_form(p)}/templates/...")
            self.assertNotIn("kit checkout's", text, where)

    def test_skills_are_host_neutral(self):
        """A kit skill is the same bytes on every host (docs/content.md), so it says nothing only one
        host understands: no plugin-root variable, no host's dispatch prefix, no token, no host folder."""
        for p in sorted(SKILLS.glob("*/*.md")):
            text = p.read_text(encoding="utf-8")
            for needle, fix in HOST_ONLY:
                self.assertNotIn(needle, text, f"{p.parent.name}/{p.name} says {needle!r}: {fix}")

    def test_repo_owned_doc_name_passes_though_kit_has_it(self):
        with tempfile.TemporaryDirectory() as d:
            kit_docs = Path(d)
            for name in ("INDEX.md", "DECISIONS.md", "conventions-digest.md", "lock-api.md"):
                (kit_docs / name).write_text("x", encoding="utf-8")
            text = "read docs/INDEX.md, docs/DECISIONS.md, docs/conventions-digest.md and docs/lock-api.md"
            self.assertEqual(_repo_relative_kit_docs(text, kit_docs), ["lock-api.md"])


if __name__ == "__main__":
    unittest.main()
