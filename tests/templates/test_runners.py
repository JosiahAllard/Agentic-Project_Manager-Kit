import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.shipped import ROOT, violations

STACKS = ROOT / "templates" / "stacks"
DOTNET_EACH = STACKS / "dotnet" / "tools" / "dotnet_each.py"
JS_TESTS = STACKS / "python" / "tools" / "js_tests.py"
RUNNERS = (DOTNET_EACH, JS_TESTS)


def load(path):
    spec = importlib.util.spec_from_file_location("runner_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli(path, *args, cwd=None):
    return subprocess.run([sys.executable, str(path), *args], cwd=cwd, capture_output=True, text=True)


class DotnetEachTest(unittest.TestCase):
    def setUp(self):
        self.mod = load(DOTNET_EACH)
        self.calls = []

    def fake(self, codes):
        def run(cmd):
            self.calls.append(cmd)
            return SimpleNamespace(returncode=codes.pop(0) if codes else 0)
        return run

    def test_no_project_runs_once_at_the_root_with_no_reuse_flags(self):
        self.assertEqual(self.mod.dotnet_each("build", [], run=self.fake([])), 0)
        self.assertEqual(self.calls, [["dotnet", "build", "--nologo", "-nodeReuse:false",
                                       "-p:UseSharedCompilation=false"]])

    def test_test_adds_no_build_and_runs_each_project_in_order(self):
        self.mod.dotnet_each("test", ["a.csproj", "b.csproj"], run=self.fake([]))
        self.assertEqual([c[3:5] for c in self.calls], [["--no-build", "a.csproj"], ["--no-build", "b.csproj"]])

    def test_stops_at_the_first_failure_and_returns_its_status(self):
        self.assertEqual(self.mod.dotnet_each("build", ["a", "b"], run=self.fake([3])), 3)
        self.assertEqual(len(self.calls), 1)

    def test_bad_verb_exits_2(self):
        self.assertEqual(cli(DOTNET_EACH, "pack").returncode, 2)
        self.assertEqual(cli(DOTNET_EACH).returncode, 2)

    def test_dotnet_gate_steps_run_through_the_runner(self):
        steps = json.loads((STACKS / "dotnet" / "tools" / "gate.json").read_text(encoding="utf-8"))["steps"]
        self.assertEqual([s["cmd"][1:] for s in steps],
                         [["{root}\\tools\\dotnet_each.py", "build"], ["{root}\\tools\\dotnet_each.py", "test"]])
        for s in steps:
            self.assertEqual(s["requires"], ["dotnet"])


class JsTestsTest(unittest.TestCase):
    def setUp(self):
        self.mod = load(JS_TESTS)

    def test_verdict(self):
        v = self.mod.verdict
        good = "# pass 3\n# fail 0\n"
        self.assertEqual(v(0, good), (True, "3 test(s) passed"))
        self.assertFalse(v(1, good)[0])
        self.assertFalse(v(0, "ok\n")[0])
        self.assertFalse(v(0, "# pass 3\n# fail 1\n")[0])
        self.assertFalse(v(0, "# pass 3\n# fail 0\n# cancelled 2\n")[0])
        self.assertFalse(v(0, "# pass 0\n# fail 0\n")[0])

    def test_empty_folder_fails_and_names_it(self):
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t").mkdir()
            rc = self.mod.run_js_tests(["t"], "*.test.cjs", root=tmp, which=lambda n: "node", out=lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("under t", lines[0])

    def test_no_node_is_a_loud_skip_with_exit_0(self):
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t").mkdir()
            (Path(tmp) / "t" / "a.test.cjs").write_text("", encoding="utf-8")
            rc = self.mod.run_js_tests(["t"], "*.test.cjs", root=tmp, which=lambda n: None, out=lines.append)
        self.assertEqual(rc, 0)
        self.assertIn(self.mod.SKIP_MARK, lines[0])
        self.assertIn("    not run: t/a.test.cjs", lines)

    def test_node_run_passes_on_a_good_tap_summary(self):
        lines = []
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="v20\n" if "--version" in cmd else "# pass 2\n# fail 0\n",
                                   stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t").mkdir()
            (Path(tmp) / "t" / "a.test.cjs").write_text("", encoding="utf-8")
            rc = self.mod.run_js_tests(["t"], "*.test.cjs", root=tmp, which=lambda n: "node", run=run,
                                       out=lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[1], ["node", "--test", "--test-reporter=tap", "t/a.test.cjs"])
        self.assertTrue(any(ln.startswith(self.mod.PASS_MARK) for ln in lines))

    def test_cli_empty_folder_exits_1_and_missing_glob_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t").mkdir()
            self.assertEqual(cli(JS_TESTS, "--glob", "*.cjs", "t", cwd=tmp).returncode, 1)
            self.assertEqual(cli(JS_TESTS, "t", cwd=tmp).returncode, 2)

    def test_python_gate_documents_the_unwired_step(self):
        gate = json.loads((STACKS / "python" / "tools" / "gate.json").read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in gate["steps"]], ["unit-tests"])
        self.assertIn("js_tests.py", gate["comment"])
        self.assertIn('"warn": true', gate["comment"])


class RunnerShapeTest(unittest.TestCase):
    def test_runners_import_only_the_standard_library(self):
        for path in RUNNERS:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for n in names:
                    self.assertIn(n.split(".")[0], sys.stdlib_module_names, f"{path.name}: {n}")

    def test_runner_names_do_not_collide_with_kit_tools(self):
        kit_tools = {p.name for p in (ROOT / "tools").glob("*.py")}
        for path in RUNNERS:
            self.assertNotIn(path.name, kit_tools)

    def test_runners_carry_no_repo_or_owner_specifics(self):
        self.assertEqual([v for v in violations(STACKS) if "tools" in str(v)], [])


if __name__ == "__main__":
    unittest.main()
