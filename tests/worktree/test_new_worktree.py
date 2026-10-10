"""kitlib.worktree.new (tools/new_worktree.py): its two modes and its refusal to guess between them,
-C, the landing lock and the `.kit.json` keys it reads. Ported from the PM kit's
test_new_worktree.py; the cases that tested retired mechanisms are listed in docs/new-worktree.md
"Not ported".

Every case runs against a throwaway repository (tests/gitfixture.py) with its own bare origin. The
git children run with the fixture's environment (no global or system git config).
"""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from kitlib.config import load
from kitlib.records import landing
from kitlib.worktree import new
from kitlib.git import cmd as gitcmd
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'new_worktree.py')
REFUSAL = new.REFUSAL
LOCK_NAME = 'test-landing.lock'
MASTER_CONFIG = {'repo': {'main_branch': 'master'}, 'landing': {'lock_file': 'git:' + LOCK_NAME}}


def kill_reads_of(spec):
    """A patch of cmd._run under which a `rev-parse` whose last argument is `spec` exits 1 having
    printed nothing (a git ended from outside, kitlib.git.refs.KILLED_SHAPE); every other git runs
    as before (the fake, or real git)."""
    real = gitcmd.spawner()

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        if argv[1] == 'rev-parse' and argv[-1] == spec:
            return 1, b'', b''
        return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
    return mock.patch.object(gitcmd, '_run', side_effect=run)


class WorktreeCase(unittest.TestCase):
    """A throwaway repo: bare origin, a BASE branch equal to it, CONFIG (when set) committed as
    `.kit.json` and pushed."""

    BASE = 'main'
    CONFIG = None

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch=self.BASE, bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.base_dir = os.path.realpath(self.fx.base)
        self.repo = os.path.realpath(self.fx.root)
        self.origin_dir = self.fx.remote
        if self.CONFIG is not None:
            self.configure(self.CONFIG)

    def configure(self, data):
        self.write('.kit.json', json.dumps(data, indent=2) + '\n')
        self.git('add', '.kit.json')
        self.git('commit', '-q', '-m', 'config')
        self.git('push', '-q', 'origin', self.BASE)

    def run_git(self, cwd, *args, env=None):
        done = gitcmd.subprocess_run(('git',) + args, cwd=cwd, capture_output=True, text=True,
                                     encoding='utf-8', errors='replace',
                                     env=gitfixture.fixture_env(env))
        return done.returncode, (done.stdout or '').strip(), (done.stderr or '').strip()

    def git(self, *args):
        return self.run_git(self.repo, *args)

    def write(self, rel, text):
        self.fx.write(rel, text, root=self.repo)

    def tool(self, *args, root=None):
        """The library call the CLI makes: `(code, output)`."""
        existing = '--existing' in args
        rest = [a for a in args if a != '--existing']
        base = None
        if '--base' in rest:
            i = rest.index('--base')
            base = rest[i + 1]
            del rest[i:i + 2]
        self.assertEqual(len(rest), 1, args)
        self.assertTrue(new.branch_ok(rest[0], existing), rest[0])
        got = new.create(root or self.repo, rest[0], existing=existing, base=base)
        return got.code, '\n'.join(got.lines)

    def cli(self, *args, cwd=None):
        done = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.repo,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env(), timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def worktree_paths(self):
        out = self.git('worktree', 'list', '--porcelain')[1]
        found = [l.split(' ', 1)[1] for l in out.splitlines() if l.startswith('worktree ')]
        return [p for p in found if os.path.normcase(os.path.normpath(p))
                != os.path.normcase(os.path.normpath(self.repo))]

    def make_branch_with_work(self, branch='fix/resumed', filename='precious.txt'):
        """A branch carrying a commit that is on no other ref, checked out nowhere."""
        scratch = os.path.join(self.base_dir, 'seed')
        self.git('worktree', 'add', '-q', '-b', branch, scratch, self.BASE)
        with open(os.path.join(scratch, filename), 'w', encoding='utf-8', newline='\n') as h:
            h.write('work that must not be orphaned\n')
        self.run_git(scratch, 'add', '-A')
        self.run_git(scratch, 'commit', '-q', '-m', 'precious work')
        tip = self.git('rev-parse', branch)[1]
        self.git('worktree', 'remove', scratch)
        return tip

    def slug_path(self, slug, worktrees='.claude/worktrees'):
        return os.path.join(self.repo, *(worktrees.split('/') + [slug]))

    def printed(self, out, label):
        return [l.split(None, 1)[1].strip() for l in out.splitlines() if l.startswith(label + ' ')]


