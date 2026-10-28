"""kitlib.proc.process ancestry: the walk ancestors_of makes, the exited flag it reports, and the
walk past an exited ancestor (through_exited) that kitlib.lock.store.caller asks for. Fakes only
(tests/proc/fakeproc.py); tests/bridge/test_ancestry.py walks real processes."""
import unittest
from unittest import mock

from kitlib.proc import process

from tests.proc.fakeproc import FakeProc, patched
from tests.platforms import windows_only

ME, SHELL, HOST, TOP = 900001, 900002, 900003, 900004
WALK = ('the fakes answer the calls the Windows walk makes (handles, the listing); on Linux the '
        'walk reads /proc (tests/proc/test_linux_process.py)')


def table():
    """this process -> SHELL (exited, held open, no longer listed) -> HOST -> TOP, whose parent
    cannot be opened."""
    return {ME: FakeProc(400, SHELL, False, 'python.exe'),
            SHELL: FakeProc(300, HOST, True, None),
            HOST: FakeProc(200, TOP, False, 'claude.exe'),
            TOP: FakeProc(100, 900099, False, 'explorer.exe')}


def walk(t, through_exited, parents=None, limit=64, listing=None):
    """The walk on table t; listing, when given, replaces the system's process listing the table
    gives (process.system_processes)."""
    with patched(t, ME):
        parents = {p: pp for p, pp, _n in process.snapshot()} if parents is None else parents
        if listing is None:
            return process.ancestry(ME, t[ME].ctime, parents, limit, through_exited=through_exited)
        with mock.patch.object(process, 'system_processes', listing):
            return process.ancestry(ME, t[ME].ctime, parents, limit, through_exited=through_exited)


