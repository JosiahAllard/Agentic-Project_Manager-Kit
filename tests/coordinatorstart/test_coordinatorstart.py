import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import coordinatorstart
from kitlib.config import ConfigError
from kitlib.lockmcp import PLUGIN_NAME
from kitlib.lock import api
from kitlib.records import landing
from tests.home import machine_config
from tests.lock.helpers import OWNER, StoreCase
from tests.realgit import real_git

# the coordinator rules hook as the plugin's hooks.json runs it (boot/hook.py, its coordinator part)
HOOK = [os.path.join(os.path.dirname(__file__), "..", "..", "boot", "hook.py"), "claude", "SessionStart", "coordinator"]
COND = {"PM_KIT_ROLE": "coordinator"}
P = PLUGIN_NAME + ":"          # the kit plugin's agent prefix (kitlib.agentname.PREFIX)


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class RenderTest(unittest.TestCase):
    def test_role_unset_prints_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["a"]}}))
            self.assertIsNone(coordinatorstart.render(env={}, path=p))
            self.assertIsNone(coordinatorstart.render(env={"PM_KIT_ROLE": "pm"}, path=p))

    def test_a_plugin_prefixed_mark_is_the_coordinator_and_no_other_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["one"]}}))
            for role in (P + "coordinator", P.upper() + "coordinator"):
                self.assertEqual(coordinatorstart.render(env={"PM_KIT_ROLE": role}, path=p, live=False),
                                 "Coordinator session start:\n- one", role)
            for role in ("some-plugin:coordinator", P + "pm", P):
                self.assertIsNone(coordinatorstart.render(env={"PM_KIT_ROLE": role}, path=p), role)

    def test_role_set_lists_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["one", "two"]}}))
            out = coordinatorstart.render(env=COND, path=p, live=False)
        self.assertEqual(out, "Coordinator session start:\n- one\n- two")

    def test_pricing_line_prices_at_start_and_leaves_follow_ups_unpriced(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"pricing_pass": True}}))
            out = coordinatorstart.render(env=COND, path=p, live=False)
        self.assertEqual(len(out.splitlines()), 2, out)   # the heading and one line
        line = out.splitlines()[1]
        self.assertTrue(line.startswith("- Pricing pass and backlog audit before your first dispatch, "
                                        "follow-up records excepted"), line)
        for want in ("python <kit>/tools/backlog.py -C <repo> list --needs-priority",
                     "A follow-up record (filed from a finding on a branch, a review or a worker's report; "
                     "a new one says so in its summary) stays unpriced until you or the PM promote it",
                     "Check every other unpriced open record (an owner story, a record from the PM's brief) "
                     "for duplicates, size and fit",
                     "--priority-by agent",
                     "Promote a follow-up at any time it enters a work set", "price before pull",
                     "When `next --all` lists fewer startable records than the set needs",
                     "price only those going into this set; never a sweep",
                     "`next` never offers an unpriced record",
                     "under `unprioritised+open`, not under `held` (`held` = blocked)",
                     "goes to the PM", "never ask the owner directly"):
            self.assertIn(want, line)
        # Withdrawn wording: no pass at all, unpriced called held, the capped-offer and duplicate triggers.
        for gone in ("No start-up pricing pass", "is held", "stay held", "held is not waiting",
                     "`next` offers fewer items", "duplicates or replaces a priced one", "each unpriced"):
            self.assertNotIn(gone, line)
        # "held" only where it is true: the banner's group, twice, and "owner-held".
        self.assertEqual(line.count("held"), 3, line)
        self.assertEqual(line.count("`held`"), 2, line)
        self.assertEqual(line.count("owner-held"), 1, line)

    def test_pricing_line_is_off_unless_the_config_switches_it_on(self):
        with tempfile.TemporaryDirectory() as d:
            for cfg in ({"standing": ["x"]}, {"pricing_pass": False}, {"pricing_pass": "yes"}):
                p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": cfg}))
                out = coordinatorstart.render(env=COND, path=p, live=False)
                self.assertNotIn(coordinatorstart.PRICING_LINE, out)
                self.assertNotIn("pricing pass", out.lower())

    def test_missing_key_or_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), "{}")
            self.assertIn("skipped", coordinatorstart.render(env=COND, path=p, live=False))
            self.assertIn("skipped", coordinatorstart.render(env=COND, path=os.path.join(d, "none.json"), live=False))

    def test_bad_json_is_one_could_not_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), "{not json")
            out = coordinatorstart.render(env=COND, path=p, live=False)
        self.assertIn("could not check config:", out)
        self.assertEqual(len(out.splitlines()), 1)