class NewBranchMode(WorktreeCase):

    def test_the_printed_path_has_one_kind_of_separator(self):
        out = self.tool('fix/fresh')[1]
        printed = self.printed(out, 'worktree')
        self.assertTrue(printed, out)
        for path in printed:
            self.assertEqual(path, os.path.normpath(path))

    def test_it_creates_the_worktree_and_a_new_branch_off_the_main_branch(self):
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 0, out)
        path = self.slug_path('fresh')
        self.assertTrue(os.path.isdir(path), out)
        self.assertEqual(self.run_git(path, 'rev-parse', '--abbrev-ref', 'HEAD')[1], 'fix/fresh')
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], self.git('rev-parse', self.BASE)[1])
        self.assertIn('from %s' % self.BASE, out)

    def test_the_new_branch_tracks_nothing(self):
        self.assertEqual(self.tool('fix/fresh')[0], 0)
        self.assertNotEqual(self.git('config', '--get', 'branch.fix/fresh.merge')[0], 0)

    def test_it_refuses_when_main_differs_from_origin(self):
        self.write('b.txt', 'local only\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'unpushed')
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 1, out)
        self.assertIn('tell the coordinator', out)
        self.assertFalse(os.path.exists(self.slug_path('fresh')))

    def test_a_killed_read_of_the_base_is_refused_as_unread_not_unresolved(self):
        """KIT-GIT-EXIT1-ANSWERS-2: `rev-parse --verify --quiet` said "no such ref" as exit 1 with
        no output, which a git ended from outside also leaves."""
        with kill_reads_of('%s^{commit}' % self.BASE):
            rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('did not resolve', out)
        self.assertIn('refused   %s could not be read (' % self.BASE, out)
        self.assertIn('printed nothing', out)
        self.assertIn('nothing was created', out)
        self.assertFalse(os.path.exists(self.slug_path('fresh')))
        self.assertFalse(new.branch_exists(self.repo, 'fix/fresh'))

    def test_another_worktree_of_the_same_repository_may_call_it(self):
        rc, out = self.tool('fix/first')
        self.assertEqual(rc, 0, out)
        rc, out = self.tool('fix/second', root=self.slug_path('first'))
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.slug_path('second')))

    def test_next_names_the_new_worktrees_preflight(self):
        out = self.tool('fix/fresh')[1]
        want = os.path.join(self.slug_path('fresh'), 'tools', 'preflight.py')
        self.assertIn('next      python "%s"' % want, out)


class BaseOption(WorktreeCase):
    """`--base <branch>`: another base than the main branch, held to the same rule."""

    def make_dev(self):
        self.git('branch', 'dev')
        self.git('switch', '-q', 'dev')
        self.write('d.txt', 'dev\n')
        self.git('add', 'd.txt')
        self.git('commit', '-q', '-m', 'dev work')
        self.git('switch', '-q', self.BASE)

    def test_a_new_branch_starts_at_the_named_base(self):
        self.make_dev()
        self.git('push', '-q', 'origin', 'dev')
        rc, out = self.tool('fix/on-dev', '--base', 'dev')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/on-dev')[1], self.git('rev-parse', 'dev')[1])
        self.assertIn('from dev @', out)

    def test_the_named_base_must_equal_its_remote_branch(self):
        self.make_dev()
        rc, out = self.tool('fix/on-dev', '--base', 'dev')
        self.assertEqual(rc, 1, out)
        self.assertIn('dev ', out)
        self.assertIn('origin/dev', out)
        self.assertIn(REFUSAL, out)
        self.assertFalse(os.path.exists(self.slug_path('on-dev')), out)


class AJunctionAliasIsResolved(WorktreeCase):
    """Through a junction alias of the repository, the new worktree is registered under the one
    real path."""

    def setUp(self):
        super().setUp()
        self.alias = os.path.join(self.base_dir, 'alias')
        try:
            import _winapi
            _winapi.CreateJunction(self.repo, self.alias)
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('cannot create a junction here: %s' % e)
        self.addCleanup(lambda: os.path.isdir(self.alias) and os.rmdir(self.alias))

    def test_main_checkout_of_resolves_the_alias(self):
        got = new.main_checkout_of(os.path.join(self.alias, '.git').replace('\\', '/'))
        self.assertEqual(os.path.normcase(got), os.path.normcase(os.path.realpath(self.repo)))

    def test_run_through_the_alias_it_prints_and_registers_the_real_path(self):
        rc, out = self.tool('fix/aliased', root=self.alias)
        self.assertEqual(rc, 0, out)
        want = os.path.normcase(os.path.realpath(self.slug_path('aliased')))
        self.assertEqual([os.path.normcase(p) for p in self.printed(out, 'worktree')], [want], out)
        listed = [os.path.normcase(os.path.normpath(p)) for p in self.worktree_paths()]
        self.assertIn(want, listed)


