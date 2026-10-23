"""Tests for the ComfyUI lifecycle added to comfy_gen.py (ensure_running / stop / the
idle watcher). Fakes only: no real ComfyUI, no real process is started or killed here (that is
the smoke test, run by hand - see docs/reference comment in comfy_gen.py). Everything below
monkeypatches the small seams (_reachable, W.details/snapshot/terminate_verified, subprocess) so
the decision logic runs against made-up state.

usage: python -m unittest test_comfy_gen -v
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'comfy')
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
import comfy_gen as G  # noqa: E402


def iso(dt):
    return G._iso(dt)


class StateFileRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patch = mock.patch.multiple(G, STATE_DIR=self.tmp, STATE_FILE=os.path.join(self.tmp, "state.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_no_file_reads_as_none(self):
        self.assertIsNone(G.read_state())

    def test_write_then_read(self):
        G.write_state({"pid": 1, "ctime": 2})
        self.assertEqual(G.read_state(), {"pid": 1, "ctime": 2})

    def test_clear_is_idempotent(self):
        G.clear_state()
        G.write_state({"pid": 1})
        G.clear_state()
        G.clear_state()
        self.assertIsNone(G.read_state())

    def test_touch_last_used_updates_existing_state_only(self):
        self.assertIsNone(G.read_state())
        G.touch_last_used()  # no state: must not create one out of nothing
        self.assertIsNone(G.read_state())
        G.write_state({"pid": 1, "last_used": "old"})
        G.touch_last_used()
        self.assertNotEqual(G.read_state()["last_used"], "old")


class EnsureRunningOwnership(unittest.TestCase):
    """The core decision: never touch a ComfyUI this tool did not start."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patch = mock.patch.multiple(G, STATE_DIR=self.tmp, STATE_FILE=os.path.join(self.tmp, "state.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_reachable_with_no_state_is_owner_started(self):
        with mock.patch.object(G, "_reachable", return_value=True), \
             mock.patch.object(G, "_launch") as launch, \
             mock.patch.object(G, "_ensure_watcher") as watcher:
            owned = G.ensure_running()
        self.assertFalse(owned)
        launch.assert_not_called()
        watcher.assert_not_called()
        self.assertIsNone(G.read_state())

    def test_reachable_with_stale_state_is_owner_started_and_clears_it(self):
        G.write_state({"pid": 999, "ctime": 111})
        with mock.patch.object(G, "_reachable", return_value=True), \
             mock.patch.object(G, "W") as w:
            w.details.return_value = {"ctime": None}   # 999 is gone; something else answers now
            owned = G.ensure_running()
        self.assertFalse(owned)
        self.assertIsNone(G.read_state())

    def test_reachable_with_matching_state_is_tool_started(self):
        G.write_state({"pid": 42, "ctime": 555, "last_used": "old"})
        with mock.patch.object(G, "_reachable", return_value=True), \
             mock.patch.object(G, "W") as w, \
             mock.patch.object(G, "_ensure_watcher") as watcher:
            w.details.return_value = {"ctime": 555}
            owned = G.ensure_running()
        self.assertTrue(owned)
        watcher.assert_called_once()
        self.assertNotEqual(G.read_state()["last_used"], "old")   # idle clock refreshed

    def test_reused_pid_is_treated_as_owner_started(self):
        """A pid recorded as ours, now belonging to a different process (ctime differs):
        never mistaken for the process this tool started."""
        G.write_state({"pid": 42, "ctime": 555})
        with mock.patch.object(G, "_reachable", return_value=True), \
             mock.patch.object(G, "W") as w:
            w.details.return_value = {"ctime": 999}    # same pid, different creation time
            owned = G.ensure_running()
        self.assertFalse(owned)
        self.assertIsNone(G.read_state())

    def test_unreachable_starts_it_and_waits(self):
        with mock.patch.object(G, "_reachable", return_value=False), \
             mock.patch.object(G, "_launch") as launch, \
             mock.patch.object(G, "_wait_for_stats", return_value=True), \
             mock.patch.object(G, "_ensure_watcher"):
            owned = G.ensure_running(wait=5)
        self.assertTrue(owned)
        launch.assert_called_once()

    def test_unreachable_and_never_comes_up_raises(self):
        with mock.patch.object(G, "_reachable", return_value=False), \
             mock.patch.object(G, "_launch"), \
             mock.patch.object(G, "_wait_for_stats", return_value=False):
            with self.assertRaises(RuntimeError):
                G.ensure_running(wait=1)

    def test_unreachable_but_our_process_still_booting_does_not_relaunch(self):
        G.write_state({"pid": 7, "ctime": 3})
        with mock.patch.object(G, "_reachable", return_value=False), \
             mock.patch.object(G, "W") as w, \
             mock.patch.object(G, "_launch") as launch, \
             mock.patch.object(G, "_wait_for_stats", return_value=True), \
             mock.patch.object(G, "_ensure_watcher"):
            w.details.return_value = {"ctime": 3}   # our recorded pid is alive, just not up yet
            G.ensure_running(wait=5)
        launch.assert_not_called()


