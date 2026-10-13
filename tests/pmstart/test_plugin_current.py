"""kitlib.pmstart.plugin_current: the session-start line when the pm-kit plugin export is behind
kit main, a folder's last refresh failed, or the state cannot be read (docs/plugin-source.md)."""
import datetime
import os
import unittest

from kitlib import pluginsource as P
from kitlib import pmstart
from kitlib.pluginsource import claude
from tests.home import temp_home
from tests.pluginsource import FakeProc, Kit

NOW = datetime.datetime(2026, 10, 11, 9, 0)
FAKES = dict(pm_data=lambda: None, handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"}, stubs={"repos": {}},
             watcher={"beat": lambda: None, "now": 0},
             foreground_lock={"read": lambda: 0},
             agent_temp={"env": lambda: "X:\\temp", "sweep_key": lambda: {"dir": "X:\\temp"},
                         "task_exists": lambda name: True})


class PluginCurrentTest(unittest.TestCase):
    def setUp(self):
        self.k = Kit(self)
        self.s = P.Settings(self.k.root, False, "origin")
        self.folders = []

    def check(self):
        return pmstart.plugin_current(settings=self.s, kit=self.k.kit, folders=self.folders)

    def refresh(self, folders, proc):
        runner = claude.Runner(self.k.home, claude="claude.cmd", run_proc=proc)
        return P.refresh(self.k.kit, self.s, folders, lambda line: None, apply=True, runner=runner,
                         now=NOW, own=(1, 1), alive=lambda pid, ct: False)

    def test_no_export_failed_folder_current_behind(self):
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].endswith('/tools/plugin_refresh.py" --migrate --apply'), got[0])
        default, two = self.folders = self.k.folders(".claude", ".claude-two")
        self.k.seed_migrated([default])
        self.refresh([default], FakeProc())
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertIn("not migrated to the export in %s" % two, got[0])
        self.assertIn("--migrate --apply", got[0])
        self.k.seed_migrated([default, two])
        self.refresh([default, two], FakeProc(fail={(".claude-two", "update"): 1}))
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertIn("refresh failed in %s" % two, got[0])
        self.refresh([default, two], FakeProc())
        self.assertEqual(self.check(), [])
        r = self.k.repo
        r.write("later.txt", "z\n")
        r.commit("later", "later.txt")
        r.git("push", "-q", "origin", "main")
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertIn("behind kit main", got[0])

    def test_unreadable_state(self):
        os.makedirs(self.k.root)
        with open(os.path.join(self.k.root, P.STATE), "w", encoding="utf-8") as fh:
            fh.write("[]")
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertIn("could not check the pm-kit plugin", got[0])

    def test_in_the_checklist_and_a_failure_isolated(self):
        temp_home(self)     # the hub check's current account reads an empty machine config
        txt = pmstart.checklist({}, NOW, procs=[], plugin_current={"settings": self.s, "kit": self.k.kit,
                                                                 "folders": []},
                                **FAKES)
        self.assertIn("- pm-kit plugin: no export at %s yet" % self.k.root, txt)

        def boom():
            raise P.RefreshError("LOCALAPPDATA is not set")
        orig = pmstart.plugin_settings
        pmstart.plugin_settings = boom
        try:
            txt = pmstart.checklist({}, NOW, procs=[], **FAKES)
        finally:
            pmstart.plugin_settings = orig
        self.assertIn("- could not check plugin_current: RefreshError: LOCALAPPDATA is not set", txt)


if __name__ == "__main__":
    unittest.main()
