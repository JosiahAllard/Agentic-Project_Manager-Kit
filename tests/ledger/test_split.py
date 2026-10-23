"""The per-repo ledger (docs/ledger.md "Files"): one file `<ledger_dir>/<repo>.md` per repo, ids
`H-<repo>-nnnn` numbered from 1 in each file, a write lock per file, supersedes within one file and
`under <id>` naming any file. kitlib.ledger.fmt, write, read, standing, files and precommit.

Ids are built by rid(n) and hid(repo, n), so this file holds no ruling id of its own (tests/scrub)."""
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tests.home import machine_config
from kitlib.ledger import files, fmt, precommit, read, standing, write
from kitlib.ledger.errors import Refused, Usage

WHEN = '2026-10-06 08:00Z'
REPOS = ['alpha', 'beta-2', 'pm']


def rid(n):
    return 'R-%04d' % n


def hid(repo, n):
    return 'H-%s-%04d' % (repo, n)


def entry(i, repo, prov='selected', decides='Keep.', source='s', supersedes='-'):
    return fmt.render(i, WHEN, repo, prov, 'q', 'a', decides, source, supersedes)


class Base(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-ledger-split-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.dir = os.path.join(self.base, 'rulings')
        self.paths = {r: os.path.join(self.dir, r + '.md') for r in REPOS}

    def add(self, repo, others=(), **kw):
        args = dict(prov='selected', q='q', a='a', decides='Keep.', source='s', when=WHEN)
        args.update(kw)
        return write.add(self.paths[repo], repo, repos=REPOS, split=True, others=others, **args)

    def text(self, repo):
        with open(self.paths[repo], encoding='utf-8') as f:
            return f.read()


class IdTest(unittest.TestCase):

    def test_an_id_names_its_file_and_number(self):
        self.assertEqual(fmt.fmt_id(3, 'alpha'), hid('alpha', 3))
        self.assertEqual(fmt.fmt_id(3), rid(3))
        self.assertEqual(fmt.num(hid('beta-2', 12)), 12)
        self.assertEqual(fmt.repo_of(hid('beta-2', 12)), 'beta-2')
        self.assertIsNone(fmt.repo_of(rid(5)))

    def test_the_id_patterns_find_both_forms(self):
        text = 'under %s, see %s and %s.' % (hid('alpha', 1), rid(2), hid('beta-2', 30))
        self.assertEqual(fmt.RID_RE.findall(text), [hid('alpha', 1), rid(2), hid('beta-2', 30)])
        self.assertEqual(fmt.UNDER_RE.search(text).group(1), hid('alpha', 1))
        self.assertTrue(fmt.is_id(hid('pm', 7)))
        self.assertTrue(fmt.is_id(rid(7)))
        for bad in ('H-alpha-12', 'H-Alpha-0001', 'H--0001', 'R-12', 'h-alpha-0001'):
            self.assertFalse(fmt.is_id(bad), bad)


class WriterTest(Base):

    def test_each_file_numbers_from_one_with_its_repo(self):
        self.assertEqual(self.add('alpha').id, hid('alpha', 1))
        self.assertEqual(self.add('beta-2').id, hid('beta-2', 1))
        self.assertEqual(self.add('alpha').id, hid('alpha', 2))
        self.assertIn('## %s | %s | alpha | selected' % (hid('alpha', 2), WHEN), self.text('alpha'))
        self.assertNotIn('beta-2', self.text('alpha'))
        self.assertEqual(write.count(self.text('beta-2')), 1)

    def test_all_is_refused_and_names_pm(self):
        with self.assertRaises(Usage) as cm:
            write.add(self.paths['pm'], 'all', 'selected', 'q', 'a', 'd', 's', repos=REPOS, split=True)
        self.assertIn('use pm', str(cm.exception))
        self.assertFalse(os.path.exists(self.paths['pm']))

    def test_a_repo_outside_the_list_is_refused(self):
        with self.assertRaises(Usage):
            write.add(os.path.join(self.dir, 'gamma.md'), 'gamma', 'selected', 'q', 'a', 'd', 's',
                      repos=REPOS, split=True)

    def test_a_repo_that_is_not_the_files_is_refused(self):
        with self.assertRaises(Usage):
            write.add(self.paths['alpha'], 'beta-2', 'selected', 'q', 'a', 'd', 's', repos=REPOS, split=True)

    def test_supersedes_stays_in_one_file(self):
        self.add('alpha')
        self.add('beta-2')
        self.assertEqual(self.add('alpha', supersedes=hid('alpha', 1)).id, hid('alpha', 2))
        with self.assertRaises(Refused) as cm:
            self.add('alpha', supersedes=hid('beta-2', 1))
        self.assertIn('supersedes %s' % hid('beta-2', 1), str(cm.exception))

    def test_under_may_name_an_owner_entry_in_any_file(self):
        self.add('alpha')                                     # an owner entry
        self.add('beta-2', prov='delegated', source='under %s' % hid('alpha', 1),
                 others=[self.text('alpha')])
        frozen = '# Ledger\n\n' + entry(rid(1), 'all')
        self.add('pm', prov='delegated', source='under %s' % rid(1), others=[frozen])
        self.add('alpha', prov='delegated', source='under %s' % hid('alpha', 1))

    def test_under_refuses_a_delegated_or_missing_entry(self):
        self.add('alpha')
        self.add('alpha', prov='delegated', source='under %s' % hid('alpha', 1))
        others = [self.text('alpha')]
        for under in (hid('alpha', 2), hid('alpha', 9), hid('pm', 1)):
            with self.subTest(under=under), self.assertRaises(Refused) as cm:
                self.add('beta-2', prov='delegated', source='under %s' % under, others=others)
            self.assertIn('delegated under %s' % under, str(cm.exception))

    def test_validate_refuses_a_foreign_repo_column_or_an_r_id(self):
        good = '# Ledger\n\n' + entry(hid('alpha', 1), 'alpha')
        self.assertEqual(fmt.validate(good, repo='alpha'), [])
        foreign = '# Ledger\n\n' + entry(hid('alpha', 1), 'beta-2')
        self.assertTrue([e for e in fmt.validate(foreign, repo='alpha') if 'beta-2' in e])
        old = '# Ledger\n\n' + entry(rid(1), 'alpha')
        self.assertTrue([e for e in fmt.validate(old, repo='alpha') if 'expected %s' % hid('alpha', 1) in e])
        gap = good + entry(hid('alpha', 3), 'alpha')
        self.assertTrue([e for e in fmt.validate(gap, repo='alpha') if 'expected %s' % hid('alpha', 2) in e])
        # the one-file ledger refuses an H id: its ids are R-nnnn
        self.assertTrue(fmt.validate(good))

    def test_retire_in_a_file_with_a_successor_in_another(self):
        self.add('alpha')
        self.add('beta-2')
        got = write.retire(self.paths['alpha'], hid('alpha', 1), 'superseded', 'moved',
                           by=hid('beta-2', 1), repos=REPOS, split=True, others=[self.text('beta-2')])
        self.assertEqual(got, hid('alpha', 1))
        e = {x.id: x for x in read.parse(self.text('alpha'))}[hid('alpha', 1)]
        self.assertEqual(e.retired, ('superseded', hid('beta-2', 1)))

    def test_retire_refuses_an_entry_another_file_cites(self):
        self.add('alpha')
        self.add('beta-2', decides='Follow %s.' % hid('alpha', 1))
        with self.assertRaises(Refused) as cm:
            write.retire(self.paths['alpha'], hid('alpha', 1), 'spent', 'done', repos=REPOS,
                         split=True, others=[self.text('beta-2')])
        self.assertIn('still cited', str(cm.exception))

    def test_retire_refuses_an_id_of_another_file(self):
        self.add('alpha')
        with self.assertRaises(Refused):
            write.retire(self.paths['alpha'], hid('beta-2', 1), 'spent', 'x', repos=REPOS, split=True)

    def test_retire_takes_the_new_id_form_for_by_and_ruling(self):
        self.add('alpha')
        self.add('alpha', decides='Retire %s.' % hid('alpha', 1))
        write.retire(self.paths['alpha'], hid('alpha', 1), 'spent', 'done', by=hid('alpha', 2),
                     ruling=hid('alpha', 2), repos=REPOS, split=True)
        with self.assertRaises(Usage):
            write.retire(self.paths['alpha'], hid('alpha', 2), 'spent', 'x', by='H-alpha-2',
                         repos=REPOS, split=True)

    def test_append_only_covers_the_new_ids(self):
        self.add('alpha')
        old = self.text('alpha')
        edited = old.replace('Keep.', 'Changed.')
        self.assertTrue([e for e in write.check(edited, old, REPOS, repo='alpha') if 'was edited' in e])
        self.assertEqual(write.check(old, old, REPOS, repo='alpha'), [])

    def test_the_one_file_ledger_is_unchanged(self):
        path = os.path.join(self.dir, 'LEDGER.md')
        self.assertEqual(write.add(path, 'all', 'selected', 'q', 'a', 'd', 's', when=WHEN).id, rid(1))


class LockPerFileTest(Base):

    def test_each_file_has_its_own_lock(self):
        self.assertNotEqual(write.lock_path(self.paths['alpha']), write.lock_path(self.paths['beta-2']))
        self.assertEqual(os.path.basename(write.lock_path(self.paths['alpha'])), '.alpha.md.lock')

    def test_a_held_file_never_blocks_another(self):
        took, release = threading.Event(), threading.Event()

        def hold():
            with write.locked(self.paths['alpha']):
                took.set()
                release.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join, 30)
        self.addCleanup(release.set)
        self.assertTrue(took.wait(30))
        with mock.patch.object(write, 'LOCK_WAIT', 0.3):
            self.assertEqual(self.add('beta-2').id, hid('beta-2', 1))
            with self.assertRaises(Refused):
                self.add('alpha')


