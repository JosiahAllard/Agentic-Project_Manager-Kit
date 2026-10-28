"""`add --found-on BRANCH|none --why-not-folded TEXT` (docs/backlog.md, "The fold-in guard"): a
finding is folded into the branch that found it unless it cannot be; `add` refuses a record whose
scope overlaps that branch's diff or the scope of a record the branch holds pulled, and records
where the finding came from and why it was not folded."""
import json
import os
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import pull_branch
from kitlib.backlog.errors import Failure, Refusal, Usage
from kitlib.git import cmd, refs

from tests.backlog import test_cli, test_verbs
from tests.backlog.helpers import ADD_FLAGS, TODAY, StoreCase, kit_json_ref, priced
from tests.realgit import real_git

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
        self.assertEqual(change.subject, 'backlog: add NEW, found on no branch (or on a branch since landed)')
        new = self.get('NEW')
        self.assertEqual(new.get('summary'), 'a defect found while working')      # the title alone
        self.assertIn('## Found\n\nFound on no branch (or on a branch since landed); not folded: %s\n' % WHY,
                      new.body)

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
        new = self.get('NEW')
        self.assertEqual(new.get('summary'), 'a defect found while working')
        self.assertIn('## Found\n\nFound on kit/foo; not folded: %s\n' % WHY, new.body)
        self.assertIn('## Why this is here', new.body)
        self.assertTrue(any('found on kit/foo' in line for line in change.lines), change.lines)

    def test_a_scope_a_record_the_branch_holds_is_refused(self):
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=FOO, scope='tools/z/**'))
        with self.assertRaises(Refusal) as cm:
            self.add(['tools/z/a.py'])
        self.assertIn('tools/z/a.py vs tools/z/** (scope of BAR, pulled by kit/foo)',
                      str(cm.exception))

    def test_a_differently_cased_branch_still_finds_what_it_holds_pulled(self):
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=FOO, scope='tools/z/**'))
        with self.assertRaises(Refusal) as cm:
            self.add(['tools/z/a.py'], found_on='KIT/FOO')
        self.assertIn('tools/z/a.py vs tools/z/** (scope of BAR, pulled by KIT/FOO)',
                      str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_record_another_branch_holds_does_not_count(self):
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by='kit/other',
                         scope='tools/z/**'))
        self.add(['tools/z/a.py'])
        self.assertIsNotNone(self.get('NEW'))

    def remote_only(self, branch, *paths, remotes=('origin',)):
        """`branch` pushed to each of `remotes` with one commit adding `paths`, and no local
        branch."""
        self.branch_with(branch, *paths)
        for remote in remotes:
            self.repo.git('push', '-q', remote, branch)
        self.repo.git('branch', '-q', '-D', branch)

    def second_remote(self, name='upstream'):
        """A second remote name, which the plan context does not name (`ctx.remote` is origin).
        Its url is the fixture's bare remote: a push to it writes refs/remotes/<name>/ only."""
        self.assertNotEqual(name, self.ctx.remote)
        self.repo.git('remote', 'add', name, self.repo.remote)
        return name

    def test_a_branch_only_a_second_remote_has_is_noted_not_refused(self):
        other = self.second_remote()
        self.remote_only('kit/far', 'kitlib/x/b.py', remotes=(other,))
        self.assertEqual(self.repo.git('rev-parse', '--verify', '--quiet',
                                       'refs/remotes/origin/kit/far', check=False), '')
        change = self.add(['docs/y.md'], found_on='kit/far')
        self.assertIn('NOTE: branch kit/far is not a local branch; its diff was compared as last '
                      'fetched from upstream', change.lines)
        self.assertIsNotNone(self.get('NEW'))

    def test_a_branch_several_remotes_have_names_each_in_the_note(self):
        other = self.second_remote()
        self.remote_only('kit/far', 'kitlib/x/b.py', remotes=('origin', other))
        change = self.add(['docs/y.md'], found_on='kit/far')
        self.assertIn('NOTE: branch kit/far is not a local branch; its diff was compared as last '
                      'fetched from origin, upstream', change.lines)

    def test_a_branch_no_remote_of_several_has_is_refused(self):
        self.second_remote()
        with self.assertRaises(Refusal) as cm:
            self.add(['docs/y.md'], found_on='kit/nowhere')
        self.assertIn('names a branch that exists neither here nor on a remote', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_branch_only_the_remote_has_is_noted_and_its_pulled_scope_still_checked(self):
        gone = 'kit/gone'
        self.remote_only(gone, 'kitlib/x/b.py')
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=gone, scope='tools/z/**'))
        with self.assertRaises(Refusal) as cm:
            self.add(['tools/z/a.py'], found_on=gone)
        self.assertIn('(scope of BAR, pulled by kit/gone)', str(cm.exception))
        change = self.add(['docs/y.md'], found_on=gone)
        self.assertIn('NOTE: branch kit/gone is not a local branch; its diff was compared as last '
                      'fetched from origin', change.lines)

    def test_a_branch_only_a_remote_has_has_its_diff_compared(self):
        other = self.second_remote()
        self.remote_only('kit/far', 'kitlib/x/b.py', remotes=(other,))
        with self.assertRaises(Refusal) as cm:
            self.add(['kitlib/x/**'], found_on='kit/far')
        self.assertIn('kitlib/x/** vs kitlib/x/b.py (changed on upstream/kit/far)',
                      str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_each_remote_tip_of_a_remote_only_branch_is_compared(self):
        self.remote_only('kit/far', 'kitlib/x/b.py')
        other = self.second_remote()
        self.branch_with('kit/far', 'tools/w/c.py')     # another tip, pushed to upstream alone
        self.repo.git('push', '-q', '-f', other, 'kit/far')
        self.repo.git('branch', '-q', '-D', 'kit/far')
        for scope, where in (('kitlib/x/**', 'origin/kit/far'), ('tools/w/**', 'upstream/kit/far')):
            with self.subTest(scope=scope):
                with self.assertRaises(Refusal) as cm:
                    self.add([scope], found_on='kit/far')
                self.assertIn('(changed on %s)' % where, str(cm.exception))
        change = self.add(['docs/y.md'], found_on='kit/far')
        self.assertIn('NOTE: branch kit/far is not a local branch; its diff was compared as last '
                      'fetched from origin, upstream', change.lines)

    def land(self, branch, *paths, subject=None):
        """`branch` merged into main with land.py's subject (`Merge <branch>`, or `subject`),
        main pushed, and the branch deleted: a branch that landed and was removed."""
        self.branch_with(branch, *paths)
        self.repo.git('merge', '-q', '--no-ff', '-m', subject or 'Merge %s' % branch, branch)
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('branch', '-q', '-D', branch)

    def test_a_landed_and_removed_branch_is_accepted_by_its_landing_merge(self):
        for name, subject in (('kit/done', None), ('kit/done2', 'Merge kit/done2: the summary')):
            with self.subTest(name=name):
                self.land(name, 'kitlib/x/%s.py' % name[4:], subject=subject)
                change = self.add(['kitlib/x/**'], found_on=name, rid='NEW' + name[8:])
                self.assertTrue(any(line.startswith('NOTE: branch %s landed on main as ' % name)
                                    for line in change.lines), change.lines)
                self.assertEqual(change.subject, 'backlog: add NEW%s, found on %s'
                                 % (name[8:], name))

    def test_a_landed_branch_name_in_another_case_is_accepted(self):
        self.land('kit/done', 'kitlib/x/done.py')
        change = self.add(['docs/y.md'], found_on='Kit/Done')
        self.assertTrue(any(line.startswith('NOTE: branch Kit/Done landed on main as ')
                            for line in change.lines), change.lines)

    def test_a_merge_subject_that_only_starts_with_the_name_is_no_proof(self):
        self.land('kit/donex', 'kitlib/x/d.py')
        self.branch_with('kit/plain', 'kitlib/x/p.py')
        self.repo.git('merge', '-q', '--ff-only', 'kit/plain')
        self.repo.git('commit', '-q', '--allow-empty', '-m', 'Merge kit/done')   # not a merge
        self.repo.git('push', '-q', 'origin', 'main')
        with self.assertRaises(Refusal) as cm:
            self.add(['docs/y.md'], found_on='kit/done')
        self.assertIn('names a branch that exists neither here nor on a remote', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_merge_off_the_code_mains_first_parent_chain_is_no_proof(self):
        # kit/x is merged into kit/side, which then lands: `Merge kit/x` is reachable from main
        # but was never made on main, so kit/x did not land there.
        self.repo.git('checkout', '-q', '-b', 'kit/side')
        self.branch_with('kit/x', 'kitlib/x/inner.py')
        self.repo.git('checkout', '-q', 'kit/side')
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge kit/x', 'kit/x')
        self.repo.git('checkout', '-q', 'main')
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge kit/side', 'kit/side')
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('branch', '-q', '-D', 'kit/x')
        self.repo.git('branch', '-q', '-D', 'kit/side')
        self.assertIn('Merge kit/x', self.repo.git('log', '--merges', '--format=%s', 'main'))
        with self.assertRaises(Refusal) as cm:
            self.add(['docs/y.md'], found_on='kit/x')
        self.assertIn('names a branch that exists neither here nor on a remote', str(cm.exception))
        self.assertIsNone(self.get('NEW'))
        change = self.add(['docs/y.md'], found_on='kit/side')
        self.assertTrue(any(line.startswith('NOTE: branch kit/side landed on main as ')
                            for line in change.lines), change.lines)

    def test_a_missing_branch_with_a_code_main_that_does_not_resolve_fails(self):
        # Neither origin/trunk nor trunk resolves: whether kit/gone landed cannot be told, so
        # this is ERROR, never the `exists neither here nor on a remote` refusal.
        self.ctx = pmod.Context(self.store, self.repo.root, branch=self.ctx.branch, today=TODAY,
                                code_main='trunk')
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'], found_on='kit/gone')
        self.assertIn('`trunk` does not resolve', str(cm.exception))
        self.assertIn('backlog.code_main', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_ref_list_git_cannot_give_fails(self):
        real = cmd.run_text
        self.failing('cmd', run_text=lambda args, root, **kw: (None, 'git broke')
                     if args[:1] == ['for-each-ref'] else real(args, root, **kw))
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'])
        self.assertIn('cannot list the refs to look for branch kit/foo: git broke',
                      str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_landing_merge_git_cannot_search_fails(self):
        real = cmd.run_text
        self.failing('cmd', run_text=lambda args, root, **kw: (None, 'git broke')
                     if args[:2] == ['log', '--merges'] else real(args, root, **kw))
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'], found_on='kit/nowhere')
        self.assertIn('git broke', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_remotes_with_asks_every_remote(self):
        other = self.second_remote()
        self.remote_only('kit/one', 'kitlib/x/b.py', remotes=(other,))
        self.remote_only('kit/both', 'kitlib/x/c.py', remotes=('origin', other))
        for branch, want in (('kit/one', ['upstream']), ('kit/both', ['origin', 'upstream']),
                             ('KIT/BOTH', ['origin', 'upstream']), ('kit/none', [])):
            with self.subTest(branch=branch):
                got = pmod._remotes_with(self.ctx, branch)
                self.assertEqual([r for r, _ref, _tip in got], want)
                self.assertEqual([ref for _r, ref, _tip in got],
                                 ['refs/remotes/%s/%s' % (r, branch.lower()) for r in want])

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
        for name in ('Main', 'MAIN', 'refs/heads/main', 'origin/main', 'refs/remotes/origin/main',
                     'heads/main', 'Origin/main', 'Refs/Heads/Main'):
            with self.subTest(name=name):
                with self.assertRaises(Usage) as cm:
                    self.add(['docs/y.md'], found_on=name)
                self.assertIn('names the code main', str(cm.exception))
                self.assertIn('write none', str(cm.exception))
        for name in ('refs/heads/kit/foo', 'origin/kit/foo', 'refs/remotes/origin/kit/foo',
                     'remotes/origin/kit/foo', 'heads/kit/foo', 'Origin/kit/foo'):
            with self.subTest(name=name):
                with self.assertRaises(Usage) as cm:
                    self.add(['docs/y.md'], found_on=name)
                self.assertIn('write the local branch name `kit/foo`', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_branch_that_exists_nowhere_is_refused(self):
        before = self.remote_tip()
        with self.assertRaises(Refusal) as cm:
            self.add(['docs/y.md'], found_on='kit/nowhere')
        msg = str(cm.exception)
        self.assertIn('`--found-on kit/nowhere` names a branch that exists neither here nor on a '
                      'remote', msg)
        self.assertIn('pass an existing branch, or `none`', msg)
        self.assertEqual(msg.splitlines()[-1], 'a branch that landed is found by its `Merge '
                         'kit/nowhere` merge on main; one removed without landing is written as '
                         '`none`, with the branch named in --why-not-folded')
        self.assertIsNone(self.get('NEW'))
        self.assertEqual(self.remote_tip(), before)

    def test_a_branch_that_exists_nowhere_is_refused_whatever_it_holds_pulled(self):
        gone = 'kit/gone'
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=gone, scope='tools/z/**'))
        with self.assertRaises(Refusal) as cm:
            self.add(['docs/y.md'], found_on=gone)
        self.assertIn('names a branch that exists neither here nor', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_remote_list_git_cannot_give_fails_and_is_not_a_missing_branch(self):
        self.failing('cmd', run_text=lambda args, root, **kw: (None, 'git broke'))
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'], found_on='kit/nowhere')
        self.assertIn('git broke', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_a_remote_branch_git_cannot_read_fails_and_is_not_a_missing_branch(self):
        real = refs.resolve
        self.failing('refs', resolve=lambda ref, root, **kw: (None, 'git broke')
                     if ref == 'refs/remotes/origin/kit/nowhere' else real(ref, root, **kw))
        with self.assertRaises(Failure) as cm:
            self.add(['docs/y.md'], found_on='kit/nowhere')
        self.assertIn('git broke', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

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


@real_git('packed-refs: the fake has no git pack-refs; a loose ref file matches any letter case on '
          'Windows, a packed-refs line does not')
class FoundOnPackedRefs(StoreCase):
    """Loose and packed refs are found alike, the name compared ignoring case (git's own list of
    ref names, kitlib/backlog/plan.py `_ref_names` and `_spelling`)."""
    branch_with = FoundOn.branch_with
    add = FoundOn.add
    remote_only = FoundOn.remote_only
    second_remote = FoundOn.second_remote

    def setUp(self):
        super().setUp()
        self.branch_with(FOO, 'kitlib/x/a.py')

    def test_a_packed_branch_named_in_another_case_is_found_and_compared(self):
        # Windows: a loose ref file matches any letter case, a packed-refs line does not. Both
        # are found the same way: by git's own list of names, compared ignoring case.
        for packed in (False, True):
            with self.subTest(packed=packed):
                if packed:
                    self.repo.git('pack-refs', '--all')
                    self.assertFalse(os.path.exists(os.path.join(
                        self.repo.root, '.git', 'refs', 'heads', 'kit', 'foo')))
                with self.assertRaises(Refusal) as cm:
                    self.add(['kitlib/x/**'], found_on='KIT/FOO')
                self.assertIn('kitlib/x/** vs kitlib/x/a.py (changed on KIT/FOO)',
                              str(cm.exception))
                change = self.add(['docs/y.md'], found_on='KIT/FOO', rid='NEW%d' % packed)
                self.assertEqual(change.subject, 'backlog: add NEW%d, found on KIT/FOO' % packed)

    def test_a_packed_remote_branch_named_in_another_case_is_found(self):
        self.remote_only('kit/far', 'kitlib/x/b.py')
        self.repo.git('pack-refs', '--all')
        with self.assertRaises(Refusal) as cm:
            self.add(['kitlib/x/**'], found_on='Kit/Far')
        self.assertIn('(changed on origin/kit/far)', str(cm.exception))

    def test_remotes_with_finds_packed_and_loose_refs_in_any_case(self):
        other = self.second_remote()
        self.remote_only('kit/one', 'kitlib/x/b.py', remotes=(other,))
        self.remote_only('kit/both', 'kitlib/x/c.py', remotes=('origin', other))
        self.repo.git('pack-refs', '--all')
        self.remote_only('kit/loose', 'kitlib/x/d.py', remotes=('origin',))
        for branch, want in (('kit/one', ['upstream']), ('kit/both', ['origin', 'upstream']),
                             ('KIT/BOTH', ['origin', 'upstream']), ('kit/loose', ['origin']),
                             ('KIT/LOOSE', ['origin']), ('kit/none', [])):
            with self.subTest(branch=branch):
                self.assertEqual([r for r, _ref, _tip in pmod._remotes_with(self.ctx, branch)],
                                 want)

    def test_two_branches_that_differ_only_in_case_are_usage_however_named(self):
        # rev-parse refs/heads/kit/foo opens the loose Kit/Foo file here, not the packed kit/foo
        self.repo.git('pack-refs', '--all')
        self.repo.git('branch', 'Kit/Foo', 'main')      # loose, beside the packed kit/foo
        names = self.repo.git('for-each-ref', '--format=%(refname)', 'refs/heads/').split()
        self.assertIn('refs/heads/kit/foo', names)
        self.assertIn('refs/heads/Kit/Foo', names)
        for name in ('KIT/FOO', FOO, 'Kit/Foo'):
            with self.subTest(name=name):
                with self.assertRaises(Usage) as cm:
                    self.add(['docs/y.md'], found_on=name)
                self.assertIn('matches refs/heads/Kit/Foo, refs/heads/kit/foo, which differ only '
                              'in letter case', str(cm.exception))
        self.assertIsNone(self.get('NEW'))


class FoundOnNoRemote(StoreCase):
    """A repository with no remote at all: `git remote` lists nothing."""
    remote = False

    def test_a_missing_branch_is_refused_and_no_remote_is_not_a_failure(self):
        self.assertEqual(self.repo.git('remote'), '')
        with self.assertRaises(Refusal) as cm:
            self.verb('add', 'NEW', 'a defect found while working', ['docs/y.md'],
                      found_on='kit/nowhere', why_not_folded=WHY)
        self.assertIn('names a branch that exists neither here nor on a remote', str(cm.exception))
        self.assertIsNone(self.get('NEW'))


# The PM data worktree's own .kit.json, as a pmdata store writes it: its main branch is
# the data branch and its remote the data remote; `code_main` names the code's main (or is unset).
PM_KIT_JSON = {'backlog': {'id_grammar': 'upper', 'code_main': 'main'},
               'repo': {'main_branch': 'pmdata', 'remote': 'private'}}


class PmDataWorktree(StoreCase):
    """The PM data worktree (kitlib.pmdata): a worktree of the code repository on the data branch,
    whose .kit.json names the data remote. The code lands on the main checkout's remote
    (`repo.remote`, origin here), so `add --found-on` measures from that remote's main, not from
    the local main, which lags it until somebody pulls (kitlib/backlog/opener.py `open_store`,
    kitlib/backlog/pull_branch.py `code_config`). Build the branches first, the worktree last:
    `branch_with` adds everything under the main checkout."""
    branch_with = FoundOn.branch_with
    land = FoundOn.land

    def pm_worktree(self, kit_json=PM_KIT_JSON):
        tree = self.repo.git('rev-parse', 'main^{tree}')
        root = self.repo.git('commit-tree', tree, '-m', 'PM data root')    # no history with main
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)
        self.repo.git('branch', 'pmdata', root)
        self.repo.git('remote', 'add', 'private', self.repo.remote)
        path = os.path.join(self.repo.root, '.claude', 'worktrees', 'pm')
        self.repo.git('worktree', 'add', '-q', path, 'pmdata')
        kit_json_ref(self.repo, kit_json, root=path)
        return path

    def lag_main(self, stale):
        """Local main back at `stale`, behind origin/main: a main checkout nobody pulled into."""
        self.repo.git('update-ref', 'refs/heads/main', stale)
        self.repo.git('checkout', '-q', '-f', 'main')
        self.assertNotEqual(self.repo.git('rev-parse', 'main'),
                            self.repo.git('rev-parse', 'refs/remotes/origin/main'))

    def later_on_main(self, path='kitlib/x/later.py'):
        """A commit on origin/main touching `path`; returns the commit before it."""
        stale = self.repo.head()
        self.repo.write(path, 'later\n')
        self.repo.commit('later on main', path)
        self.repo.git('push', '-q', 'origin', 'main')
        return stale

    def plan(self, ctx, scopes, found_on):
        return pmod.plan_add(ctx.store.load(), ctx, 'NEW', 'a defect found while working', scopes,
                             found_on=found_on, why_not_folded=WHY)


class FoundOnInThePmDataWorktree(PmDataWorktree):

    def test_a_branch_cut_from_the_code_remotes_main_is_measured_from_it(self):
        # kit/new is cut after kitlib/x/later.py landed; local main still lacks it. Measured from
        # the local main, later.py would read as kit/new's own work.
        stale = self.later_on_main()
        self.branch_with('kit/new', 'kitlib/y/own.py')
        self.lag_main(stale)
        ctx = opener.open_store(self.pm_worktree(), fetch=False)
        change = self.plan(ctx, ['kitlib/x/later.py'], 'kit/new')
        self.assertIn('ADDED NEW, found on kit/new', change.lines)
        with self.assertRaises(Refusal) as cm:
            self.plan(ctx, ['kitlib/y/**'], 'kit/new')
        self.assertIn('kitlib/y/** vs kitlib/y/own.py (changed on kit/new)', str(cm.exception))

    def test_a_branch_that_landed_on_the_code_remotes_main_is_proof(self):
        stale = self.repo.head()
        self.land('kit/done', 'kitlib/x/done.py')
        self.lag_main(stale)
        ctx = opener.open_store(self.pm_worktree(), fetch=False)
        change = self.plan(ctx, ['docs/y.md'], 'kit/done')
        self.assertTrue(any(line.startswith('NOTE: branch kit/done landed on main as ')
                            for line in change.lines), change.lines)

    def test_an_unset_code_main_is_the_main_checkouts_main(self):
        self.branch_with(FOO, 'kitlib/x/a.py')
        kit_json = {'backlog': {'id_grammar': 'upper'}, 'repo': PM_KIT_JSON['repo']}
        ctx = opener.open_store(self.pm_worktree(kit_json), fetch=False)
        self.assertEqual(ctx.code_main, 'main')
        change = self.plan(ctx, ['docs/y.md'], FOO)
        self.assertIn('ADDED NEW, found on kit/foo', change.lines)

    def test_a_code_main_that_does_not_resolve_still_fails(self):
        kit_json = {'backlog': {'id_grammar': 'upper', 'code_main': 'trunk'},
                    'repo': PM_KIT_JSON['repo']}
        ctx = opener.open_store(self.pm_worktree(kit_json), fetch=False)
        with self.assertRaises(Failure) as cm:
            self.plan(ctx, ['docs/y.md'], 'kit/gone')
        self.assertIn('`trunk` does not resolve', str(cm.exception))
        self.assertIn('backlog.code_main', str(cm.exception))

    def test_outside_the_pm_data_worktree_the_code_remote_is_repo_remote(self):
        self.pm_worktree()
        self.assertIsNone(pull_branch.code_config(self.repo.root))
        ctx = opener.open_store(self.repo.root, fetch=False)
        self.assertEqual((ctx.code_remote, ctx.code_main), (ctx.remote, ctx.main_branch))


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

    def test_add_found_on_a_branch_that_exists_nowhere_is_refused_exit_2(self):
        rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                                '--found-on', 'kit/nowhere', '--why-not-folded', WHY)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^REFUSED: ')
        self.assertIn('kit/nowhere', out)
        self.assertIn('pass an existing branch, or `none`', out)

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
