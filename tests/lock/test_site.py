"""kitlib.lock.site: the machine config's "machine_lock" object, checked; defaults when absent or
bad, and a lock run never fails on it. The machine's store reads it under the store's own home
(kitlib.lock.store.machine_home), which is refused under the tests: a test that reads one gives
that home as a temp folder."""
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from kitlib.config import user
from kitlib.lock import site
from kitlib.lock import store as lockstore
from tests.home import temp_home


def store(testing=False):
    said = []
    return types.SimpleNamespace(testing=testing, say=said.append, said=said)


def folder(case, prefix='kit-lock-site-'):
    d = tempfile.mkdtemp(prefix=prefix)
    case.addCleanup(shutil.rmtree, d, True)
    return d


def machine_home(case, **kw):
    """A new temp folder that the store's machine_home() returns for the rest of the test."""
    home = folder(case)
    patch = mock.patch.object(lockstore, 'machine_home', return_value=home, **kw)
    patch.start()
    case.addCleanup(patch.stop)
    return home


class Check(unittest.TestCase):
    def test_valid(self):
        for cfg in ({}, {"daemons": "stop"}, {"daemons": "leave"}, {"daemons": "gates"},
                    {"notice": {"dir": "C:\\notices" if site.os.name == "nt" else "/n"}},
                    {"notice": {"dir": "C:\\n" if site.os.name == "nt" else "/n",
                                "kinds": ["measure"]}}):
            self.assertEqual(site.check(cfg), [], cfg)

    def test_invalid(self):
        for cfg, part in ((["x"], "must be an object"), ({"other": 1}, "unknown key"),
                          ({"daemons": "pin"}, "daemons must be one of"),
                          ({"notice": "x"}, "notice must be an object"),
                          ({"notice": {"dir": "relative"}}, "absolute path"),
                          ({"notice": {"dir": None, "kinds": "measure"}}, "list of kind names"),
                          ({"notice": {"when": 1}}, "unknown key")):
            errs = site.check(cfg)
            self.assertTrue(any(part in e for e in errs), (cfg, errs))


class Load(unittest.TestCase):
    def test_a_scratch_store_reads_no_machine_config(self):
        s = store(testing=True)
        with mock.patch("kitlib.config.user.user_config") as uc:
            self.assertEqual(site.load(s), {})
        uc.assert_not_called()
        self.assertEqual(site.daemon_policy(s), "gates")
        self.assertIsNone(site.notice(s))

    def test_a_set_site_wins(self):
        s = store(testing=True)
        s.site = {"daemons": "leave", "notice": {"dir": "/n", "kinds": ["measure"]}}
        self.assertEqual(site.daemon_policy(s), "leave")
        self.assertEqual(site.notice(s), ("/n", ["measure"]))

    def test_the_machine_config_is_read_once(self):
        s = store()
        home = machine_home(self)
        cfg = {"machine_lock": {"daemons": "leave"}}
        with mock.patch("kitlib.config.user.user_config", return_value=cfg) as uc:
            self.assertEqual(site.daemon_policy(s), "leave")
            self.assertEqual(site.daemon_policy(s), "leave")
        self.assertEqual(uc.call_count, 1)
        uc.assert_called_once_with(home=home)

    def test_a_bad_value_is_said_and_defaults_used(self):
        s = store()
        machine_home(self)
        with mock.patch("kitlib.config.user.user_config",
                        return_value={"machine_lock": {"daemons": "pin"}}):
            self.assertEqual(site.daemon_policy(s), "gates")
        self.assertTrue(any("daemons must be one of" in x for x in s.said), s.said)

    def test_build_daemons_stop_after_gate_runs_unless_the_config_says_otherwise(self):
        for cfg, gate, want in (({}, True, True), ({}, False, False),
                                ({"daemons": "gates"}, True, True),
                                ({"daemons": "gates"}, False, False),
                                ({"daemons": "stop"}, True, True), ({"daemons": "stop"}, False, True),
                                ({"daemons": "leave"}, True, False),
                                ({"daemons": "leave"}, False, False)):
            s = store(testing=True)
            s.site = cfg
            with self.subTest(cfg=cfg, gate=gate):
                self.assertIs(site.stops_daemons(s, gate), want)

    def test_an_unreadable_config_is_said_and_defaults_used(self):
        s = store()
        machine_home(self)
        with mock.patch("kitlib.config.user.user_config", side_effect=ValueError("bad json")):
            self.assertEqual(site.load(s), {})
        self.assertTrue(any("bad json" in x for x in s.said), s.said)


class Fenced(unittest.TestCase):
    def test_the_machines_config_is_not_read_under_the_tests(self):
        # a temp home, so the same test without the fence reads an empty folder, not the real home
        temp_home(self)
        s = store()
        with mock.patch("kitlib.config.user.user_config") as uc:
            with self.assertRaises(user.HomeFenced) as cm:
                site.load(s)
        uc.assert_not_called()
        self.assertIn("Home fence", str(cm.exception))
        self.assertEqual(s.said, [])                # refused, not a warning and defaults
        self.assertFalse(hasattr(s, "site"))        # and nothing kept: the next load is refused too


class MachineHome(unittest.TestCase):
    def config(self, home, policy):
        path = user.writable_file(home)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"machine_lock": {"daemons": policy}}, f)

    def test_the_config_is_read_under_the_stores_home_not_the_environments(self):
        # create=True: where the store has no machine_home yet, the config under the environment's
        # home is read, and the test fails on the policy, not on a missing name
        mine = machine_home(self, create=True)
        self.config(mine, "leave")
        self.config(temp_home(self), "stop")        # USERPROFILE and HOME: another folder
        s = store()
        self.assertEqual(site.daemon_policy(s), "leave")
        self.assertEqual(s.said, [])


if __name__ == "__main__":
    unittest.main()