class ReaderTest(Base):

    def test_the_reader_takes_the_new_headers(self):
        self.add('alpha', decides='One.')
        self.add('alpha', decides='Two.')
        write.retire(self.paths['alpha'], hid('alpha', 1), 'superseded', 'x', by=hid('alpha', 2),
                     repos=REPOS, split=True)
        got = {e.id: e for e in read.parse(self.text('alpha'))}
        self.assertEqual(sorted(got), [hid('alpha', 1), hid('alpha', 2)])
        self.assertTrue(got[hid('alpha', 2)].strict)
        self.assertEqual(got[hid('alpha', 2)].repo, 'alpha')
        self.assertEqual(got[hid('alpha', 1)].retired, ('superseded', hid('alpha', 2)))
        found, why = read.read_entries(self.paths['alpha'])
        self.assertIsNone(why)
        self.assertIn(hid('alpha', 2), found)

    def test_the_digest_is_checked_against_every_file(self):
        self.add('alpha')
        self.add('alpha')
        self.add('beta-2')
        write.retire(self.paths['alpha'], hid('alpha', 1), 'spent', 'x', repos=REPOS, split=True)
        digest = ('# Standing\n\n## alpha\n- %s: live\n- %s: retired\n\n## beta-2\n- %s: live\n- %s: none\n'
                  % (hid('alpha', 2), hid('alpha', 1), hid('beta-2', 1), hid('beta-2', 5)))
        errs = standing.problems([self.text('alpha'), self.text('beta-2')], digest)
        self.assertEqual(len(errs), 2, errs)
        self.assertIn('%s, which is retired' % hid('alpha', 1), errs[0])
        self.assertIn('%s, which is not in the ledger' % hid('beta-2', 5), errs[1])
        # one text, as before
        self.assertEqual(standing.problems(self.text('beta-2'), '- %s: x\n' % hid('beta-2', 1)), [])


