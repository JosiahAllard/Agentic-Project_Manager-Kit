"""`add --found-on BRANCH|none --why-not-folded TEXT` (docs/backlog.md, "The fold-in guard"): a
finding is folded into the branch that found it unless it cannot be; `add` refuses a record whose
scope overlaps that branch's diff or the scope of a record the branch holds pulled, and records
where the finding came from and why it was not folded."""
import json
import os
import unittest
from unittest import mock

from kitlib.backlog import plan as pmod
from kitlib.backlog.errors import Failure, Refusal, Usage
from kitlib.git import cmd, refs

from tests.backlog import test_cli, test_verbs
from tests.backlog.helpers import ADD_FLAGS, TODAY, StoreCase, priced

FOO = 'kit/foo'
WHY = 'it needs the owner to choose between two formats'


class _Wrap(object):
    """A module stand-in: the functions given override, every other name is the real module's."""

    def __init__(self, real, **over):
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


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
        self.repo.git('add', '-A')      # not by name: `[a]` in a pathspec is a glob
        self.repo.git('commit', '-q', '-m', 'work on %s' % branch)
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

    def test_a_ref_spelling_or_a_case_variant_of_the_code_main_is_usage(self):
        for name in ('Main', 'MAIN', 'refs/heads/main', 'origin/main', 'refs/remotes/origin/main'):
            with self.subTest(name=name):
                with self.assertRaises(Usage) as cm:
                    self.add(['docs/y.md'], found_on=name)
                self.assertIn('names the code main', str(cm.exception))
                self.assertIn('write none', str(cm.exception))
        for name in ('refs/heads/kit/foo', 'origin/kit/foo', 'refs/remotes/origin/kit/foo',
                     'remotes/origin/kit/foo'):
            with self.subTest(name=name):
                with self.assertRaises(Usage) as cm:
                    self.add(['docs/y.md'], found_on=name)
                self.assertIn('write the local branch name `kit/foo`', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_plain_branch_that_does_not_exist_is_a_note(self):
        change = self.add(['docs/y.md'], found_on='kit/nowhere')
        self.assertIn('NOTE: branch kit/nowhere not found; its diff was not compared', change.lines)
        self.assertIsNotNone(self.get('NEW'))

    def test_an_unreadable_record_fails_the_pulled_scope_check(self):
        # Store.write refuses an unreadable store first (store.require_readable); the guard's own
        # check is reached by planning directly.
        self.seed({'BAD': 'no separator here\n'})
        with self.assertRaises(Failure) as cm:
            pmod.plan_add(self.store.load(), self.ctx, 'NEW', 'a defect found while working',
                          ['docs/y.md'], found_on=FOO, why_not_folded=WHY)
        self.assertIn('cannot tell which records kit/foo holds pulled', str(cm.exception))
        self.assertIn('BAD', str(cm.exception))

    def failing(self, module, **over):
        """Patch plan.py's own `module` (refs or cmd) so the named functions answer as `over`
        says; every other call goes to the real module."""
        patch = mock.patch.object(pmod, module, _Wrap(getattr(pmod, module), **over))
        patch.start()
        self.addCleanup(patch.stop)

    def assert_git_failure(self):
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'])
        self.assertIn('git broke', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_branch_git_cannot_read_fails(self):
        real = refs.resolve
        self.failing('refs', resolve=lambda ref, root, **kw: (None, 'git broke')
                     if ref == 'refs/heads/' + FOO else real(ref, root, **kw))
        self.assert_git_failure()

    def test_a_code_main_git_cannot_read_fails(self):
        real = refs.resolve
        base = refs.default_base(self.repo.root, 'origin', 'main')[0]
        self.failing('refs', resolve=lambda ref, root, **kw: (None, 'git broke')
                     if ref == base else real(ref, root, **kw))
        self.assert_git_failure()

    def test_a_merge_base_git_cannot_find_fails(self):
        self.failing('refs', merge_base=lambda a, b, root: (None, 'git broke'))
        self.assert_git_failure()

    def test_a_failed_diff_fails(self):
        real = cmd.run
        self.failing('cmd', run=lambda args, root, **kw: (None, 'git broke')
                     if args[:2] == ['diff', '--no-renames'] else real(args, root, **kw))
        self.assert_git_failure()

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

    def test_the_code_main_comes_from_backlog_code_main(self):
        with open(os.path.join(self.repo.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'backlog': {'code_main': 'trunk'}}, f)
        self.repo.git('add', '.kit.json')
        self.repo.git('commit', '-q', '-m', 'kit config')
        rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                                '--found-on', 'Trunk', '--why-not-folded', WHY)
        self.assertEqual(rc, 2, out)
        self.assertIn('names the code main', out)
        # main is an ordinary branch now, measured from trunk, which does not exist
        rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                                '--found-on', 'main', '--why-not-folded', WHY)
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'^ERROR: ')
        self.assertIn('`trunk`', out)
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