class StopOnlyOwnPid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patch = mock.patch.multiple(G, STATE_DIR=self.tmp, STATE_FILE=os.path.join(self.tmp, "state.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_no_state_is_a_silent_no_op(self):
        self.assertEqual(G._stop_now(manual=False), 0)

    def test_kills_the_recorded_pid_when_ctime_matches(self):
        G.write_state({"pid": 42, "ctime": 555})
        with mock.patch.object(G, "W") as w:
            w.details.return_value = {"ctime": 555}
            w.snapshot.return_value = []
            w.terminate_verified.return_value = "killed"
            G._stop_now(manual=False)
        w.terminate_verified.assert_called_once_with(42, 555)
        self.assertIsNone(G.read_state())

    def test_never_kills_a_reused_pid(self):
        G.write_state({"pid": 42, "ctime": 555})
        with mock.patch.object(G, "W") as w:
            w.details.return_value = {"ctime": 999}   # pid 42 now belongs to someone else
            G._stop_now(manual=False)
        w.terminate_verified.assert_not_called()
        self.assertIsNone(G.read_state())   # still forgotten either way: not ours to track any more

    def test_also_kills_direct_children_of_the_recorded_pid(self):
        G.write_state({"pid": 42, "ctime": 555})
        with mock.patch.object(G, "W") as w:
            def details(pid):
                return {42: {"ctime": 555}, 98: {"ctime": 100}}.get(pid, {"ctime": 777})
            w.details.side_effect = details
            # 98 is older than 42: it only names 42 as a stale ppid (its real parent died, 42 was reused)
            w.snapshot.return_value = [(42, 1, "python.exe"), (99, 42, "child.exe"), (98, 42, "older.exe"),
                                       (5, 1, "unrelated.exe")]
            w.terminate_verified.return_value = "killed"
            G._stop_now(manual=False)
        killed_pids = {c.args[0] for c in w.terminate_verified.call_args_list}
        self.assertEqual(killed_pids, {42, 99})


class IdleWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patch = mock.patch.multiple(G, STATE_DIR=self.tmp, STATE_FILE=os.path.join(self.tmp, "state.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_no_state_returns_immediately(self):
        with mock.patch("comfy_gen.time.sleep") as sleep:
            self.assertEqual(G._watch_and_stop(), 0)
        sleep.assert_not_called()

    def test_exits_without_stopping_when_state_is_replaced(self):
        G.write_state({"pid": 1, "ctime": 1, "last_used": iso(G._now())})

        def sleep_then_replace(_):
            G.write_state({"pid": 2, "ctime": 2, "last_used": iso(G._now())})

        with mock.patch("comfy_gen.time.sleep", side_effect=sleep_then_replace), \
             mock.patch.object(G, "_stop_now") as stop:
            self.assertEqual(G._watch_and_stop(), 0)
        stop.assert_not_called()

    def test_stops_once_idle_past_threshold_and_queue_empty(self):
        old = iso(G._now() - __import__("datetime").timedelta(seconds=G.IDLE_STOP_SECONDS + 5))
        G.write_state({"pid": 1, "ctime": 1, "last_used": old})
        with mock.patch("comfy_gen.time.sleep"), \
             mock.patch.object(G.C, "get_json", return_value={"queue_running": [], "queue_pending": []}), \
             mock.patch.object(G, "_stop_now", return_value=0) as stop:
            G._watch_and_stop()
        stop.assert_called_once_with(manual=False)

    def test_stays_up_while_the_queue_is_busy(self):
        old = iso(G._now() - __import__("datetime").timedelta(seconds=G.IDLE_STOP_SECONDS + 5))
        G.write_state({"pid": 1, "ctime": 1, "last_used": old})
        calls = {"n": 0}

        def sleep_side_effect(_):
            calls["n"] += 1
            if calls["n"] >= 2:
                G.write_state({"pid": 2, "ctime": 2, "last_used": iso(G._now())})  # end the test

        with mock.patch("comfy_gen.time.sleep", side_effect=sleep_side_effect), \
             mock.patch.object(G.C, "get_json", return_value={"queue_running": [{"x": 1}], "queue_pending": []}), \
             mock.patch.object(G, "_stop_now") as stop:
            G._watch_and_stop()
        stop.assert_not_called()

    def test_still_under_idle_threshold_does_not_stop(self):
        G.write_state({"pid": 1, "ctime": 1, "last_used": iso(G._now())})
        calls = {"n": 0}

        def sleep_side_effect(_):
            calls["n"] += 1
            if calls["n"] >= 2:                              # first loop: idle is small, must continue
                G.write_state({"pid": 2, "ctime": 2, "last_used": iso(G._now())})  # second: end the test

        with mock.patch("comfy_gen.time.sleep", side_effect=sleep_side_effect), \
             mock.patch.object(G, "_stop_now") as stop:
            G._watch_and_stop()
        stop.assert_not_called()
        self.assertGreaterEqual(calls["n"], 2)


class CheckWhenOff(unittest.TestCase):
    """`check` must pass while ComfyUI is off but startable, or agents fall back and never start it."""
    def run_check(self, direct, desktop):
        with mock.patch.object(G.C, "get_json", side_effect=OSError("refused")), \
             mock.patch.object(G, "_direct_launch_command", return_value=direct), \
             mock.patch.object(G, "_desktop_launch_command", return_value=desktop), \
             mock.patch.object(G, "engine_running", return_value=[]), \
             mock.patch.object(G, "ensure_running") as ensure, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            code = G.cmd_check(None)
        ensure.assert_not_called()   # check holds no lock, so it never starts ComfyUI
        return code

    def test_off_but_startable_passes(self):
        self.assertEqual(self.run_check((["python"], "C:\\x"), (None, None)), 0)

    def test_off_and_not_startable_fails(self):
        self.assertEqual(self.run_check((None, None), (None, None)), 2)


class DirectLaunchCommandIsReadOnly(unittest.TestCase):
    """No Comfy Desktop config here -> falls back cleanly rather than raising."""

    def test_missing_config_returns_none(self):
        with mock.patch.object(G, "APPDATA_COMFY", os.path.join(tempfile.mkdtemp(), "nope")):
            cmd, cwd = G._direct_launch_command()
        self.assertIsNone(cmd)
        self.assertIsNone(cwd)

    def test_missing_desktop_exe_returns_none(self):
        with mock.patch.object(G, "DESKTOP_EXE", os.path.join(tempfile.mkdtemp(), "nope.exe")):
            cmd, cwd = G._desktop_launch_command()
        self.assertIsNone(cmd)


class DesktopExe(unittest.TestCase):
    """COMFY_DESKTOP_EXE overrides the installer's default path."""

    def test_env_overrides_the_default(self):
        self.assertEqual(G.desktop_exe({"COMFY_DESKTOP_EXE": "elsewhere/Comfy.exe"}), "elsewhere/Comfy.exe")

    def test_unset_or_empty_keeps_the_default(self):
        for env in ({}, {"COMFY_DESKTOP_EXE": ""}):
            self.assertEqual(G.desktop_exe(env), G.DESKTOP_EXE_DEFAULT)


class AgentOutputDir(unittest.TestCase):
    """A ComfyUI this tool starts writes its output under %AGENT_TEMP%\\comfy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_agent_temp_set_gives_comfy_subfolder_and_creates_it(self):
        with mock.patch.dict(os.environ, {"AGENT_TEMP": self.tmp}):
            path = G._agent_output_dir()
        self.assertEqual(path, os.path.join(self.tmp, "comfy"))
        self.assertTrue(os.path.isdir(path))

    def test_no_agent_temp_anywhere_gives_none(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_TEMP"}
        fake_winreg = mock.MagicMock()
        fake_winreg.OpenKey.side_effect = OSError("no key")
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.dict(sys.modules, {"winreg": fake_winreg}):
            self.assertIsNone(G._agent_output_dir())


class ComfyPaths(unittest.TestCase):
    """The state folder is in the shared temp (kitlib.config.user.shared_temp), so the owner's
    shell and every agent find the same files; with no kit library, or no shared temp, the old
    %LOCALAPPDATA%\\Temp path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lad = os.path.join(self.tmp, "lad")
        env = dict(os.environ)
        env["LOCALAPPDATA"] = self.lad
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(sys, "path", list(sys.path))):
            p.start()
            self.addCleanup(p.stop)

    def load(self):
        """A fresh copy of comfy_gen, so its module-level paths are worked out again."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("comfy_gen_paths_probe", G.__file__)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def old_paths(self, mod):
        self.assertEqual(mod.STATE_DIR, os.path.join(self.lad, "Temp", "comfy_gen"))

    def test_state_follows_the_shared_temp(self):
        from kitlib.config import user as kuser
        shared = os.path.join(self.tmp, "shared")
        with mock.patch.object(kuser, "shared_temp", return_value=shared):
            mod = self.load()
        self.assertEqual(mod.STATE_DIR, os.path.join(shared, "comfy_gen"))
        self.assertEqual(mod.STATE_FILE, os.path.join(shared, "comfy_gen", "state.json"))
        self.assertFalse(hasattr(mod, "GPU_LOCK"))   # no private lock file: the machine lock is the one

    def test_no_kit_library_keeps_the_old_paths(self):
        with mock.patch.dict(sys.modules, {"kitlib": None, "kitlib.config": None,
                                           "kitlib.config.user": None}):
            mod = self.load()
        self.old_paths(mod)

    def test_a_shared_temp_that_cannot_be_resolved_keeps_the_old_paths(self):
        from kitlib.config import user as kuser
        with mock.patch.object(kuser, "shared_temp", side_effect=RuntimeError("no LOCALAPPDATA")):
            mod = self.load()
        self.old_paths(mod)


class MachineLockRerun(unittest.TestCase):
    """Inside another locked run comfy_gen still asks the tool for gpu; only its own re-run skips."""

    def rerun(self, env):
        args = G.argparse.Namespace(cmd="image", name="cat", lock_wait=60)
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            tool = f.name
        self.addCleanup(os.remove, tool)
        clean = {k: v for k, v in os.environ.items() if k not in ("MACHINE_LOCK_RUN", G.RERUN_ENV)}
        with mock.patch.dict(os.environ, dict(clean, **env), clear=True), \
                mock.patch.object(G, "MACHINE_LOCK_TOOL", tool), \
                mock.patch.object(G.subprocess, "call", return_value=0) as call, \
                mock.patch.object(G.sys, "exit") as exit_:
            G.machine_lock_rerun(args, ["image", "--name", "cat"])
            left = os.environ.get(G.RERUN_ENV)
        return call, exit_, left

    def test_no_enclosing_run_takes_the_lock(self):
        call, exit_, _ = self.rerun({})
        self.assertEqual(call.call_count, 1)
        cmd = call.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--kind") + 1], "local-ai")
        self.assertEqual(call.call_args[1]["env"][G.RERUN_ENV], "1")
        exit_.assert_called_once_with(0)

    def test_an_enclosing_run_still_goes_through_the_tool(self):
        # e.g. a cpu-only run: the tool runs it directly if that run holds gpu, else takes gpu
        call, exit_, _ = self.rerun({"MACHINE_LOCK_RUN": "r-cpu-run"})
        self.assertEqual(call.call_count, 1)
        exit_.assert_called_once_with(0)

    def test_its_own_rerun_skips_and_consumes_the_marker(self):
        call, exit_, left = self.rerun({"MACHINE_LOCK_RUN": "r-local-ai", G.RERUN_ENV: "1"})
        call.assert_not_called()
        exit_.assert_not_called()
        self.assertIsNone(left)

    def test_the_run_frees_comfyui_on_release(self):
        for outer in ({}, {"MACHINE_LOCK_RUN": "r-cpu-run:1:2"}):
            call, _, _ = self.rerun(outer)
            cmd = call.call_args[0][0]
            self.assertLess(cmd.index("--on-release"), cmd.index("--"))
            rel = cmd[cmd.index("--on-release") + 1]
            self.assertIn(" release ", rel + " ")
            self.assertIn(os.path.basename(G.__file__), rel)
            self.assertIn("--outer=" + outer.get("MACHINE_LOCK_RUN", ""), rel)

    def test_no_tool_refuses_rather_than_run_unlocked(self):
        args = G.argparse.Namespace(cmd="image", name="cat", lock_wait=60)
        clean = {k: v for k, v in os.environ.items() if k not in ("MACHINE_LOCK_RUN", G.RERUN_ENV)}
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(G, "MACHINE_LOCK_TOOL", os.path.join(HERE, "no-such-tool.py")), \
                mock.patch.object(G.subprocess, "call") as call, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as e:
                G.machine_lock_rerun(args, ["image", "--name", "cat"])
        self.assertEqual(e.exception.code, 2)
        call.assert_not_called()
        self.assertIn("refused", err.getvalue())

    def test_a_generating_command_takes_the_machine_lock_before_its_handler(self):
        # the machine lock's GPU slot is the only serialisation: main() hands every generating
        # command to machine_lock_rerun (which exits with the locked run's code) and never reaches
        # the handler first; there is no private lock or --force to skip it
        argv = ["comfy_gen.py", "image", "--out", HERE, "--name", "cat", "--prompt", "x"]
        with mock.patch.object(G.sys, "argv", argv), \
                mock.patch.object(G, "machine_lock_rerun", side_effect=SystemExit(0)) as rerun, \
                mock.patch.object(G, "run_handler") as handler:
            with self.assertRaises(SystemExit):
                G.main()
        rerun.assert_called_once()
        handler.assert_not_called()
        self.assertFalse(hasattr(G, "gpu_lock"))
        with mock.patch.object(G.sys, "argv", argv + ["--force"]), \
                mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as e:
            G.main()
        self.assertEqual(e.exception.code, 2)   # --force is gone (argparse refuses it)

    def test_the_rerun_child_runs_the_handler_under_the_slot(self):
        # inside its own locked re-run main() skips the rerun and runs the handler
        argv = ["comfy_gen.py", "image", "--out", HERE, "--name", "cat", "--prompt", "x"]
        env = {G.RERUN_ENV: "1", "MACHINE_LOCK_RUN": "r-local-ai"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(G.sys, "argv", argv), \
                mock.patch.object(G, "run_handler", return_value=0) as handler:
            self.assertEqual(G.main(), 0)
        handler.assert_called_once()

    def test_release_never_takes_a_slot(self):
        argv = ["comfy_gen.py", "release", "--outer="]
        with mock.patch.object(G.sys, "argv", argv), \
                mock.patch.object(G, "machine_lock_rerun") as rerun, \
                mock.patch.object(G, "cmd_release", return_value=0) as rel, \
                mock.patch.object(G, "touch_last_used"):
            with self.assertRaises(SystemExit) as e:
                G.main()
        rerun.assert_not_called()
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(rel.call_args[0][0].outer, "")


class ReleaseOnSlotEnd(unittest.TestCase):
    """cmd_release: the local-ai run's --on-release."""

    def release(self, env_run, outer, state, reachable, free=None):
        env = {k: v for k, v in os.environ.items() if k != "MACHINE_LOCK_RUN"}
        if env_run is not None:
            env["MACHINE_LOCK_RUN"] = env_run
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(G, "read_state", return_value=state), \
                mock.patch.object(G, "_tool_started_and_alive", return_value=bool(state)), \
                mock.patch.object(G, "_reachable", return_value=reachable), \
                mock.patch.object(G, "_stop_now", return_value=0) as stop, \
                mock.patch.object(G, "cmd_free", side_effect=free, return_value=0) as free_, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = G.cmd_release(G.argparse.Namespace(outer=outer))
        return rc, stop, free_

    def test_its_own_comfyui_is_stopped(self):
        rc, stop, free = self.release("r-own:1:2", "", {"pid": 42, "ctime": 5}, True)
        self.assertEqual(rc, 0)
        stop.assert_called_once_with(manual=False)
        free.assert_not_called()

    def test_the_owners_comfyui_is_freed_never_stopped(self):
        rc, stop, free = self.release("r-own:1:2", "", None, True)
        self.assertEqual(rc, 0)
        stop.assert_not_called()
        free.assert_called_once()

    def test_a_failed_free_still_exits_0(self):
        rc, stop, free = self.release("r-own:1:2", "", None, True, free=OSError("refused"))
        self.assertEqual(rc, 0)
        free.assert_called_once()

    def test_nothing_running_is_a_no_op(self):
        rc, stop, free = self.release("r-own:1:2", "", None, False)
        self.assertEqual(rc, 0)
        stop.assert_not_called()
        free.assert_not_called()

    def test_a_run_nested_in_an_enclosing_one_leaves_comfyui(self):
        rc, stop, free = self.release("r-outer:1:2", "r-outer:1:2", {"pid": 42, "ctime": 5}, True)
        self.assertEqual(rc, 0)
        stop.assert_not_called()
        free.assert_not_called()

    def test_its_own_slot_inside_a_cpu_run_is_freed(self):
        rc, stop, _ = self.release("r-gpu:3:4", "r-cpu:1:2", {"pid": 42, "ctime": 5}, True)
        stop.assert_called_once_with(manual=False)


class GeneratorOutage(unittest.TestCase):
    """A generator that is down, or drops mid-run, stops the run with a clear error (exit 2):
    never a traceback, and never an instruction to write prompts in place of outputs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(G, "touch_last_used")
        p.start()
        self.addCleanup(p.stop)

    def args(self):
        return G.argparse.Namespace(cmd="image", name="cat", out=self.tmp, model="zimage",
                                    prompt="a cat", width=1024, height=1024, count=2, seed=1,
                                    steps=None, timeout=60)

    def run_handler(self, args):
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as e:
            G.run_handler(args)
        return e.exception.code, err.getvalue()

    def assert_outage(self, code, err):
        self.assertEqual(code, G.EXIT_GENERATOR_DOWN)
        self.assertIn("GENERATOR UNAVAILABLE", err)
        self.assertIn("stop", err.lower())
        self.assertNotIn("Traceback", err)
        self.assertNotIn("ChatGPT", err)

    def test_down_before_the_run(self):
        with mock.patch.object(G, "ensure_running", side_effect=RuntimeError("did not answer")):
            code, err = self.run_handler(self.args())
        self.assert_outage(code, err)
        self.assertIn("did not answer", err)

    def test_lost_mid_run_names_what_was_saved(self):
        import urllib.error
        saved = os.path.join(self.tmp, "cat_01.png")
        calls = {"n": 0}

        def run_job(api, timeout=900):
            calls["n"] += 1
            if calls["n"] == 2:
                raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
            return {}

        def save_outputs(args, entry, meta, started, only_nodes=None):
            G.SAVED.append(saved)
            return [saved]

        with mock.patch.object(G, "preflight"), \
                mock.patch.object(G.C, "load_template", return_value={}), \
                mock.patch.object(G.C, "to_api", return_value={}), \
                mock.patch.object(G, "set_input"), mock.patch.object(G, "keep_outputs"), \
                mock.patch.object(G, "run_job", side_effect=run_job), \
                mock.patch.object(G, "save_outputs", side_effect=save_outputs):
            code, err = self.run_handler(self.args())
        self.assert_outage(code, err)
        self.assertIn(saved, err)

    def test_connection_reset_while_reading_a_result(self):
        with mock.patch.object(G, "preflight"), \
                mock.patch.object(G.C, "load_template", side_effect=ConnectionResetError("reset")):
            code, err = self.run_handler(self.args())
        self.assert_outage(code, err)

    def test_an_http_error_from_a_live_comfyui_is_exit_1_not_an_outage(self):
        import urllib.error
        for status in (404, 500):
            err_obj = urllib.error.HTTPError(G.C.HOST + "/prompt", status, "Server said no", {}, None)
            with mock.patch.object(G, "preflight"), \
                    mock.patch.object(G.C, "load_template", side_effect=err_obj):
                code, err = self.run_handler(self.args())
            self.assertEqual(code, 1, err)
            self.assertNotIn("GENERATOR UNAVAILABLE", err)
            self.assertIn("HTTP %d" % status, err)

    def test_a_url_error_without_a_socket_reason_is_exit_1(self):
        import urllib.error
        with mock.patch.object(G, "preflight"), \
                mock.patch.object(G.C, "load_template",
                                  side_effect=urllib.error.URLError("unknown url type: x")):
            code, err = self.run_handler(self.args())
        self.assertEqual(code, 1, err)
        self.assertNotIn("GENERATOR UNAVAILABLE", err)

    def test_a_job_error_is_still_exit_1(self):
        with mock.patch.object(G, "preflight"), \
                mock.patch.object(G.C, "load_template", side_effect=RuntimeError("job failed")):
            code, err = self.run_handler(self.args())
        self.assertEqual(code, 1)
        self.assertNotIn("GENERATOR UNAVAILABLE", err)


if __name__ == "__main__":
    unittest.main()
