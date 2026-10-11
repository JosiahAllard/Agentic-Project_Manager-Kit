"""pmstart.hub's full set of lines: page source resolved from the kit, the watch, refill and
process steps in both branches, and the note for an ignored hub_url (docs/pm-start.md "Config")."""
import os
import unittest

from kitlib import hubmirror, pmstart
from tests.pmstart import keep_off_launcher_repos

KIT =os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SRC = os.path.join(KIT, "claude", "hub", "action-hub.html")
URL = "https://claude.ai/artifact/X"
NOTE = "hub: pm_start.hub_url is ignored because hub_urls is set"


def setUpModule():
    keep_off_launcher_repos()   # like the other pmstart modules: never this machine's launcher.repos


def lines(cfg,account=lambda: "1", isfile=lambda p: True):
    return pmstart.hub(cfg, account=account, isfile=isfile)


class SourceTest(unittest.TestCase):

    def test_default_source_is_the_kits_page_as_an_absolute_path(self):
        self.assertEqual(pmstart.hub_source({}), (DEFAULT_SRC, True))
        self.assertIn("page %s to %s" % (DEFAULT_SRC, URL), lines({"hub_url": URL})[0])

    def test_a_relative_source_is_taken_from_the_kit_and_an_absolute_one_as_is(self):
        rel, _ = pmstart.hub_source({"hub_source": "hub/action-hub.html"}, isfile=lambda p: True)
        self.assertEqual(rel, os.path.join(KIT, "hub", "action-hub.html"))
        absolute = os.path.join(KIT, "elsewhere", "page.html")
        self.assertEqual(pmstart.hub_source({"hub_source": absolute}, isfile=lambda p: True)[0], absolute)

    def test_the_source_must_be_a_string(self):
        with self.assertRaises(TypeError):
            pmstart.hub_source({"hub_source": 5})

    def test_a_missing_source_replaces_the_publish_step_but_keeps_watch_and_process(self):
        for cfg in ({"hub_url": URL}, {}):
            out = lines(dict(cfg, hub_source="gone.html"), isfile=lambda p: False)
            text = "\n".join(out)
            self.assertIn("hub: the page source %s does not exist (pm_start.hub_source); point it at %s"
                          % (os.path.join(KIT, "gone.html"), DEFAULT_SRC), out[0])
            self.assertNotIn("republish", text)
            self.assertNotIn("no Action Hub copy is recorded", text)
            self.assertIn("ArtifactComments watch", text)
            self.assertEqual(out[-1], pmstart.HUB_PROCESS_LINE)

    def test_a_source_that_is_not_a_string_gets_its_own_line_and_keeps_watch_and_process(self):
        for cfg in ({"hub_url": URL}, {}):
            for bad, kind in ((5, "int"), (["a.html"], "list")):
                out = lines(dict(cfg, hub_source=bad))
                self.assertEqual(out[0], "hub: pm_start.hub_source is %s, not a path string: fix it in "
                                         "the machine config, or remove it to use %s" % (kind, DEFAULT_SRC))
                text = "\n".join(out)
                self.assertNotIn("republish", text)
                self.assertNotIn("no Action Hub copy is recorded", text)
                self.assertIn("ArtifactComments watch", text)
                self.assertEqual(out[-1], pmstart.HUB_PROCESS_LINE)


class BranchLinesTest(unittest.TestCase):

    def test_url_branch_republish_watch_refill_process_in_order(self):
        out = lines({"hub_url": URL})
        self.assertEqual(len(out), 4)
        self.assertTrue(out[0].startswith("hub: republish"), out[0])
        self.assertNotIn("process the hub", out[0])
        self.assertEqual(out[1], "hub: start ArtifactComments watch on %s (the owner's sends reach only "
                                 "a session that watches it), or start it again after a resume or "
                                 "compact" % URL)
        self.assertIn("refill check: ArtifactData list backlog on %s" % URL, out[2])
        self.assertIn(hubmirror.regenerate_text(), out[2])
        self.assertEqual(out[3], "hub: then process the hub (answers, stories, to-dos, pm_ack)")

    def test_no_url_branch_uses_the_same_watch_and_process_lines(self):
        out = lines({})
        self.assertEqual(len(out), 3)
        self.assertIn("no Action Hub copy is recorded for account 1", out[0])
        self.assertIn(hubmirror.regenerate_text(), out[0])
        self.assertEqual(out[1], pmstart._hub_watch_line(None))
        self.assertIn("on the new copy's URL", out[1])
        self.assertEqual(out[2], pmstart.HUB_PROCESS_LINE)
        self.assertNotIn("refill check", "\n".join(out))


class IgnoredHubUrlTest(unittest.TestCase):

    def test_both_keys_note_it_whether_or_not_this_account_has_a_url(self):
        for label in ("1", "2"):
            out = lines({"hub_urls": {"1": URL}, "hub_url": "https://claude.ai/artifact/OLD"},
                        account=lambda: label)
            self.assertTrue(out[0].startswith(NOTE), (label, out))
            self.assertNotIn("OLD", "\n".join(out))

    def test_no_note_with_only_one_key_or_an_empty_map(self):
        for cfg in ({"hub_url": URL}, {"hub_urls": {"1": URL}}, {"hub_urls": {}, "hub_url": URL}, {}):
            self.assertNotIn(NOTE, "\n".join(lines(cfg)), cfg)


if __name__ == "__main__":
    unittest.main()