@windows_only(WALK)
class Ancestry(unittest.TestCase):
    def test_by_default_the_walk_ends_at_an_exited_ancestor_and_says_so(self):
        t = table()
        with patched(t, ME):
            plain = process.ancestors_of(ME, 400, {ME: SHELL})
        self.assertEqual(plain, [(SHELL, 300)])
        self.assertEqual(walk(t, False), ([(SHELL, 300)], True))

    def test_a_chain_of_running_processes_is_not_exited(self):
        t = {ME: FakeProc(400, HOST, False, 'python.exe'), HOST: table()[HOST], TOP: table()[TOP]}
        self.assertEqual(walk(t, False), ([(HOST, 200), (TOP, 100)], False))
        self.assertEqual(walk(t, True), ([(HOST, 200), (TOP, 100)], False))

    def test_through_exited_goes_on_by_the_parent_the_exited_one_records(self):
        # The listing no longer names SHELL, so only its own handle gives HOST.
        self.assertEqual(walk(table(), True),
                         ([(SHELL, 300), (HOST, 200), (TOP, 100)], False))

    def test_a_reused_parent_above_an_exited_ancestor_ends_the_walk_there(self):
        # HOST's pid now names a process newer than SHELL: SHELL's real parent is gone.
        t = table()
        t[HOST] = FakeProc(350, TOP, False, 'claude.exe')
        self.assertEqual(walk(t, True), ([(SHELL, 300)], True))

    def test_an_exited_ancestor_whose_parent_is_gone_ends_the_walk_exited(self):
        t = table()
        del t[HOST]
        self.assertEqual(walk(t, True), ([(SHELL, 300)], True))

    def test_a_listed_parent_that_cannot_be_opened_above_an_exited_one_is_taken_as_running(self):
        # winlogon above the desktop's exited userinit (tests/lock/test_caller_tree.py).
        t = table()
        t[HOST] = FakeProc(200, TOP, False, 'winlogon.exe', opens=False)
        self.assertEqual(walk(t, True), ([(SHELL, 300)], False))

    def test_an_unlisted_parent_that_cannot_be_opened_above_an_exited_one_is_gone(self):
        t = table()
        t[HOST] = FakeProc(200, TOP, False, None, opens=False)
        self.assertEqual(walk(t, True), ([(SHELL, 300)], True))

    def test_an_unopenable_parent_the_listing_dates_after_the_exited_one_is_a_reused_pid(self):
        # HOST's pid now names a SYSTEM process made after SHELL (the snapshot lists it, the
        # system's listing gives its creation time): SHELL's real parent is gone.
        t = table()
        t[HOST] = FakeProc(350, TOP, False, 'svchost.exe', opens=False)
        self.assertEqual(walk(t, True), ([(SHELL, 300)], True))
        t[HOST] = FakeProc(300, TOP, False, 'svchost.exe', opens=False)     # same tick: running
        self.assertEqual(walk(t, True), ([(SHELL, 300)], False))

    def test_an_unopenable_parent_gone_from_the_listing_since_the_snapshot_is_gone(self):
        # The snapshot still names HOST; the system's listing, read later, no longer does.
        t = table()
        t[HOST] = FakeProc(200, TOP, False, 'winlogon.exe', opens=False)
        later = [(ME, 400, 'python.exe'), (TOP, 100, 'explorer.exe')]
        self.assertEqual(walk(t, True, listing=lambda: later), ([(SHELL, 300)], True))

    def test_with_no_listing_an_unopenable_parent_the_snapshot_names_is_running(self):
        def fails():
            raise OSError('no listing')
        t = table()
        t[HOST] = FakeProc(350, TOP, False, 'svchost.exe', opens=False)
        self.assertEqual(walk(t, True, listing=fails), ([(SHELL, 300)], False))   # cannot tell
        t[HOST] = FakeProc(350, TOP, False, None, opens=False)
        self.assertEqual(walk(t, True, listing=fails), ([(SHELL, 300)], True))

    def test_a_walk_reads_the_listing_once_and_only_at_an_unopenable_parent_past_an_exited_one(self):
        calls = []

        def counted(t):
            def listing():
                calls.append(1)
                return [(pid, p.ctime, p.listed) for pid, p in t.items() if p.listed]
            return listing
        t = table()
        t[HOST] = FakeProc(200, TOP, False, 'winlogon.exe', opens=False)
        self.assertEqual(walk(t, True, listing=counted(t)), ([(SHELL, 300)], False))
        self.assertEqual(len(calls), 1)
        del calls[:]
        # Every hop opens, the walk ends at an exited one, or an unopenable parent is above a
        # running hop: no listing is read.
        running_top = {ME: FakeProc(400, HOST, False, 'python.exe'), HOST: table()[HOST],
                       TOP: FakeProc(100, 900099, False, 'explorer.exe', opens=False)}
        for t, through in ((table(), False), (table(), True), (running_top, True)):
            walk(t, through, listing=counted(t))
        self.assertEqual(calls, [])

    def test_an_exited_ancestor_that_records_itself_as_its_parent_ends_the_walk(self):
        t = table()
        t[SHELL] = FakeProc(300, SHELL, True, None)
        self.assertEqual(walk(t, True), ([(SHELL, 300)], True))

    def test_a_parent_cycle_through_exited_ancestors_is_bounded(self):
        # SHELL and HOST each record the other. Creation order breaks the cycle when the two
        # differ; with equal creation times nothing does, and the limit ends the walk.
        t = table()
        t[HOST] = FakeProc(200, SHELL, True, None)
        self.assertEqual(walk(t, True), ([(SHELL, 300), (HOST, 200)], True))
        t[SHELL] = FakeProc(300, HOST, True, None)
        t[HOST] = FakeProc(300, SHELL, True, None)
        chain, exited = walk(t, True, limit=7)
        self.assertEqual(len(chain), 7)
        self.assertTrue(exited)

    def test_the_limit_bounds_the_walk_through(self):
        self.assertEqual(walk(table(), True, limit=2), ([(SHELL, 300), (HOST, 200)], False))


if __name__ == '__main__':
    unittest.main()
