"""kitlib.worktree.remove (tools/worktree_remove.py): the plain verb, --abandon and --check, against
throwaway repositories (tests/gitfixture.py) with their own bare origin. Ported from the PM kit's
worktree_remove cases; docs/worktree-remove.md lists what is not ported.

Every removal here is of a worktree or branch the test itself made in a temp folder.
"""
import datetime
import json
import os
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from kitlib.git import cmd
from kitlib.worktree import holders, remove
from tests import gitfixture
from tests.realgit import real_git

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'worktree_remove.py')
WINDOWS = os.name == 'nt'


class RemoveCase(unittest.TestCase):
    """A throwaway repo with a bare origin, CONFIG committed as `.kit.json` and pushed."""

    CONFIG = None

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.repo = os.path.realpath(self.fx.root)
        self.home = os.path.join(self.repo, '.claude', 'worktrees')
        if self.CONFIG is not None:
            self.configure(self.CONFIG)

    def configure(self, data):
        self.fx.write('.kit.json', json.dumps(data, indent=2) + '\n')
        self.fx.commit('config', '.kit.json')
        self.fx.git('push', '-q', 'origin', 'main')

    def git(self, *args, cwd=None, check=True):
        return self.fx.git(*args, cwd=cwd or self.repo, check=check)

    def workstream(self, slug, commits=1, branch=None):
        """A worktree on kit/<slug> (or `branch`) with `commits` commits of its own."""
        path = os.path.realpath(self.fx.worktree(slug, branch))
        for n in range(commits):
            self.fx.write('%s-%d.txt' % (slug, n), 'work %d\n' % n, root=path)
            self.fx.commit('work %d' % n, '%s-%d.txt' % (slug, n), cwd=path)
        return path

    def orphan(self, slug, commits=1, branch=None):
        """A branch of its own commits whose worktree is already gone (temp repo only)."""
        path = self.workstream(slug, commits=commits, branch=branch)
        self.git('worktree', 'remove', '--force', path)
        return self.git('rev-parse', branch or 'kit/' + slug)

    def land(self, branch):
        """Merge `branch` into main the way landings do (--no-ff) and push it."""
        self.git('merge', '-q', '--no-ff', '-m', 'Merge %s' % branch, branch)
        self.git('push', '-q', 'origin', 'main')

    def run_verb(self, verb, *args, **kw):
        got = getattr(remove, verb)(self.repo, *args, **kw)
        return got.code, '\n'.join(got.lines)

    def branches(self):
        return self.git('branch', '--format=%(refname:short)').split()

    def registered(self):
        return [os.path.normcase(os.path.realpath(line[len('worktree '):]))
                for line in self.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]


