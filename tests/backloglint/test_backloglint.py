import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from kitlib import backloglint as bl
from kitlib.git import cmd as gitcmd

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(os.path.dirname(HERE))
sys.path.append(os.path.join(KIT, "tools"))   # last: tools/gate.py must not shadow tests/gate
import backlog_lint                                                 # noqa: E402

EXAMPLE = os.path.join(KIT, "docs", "backlog-lint.example.json")
INCIDENT = ["EX-SIM-BATCH-STATE", "EX-SIM-BUFF-DURATION", "EX-TOOLS-LANDING-INFLIGHT",
            "EX-LINK-CYCLECOST", "EX-LINK-PRESENCE-LIVECHECK"]


def fixtures():
    texts = {}
    for p in glob.glob(os.path.join(HERE, "fixtures", "*.md")):
        with open(p, encoding="utf-8") as f:
            texts[os.path.basename(p)[:-3]] = f.read()
    return bl.TextReader(texts).records()


def rec(rid, **kv):
    head = {"id": rid, "area": "x", "priority": "3", "effort": "s", "state": "open", "blocked_on": "-",
            "scope": "src/a.py", "summary": "something"}
    head.update(kv)
    lines = []
    for k, v in head.items():
        for one in (v if isinstance(v, list) else [v]):
            lines.append("%s: %s" % (k, one))
    return bl.parse_record(rid, "\n".join(lines) + "\n\nbody\n")


def cfg(rules=(), **kw):
    return bl.parse_repo("r", dict({"root": "R", "rules": list(rules)}, **kw))


GAMMA = {"name": "gamma", "session": "gamma", "ids": ["EX-SIM-*"], "paths": ["research/gamma/"]}