class ExistingBranchModeResumesTheRealWork(WorktreeCase):

    def test_the_worktree_holds_the_branch_tip_not_a_fresh_branch_off_the_base(self):
        tip = self.make_branch_with_work()
        rc, out = self.tool('--existing', 'fix/resumed')
        self.assertEqual(rc, 0, out)
        path = self.slug_path('resumed')
        self.assertEqual(self.run_git(path, 'rev-parse', 'HEAD')[1], tip, out)
        self.assertEqual(self.run_git(path, 'rev-parse', '--abbrev-ref', 'HEAD')[1], 'fix/resumed')
        self.assertNotEqual(tip, self.git('rev-parse', self.BASE)[1],
                            'the fixture must differ from the base or this proves nothing')
        self.assertIn('resumed (0 commit(s) of %s not in it)' % self.BASE, out)

    def test_the_branchs_own_file_is_in_the_tree(self):
        self.make_branch_with_work()
        rc, out = self.tool('--existing', 'fix/resumed')
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.slug_path('resumed'), 'precious.txt')))

    def test_it_does_not_move_the_branch(self):
        tip = self.make_branch_with_work()
        self.tool('--existing', 'fix/resumed')
        self.assertEqual(self.git('rev-parse', 'fix/resumed')[1], tip)


class ACallerWrongAboutTheBranchIsTold(WorktreeCase):
    """Neither mode is inferred from the repository, so a typo cannot be accommodated."""

    def test_the_default_refuses_an_existing_branch_and_names_the_flag(self):
        tip = self.make_branch_with_work()
        rc, out = self.tool('fix/resumed')
        self.assertEqual(rc, 1, out)
        self.assertIn('--existing', out)
        self.assertFalse(os.path.exists(self.slug_path('resumed')), out)
        self.assertEqual(self.git('rev-parse', 'fix/resumed')[1], tip)

    def test_existing_refuses_a_branch_that_is_not_there(self):
        rc, out = self.tool('--existing', 'fix/never-was')
        self.assertEqual(rc, 1, out)
        self.assertIn('no such branch', out)
        self.assertFalse(os.path.exists(self.slug_path('never-was')), out)
        self.assertEqual(self.git('rev-parse', '--verify', '--quiet', 'refs/heads/fix/never-was')[0], 1)

    def test_a_refused_default_leaves_no_worktree_registered(self):
        self.make_branch_with_work()
        self.tool('fix/resumed')
        self.assertEqual(self.worktree_paths(), [])

    def test_an_existing_directory_is_never_reused(self):
        os.makedirs(self.slug_path('taken'))
        rc, out = self.tool('fix/taken')
        self.assertEqual(rc, 1, out)
        self.assertIn('exists already', out)
        self.assertIn('tools/worktree_remove.py --check', out)


class UppercaseRecordIdBranches(WorktreeCase):
    """`--existing` resumes `codex/<RECORD-ID>` branches; a new branch stays lowercase; case
    twins (folder or branch) are refused, never resolved."""

    BRANCH = 'codex/TOOLS-RECORD-ID'

    def test_existing_resumes_an_uppercase_record_id_branch(self):
        tip = self.make_branch_with_work(self.BRANCH)
        rc, out = self.tool('--existing', self.BRANCH)
        self.assertEqual(rc, 0, out)
        path = self.slug_path('TOOLS-RECORD-ID')
        self.assertEqual(self.run_git(path, 'rev-parse', 'HEAD')[1], tip, out)
        self.assertEqual(self.run_git(path, 'rev-parse', '--abbrev-ref', 'HEAD')[1], self.BRANCH)
        self.assertEqual([os.path.basename(p) for p in self.worktree_paths()], ['TOOLS-RECORD-ID'])

    def test_a_new_branch_still_has_to_be_lowercase_first(self):
        self.assertFalse(new.branch_ok('codex/TOOLS-NEW-ID', existing=False))
        rc, out = self.cli('codex/TOOLS-NEW-ID')
        self.assertEqual(rc, 2, out)
        self.assertIn('usage:', out)
        self.assertFalse(new.branch_exists(self.repo, 'codex/TOOLS-NEW-ID'), out)

    def test_existing_in_the_wrong_case_is_refused_and_names_the_real_branch(self):
        self.make_branch_with_work(self.BRANCH)
        rc, out = self.tool('--existing', 'codex/tools-record-id')
        self.assertEqual(rc, 1, out)
        self.assertIn('differs only in case', out)
        self.assertIn(self.BRANCH, out)
        self.assertEqual(self.worktree_paths(), [])

    def test_a_new_branch_that_is_a_case_twin_of_an_existing_one_is_refused(self):
        self.make_branch_with_work('codex/Twin')
        rc, out = self.tool('codex/twin')
        self.assertEqual(rc, 1, out)
        self.assertIn('differs only in case', out)
        self.assertIn('codex/Twin', out)
        self.assertEqual(self.worktree_paths(), [])

    def test_a_folder_that_differs_only_in_case_is_refused_on_any_filesystem(self):
        os.makedirs(self.slug_path('tools-record-id'))
        self.make_branch_with_work(self.BRANCH)
        rc, out = self.tool('--existing', self.BRANCH)
        self.assertEqual(rc, 1, out)
        self.assertIn('differs only in case', out)
        self.assertEqual(self.worktree_paths(), [])

    def test_branch_exists_is_case_exact(self):
        self.make_branch_with_work(self.BRANCH)
        self.assertTrue(new.branch_exists(self.repo, self.BRANCH))
        self.assertFalse(new.branch_exists(self.repo, 'codex/tools-record-id'))


