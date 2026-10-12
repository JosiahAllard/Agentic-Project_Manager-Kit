"""pmstart.hub with one Action Hub copy per account (pm_start.hub_urls) and the hub_url fallback."""
import datetime
import json
import os
import tempfile
import unittest

from kitlib import hubmirror, pmstart
from kitlib.pmstart import fglock
from tests.pmstart import keep_off_hub_site, keep_off_launcher_repos

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime.datetime(2026, 10, 6, 9, 0)
SRC = os.path.join(KIT, "claude", "hub", "action-hub.html")    # absolute: resolved from the kit
FAKES = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"})


def setUpModule():
    keep_off_launcher_repos()   # the checklist's stubs check must not read this machine's launcher.repos
    keep_off_hub_site()         # nor its hub check this machine's hub key


def joined(cfg, **kw):
    """pmstart.hub's lines as one text."""
    return "\n".join(pmstart.hub(cfg, **kw))


def never():
    raise AssertionError("the account label was read although no hub_urls entry needs it")


class HubAccountsTest(unittest.TestCase):

    def setUp(self):
        self._orig = pmstart.pm_data_settings
        pmstart.pm_data_settings = lambda: None
        self._orig_live = fglock.read_live
        fglock.read_live = lambda user32=None: 0
        self._orig_beat = pmstart.read_heartbeat
        pmstart.read_heartbeat = lambda: None

    def tearDown(self):
        pmstart.pm_data_settings = self._orig
        fglock.read_live = self._orig_live
        pmstart.read_heartbeat = self._orig_beat

    def test_this_accounts_url_gives_the_republish_line(self):
        cfg = {"hub_urls": {"1": "https://claude.ai/artifact/ONE", "2": "https://claude.ai/artifact/TWO"},
               "hub_url": "https://claude.ai/artifact/OLD"}
        s = joined(cfg, account=lambda: "2")
        self.assertIn("republish the kit's Action Hub page %s to https://claude.ai/artifact/TWO" % SRC, s)
        self.assertNotIn("OLD", s)

    def test_no_url_for_this_account_gives_publish_record_restore(self):
        cfg = {"hub_urls": {"1": "https://claude.ai/artifact/ONE"},
               "hub_url": "https://claude.ai/artifact/OLD"}      # ignored once hub_urls has an entry
        s = joined(cfg, account=lambda: "3")
        self.assertNotIn("republish", s)
        self.assertNotIn("artifact/", s)
        self.assertIn("no Action Hub copy is recorded for account 3", s)
        self.assertIn("publish the kit's page %s" % SRC, s)
        self.assertIn(pmstart.HUB_CAPABILITIES, s)
        self.assertIn("pin it", s)
        tool = os.path.join(KIT, "tools", "hub_mirror.py")        # absolute: the PM's cwd is not the kit
        self.assertTrue(os.path.isfile(tool))
        self.assertIn('python "%s" set-url --account 3 <url>' % tool, s)
        self.assertIn('python "%s" restore --out <file>' % tool, s)
        self.assertIn(hubmirror.regenerate_text(), s)
        self.assertIn("ArtifactComments watch", s)

    def test_legacy_hub_url_republishes_without_reading_the_account(self):
        for urls in (None, {}):
            cfg = {"hub_url": "https://claude.ai/artifact/X"}
            if urls is not None:
                cfg["hub_urls"] = urls
            s = joined(cfg, account=never)
            self.assertIn("to https://claude.ai/artifact/X with the Artifact tool", s)

    def test_nothing_set_gives_the_publish_line_for_this_account(self):
        s = joined({}, account=lambda: "default")
        self.assertIn("no Action Hub copy is recorded for account default", s)
        self.assertIn("set-url --account default <url>", s)

    def test_bad_hub_urls_raises_and_is_isolated_in_the_checklist(self):
        for bad in ("u", ["u"], {"1": ""}, {"": "u"}, {"1": 5}):
            with self.assertRaises(TypeError, msg=repr(bad)):
                pmstart.hub({"hub_urls": bad}, account=never)
        txt = pmstart.checklist({"hub_urls": "u"}, NOW, procs=[], **FAKES)
        self.assertIn("could not check hub:", txt)

    def test_checklist_passes_the_account_fake(self):
        cfg = {"hub_urls": {"7": "https://claude.ai/artifact/SEVEN"}}
        txt = pmstart.checklist(cfg, NOW, procs=[], hub={"account": lambda: "7"}, **FAKES)
        self.assertIn("to https://claude.ai/artifact/SEVEN", txt)


class MachineConfigTest(unittest.TestCase):

    def test_reads_the_whole_file_and_tolerates_missing_or_bad(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            self.assertEqual(pmstart.machine_config(p), {})
            with open(p, "w", encoding="utf-8") as f:
                f.write("[1]")
            self.assertEqual(pmstart.machine_config(p), {})
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(pmstart.machine_config(p), {})
            data = {"launcher": {"accounts": {"2": ["~/.claude"]}}, "pm_start": {"hub_urls": {}}}
            with open(p, "w", encoding="utf-8-sig") as f:
                json.dump(data, f)
            self.assertEqual(pmstart.machine_config(p), data)


if __name__ == "__main__":
    unittest.main()
