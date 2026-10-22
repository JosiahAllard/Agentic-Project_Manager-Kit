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

    # Guard (green before it was written): the fallback itself.
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

    def test_two_turns_with_no_message_id_are_both_counted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        F.write(os.path.join(tmp.name, "s.jsonl"), [
            F.turn(None, "2026-09-20T00:00:00Z", usage={"output_tokens": 100}),
            F.turn(None, "2026-09-20T00:00:01Z", usage={"output_tokens": 100}),
        ])
        rows = AC.costs(tmp.name, "2026-09-18")
        self.assertEqual(round(sum(c for c, _, _ in rows)), 1000)

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

    # Guard (green before it was written): on this Python expanduser leaves `~name` as written
    # too, so the old print gave the same text.
    def test_the_root_printed_is_the_root_read(self):
        home = thome.temp_home(self)
        for module in (AC, CP):
            with self.subTest(module.__name__):
                self.assertEqual(self.printed_root(module, "~/x"), home + "/x")
                # `~name` is read as written, so it is printed as written.
                self.assertEqual(self.printed_root(module, "~nobody/x"), "~nobody/x")


# The store history as AC.PULL_LOG prints it. EX-A is pulled onto kit/alpha, released, pulled
# again onto kit/alpha-again and closed (its file deleted); EX-C and EX-D share kit/bundle; the
# slug "beta" is kit/beta's and launcher/beta's, both pulled from 13:00.
PULL_LOG = """commit aaa1 2026-10-01T08:00:00-04:00
diff --git a/kit/backlog/EX-A.md b/kit/backlog/EX-A.md
--- a/kit/backlog/EX-A.md
+++ b/kit/backlog/EX-A.md
+pulled_by: kit/alpha
diff --git a/kit/backlog/EX-B.md b/kit/backlog/EX-B.md
+pulled_by: kit/beta
diff --git a/kit/backlog/EX-C.md b/kit/backlog/EX-C.md
+pulled_by: kit/bundle
diff --git a/kit/backlog/EX-D.md b/kit/backlog/EX-D.md
+pulled_by: kit/bundle
commit bbb2 2026-10-01T13:00:00Z
diff --git a/launcher/backlog/EX-X.md b/launcher/backlog/EX-X.md
+pulled_by: launcher/beta
commit ccc3 2026-10-02T12:00:00Z
diff --git a/kit/backlog/EX-A.md b/kit/backlog/EX-A.md
-pulled_by: kit/alpha
+pulled_by: -
commit ddd4 2026-10-03T12:00:00Z
diff --git a/kit/backlog/EX-A.md b/kit/backlog/EX-A.md
+pulled_by: kit/alpha-again
commit eee5 2026-10-04T12:00:00Z
diff --git a/kit/backlog/EX-A.md b/kit/backlog/EX-A.md
deleted file mode 100644
--- a/kit/backlog/EX-A.md
+++ /dev/null
-id: EX-A
-pulled_by: kit/alpha-again
"""
OUT = {"output_tokens": 1000}                   # $0.025 on opus 5
WT = "Z:\\r\\.claude\\worktrees\\"


def agent(tid, prompt):
    return F.tool(tid, "Agent", prompt=prompt)


class RecordCosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "acct1", "projects", "p")
        self.root2 = os.path.join(self.tmp.name, "acct2", "projects", "p")
        # A coordinator main session spanning several records, placed turn by turn.
        F.write(os.path.join(self.root, "s1.jsonl"), [
            F.setting("pm-kit:coordinator"),
            F.user("2026-10-01T12:59:00Z", "start up"),
            F.turn("c1", "2026-10-01T13:00:00Z", usage=OUT, branch="main",
                   content=[agent("t1", "Build on branch kit/alpha in its worktree")]),
            F.turn("c2", "2026-10-01T13:05:00Z", usage=OUT, content=[F.shell("t2", "git -C %sbeta log" % WT)]),
            F.turn("c3", "2026-10-01T13:10:00Z", usage=OUT, content=[agent("t3", "review kit/beta")]),
            F.turn("c4", "2026-10-01T13:15:00Z", usage=OUT),
            F.turn("c5", "2026-10-01T13:20:00Z", usage=OUT,
                   content=[F.shell("t5", "git log kit/alpha kit/beta")]),
            F.turn("c6", "2026-10-02T13:00:00Z", usage=OUT, content=[agent("t6", "redo kit/alpha")]),
            F.turn("c0", "2026-09-01T00:00:00Z", usage=OUT, content=[agent("t0", "kit/alpha")]),
        ])
        # A worker run from the main checkout, its tools in its worktree: the record's second pull.
        F.write(os.path.join(self.root, "s1", "subagents", "a1.jsonl"), [
            F.user("2026-10-03T12:59:00Z", "Build the thing. Secret description text."),
            F.turn("w1", "2026-10-03T13:00:00Z", usage=OUT, branch="main",
                   content=[F.tool("t7", "Edit", file_path="Z:/r/.claude/worktrees/alpha-again/x.py")]),
            F.turn("w2", "2026-10-03T13:01:00Z", usage=OUT, model="mystery-model",
                   content=[F.shell("t8", "git -C %salpha-again status" % WT)]),
            F.turn("w3", "2026-10-03T13:02:00Z", usage={
                "cache_creation_input_tokens": 3000,
                "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000}}),
        ], meta={"agentType": "pm-kit:worker", "description": "Secret description text"})
        # A bundle branch, by the turn's own gitBranch.
        F.write(os.path.join(self.root, "s1", "subagents", "a2.jsonl"), [
            F.user("2026-10-01T14:00:00Z", "fix it"),
            F.turn("b1", "2026-10-01T14:01:00Z", usage=OUT, branch="kit/bundle"),
        ], meta={"agentType": "worker", "description": "x"})
        # An analyst naming two branches evenly, and one naming none.
        F.write(os.path.join(self.root, "s1", "subagents", "a3.jsonl"), [
            F.user("2026-10-01T14:00:00Z", "compare kit/alpha with kit/beta"),
            F.turn("r1", "2026-10-01T14:01:00Z", usage=OUT),
        ], meta={"agentType": "analyst", "description": "x"})
        F.write(os.path.join(self.root, "s1", "subagents", "a4.jsonl"), [
            F.user("2026-10-01T14:00:00Z", "read the docs"),
            F.turn("n1", "2026-10-01T14:01:00Z", usage=OUT),
        ], meta={"agentType": "analyst", "description": "x"})
        # A second account: a 4-to-1 majority places the subagent; a copied turn counts once.
        F.write(os.path.join(self.root2, "s2", "subagents", "a5.jsonl"), [
            F.user("2026-10-01T14:00:00Z", "land kit/beta (not kit/alpha)"),
            F.turn("l1", "2026-10-01T14:01:00Z", usage=OUT,
                   content=[F.shell("t9", "git -C x merge kit/beta; git log kit/beta; git show kit/beta")]),
            F.turn("w1", "2026-10-03T13:00:00Z", usage=OUT),
        ], meta={"agentType": "lander", "description": "x"})
        self.log = os.path.join(self.tmp.name, "pull.log")
        with open(self.log, "w", encoding="utf-8") as f:
            f.write(PULL_LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def turns(self):
        paths = [("account %d" % (i + 1), p) for i, r in enumerate((self.root, self.root2))
                 for p in sorted(U.transcripts(r))]
        index = AC.PullIndex(AC.parse_pull_log(PULL_LOG))
        return {t.mid: t for t in AC.attribute(paths, index, "2026-09-30")}

    def placed(self, mid):
        t = self.turns()[mid]
        return t.key, t.reason, t.role, t.sure

    def test_parse_pull_log(self):
        self.assertEqual(sorted(AC.parse_pull_log(PULL_LOG)), sorted([
            AC.Pull("EX-A", "kit/alpha", "2026-10-01T12:00:00", "2026-10-02T12:00:00"),
            AC.Pull("EX-A", "kit/alpha-again", "2026-10-03T12:00:00", "2026-10-04T12:00:00"),
            AC.Pull("EX-B", "kit/beta", "2026-10-01T12:00:00", None),
            AC.Pull("EX-C", "kit/bundle", "2026-10-01T12:00:00", None),
            AC.Pull("EX-D", "kit/bundle", "2026-10-01T12:00:00", None),
            AC.Pull("EX-X", "launcher/beta", "2026-10-01T13:00:00", None)]))

    def test_a_subagent_in_a_worktree_goes_to_the_record_then_pulled_onto_it(self):
        self.assertEqual(self.placed("w1"), ("EX-A", None, "worker", "medium"))
        self.assertEqual(self.placed("w3")[0], "EX-A")

    def test_a_coordinator_session_is_placed_turn_by_turn(self):
        self.assertEqual(self.placed("c1"), ("EX-A", None, "coordinator", "medium"))
        self.assertEqual(self.placed("c3")[0], "EX-B")
        self.assertEqual(self.placed("c4"), (None, "coordination", "coordinator", None))
        self.assertEqual(self.placed("c5")[:2], (None, "several"))
        self.assertEqual(self.placed("c6")[:2], (None, "window"))          # kit/alpha released
        self.assertNotIn("c0", self.turns())                                # before since

    def test_a_main_session_turn_written_as_several_records_is_placed_by_all_of_them(self):
        F.write(os.path.join(self.root, "s3.jsonl"), [
            F.turn("m7", "2026-10-01T13:30:00Z", usage=OUT, content=[{"type": "thinking", "thinking": "x"}]),
            F.turn("m7", "2026-10-01T13:30:00Z", usage=OUT, content=[agent("t71", "build kit/alpha")]),
            F.turn("m7", "2026-10-01T13:30:01Z", usage=OUT, content=[F.shell("t72", "git log kit/alpha")]),
        ])
        self.assertEqual(self.placed("m7"), ("EX-A", None, "main", "medium"))

    def test_an_empty_pull_log_is_warned_about(self):
        empty = os.path.join(self.tmp.name, "empty.log")
        open(empty, "w").close()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(AC.main([self.root, "2026-09-30", "--records", "--pull-log", empty]), 0)
        self.assertIn("warning: the pull log is empty", err.getvalue())
        self.assertNotIn("warning", self.report())

    def test_a_slug_two_pulled_branches_end_in_is_a_clash(self):
        self.assertEqual(self.placed("c2")[:2], (None, "clash"))

    def test_a_bundle_branch_is_one_row(self):
        self.assertEqual(self.placed("b1"), ("EX-C + EX-D", None, "worker", "high"))

    def test_a_subagent_is_placed_by_a_clear_majority_only(self):
        self.assertEqual(self.placed("r1")[:2], (None, "several"))
        self.assertEqual(self.placed("n1")[:2], (None, "none"))
        self.assertEqual(self.placed("l1"), ("EX-B", None, "lander", "medium"))

    def test_a_message_id_counts_once_across_transcripts_and_accounts(self):
        turns = self.turns()
        self.assertEqual(turns["w1"].account, "account 1")
        self.assertEqual(sorted(turns), sorted(["c1", "c2", "c3", "c4", "c5", "c6", "w1", "w2", "w3",
                                                "b1", "r1", "n1", "l1"]))

    def test_two_turns_with_no_message_id_are_both_attributed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = F.write(os.path.join(tmp.name, "s.jsonl"), [
            F.turn(None, "2026-10-01T13:00:00Z", usage=OUT),
            F.turn(None, "2026-10-01T13:05:00Z", usage=OUT),
        ])
        index = AC.PullIndex(AC.parse_pull_log(PULL_LOG))
        self.assertEqual(len(AC.attribute([("account 1", path)], index, "2026-09-30")), 2)

    def test_turns_with_no_message_id_keep_their_own_tool_text(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        a = F.turn(None, "2026-10-01T13:00:00Z", usage=OUT, content=[F.shell("ta", "git log kit/alpha")])
        b = F.turn(None, "2026-10-01T13:05:00Z", usage=OUT, content=[F.shell("tb", "git log kit/beta")])
        c = F.turn(None, "2026-10-01T13:10:00Z", usage=OUT, content=[F.shell("tc", "git log kit/alpha")])
        c["uuid"], b["uuid"] = "u-c", "u-b"       # some records carry a uuid, some are told by position
        path = F.write(os.path.join(tmp.name, "s.jsonl"), [a, b, c])
        index = AC.PullIndex(AC.parse_pull_log(PULL_LOG))
        turns = AC.attribute([("account 1", path)], index, "2026-09-30")
        self.assertEqual([t.reason for t in turns], [None, None, None])
        self.assertEqual([t.key for t in turns], [turns[0].key, turns[1].key, turns[0].key])
        self.assertNotEqual(turns[0].key, turns[1].key)

    def test_tokens_and_list_price_per_turn(self):
        turns = self.turns()
        self.assertAlmostEqual(turns["w1"].usd, 1000 * 25 / 1e6)
        self.assertIsNone(turns["w2"].usd)
        self.assertEqual((turns["w3"].tokens["write_5m"], turns["w3"].tokens["write_1h"]), (1000, 2000))
        self.assertAlmostEqual(turns["w3"].usd, (1000 * 1.25 + 2000 * 2.0) * 5 / 1e6)

    def report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(AC.main([self.root, "2026-09-30", "--records", "--pull-log", self.log,
                                      "--root", self.root2]), 0)
        return out.getvalue()

    def row(self, text, title, name):
        """The columns after `name` in the table `title`."""
        block = text.split("\n" + title + "\n", 1)[1].split("\n\n", 1)[0]
        return next(line[len(name):].split() for line in block.splitlines()
                    if line.startswith(name + " "))

    def test_report_by_record_role_and_reason(self):
        text = self.report()
        # calls, input, output, cache rd, wr 5m, wr 1h, USD, share, pulls, high: c1 w1 w2 w3.
        a = self.row(text, "by record", "EX-A")
        self.assertEqual(a[:7], ["4", "0", "3.0k", "0", "1.0k", "2.0k", "0.08"])
        self.assertEqual(a[-2:], ["2", "0%"])
        self.assertEqual(self.row(text, "by record", "EX-C + EX-D")[-1], "100%")
        self.assertEqual(self.row(text, "by record x role", "EX-A | worker")[0], "3")
        self.assertEqual(self.row(text, "by record x role", "EX-A | coordinator")[0], "1")
        self.assertEqual(self.row(text, "by role", "coordinator")[0], "6")
        self.assertEqual(self.row(text, "unattributed", AC.REASONS["coordination"])[0], "1")
        self.assertEqual(self.row(text, "unattributed", AC.REASONS["several"])[0], "2")
        self.assertIn(AC.REASONS["clash"], text)
        self.assertIn("no list price (tokens counted, USD and share for priced models only): "
                      "mystery-model 1 calls", text)
        self.assertIn("account 1: 12 calls, account 2: 1 calls", text)

    def test_a_transcript_last_written_before_since_is_not_read(self):
        old = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp()
        for p in U.transcripts(self.root2):
            os.utime(p, (old, old))
        self.assertIn("account 1: 12 calls\n", self.report())
        self.assertFalse(AC._touched_since(p, "2026-09-02"))
        self.assertTrue(AC._touched_since(p, "2026-09-01"))

    def test_report_names_no_path_or_description(self):
        text = self.report()
        for leak in (self.tmp.name, "Secret", "worktrees", "Z:", "acct1", "acct2"):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main()