class BranchExistsProbeIsExact(WorktreeCase):
    """`branch_exists` answers about local branches only - a remote-tracking ref is not one."""

    def test_it_sees_a_local_branch(self):
        self.make_branch_with_work()
        self.assertTrue(new.branch_exists(self.repo, 'fix/resumed'))

    def test_it_does_not_see_an_absent_branch(self):
        self.assertFalse(new.branch_exists(self.repo, 'fix/never-was'))

    def test_a_remote_tracking_ref_alone_is_not_a_local_branch(self):
        self.assertEqual(self.git('rev-parse', '--verify', '--quiet',
                                  'refs/remotes/origin/%s' % self.BASE)[0], 0)
        self.assertFalse(new.branch_exists(self.repo, 'origin/' + self.BASE))


class MasterRepo(WorktreeCase):
    """A repository's values: main branch `master`, the landing lock named in the git common dir."""

    BASE = 'master'
    CONFIG = MASTER_CONFIG


class MasterRepoNewBranch(MasterRepo):
    def test_it_creates_the_branch_off_master_and_says_so(self):
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], self.git('rev-parse', 'master')[1])
        self.assertIn('from master @', out)


class MinusCNamesTheRepository(MasterRepo):
    """`-C <work tree>`: one plain command from any directory, acting on the MAIN checkout of the
    repository that work tree belongs to; a bad -C is a usage error, never a refusal."""

    def test_minus_c_at_the_main_checkout_from_outside_it(self):
        rc, out = self.cli('-C', self.repo, 'fix/from-outside', cwd=self.base_dir)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.slug_path('from-outside')), out)

    def test_without_minus_c_the_current_directory_names_the_repository(self):
        rc, out = self.cli('fix/here', cwd=self.repo)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.slug_path('here')), out)

    def test_minus_c_at_a_linked_worktree_acts_on_the_main_checkout(self):
        self.assertEqual(self.tool('fix/first')[0], 0)
        rc, out = self.cli('fix/second', '-C', self.slug_path('first'), cwd=self.base_dir)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.slug_path('second')), out)
        self.assertFalse(os.path.exists(os.path.join(self.slug_path('first'), '.claude')), out)
        self.assertEqual(self.git('rev-parse', 'fix/second')[1], self.git('rev-parse', 'master')[1])

    def test_the_origin_guard_still_refuses_under_minus_c(self):
        self.assertEqual(self.tool('fix/first')[0], 0)
        self.write('b.txt', 'unpushed\n')
        self.git('add', 'b.txt')
        self.git('commit', '-q', '-m', 'unpushed on master')
        rc, out = self.cli('-C', self.slug_path('first'), 'fix/second', cwd=self.base_dir)
        self.assertEqual(rc, 1, out)
        self.assertIn(REFUSAL, out)
        self.assertFalse(os.path.exists(self.slug_path('second')), out)

    def test_a_missing_directory_is_a_usage_error(self):
        rc, out = self.cli('-C', os.path.join(self.base_dir, 'nope'), 'fix/x', cwd=self.base_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn('usage:', out)
        self.assertIn('USAGE:', out)
        self.assertNotIn('refused', out.lower())
        self.assertFalse(os.path.exists(self.slug_path('x')))

    def test_a_directory_in_no_repository_is_a_usage_error(self):
        rc, out = self.cli('-C', self.base_dir, 'fix/x', cwd=self.base_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE:', out)
        self.assertNotIn('refused', out.lower())

    def test_minus_c_with_no_path_is_a_usage_error(self):
        rc, out = self.cli('fix/x', '-C', cwd=self.base_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn('usage:', out)


class LandingCase(MasterRepo):
    """Shapes of the main checkout's master against origin/master, with the kit landing lock
    held or free. Nothing pushes after setUp unless it says so."""

    def lock(self):
        return landing.LandingLock.for_config(self.repo, load(self.repo))

    def lock_path(self):
        return self.lock().path

    def hold(self, text=None):
        """Take the lock as a lander would (a token holder), or write `text` in its place."""
        if text is None:
            return self.lock().take('full', ['feat/landing'])
        with open(self.lock_path(), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def land(self, name, subject=None, date=None):
        """One lander merge on the base, unpushed: `git merge --no-ff` of a one-commit branch."""
        self.git('switch', '-q', '-c', 'feat/' + name)
        self.write(name + '.txt', name + '\n')
        self.git('add', name + '.txt')
        self.git('commit', '-q', '-m', name)
        self.git('switch', '-q', self.BASE)
        env = {'GIT_COMMITTER_DATE': date} if date else None
        rc, _o, err = self.run_git(self.repo, 'merge', '-q', '--no-ff', '-m',
                                   subject or 'Merge feat/%s: work' % name, 'feat/' + name, env=env)
        self.assertEqual(rc, 0, err)
        return self.git('rev-parse', self.BASE)[1]

    def plain(self, name):
        self.write(name + '.txt', name + '\n')
        self.git('add', name + '.txt')
        self.git('commit', '-q', '-m', 'plain ' + name)
        return self.git('rev-parse', self.BASE)[1]

    def origin(self):
        return self.git('rev-parse', 'origin/' + self.BASE)[1]

    def assert_refused(self, rc, out, why, slug='fresh', prefix='fix/'):
        """The refusal as the FIRST line, then the reason line; nothing created."""
        self.assertEqual(rc, 1, out)
        lines = out.splitlines()
        self.assertTrue(lines and lines[0].endswith(REFUSAL), out)
        self.assertTrue(lines[0].startswith(self.BASE + ' '), out)
        self.assertEqual(len(lines), 2, out)
        self.assertTrue(lines[1].startswith('  not a landing in progress: '), out)
        self.assertIn(why, lines[1])
        self.assertNotIn('landing   in progress', out)
        self.assertFalse(os.path.exists(self.slug_path(slug)), out)
        if prefix:
            self.assertFalse(new.branch_exists(self.repo, prefix + slug), out)

    def assert_based_on_origin(self, rc, out, pushed):
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], pushed)
        self.assertIn('landing   in progress: ', out)
        self.assertIn('branch    fix/fresh from pushed origin/master @ %s' % pushed[:7], out)


class ALandingInProgressBasesOnOrigin(LandingCase):

    def test_one_landing_merge_the_branch_starts_at_pushed_origin_master(self):
        pushed = self.origin()
        merge = self.land('one')
        self.hold()
        rc, out = self.tool('fix/fresh')
        self.assert_based_on_origin(rc, out, pushed)
        self.assertNotEqual(self.git('merge-base', '--is-ancestor', merge, 'fix/fresh')[0], 0)
        self.assertIn("based on pushed origin/master %s and does NOT contain master's unpushed "
                      'commit(s) %s' % (pushed[:7], merge[:7]), out)

    def test_the_landing_line_names_the_lock_and_its_holder(self):
        self.land('one')
        record = self.hold()
        out = self.tool('fix/fresh')[1]
        self.assertIn('landing   in progress: the landing lock %s is held: %s'
                      % (self.lock_path(), landing.describe(record)), out)
        self.assertIn('feat/landing', out)

    def test_the_new_branch_tracks_nothing(self):
        self.land('one')
        self.hold()
        self.assertEqual(self.tool('fix/fresh')[0], 0)
        self.assertNotEqual(self.git('config', '--get', 'branch.fix/fresh.merge')[0], 0)

    def test_master_and_the_lock_are_not_touched(self):
        merge = self.land('one')
        self.hold()
        with open(self.lock_path(), encoding='utf-8') as fh:
            before = fh.read()
        self.assertEqual(self.tool('fix/fresh')[0], 0)
        self.assertEqual(self.git('rev-parse', 'master')[1], merge)
        with open(self.lock_path(), encoding='utf-8') as fh:
            self.assertEqual(fh.read(), before)

    def test_any_shape_under_a_held_lock_bases_on_origin(self):
        """No shape rule: a plain commit, many merges, a merge not named `Merge `."""
        pushed = self.origin()
        shas = [self.plain('b')] + [self.land(n) for n in ('one', 'two', 'three', 'four')]
        shas.append(self.land('five', subject='wip: merged by hand'))
        self.hold()
        rc, out = self.tool('fix/fresh')
        self.assert_based_on_origin(rc, out, pushed)
        for sha in shas:
            self.assertIn(sha[:7], out)

    def test_no_age_rule(self):
        """A landing merge from a day ago is still a landing while the lock is held."""
        pushed = self.origin()
        self.land('one', date='%d +0000' % (int(time.time()) - 86400))
        self.hold()
        self.assert_based_on_origin(*self.tool('fix/fresh'), pushed=pushed)

    def test_master_equal_to_origin_under_a_held_lock(self):
        pushed = self.origin()
        self.hold()
        rc, out = self.tool('fix/fresh')
        self.assert_based_on_origin(rc, out, pushed)
        self.assertIn('unpushed commit(s) (none)', out)

    def test_an_empty_lock_file_is_held(self):
        pushed = self.origin()
        self.land('one')
        self.hold('')
        rc, out = self.tool('fix/fresh')
        self.assert_based_on_origin(rc, out, pushed)
        self.assertIn('treated as held', out)

    def test_an_unreadable_lock_is_held(self):
        """A lock path that cannot be opened (here a directory) is held, by nobody we can name."""
        pushed = self.origin()
        self.land('one')
        os.makedirs(self.lock_path())
        rc, out = self.tool('fix/fresh')
        self.assert_based_on_origin(rc, out, pushed)
        self.assertIn('cannot be read (', out)
        self.assertIn('treated as held', out)

    def test_existing_resumes_with_the_note(self):
        tip = self.make_branch_with_work()
        merge = self.land('one')
        self.hold()
        rc, out = self.tool('--existing', 'fix/resumed')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/resumed')[1], tip)
        self.assertIn('landing   in progress', out)
        self.assertIn("unpushed  master's commit(s) %s, not counted below" % merge[:7], out)
        self.assertIn('of pushed origin/master not in it', out)

    def test_held_and_a_killed_read_of_origin_is_refused_as_unread(self):
        self.land('one')
        self.hold()
        with kill_reads_of('refs/remotes/origin/master^{commit}'):
            rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('did not resolve', out)
        self.assertIn('refused   refs/remotes/origin/master could not be read (', out)
        self.assertFalse(os.path.exists(self.slug_path('fresh')), out)
        self.assertFalse(new.branch_exists(self.repo, 'fix/fresh'), out)

    def test_held_but_origin_does_not_resolve_refuses(self):
        """The one refusal under a held lock: no pushed commit to base on."""
        self.run_git(self.origin_dir, 'config', 'receive.denyDeleteCurrent', 'ignore')
        self.assertEqual(self.git('push', '-q', 'origin', '--delete', 'master')[0], 0)
        self.git('update-ref', '-d', 'refs/remotes/origin/master')
        self.hold()
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 1, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('landing   in progress: '), out)
        self.assertTrue(lines[1].startswith('refused   the landing lock is held, but origin/master '
                                            'did not resolve'), out)
        self.assertNotIn('not a landing in progress', out)
        self.assertFalse(os.path.exists(self.slug_path('fresh')), out)
        self.assertFalse(new.branch_exists(self.repo, 'fix/fresh'), out)


class NoLandingStillRefuses(LandingCase):
    """The lock free: every difference refuses with the plain message and creates nothing."""

    FREE = 'nobody holds the landing lock'

    def test_an_unpushed_plain_commit(self):
        self.plain('b')
        self.assert_refused(*self.tool('fix/fresh'), why=self.FREE)

    def test_a_landing_shaped_merge_without_the_lock(self):
        self.land('one')
        self.assert_refused(*self.tool('fix/fresh'), why=self.FREE)

    def test_the_reason_names_the_lock_file(self):
        self.land('one')
        out = self.tool('fix/fresh')[1]
        self.assertIn('(%s), so the difference is no landing' % self.lock_path(), out)

    def test_master_diverged_from_origin_master(self):
        self.plain('b')
        self.git('push', '-q', 'origin', 'master')
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.land('one')
        self.assert_refused(*self.tool('fix/fresh'), why=self.FREE)

    def test_existing_refuses_too(self):
        self.make_branch_with_work()
        self.land('one')
        self.assert_refused(*self.tool('--existing', 'fix/resumed'), why=self.FREE,
                            slug='resumed', prefix=None)

    def test_a_released_lock_left_behind_no_trace(self):
        record = self.hold()
        self.lock().release(record['token'])
        self.land('one')
        self.assert_refused(*self.tool('fix/fresh'), why=self.FREE)


class LocalMainBehindOrigin(LandingCase):
    """The lock free and local main a pure fast-forward behind origin: the branch starts at origin
    and one line says so; local main is not updated. Ahead, diverged and equal are the cases
    above and in NoLandingStillRefuses."""

    def push_ahead(self, n):
        """origin/master `n` plain commits ahead of local master (local reset back, not updated)."""
        for i in range(n):
            self.plain('p%d' % i)
        self.git('push', '-q', 'origin', 'master')
        pushed = self.git('rev-parse', 'master')[1]
        self.git('reset', '-q', '--hard', 'HEAD~%d' % n)
        return pushed

    def test_it_bases_on_origin_and_says_how_far_behind(self):
        pushed = self.push_ahead(2)
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], pushed)
        self.assertIn('base      local master is 2 commit(s) behind origin/master; this branch '
                      'starts at origin/master %s and local master is not updated' % pushed[:7], out)
        self.assertIn('branch    fix/fresh from pushed origin/master @ %s (not master: it is '
                      'behind)' % pushed[:7], out)
        self.assertNotIn('landing', out)

    def test_local_main_is_not_updated(self):
        self.push_ahead(1)
        before = self.git('rev-parse', 'master')[1]
        self.assertEqual(self.tool('fix/fresh')[0], 0)
        self.assertEqual(self.git('rev-parse', 'master')[1], before)
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', 'HEAD')[1], 'master')

    def test_the_new_branch_tracks_nothing(self):
        self.push_ahead(1)
        self.assertEqual(self.tool('fix/fresh')[0], 0)
        self.assertNotEqual(self.git('config', '--get', 'branch.fix/fresh.merge')[0], 0)

    def test_existing_resumes_with_origin_as_the_base(self):
        self.make_branch_with_work()
        pushed = self.push_ahead(1)
        rc, out = self.tool('--existing', 'fix/resumed')
        self.assertEqual(rc, 0, out)
        self.assertIn('is 1 commit(s) behind origin/master', out)
        self.assertIn('1 commit(s) of pushed origin/master not in it', out)
        self.assertNotEqual(self.git('rev-parse', 'fix/resumed')[1], pushed)

    def test_a_held_lock_keeps_the_landing_wording(self):
        self.push_ahead(1)
        self.hold()
        out = self.tool('fix/fresh')[1]
        self.assertIn('landing   in progress', out)
        self.assertNotIn('commit(s) behind', out)

    def test_ahead_of_origin_still_refuses(self):
        self.plain('a')
        self.assert_refused(*self.tool('fix/fresh'), why=self.FREE)

    def test_behind_and_ahead_at_once_is_diverged_and_refuses(self):
        self.push_ahead(1)
        self.plain('mine')
        self.assert_refused(*self.tool('fix/fresh'), why=self.FREE)

    def test_equal_prints_no_behind_line(self):
        out = self.tool('fix/fresh')[1]
        self.assertNotIn('behind', out)

    FREE = NoLandingStillRefuses.FREE

    def test_base_origin_master_compares_origin_to_itself(self):
        rc, out = self.tool('fix/fresh', '--base', 'origin/master')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], self.origin())
        self.assertIn('branch    fix/fresh from master @', out)
        self.assertNotIn('origin/origin', out)

    def test_base_origin_master_through_the_cli(self):
        rc, out = self.cli('fix/fresh', '--base', 'origin/master')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('origin/origin', out)

    def test_base_origin_master_while_behind(self):
        pushed = self.push_ahead(1)
        rc, out = self.tool('fix/fresh', '--base', 'origin/master')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], pushed)
        self.assertIn('is 1 commit(s) behind origin/master', out)


