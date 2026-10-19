"""kitlib.dailyfull.bisect and kitlib.dailyfull.file: after a red daily run, the first-parent
commit that turned it red, and the backlog record filed for it through the real store (a ref
store in the scratch repository, pushed to its bare remote). The record's price comes from the
machine config's `daily_red_ruling`, read under a temp home (tests/home.py), never the real one.
The ruling id is made up, built at run time so no ruling id is written here."""
import os
import tempfile
import unittest

from kitlib import dailyfull as D
from kitlib.backlog import opener
from kitlib.dailyfull import bisect as B
from kitlib.dailyfull import file as filing
from tests import gitfixture
from tests.gate import fixtures
from tests.home import machine_config

from .test_dailyfull import Case

RULING = 'R-' + '9001'        # made up; never a real ruling


def bisect_kit(prefix='KIT'):
    conf = {'max_probes': 5, 'session': 'kit'}
    if prefix:
        conf['prefix'] = prefix
    return {'daily_full': {'enabled': True, 'kind': 'engine-test', 'bisect': conf},
            'backlog': {'store': 'ref'}}


class Search(unittest.TestCase):
    """search() with a fake probe: commits from index `first_red` on are red."""

    def probe(self, first_red, seen):
        def run(sha):
            seen.append(sha)
            return (B.RED, ['unit'], None) if int(sha) >= first_red else (B.GREEN, [], None)
        return run

    def commits(self, n):
        return ['%040d' % i for i in range(n)]

    def test_the_first_red_commit_is_found_in_about_log2_n_probes(self):
        for n in (1, 2, 5, 8, 13):
            for first in range(n):
                seen = []
                got = B.search(self.commits(n), self.probe(first, seen), 10)
                self.assertEqual(got['culprit'], self.commits(n)[first], (n, first))
                self.assertEqual(got['verdict'], 'culprit %s' % self.commits(n)[first][:10])
                self.assertEqual(seen[0], self.commits(n)[-1])       # the red run's commit first
                self.assertLessEqual(len(seen), 2 + (n - 1).bit_length(), (n, first))

    def test_the_red_commit_green_again_is_a_flake_after_one_probe(self):
        seen = []
        got = B.search(self.commits(6), self.probe(99, seen), 5)
        self.assertEqual(got['verdict'], B.FLAKE)
        self.assertIsNone(got['culprit'])
        self.assertEqual(len(seen), 1)

    def test_the_cap_names_the_range_left(self):
        seen = []
        got = B.search(self.commits(16), self.probe(5, seen), 2)
        self.assertEqual(len(seen), 2)
        self.assertIsNone(got['culprit'])
        self.assertIn('probe cap 2 reached', got['verdict'])
        self.assertEqual(got['bad'], self.commits(16)[7])

    def test_an_inconclusive_probe_stops_the_search(self):
        def probe(sha):
            return None, [], 'could not put it there'
        got = B.search(self.commits(4), probe, 5)
        self.assertEqual(got['verdict'], 'inconclusive: could not put it there')
        self.assertIsNone(got['culprit'])

    def test_an_infra_red_or_an_error_is_not_bisected(self):
        self.assertEqual(B.bisectable({'red': ['infra']}), [])
        self.assertEqual(B.bisectable({'red': ['unit'], 'error': 'x'}), [])
        self.assertEqual(B.bisectable({'red': ['unit', 'gate', 'windows']}), ['unit'])


class BisectCase(Case):
    KIT = bisect_kit()

    def setUp(self):
        Case.setUp(self)
        self.home = os.environ['USERPROFILE']        # Case's temp home

    def red_history(self):
        """A green run, then a, b, RED, c, d pushed one commit each, then a red run: (sha of the
        RED commit, the red result)."""
        self.run_daily('20261008-040000')
        shas = {}
        for name in ('a', 'b', 'RED', 'c', 'd'):
            self.push({name: 'x\n'})
            shas[name] = self.repo.git('rev-parse', 'HEAD')
        o = self.run_daily('20261009-040000')
        self.lines = o.lines
        return shas['RED'], self.result('20261009-040000')

    def record(self, rid):
        ctx = opener.open_store(self.main_dir, fetch=True)
        return ctx.store.load().get(rid)

    def daily_head(self):
        wt = os.path.join(self.main_dir, '.claude', 'worktrees', D.SLUG)
        return self.repo.git('rev-parse', 'HEAD', cwd=wt)


