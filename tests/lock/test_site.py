"""kitlib.lock.site: the machine config's "machine_lock" object, checked; defaults when absent or
bad, and a lock run never fails on it."""
import types
import unittest
from unittest import mock

from kitlib.lock import site


def store(testing=False):
    said = []
    return types.SimpleNamespace(testing=testing, say=said.append, said=said)


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
        cfg = {"machine_lock": {"daemons": "leave"}}
        with mock.patch("kitlib.config.user.user_config", return_value=cfg) as uc:
            self.assertEqual(site.daemon_policy(s), "leave")
            self.assertEqual(site.daemon_policy(s), "leave")
        self.assertEqual(uc.call_count, 1)

    def test_a_bad_value_is_said_and_defaults_used(self):
        s = store()
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
        with mock.patch("kitlib.config.user.user_config", side_effect=ValueError("bad json")):
            self.assertEqual(site.load(s), {})
        self.assertTrue(any("bad json" in x for x in s.said), s.said)


if __name__ == "__main__":
    unittest.main()