class TheDefaultLock(LandingCase):
    """No "landing".lock_file: the kit's own lock file (kitlib.records.landing.LOCK_NAME) is read.
    (The old tool read no lock at all then; the kit always has one.)"""

    BASE = 'main'
    CONFIG = None

    def test_the_default_lock_is_read(self):
        self.assertEqual(os.path.basename(self.lock_path()), landing.LOCK_NAME)
        pushed = self.origin()
        self.land('one')
        self.hold()
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1], pushed)
        self.assertIn('from pushed origin/main @ %s' % pushed[:7], out)

    def test_free_it_refuses(self):
        self.land('one')
        self.assert_refused(*self.tool('fix/fresh'), why='nobody holds the landing lock')


class BehindCountOnAKilledGit(unittest.TestCase):
    def test_a_killed_ancestor_check_is_not_behind(self):
        from kitlib.git import cmd
        for killed in (cmd.Result(None, b'', 'git rev-list timed out after 30 seconds'),
                       cmd.Result(1, b'', '')):     # ended from outside: exit 1, nothing printed
            with mock.patch.object(cmd, 'call', return_value=killed):
                self.assertIsNone(new.behind_count('.', 'aaa', 'bbb'), killed)

    def test_equal_or_missing_refs_are_not_behind(self):
        self.assertIsNone(new.behind_count('.', 'aaa', 'aaa'))
        self.assertIsNone(new.behind_count('.', '', 'bbb'))


