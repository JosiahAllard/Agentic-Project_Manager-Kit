"""kitlib.pmstart.agent_temp_lines: the session-start warnings when the agents' temp folder is not
set up (docs/pm-start.md, docs/machine-setup.md "Agent temp init"). Every reader is a fake: nothing
reads this machine's environment, config or task scheduler."""
import datetime
import unittest
from unittest import mock

from kitlib import pmstart
from tests.home import temp_home
from tests.pmstart import HANDOFF_FAKE, keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 11, 9, 0)
REAL_TASK_EXISTS = pmstart.agent_temp_task_exists      # before setUpModule patches it off this machine
FAKES = dict(pm_data=lambda: None, handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"}, stubs={"repos": {}},
             watcher={"beat": lambda: None, "now": 0}, foreground_lock={"read": lambda: 0},
             cdrive={"environ": {}})


def setUpModule():
    keep_off_launcher_repos()


def fakes(env="X:\\temp", key=None, task=True):
    asked = []

    def task_exists(name):
        asked.append(name)
        if isinstance(task, Exception):
            raise task
        return task
    key = {"dir": "X:\\temp", "max_age_days": 3} if key is None else key
    return dict(env=lambda: env, sweep_key=lambda: key, task_exists=task_exists), asked


class AgentTempCheck(unittest.TestCase):
    def checklist(self, **kw):
        temp_home(self)     # the hub check's current account reads an empty machine config
        f, asked = fakes(**kw)
        return pmstart.checklist({}, NOW, procs=[], agent_temp=f, **FAKES), asked

    def test_checklist_warns_when_agent_temp_unset(self):
        txt, _ = self.checklist(env=None)
        self.assertIn("- AGENT_TEMP is not set (machine_setup.py plans it)", txt)
        self.assertNotIn("agent_temp_sweep is not set", txt)

    def test_checklist_warns_when_sweep_key_missing(self):
        txt, _ = self.checklist(key={})
        self.assertIn("- agent_temp_sweep is not set in the machine config", txt)
        self.assertNotIn("AGENT_TEMP is not set", txt)

    def test_checklist_warns_when_task_missing(self):
        txt, asked = self.checklist(task=False)
        self.assertIn("- agent temp sweep task agent-temp-sweep is not registered", txt)
        self.assertEqual(asked, ["agent-temp-sweep"])

    def test_no_agent_temp_line_when_all_set(self):
        txt, asked = self.checklist()
        self.assertNotIn("AGENT_TEMP", txt)
        self.assertNotIn("agent_temp", txt)
        self.assertNotIn("agent temp", txt)
        self.assertEqual(asked, ["agent-temp-sweep"])

    def test_agent_temp_query_failure_is_one_could_not_check_line(self):
        txt, _ = self.checklist(task=OSError("no schtasks"))
        self.assertEqual(txt.count("could not check the agent temp sweep task agent-temp-sweep: "
                                   "OSError: no schtasks"), 1, txt)
        self.assertNotIn("is not registered", txt)
        self.assertIn("PM session start checklist:", txt)

    def test_a_failed_task_check_keeps_the_env_and_config_warnings(self):
        f, _ = fakes(env="", key={}, task=RuntimeError("schtasks /Query exit 1: ERROR: Access is denied."))
        lines = pmstart.agent_temp_lines(**f)
        self.assertEqual(lines[:2], ["AGENT_TEMP is not set (machine_setup.py plans it)",
                                     "agent_temp_sweep is not set in the machine config (machine_setup.py plans it)"])
        self.assertEqual(lines[2], "could not check the agent temp sweep task agent-temp-sweep: RuntimeError: "
                                   "schtasks /Query exit 1: ERROR: Access is denied.")
        self.assertEqual(len(lines), 3)

    def test_the_default_task_check_uses_the_shared_reader_with_the_session_start_timeout(self):
        seen = []

        def fake(name, timeout):
            seen.append((name, timeout))
            raise RuntimeError("schtasks /Query timed out after %s s" % timeout)
        with mock.patch("kitlib.machinesetup.agenttemp.task_registered", fake), \
                mock.patch.object(pmstart, "agent_temp_task_exists", REAL_TASK_EXISTS):
            lines = pmstart.agent_temp_lines(env=lambda: "X:\\t", sweep_key=lambda: {"dir": "X:\\t"})
        self.assertEqual(seen, [("agent-temp-sweep", pmstart.TIMEOUT)])
        self.assertEqual(lines, ["could not check the agent temp sweep task agent-temp-sweep: RuntimeError: "
                                 "schtasks /Query timed out after 4 s"])

    def test_lines_alone_all_three(self):
        f, _ = fakes(env="", key={}, task=False)
        self.assertEqual(len(pmstart.agent_temp_lines(**f)), 3)


D_ENV = {"TEMP": "D:\\t", "TMP": "D:\\t", "PIP_CACHE_DIR": "D:\\pip", "npm_config_cache": "D:\\npm",
         "NUGET_PACKAGES": "D:\\nuget", "DOTNET_CLI_HOME": "D:\\dotnet", "CLAUDE_CONFIG_DIR": "D:\\claude",
         "NUGET_HTTP_CACHE_PATH": "D:\\nh", "GRADLE_USER_HOME": "D:\\gr", "UV_CACHE_DIR": "D:\\uv",
         "LOCALAPPDATA": "D:\\local", "USERPROFILE": "D:\\home"}
