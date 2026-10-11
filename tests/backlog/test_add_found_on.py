"""`add --found-on BRANCH|none --why-not-folded TEXT` (docs/backlog.md, "The fold-in guard"): a
finding is folded into the branch that found it unless it cannot be; `add` refuses a record whose
scope overlaps that branch's diff or the scope of a record the branch holds pulled, and records
where the finding came from and why it was not folded."""
import os
import unittest

from kitlib.backlog import plan as pmod
from kitlib.backlog.errors import Failure, Refusal, Usage

from tests.backlog import test_cli, test_verbs
from tests.backlog.helpers import ADD_FLAGS, TODAY, StoreCase, priced

FOO = 'kit/foo'
WHY = 'it needs the owner to choose between two formats'


class FoundOn(StoreCase):

    def setUp(self):
        super().setUp()
        self.branch_with(FOO, 'kitlib/x/a.py')

    def branch_with(self, branch, *paths, orphan=False):
        """`branch` with one commit that adds `paths`, cut from main (or an orphan); main stays
        checked out."""
        self.repo.git('checkout', '-q', '--orphan' if orphan else '-b', branch)
        for p in paths:
            self.repo.write(p, 'x\n')
        self.repo.commit('work on %s' % branch, *paths)
        self.repo.git('checkout', '-q', '-f', 'main')

    def add(self, scopes, found_on=FOO, why=WHY, rid='NEW', **kw):
        return self.verb('add', rid, 'a defect found while working', scopes,
                         found_on=found_on, why_not_folded=why, **kw)

    def test_both_flags_are_required(self):
        for kw in ({}, {'found_on': 'none'}, {'why_not_folded': WHY}):
            with self.subTest(kw=kw):
                with self.assertRaises(Usage) as cm:
                    self.verb('add', 'NEW', 'a defect found while working', ['docs/y.md'], **kw)
                self.assertIn('--found-on' if 'found_on' not in kw else '--why-not-folded',
                              str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_none_skips_the_overlap_check_and_records_the_reason(self):
        change = self.add(['kitlib/x/**'], found_on='none')
        self.assertEqual(change.subject, 'backlog: add NEW, found on no branch')
        self.assertTrue(self.get('NEW').get('summary').endswith(
            '(found on no branch; not folded: %s)' % WHY), self.get('NEW').get('summary'))

    def test_a_scope_the_branch_changed_is_refused(self):
        before = self.remote_tip()
        with self.assertRaises(Refusal) as cm:
            self.add(['kitlib/x/**'])
        msg = str(cm.exception)
        self.assertIn('kitlib/x/** vs kitlib/x/a.py (changed on kit/foo)', msg)
        self.assertIn('fold it into kit/foo, or file it after kit/foo lands', msg)
        self.assertNotIn('--found-on none', msg)
        self.assertNotIn('--ruling', msg)
        self.assertEqual(self.remote_tip(), before)

    def test_a_disjoint_scope_is_added_with_where_it_was_found(self):
        change = self.add(['docs/y.md'])
        self.assertEqual(change.subject, 'backlog: add NEW, found on kit/foo')
        summary = self.get('NEW').get('summary')
        self.assertEqual(summary, 'a defect found while working (found on kit/foo; not folded: %s)'
                         % WHY)
        self.assertTrue(any('found on kit/foo' in line for line in change.lines), change.lines)

    def test_a_scope_a_record_the_branch_holds_is_refused(self):
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=FOO, scope='tools/z/**'))
        with self.assertRaises(Refusal) as cm:
            self.add(['tools/z/a.py'])
        self.assertIn('tools/z/a.py vs tools/z/** (scope of BAR, pulled by kit/foo)',
                      str(cm.exception))

    def test_a_record_another_branch_holds_does_not_count(self):
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by='kit/other',
                         scope='tools/z/**'))
        self.add(['tools/z/a.py'])
        self.assertIsNotNone(self.get('NEW'))

    def test_a_missing_branch_is_noted_and_its_pulled_scope_still_checked(self):
        gone = 'kit/gone'
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=gone, scope='tools/z/**'))
        with self.assertRaises(Refusal) as cm:
            self.add(['tools/z/a.py'], found_on=gone)
        self.assertIn('(scope of BAR, pulled by kit/gone)', str(cm.exception))
        change = self.add(['kitlib/x/**'], found_on=gone)
        self.assertIn('NOTE: branch kit/gone not found; its diff was not compared', change.lines)

    def test_no_merge_base_fails_closed_naming_the_key(self):
        self.branch_with('kit/orphan', 'elsewhere.txt', orphan=True)
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'], found_on='kit/orphan')
        self.assertIn('backlog.code_main', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_code_main_that_does_not_resolve_fails_closed_naming_the_key(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=self.ctx.branch, today=TODAY,
                                code_main='trunk')
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'])
        self.assertIn('backlog.code_main', str(cm.exception))

    def test_a_diff_path_that_cannot_be_compared_is_refused(self):
        self.branch_with('kit/odd', 'kitlib/y/[a].py')
        with self.assertRaises(Refusal) as cm:
            self.add(['docs/y.md'], found_on='kit/odd')
        self.assertIn('kitlib/y/[a].py', str(cm.exception))

    def test_a_short_or_machine_path_reason_is_usage(self):
        with self.assertRaises(Usage) as cm:
            self.add(['docs/y.md'], why='too big')
        self.assertIn('--why-not-folded', str(cm.exception))
        for d in test_verbs.MachinePaths.DRIVES:
            with self.assertRaises(Usage) as cm:
                self.add(['docs/y.md'], why='the log under %s says so' % d)
            self.assertIn('machine path', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_the_code_main_and_a_malformed_name_are_usage(self):
        with self.assertRaises(Usage) as cm:
            self.add(['docs/y.md'], found_on='main')
        self.assertIn('write none', str(cm.exception))
        with self.assertRaises(Usage) as cm:
            self.add(['docs/y.md'], found_on='kit/a..b')
        self.assertIn('not a branch name', str(cm.exception))

    def test_a_machine_path_ref_is_usage(self):
        for d in test_verbs.MachinePaths.DRIVES:
            with self.assertRaises(Usage) as cm:
                self.add(['docs/y.md'], found_on='none', ref='%s%sNOTES.md' % (d, os.sep))
            self.assertIn('--ref', str(cm.exception))
            self.assertIn('machine path', str(cm.exception))
        self.assertIsNone(self.get('NEW'))


class FoundOnCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_add_without_the_flags_is_usage(self):
        for extra in ((), ('--found-on', 'none'), ('--why-not-folded', WHY)):
            rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope',
                                    'tools/x.py', *extra)
            self.assertEqual(rc, 2, (extra, out))

    def test_add_found_on_main_is_usage(self):
        rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                                '--found-on', 'main', '--why-not-folded', WHY)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^USAGE: ')
        self.assertIn('write none', out)

    def test_add_with_no_merge_base_is_error_exit_1(self):
        self.repo.git('checkout', '-q', '--orphan', 'kit/orphan')
        self.repo.write('elsewhere.txt', 'x\n')
        self.repo.commit('orphan', 'elsewhere.txt')
        self.repo.git('checkout', '-q', '-f', 'main')
        rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                                '--found-on', 'kit/orphan', '--why-not-folded', WHY)
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'^ERROR: ')
        self.assertIn('backlog.code_main', out)

    def test_add_none_is_ok(self):
        out = self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                      *ADD_FLAGS)
        self.assertIn('ADDED FOO', out)

    def test_add_ref_with_a_machine_path_is_usage(self):
        for d in test_verbs.MachinePaths.DRIVES:
            rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope',
                                    'tools/x.py', '--ref', d, *ADD_FLAGS)
            self.assertEqual(rc, 2, out)
            self.assertRegex(out, r'^USAGE: ')
            self.assertIn('machine path', out)


if __name__ == '__main__':
    unittest.main()