class ChooseBase(unittest.TestCase):
    """`choose_base` alone: the held/free decision and the one re-read."""

    def never(self):
        raise AssertionError('re-read under a held lock')

    def test_held_starts_at_the_pushed_commit(self):
        self.assertEqual(new.choose_base('m1', 'o1', True, self.never), ('o1', 'm1', 'o1', None))

    def test_held_without_a_pushed_commit_refuses(self):
        self.assertEqual(new.choose_base('m1', '', True, self.never),
                         (None, 'm1', '', new.HELD_NO_REMOTE))

    def test_free_and_equal_reads_once(self):
        self.assertEqual(new.choose_base('a', 'a', False, self.never), ('a', 'a', 'a', None))

    def test_free_and_different_rereads_once_and_takes_the_new_equal_refs(self):
        calls = []

        def reread():
            calls.append(1)
            return 'b', 'b'
        self.assertEqual(new.choose_base('a', 'o', False, reread), ('b', 'b', 'b', None))
        self.assertEqual(len(calls), 1)

    def test_free_and_still_different_refuses_with_the_free_reason(self):
        start, m, om, why = new.choose_base('a', 'o', False, lambda: ('a2', 'o'),
                                            free_why='nobody holds it')
        self.assertEqual((start, m, om), (None, 'a2', 'o'))
        self.assertEqual(why, 'nobody holds it, so the difference is no landing')

    def test_free_and_unresolved_refuses(self):
        why = new.choose_base('', 'o', False, lambda: ('', 'o'))[3]
        self.assertIn('did not resolve', why)


