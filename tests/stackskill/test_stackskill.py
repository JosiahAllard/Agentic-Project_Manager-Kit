import contextlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path

from kitlib import stackskill
from tests.shipped import ROOT

sys_path_tool = ROOT / "tools" / "stack_skill.py"
SKILL_REL = Path(".claude/skills/godot-render-check/SKILL.md")
TEMPLATE = ROOT / "templates" / "stacks" / "godot" / SKILL_REL
CMD = 'python "{kit_root}/tools/machine_lock.py" run --repo r --slug s --kind render -- x'


def make_kit(base, name="Some Kit", skill=None):
    """A fake kit folder (a space in its name) with the files resolve_kit_root wants and one skill."""
    kit = Path(base) / name
    (kit / "tools").mkdir(parents=True)
    (kit / "tools" / "machine_lock.py").write_text("", encoding="utf-8")
    (kit / "kitlib").mkdir()
    skill_file = kit / "templates" / "stacks" / "godot" / SKILL_REL
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(CMD + "\n" if skill is None else skill, encoding="utf-8")
    return kit


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "repo"
        self.target.mkdir()

    def test_path_with_a_space_is_one_quoted_argument_with_forward_slashes(self):
        kit = make_kit(self.tmp.name)
        stackskill.install("godot", str(self.target), kit_root=str(kit), apply=True)
        text = (self.target / SKILL_REL).read_text(encoding="utf-8")
        self.assertNotIn("{kit_root}", text)
        argv = shlex.split(text.strip())
        want = os.path.normpath(str(kit)).replace("\\", "/") + "/tools/machine_lock.py"
        self.assertEqual(argv[1], want)
        self.assertIn(" ", argv[1])
        self.assertNotIn("\\", argv[1])

    def test_dry_run_writes_nothing(self):
        kit = make_kit(self.tmp.name)
        lines = stackskill.install("godot", str(self.target), kit_root=str(kit))
        self.assertTrue(lines[0].startswith("would create"))
        self.assertFalse((self.target / ".claude").exists())

    def test_existing_file_is_kept(self):
        kit = make_kit(self.tmp.name)
        dst = self.target / SKILL_REL
        dst.parent.mkdir(parents=True)
        dst.write_text("mine\n", encoding="utf-8")
        lines = stackskill.install("godot", str(self.target), kit_root=str(kit), apply=True)
        self.assertEqual(dst.read_text(encoding="utf-8"), "mine\n")
        self.assertTrue(lines[0].startswith("kept"))

    def test_refuses_a_kit_root_it_cannot_use_and_writes_nothing(self):
        kit = make_kit(self.tmp.name)
        (kit / "tools" / "machine_lock.py").unlink()
        bad = [str(kit), "relative/kit", str(Path(self.tmp.name) / 'a"b'), str(Path(self.tmp.name) / "a{b}")]
        for root in bad:
            with self.assertRaises(stackskill.StackSkillRefused, msg=repr(root)):
                stackskill.install("godot", str(self.target), kit_root=root, apply=True)
        self.assertFalse((self.target / ".claude").exists())

    def test_refuses_a_kit_path_that_would_write_a_placeholder_back(self):
        kit = make_kit(self.tmp.name, name="K{kit_root}")
        with self.assertRaises(stackskill.StackSkillRefused):
            stackskill.install("godot", str(self.target), kit_root=str(kit), apply=True)
        self.assertFalse((self.target / ".claude").exists())

    def test_unknown_stack_name_is_refused(self):
        kit = make_kit(self.tmp.name)
        with self.assertRaises(stackskill.StackSkillRefused):
            stackskill.install("../x", str(self.target), kit_root=str(kit))


class RealTemplateTest(unittest.TestCase):
    def test_godot_template_uses_the_placeholder_not_a_repo_relative_path(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(text.count('python "{kit_root}/tools/machine_lock.py"'), 2)
        self.assertNotIn("python tools/machine_lock.py", text)

    def test_only_the_godot_template_carries_a_placeholder(self):
        for p in (ROOT / "templates").rglob("*"):
            if p.is_file() and p != TEMPLATE and p.suffix in {".md", ".json", ".yml", ".cmd"}:
                self.assertNotIn("{kit_root}", p.read_text(encoding="utf-8"), p)

    def test_the_real_kit_installs_the_godot_skill_with_a_machine_lock_that_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            stackskill.install("godot", tmp, apply=True)
            text = (Path(tmp) / SKILL_REL).read_text(encoding="utf-8")
        self.assertNotIn("{kit_root}", text)
        self.assertIn('python "%s/tools/machine_lock.py" run' % stackskill.resolve_kit_root(), text)
        self.assertTrue(Path(stackskill.resolve_kit_root(), "tools", "machine_lock.py").is_file())


class ToolExitCodesTest(unittest.TestCase):
    def run_tool(self, *argv):
        import runpy
        import sys
        out = io.StringIO()
        old = sys.argv
        try:
            sys.argv = ["stack_skill.py", *argv]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    runpy.run_path(str(sys_path_tool), run_name="__main__")
                except SystemExit as e:
                    return e.code, out.getvalue()
        finally:
            sys.argv = old
        return None, out.getvalue()

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_tool("install", "--stack", "godot", "-C", tmp)
            self.assertEqual(code, 0)
            self.assertIn("would create", out)
            self.assertFalse((Path(tmp) / ".claude").exists())
            code, out = self.run_tool("install", "--stack", "nosuch", "-C", tmp)
            self.assertEqual(code, 0)          # a stack without skills: nothing to copy
            code, out = self.run_tool("install", "--stack", "Bad/Name", "-C", tmp)
            self.assertEqual(code, 1)
            self.assertIn("REFUSED", out)
            code, _ = self.run_tool("install", "-C", tmp)
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
