"""`backlog.py find TEXT` (kitlib/backlog/find.py, store.py `removed_records`): open and ended
records matched in id, summary and body; one git log and one cat-file however many records ended;
no fetch; the cap says how many hits it left out."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.git import cmd
from kitlib.backlog import find as findmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.backlog.helpers import ADD_FLAGS, StoreCase, kit_json_ref, rec
from tests.gitfixture import TempRepo, fixture_env, rmtree
from tests.home import temp_home

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')


class Search(StoreCase):

    def find(self, text, **kw):
        return findmod.search(self.store.load(), self.store, text, **kw)

    def test_an_open_record_is_found_by_id_summary_and_body_in_any_case(self):
        self.seed(rec('FOO-ONE', summary='the Quick fox', body='nothing here'),
                  rec('BAR', summary='plain', body='a hidden Needle in the body'))
        self.assertEqual([l.split()[0] for l in self.find('foo-one')[0]], ['FOO-ONE'])
        self.assertEqual([l.split()[0] for l in self.find('QUICK FOX')[0]], ['FOO-ONE'])
        lines, omitted = self.find('needle')
        self.assertEqual((len(lines), omitted), (1, 0))
        self.assertRegex(lines[0], r'^BAR\s+open\s+plain$')

    def test_a_field_other_than_the_summary_is_not_searched(self):
        self.seed(rec('FOO', area='uniquearea'))
        self.assertEqual(self.find('uniquearea'), ([], 0))

    def test_a_closed_record_is_found_by_its_last_text_with_the_ending_commit(self):
        self.seed(rec('GONE', summary='retired thing', body='the walrus detail'), rec('KEEP'))
        self.verb('close', 'GONE', 'the work is finished')
        tip = self.remote_tip()
        short = self.repo.git('rev-parse', '--short', tip)
        lines, omitted = self.find('WALRUS')
        self.assertEqual(omitted, 0)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^GONE\s+ended\s+retired thing\s+\(closed by %s \d{4}-\d\d-\d\d\)$'
                         % short)

    def test_retired_and_deleted_records_say_how_they_ended(self):
        self.seed(rec('RET', summary='zebra one'), rec('DEL', summary='zebra two'))
        self.verb('retire', 'RET', 'no longer wanted at all')
        self.verb('delete', 'DEL', 'the owner says delete it')
        said = '\n'.join(self.find('zebra')[0])
        self.assertIn('retired by', said)
        self.assertIn('deleted by', said)

    def test_a_record_added_back_is_listed_once_as_open(self):
        self.seed(rec('BACK', summary='phoenix'))
        self.verb('close', 'BACK', 'the work is finished')
        self.seed(rec('BACK', summary='phoenix again'))
        lines, _ = self.find('phoenix')
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^BACK\s+open\s+phoenix again')

    def test_open_records_come_first_then_ended_newest_first(self):
        self.seed(rec('OLD', summary='tag'), rec('NEW', summary='tag'), rec('LIVE', summary='tag'))
        self.verb('close', 'OLD', 'the work is finished')
        self.verb('close', 'NEW', 'the work is finished')
        self.assertEqual([l.split()[0] for l in self.find('tag')[0]], ['LIVE', 'NEW', 'OLD'])

    def test_the_cap_says_how_many_hits_it_left_out(self):
        self.seed(*[rec('R%02d' % i, summary='same words') for i in range(5)])
        lines, omitted = self.find('same words', cap=2)
        self.assertEqual((len(lines), omitted), (2, 3))

    def test_a_store_with_no_commit_matches_nothing(self):
        self.assertEqual(self.find('anything'), ([], 0))

    def test_many_ended_records_cost_one_log_and_one_cat_file_and_no_fetch(self):
        self.seed(*[rec('E%03d' % i, summary='bulk %d' % i) for i in range(40)])
        for i in range(40):
            self.verb('close', 'E%03d' % i, 'the work is finished')
        snap = self.store.load()
        with mock.patch.object(smod.cmd, 'run_text', wraps=smod.cmd.run_text) as log, \
                mock.patch.object(smod.batch, 'cat_file', wraps=smod.batch.cat_file) as cat, \
                mock.patch.object(self.backend, '_fetch', side_effect=AssertionError('fetched')):
            lines, omitted = findmod.search(snap, self.store, 'bulk', cap=100)
        self.assertEqual((len(lines), omitted), (40, 0))
        self.assertEqual((log.call_count, cat.call_count), (1, 1))


class Cli(StoreCase):

    def test_find_prints_hits_and_a_miss_exits_0_too(self):
        kit_json_ref(self.repo)
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})

        def run(*args):
            p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                               capture_output=True, text=True, encoding='utf-8', env=env,
                               timeout=120)
            return p.returncode, p.stdout + p.stderr
        rc, out = run('add', 'FOO', '--summary', 'a lemur thing', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.assertEqual(rc, 0, out)
        rc, out = run('close', 'FOO', '--reason', 'it is all done')
        self.assertEqual(rc, 0, out)
        rc, out = run('find', 'LEMUR')
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'FOO\s+ended\s+a lemur thing\s+\(closed by ')
        self.assertIn('1 hit(s) shown', out)
        rc, out = run('--no-fetch', 'find', 'nothing like this')
        self.assertEqual(rc, 0, out)
        self.assertIn('no record matches "nothing like this"', out)
        rc, out = run('find', '  ')
        self.assertEqual(rc, 2, out)
        self.assertIn('example: backlog.py find', out)



class FolderStore(unittest.TestCase):
    """`find` on a folder (main) store: closed records come from the branch's own history
    (MainBackend.removed), and a repository with no commit yet has none to read."""

    DIR = 'docs/backlog'

    def make(self):
        temp_home(self)
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        return smod.Store(smod.MainBackend(self.repo.root, self.DIR), rmod.Rules())

    def test_a_record_deleted_from_the_folder_is_found_with_its_ending_commit(self):
        store = self.make()
        for rid, summary in (('GONE', 'a narwhal thing'), ('KEEP', 'stays open')):
            self.repo.write('%s/%s.md' % (self.DIR, rid), rec(rid, summary=summary)[rid])
        self.repo.commit('seed the store', self.DIR)
        self.repo.git('rm', '-q', '%s/GONE.md' % self.DIR)
        self.repo.git('commit', '-q', '-m', 'close GONE: all done')
        short = self.repo.git('rev-parse', '--short', 'HEAD')
        lines, omitted = findmod.search(store.load(), store, 'NARWHAL')
        self.assertEqual(omitted, 0)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r'^GONE\s+ended\s+a narwhal thing\s+\(\w+ by %s ' % short)
        self.assertEqual([l.split()[0] for l in findmod.search(store.load(), store, 'stays')[0]],
                         ['KEEP'])

    def test_a_repository_with_no_commit_has_no_match_and_no_error(self):
        temp_home(self)
        root = tempfile.mkdtemp(prefix='kit-empty-')
        self.addCleanup(rmtree, root)
        self.assertIsNotNone(cmd.run_text(['init', '-q'], root)[0])
        store = smod.Store(smod.MainBackend(root, self.DIR), rmod.Rules())
        self.assertEqual(store.removed(store.load()), [])
        self.assertEqual(findmod.search(store.load(), store, 'anything'), ([], 0))


if __name__ == '__main__':
    unittest.main()
