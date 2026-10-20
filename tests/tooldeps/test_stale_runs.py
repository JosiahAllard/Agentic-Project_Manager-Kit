"""kitlib.tooldeps.selfrun.prune_stale: run folders a killed gate left in the kit's temp folder
go once a day old, claimed by a rename first, and nothing a link points at is ever touched."""
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from kitlib.tooldeps import selfrun
from tests.gitfixture import rmtree

DAY = 24 * 3600


def junction(target, link):
    """Make `link` a junction to `target`; False when it cannot be made here."""
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return True
    except (ImportError, OSError, AttributeError):
        return False


class PruneStale(unittest.TestCase):

    def setUp(self):
        self.parent = tempfile.mkdtemp(prefix='kit-prune-')
        self.addCleanup(rmtree, self.parent)
        self.now = time.time()

    def folder(self, name, age, files=('a.txt',)):
        path = os.path.join(self.parent, name)
        os.makedirs(os.path.join(path, 'sub'))
        for f in files:
            with open(os.path.join(path, 'sub', f), 'w') as fh:
                fh.write('x')
        self.age(path, age)
        return path

    def age(self, path, age):
        t = self.now - age
        os.utime(path, (t, t))

    def names(self):
        return sorted(os.listdir(self.parent))

    def test_an_old_run_folder_and_an_old_direct_folder_go(self):
        self.folder('kit-tests-old', DAY + 60)
        self.folder('kit-direct-old', DAY + 60)
        self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (2, 0))
        self.assertEqual(self.names(), [])

    def test_a_folder_younger_than_a_day_stays(self):
        self.folder('kit-tests-young', DAY - 60)
        self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (0, 0))
        self.assertEqual(self.names(), ['kit-tests-young'])

    def test_other_names_and_plain_files_stay(self):
        self.folder('kit-git-old', DAY * 3)
        self.folder('gate-old', DAY * 3)
        f = os.path.join(self.parent, 'kit-tests-file')
        with open(f, 'w') as fh:
            fh.write('x')
        self.age(f, DAY * 3)
        self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (0, 0))
        self.assertEqual(self.names(), ['gate-old', 'kit-git-old', 'kit-tests-file'])

    def test_a_claimed_folder_left_part_removed_is_claimed_again(self):
        self.folder('kit-tests-x.stale-1', DAY + 60)
        self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (1, 0))
        self.assertEqual(self.names(), [])

    def test_a_parent_that_cannot_be_listed_is_nothing(self):
        self.assertEqual(selfrun.prune_stale(os.path.join(self.parent, 'none')), (0, 0))

    @unittest.skipUnless(os.name == 'nt', 'Windows refuses to rename a folder with a file open')
    def test_a_folder_with_a_file_open_in_it_is_kept_whole(self):
        path = self.folder('kit-tests-busy', DAY + 60)
        held = open(os.path.join(path, 'sub', 'a.txt'))
        try:
            self.age(path, DAY + 60)
            self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (0, 1))
            self.assertEqual(self.names(), ['kit-tests-busy'])
            self.assertTrue(os.path.isfile(os.path.join(path, 'sub', 'a.txt')))
        finally:
            held.close()

    def outside(self):
        out = tempfile.mkdtemp(prefix='kit-prune-out-')
        self.addCleanup(rmtree, out)
        keep = os.path.join(out, 'keep.txt')
        with open(keep, 'w') as fh:
            fh.write('keep')
        return out, keep

    def test_a_junction_inside_a_stale_folder_is_not_followed(self):
        out, keep = self.outside()
        path = self.folder('kit-tests-link', DAY + 60)
        if not junction(out, os.path.join(path, 'sub', 'j')):
            self.skipTest('no junction can be made here')
        self.age(path, DAY + 60)
        self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (1, 0))
        self.assertEqual(self.names(), [])
        self.assertTrue(os.path.isfile(keep))

    def test_a_symlink_inside_a_stale_folder_is_not_followed(self):
        out, keep = self.outside()
        path = self.folder('kit-tests-sym', DAY + 60)
        try:
            os.symlink(out, os.path.join(path, 'sub', 's'), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('no symlink can be made here')
        self.age(path, DAY + 60)
        self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (1, 0))
        self.assertEqual(self.names(), [])
        self.assertTrue(os.path.isfile(keep))

    def test_a_stale_named_junction_is_left_and_its_target_untouched(self):
        out, keep = self.outside()
        link = os.path.join(self.parent, 'kit-tests-junction')
        if not junction(out, link):
            self.skipTest('no junction can be made here')
        # The junction's own times (os.lstat) are fresh: a clock three days on makes the
        # junction itself, not only its target, look stale, so only the reparse check keeps it.
        later = self.now + DAY * 3
        self.assertGreaterEqual(later - os.lstat(link).st_mtime, DAY)
        self.assertEqual(selfrun.prune_stale(self.parent, now=later), (0, 0))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.isfile(keep))
        os.rmdir(link)

    def test_an_entry_that_is_no_plain_folder_once_claimed_is_kept_not_removed(self):
        # Something swapped in between the first look and the rename: the second look refuses it.
        path = self.folder('kit-tests-swapped', DAY * 2)
        real = selfrun._plain_dir
        looks = []

        def plain(p):
            looks.append(p)
            return real(p) if len(looks) == 1 else None
        with mock.patch.object(selfrun, '_plain_dir', side_effect=plain):
            self.assertEqual(selfrun.prune_stale(self.parent, now=self.now), (0, 1))
        self.assertEqual(len(looks), 2)
        self.assertNotEqual(os.path.normcase(looks[1]), os.path.normcase(path))
        self.assertTrue(os.path.isfile(os.path.join(looks[1], 'sub', 'a.txt')))


class RunPrunes(unittest.TestCase):
    """selfrun.run prunes its run folder's parent, unfenced only."""

    def setUp(self):
        self.parent = tempfile.mkdtemp(prefix='kit-prune-')
        self.addCleanup(rmtree, self.parent)

    def run_once(self, fenced, note=None, pruned=(0, 0)):
        calls = []
        work = tempfile.mkdtemp(prefix=selfrun.RUN_PREFIX, dir=self.parent)

        def unit(u, audit, timeout, root=None, cmd=None, env=None):
            return u, 'OK', 1, 0.0, ''
        out = io.StringIO()
        with mock.patch.object(selfrun, 'make_work', return_value=(work, note)), \
                mock.patch.object(selfrun.kuser, 'fenced', return_value=fenced), \
                mock.patch.object(selfrun, 'prune_stale',
                                  side_effect=lambda p: calls.append(p) or pruned):
            rc, _s = selfrun.run(['tests.a.test_1'], self.parent, 1, out=out, run_unit=unit,
                                 counts={})
        self.assertEqual(rc, 0, out.getvalue())
        return calls, out.getvalue()

    def test_an_unfenced_run_prunes_its_parent_and_says_so(self):
        calls, out = self.run_once(False, pruned=(2, 1))
        self.assertEqual(calls, [self.parent])
        self.assertIn('removed 2 stale run folder(s) from %s (1 older one(s) left: in use)'
                      % self.parent, out)

    def test_nothing_pruned_says_nothing(self):
        calls, out = self.run_once(False)
        self.assertEqual(calls, [self.parent])
        self.assertNotIn('stale', out)

    def test_a_fenced_run_or_a_fallback_folder_prunes_nothing(self):
        self.assertEqual(self.run_once(True)[0], [])
        self.assertEqual(self.run_once(False, note='the run folder X is elsewhere')[0], [])


if __name__ == '__main__':
    unittest.main()