class IncidentTest(unittest.TestCase):
    """The incident behind this tool: a coordinator's capped next was filled with five items it could not take."""

    def setUp(self):
        self.cfg = bl.load_config(EXAMPLE)["alpha"]
        self.records = fixtures()

    def test_fixtures_are_the_five(self):
        self.assertEqual(sorted(r.id for r in self.records), sorted(INCIDENT))

    def test_every_incident_item_has_its_fix(self):
        by = {f.id: f for f in bl.lint(self.records, self.cfg)}
        self.assertEqual(sorted(by), sorted(INCIDENT))
        self.assertEqual(by["EX-SIM-BATCH-STATE"].kind, "session")
        self.assertEqual(by["EX-SIM-BATCH-STATE"].fix,
                         "python tools/backlog.py -C <worktree> set EX-SIM-BATCH-STATE --session gamma")
        self.assertIn("--session gamma", by["EX-SIM-BUFF-DURATION"].fix)
        self.assertIn("--session pm", by["EX-TOOLS-LANDING-INFLIGHT"].fix)
        self.assertIn("scope under tools/new_worktree.py", by["EX-TOOLS-LANDING-INFLIGHT"].why)
        self.assertEqual(by["EX-LINK-CYCLECOST"].kind, "hold")
        self.assertIn("--on live-client", by["EX-LINK-CYCLECOST"].fix)
        self.assertEqual(by["EX-LINK-PRESENCE-LIVECHECK"].kind, "hold")
        self.assertIn("--on owner", by["EX-LINK-PRESENCE-LIVECHECK"].fix)
        self.assertIn("owner's go", by["EX-LINK-PRESENCE-LIVECHECK"].why)

    def test_offer_to_the_untagged_session_lists_all_five(self):
        offered, found = bl.offer_findings(self.records, self.cfg, "none", cap=5)
        self.assertEqual(sorted(r.id for r in offered), sorted(INCIDENT))
        self.assertEqual(sorted(f.id for f in found), sorted(INCIDENT))
        self.assertTrue(all(f.kind == "offer" for f in found))

    def test_offer_to_gamma_is_empty_until_tagged(self):
        self.assertEqual(bl.offer_findings(self.records, self.cfg, "gamma")[0], [])

    def test_cyclecost_needs_a_rule_its_text_names_no_live_client(self):
        no_rule = cfg([GAMMA])
        self.assertNotIn("EX-LINK-CYCLECOST", [f.id for f in bl.lint(self.records, no_rule)])

    def test_cli_exit_and_lines(self):
        lines = []
        rc = backlog_lint.run({"alpha": self.cfg}, ["alpha"], offer="none", cap=5,
                              reader=lambda c: _Fixed(self.records), out=lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("would offer (approx.)", lines[0])
        self.assertEqual(lines[1], "alpha: 5 records, 5 findings; 5 offer")
        self.assertEqual(sum(1 for ln in lines if "EX-LINK-CYCLECOST" in ln and "->" in ln), 1)


class _Fixed:
    def __init__(self, records):
        self._r = records

    def records(self):
        return self._r


class SessionTest(unittest.TestCase):
    def test_tagged_right_is_clean(self):
        self.assertEqual(bl.lint([rec("EX-SIM-A", session="gamma")], cfg([GAMMA])), [])

    def test_other_session_is_named(self):
        f, = bl.lint([rec("EX-SIM-A", session="alpha")], cfg([GAMMA]))
        self.assertIn("session alpha", f.why)
        self.assertIn("--session gamma", f.fix)

    def test_path_prefix_needs_every_scope_member(self):
        f, = bl.lint([rec("X-1", scope=["research/gamma/a.py", "research/gamma/eval/**"])], cfg([GAMMA]))
        self.assertIn("scope under research/gamma/", f.why)
        self.assertEqual(bl.lint([rec("X-2", scope=["src/a.py", "research/gamma/eval.py"])], cfg([GAMMA])), [])

    def test_area_selector(self):
        f, = bl.lint([rec("X-1", area="gamma")], cfg([{"session": "gamma", "areas": ["gamma"]}]))
        self.assertIn("area gamma", f.why)

    def test_first_rule_wins(self):
        rules = [{"session": "pm", "ids": ["EX-SIM-X*"]}, GAMMA]
        f, = bl.lint([rec("EX-SIM-XY")], cfg(rules))
        self.assertIn("--session pm", f.fix)

    def test_unmapped_and_finished_records_are_skipped(self):
        recs = [rec("EX-OTHER-1"), rec("EX-SIM-P", state="pulled"), rec("EX-SIM-C", closed="2026-10-01")]
        self.assertEqual(bl.lint(recs, cfg([GAMMA])), [])

    def test_example_pm_rule_catches_a_new_id_by_path(self):
        c = bl.load_config(EXAMPLE)["alpha"]
        f, = bl.lint([rec("EX-TOOLS-SYNC-NEW", scope="tools/sync_base.py")], c)
        self.assertIn("scope under tools/sync_base.py", f.why)
        self.assertIn("set EX-TOOLS-SYNC-NEW --session pm", f.fix)

    def test_example_pm_rule_catches_by_text(self):
        c = bl.load_config(EXAMPLE)["alpha"]
        f, = bl.lint([rec("EX-GATE-X", scope="tools/gate_hook.py", summary="a kit tool fix; the PM takes it")], c)
        self.assertIn('text "', f.why)
        self.assertIn("--session pm", f.fix)

    def test_example_gamma_rule_catches_a_non_pvm_id_by_path(self):
        c = bl.load_config(EXAMPLE)["alpha"]
        f, = bl.lint([rec("EX-NEW-THING", scope=["artifacts/models/env/nh.py", "research/gamma/nh/**"])], c)
        self.assertIn("scope under artifacts/models/, research/gamma/", f.why)
        self.assertIn("--session gamma", f.fix)

    def test_blocked_record_still_gets_its_session(self):
        f, = bl.lint([rec("EX-SIM-B", state="blocked", blocked_on="owner")], cfg([GAMMA]))
        self.assertEqual(f.kind, "session")


class HoldTest(unittest.TestCase):
    def test_keywords(self):
        for text, member in (("needs the live client", "live-client"), ("a live run on a throwaway", "live-client"),
                             ("the owner's go first", "owner"), ("owner runs it", "owner"),
                             ("owner-run check", "owner"), ("next owner-present session", "owner")):
            f, = bl.lint([rec("A-1", summary=text)], cfg())
            self.assertIn("--on %s" % member, f.fix, text)

    def test_body_is_not_read(self):
        r = bl.parse_record("A-1", "id: A-1\nstate: open\nblocked_on: -\nsummary: plain\n\nNeeds the live client.\nRisk: High\n")
        self.assertEqual(bl.lint([r], cfg()), [])
        self.assertIsNone(r.get("Risk"))

    def test_requires_field_is_read(self):
        f, = bl.lint([rec("A-1", requires="a live client")], cfg())
        self.assertEqual(f.kind, "hold")

    def test_held_or_pulled_is_clean(self):
        recs = [rec("A-1", summary="owner's go", blocked_on="owner", state="blocked"),
                rec("A-2", summary="live client", blocked_on="item:A-1"),
                rec("A-3", summary="live client", state="pulled")]
        self.assertEqual(bl.lint(recs, cfg()), [])

    def test_custom_keywords_replace_defaults(self):
        c = cfg(keywords={"owner": [r"\bprocmon\b"]})
        self.assertEqual(bl.lint([rec("A-1", summary="live client")], c), [])
        f, = bl.lint([rec("A-2", summary="a Procmon trace")], c)
        self.assertIn("--on owner", f.fix)

    def test_hold_rule(self):
        f, = bl.lint([rec("EX-LINK-H")], cfg([{"hold": "live-client", "ids": ["EX-LINK-H"]}]))
        self.assertIn("--on live-client", f.fix)

    def test_worst_first(self):
        recs = [rec("A-UNPRICED", priority="-", summary="live client"), rec("A-P4", priority="4", summary="live client"),
                rec("A-P1", priority="1", summary="live client")]
        self.assertEqual([f.id for f in bl.lint(recs, cfg())], ["A-P1", "A-P4", "A-UNPRICED"])


class OfferTest(unittest.TestCase):
    def test_rank_order_cap_and_session(self):
        recs = [rec("B", priority="2", effort="m"), rec("A", priority="2", effort="s"), rec("C", priority="1"),
                rec("D", priority="1", session="gamma"), rec("E", priority="-"), rec("F", priority="1", blocked_on="owner"),
                rec("P", priority="5", pin="1")]
        self.assertEqual([r.id for r in bl.approx_offer(recs, None, cap=3)], ["P", "C", "A"])
        self.assertEqual([r.id for r in bl.approx_offer(recs, "gamma")], ["D"])

    def test_only_offered_items_are_reported(self):
        recs = [rec("EX-SIM-1", priority="1"), rec("OK", priority="2"), rec("EX-SIM-9", priority="5")]
        offered, found = bl.offer_findings(recs, cfg([GAMMA]), "none", cap=2)
        self.assertEqual([r.id for r in offered], ["EX-SIM-1", "OK"])
        self.assertEqual([f.id for f in found], ["EX-SIM-1"])
        self.assertTrue(found[0].why.startswith("session: "))

    def test_one_offer_line_per_record_with_every_reason(self):
        recs = [rec("EX-SIM-1", priority="1", summary="needs the live client")]
        _, found = bl.offer_findings(recs, cfg([GAMMA]), "none")
        f, = found
        self.assertTrue(f.why.startswith("session: ") and "; hold: " in f.why, f.why)
        self.assertIn("--session gamma", f.fix)
        self.assertIn("--on live-client", f.fix)

    def test_cli_drops_the_lint_line_an_offer_line_repeats(self):
        recs = [rec("EX-SIM-1", priority="1"), rec("EX-SIM-9", priority="5")]
        lines = []
        backlog_lint.run({"r": cfg([GAMMA])}, ["r"], offer="none", cap=1, reader=lambda c: _Fixed(recs),
                         out=lines.append)
        self.assertEqual(lines[1], "r: 2 records, 2 findings; 1 offer; 1 session")
        self.assertEqual([ln.split()[1] for ln in lines[2:]], ["EX-SIM-1", "EX-SIM-9"])


NEXT_PRINTOUT = """BACKLOG d84be6ce8 | 367 records | 255 pullable | 104 held
held 104: 6 owner, 44 item

  rank 59  priority 3  EX-LINK-PRESENCE-LIVECHECK one worker run (owner's go) confirms ...
      scope: worker/link/src/main/java/org/example/link/PresenceConfig.java
  rank 67  priority 3  EX-SIM-BATCH-STATE SimBatch keeps process-wide state
      scope: artifacts/models/env/batch.py, artifacts/models/env/batch_shim.c
  rank 74  priority 3  EX-SIM-BUFF-DURATION Sim buff lasts 300 ticks
  rank 80  priority 3  EX-TOOLS-LANDING-INFLIGHT new_worktree/sync_base cut from pushed origin/master
  rank 83  priority 3  EX-LINK-CYCLECOST      a marshalled member costs an engine cycle
  rank 96  priority 3  EX-NOT-IN-FIXTURES    skipped

ON LIVE BRANCHES (12) - not offered, not counted against the cap:
  EX-API-TABLES closed on api/errand-tables, waiting to land, claimed 0 days ago
PASSED OVER (1) - rank is a printed column and is never rewritten:
  rank 12  EX-SIM-OTHER          scope x collides with y already taken
"""


class NextOutputTest(unittest.TestCase):
    """A `next --cap 8` printout (head of it): the incident's five lead the offer."""

    def test_parse(self):
        self.assertEqual(bl.parse_next_output(NEXT_PRINTOUT), [
            "EX-LINK-PRESENCE-LIVECHECK", "EX-SIM-BATCH-STATE", "EX-SIM-BUFF-DURATION",
            "EX-TOOLS-LANDING-INFLIGHT", "EX-LINK-CYCLECOST", "EX-NOT-IN-FIXTURES"])

    def test_real_offer_flags_the_five(self):
        c = bl.load_config(EXAMPLE)["alpha"]
        offered, found = bl.offer_findings(fixtures(), c, "none", offered_ids=bl.parse_next_output(NEXT_PRINTOUT))
        self.assertEqual([r.id for r in offered], bl.parse_next_output(NEXT_PRINTOUT)[:5])
        self.assertEqual(sorted(f.id for f in found), sorted(INCIDENT))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "next.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(NEXT_PRINTOUT)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(backlog_lint.main(["--config", EXAMPLE, "--next-output", p]), 2)   # needs --offer

    def test_not_a_printout_is_an_error_nothing_is_empty(self):
        with self.assertRaises(bl.NextOutputError):
            bl.parse_next_output("usage: backlog.py ...\n")
        with self.assertRaises(bl.NextOutputError):
            bl.parse_next_output("")
        self.assertEqual(bl.parse_next_output("BACKLOG x\n  NOTHING CAN BE PULLED, and this is an answer\n"), [])

    def test_decode_utf16_and_bom(self):
        for data in (NEXT_PRINTOUT.encode("utf-16"), NEXT_PRINTOUT.encode("utf-8-sig"), NEXT_PRINTOUT.encode()):
            self.assertEqual(bl.parse_next_output(bl.decode_next_output(data))[0], "EX-LINK-PRESENCE-LIVECHECK")


def git_store(root, files):
    def git(*a):
        gitcmd.subprocess_run(["git", "-C", root, *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    os.makedirs(os.path.join(root, "docs", "backlog"))
    for name, text in files.items():
        with open(os.path.join(root, "docs", "backlog", name + ".md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    git("add", "-A")
    git("commit", "-q", "-m", "a")


class MainOfferTest(unittest.TestCase):
    """main() end to end: a config file, a git store, --offer with a saved next printout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.root = os.path.join(d, "repo")
        os.makedirs(self.root)
        texts = {}
        for p in glob.glob(os.path.join(HERE, "fixtures", "*.md")):
            with open(p, encoding="utf-8") as f:
                texts[os.path.basename(p)[:-3]] = f.read()
        git_store(self.root, texts)
        with open(os.path.join(self.root, ".kit.json"), "w", encoding="utf-8") as f:
            f.write('{"backlog": {"store": "docs/backlog"}}')   # as a repository's config names its store
        with open(EXAMPLE, encoding="utf-8") as f:
            conf = json.load(f)
        conf["backlog_lint"]["repos"]["alpha"]["root"] = self.root
        self.conf = os.path.join(d, "config.json")
        with open(self.conf, "w", encoding="utf-8") as f:
            json.dump(conf, f)
        self.next = os.path.join(d, "next.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = backlog_lint.main(["--config", self.conf, "--repo", "alpha", *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_offer_with_next_output_utf16(self):
        with open(self.next, "wb") as f:
            f.write(NEXT_PRINTOUT.encode("utf-16"))            # what PowerShell 5.1 `>` writes
        rc, out, err = self.main("--offer", "none", "--next-output", self.next)
        self.assertEqual((rc, err), (1, ""))
        lines = out.splitlines()
        self.assertIn("offers (from next output)", lines[0])
        self.assertEqual(lines[1], "alpha: 5 records, 5 findings; 5 offer")
        self.assertEqual(sorted(ln.split()[1] for ln in lines[2:]), sorted(INCIDENT))
        self.assertTrue(all(ln.split()[0] == "offer" for ln in lines[2:]))

    def test_unreadable_next_output_is_two(self):
        rc, out, err = self.main("--offer", "none", "--next-output", os.path.join(self.tmp.name, "missing.txt"))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("cannot read --next-output", err)

    def test_garbage_next_output_is_two(self):
        with open(self.next, "w", encoding="utf-8") as f:
            f.write("Traceback (most recent call last):\n")
        rc, out, err = self.main("--offer", "none", "--next-output", self.next)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("not a `backlog.py next` printout", err)


class ConfigTest(unittest.TestCase):
    def bad(self, entry):
        with self.assertRaises(bl.LintConfigError):
            bl.parse_repo("r", entry)

    def test_rejects(self):
        self.bad([])
        self.bad({"root": "R", "extra": 1})
        self.bad({"rules": [{"ids": ["A*"]}]})                          # neither session nor hold
        self.bad({"rules": [{"session": "a", "hold": "owner", "ids": ["A*"]}]})
        self.bad({"rules": [{"session": "Bad Name", "ids": ["A*"]}]})
        self.bad({"rules": [{"hold": "someday", "ids": ["A*"]}]})
        self.bad({"rules": [{"session": "a"}]})                         # matches nothing
        self.bad({"rules": [{"session": "a", "ids": "A*"}]})
        self.bad({"keywords": {"owner": ["("]}})
        self.bad({"keywords": {"other": ["x"]}})
        self.bad({"rules": {"session": "a", "ids": ["A*"]}})            # rules not a list
        self.bad({"rules": 5})
        self.bad({"rules": [{"session": 5, "ids": ["A*"]}]})
        self.bad({"rules": [{"hold": ["owner"], "ids": ["A*"]}]})
        self.bad({"rules": [{"session": "a", "text": ["("]}]})
        self.bad({"backlog_cmd": 1})

    def test_type_errors_exit_two_not_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            for repo in ({"rules": 5}, {"rules": [{"session": 5, "ids": ["A*"]}]}):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"backlog_lint": {"repos": {"r": repo}}}, f)
                with redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(backlog_lint.main(["--config", p]), 2)
                self.assertIn("config: ", err.getvalue())

    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            self.assertIsNone(bl.load_config(p))
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"pm_start": {}}, f)
            self.assertIsNone(bl.load_config(p))
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"backlog_lint": {"repos": []}}, f)
            with self.assertRaises(bl.LintConfigError):
                bl.load_config(p)

    def test_example_parses(self):
        self.assertEqual(list(bl.load_config(EXAMPLE)), ["alpha"])


class GitReaderTest(unittest.TestCase):
    def test_reads_main_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*a):
                gitcmd.subprocess_run(["git", "-C", root, *a], check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.invalid")
            git("config", "user.name", "t")
            os.makedirs(os.path.join(root, "docs", "backlog"))
            p = os.path.join(root, "docs", "backlog", "EX-SIM-A.md")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write("id: EX-SIM-A\nstate: open\nblocked_on: -\npriority: 2\nsummary: s\n")
            git("add", "-A")
            git("commit", "-q", "-m", "a")
            with open(p, "a", encoding="utf-8") as f:
                f.write("session: gamma\n")                            # uncommitted: not read
            with open(os.path.join(root, ".kit.json"), "w", encoding="utf-8") as f:
                f.write('{"backlog": {"store": "main"}}')               # the folder store
            recs = bl.GitReader(root).records()
            self.assertEqual([r.id for r in recs], ["EX-SIM-A"])
            self.assertIsNone(recs[0].session)


class StoreResolutionTest(unittest.TestCase):
    """With no `store` key the lint reads the store the repository's `.kit.json` names, resolved
    by kitlib.backlog (kitlib.hubbacklog.store_at), not a default of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        git_store(self.root, {"EX-ON-MAIN": "id: EX-ON-MAIN\nstate: open\nblocked_on: -\nsummary: not the store\n"})

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, texts):
        from kitlib.backlog import record, store
        backend = store.RefBackend(self.root, "origin", store.STORE_REF, store.STORE_REF)
        backend.publish(backend.load(record.Rules()), texts, "seed\n")

    def test_no_store_key_means_no_lint_default(self):
        self.assertIsNone(cfg().store)

    def test_the_default_is_the_ref_store(self):
        self.publish({"EX-SIM-A": "id: EX-SIM-A\nstate: open\nblocked_on: -\npriority: 2\nsummary: s\n"})
        self.assertEqual([r.id for r in bl.GitReader(self.root).records()], ["EX-SIM-A"])
        lines = []
        rc = backlog_lint.run({"r": cfg(root=self.root)}, ["r"], out=lines.append)
        self.assertEqual((rc, lines[0]), (0, "r: 1 records, 0 findings"))

    def test_no_store_yet_is_a_read_error_not_a_clean_run(self):
        """No published store (nothing at the rev store_at names, read locally with no fetch) is
        exit 2, never "0 records, 0 findings"."""
        with self.assertRaises(RuntimeError) as cm:
            bl.GitReader(self.root)
        self.assertTrue(str(cm.exception).startswith("backlog store: "), cm.exception)
        lines = []
        self.assertEqual(backlog_lint.run({"r": cfg(root=self.root)}, ["r"], out=lines.append), 2)
        self.assertEqual(lines, ["r: could not read records: %s" % cm.exception])

    def test_an_explicit_store_folder_is_still_read_on_main(self):
        self.publish({"EX-SIM-A": "id: EX-SIM-A\nstate: open\nblocked_on: -\nsummary: s\n"})
        self.assertEqual([r.id for r in bl.GitReader(self.root, "docs/backlog").records()], ["EX-ON-MAIN"])

    def test_an_invalid_kit_json_is_a_read_error(self):
        with open(os.path.join(self.root, ".kit.json"), "w", encoding="utf-8") as f:
            f.write('{"backlog": {"store": 5}}')
        lines = []
        self.assertEqual(backlog_lint.run({"r": cfg(root=self.root)}, ["r"], out=lines.append), 2)
        self.assertTrue(lines[0].startswith("r: could not read records: backlog store: "), lines)


class CliTest(unittest.TestCase):
    def test_clean_is_zero(self):
        lines = []
        rc = backlog_lint.run({"r": cfg([GAMMA])}, ["r"], reader=lambda c: _Fixed([rec("OK")]), out=lines.append)
        self.assertEqual((rc, lines), (0, ["r: 1 records, 0 findings"]))

    def test_read_error_is_two(self):
        for exc in (RuntimeError("no git"), subprocess.TimeoutExpired(["git"], 30),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), OSError("gone")):
            def boom(c, exc=exc):
                raise exc
            lines = []
            self.assertEqual(backlog_lint.run({"r": cfg()}, ["r"], reader=boom, out=lines.append), 2, exc)
            self.assertIn("could not read records", lines[0])

    def test_main_config_errors_are_two(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"backlog_lint": {"repos": {"r": {"rules": [{"session": "a"}]}}}}, f)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(backlog_lint.main(["--config", p]), 2)
                self.assertEqual(backlog_lint.main(["--config", os.path.join(d, "none.json")]), 2)
                self.assertEqual(backlog_lint.main(["--config", EXAMPLE, "--repo", "nope"]), 2)
                self.assertEqual(backlog_lint.main(["--config", EXAMPLE, "--cap", "0"]), 2)


if __name__ == "__main__":
    unittest.main()
