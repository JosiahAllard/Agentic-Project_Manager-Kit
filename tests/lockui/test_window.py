"""kitlib.lockui.window and reserve_ui.pyw: import smoke tests, and the window's refresh and
write paths with a fake client (skipped where tkinter has no display). The new files also pass the
shipped-text scrub (tests/shipped.py)."""
import importlib.util
import os
import threading
import time
import unittest
from unittest import mock

from tests.shipped import ROOT, forbidden, violations

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    HAVE_TK = True
except Exception:                   # no tkinter, or no display
    HAVE_TK = False

GOOD = {"revision": 1, "holds": [], "runs": [], "queue": {"entries": []}, "slots": {"cpu": [], "gpu": []},
        "layout": {"cpu_slots": 2, "gpu_slots": 0}, "game": {"enabled": False, "games": []}}


class FakeClient:
    def __init__(self, snap=None, fail=None):
        self.snap, self.fail = snap if snap is not None else GOOD, fail
        self.reads, self.threads = 0, set()

    def snapshot(self):
        self.reads += 1
        self.threads.add(threading.get_ident())
        if self.fail:
            raise self.fail
        return self.snap

    def history(self, days=7):
        return []

    def game_mode(self, enabled):
        return {"ok": True, "result": {}, "snapshot": dict(GOOD, revision=2,
                                                           game={"enabled": enabled, "games": []})}


class BlockingClient(FakeClient):
    """A read that waits for go: a write can land while it is out."""

    def __init__(self, fail=None):
        FakeClient.__init__(self)
        self.started, self.go, self.later_fail = threading.Event(), threading.Event(), fail

    def snapshot(self):
        self.started.set()
        self.go.wait(10)
        self.fail = self.later_fail
        return FakeClient.snapshot(self)


class Scrub(unittest.TestCase):
    def test_the_window_files_carry_no_ruling_ids_names_or_paths(self):
        self.assertEqual(violations(ROOT / "kitlib" / "lockui"), [])
        self.assertEqual(violations(ROOT / "tests" / "lockui"), [])
        entry = ROOT / "reserve_ui.pyw"             # at the kit root; fail, never pass vacuously
        self.assertTrue(entry.is_file(), "reserve_ui.pyw is missing from the kit root")
        text = entry.read_text(encoding="utf-8", errors="replace")
        self.assertEqual([what for rx, what in forbidden() if rx.search(text)], [])