class Remove(RemoveCase):

    def test_a_landed_worktree_is_removed_and_its_branch_deleted(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out = self.run_verb('remove', 'done')
        self.assertEqual(rc, 0, out)
        self.assertIn('merged    kit/done is in origin/main', out)
        self.assertIn('removed   %s' % path, out)
        self.assertIn('deleted   branch kit/done', out)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn('kit/done', self.branches())

    def test_keep_branch_keeps_it(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out = self.run_verb('remove', 'done', keep_branch=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('kept      branch kit/done (--keep-branch)', out)
        self.assertFalse(os.path.exists(path))
        self.assertIn('kit/done', self.branches())

    def test_a_path_argument_resolves_against_the_callers_directory(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out = self.run_verb('remove', os.path.join('.claude', 'worktrees', 'done'),
                                cwd=self.repo)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))

    def test_unlanded_work_is_refused_with_the_owners_commands(self):
        path = self.workstream('open', commits=2)
        rc, out = self.run_verb('remove', 'open')
        self.assertEqual(rc, 1, out)
        self.assertIn('holds UNLANDED work: kit/open has 2 commit(s) not in origin/main', out)
        self.assertIn('git branch -D kit/open', out)
        self.assertIn('--abandon "<why>"', out)
        self.assertIn('was read as it stands, not fetched', out)
        self.assertTrue(os.path.isdir(path))
        self.assertIn('kit/open', self.branches())

    def test_landed_locally_but_not_pushed_is_unlanded(self):
        path = self.workstream('local')
        self.git('merge', '-q', '--no-ff', '-m', 'Merge', 'kit/local')
        rc, out = self.run_verb('remove', 'local')
        self.assertEqual(rc, 1, out)
        self.assertIn('UNLANDED', out)
        self.assertTrue(os.path.isdir(path))

    def test_uncommitted_work_refuses(self):
        path = self.workstream('dirty')
        self.land('kit/dirty')
        self.fx.write('new.txt', 'unsaved\n', root=path)
        rc, out = self.run_verb('remove', 'dirty')
        self.assertEqual(rc, 1, out)
        self.assertIn('has uncommitted or untracked files (1): ?? new.txt', out)
        self.assertIn('Nothing was removed.', out)
        self.assertTrue(os.path.isdir(path))

    def test_an_ignored_file_outside_build_output_refuses(self):
        self.fx.write('.gitignore', '*.local.md\nbin/\n')
        self.fx.commit('ignore', '.gitignore')
        self.fx.git('push', '-q', 'origin', 'main')
        path = self.workstream('ign')
        self.land('kit/ign')
        self.fx.write('notes.local.md', 'mine\n', root=path)
        rc, out = self.run_verb('remove', 'ign')
        self.assertEqual(rc, 1, out)
        self.assertIn('ignored files that are not build output (1): !! notes.local.md', out)
        self.assertIn('  rerun    python tools/worktree_remove.py ign', out)
        self.assertTrue(os.path.isfile(os.path.join(path, 'notes.local.md')))

    def test_a_missing_target_is_nothing(self):
        rc, out = self.run_verb('remove', 'ghost')
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   ', out)

    def test_outside_the_worktrees_dir_is_refused(self):
        rc, out = self.run_verb('remove', self.repo + os.sep + 'README.md')
        self.assertEqual(rc, 1, out)
        self.assertIn('is not under', out)

    def test_a_stump_is_named_and_never_deleted(self):
        stump = os.path.join(self.home, 'stump')
        os.makedirs(stump)
        rc, out = self.run_verb('remove', 'stump')
        self.assertEqual(rc, 1, out)
        self.assertIn('leftover  %s is not a registered worktree' % stump, out)
        self.assertTrue(os.path.isdir(stump))

    def test_a_detached_landed_worktree_is_removed_with_no_branch(self):
        path = self.workstream('det')
        self.land('kit/det')
        self.git('checkout', '-q', '--detach', cwd=path)
        rc, out = self.run_verb('remove', 'det')
        self.assertEqual(rc, 0, out)
        self.assertIn('branch    none: it was a detached HEAD', out)
        self.assertIn('kit/det', self.branches())

    def test_branch_d_refusal_keeps_the_branch_and_is_exit_1(self):
        path = self.workstream('behind')
        # Land on the remote only: main's own HEAD stays behind, so `git branch -d` refuses.
        self.git('push', '-q', 'origin', 'kit/behind:main')
        self.git('fetch', '-q', 'origin')
        rc, out = self.run_verb('remove', 'behind')
        self.assertEqual(rc, 1, out)
        self.assertIn('removed   %s' % path, out)
        self.assertIn('failed    git branch -d kit/behind', out)
        self.assertIn('kit/behind', self.branches())


class RemoveSettings(RemoveCase):

    CONFIG = {'worktree_remove': {
        'exempt': {'daily': 'kept by ruling X'},
        'disposable_untracked': ['*.tmp'],
        'build_output': ['bin', 'out/*.o'],
        'keepers': ['*.keep'],
    }}

    def setUp(self):
        super().setUp()
        self.fx.write('.gitignore', 'bin/\n*.o\n*.log\n')
        self.fx.commit('ignore', '.gitignore')
        self.fx.git('push', '-q', 'origin', 'main')

    def test_an_exempt_folder_is_refused(self):
        self.workstream('daily')
        self.land('kit/daily')
        rc, out = self.run_verb('remove', 'daily')
        self.assertEqual(rc, 1, out)
        self.assertIn('is exempt (kept by ruling X)', out)

    def test_disposable_untracked_files_go_after_every_check(self):
        path = self.workstream('tmp')
        self.land('kit/tmp')
        self.fx.write('scratch.tmp', 'x\n', root=path)
        rc, out = self.run_verb('remove', 'tmp')
        self.assertEqual(rc, 0, out)
        self.assertIn('cleared   1 untracked file(s)', out)
        self.assertFalse(os.path.exists(path))

    def test_disposable_files_stay_when_a_check_refuses(self):
        path = self.workstream('tmp2')
        self.fx.write('scratch.tmp', 'x\n', root=path)
        rc, out = self.run_verb('remove', 'tmp2')          # unlanded
        self.assertEqual(rc, 1, out)
        self.assertTrue(os.path.isfile(os.path.join(path, 'scratch.tmp')))

    def test_build_output_goes_with_the_tree(self):
        path = self.workstream('build')
        self.land('kit/build')
        self.fx.write('bin/app.exe', 'binary\n', root=path)
        self.fx.write('out/a.o', 'obj\n', root=path)
        rc, out = self.run_verb('remove', 'build')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))

    def test_a_keeper_inside_build_output_refuses(self):
        path = self.workstream('keeper')
        self.land('kit/keeper')
        self.fx.write('bin/dev.db', 'data\n', root=path)
        self.fx.write('bin/x.keep', 'data\n', root=path)
        rc, out = self.run_verb('remove', 'keeper')
        self.assertEqual(rc, 1, out)
        self.assertIn('bin/dev.db', out)
        self.assertIn('bin/x.keep', out)
        self.assertTrue(os.path.isfile(os.path.join(path, 'bin', 'dev.db')))

    def test_ignored_outside_build_output_globs_refuses(self):
        path = self.workstream('outx')
        self.land('kit/outx')
        self.fx.write('out/run.log', 'mine\n', root=path)
        rc, out = self.run_verb('remove', 'outx')
        self.assertEqual(rc, 1, out)
        self.assertIn('!! out/run.log', out)

    def test_check_skips_exempt_and_lists_stumps_and_landed(self):
        self.workstream('daily')
        landed = self.workstream('landed')
        self.land('kit/landed')
        self.workstream('fresh', commits=0)
        os.makedirs(os.path.join(self.home, 'stump'))
        rc, out = self.run_verb('check')
        self.assertEqual(rc, 1, out)
        self.assertIn('leftover  %s' % os.path.join(self.home, 'stump'), out)
        self.assertIn('merged    %s [kit/landed] landed' % landed, out)
        self.assertNotIn('fresh', out)
        self.assertNotIn('daily', out)


