"""hosts.codex.adapter.launcher: launch plans for a kit role as its own Codex session (the role, its sandbox and instructions come from
content/roles through the Codex renderer)."""
import os
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest import mock

from hosts.codex.adapter.launcher import execute, plan, launch_environment
from hosts.codex.adapter import content
from hosts.codex.adapter.kit import catalog
from kitlib import kitroot
from tests.guard.world import World


class Launcher(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        original = Path(self.world.wt)
        assigned = Path(self.world.main) / ".codex" / "worktrees" / "wt"
        assigned.parent.mkdir(parents=True)
        original.rename(assigned)
        self.world.wt = str(assigned)
        self.brief = Path(self.world.wt) / "brief.txt"
        self.brief.write_text('Exact brief\n$(literal); "quoted"\n', encoding="utf-8")
        self.codex_exe = Path(self.world.wt) / "codex.exe"
        self.codex_exe.touch()

    def build(self, role="worker", cwd=None, **kwargs):
        return plan(role, cwd or self.world.wt, "task-1", self.brief, classify=self.world.classify,
                    codex_exe=self.codex_exe, **kwargs)

    def test_worker_uses_existing_worktree_and_inherits_model(self):
        result = self.build()
        self.assertEqual(result.cwd, self.world.wt)
        self.assertNotIn("--model", result.argv)
        self.assertNotIn("--worktree", result.argv)
        self.assertIn("--no-daemon", result.argv)
        self.assertEqual(result.environment["PM_KIT_ROLE"], "worker")
        self.assertEqual(result.environment["PM_KIT_HOST"], "codex")

    def test_main_checkout_is_not_a_worker_assignment(self):
        with self.assertRaises(ValueError):
            self.build(cwd=self.world.main)

    def test_linked_claude_worktree_is_rejected_for_codex_roles(self):
        from kitlib.git import repo
        other = Path(self.world.main) / ".claude" / "worktrees" / "other"
        other.mkdir(parents=True)
        place = repo.Place(repo.LINKED, str(other), "", str(other / ".git"), str(Path(self.world.main) / ".git"))
        for role in ("worker", "reviewer", "test-runner"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, ".codex/worktrees"):
                plan(role, other, "task-1", self.brief, classify=lambda _: place, codex_exe=self.codex_exe)

    def test_reader_can_inspect_main_and_lander_requires_main(self):
        self.assertEqual(self.build("reviewer", self.world.main).role, "reviewer")
        self.assertEqual(self.build("lander", self.world.main).role, "lander")
        with self.assertRaises(ValueError):
            self.build("lander")

    def test_explicit_model_and_effort_are_preserved(self):
        result = self.build(model="chosen-model", effort="high")
        self.assertEqual(result.argv[result.argv.index("--model") + 1], "chosen-model")
        self.assertIn('model_reasoning_effort="high"', result.argv)

    def test_configuration_literals_parse_and_resolve_packaged_paths(self):
        result = self.build()
        parsed = {}
        for i, arg in enumerate(result.argv[:-1]):
            if arg == "--config":
                key, value = result.argv[i + 1].split("=", 1)
                parsed[key] = tomllib.loads("v=" + value)["v"]
        self.assertTrue(Path(parsed["mcp_servers.machine-lock.args"][0]).is_file())
        self.assertNotIn("${PLUGIN_ROOT}", str(parsed))
        self.assertTrue(parsed["features.hooks"])
        self.assertFalse(parsed["features.unified_exec"])
        self.assertFalse(parsed["features.shell_tool"])
        self.assertEqual(parsed["shell_environment_policy.set.PM_KIT_ROLE"], "worker")
        self.assertEqual(parsed["shell_environment_policy.set.PM_KIT_ROOT"], result.environment["PM_KIT_ROOT"])
        self.assertFalse(any(key.startswith("hooks.") for key in parsed))

    def test_brief_is_passed_verbatim_and_no_bypass_flags_are_added(self):
        result = self.build()
        self.assertTrue(result.prompt.endswith(self.brief.read_text()))
        self.assertEqual(result.argv[-1], "-")
        self.assertNotIn("--dangerously-bypass-hook-trust", result.argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", result.argv)

    def test_execution_stays_disabled_before_any_child_start(self):
        result = self.build()
        runner = mock.Mock(return_value=SimpleNamespace(returncode=3))
        before = dict(os.environ)
        with self.assertRaisesRegex(ValueError, "disabled"):
            execute(result, runner)
        runner.assert_not_called()
        self.assertEqual(os.environ, before)

    def test_child_environment_has_own_marks_and_no_inherited_identity_or_lock(self):
        parent = {"PATH": "keep", "PM_KIT_ROLE": "pm", "PM_KIT_SESSION_NAME": "parent",
                  "MACHINE_LOCK_RUN": "run", "machine_lock_other": "secret",
                  "CLAUDE_AGENT_MODEL": "claude", "CLAUDE_CODE_X": "x", "ANTHROPIC_API_KEY": "key"}
        before = dict(parent)
        result = launch_environment(self.build(), parent)
        self.assertEqual(parent, before)
        self.assertEqual(set(result), {"PATH", "PM_KIT_ROOT", "PM_KIT_ROLE", "PM_KIT_HOST"})
        self.assertEqual(result["PM_KIT_ROLE"], "worker")

    def test_the_lander_is_read_only_on_codex(self):
        result = self.build("lander", self.world.main)
        self.assertEqual(result.argv[result.argv.index("--sandbox") + 1], "read-only")
        path = os.path.join(kitroot.root(), *content.AGENTS_REL.split("/"), "kit-lander.toml")
        with open(path, "rb") as f:
            self.assertEqual(tomllib.load(f)["sandbox_mode"], "read-only")

    def test_every_roles_sandbox_and_instructions_agree_with_its_agent_file(self):
        for name, role in catalog().items():
            cwd = self.world.main if role["mode"] == "lander" else self.world.wt
            with self.subTest(role=name):
                result = self.build(name, cwd)
                path = os.path.join(kitroot.root(), *content.AGENTS_REL.split("/"), "kit-%s.toml" % name)
                with open(path, "rb") as f:
                    native = tomllib.load(f)
                self.assertEqual(result.argv[result.argv.index("--sandbox") + 1], native["sandbox_mode"])
                config = [result.argv[i + 1] for i, a in enumerate(result.argv) if a == "--config"]
                given = [c.split("=", 1)[1] for c in config if c.startswith("developer_instructions=")][0]
                self.assertEqual(tomllib.loads("v=" + given)["v"], native["developer_instructions"])

    def test_long_brief_does_not_enter_windows_command_line(self):
        self.brief.write_text("x" * 40000, encoding="utf-8")
        result = self.build()
        self.assertLess(len(" ".join(result.argv)), 20000)
        self.assertTrue(result.prompt.endswith("x" * 40000))

    def test_every_marked_role_plan_requests_unified_exec_disabled(self):
        for name, role in catalog().items():
            cwd = self.world.main if role["mode"] == "lander" else self.world.wt
            with self.subTest(role=name):
                self.assertIn("features.unified_exec=false", self.build(name, cwd).argv)
                self.assertIn("features.shell_tool=false", self.build(name, cwd).argv)

    def test_invalid_identity_or_empty_brief_is_refused(self):
        with self.assertRaises(ValueError):
            plan("worker", self.world.wt, "bad/name", self.brief, classify=self.world.classify)
        self.brief.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.build()