NOW = datetime.datetime(2026, 10, 6, 18, 30, tzinfo=datetime.timezone.utc)
TODAY = NOW.date()
ROOT = "D:/src"
ENTRY = {"repo": "alpha", "agent": True, "stem": "alpha-coordinator",
         "state": "STATE.md", "name": "alpha-coordinator-a2-n6"}


def reader(files):
    def read(p):
        if p not in files:
            raise FileNotFoundError(p)
        return files[p]
    return read


def proc(pid, ppid, name, cmd, exe="C:/tools/claude.exe", created="2026-10-06T10:00:00"):
    return {"ProcessId": pid, "ParentProcessId": ppid, "Name": name, "CommandLine": cmd,
            "ExecutablePath": exe, "Created": created}


class LoadConfigBomTest(unittest.TestCase):
    def test_reads_bom_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "wb") as f:
                f.write(b"\xef\xbb\xbf" + json.dumps({"coordinator_start": {"k": 1}}).encode("utf-8"))
            self.assertEqual(coordinatorstart.load_config(p), {"k": 1})


class FindEntryTest(unittest.TestCase):
    S = {"alpha": ENTRY, "gamma": dict(ENTRY, agent=False, stem="gamma-training"),
         "Beta": {"repo": "Beta", "agent": True, "stem": "beta-coordinator", "name": "g"}}

    def test_cwd_top_folder_and_agent_true(self):
        e, note = coordinatorstart.find_entry(self.S, ROOT + "/alpha/tools", root=ROOT)
        self.assertEqual((e["stem"], note), ("alpha-coordinator", None))
        self.assertEqual(coordinatorstart.find_entry(self.S, "d:/SRC/beta", root=ROOT)[0]["name"], "g")

    def test_no_repos_root_is_a_key_error(self):
        with self.assertRaises(KeyError):
            coordinatorstart.find_entry(self.S, ROOT + "/alpha")

    def test_not_under_root_or_no_entry(self):
        with self.assertRaises(ValueError):
            coordinatorstart.find_entry(self.S, "C:/x", root=ROOT)
        with self.assertRaises(LookupError):
            coordinatorstart.find_entry(self.S, ROOT + "/other", root=ROOT)

    def test_ambiguous_prefers_name_else_first_and_says_so(self):
        two = {"a": dict(ENTRY, name="n1", stem="s1"), "b": dict(ENTRY, name="n2", stem="s2")}
        self.assertEqual(coordinatorstart.find_entry(two, ROOT + "/alpha", "n2", ROOT)[0]["name"], "n2")
        e, note = coordinatorstart.find_entry(two, ROOT + "/alpha", None, ROOT)
        self.assertEqual(e["name"], "n1")
        self.assertIn("2 sessions entries", note)


class CapReviewTest(unittest.TestCase):
    CAPS = {"alpha": [{"cap": 6, "until": "2026-10-10"}, {"cap": 3}]}

    def test_cap_first_unexpired(self):
        self.assertEqual(coordinatorstart.cap({"cap": self.CAPS}, "alpha", TODAY),
                         ["agent cap 6 until 2026-10-10, then 3"])
        self.assertEqual(coordinatorstart.cap({"cap": self.CAPS}, "alpha", datetime.date(2026, 10, 11)),
                         ["agent cap 3"])

    def test_cap_missing(self):
        with self.assertRaises(KeyError):
            coordinatorstart.cap({}, "alpha", TODAY)
        with self.assertRaises(KeyError):
            coordinatorstart.cap({"cap": {}}, "alpha", TODAY)


LOCK = {"lane": "full", "branches": ["b1"], "pid": None, "phase": "gate", "started_utc": "T"}


class StatusCase(StoreCase):
    """A status payload from the current producer, api.snapshot() (what `machine_lock.py status
    --json` prints, docs/lock-api.md "Snapshot"), on a scratch store."""

    def status(self, *specs):
        for s in specs:
            self.assertTrue(api.hold_create(OWNER, s, self.store)["ok"])
        return api.snapshot(self.store)

    def free(self, snap, pool):
        d = snap["slots"][pool]
        return sum(1 for x in d if not x["holder"] and not x["hold"]), len(d)