class ConfigKeys(WorktreeCase):
    """The `.kit.json` keys it reads: "repo".remote / worktrees_dir / tools_dir / main_branch and
    "landing".lock_file. "new_worktree" takes no key. A refused configuration exits 2, nothing made."""

    def assert_config_refused(self, data, why):
        self.configure(data)
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 2, out)
        self.assertIn('new_worktree: refused: kit configuration', out)
        self.assertIn(why, out)
        self.assertFalse(os.path.exists(self.slug_path('fresh')), out)

    def test_the_dropped_landing_stale_minutes_refuses(self):
        self.assert_config_refused({'new_worktree': {'landing_stale_minutes': 90}},
                                   'landing_stale_minutes')

    def test_a_lock_file_outside_the_git_dir_refuses(self):
        self.assert_config_refused({'landing': {'lock_file': 'git:../escape.lock'}},
                                   'must name one file in the git common dir')

    def test_a_lock_file_that_is_not_in_the_git_dir_refuses(self):
        self.assert_config_refused({'landing': {'lock_file': 'landing.lock'}}, 'must be git:<name>')

    def test_an_unknown_key_refuses(self):
        self.assert_config_refused({'new_worktree': {'landing_minutes': 90}}, 'landing_minutes')

    def test_the_remote_defaults_to_origin(self):
        self.git('remote', 'rename', 'origin', 'upstream')
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 1, out)
        self.assertIn('fetch failed', out)

    def test_the_remote_is_the_configured_one(self):
        self.configure({'repo': {'remote': 'upstream'}})
        self.git('remote', 'rename', 'origin', 'upstream')
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git('rev-parse', 'fix/fresh')[1],
                         self.git('rev-parse', 'upstream/main')[1])
        self.assertIn('from main @', out)

    def test_the_remote_guard_names_the_configured_remote(self):
        self.configure({'repo': {'remote': 'upstream'}})
        self.git('remote', 'rename', 'origin', 'upstream')
        self.write('b.txt', 'local only\n')
        self.git('add', 'b.txt')
        self.git('commit', '-q', '-m', 'unpushed')
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 1, out)
        self.assertIn('!= upstream/main ', out)

    def test_the_worktrees_dir_is_the_configured_one(self):
        self.configure({'repo': {'worktrees_dir': 'wt/trees'}})
        rc, out = self.tool('fix/fresh')
        self.assertEqual(rc, 0, out)
        want = self.slug_path('fresh', worktrees='wt/trees')
        self.assertTrue(os.path.isdir(want), out)
        self.assertEqual(self.printed(out, 'worktree'), [want], out)
        self.assertFalse(os.path.exists(self.slug_path('fresh')), out)

    def test_the_tools_dir_spells_the_commands(self):
        self.configure({'repo': {'tools_dir': 'devtools'}})
        out = self.tool('fix/fresh')[1]
        self.assertIn(os.path.join(self.slug_path('fresh'), 'devtools', 'preflight.py'), out)
        os.makedirs(self.slug_path('taken'))
        out = self.tool('fix/taken')[1]
        self.assertIn('python devtools/worktree_remove.py --check', out)


