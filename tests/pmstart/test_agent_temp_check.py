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
             watcher={"beat": lambda: None, "now": 0}, foreground_lock={"read": lambda: 0})


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


if __name__ == "__main__":
    unittest.main()
