"""The PLUGIN event: pm_watch's check of the pm-kit plugin export against the kit's
refs/remotes/<remote>/main (kitlib.pmwatch.plugin_check over kitlib.pluginsource.poll). A temp kit
with a bare remote, a temp export root; spawn and the liveness probe are fakes."""
import json
import os
import unittest
from unittest import mock

from kitlib import pluginsource as P
from kitlib import pmwatch as W
from tests.pluginsource import Kit


class PluginEventTest(unittest.TestCase):
    def setUp(self):
        self.k = Kit(self)
        self.spawned = []
        self.live = set()

    def watcher(self, auto, state=None, folders=(), hub_job=lambda: False):
        s = P.Settings(self.k.root, auto, "origin")
        check = W.plugin_check(kit=self.k.kit, settings=s,
                               spawn=lambda kit, root: self.spawned.append((kit, root)),
                               alive=lambda pid, ct: pid in self.live, folders=list(folders),
                               hub_job=hub_job)
        w = W.Watcher(state if state is not None else {}, [], lambda: {}, os.path.join(self.k.base, "alerts"),
                      os.path.join(self.k.base, "sessions.json"), lambda n: None, branch_poll=0,
                      plugin=check)
        w.start(0.0)
        return w

    def main(self):
        return self.k.repo.git("rev-parse", "refs/remotes/origin/main")

    def export_at(self, sha):
        P.write_state(self.k.root, {"sha": sha, "folders": {}})

    def advance(self):
        r, self.n = self.k.repo, getattr(self, "n", 0) + 1
        name = "later-%d.txt" % self.n
        r.write(name, "z\n")
        r.commit("later", name)
        r.git("push", "-q", "origin", "main")
        return self.main()

    def test_current_is_silent(self):
        os.makedirs(self.k.root)
        self.export_at(self.main())
        w = self.watcher(auto=True)
        self.assertEqual(w.poll(1.0), [])
        self.assertEqual(self.spawned, [])

    def test_behind_reports_once_per_sha_and_auto_off_spawns_nothing(self):
        os.makedirs(self.k.root)
        self.export_at(self.main())
        new = self.advance()
        w = self.watcher(auto=False)
        self.assertEqual(w.poll(1.0), ["PLUGIN pm-kit plugin behind kit main %s: %s"
                                       % (new[:12], P.refresh_cmd())])
        self.assertEqual(w.poll(2.0), [])
        w2 = self.watcher(auto=False, state=json.loads(json.dumps(w.s)))   # a relaunch: saved state
        self.assertEqual(w2.poll(3.0), [])
        newer = self.advance()
        self.assertEqual(len(w2.poll(4.0)), 1)
        self.assertIn(newer[:12], w2.s["plugin_main"])
        self.assertEqual(self.spawned, [])
        self.export_at(newer)                       # refreshed: silent, and the memory clears
        self.assertEqual(w2.poll(5.0), [])
        self.assertIsNone(w2.s["plugin_main"])

    def test_auto_refresh_spawns_once_per_sha_and_not_while_a_refresh_runs(self):
        os.makedirs(self.k.root)
        self.export_at(self.main())
        self.advance()
        w = self.watcher(auto=True)
        ev = w.poll(1.0)
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].endswith("(auto_refresh: started)"), ev[0])
        self.assertEqual(self.spawned, [(self.k.kit, self.k.root)])
        self.assertEqual(w.poll(2.0), [])
        self.assertEqual(len(self.spawned), 1)
        with open(os.path.join(self.k.root, P.PIDFILE), "w", encoding="utf-8") as fh:
            json.dump({"pid": 4242, "ctime": 1.0}, fh)
        self.live.add(4242)
        self.advance()
        ev = w.poll(3.0)
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].endswith("(auto_refresh: a refresh is already running)"), ev[0])
        self.assertEqual(len(self.spawned), 1)

    def test_the_hub_plugin_job_up_owns_the_refresh_and_hub_down_falls_back(self):
        os.makedirs(self.k.root)
        self.export_at(self.main())
        new = self.advance()
        w = self.watcher(auto=True, hub_job=lambda: True)
        self.assertEqual(w.poll(1.0), ["PLUGIN pm-kit plugin behind kit main %s: %s"
                                       % (new[:12], P.refresh_cmd())])
        self.assertEqual(self.spawned, [])
        w = self.watcher(auto=True, hub_job=lambda: False)
        self.assertTrue(w.poll(1.0)[0].endswith("(auto_refresh: started)"))
        self.assertEqual(len(self.spawned), 1)

    def test_hub_plugin_job_up_reads_health_jobs(self):
        class Client:
            def __init__(self, health):
                self.health = health

            def get(self, path, timeout=None):
                if isinstance(self.health, Exception):
                    raise self.health
                return self.health

        def up(health):
            return W.hub_plugin_job_up(load=lambda: mock.Mock(port=1), make_client=lambda port: Client(health))
        self.assertTrue(up({"jobs": {"plugin-refresh": {}}}))
        self.assertFalse(up({"jobs": {}}))
        self.assertFalse(up({}))
        self.assertFalse(up(OSError("down")))

    def test_a_failing_check_is_an_error_on_the_third(self):
        def boom(last):
            raise P.RefreshError("LOCALAPPDATA is not set")
        w = W.Watcher({}, [], lambda: {}, os.path.join(self.k.base, "alerts"),
                      os.path.join(self.k.base, "sessions.json"), lambda n: None, branch_poll=0,
                      plugin=boom)
        w.start(0.0)
        got = [w.poll(1.0 + i) for i in range(4)]
        self.assertEqual([g for g in got if g], [["ERROR plugin check: LOCALAPPDATA is not set"]])


    def test_no_export_or_a_folder_not_migrated_names_migrate_and_starts_nothing(self):
        w = self.watcher(auto=True)
        ev = w.poll(1.0)
        self.assertEqual(ev, ["PLUGIN pm-kit plugin behind kit main %s: %s (auto_refresh: not started: "
                              "no export yet)" % (self.main()[:12], P.refresh_cmd(True))])
        os.makedirs(self.k.root)
        (two,) = self.k.folders(".claude-two")
        self.export_at(self.main())
        new = self.advance()
        w = self.watcher(auto=True, folders=[two])
        self.assertEqual(w.poll(2.0), ["PLUGIN pm-kit plugin behind kit main %s: %s (auto_refresh: not "
                                       "started: not migrated in %s)" % (new[:12], P.refresh_cmd(True), two)])
        self.assertEqual(self.spawned, [])

    def test_an_unreadable_ref_or_state_is_an_error_on_the_third(self):
        for broken in ("ref", "state"):
            w = self.watcher(auto=False)
            if broken == "state":
                os.makedirs(self.k.root, exist_ok=True)
                with open(os.path.join(self.k.root, P.STATE), "w", encoding="utf-8") as fh:
                    fh.write("{not json")
                got = [w.poll(1.0 + i) for i in range(4)]
            else:
                with mock.patch.object(P, "tracking_main", lambda kit, remote: (None, "git broke")):
                    got = [w.poll(1.0 + i) for i in range(4)]
            errs = [g for g in got if g]
            self.assertEqual(len(errs), 1, got)
            self.assertTrue(errs[0][0].startswith("ERROR plugin check: "), errs)
            self.assertIn("git broke" if broken == "ref" else P.STATE, errs[0][0])

    def export_kit(self, checkout):
        """A folder shaped like the plugin export the watcher runs from: a marker, no .git."""
        folder = os.path.join(self.k.base, "export-kit")
        os.makedirs(folder)
        with open(os.path.join(folder, P.MARKER), "w", encoding="utf-8") as fh:
            json.dump({"checkout": checkout}, fh)
        return folder

    def test_a_kit_that_is_an_export_reads_the_ref_from_its_source_checkout(self):
        os.makedirs(self.k.root)
        self.export_at(self.main())
        new = self.advance()
        s = P.Settings(self.k.root, False, "origin")
        got, _ = W.plugin_check(kit=self.export_kit(self.k.kit), settings=s, folders=[])(None)
        self.assertEqual(got, ["pm-kit plugin behind kit main %s: %s" % (new[:12], P.refresh_cmd())])

    def test_an_export_with_no_source_checkout_is_a_failed_check_with_a_clear_line(self):
        s = P.Settings(self.k.root, False, "origin")
        check = W.plugin_check(kit=self.export_kit(os.path.join(self.k.base, "gone")), settings=s,
                               folders=[])
        with self.assertRaises(P.RefreshError) as cm:
            check(None)
        self.assertIn("export", str(cm.exception))
        self.assertIn("source checkout", str(cm.exception))

    def test_no_tracking_ref_is_silent(self):
        with mock.patch.object(P, "tracking_main", lambda kit, remote: ("", None)):
            w = self.watcher(auto=True)
            self.assertEqual([w.poll(1.0 + i) for i in range(4)], [[], [], [], []])


if __name__ == "__main__":
    unittest.main()