class UsageIsRejectedNotGuessed(unittest.TestCase):
    """Argument shapes that are not a branch name exit 2 with USAGE, before any git call."""

    def tool(self, *args):
        done = subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=gitfixture.fixture_env(),
                              timeout=120)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def assert_usage(self, *args):
        rc, out = self.tool(*args)
        self.assertEqual(rc, 2, (args, out))
        self.assertIn('USAGE:', out)
        self.assertNotIn('refused', out.lower())
        return out

    def test_no_argument(self):
        self.assert_usage()

    def test_existing_with_no_branch(self):
        self.assert_usage('--existing')

    def test_a_bare_slug_with_no_area(self):
        self.assert_usage('--existing', 'resumed')

    def test_base_with_no_branch(self):
        self.assert_usage('fix/x', '--base')
        self.assert_usage('fix/x', '--base', '')

    def test_existing_still_refuses_unsafe_slugs(self):
        """Uppercase is the only widening: no leading dot or dash, no `..` walk, no third part."""
        for bad in ('codex/.X', 'codex/..', 'codex/A/B', 'codex/A\\B', 'Codex/X', 'codex/A:B',
                    'codex/'):
            self.assert_usage('--existing', bad)
            self.assertFalse(new.branch_ok(bad, existing=True), bad)
        self.assertFalse(new.branch_ok('codex/-X', existing=True))

    def test_existing_refuses_a_non_ascii_letter_past_the_first(self):
        for bad in ('codex/aé', 'codex/Aé', 'codé/x'):
            self.assertIsNone(new.EXISTING_BRANCH.match(bad), bad)

    def test_a_new_branch_refuses_a_non_ascii_letter_past_the_first(self):
        for bad in ('codex/aé', 'codé/x'):
            self.assertIsNone(new.NEW_BRANCH.match(bad), bad)

    def test_the_usage_text_documents_both_modes(self):
        out = self.assert_usage()
        self.assertIn('--existing', out)
        self.assertIn('--base', out)
        self.assertIn('-C', out)


if __name__ == '__main__':
    unittest.main()
