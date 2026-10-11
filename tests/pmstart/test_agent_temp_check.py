"""kitlib.pmstart.agent_temp_lines: the session-start warnings when the agents' temp folder is not
set up (docs/pm-start.md, docs/machine-setup.md "Agent temp init"). Every reader is a fake: nothing
reads this machine's environment, config or task scheduler."""
import datetime
import unittest

from kitlib import pmstart
from tests.home import temp_home
from tests.pmstart import keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 11, 9, 0)
FAKES = dict(pm_data=lambda: None, handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
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
        self.assertEqual(txt.count("could not check agent_temp: OSError: no schtasks"), 1, txt)
        self.assertIn("PM session start checklist:", txt)

    def test_lines_alone_all_three(self):
        f, _ = fakes(env="", key={}, task=False)
        self.assertEqual(len(pmstart.agent_temp_lines(**f)), 3)


if __name__ == "__main__":
    unittest.main()