def idle_status():
    """The producer's snapshot of an idle scratch store, for tests that are not about holds."""
    case = StatusCase()
    case.setUp()
    try:
        return case.status()
    finally:
        case.doCleanups()


class MachineTest(StatusCase):
    def run_fake(self, status):
        calls = []

        def run(args, timeout=10):
            calls.append(args)
            return json.dumps(status) if "machine_lock.py" in args[1] else ROOT + "/alpha/.git\n"
        return run, calls

    def test_summary_and_landing_lock(self):
        snap = self.status({"owner": "owner", "scope": "half", "for_s": 7200, "note": "gaming"},
                           {"owner": "owner", "scope": "half", "start": api.iso(
                               api.utc_now() + datetime.timedelta(hours=9)), "for_s": 3600})
        [soon, later] = snap["holds"]
        run, calls = self.run_fake(snap)
        read = reader({os.path.join(ROOT + "/alpha/.git", landing.LOCK_NAME): json.dumps(LOCK)})
        out = coordinatorstart.machine(ENTRY, run, read, api.utc_now(), "H", ROOT)
        self.assertEqual(out[0], "machine: owner hold %s-%s: gaming" % (soon["start_local"], soon["end_local"]))
        self.assertNotIn(later["start_local"] + "-", out[0])
        (gf, gn), (cf, cn) = self.free(snap, "gpu"), self.free(snap, "cpu")
        self.assertLess(cf, cn)             # the half hold covers cpu slots, so they are not free
        self.assertEqual(out[1], "machine: free gpu slots %d of %d; free cpu slots %d of %d" % (gf, gn, cf, cn))
        self.assertIn("HELD - branches b1, a token holder, phase gate", out[2])
        self.assertEqual(calls[0][-2:], ["status", "--json"])
        self.assertEqual(calls[0][1].replace("\\", "/"), "H/tools/machine_lock.py")

    def test_a_run_holding_a_slot_is_not_free(self):
        snap = self.status()
        snap["slots"]["cpu"][0]["holder"] = "r-1"
        run, _ = self.run_fake(snap)
        out = coordinatorstart.machine(ENTRY, run, reader({}), api.utc_now(), "H")
        n = len(snap["slots"]["cpu"])
        self.assertIn("free cpu slots %d of %d" % (n - 1, n), out[1])

    def test_an_unknown_schema_is_refused_not_read_as_an_idle_machine(self):
        run, _ = self.run_fake({"reservations": {}, "gpus": [], "cores": []})
        with self.assertRaises(ValueError):
            coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H")

    def test_lock_free_and_no_repos_root(self):
        run, _ = self.run_fake(self.status())
        out = coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H", ROOT)
        self.assertIn("no owner or agent hold within 4 h", out[0])
        self.assertEqual(out[2], "landing lock (alpha): free")
        self.assertEqual(len(coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H", None)), 2)

    def test_runner_failure_propagates(self):
        def run(args, timeout=10):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H")


class OthersTest(unittest.TestCase):
    def procs(self):
        rc = '"C:/tools/claude.exe" --remote-control "%s" -n x'
        return [proc(1, 0, "claude.exe", rc % "alpha-coordinator-a2-n5", created="2026-10-06T08:00"),
                proc(2, 0, "claude.exe", rc % "alpha-coordinator-a2-n6", created="2026-10-06T09:00"),
                proc(3, 2, "bash.exe", "bash", exe="C:/bash.exe"),
                proc(4, 3, "python.exe", "hook", exe="C:/py.exe"),
                proc(5, 0, "claude.exe", rc % "beta-coordinator-a2"),
                proc(6, 0, "claude.exe", "Claude.exe", exe="C:/Program Files/WindowsApps/Claude/Claude.exe")]

    def test_own_name_from_process_tree(self):
        self.assertEqual(coordinatorstart.own_name(self.procs(), {}, 4), "alpha-coordinator-a2-n6")
        self.assertEqual(coordinatorstart.own_name(self.procs(), {"PM_KIT_SESSION_NAME": "z"}, 4), "z")
        self.assertIsNone(coordinatorstart.own_name(self.procs(), {}, 99))

    def test_lists_predecessor(self):
        out = coordinatorstart.others(ENTRY, self.procs(), {}, 4)
        self.assertEqual(len(out), 1)
        self.assertIn("also running: alpha-coordinator-a2-n5; if one is your winding-down", out[0])
        self.assertNotIn("beta", out[0])

    def test_unknown_self_is_newest_and_alone_prints_nothing(self):
        self.assertIn("a2-n5", coordinatorstart.others(ENTRY, self.procs(), {}, 99)[0])
        self.assertEqual(coordinatorstart.others(ENTRY, self.procs()[1:], {}, 4), [])
        self.assertEqual(coordinatorstart.others(ENTRY, [], {}, 4), [])

    def test_no_stem_raises(self):
        with self.assertRaises(KeyError):
            coordinatorstart.others({"repo": "x"}, [], {}, 1)


class ContextTest(unittest.TestCase):
    PM = {"sessions_file": "S.json"}

    def build(self, cfg, **kw):
        files = {"S.json": json.dumps({"alpha": ENTRY})}
        status = idle_status()

        def run(args, timeout=10):
            return json.dumps(status) if "machine_lock.py" in args[1] else "G"
        empty = os.path.join(tempfile.gettempdir(), "coordinatorstart-test-no-notes")   # never holds a note
        base = dict(env={"CLAUDE_CONFIG_DIR": "C:/u/.claude-ex2", "AGENT_TEMP": empty, "TEMP": empty},
                    cwd=ROOT + "/alpha", now=NOW,
                    procs=[], run=run, read=reader(files), kit="H", pid=1, pm_cfg=self.PM, machine_cfg={})
        base.update(kw)
        return coordinatorstart.context(dict(dict(repos_root=ROOT), **cfg), **base)

    def test_standing_first_then_checks(self):
        cfg = {"standing": ["wind down"], "cap": {"alpha": [{"cap": 4}]}}
        text = self.build(cfg)
        # The cap check is the first live check: its line follows the standing line.
        self.assertEqual(text.split("\n")[:3], ["Coordinator session start:", "- wind down", "- agent cap 4"])
        gf, gn = StatusCase().free(idle_status(), "gpu")
        self.assertIn("- machine: free gpu slots %d of %d" % (gf, gn), text)
        self.assertNotIn("also running", text)
        self.assertNotIn("usage", text.lower())

    def test_the_landing_lock_line_reads_the_kits_lock_file_whatever_the_config_names(self):
        # a retired "landing_lock" naming a wrong file is ignored: kit-landing.lock held reads HELD
        common = ROOT + "/alpha/.git"
        status = idle_status()

        def run(args, timeout=10):
            return json.dumps(status) if "machine_lock.py" in args[1] else common
        held = {"S.json": json.dumps({"alpha": ENTRY}),
                os.path.join(common, landing.LOCK_NAME): json.dumps(LOCK)}
        for cfg in ({"landing_lock": {"alpha": "wrong.lock"}}, {}):
            with self.subTest(cfg=cfg):
                text = self.build(cfg, run=run, read=reader(held))
                self.assertIn("- landing lock (alpha): HELD - branches b1", text)
        free = self.build({"landing_lock": {"alpha": "wrong.lock"}}, run=run,
                          read=reader({"S.json": json.dumps({"alpha": ENTRY})}))
        self.assertIn("- landing lock (alpha): free", free)

    def test_the_machine_check_runs_the_named_kits_lock_tool(self):
        calls, status = [], idle_status()

        def run(args, timeout=10):
            calls.append(args)
            return json.dumps(status) if "machine_lock.py" in args[1] else "G"
        self.assertIn("- machine: free gpu slots", self.build({}, run=run))
        self.assertEqual([a[1].replace("\\", "/") for a in calls if "machine_lock.py" in a[1]],
                         ["H/tools/machine_lock.py"])

    def test_eng_filing_lines_follow_the_standing_lines(self):
        lines = self.build({"standing": ["wind down"], "eng_filing": True, "pricing_pass": True}).splitlines()
        self.assertEqual(lines[1], "- wind down")
        self.assertTrue(lines[2].startswith("- ENG rulings:"))
        self.assertEqual(lines[3], "- " + coordinatorstart.PRICING_LINE)
        self.assertEqual(lines[4], "- could not check cap: cap not configured")   # the first live check
        self.assertIn("ENG <repo> <item>: <question> | options: a) .. b) .. | rec: <x> | why: <one line>", lines[2])
        self.assertIn("consultant-<repo>", lines[2])
        for owner_only in ("owner ruling", "Realism", "Security", "Money and accounts", "Priority", "Go-live"):
            self.assertIn(owner_only, lines[2])

    def test_eng_filing_line_is_off_unless_the_config_switches_it_on(self):
        for cfg in ({"standing": ["x"]}, {"standing": ["x"], "eng_filing": False}, {"eng_filing": "yes"}):
            self.assertNotIn("ENG rulings", self.build(cfg))

    def test_missing_config_gives_one_line_per_check(self):
        text = self.build({})
        self.assertIn("could not check cap: cap not configured", text)
        self.assertNotIn("usage", text.lower())

    def test_no_sessions_file_and_no_cwd_match_isolated(self):
        text = self.build({}, pm_cfg={})
        self.assertIn("could not check cap: pm_start.sessions_file not configured", text)
        self.assertIn("could not check other coordinators:", text)
        self.assertIn("could not check cap: LookupError", self.build({}, cwd=ROOT + "/zzz"))
        self.assertIn("could not check cap: ValueError", self.build({}, cwd="C:/elsewhere"))

    def test_the_hub_key_line_names_the_sessions_key_of_this_coordinator(self):
        # the key differs from the repo and the name: only the sessions.json key can give it
        files = {"S.json": json.dumps({"_note": "x", "kitc": ENTRY,
                                       "plain": {"repo": "alpha", "stem": "alpha-plain"}})}
        lines = self.build({}, read=reader(files)).splitlines()
        self.assertEqual([l for l in lines if "hub key" in l],
                         ["- hub key: kitc (hub.py questions add --from kitc --record REPO:ID|"
                          "--class C; its pending list is in this start context; answers come "
                          "by push)"])
        self.assertNotIn("wait", "\n".join(lines))   # no session keeps a background hub wait

    def test_several_matching_entries_give_hub_key_unknown_never_the_first_key(self):
        # no own name (procs=[]), two agent entries for alpha: find_entry takes the first with a note
        other = dict(ENTRY, name="alpha-coordinator-b1", stem="alpha-other")
        files = {"S.json": json.dumps({"kitc": ENTRY, "kitd": other})}
        text = self.build({}, read=reader(files))
        self.assertIn("2 sessions entries match repo alpha; took the first", text)
        self.assertEqual([l for l in text.splitlines() if "hub key" in l],
                         ["- hub key: unknown (several sessions entries match this repo); ask the PM for it"])

    def test_no_entry_gives_hub_key_unknown_and_every_other_check_still_runs(self):
        for kw in ({"cwd": ROOT + "/zzz"}, {"cwd": "C:/elsewhere"}, {"pm_cfg": {}}):
            text = self.build({}, **kw)
            self.assertIn("- hub key: unknown (", text, kw)
            self.assertNotIn("hub key: alpha", text)
            self.assertIn("could not check cap:", text)
        self.assertEqual(coordinatorstart.hub_key({"a": ENTRY}, ENTRY)[0].split()[:3], ["hub", "key:", "a"])
        with self.assertRaises(LookupError):
            coordinatorstart.hub_key({"a": dict(ENTRY)}, ENTRY)     # equal, but not the entry found

    def test_default_read_takes_a_bom_sessions_file(self):
        # regression-only: the default _read is utf-8-sig, so a PowerShell-saved sessions file loads
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sessions.json")
            with open(p, "wb") as f:
                f.write(b"\xef\xbb\xbf" + json.dumps({"alpha": ENTRY}).encode("utf-8"))
            text = coordinatorstart.context(
                {"repos_root": ROOT, "cap": {"alpha": [{"cap": 4}]}},
                env={"AGENT_TEMP": d}, cwd=ROOT + "/alpha", now=NOW, procs=[], run=lambda a, timeout=10: "G",
                kit="H", pid=1, pm_cfg={"sessions_file": p}, machine_cfg={})
        self.assertIn("- agent cap 4", text)

    def test_repos_root_comes_from_the_config(self):
        self.assertIn("could not check cap: repos_root not configured", self.build({"repos_root": None}))


class NoRulingsCheckTest(unittest.TestCase):
    """The start reads no rulings ledger: no check, no line, no reader, whatever the config says."""
    CAP = {"cap": {"alpha": [{"cap": 4}]}}

    def test_no_rulings_line_with_or_without_a_ledger_key(self):
        t = ContextTest()
        plain, keyed = t.build(self.CAP), t.build(dict(self.CAP, ledger="L.md"))
        self.assertEqual(plain, keyed)                   # a "ledger" key in the config changes nothing
        for text in (plain, keyed, t.build({}), t.build({"ledger": "L.md"})):
            self.assertNotIn("rulings newer", text)
            self.assertNotIn("could not check rulings", text)
            self.assertNotIn("rulings", text.lower())

    def test_no_rulings_line_when_no_check_can_run(self):
        # No sessions file, or a cwd outside repos_root: every check fails, and none is a rulings check.
        t = ContextTest()
        for text in (t.build({"ledger": "L.md"}, pm_cfg={}), t.build({"ledger": "L.md"}, cwd="C:/elsewhere")):
            self.assertIn("could not check cap:", text)
            self.assertNotIn("rulings", text.lower())

    def test_only_the_sessions_file_is_read(self):
        # The entry's state note and a configured ledger both exist; neither is opened.
        files = {"S.json": json.dumps({"alpha": ENTRY}), "STATE.md": "a note", "L.md": "a ledger"}
        seen = []

        def read(p):
            seen.append(p)
            return files[p]
        ContextTest().build(dict(self.CAP, ledger="L.md"), read=read)
        # the landing lock file is also read (never the ledger or the state note)
        self.assertEqual([p for p in seen if not p.endswith(landing.LOCK_NAME)], ["S.json"])

    def test_the_reader_and_its_limits_are_gone(self):
        for name in ("rulings", "ledger_file", "LEDGER_CAP", "DECIDES_CHARS", "safepath"):
            self.assertFalse(hasattr(coordinatorstart, name), name)


ID = {"i": "R-%04d" % 1}     # built, so no ruling id sits in the file (tests/scrub)


class LedgerLineTest(unittest.TestCase):
    """With the split ledger (ledger_dir), one line names this repo's ledger file, the reference for
    HOW the repo works; it comes from the machine config and the file is never opened."""
    CAP = {"cap": {"alpha": [{"cap": 4}]}}
    DIR = "D:/pm/rulings"
    SPLIT = {"ledger_dir": DIR, "ledger_repos": ["kit", "alpha", "pm"], "ledger_path": "D:/pm/LEDGER.md"}

    def test_the_repos_file_is_named_once(self):
        text = ContextTest().build(self.CAP, machine_cfg=self.SPLIT)
        want = "- ledger (reference, HOW this repo works): %s" % os.path.join(self.DIR, "alpha.md")
        self.assertEqual([l for l in text.splitlines() if l.startswith("- ledger")], [want])

    def test_the_repo_name_is_matched_without_case(self):
        self.assertEqual(coordinatorstart.ledger_lines(self.SPLIT, "Alpha")[0],
                         "ledger (reference, HOW this repo works): %s" % os.path.join(self.DIR, "alpha.md"))

    def test_the_sessions_repo_name_is_lower_cased_with_underscores_as_hyphens(self):
        machine = dict(self.SPLIT, ledger_repos=["my-tools-repo", "side-repo", "pm"])
        for repo, name in (("My-Tools_Repo", "my-tools-repo"), ("my_tools_repo", "my-tools-repo"),
                           ("side-repo", "side-repo"), ("Side_Repo", "side-repo")):
            with self.subTest(repo=repo):
                self.assertEqual(coordinatorstart.ledger_lines(machine, repo)[0],
                                 "ledger (reference, HOW this repo works): %s"
                                 % os.path.join(self.DIR, name + ".md"))

    def test_a_repo_not_in_ledger_repos_gets_the_folder_and_the_names(self):
        line = coordinatorstart.ledger_lines(self.SPLIT, "Some_Folder")[0]
        self.assertEqual(line, "ledger (reference, HOW each repo works): one file per ledger_repos name in %s "
                               "(kit, alpha, pm); none is named some-folder (from Some_Folder)" % self.DIR)
        line = coordinatorstart.ledger_lines(self.SPLIT, "beta")[0]
        self.assertTrue(line.endswith("(kit, alpha, pm); none is named beta"), line)

    def test_no_ledger_file_line_without_ledger_dir(self):
        for machine in ({}, {"ledger_path": "D:/pm/LEDGER.md", "ledger_repos": ["alpha"]},
                        {"ledger_dir": None, "ledger_repos": ["alpha"]}):
            with self.subTest(machine_cfg=machine):
                line, = coordinatorstart.ledger_lines(machine, "alpha")
                self.assertTrue(line.startswith("rulebooks (standing rules)"), line)
                self.assertNotIn("- ledger", ContextTest().build(self.CAP, machine_cfg=machine))

    def _rules(self, books):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "rules"))
        for name, text in books.items():
            with open(os.path.join(d, "rules", name + ".md"), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        return d

    def test_the_rulebook_line_prints_without_ledger_dir_and_says_none(self):
        text = ContextTest().build(self.CAP, machine_cfg={})
        self.assertIn("- rulebooks (standing rules): none", text)

    def test_the_rulebook_line_shows_counts_and_the_repo_command(self):
        d = self._rules({
            "all": "# Shared\n\n## T\n- a.one: x\n  (from %(i)s)\n- a.two: y\n  (from %(i)s)\n" % ID,
            "alpha": "# Alpha\n\nRepos: alpha\n\n## T\n- k.one: x\n  (from %(i)s)\n" % ID})
        machine = {"ledger_dir": d, "ledger_repos": ["alpha"]}
        line = coordinatorstart.ledger_lines(machine, "Alpha")[1]
        self.assertIn("alpha 1, shared 2 rules", line)
        self.assertIn(os.path.join(d, "rules"), line)
        self.assertTrue(line.endswith('tools/ledger.py" rules --repo Alpha'), line)

    def test_a_missing_rulebook_says_so(self):
        d = self._rules({})
        line = coordinatorstart.ledger_lines({"ledger_dir": d, "ledger_repos": ["alpha"]}, "alpha")[1]
        self.assertEqual(line, "rulebooks (standing rules): none in %s" % os.path.join(d, "rules"))

    def test_the_ledger_file_is_never_opened(self):
        seen = []
        files = {"S.json": json.dumps({"alpha": ENTRY})}

        def read(p):
            seen.append(p)
            return files[p]
        text = ContextTest().build(self.CAP, read=read, machine_cfg=self.SPLIT)
        self.assertIn("- ledger (reference", text)
        # the landing lock file is also read (never the ledger or the state note)
        self.assertEqual([p for p in seen if not p.endswith(landing.LOCK_NAME)], ["S.json"])

    def test_no_line_when_not_live(self):
        self.assertNotIn("ledger", coordinatorstart.context(dict(self.CAP, standing=["s"]), live=False,
                                                            machine_cfg=self.SPLIT))

    def test_a_config_that_cannot_be_read_gives_the_unknown_rulebook_line_only(self):
        with mock.patch.object(coordinatorstart.kuser, "user_config",
                               side_effect=ConfigError("bad key")):
            broken = ContextTest().build(self.CAP, machine_cfg=None)
        self.assertNotIn("- ledger", broken)
        self.assertIn("- rulebooks (standing rules): unknown - the machine config cannot be read (see the machine config line)", broken)
        self.assertNotIn("no ledger_dir or ledger_path is set", broken)
        self.assertIn("- agent cap 4", broken)

    def test_a_bug_reading_the_config_is_reported_not_dropped(self):
        with mock.patch.object(coordinatorstart.kuser, "user_config", side_effect=TypeError("bug")):
            text = ContextTest().build(self.CAP, machine_cfg=None)
        self.assertIn("- could not check ledger: TypeError: bug", text)
        self.assertIn("- agent cap 4", text)

    def test_without_ledger_dir_the_sessions_entry_is_not_needed(self):
        # No sessions file: cap and the others cannot run, and the ledger check adds only the rulebook line.
        text = ContextTest().build({}, pm_cfg={}, machine_cfg={})
        self.assertNotIn("could not check ledger", text)
        self.assertIn("- rulebooks (standing rules): none", text)

    def test_without_ledger_dir_a_missing_sessions_file_is_no_ledger_error(self):
        def read(path):
            raise FileNotFoundError(path)
        text = ContextTest().build(self.CAP, machine_cfg={}, read=read)
        self.assertNotIn("could not check ledger", text)
        self.assertIn("- rulebooks (standing rules): none", text)

    def test_ledger_path_only_names_the_real_repo_in_the_command_through_context(self):
        d = self._rules({})
        text = ContextTest().build(self.CAP, machine_cfg={"ledger_path": os.path.join(d, "LEDGER.md")})
        row, = [l for l in text.splitlines() if l.startswith("- rulebooks")]
        self.assertIn("none in %s" % os.path.join(d, "rules"), row)
        d = self._rules({"all": "# Shared\n\n## T\n- a.one: x\n  (from %(i)s)\n" % ID})
        text = ContextTest().build(self.CAP, machine_cfg={"ledger_path": os.path.join(d, "LEDGER.md")})
        row, = [l for l in text.splitlines() if l.startswith("- rulebooks")]
        self.assertTrue(row.endswith("rules --repo alpha"), row)
        self.assertNotIn("<this repo", row)

    def test_a_ledger_path_only_config_reads_the_rules_beside_the_frozen_file(self):
        d = self._rules({"all": "# Shared\n\n## T\n- a.one: x\n  (from %(i)s)\n" % ID})
        machine = {"ledger_path": os.path.join(d, "LEDGER.md")}
        line, = coordinatorstart.ledger_lines(machine, "alpha")
        self.assertIn("in %s: shared 1 rules" % os.path.join(d, "rules"), line)
        self.assertTrue(line.endswith("rules --repo alpha"), line)


@real_git('the hook child runs git check-ignore (the auto-tasks check): the fake lacks check-ignore')
class HookTest(unittest.TestCase):
    def run_hook(self, role, config):
        with tempfile.TemporaryDirectory() as d:
            machine_config(d, config, case=self)        # the fenced pointer's PM repo, which the child inherits
            env = {k: v for k, v in os.environ.items() if k not in ("PM_KIT_ROLE", "PM_KIT_SESSION_NAME")}
            env.update(USERPROFILE=d, HOME=d, AGENT_TEMP=d, TEMP=d, TMP=d)
            if role:
                env["PM_KIT_ROLE"] = role
            return subprocess.run([sys.executable] + HOOK, capture_output=True, text=True, env=env, timeout=30,
                                  input=json.dumps({"cwd": "D:/src/elsewhere"}))

    def test_role_unset_silent_exit_0(self):
        r = self.run_hook(None, json.dumps({"coordinator_start": {"standing": ["a"]}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_role_coordinator_emits_context(self):
        r = self.run_hook("coordinator", json.dumps({"coordinator_start": {"standing": ["wind down"]}}))
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("- wind down", out["additionalContext"])

    def test_a_plugin_prefixed_mark_still_fires(self):
        r = self.run_hook(P + "coordinator", json.dumps({"coordinator_start": {"standing": ["wind down"]}}))
        self.assertEqual(r.returncode, 0)
        self.assertIn("- wind down", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_another_plugins_prefix_stays_silent(self):
        r = self.run_hook("some-plugin:coordinator", json.dumps({"coordinator_start": {"standing": ["a"]}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_role_coordinator_checks_run_and_never_fail_the_hook(self):
        # temp HOME: no pm_start config, no machine_lock.py -> one could-not line per check
        r = self.run_hook("coordinator", json.dumps({"coordinator_start": {"standing": ["s"]}}))
        self.assertEqual(r.returncode, 0)
        text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(text.split("\n")[1], "- s")
        for name in ("cap", "machine state", "other coordinators"):
            self.assertIn("could not check %s:" % name, text)
        self.assertNotIn("rulings", text.lower())

    def test_the_retired_old_mark_and_old_key_are_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["s"]}}))
            for role in ("conductor", P + "conductor"):
                self.assertIsNone(coordinatorstart.render(env={"PM_KIT_ROLE": role}, path=p), role)
            old = write(os.path.join(d, "old.json"), json.dumps({"conductor_start": {"standing": ["old"]}}))
            self.assertIsNone(coordinatorstart.load_config(old))

    def test_bad_json_exit_0(self):
        r = self.run_hook("coordinator", "{bad")
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not check", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()


class LinuxCli(unittest.TestCase):
    """On Linux own_name and others take only a process that runs a host's CLI
    (pmstart.runs_a_cli): a node with --remote-control that runs anything else is no session."""
    def test_own_name_and_others_skip_a_node_that_runs_no_cli(self):
        rc = "node /usr/local/bin/claude --remote-control %s -n x"
        procs = [proc(1, 0, "node", rc % "alpha-coordinator-a2-n6", exe="/usr/bin/node"),
                 proc(2, 1, "node", "node app.js --remote-control alpha-coordinator-a2-n9", exe="/usr/bin/node"),
                 proc(3, 2, "bash", "bash", exe="/usr/bin/bash")]
        with mock.patch("kitlib.platform.NAME", "linux"):
            self.assertEqual(coordinatorstart.own_name(procs, {}, 3), "alpha-coordinator-a2-n6")
            self.assertEqual(coordinatorstart.others(ENTRY, procs, {}, 3), [])
