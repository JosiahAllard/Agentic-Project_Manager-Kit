"""hosts.codex.adapter.dispatch: one kit tool as literal argv for a Codex role, with the broker's
prechecks (ported from the Codex branch's tests/codex/test_kit_adapter.py). The host is the run's
PM_KIT_HOST, never an inserted --provider; the model must be explicit."""
from pathlib import Path
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from hosts.codex.adapter import kit as attribution
from hosts.codex.adapter.dispatch import execute, execute_in_host, plan


ROOT = str(Path(__file__).resolve().parents[2])


class KitAdapter(unittest.TestCase):
    def test_existing_tool_is_called_by_native_argv(self):
        result = plan("preflight", [], ROOT, role="worker")
        self.assertTrue(result.argv[1].endswith("tools\\preflight.py") or result.argv[1].endswith("tools/preflight.py"))
        runner = mock.Mock(return_value=SimpleNamespace(returncode=7))
        self.assertEqual(execute(result, runner), 7)
        self.assertFalse(runner.call_args.kwargs["shell"])

    def test_runtime_host_gets_literal_argv_and_closed_stdin_without_subprocess_fallback(self):
        planned = plan("preflight", [], ROOT, role="worker")
        host = mock.Mock()
        host.run_argv.return_value = {"exit_code": 0, "output": "OK"}
        with mock.patch("kitlib.proc.spawn.run", side_effect=AssertionError("unsafe fallback")):
            result = execute_in_host(planned, host)
            with self.assertRaises(ValueError):
                execute_in_host(planned, None)
        self.assertEqual(result["exit_code"], 0)
        host.run_argv.assert_called_once_with(planned.argv, cwd=planned.cwd, stdin_closed=True)

    def test_arguments_are_literal_not_shell_code(self):
        args = ["--section", "$(literal); & text"]
        self.assertEqual(plan("doc_index", args, ROOT, role="worker").argv[-2:], tuple(args))

    def test_directory_override_and_owner_flag_abbreviations_are_refused(self):
        for args in (["-C", ROOT], ["-C" + ROOT], ["--root=" + ROOT], ["--owner-a"], ["--owner-confirmed"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan("preflight", args, ROOT, role="worker")

    def test_formatted_codex_trailer_is_truthful(self):
        from kitlib import commit
        before = commit.coauthor("model-id")
        self.assertEqual(attribution.trailer("model-id"), "Co-Authored-By: Codex model-id <noreply@openai.com>")
        self.assertEqual(commit.coauthor("model-id"), before)

    def test_missing_model_is_not_guessed(self):
        for model in (None, "", "model\nOwner-Approved: yes"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                attribution.trailer(model)

    def test_reviewer_cannot_mutate_backlog_using_a_read_verb_as_data(self):
        for args in (["add", "ID", "--summary", "next"], ["--session", "next", "add", "ID"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan("backlog", args, ROOT, role="reviewer")

    def test_reviewer_can_read_backlog_with_documented_globals(self):
        result = plan("backlog", ["--session", "a", "--no-fetch", "next"], ROOT, role="reviewer")
        self.assertEqual(result.role, "reviewer")

    def test_owner_lock_commands_and_abbreviated_force_are_refused(self):
        for args in (["division", "--set", "x"], ["hold", "whole"], ["stop", "id", "--forc"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan("machine_lock", args, ROOT, role="pm")

    def test_worker_can_run_locked_job_without_reimplementing_lock(self):
        result = plan("machine_lock", ["run", "--repo", "x", "--kind", "engine-test", "--", "python", "-m", "unittest"], ROOT, role="worker")
        self.assertEqual(result.argv[-3:], ("python", "-m", "unittest"))

    def test_another_hosts_folder_or_attribution_is_refused(self):
        planned = plan("new_worktree", ["codex/task"], ROOT, role="worker")
        self.assertEqual(planned.argv[2:], ("codex/task",), "the host comes from PM_KIT_HOST, not an argument")
        for args in (["--host", "claude", "codex/task"], ["--host=claude", "codex/task"], ["--hos", "codex", "x"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan("new_worktree", args, ROOT, role="worker")
        self.assertEqual(plan("new_worktree", ["--host", "codex", "codex/task"], ROOT, role="worker").role, "worker")

    def test_a_commit_needs_an_explicit_model_and_no_other_host(self):
        for tool in ("commit", "sync_base"):
            with self.subTest(tool=tool):
                planned = plan(tool, ["--model", "gpt-6.1-sol"], ROOT, role="worker")
                self.assertEqual(planned.argv[2:], ("--model", "gpt-6.1-sol"))
                with self.assertRaises(ValueError):
                    plan(tool, [], ROOT, role="worker")
                with self.assertRaises(ValueError):
                    plan(tool, ["--provider", "claude", "--model", "gpt-6.1-sol"], ROOT, role="worker")

    def test_a_pm_ledger_commit_needs_its_model(self):
        planned = plan("ledger", ["add", "--commit", "--model", "gpt-6.1-sol"], ROOT, role="pm")
        self.assertEqual(planned.argv[2:], ("add", "--commit", "--model", "gpt-6.1-sol"))
        for args in (["add", "--comm", "--model", "gpt-6.1-sol"], ["add", "--commit"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan("ledger", args, ROOT, role="pm")

    def test_lander_and_test_runner_cannot_mutate_backlog(self):
        for role in ("lander", "test-runner"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                plan("backlog", ["add", "EX-FINDING", "--summary", "finding"], ROOT, role=role)
            self.assertEqual(plan("backlog", ["next"], ROOT, role=role).role, role)
        self.assertEqual(plan("gate", ["--full"], ROOT, role="test-runner").role, "test-runner")
        with self.assertRaises(ValueError):
            plan("gate", ["--full"], ROOT, role="lander")

    def test_ledger_write_is_pm_workflow_only_and_commit_flags_remain_blocked(self):
        with self.assertRaises(ValueError):
            plan("ledger", ["add"], ROOT, role="worker")
        self.assertEqual(plan("ledger", ["add"], ROOT, role="pm").role, "pm")
        with self.assertRaises(ValueError):
            plan("ledger", ["add", "--comm"], ROOT, role="pm")

    def test_landing_needs_role_verdict_and_full_hashes(self):
        args = ["codex/task", "--expect-main", "1" * 40, "--expect-tip", "2" * 40]
        for role, verdict in (("worker", "LAND"), ("lander", None), ("coordinator", "LAND")):
            with self.subTest(role=role), self.assertRaises(ValueError):
                plan("land", args, ROOT, role=role, review_verdict=verdict)
        self.assertEqual(plan("land", args, ROOT, role="lander", review_verdict="LAND").role, "lander")
        with self.assertRaises(ValueError):
            plan("land", ["task", "--expect-main", "123", "--expect-tip", "2" * 40], ROOT, role="lander", review_verdict="LAND")

    def test_reading_landing_queue_does_not_require_landing_privilege(self):
        self.assertEqual(plan("land", ["--queue"], ROOT, role="reviewer").role, "reviewer")

    def test_shared_roster_write_and_unknown_tools_are_refused(self):
        for tool, args in (("agent_roster", ["--write"]), ("agent_roster", ["--w"]), ("../other", []), ("machine_setup", [])):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                plan(tool, args, ROOT, role="pm")

    def test_duplicate_expected_hashes_do_not_validate_the_first_value(self):
        for flag in ("--expect-main", "--expect-tip"):
            args = ["task", "--expect-main", "1" * 40, "--expect-tip", "2" * 40, flag + "=other"]
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                plan("land", args, ROOT, role="lander", review_verdict="LAND")
        with self.assertRaises(ValueError):
            plan("land", ["task", "--expect-main", "1" * 40, "--expect-tip", "2" * 40,
                          "--expect-ma", "bad"], ROOT, role="lander", review_verdict="LAND")

    def test_multiple_land_tip_hashes_are_all_validated_and_execution_is_disabled(self):
        args = ["task-a", "task-b", "--expect-main", "1" * 40, "--expect-tip", "2" * 40,
                "--expect-tip", "3" * 40]
        planned = plan("land", args, ROOT, role="lander", review_verdict="LAND")
        runner = mock.Mock()
        with self.assertRaisesRegex(ValueError, "disabled"):
            execute(planned, runner)
        runner.assert_not_called()

    def test_gate_wait_result_land_kind_is_not_a_landing(self):
        result = plan("gate", ["wait-result", "--kind", "land"], ROOT, role="worker")
        self.assertEqual(result.argv[-2:], ("--kind", "land"))

    def test_repo_label_exception_is_not_available_to_checkout_target_options(self):
        with self.assertRaises(ValueError):
            plan("preflight", ["--repo", "other"], ROOT, role="worker")
        self.assertEqual(plan("ledger", ["show", "--repo", "label"], ROOT, role="reviewer").role, "reviewer")

    def test_dispatch_child_environment_scrubs_parent_marks_without_mutating_them(self):
        parent = {"PATH": "keep", "PM_KIT_ROLE": "pm", "PM_KIT_SESSION_NAME": "parent",
                  "MACHINE_LOCK_RUN": "run", "CLAUDE_AGENT_MODEL": "model", "CLAUDE_CODE_X": "x"}
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.dict(os.environ, parent, clear=True):
            execute(plan("preflight", [], ROOT, role="worker"), runner)
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(runner.call_args.kwargs["env"], {"PATH": "keep", "MACHINE_LOCK_RUN": "run",
                                                          "PM_KIT_HOST": "codex"})

    def test_gate_dispatched_inside_lock_run_retains_nesting_and_cpu_context(self):
        parent = {"PATH": "keep", "MACHINE_LOCK_RUN": "r-parent:123:456",
                  "MACHINE_LOCK_CPUMASK": "15", "MACHINE_LOCK_EXTRA": "keep-too"}
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.dict(os.environ, parent, clear=True):
            execute(plan("gate", [], ROOT, role="worker"), runner)
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(runner.call_args.kwargs["env"], dict(parent, PM_KIT_HOST="codex"))
