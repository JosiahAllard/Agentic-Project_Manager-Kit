"""kitlib.pmstart.plugin_enabled: the session-start line for an account folder whose settings.json
does not enable the pm-kit plugin (docs/pm-start.md, docs/machine-setup.md "Default mode"). Temp
folders only: no real account folder is read."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import pmstart
from tests.pmstart import HANDOFF_FAKE, keep_off_hub_site, keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 20, 9, 0)
PID = 'pm-kit@pm-kit'
FAKES = dict(pm_data=lambda: None, handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"}, repos={},
             watcher={"beat": lambda: None, "now": 0},
             foreground_lock={"read": lambda: 0},
             agent_temp={"env": lambda: "X:\\temp", "sweep_key": lambda: {"dir": "X:\\temp"},
                         "task_exists": lambda name: True})


def setUpModule():
    keep_off_launcher_repos()   # the other checks read nothing of this machine
    keep_off_hub_site()


class PluginEnabledTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='pmstart-plugin-enabled-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def folder(self, name, doc=None, raw=None):
        d = os.path.join(self.root, name)
        os.makedirs(d)
        if doc is not None:
            raw = json.dumps(doc).encode('utf-8')
        if raw is not None:
            with open(os.path.join(d, 'settings.json'), 'wb') as f:
                f.write(raw)
        return d

    def check(self, folders):
        return pmstart.plugin_enabled(settings=object(), folders=folders)

    def test_absent_warns_true_and_false_are_silent(self):
        on = self.folder('.claude', {'enabledPlugins': {PID: True}})
        off = self.folder('.claude-off', {'enabledPlugins': {PID: False}})       # the owner's choice
        none = self.folder('.claude-none')                                       # no settings.json
        other = self.folder('.claude-other', {'enabledPlugins': {'x@y': True}, 'model': 'opus'})
        got = self.check([on, off, none, other])
        self.assertEqual(len(got), 1, got)
        self.assertIn('pm-kit plugin: not enabled in %s, %s' % (none, other), got[0])
        self.assertIn('/tools/machine_setup.py" --apply', got[0])
        self.assertIn('docs/machine-setup.md "Default mode"', got[0])
        self.assertEqual(self.check([on, off]), [])

    def test_a_settings_file_that_is_not_a_json_object_is_named(self):
        bad = self.folder('.claude-bad', raw=b'[1, 2')
        got = self.check([bad])
        self.assertEqual(len(got), 1, got)
        self.assertIn('could not check the pm-kit plugin in %s' % bad, got[0])

    def test_a_loader_that_gives_none_checks_nothing(self):
        none = self.folder('.claude-none')
        with mock.patch.object(pmstart, 'plugin_settings', lambda: None):
            self.assertEqual(pmstart.plugin_enabled(folders=[none]), [])

    def test_in_the_checklist(self):
        none = self.folder('.claude-none')
        txt = pmstart.checklist({}, NOW, procs=[], plugin_enabled={"settings": object(), "folders": [none]},
                                **FAKES)
        self.assertIn('- pm-kit plugin: not enabled in %s' % none, txt)


if __name__ == "__main__":
    unittest.main()
