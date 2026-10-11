"""The launcher's shipped state-file examples (docs/launcher.md "State files") load through the
readers the launcher uses, and hold the keys `start_entry` and hand-off read, so a fresh machine
that copies them starts from files the launcher accepts."""
import os
import re
import shutil
import tempfile
import unittest

from kitlib.launcher import start, state
from kitlib.launcher.settings import LaunchError
from tests.shipped import ROOT

DOCS = ROOT / "docs"
SESSIONS_EXAMPLE = DOCS / "launcher.sessions.example.json"
COORDINATORS_EXAMPLE = DOCS / "launcher.coordinators.example.json"


class _Settings:
    def __init__(self, folder):
        self.folder = folder

    def state_dir(self):
        return self.folder


class ExamplesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = _Settings(self.tmp)

    def _copy(self, src, name):
        shutil.copyfile(str(src), os.path.join(self.tmp, name))

    def test_missing_sessions_hint_names_the_shipped_example(self):
        with self.assertRaises(LaunchError) as cm:
            state.read_sessions(self.settings)
        named = re.search(r"copy (\S+) to it", str(cm.exception)).group(1)
        self.assertTrue((ROOT / named).is_file(), named)
        self.assertEqual(ROOT / named, SESSIONS_EXAMPLE)

    def test_sessions_example_loads_and_every_entry_can_start(self):
        self._copy(SESSIONS_EXAMPLE, state.SESSIONS)
        sessions = state.read_sessions(self.settings)
        self.assertIn("_note", sessions)
        entries = state.entries(sessions)
        self.assertTrue(entries)
        for key, e in entries:
            for k in ("repo", "agent", "stem", "state", "account", "name"):
                self.assertIn(k, e, "%s: %s" % (key, k))
            self.assertIsInstance(e["agent"], (bool, str), key)
            self.assertTrue(e["name"].startswith("%s-a%s" % (e["stem"], e["account"])), key)
            args = start.entry_start_args(self.settings, key, e, str(e["account"]), None)
            self.assertEqual(args["repo"], e["repo"], key)
            self.assertEqual(args.get("no_agent", False), not e["agent"], key)

    def test_coordinators_example_loads_and_names_the_coordinator_repos(self):
        self._copy(SESSIONS_EXAMPLE, state.SESSIONS)
        self._copy(COORDINATORS_EXAMPLE, state.COORDINATORS)
        coordinators = state.read_coordinators(self.settings)
        for k in ("pm_session_id", "pm_folder", "pm_account"):
            self.assertIn(k, coordinators)
        repos = {e["repo"] for _, e in state.entries(state.read_sessions(self.settings)) if e["agent"]}
        for repo in repos:
            self.assertIn(repo, coordinators)


if __name__ == "__main__":
    unittest.main()