class BranchTemplate(RemoveCase):

    CONFIG = {'worktree_remove': {'branch_template': 'phase/{slug}'}}

    def test_a_worktree_off_its_template_branch_is_refused(self):
        self.workstream('one')
        self.land('kit/one')
        rc, out = self.run_verb('remove', 'one')
        self.assertEqual(rc, 1, out)
        self.assertIn('is on kit/one, not phase/one', out)

    def test_the_template_branch_is_removed(self):
        path = self.workstream('two', branch='phase/two')
        self.land('phase/two')
        rc, out = self.run_verb('remove', 'two')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))


class Check(RemoveCase):

    def test_clean(self):
        self.workstream('fresh', commits=0)
        rc, out = self.run_verb('check')
        self.assertEqual(rc, 0, out)
        self.assertIn('clean     every directory under', out)

    def test_an_orphan_is_named(self):
        path = self.workstream('gone', commits=0)
        gitfixture.rmtree(path)
        rc, out = self.run_verb('check')
        self.assertEqual(rc, 1, out)
        self.assertIn('orphan    ', out)


class Abandon(RemoveCase):

    TODAY = datetime.date(2026, 10, 7)

    def abandon(self, slug, why='superseded', **kw):
        return self.run_verb('abandon', slug, why, today=self.TODAY, **kw)

    def test_unlanded_work_is_tagged_then_removed(self):
        path = self.workstream('drop', commits=2)
        tip = self.git('rev-parse', 'kit/drop')
        rc, out = self.abandon('drop')
        self.assertEqual(rc, 0, out)
        self.assertIn('tagged    archive/drop -> %s' % tip[:10], out)
        self.assertIn('deleted   branch kit/drop (was %s)' % tip[:10], out)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn('kit/drop', self.branches())
        self.assertEqual(self.git('rev-parse', 'archive/drop^{commit}'), tip)
        message = self.git('cat-file', 'tag', 'archive/drop')
        self.assertIn('abandoned: superseded\n\nbranch kit/drop at %s\n' % tip, message)
        self.assertIn('date 2026-10-07', message)

    def test_landed_work_is_refused(self):
        self.workstream('landed')
        self.land('kit/landed')
        rc, out = self.abandon('landed')
        self.assertEqual(rc, 1, out)
        self.assertIn('nothing to abandon', out)
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_dirty_refuses_before_the_tag(self):
        path = self.workstream('dirty')
        self.fx.write('wip.txt', 'x\n', root=path)
        rc, out = self.abandon('dirty')
        self.assertEqual(rc, 1, out)
        self.assertIn('the tag would keep commits only', out)
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_a_tag_on_another_commit_refuses(self):
        self.workstream('clash')
        self.git('tag', 'archive/clash', 'main')
        rc, out = self.abandon('clash')
        self.assertEqual(rc, 1, out)
        self.assertIn('tag archive/clash already exists on', out)
        self.assertIn('kit/clash', self.branches())

    def test_a_tag_on_the_tip_is_reused(self):
        path = self.workstream('again')
        self.git('tag', 'archive/again', 'kit/again')
        rc, out = self.abandon('again')
        self.assertEqual(rc, 0, out)
        self.assertIn('reused    tag archive/again', out)
        self.assertFalse(os.path.exists(path))

    def test_keep_branch(self):
        self.workstream('kb')
        rc, out = self.abandon('kb', keep_branch=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('kept      branch kit/kb (--keep-branch)', out)
        self.assertIn('kit/kb', self.branches())

    def test_a_branch_checked_out_twice_is_refused(self):
        self.workstream('twice')
        other = os.path.join(self.fx.base, 'other')
        self.git('worktree', 'add', '-q', '-f', other, 'kit/twice')
        rc, out = self.abandon('twice')
        self.assertEqual(rc, 1, out)
        self.assertIn('is also checked out at', out)


class NoAncestryAnswer(RemoveCase):
    """kitlib.git.refs.is_ancestor gives no answer (None) for a git ended from outside
    (KIT-TEST-GRANDCHILD-SHIELD), where `merge-base --is-ancestor` gave exit 1, a NO. Every verb
    refuses on no answer and changes nothing: an abandon must not tag and remove work it was told,
    by a killed git, has not landed."""

    WHY = 'git rev-list exited 1 and printed nothing'

    def unknown(self):
        return mock.patch.object(remove.refs, 'is_ancestor', return_value=(None, self.WHY))

    def test_remove_refuses(self):
        path = self.workstream('done')
        self.land('kit/done')
        with self.unknown():
            rc, out = self.run_verb('remove', 'done')
        self.assertEqual(rc, 1, out)
        self.assertIn('the merge check failed (%s); nothing was removed' % self.WHY, out)
        self.assertTrue(os.path.isdir(path))
        self.assertIn('kit/done', self.branches())

    def test_abandon_refuses(self):
        path = self.workstream('drop')
        with self.unknown():
            rc, out = self.run_verb('abandon', 'drop', 'superseded')
        self.assertEqual(rc, 1, out)
        self.assertIn('the merge check failed (%s); nothing was tagged or removed' % self.WHY, out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_abandon_branch_refuses(self):
        self.orphan('gone')
        with self.unknown():
            rc, out = self.run_verb('abandon_branch', 'kit/gone', 'superseded')
        self.assertEqual(rc, 1, out)
        self.assertIn('the merge check failed (%s); nothing was tagged or deleted' % self.WHY, out)
        self.assertIn('kit/gone', self.branches())
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')


class AbandonNextFree(RemoveCase):

    CONFIG = {'worktree_remove': {'archive_next_free': True}}

    def test_the_next_free_tag_is_taken(self):
        self.workstream('nf')
        self.git('tag', 'archive/nf', 'main')
        rc, out = self.run_verb('abandon', 'nf', 'retry')
        self.assertEqual(rc, 0, out)
        self.assertIn('tagged    archive/nf-2 ->', out)


def kill_reads_of(prefix):
    """A patch of cmd._run under which a `rev-parse` whose last argument starts with `prefix`
    exits 1 having printed nothing (a git ended from outside, refs.KILLED_SHAPE); every other git
    runs as before (the fake, or real git)."""
    real = cmd.spawner()

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        if argv[1] == 'rev-parse' and argv[-1].startswith(prefix):
            return 1, b'', b''
        return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
    return mock.patch.object(cmd, '_run', side_effect=run)


class KilledTagAndBranchReads(RemoveCase):
    """KIT-GIT-EXIT1-ANSWERS: tag_tip and branch_tip asked `rev-parse -q --verify`, whose "no such
    ref" is exit 1 with no output, the shape a git ended from outside leaves. A killed read is no
    answer, never a free tag name or a branch that is gone."""

    CONFIG = {'worktree_remove': {'archive_next_free': True}}

    def test_a_killed_tag_read_is_never_a_free_tag_name(self):
        self.workstream('nf')
        self.git('tag', 'archive/nf', 'main')
        with kill_reads_of('refs/tags/archive/nf'):
            rc, out = self.run_verb('abandon', 'nf', 'retry')
        self.assertEqual(rc, 1, out)
        self.assertIn('cannot read tag archive/nf', out)
        self.assertIn('printed nothing', out)
        self.assertEqual(self.git('tag', '-l', 'archive/*').split(), ['archive/nf'])
        self.assertIn('kit/nf', self.branches())

    def test_a_killed_branch_read_is_never_reported_as_no_branch(self):
        tip = self.orphan('kept')
        self.git('tag', 'archive/kept', tip)
        said = []
        ctx = types.SimpleNamespace(main=self.repo, say=lambda *lines: said.extend(lines))
        with kill_reads_of('refs/heads/kit/kept'):
            remove.left_after(ctx, None, 'kit/kept', 'archive/kept')
        out = '\n'.join(said)
        self.assertNotIn('kept at (none)', out, 'a killed git was read as "no branch"')
        self.assertIn('branch kit/kept kept at (unknown: git could not tell', out)
        self.assertIn('tag archive/kept holds %s' % tip[:10], out)


class AbandonPulled(RemoveCase):

    CONFIG = {'backlog': {'store': 'main'}}

    def test_a_record_pulled_by_the_branch_on_main_is_warned(self):
        self.fx.write('docs/backlog/T-001.md', 'id: T-001\nstate: pulled\npulled_by: kit/claim\n')
        self.fx.commit('claim', 'docs/backlog/T-001.md')
        self.fx.git('push', '-q', 'origin', 'main')
        self.workstream('claim')
        rc, out = self.run_verb('abandon', 'claim', 'dead end')
        self.assertEqual(rc, 0, out)
        self.assertIn('warning   backlog record T-001 on origin/main is pulled by kit/claim', out)

    def test_no_record_no_warning(self):
        self.workstream('noclaim')
        rc, out = self.run_verb('abandon', 'noclaim', 'dead end')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('warning', out)
        self.assertNotIn('not checked', out)


@unittest.skipUnless(WINDOWS, 'the holder probes are Windows ones')
class AbandonBranch(RemoveCase):
    """`<branch> --branch --abandon`: an unlanded local branch with no worktree."""

    TODAY = datetime.date(2026, 10, 9)

    def drop(self, branch, why='superseded'):
        return self.run_verb('abandon_branch', branch, why, today=self.TODAY)

    def test_an_unlanded_branch_is_tagged_then_deleted(self):
        tip = self.orphan('gone', commits=2)
        rc, out = self.drop('kit/gone')
        self.assertEqual(rc, 0, out)
        self.assertIn('tagged    archive/kit/gone -> %s' % tip[:10], out)
        self.assertIn('deleted   branch kit/gone (was %s)' % tip[:10], out)
        self.assertNotIn('kit/gone', self.branches())
        self.assertEqual(self.git('rev-parse', 'archive/kit/gone^{commit}'), tip)
        message = self.git('cat-file', 'tag', 'archive/kit/gone')
        self.assertIn('abandoned: superseded' + chr(10) * 2 + 'branch kit/gone at %s' % tip + chr(10) + 'no worktree' + chr(10), message)
        self.assertIn('date 2026-10-09', message)

    def test_the_remote_branch_is_left_alone(self):
        self.orphan('pushed')
        self.git('push', '-q', 'origin', 'kit/pushed')
        rc, out = self.drop('kit/pushed')
        self.assertEqual(rc, 0, out)
        self.assertIn('kit/pushed', self.git('ls-remote', '--heads', 'origin'))

    def test_a_landed_branch_is_refused(self):
        self.orphan('landed')
        self.land('kit/landed')
        rc, out = self.drop('kit/landed')
        self.assertEqual(rc, 1, out)
        self.assertIn('nothing to abandon', out)
        self.assertIn('kit/landed', self.branches())
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_a_branch_a_worktree_has_is_refused(self):
        self.workstream('live')
        rc, out = self.drop('kit/live')
        self.assertEqual(rc, 1, out)
        self.assertIn('is checked out at', out)
        self.assertIn('kit/live', self.branches())
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_the_main_branch_and_a_missing_branch_are_refused(self):
        for name, want in (('main', 'is the main branch'), ('kit/none', 'no local branch')):
            rc, out = self.drop(name)
            self.assertEqual(rc, 1, out)
            self.assertIn(want, out)
        self.assertIn('main', self.branches())

    def test_a_tag_on_another_commit_refuses_before_the_delete(self):
        self.orphan('clash')
        self.git('tag', 'archive/kit/clash', 'main')
        rc, out = self.drop('kit/clash')
        self.assertEqual(rc, 1, out)
        self.assertIn('already exists', out)
        self.assertIn('kit/clash', self.branches())

    def test_a_rerun_reuses_the_tag(self):
        tip = self.orphan('again')
        self.git('tag', 'archive/kit/again', tip)
        rc, out = self.drop('kit/again')
        self.assertEqual(rc, 0, out)
        self.assertIn('reused    tag archive/kit/again', out)
        self.assertNotIn('kit/again', self.branches())

    def test_a_case_variant_of_a_checked_out_branch_is_refused(self):
        """refs/heads/KIT/LIVE opens kit/live's file on a case-insensitive file system."""
        path = self.workstream('live')
        rc, out = self.drop('KIT/LIVE')
        self.assertEqual(rc, 1, out)
        self.assertIn('differ only in case', out)
        self.assertIn('kit/live', self.branches())
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_a_template_branch_is_tagged_by_its_slug(self):
        self.configure({'worktree_remove': {'branch_template': 'phase/{slug}'}})
        self.orphan('two', branch='phase/two')
        rc, out = self.drop('phase/two')
        self.assertEqual(rc, 0, out)
        self.assertIn('tagged    archive/two -> ', out)


@unittest.skipUnless(WINDOWS, 'the holder probes are Windows ones')
@real_git('check-ref-format --branch @{-1}: the fake does not resolve @{-N} (no checkout reflog)')
class AbandonBranchRevisionSyntax(RemoveCase):
    """Moved out of AbandonBranch for the real-git tier."""

    TODAY = AbandonBranch.TODAY
    drop = AbandonBranch.drop

    def test_revision_syntax_is_not_a_branch_name(self):
        self.orphan('open', commits=2)
        for name in ('kit/open~1', 'kit/open^', 'kit/open@{0}', '@{-1}'):
            rc, out = self.drop(name)
            self.assertEqual(rc, 1, out)
            self.assertIn('is not a branch name', out)
        self.assertIn('kit/open', self.branches())
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')


class RemoveBranch(RemoveCase):
    """`<branch> --branch`: a LANDED local branch with no worktree goes with `git branch -d`."""

    def test_a_landed_branch_is_deleted_and_the_remote_left_alone(self):
        self.orphan('done')
        self.git('push', '-q', 'origin', 'kit/done')
        self.land('kit/done')
        rc, out = self.run_verb('remove_branch', 'kit/done')
        self.assertEqual(rc, 0, out)
        self.assertIn('merged    kit/done is in origin/main', out)
        self.assertIn('deleted   branch kit/done (Deleted branch kit/done (was ', out)
        self.assertNotIn('kit/done', self.branches())
        self.assertIn('kit/done', self.git('ls-remote', '--heads', 'origin'))

    def test_unlanded_work_is_refused_with_the_abandon_command(self):
        self.orphan('open', commits=2)
        rc, out = self.run_verb('remove_branch', 'kit/open')
        self.assertEqual(rc, 1, out)
        self.assertIn('refused   branch kit/open holds UNLANDED work: 2 commit(s) not in origin/main',
                      out)
        self.assertIn('kit/open --branch --abandon "<why>"', out)
        self.assertIn('kit/open', self.branches())

    def test_landed_locally_but_not_pushed_is_unlanded(self):
        self.orphan('local')
        self.git('merge', '-q', '--no-ff', '-m', 'Merge', 'kit/local')
        rc, out = self.run_verb('remove_branch', 'kit/local')
        self.assertEqual(rc, 1, out)
        self.assertIn('UNLANDED', out)
        self.assertIn('kit/local', self.branches())

    def test_a_branch_a_worktree_has_is_refused(self):
        path = self.workstream('live')
        self.land('kit/live')
        rc, out = self.run_verb('remove_branch', 'kit/live')
        self.assertEqual(rc, 1, out)
        self.assertIn('refused   branch kit/live is checked out at %s' % os.path.normpath(path), out)
        self.assertIn('kit/live', self.branches())
        self.assertTrue(os.path.isdir(path))

    def test_the_main_branch_is_refused_and_a_missing_branch_is_nothing(self):
        rc, out = self.run_verb('remove_branch', 'main')
        self.assertEqual(rc, 1, out)
        self.assertIn('is the main branch', out)
        rc, out = self.run_verb('remove_branch', 'kit/none')
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   there is no local branch kit/none', out)
        self.assertIn('main', self.branches())

    def test_git_branch_d_refusing_keeps_the_branch(self):
        """origin/main has it, the main checkout's HEAD does not: `-d` refuses, never -D."""
        self.orphan('behind')
        before = self.git('rev-parse', 'main')
        self.land('kit/behind')
        self.git('update-ref', 'refs/heads/main', before)        # temp repo: HEAD behind origin
        rc, out = self.run_verb('remove_branch', 'kit/behind')
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    git branch -d kit/behind exited', out)
        self.assertIn('branch kit/behind is still there; nothing was changed.', out)
        self.assertIn('kit/behind', self.branches())


    def test_a_case_variant_of_main_is_the_main_branch(self):
        main = self.git('rev-parse', 'main')
        for name in ('Main', 'MAIN'):
            rc, out = self.run_verb('remove_branch', name)
            self.assertEqual(rc, 1, out)
            self.assertIn('is the main branch', out)
        self.assertEqual(self.git('rev-parse', '--verify', 'refs/heads/main'), main)
        self.assertIn('main', self.branches())

    def test_a_case_variant_of_a_checked_out_branch_is_refused(self):
        """refs/heads/KIT/LIVE opens kit/live's file on a case-insensitive file system."""
        path = self.workstream('live')
        self.land('kit/live')
        rc, out = self.run_verb('remove_branch', 'KIT/LIVE')
        self.assertEqual(rc, 1, out)
        self.assertIn('differ only in case', out)
        self.assertIn('kit/live', self.branches())
        self.assertTrue(os.path.isdir(path))

    def test_a_case_variant_of_a_landed_branch_is_refused_not_resolved(self):
        self.orphan('done')
        self.land('kit/done')
        rc, out = self.run_verb('remove_branch', 'KIT/Done')
        self.assertEqual(rc, 1, out)
        self.assertIn('differ only in case', out)
        self.assertNotIn('merged ', out)
        self.assertIn('kit/done', self.branches())

    def test_a_merge_check_that_errors_refuses(self):
        self.orphan('done')
        self.land('kit/done')
        # No answer (kitlib.git.refs.is_ancestor: a failed git, or one ended from outside) is no
        # NO and no YES: the verb refuses.
        with mock.patch.object(remove.refs, 'is_ancestor', return_value=(None, 'fatal: boom')):
            rc, out = self.run_verb('remove_branch', 'kit/done')
        self.assertEqual(rc, 1, out)
        self.assertIn('refused   kit/done: the merge check failed (fatal: boom); nothing was'
                      ' deleted', out)
        self.assertIn('kit/done', self.branches())

    def test_an_unreadable_worktree_list_refuses(self):
        self.orphan('done')
        self.land('kit/done')
        with mock.patch.object(remove, 'worktree_listing',
                               side_effect=remove.GitReadError('boom')):
            rc, out = self.run_verb('remove_branch', 'kit/done')
        self.assertEqual(rc, 1, out)
        self.assertIn('refused   kit/done: the worktree list could not be read (boom), so whether a'
                      ' worktree holds it is unknown; nothing was deleted', out)
        self.assertIn('kit/done', self.branches())


@real_git('check-ref-format --branch @{-1}: the fake does not resolve @{-N} (no checkout reflog)')
class RemoveBranchRevisionSyntax(RemoveCase):
    """Moved out of RemoveBranch for the real-git tier."""

    def test_revision_syntax_is_not_a_branch_name(self):
        """kit/open~2 is main's commit (landed); the ref that would be deleted is another name."""
        self.orphan('open', commits=2)
        for name in ('kit/open~2', 'kit/open^^', 'kit/open@{0}', 'main@{0}', '@{-1}'):
            rc, out = self.run_verb('remove_branch', name)
            self.assertEqual(rc, 1, out)
            self.assertIn('is not a branch name', out)
            self.assertNotIn('merged ', out)
        self.assertIn('kit/open', self.branches())


class RemoveBranchPulled(RemoveCase):

    CONFIG = {'backlog': {'store': 'main'}}

    def test_a_record_still_pulled_by_the_branch_refuses(self):
        self.workstream('claim')
        self.git('worktree', 'remove', '--force', os.path.join(self.home, 'claim'))
        self.land('kit/claim')
        self.fx.write('docs/backlog/T-001.md', 'id: T-001\nstate: pulled\npulled_by: kit/claim\n')
        self.fx.commit('claim', 'docs/backlog/T-001.md')
        self.fx.git('push', '-q', 'origin', 'main')
        rc, out = self.run_verb('remove_branch', 'kit/claim')
        self.assertEqual(rc, 1, out)
        self.assertIn('a backlog record is still pulled by kit/claim (an open claim): T-001', out)
        self.assertIn('kit/claim', self.branches())


class RemoveBranchKilledStoreRead(RemoveCase):
    """KIT-GIT-EXIT1-ANSWERS: a git ended from outside exits 1 having printed nothing, the shape
    `rev-parse --verify --quiet` gives for "no such ref". The open-claim check of a ref store must
    refuse on it, never read the store as holding no records."""

    CONFIG = {'backlog': {'store': 'ref'}}

    def test_a_killed_store_ref_read_refuses(self):
        from kitlib.git import cmd
        self.workstream('claim')
        self.git('worktree', 'remove', '--force', os.path.join(self.home, 'claim'))
        self.land('kit/claim')
        real = cmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] in ('rev-parse', 'for-each-ref') and any('refs/backlog/' in a for a in argv):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        with mock.patch.object(cmd, '_run', side_effect=run):
            rc, out = self.run_verb('remove_branch', 'kit/claim')
        self.assertEqual(rc, 1, out)
        self.assertIn('the open-claim check failed', out)
        self.assertIn('kit/claim', self.branches())

    def test_an_absent_store_ref_holds_no_claim(self):
        # The premise of the test above: unkilled, the same branch is removed.
        self.workstream('claim')
        self.git('worktree', 'remove', '--force', os.path.join(self.home, 'claim'))
        self.land('kit/claim')
        rc, out = self.run_verb('remove_branch', 'kit/claim')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('kit/claim', self.branches())


class Holders(RemoveCase):

    def test_a_child_process_standing_in_the_tree_refuses(self):
        path = self.workstream('held')
        self.land('kit/held')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], cwd=path)
        try:
            deadline = time.time() + 10
            while not holders.blockers(path) and time.time() < deadline:
                time.sleep(0.1)
            rc, out = self.run_verb('remove', 'held')
        finally:
            child.kill()
            child.wait()
        self.assertEqual(rc, 1, out)
        self.assertIn('refused   %s is held; git was NOT run' % path, out)
        self.assertIn('holder   pid %d' % child.pid, out)
        self.assertIn(os.path.normcase(path), self.registered())
        self.assertTrue(os.path.isdir(path))

    def test_an_open_file_handle_refuses(self):
        path = self.workstream('handle')
        self.land('kit/handle')
        with open(os.path.join(path, 'handle-0.txt'), encoding='utf-8'):
            rc, out = self.run_verb('remove', 'handle')
        self.assertEqual(rc, 1, out)
        self.assertIn('held open (ERROR_SHARING_VIOLATION)', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_junction_inside_refuses(self):
        import _winapi
        self.fx.write('.gitignore', 'bin/\n')
        self.fx.commit('ignore', '.gitignore')
        self.fx.git('push', '-q', 'origin', 'main')
        self.configure({'worktree_remove': {'build_output': ['bin']}})
        path = self.workstream('link')
        self.land('kit/link')
        target = os.path.join(self.fx.base, 'elsewhere')
        os.makedirs(target)
        with open(os.path.join(target, 'precious.txt'), 'w', encoding='utf-8') as f:
            f.write('outside the tree\n')
        os.makedirs(os.path.join(path, 'bin'))
        _winapi.CreateJunction(target, os.path.join(path, 'bin', 'j'))
        rc, out = self.run_verb('remove', 'link')
        self.assertEqual(rc, 1, out)
        self.assertIn(holders.LINK_ADVICE, out)
        self.assertTrue(os.path.isfile(os.path.join(target, 'precious.txt')))
        os.rmdir(os.path.join(path, 'bin', 'j'))       # the junction itself, not its target


class Globs(unittest.TestCase):

    def test_path_glob_never_crosses_a_slash(self):
        self.assertTrue(holders.path_glob('bin/*').match('bin/a.exe'))
        self.assertFalse(holders.path_glob('bin/*').match('bin/sub/a.exe'))

    def test_is_build_output_full_path_or_final_component(self):
        self.assertTrue(holders.is_build_output('src/bin/', ['bin']))
        self.assertTrue(holders.is_build_output('out/a.o', ['out/*.o']))
        self.assertFalse(holders.is_build_output('out/notes.txt', ['out/*.o']))


class KeepersInBuildOutput(unittest.TestCase):

    def keepers(self, *names):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            for name in names:
                full = os.path.join(root, 'bin', *name.split('/'))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'w', encoding='utf-8') as f:
                    f.write('x\n')
            return holders.keepers_in(root, 'bin/', list(remove.KEEPERS))

    def test_a_framework_dll_with_a_capital_Local_is_not_a_keeper(self):
        self.assertEqual(self.keepers('Debug/net10.0/refs/System.Transactions.Local.dll'), [])

    def test_lowercase_dot_local_files_still_are_keepers(self):
        hits = self.keepers('state.local.md', 'Debug/settings.local.json')
        self.assertEqual(len(hits), 2, hits)

    def test_the_other_keeper_patterns_stay_case_insensitive(self):
        self.assertEqual(len(self.keepers('App.DB', 'Server.PEM')), 2)


