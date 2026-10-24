"""kitlib.proc.process ancestry: the walk ancestors_of makes, the exited flag it reports, and the
walk past an exited ancestor (through_exited) that kitlib.lock.store.caller asks for. Fakes only
(tests/proc/fakeproc.py); tests/bridge/test_ancestry.py walks real processes."""
import unittest

from kitlib.proc import process

from tests.proc.fakeproc import FakeProc, patched

ME, SHELL, HOST, TOP = 900001, 900002, 900003, 900004


def table():
    """this process -> SHELL (exited, held open, no longer listed) -> HOST -> TOP, whose parent
    cannot be opened."""
    return {ME: FakeProc(400, SHELL, False, 'python.exe'),
            SHELL: FakeProc(300, HOST, True, None),
            HOST: FakeProc(200, TOP, False, 'claude.exe'),
            TOP: FakeProc(100, 900099, False, 'explorer.exe')}


def walk(t, through_exited, parents=None, limit=64):
    with patched(t, ME):
        parents = {p: pp for p, pp, _n in process.snapshot()} if parents is None else parents
        return process.ancestry(ME, t[ME].ctime, parents, limit, through_exited=through_exited)


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
