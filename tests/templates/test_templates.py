import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kitlib import config
from tests.shipped import ROOT, violations

TEMPLATES = ROOT / "templates"
STACKS = {"dotnet", "godot", "java", "python"}


class TemplateFilesTest(unittest.TestCase):
    def test_stack_set_is_complete(self):
        self.assertEqual({p.name for p in (TEMPLATES / "stacks").iterdir() if p.is_dir()}, STACKS)

    def test_every_stack_has_its_files(self):
        for s in STACKS:
            d = TEMPLATES / "stacks" / s
            for rel in ("stack.json", "tools/gate.json", ".github/workflows/quick.yml",
                        ".github/workflows/full.yml"):
                self.assertTrue((d / rel).is_file(), f"{s}/{rel}")
            stack = json.loads((d / "stack.json").read_text(encoding="utf-8"))
            for key in ("BUILD_COMMAND", "TEST_COMMAND", "FAST_TEST_COMMAND", "gitignore", "gitattributes"):
                self.assertIn(key, stack, f"{s}: {key}")

    def test_common_claude_md_keeps_its_placeholders(self):
        text = (TEMPLATES / "common" / "CLAUDE.md").read_text(encoding="utf-8")
        for ph in ("<PROJECT_NAME>", "<BUILD_COMMAND>", "<PM_LEDGER_PATH>", "<OWNER_EMAIL>"):
            self.assertIn(ph, text)

    def test_godot_template_ships_the_render_check_skill(self):
        self.assertTrue((TEMPLATES / "stacks" / "godot" / ".claude" / "skills" / "godot-render-check"
                         / "SKILL.md").is_file())

    def test_common_ships_the_ignore_files_and_the_starter_index(self):
        common = TEMPLATES / "common"
        lines = (common / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".claude/worktrees/", lines)
        self.assertIn(".codex/worktrees/", lines)
        self.assertTrue((common / "gitattributes").is_file())
        self.assertFalse((common / ".gitattributes").exists())
        self.assertIn("kw:", (common / "INDEX.md").read_text(encoding="utf-8"))

    def test_no_repo_or_owner_specifics(self):
        self.assertEqual(violations(TEMPLATES), [])


class ProfileTemplatesTest(unittest.TestCase):
    """The coordinator drafts both profiles from these; the agents find sections by heading."""

    AGENT = ["Branches and worktrees", "Build and test", "Known flakes", "Test runner", "Landing",
             "Shared resources that must be serialised", "Risk map", "Moves and renames", "Records",
             "Art", "Audio"]
    COORDINATOR = ["Plan and instructions", "Risk map, for review depth", "Agent types used here",
                   "Owner"]

    def headings(self, name):
        text = (TEMPLATES / "common" / name).read_text(encoding="utf-8")
        return {m.group(1).strip() for m in re.finditer(r"^#{2,3} (.+)$", text, re.M)}

    def test_agent_profile_template_has_every_heading(self):
        self.assertLessEqual(set(self.AGENT), self.headings("agent-profile.TEMPLATE.md"))

    def test_coordinator_profile_template_has_every_heading(self):
        self.assertLessEqual(set(self.COORDINATOR), self.headings("coordinator-profile.TEMPLATE.md"))

    def test_agent_sections_are_named_by_the_agents(self):
        agents = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "claude" / "agents").glob("*.md"))
        for h in self.AGENT:
            if h == "Risk map":  # read by the dispatch brief (`doc_index --find risk`), not named in the agents
                continue
            # the agents write "Shared resources" for the longer heading
            self.assertIn(h.replace(" that must be serialised", ""), agents, h)

    def test_coordinator_points_at_the_shipped_templates(self):
        text = (ROOT / "claude" / "agents" / "coordinator.md").read_text(encoding="utf-8")
        for name in ("coordinator-profile.TEMPLATE.md", "agent-profile.TEMPLATE.md"):
            self.assertIn("templates/common/" + name, text)
            self.assertNotIn("~/.claude/" + name, text)


class GateTemplateTest(unittest.TestCase):
    """Each tools/gate.json is a "gate" section: kitlib.config must accept it as one."""

    def test_gate_json_loads_as_a_gate_section(self):
        for s in STACKS:
            src = (TEMPLATES / "stacks" / s / "tools" / "gate.json").read_text(encoding="utf-8")
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "tools").mkdir()
                (root / "tools" / "gate.json").write_text(src, encoding="utf-8")
                (root / ".kit.json").write_text('{"gate": "tools/gate.json"}', encoding="utf-8")
                cfg = config.load(str(root))
                self.assertTrue(cfg.section("gate")["steps"], s)

    def test_python_gate_command_finds_tests_without_an_init_file(self):
        step = json.loads((TEMPLATES / "stacks" / "python" / "tools" / "gate.json")
                          .read_text(encoding="utf-8"))["steps"][0]
        argv = [sys.executable if a == "{python}" else a for a in step["cmd"]]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tests").mkdir()
            (Path(tmp) / "tests" / "test_a.py").write_text(
                "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_a(self):\n        pass\n",
                encoding="utf-8")
            done = subprocess.run(argv, cwd=tmp, capture_output=True, text=True)
        for pat in step["pass"]:
            self.assertRegex(done.stderr, pat, done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_python_gate_pass_check_needs_a_test_to_have_run(self):
        step = json.loads((TEMPLATES / "stacks" / "python" / "tools" / "gate.json")
                          .read_text(encoding="utf-8"))["steps"][0]
        zero = "\n----------------------------------------------------------------------\nRan 0 tests in 0.000s\n\nOK\n"
        self.assertTrue(any(not re.search(p, zero, re.M) for p in step["pass"]))
        one = "Ran 1 test in 0.001s\n\nOK\n"
        self.assertTrue(all(re.search(p, one, re.M) for p in step["pass"]))

    def test_common_index_has_no_bare_entry_line(self):
        from kitlib.docs import index
        text = (TEMPLATES / "common" / "INDEX.md").read_text(encoding="utf-8")
        self.assertEqual([ln for ln in text.splitlines() if index.BARE_ENTRY_RE.match(ln)], [])

    def test_no_old_schema_keys(self):
        for s in STACKS:
            text = (TEMPLATES / "stacks" / s / "tools" / "gate.json").read_text(encoding="utf-8")
            for old in ('"exit_code"', "{worktree}", "with_machine_lock", "dependency_pin_check", "cmd_windows"):
                self.assertNotIn(old, text, f"{s}: {old}")


if __name__ == "__main__":
    unittest.main()
