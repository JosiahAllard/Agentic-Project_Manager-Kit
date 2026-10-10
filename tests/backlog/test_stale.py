"""`stale`: age from the store's own history, body paths that name nothing, `files` entries that
moved since `files_at`. Ages are made deterministic with GIT_COMMITTER_DATE around the seeding."""
import datetime
import os
import subprocess
import sys
import tempfile
from unittest import mock

from kitlib.backlog import stale as stmod
from kitlib.backlog import store as smod
from kitlib.backlog import record as rmod
from tests.backlog import helpers
from tests.backlog.helpers import StoreCase, rec, TODAY
from tests.gitfixture import TempRepo, fixture_env, rmtree

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')


def at(date):
    return mock.patch.dict(os.environ, {'GIT_COMMITTER_DATE': date + 'T12:00:00Z',
                                        'GIT_AUTHOR_DATE': date + 'T12:00:00Z'})


class Stale(StoreCase):

    def sweep(self, days=30):
        snap = self.store.load()
        return stmod.sweep(snap, self.store.touched(snap), TODAY, days, self.repo.root)

    def test_age_uses_the_newest_commit_that_wrote_the_record(self):
        with at('2026-08-01'):
            self.seed(rec('OLD'), rec('MID'), rec('NEW'))
        with at('2026-09-20'):
            self.seed(rec('MID', summary='changed since'))
        with at('2026-10-06'):
            self.seed(rec('NEW', summary='changed lately'))
        rep = self.sweep()
        self.assertEqual(rep.old, [('OLD', 67)])
        self.assertEqual(rep.no_history, [])

    def test_the_boundary_is_at_least_days_and_oldest_comes_first(self):
        with at('2026-09-07'):
            self.seed(rec('EDGE'))
        with at('2026-09-08'):
            self.seed(rec('JUST'))
        with at('2026-08-01'):
            self.seed(rec('OLD'))
        rep = self.sweep(days=30)
        self.assertEqual(rep.old, [('OLD', 67), ('EDGE', 30)])

    def test_body_paths_that_name_no_file(self):
        self.repo.write('tools/real.py', 'x\n')
        body = 'See `tools/real.py`, `tools/gone.py`, `tools/gone.py` and `bare.py`.'
        self.seed(rec('FOO', body=body))
        rep = self.sweep()
        self.assertEqual(rep.bad_paths, [('FOO', 'tools/gone.py')])

    def test_files_changed_gone_and_unchanged(self):
        self.repo.write('a/kept.py', '1\n')
        self.repo.write('a/edited.py', '1\n')
        self.repo.write('a/removed.py', '1\n')
        base = self.repo.commit('base', 'a')
        self.repo.write('a/edited.py', '2\n')
        os.remove(self.repo.path('a', 'removed.py'))
        self.repo.git('add', '-A', 'a')
        self.repo.git('commit', '-q', '-m', 'later')
        self.seed(rec('FOO', files=['a/kept.py', 'a/edited.py', 'a/removed.py'], files_at=base))
        rep = self.sweep()
        self.assertEqual(sorted(rep.moved), [('FOO', 'a/edited.py', stmod.CHANGED),
                                             ('FOO', 'a/removed.py', stmod.GONE)])
        self.assertEqual((rep.files_checked, rep.files_skipped), (3, 0))

    def test_files_without_a_usable_files_at_are_not_checked(self):
        self.seed(rec('NOAT', files='a/x.py'),
                  rec('BAD', files='a/x.py', files_at='0123456789abcdef'))
        rep = self.sweep()
        self.assertEqual(rep.moved, [])
        self.assertEqual((rep.files_checked, rep.files_skipped), (0, 2))
        self.assertEqual(sorted(i for i, _w in rep.unchecked), ['BAD', 'NOAT'])
        text = '\n'.join(rep.lines())
        self.assertIn('NOT CHECKED (2)', text)
        self.assertIn('BLIND report', text)

    def test_a_record_no_commit_wrote_is_listed_not_aged(self):
        snap = self.store.load()
        rep = stmod.sweep(snap, {}, TODAY, 30, self.repo.root)
        self.assertEqual(rep.old, [])
        with at('2026-10-01'):
            self.seed(rec('FOO'))
        snap = self.store.load()
        rep = stmod.sweep(snap, {}, TODAY, 30, self.repo.root)
        self.assertEqual(rep.no_history, ['FOO'])

    def test_the_main_backend_ages_records_by_the_files_commits(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        root = repo.worktree('w')
        backend = smod.MainBackend(root, 'docs/backlog')
        store = smod.Store(backend, rmod.Rules())
        repo.write('docs/backlog/FOO.md', helpers.record_text(id='FOO'), root=root)
        with at('2026-08-01'):
            repo.commit('add FOO', 'docs/backlog/FOO.md', cwd=root)
        snap = store.load()
        got = store.touched(snap)
        self.assertEqual(list(got), ['FOO'])
        rep = stmod.sweep(snap, got, TODAY, 30, root)
        self.assertEqual(rep.old, [('FOO', 67)])

    def test_age_is_counted_in_the_local_day_near_midnight(self):
        """`today` is the local day, so the commit's day must be the local one too: 23:59 on day D
        is 30 days old on D+30, 00:01 on D+1 is 29 (UTC would shift one of them in most zones)."""
        late = datetime.datetime(2026, 9, 6, 23, 59).timestamp()
        early = datetime.datetime(2026, 9, 7, 0, 1).timestamp()
        self.seed(rec('LATE'), rec('EARLY'))
        snap = self.store.load()
        rep = stmod.sweep(snap, {'LATE': late, 'EARLY': early}, '2026-10-06', 30, self.repo.root)
        self.assertEqual(rep.old, [('LATE', 30)])

    def test_a_directory_entry_is_changed_by_a_file_under_it(self):
        self.repo.write('d/sub/one.py', '1\n')
        self.repo.write('e/two.py', '1\n')
        base = self.repo.commit('base', 'd', 'e')
        self.repo.write('d/sub/one.py', '2\n')
        self.repo.commit('later', 'd')
        self.seed(rec('DIR', files=['d', 'e'], files_at=base))
        rep = self.sweep()
        self.assertEqual(rep.moved, [('DIR', 'd', stmod.CHANGED)])

    def test_a_non_ascii_file_name_is_matched(self):
        self.repo.write('d/base.py', '1\n')
        base = self.repo.commit('base', 'd')
        self.repo.write('d/caf\u00e9 \u00fc.py', '1\n')
        self.repo.commit('later', 'd')
        self.seed(rec('UNI', files='d/caf\u00e9 \u00fc.py', files_at=base))
        rep = self.sweep()
        self.assertEqual(rep.moved, [('UNI', 'd/caf\u00e9 \u00fc.py', stmod.CHANGED)])

    def test_one_malformed_files_at_does_not_blind_the_others(self):
        self.repo.write('a/x.py', '1\n')
        base = self.repo.commit('base', 'a')
        self.repo.write('a/x.py', '2\n')
        self.repo.commit('later', 'a')

        class Rec(object):
            body = ''

            def __init__(self, stem, at):
                self.stem, self.at = stem, at

            def get(self, key):
                return self.at if key == 'files_at' else None

            def all(self, key):
                return ['a/x.py'] if key == 'files' else []

        class Snap(object):
            records = readable = [Rec('GOOD', base), Rec('NL', base + '\nx'), Rec('DASH', '--all')]
            unreadable = []

        rep = stmod.sweep(Snap, {}, TODAY, 30, self.repo.root)
        self.assertEqual(rep.moved, [('GOOD', 'a/x.py', stmod.CHANGED)])
        self.assertEqual(sorted(i for i, _w in rep.unchecked), ['DASH', 'NL'])
        self.assertEqual((rep.files_checked, rep.files_skipped), (1, 2))

    def test_several_files_at_commits_share_one_walk(self):
        self.repo.write('a/x.py', '1\n')
        self.repo.write('b/y.py', '1\n')
        c1 = self.repo.commit('one', 'a', 'b')
        self.repo.write('a/x.py', '2\n')
        c2 = self.repo.commit('two', 'a')
        self.repo.write('b/y.py', '2\n')
        self.repo.commit('three', 'b')
        self.seed(rec('R1', files='a/x.py', files_at=c1), rec('R2', files='a/x.py', files_at=c2),
                  rec('R3', files='b/y.py', files_at=c2))
        rep = self.sweep()
        self.assertEqual(sorted(rep.moved), [('R1', 'a/x.py', stmod.CHANGED),
                                             ('R3', 'b/y.py', stmod.CHANGED)])

    def test_an_unreadable_record_is_not_checked(self):
        self.seed(rec('FOO'), {'BAD': 'no header here\n'})
        rep = self.sweep()
        self.assertEqual([i for i, _w in rep.unchecked], ['BAD'])
        self.assertIn('NOT CHECKED (1)', '\n'.join(rep.lines()))

    def test_stale_leaves_the_git_config_alone(self):
        """The first read of a store the remote holds adds its fetch refspec (once); `stale` adds
        no other."""
        self.seed(rec('FOO'))
        self.store.load()
        config = os.path.join(self.repo.root, '.git', 'config')

        def read():
            with open(config, 'rb') as f:
                return f.read()
        before = read()
        self.sweep()
        self.assertEqual(read(), before)


class StaleCli(StoreCase):
    remote = True

    def run_tool(self, *args):
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})
        p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                           capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_stale_reports_and_days_must_be_positive(self):
        self.seed(rec('FOO'))
        rc, out = self.run_tool('stale', '--days', '1')
        self.assertEqual(rc, 0, out)
        self.assertIn('SWEPT: 1 records', out)
        rc, out = self.run_tool('stale', '--days', '0')
        self.assertEqual(rc, 2, out)

    def test_set_body_file_replaces_the_whole_body(self):
        self.seed(rec('FOO', body='old body line'))
        d = tempfile.mkdtemp(prefix='kit-body-')
        self.addCleanup(rmtree, d)
        good, empty = os.path.join(d, 'b.md'), os.path.join(d, 'e.md')
        with open(good, 'w', encoding='utf-8', newline='\n') as f:
            f.write('new body\nsecond line\n')
        open(empty, 'w').close()
        rc, out = self.run_tool('set', 'FOO', '--body-file', good)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool('show', 'FOO')
        self.assertIn('second line', out)
        self.assertNotIn('old body line', out)
        self.assertIn('summary: a thing that is wrong', out)
        rc, out = self.run_tool('set', 'FOO', '--body-file', empty)
        self.assertEqual(rc, 2, out)
