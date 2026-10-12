"""Tests for agent_costs.py and the shared _usage.py, on made-up transcripts in a temp folder.

    python -m unittest test_agent_costs -v
"""
import contextlib
import datetime
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'usage')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)            # a run from this folder: tests.home and kitlib
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
from kitlib.config import user as kuser  # noqa: E402
from tests import home as thome  # noqa: E402
import _fixture as F  # noqa: E402
import _usage as U  # noqa: E402
import agent_costs as AC  # noqa: E402
import coordinator_profile as CP  # noqa: E402


class UsageHelpers(unittest.TestCase):
    def test_weights_and_model_multiplier(self):
        u = {"input_tokens": 10, "cache_creation_input_tokens": 8, "cache_read_input_tokens": 100,
             "output_tokens": 2}
        self.assertAlmostEqual(U.weighted(u, "claude-opus-5"), 10 + 10 + 10 + 10)
        self.assertAlmostEqual(U.weighted(u, "claude-sonnet-5"), 40 * 0.6)
        self.assertAlmostEqual(U.weighted(u, "some-new-model"), 40)
        self.assertEqual(U.weighted(None, None), 0)

    def test_since_defaults_to_days_ago_utc(self):
        now = datetime.datetime(2026, 10, 4, 1, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(U.since_value(None, 7, now), "2026-09-27")
        self.assertEqual(U.since_value("2026-09-18T16:00", 7, now), "2026-09-18T16:00")

    def test_since_compares_dates_and_timestamps_as_text(self):
        self.assertFalse("2026-09-18T05:00:00Z" < "2026-09-18")      # same day counts
        self.assertTrue("2026-09-17T23:59:59Z" < "2026-09-18")
        self.assertTrue("2026-09-18T15:59:00Z" < "2026-09-18T16:00")

    def no_config_dir_env(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_config_dirs_given_or_default(self):
        home = thome.temp_home(self)
        self.assertEqual(U.config_dirs(["/a", "/b"]), ["/a", "/b"])
        self.assertIn(home + "/.claude", U.config_dirs(None))

    def test_config_dirs_default_is_the_env_folder_and_the_home_one(self):
        # The home folder is spelled as os.path.expanduser("~/.claude") spells it.
        home = thome.temp_home(self)
        self.no_config_dir_env()
        self.assertEqual(U.config_dirs(None), [home + "/.claude"])
        self.assertEqual(U.config_dirs(None), [os.path.expanduser("~/.claude")])
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(home, "acct2")
        self.assertEqual(U.config_dirs(None), sorted([home + "/.claude",
                                                      os.path.join(home, "acct2")]))

    def test_config_dirs_default_names_one_folder_once_whichever_way_it_is_spelled(self):
        home = thome.temp_home(self)
        self.no_config_dir_env()
        spellings = [home + "/.claude", home + "/./.claude"]
        if os.name == "nt":
            spellings += [home + "\\.claude", home.upper() + "\\.CLAUDE"]
        for spelled in spellings:
            with self.subTest(spelled):
                os.environ["CLAUDE_CONFIG_DIR"] = spelled
                dirs = U.config_dirs(None)
                self.assertEqual(len(dirs), 1, dirs)
                self.assertEqual(U.same_folder_key(dirs[0]), U.same_folder_key(home + "/.claude"))

    def test_config_dirs_default_is_fenced(self):
        thome.fence_decoy(self)
        with self.assertRaises(kuser.HomeFenced):
            U.config_dirs(None)

    def test_config_dirs_given_look_up_no_home_unless_they_say_tilde(self):
        thome.fence_decoy(self)
        self.assertEqual(U.config_dirs(["/a", "b~"]), ["/a", "b~"])
        with self.assertRaises(kuser.HomeFenced):
            U.config_dirs(["~/acct"])

    # Guards (green before they were written): the next two.

    def test_a_tilde_is_the_home(self):
        home = thome.temp_home(self)
        self.assertEqual(U.config_dirs(["~", "~/acct", "~\\acct"]),
                         [home, home + "/acct", home + "\\acct"])

    def test_transcripts_under_a_tilde_root_are_fenced(self):
        thome.fence_decoy(self)
        with self.assertRaises(kuser.HomeFenced):
            U.transcripts("~/x")

    def test_transcripts_under_a_tilde_root_are_the_homes(self):
        home = thome.temp_home(self)
        p = F.write(os.path.join(home, "x", "s1.jsonl"), [])
        self.assertEqual([os.path.normpath(t) for t in U.transcripts("~/x")], [os.path.normpath(p)])

    def without_a_kit(self, stand_in):
        """Until the test ends, `from kitlib.config import user` gives `stand_in` (None: an
        ImportError) and this folder's own kit is an empty folder."""
        empty = tempfile.mkdtemp(prefix="kit-usage-nokit-")
        self.addCleanup(os.rmdir, empty)
        for patch in (mock.patch.dict(sys.modules, {"kitlib.config": stand_in}),
                      mock.patch.object(sys, "path", list(sys.path)),
                      mock.patch.object(U, "KIT_ROOT", empty)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_without_the_kit_the_home_is_expanduser(self):
        home = thome.temp_home(self)
        with mock.patch.object(kuser, "home_dir", side_effect=AssertionError("the kit was asked")):
            self.without_a_kit(None)
            self.assertIsNone(U._kit_user())
            self.assertEqual(U.home(), home)
            self.assertEqual(U.config_dirs(["~/acct"]), [home + "/acct"])

    def test_a_kitlib_that_does_not_import_or_has_no_home_dir_is_no_kit(self):
        home = thome.temp_home(self)

        class Broken(types.ModuleType):
            def __getattr__(self, name):
                raise RuntimeError("a broken kitlib: %s" % name)

        foreign = types.ModuleType("kitlib.config")
        foreign.user = types.ModuleType("kitlib.config.user")       # no home_dir
        for what, stand_in in (("broken", Broken("kitlib.config")), ("foreign", foreign)):
            with self.subTest(what):
                with mock.patch.dict(sys.modules, {"kitlib.config": stand_in}), \
                        mock.patch.object(sys, "path", list(sys.path)):
                    self.assertIsNone(U._kit_user())
                    self.assertEqual(U.home(), home)

    def test_agent_type_from_meta_or_path(self):
        with tempfile.TemporaryDirectory() as d:
            main = F.write(os.path.join(d, "s1.jsonl"), [])
            sub = F.write(os.path.join(d, "s1", "subagents", "a1.jsonl"), [],
                          meta={"agentType": "worker", "description": "build x"})
            bare = F.write(os.path.join(d, "s1", "subagents", "a2.jsonl"), [])
            self.assertEqual(U.agent_type(main), ("coordinator", ""))
            self.assertEqual(U.agent_type(sub), ("worker", "build x"))
            self.assertEqual(U.agent_type(bare), ("?", ""))

    def test_a_plugin_prefixed_agent_type_is_reported_bare(self):
        with tempfile.TemporaryDirectory() as d:
            for i, (given, want) in enumerate((("some-plugin:worker", "worker"),
                                                ("some.plugin_2:test-runner", "test-runner"),
                                                (":worker", ":worker"), ("worker:", "worker:"),
                                                ("worker", "worker"))):
                with self.subTest(given):
                    sub = F.write(os.path.join(d, "s1", "subagents", "p%d.jsonl" % i), [],
                                  meta={"agentType": given, "description": "x"})
                    self.assertEqual(U.agent_type(sub), (want, "x"))
        self.assertEqual(U.bare_type(None), None)


class AgentCosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        big = {"output_tokens": 100}                  # weight 500 on opus
        F.write(os.path.join(self.root, "s1.jsonl"), [
            F.turn("m1", "2026-09-20T00:00:00Z", usage=big),
            F.turn("m1", "2026-09-20T00:00:01Z", usage=big),      # same message id: counted once
            F.turn("m0", "2026-09-01T00:00:00Z", usage=big),      # before since: skipped
        ])
        F.write(os.path.join(self.root, "s1", "subagents", "a1.jsonl"),
                [F.turn("m2", "2026-09-21T00:00:00Z", model="claude-sonnet-5", usage=big)],
                meta={"agentType": "worker", "description": "port the tool"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_costs_dedupe_filter_and_order(self):
        rows = AC.costs(self.root, "2026-09-18")
        self.assertEqual([(round(c), at) for c, at, _ in rows], [(500, "coordinator"), (300, "worker")])

    def test_main_prints_shares(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(AC.main([self.root, "2026-09-18"]), 0)
        text = out.getvalue()
        self.assertIn("since 2026-09-18", text)
        self.assertIn(" 62.5%  coordinator", text)
        self.assertIn(" 37.5%  worker", text)
        self.assertIn("port the tool", text)

    def printed_root(self, module, root):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            module.main([root, "2026-09-18"])
        return out.getvalue().split(" | ")[0]

    def test_the_root_printed_is_the_root_read(self):
        home = thome.temp_home(self)
        for module in (AC, CP):
            with self.subTest(module.__name__):
                self.assertEqual(self.printed_root(module, "~/x"), home + "/x")
                # `~name` is read as written, so it is printed as written.
                self.assertEqual(self.printed_root(module, "~nobody/x"), "~nobody/x")


if __name__ == "__main__":
    unittest.main()