SAME = lambda p: p      # a realpath that follows no link: the tests never read this machine's folders


class CDriveCheck(unittest.TestCase):
    """cdrive_lines: the env is a plain dict, never this machine's."""
    def lines(self, **over):
        env = dict(D_ENV)
        env.update(over)
        return pmstart.cdrive_lines(environ={k: v for k, v in env.items() if v is not None},
                                  realpath=SAME)

    def test_nothing_on_c_is_no_line(self):
        self.assertEqual(self.lines(), [])

    def test_each_item_alone_on_c_names_itself_and_its_variable(self):
        for var, label in (("TEMP", "TEMP"), ("TMP", "TMP"), ("PIP_CACHE_DIR", "pip cache"),
                           ("npm_config_cache", "npm cache"), ("NUGET_PACKAGES", "nuget packages"),
                           ("DOTNET_CLI_HOME", "dotnet CLI home"), ("CLAUDE_CONFIG_DIR", "agent transcripts"),
                           ("NUGET_HTTP_CACHE_PATH", "nuget http cache"), ("GRADLE_USER_HOME", "gradle home"),
                           ("UV_CACHE_DIR", "uv cache")):
            with self.subTest(var=var):
                out = self.lines(**{var: "C:\\x"})
                self.assertEqual(len(out), 1)
                self.assertEqual(out[0].count(" (C:"), 1, out)
                self.assertIn("%s (C:\\x" % label if var != "CLAUDE_CONFIG_DIR" else
                              "agent transcripts (C:\\x\\projects", out[0])
                if var == "CLAUDE_CONFIG_DIR":      # already set: move the folder it names
                    self.assertIn("; move the folder %s names)" % var, out[0])
                    self.assertNotIn("set %s" % var, out[0])
                else:
                    self.assertIn("set %s)" % var, out[0])

    def test_defaults_apply_when_the_overrides_are_unset(self):
        out = self.lines(PIP_CACHE_DIR=None, npm_config_cache=None, NUGET_PACKAGES=None, CLAUDE_CONFIG_DIR=None,
                         NUGET_HTTP_CACHE_PATH=None, GRADLE_USER_HOME=None, UV_CACHE_DIR=None, LOCALAPPDATA="C:\\L", USERPROFILE="C:\\U")
        self.assertEqual(len(out), 1)
        for part in ("pip cache (C:\\L\\pip\\Cache; set PIP_CACHE_DIR)", "npm cache (C:\\L\\npm-cache; set npm_config_cache)",
                     "nuget packages (C:\\U\\.nuget\\packages; set NUGET_PACKAGES)",
                     "agent transcripts (C:\\U\\.claude\\projects; set CLAUDE_CONFIG_DIR)",
                     "nuget http cache (C:\\L\\NuGet\\v3-cache; set NUGET_HTTP_CACHE_PATH)",
                     "gradle home (C:\\U\\.gradle; set GRADLE_USER_HOME)", "uv cache (C:\\L\\uv\\cache; set UV_CACHE_DIR)"):
            self.assertIn(part, out[0])
        self.assertNotIn("TEMP", out[0])

    def test_an_override_on_d_beats_a_c_default(self):
        self.assertEqual(self.lines(LOCALAPPDATA="C:\\L", USERPROFILE="C:\\U"), [])

    def test_unset_dotnet_home_and_unset_bases_are_skipped(self):
        env = {"TEMP": "D:\\t"}
        self.assertEqual(pmstart.cdrive_lines(environ=env), [])

    def test_lower_case_drive_forward_slashes_and_name_case(self):
        out = pmstart.cdrive_lines(environ={"temp": "c:/work/Temp"})
        self.assertEqual(len(out), 1)
        self.assertIn("TEMP (c:/work/Temp; set TEMP)", out[0])

    def test_a_relative_path_is_skipped(self):
        self.assertEqual(pmstart.cdrive_lines(environ={"TEMP": "tmp"}), [])

    def test_a_claude_folder_linked_to_another_drive_is_not_a_hit(self):
        asked = []

        def realpath(p):
            asked.append(p)
            return "Z:\\claude" if p == "C:\\U\\.claude" else p
        env = dict(D_ENV, USERPROFILE="C:\\U")
        del env["CLAUDE_CONFIG_DIR"]
        self.assertEqual(pmstart.cdrive_lines(environ=env, realpath=realpath), [])
        self.assertEqual(asked, ["C:\\U\\.claude"])

    def test_a_claude_folder_linked_onto_c_is_a_hit(self):
        out = pmstart.cdrive_lines(environ=dict(D_ENV), realpath=lambda p: "C:\\real")
        self.assertEqual(out, ["on the C: drive: agent transcripts (C:\\real\\projects; "
                               "move the folder CLAUDE_CONFIG_DIR names)"])

    def test_the_machine_scope_environment_is_not_read(self):
        with mock.patch("kitlib.launcher.host._user_env", return_value="C:\\WINDOWS\\TEMP") as user_env, \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(pmstart.cdrive_lines(realpath=SAME), [])
        user_env.assert_not_called()

    def test_the_checklist_carries_the_line(self):
        temp_home(self)
        f, _ = fakes()
        kw = dict(FAKES, cdrive={"environ": {"TMP": "C:\\Temp"}})
        txt = pmstart.checklist({}, NOW, procs=[], agent_temp=f, **kw)
        self.assertIn("- on the C: drive: TMP (C:\\Temp; set TMP)", txt)


if __name__ == "__main__":
    unittest.main()