class FilesTest(Base):

    def config(self, **cfg):
        out = {'ledger_dir': None, 'ledger_repos': None, 'ledger_path': None}
        out.update(cfg)
        return out

    def test_of_config(self):
        frozen = os.path.join(self.dir, 'LEDGER.md')
        f = files.of_config(self.config(ledger_dir=self.dir, ledger_repos=REPOS, ledger_path=frozen))
        self.assertTrue(f.split)
        self.assertEqual(f.repos, self.paths)
        self.assertEqual(f.frozen, frozen)
        self.assertEqual(f.digest, os.path.join(self.dir, standing.NAME))
        self.assertEqual(files.path_of(f, hid('beta-2', 4)), self.paths['beta-2'])
        self.assertEqual(files.path_of(f, rid(4)), frozen)
        self.assertIsNone(files.path_of(f, hid('gamma', 4)))
        self.assertEqual(files.readable(f), [(None, frozen)] + [(r, self.paths[r]) for r in REPOS])

    def test_one_file_config(self):
        path = os.path.join(self.dir, 'LEDGER.md')
        f = files.of_config(self.config(ledger_path=path, ledger_repos=['alpha']))
        self.assertFalse(f.split)
        self.assertEqual(f.repos, {})
        self.assertEqual(files.readable(f), [(None, path)])
        self.assertEqual(files.path_of(f, rid(1)), path)
        self.assertIsNone(files.path_of(f, hid('alpha', 1)))
        self.assertIsNone(files.of_config(self.config()))

    def test_others_reads_every_other_file_that_exists(self):
        self.add('alpha')
        self.add('pm')
        f = files.of_config(self.config(ledger_dir=self.dir, ledger_repos=REPOS))
        texts, whys = files.others(f, self.paths['alpha'])
        self.assertEqual(whys, [])
        self.assertEqual(texts, [self.text('pm')])


class PrecommitTargetsTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dir = os.path.join(self.home, 'rulings')

    def rulebooks(self):
        return tuple(os.path.join(self.dir, 'rules', n + '.md')
                     for n in ('kit', 'retribution', 'griffball', 'pm', 'all'))

    def test_every_file_the_frozen_one_and_the_digest(self):
        frozen = os.path.join(self.dir, 'LEDGER.md')
        machine_config(self.home, {'ledger_dir': self.dir, 'ledger_repos': ['alpha', 'pm'], 'ledger_path': frozen},
                       case=self)
        self.assertEqual(precommit.targets(self.home),
                         (os.path.join(self.dir, 'alpha.md'), os.path.join(self.dir, 'pm.md'), frozen,
                          os.path.join(self.dir, standing.NAME)) + self.rulebooks())

    def test_without_the_frozen_file(self):
        machine_config(self.home, {'ledger_dir': self.dir, 'ledger_repos': ['alpha']}, case=self)
        self.assertEqual(precommit.targets(self.home),
                         (os.path.join(self.dir, 'alpha.md'), os.path.join(self.dir, standing.NAME)) + self.rulebooks())


if __name__ == '__main__':
    unittest.main()