class Cli(RemoveCase):

    def cli(self, *args, cwd=None):
        done = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.repo,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env(), timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def test_usage_errors_are_exit_2(self):
        for args in ([], ['a', 'b'], ['--check', '--keep-branch'], ['x', '--abandon'],
                     ['x', '--abandon', '--keep-branch'], ['x', '--abandon', '  '],
                     ['--sweep-merged', 'x'], ['--apply'], ['-C'], ['-C', 'a', '-C', 'b', 'x'],
                     ['--bogus'], ['b', '--branch', '--keep-branch'], ['--check', '--branch'],
                     ['b', '--branch', '--branch'],
                     ['b', '--branch', '--abandon', 'w', '--keep-branch']):
            rc, out = self.cli(*args)
            self.assertEqual(rc, 2, (args, out))
            self.assertIn('USAGE: ', out)

    def test_branch_abandon_through_the_cli(self):
        path = self.workstream('cli')
        self.git('worktree', 'remove', '--force', path)
        rc, out = self.cli('kit/cli', '--branch', '--abandon', 'not needed')
        self.assertEqual(rc, 0, out)
        self.assertIn('deleted   branch kit/cli', out)
        self.assertNotIn('kit/cli', self.branches())

    def test_branch_remove_through_the_cli(self):
        path = self.workstream('clidone')
        self.git('worktree', 'remove', '--force', path)
        self.land('kit/clidone')
        rc, out = self.cli('kit/clidone', '--branch')
        self.assertEqual(rc, 0, out)
        self.assertIn('deleted   branch kit/clidone', out)
        self.assertNotIn('kit/clidone', self.branches())

    def test_a_bad_dash_c_is_usage(self):
        rc, out = self.cli('-C', os.path.join(self.fx.base, 'nowhere'), '--check')
        self.assertEqual(rc, 2, out)

    def test_dash_c_from_elsewhere_removes(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out = self.cli('-C', path, 'done', cwd=self.fx.base)
        self.assertEqual(rc, 0, out)                    # -C names the repository by this tree
        self.assertFalse(os.path.exists(path))

    def test_a_refused_configuration_is_exit_2(self):
        self.configure({'worktree_remove': {'branch_template': 'no-slug'}})
        rc, out = self.cli('--check')
        self.assertEqual(rc, 2, out)
        self.assertIn('worktree_remove: refused: kit configuration:', out)

    def test_check_through_the_cli(self):
        rc, out = self.cli('--check')
        self.assertEqual(rc, 0, out)
        self.assertIn('clean     ', out)


if __name__ == '__main__':
    unittest.main()