@unittest.skipUnless(HAVE_TK, "no tkinter display")
class Window(unittest.TestCase):
    def setUp(self):
        from kitlib.lockui import window
        self.window = window
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def pending_afters(self):
        return self.root.tk.splitlist(self.root.tk.call("after", "info"))

    def test_the_entry_point_imports(self):
        spec = importlib.util.spec_from_file_location("reserve_ui_entry",
                                                      os.path.join(ROOT, "reserve_ui.pyw"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)            # not __main__: nothing opens
        self.assertIs(mod.window, self.window)

    def test_a_snapshot_the_view_cannot_build_is_shown_and_refresh_goes_on(self):
        app = self.window.App(self.root, FakeClient(snap={"holds": 5}), threaded=False)
        self.assertIn("could not be shown", app.info.get())
        self.assertTrue(self.pending_afters())

    def test_a_read_error_is_shown_and_refresh_goes_on(self):
        app = self.window.App(self.root, FakeClient(fail=OSError("gone")), threaded=False)
        self.assertIn("gone", app.info.get())
        self.assertTrue(self.pending_afters())

    def test_reads_run_off_the_tk_thread(self):
        c = FakeClient()
        app = self.window.App(self.root, c, auto_refresh=False)
        end = time.monotonic() + 5
        while app.busy and time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy)
        self.assertEqual(app.snap["revision"], 1)
        self.assertNotIn(threading.get_ident(), c.threads)

    def test_the_division_dialog_opens_on_the_layout_it_gets(self):
        snap = dict(GOOD, layout={"cpu_slots": 12, "half": {"cpu": 6, "gpu": 1}, "agent_cap": 6,
                                  "owner_division": {"cpu_slots": 12}, "default_division": {}})
        app = self.window.App(self.root, FakeClient(snap=snap), auto_refresh=False, threaded=False)
        d = app.open_division()
        self.addCleanup(d.destroy)
        self.assertEqual(d.vars["cpu_slots"].get(), "12")
        self.assertEqual(d.vars["agent_cap"].get(), "")

    def division_app(self, default):
        """A window whose client records division writes; the owner set cpu_slots 12."""
        snap = dict(GOOD, layout={"cpu_slots": 12, "owner_division": {"cpu_slots": 12},
                                  "default_division": default})
        c = FakeClient(snap=snap)
        c.divisions = []

        def division(d):
            c.divisions.append(d)
            return {"ok": True, "result": {"applied": True, "pending": None},
                    "snapshot": dict(snap, revision=2)}
        c.division = division
        return self.window.App(self.root, c, auto_refresh=False, threaded=False), c

    def test_a_division_save_sends_the_fields(self):
        said = self.told()
        app, c = self.division_app({"cpu_slots": 6, "agent_cap": 3})
        d = app.open_division()
        d.vars["cpu_slots"].set("8")
        d.ok()
        self.assertEqual(c.divisions, [{"cpu_slots": 8}])
        self.assertEqual(said, ["done"])

    def test_an_all_blank_division_save_writes_the_default_and_says_so(self):
        said = self.told()
        app, c = self.division_app({"cpu_slots": 6, "agent_cap": 3, "gpu_share": [4, 2]})
        d = app.open_division()
        d.vars["cpu_slots"].set("")
        d.ok()
        self.assertEqual(c.divisions, [{"cpu_slots": 6, "agent_cap": 3, "gpu_share": [4, 2]}])
        self.assertNotIn(None, c.divisions)         # never the removal
        self.assertEqual(len(said), 1)
        self.assertIn("Every field was blank", said[0])
        self.assertIn("cpu_slots 6, agent_cap 3, gpu_share 4, 2", said[0])

    def test_an_all_blank_division_save_without_a_known_default_is_refused(self):
        app, c = self.division_app({})
        d = app.open_division()
        self.addCleanup(d.destroy)
        d.vars["cpu_slots"].set(" ")
        errs = []
        with mock.patch.object(self.window.messagebox, "showerror",
                               lambda title, text, **kw: errs.append(text)):
            d.ok()
        self.assertEqual(c.divisions, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("every field is blank", errs[0])

    def told(self):
        """Records the window's info boxes instead of opening them."""
        said = []
        patch = mock.patch.object(self.window.messagebox, "showinfo",
                                  lambda title, text, **kw: said.append(text))
        patch.start()
        self.addCleanup(patch.stop)
        return said

    def test_games_and_division_refuse_to_open_before_the_lock_is_read(self):
        said = self.told()
        app = self.window.App(self.root, FakeClient(fail=OSError("gone")), auto_refresh=False,
                              threaded=False)
        self.assertEqual(app.snap, {})
        self.assertIsNone(app.open_games())         # its Save would clear the games list
        self.assertIsNone(app.open_division())      # its Save would drop the owner's division
        self.assertEqual(len(said), 2)
        self.assertTrue(all("not read yet" in s for s in said))

    def test_games_and_division_refuse_while_the_first_read_is_out(self):
        said = self.told()
        c = BlockingClient()
        self.addCleanup(c.go.set)
        app = self.window.App(self.root, c, auto_refresh=False)
        self.assertTrue(c.started.wait(5))
        self.assertIsNone(app.open_games())
        self.assertIsNone(app.open_division())
        self.assertEqual(len(said), 2)

    def wait_idle(self, app):
        end = time.monotonic() + 5
        while app.busy and time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy)

    def queued_app(self):
        """A window on a snapshot with one waiting run (r-B), its client recording move_to_top."""
        entry = {"id": "r-B", "position": 1, "ticket": 2, "repo": "game", "slug": "e2e",
                 "kind": "measure", "needs": {"cpu": 1, "gpu": 0}, "since": None,
                 "est_start": None, "backfill": False, "parked": False, "moved": None, "why": None}
        snap = dict(GOOD, queue={"entries": [entry]})
        c = FakeClient(snap=snap)
        c.moves = []

        def move_to_top(rid):
            c.moves.append(rid)
            moved = dict(entry, moved={"by": "owner", "name": "w", "at": "2026-01-01T00:00:00Z",
                                       "seq": 1})
            return {"ok": True, "result": {"id": rid, "position": 1},
                    "snapshot": dict(snap, revision=2, queue={"entries": [moved]})}
        c.move_to_top = move_to_top
        app = self.window.App(self.root, c, auto_refresh=False, threaded=False)
        return app, c

    def buttons(self, app):
        return [w for w in app.queue.winfo_children() if isinstance(w, self.window.ttk.Button)]

    def test_each_waiting_run_has_a_move_to_top_button(self):
        app, _c = self.queued_app()
        self.assertEqual(len(app.queue.window_names()), 1)
        self.assertEqual([b.cget("text") for b in self.buttons(app)], ["Move to top"])
        app.load(app.snap)                          # a refresh replaces it, never adds one
        self.assertEqual(len(self.buttons(app)), 1)

    def test_move_to_top_asks_first_then_calls_the_operation(self):
        said = self.told()
        app, c = self.queued_app()
        asked = []
        with mock.patch.object(self.window.messagebox, "askyesno",
                               lambda title, text, **kw: asked.append(text) or False):
            self.buttons(app)[0].invoke()
        self.assertEqual(c.moves, [])               # no: nothing is called
        self.assertIn("r-B", asked[0])
        self.assertIn("keep them", asked[0])
        with mock.patch.object(self.window.messagebox, "askyesno", lambda *a, **kw: True):
            self.buttons(app)[0].invoke()
        self.assertEqual(c.moves, ["r-B"])
        self.assertEqual(app.snap["revision"], 2)   # the write's snapshot is shown
        self.assertIn("moved to the top by the owner", app.queue.get("1.0", "end"))
        self.assertEqual(said, [])                  # done quietly: the queue shows it

    def test_a_refused_move_to_top_is_shown(self):
        app, c = self.queued_app()
        c.move_to_top = lambda rid: {"ok": False, "error": {"code": "forbidden",
                                                            "message": "move_to_top is the owner's"},
                                     "snapshot": None}
        errs = []
        with mock.patch.object(self.window.messagebox, "askyesno", lambda *a, **kw: True), \
                mock.patch.object(self.window.messagebox, "showerror",
                                  lambda title, text, **kw: errs.append(text)):
            self.buttons(app)[0].invoke()
        self.assertEqual(errs, ["forbidden: move_to_top is the owner's"])

    def test_a_read_begun_before_a_write_is_not_shown(self):
        c = BlockingClient()
        app = self.window.App(self.root, c, auto_refresh=False)
        self.assertTrue(c.started.wait(5))          # the read (revision 1) is out
        app.game_on.set(True)
        app.toggle_game()                           # the write shows revision 2
        self.assertEqual(app.snap["revision"], 2)
        c.go.set()
        self.wait_idle(app)                         # the stale read arrived and was dropped
        self.assertEqual(c.reads, 1)
        self.assertEqual(app.snap["revision"], 2)

    def test_a_stale_read_error_does_not_cover_a_write(self):
        c = BlockingClient(fail=OSError("gone"))
        app = self.window.App(self.root, c, auto_refresh=False)
        self.assertTrue(c.started.wait(5))
        app.game_on.set(True)
        app.toggle_game()
        c.go.set()
        self.wait_idle(app)
        self.assertNotIn("could not be read", app.info.get())
        self.assertEqual(app.snap["revision"], 2)

    def test_a_write_shows_its_own_snapshot_without_reading_again(self):
        c = FakeClient()
        app = self.window.App(self.root, c, auto_refresh=False, threaded=False)
        reads = c.reads
        app.game_on.set(True)
        app.toggle_game()
        self.assertEqual(c.reads, reads)
        self.assertEqual(app.snap["revision"], 2)
        self.assertTrue(app.game_on.get())


if __name__ == "__main__":
    unittest.main()