class Culprit(BisectCase):
    def test_the_culprit_is_found_the_tree_put_back_and_priority_2_filed_without_a_ruling(self):
        culprit, r = self.red_history()
        found = r['bisect']
        self.assertEqual(found['culprit'], culprit, self.lines)
        self.assertLessEqual(len(found['probes']), 4)
        self.assertEqual(self.daily_head(), r['sha'])
        rid = filing.record_id('KIT', culprit)
        self.assertEqual(found['filed'], rid, self.lines)
        rec = self.record(rid)
        self.assertEqual(rec.get('priority'), '2')
        self.assertEqual(rec.get('priority_by'), 'agent')
        self.assertIsNone(rec.get('priority_ruling'))
        self.assertEqual(rec.all('scope'), ['RED'])
        self.assertEqual(found['note'], filing.NO_RULING)
        self.assertIn('daily: %s' % filing.NO_RULING, self.lines)

    def test_with_daily_red_ruling_set_it_is_filed_at_priority_1_by_the_pm(self):
        machine_config(self.home, {'daily_red_ruling': RULING})
        culprit, r = self.red_history()
        rec = self.record(filing.record_id('KIT', culprit))
        self.assertEqual(rec.get('priority'), '1', self.lines)
        self.assertEqual(rec.get('priority_by'), 'pm')
        self.assertEqual(rec.get('priority_ruling'), RULING)
        self.assertNotIn('note', r['bisect'])

    def test_a_second_red_run_files_nothing_new(self):
        culprit, _r = self.red_history()
        rid = filing.record_id('KIT', culprit)
        before = self.record(rid).text
        self.run_daily('20261010-040000')
        r = self.result('20261010-040000')
        self.assertEqual(r['bisect']['culprit'], culprit)
        rec = self.record(rid)
        self.assertEqual(rec.text, before)
        ids = [i for i in self.ids() if '-DAILY-RED-' in i]
        self.assertEqual(ids, [rid])

    def ids(self):
        ctx = opener.open_store(self.main_dir, fetch=True)
        return [r.stem for r in ctx.store.load().records]


class NoPrefix(BisectCase):
    KIT = bisect_kit(prefix=None)

    def test_without_a_prefix_the_culprit_is_named_and_nothing_is_filed(self):
        culprit, r = self.red_history()
        self.assertEqual(r['bisect']['culprit'], culprit)
        self.assertNotIn('filed', r['bisect'])


class Flake(BisectCase):
    """The step fails on its second run only: the red run's commit probes green again."""

    def setUp(self):
        marker_dir = tempfile.mkdtemp(prefix='kit-flake-')
        self.addCleanup(gitfixture.rmtree, marker_dir)
        count = os.path.join(marker_dir, 'count').replace('\\', '/')
        code = ('import os, sys; p = %r; n = int(open(p).read()) + 1 if os.path.exists(p) else 1; '
                'open(p, "w").write(str(n)); print("ok"); sys.exit(1 if n == 2 else 0)' % count)
        self.GATE = {'steps': [dict(fixtures.GATE['steps'][0], cmd=['{python}', '-c', code])]}
        BisectCase.setUp(self)

    def test_a_red_that_does_not_reproduce_is_a_flake_and_files_nothing(self):
        self.run_daily('20261008-040000')
        self.push({'a': 'x\n'})
        self.run_daily('20261009-040000')
        r = self.result('20261009-040000')
        self.assertEqual(r['verdict'], 'red')
        self.assertEqual(r['bisect']['verdict'], B.FLAKE)
        self.assertEqual(len(r['bisect']['probes']), 1)
        self.assertNotIn('filed', r['bisect'])
        self.assertEqual(self.daily_head(), r['sha'])


if __name__ == '__main__':
    unittest.main()
