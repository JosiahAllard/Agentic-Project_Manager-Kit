"""kitlib.syncbase and tools/sync_base.py on real throwaway repositories (tests/gitfixture.py).

Ported from the PM kit's test_sync_base.py. The fixture is a MAIN checkout parked on its own
branch, a LINKED worktree every case works in, and a real bare remote the base is pushed to:
sync_base fetches <remote> <base> itself and merges only a hash on the fetched branch (P27).
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import syncbase
from kitlib.config import load as load_config
from kitlib.git import cmd as gitcmd
from kitlib.records import landing
from tests.gitfixture import NAME, EMAIL, TempRepo, fixture_env
from tests.realgit import real_git

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(KIT, 'tools', 'sync_base.py')


class SyncCase(unittest.TestCase):
    """The throwaway repository; no tests of its own. BASE / REMOTE / CONFIG set the shape."""

    MODEL = 'Test Model'
    BASE = 'master'
    REMOTE = 'origin'
    CONFIG = {'repo': {'main_branch': 'master'}}

    def setUp(self):
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(syncbase.MODEL_ENV, None)
        base, config, remote = self.BASE, self.CONFIG, self.REMOTE

        def prepare(r):             # built once per process per case shape; each test gets a copy
            r.write('docs/rows.md', 'A | one\nB | two\n')
            paths = ['docs/rows.md']
            if config is not None:
                r.write('.kit.json', json.dumps(config))
                paths.append('.kit.json')
            r.commit('base', *paths)
            r.git('push', '-q', 'origin', base)
            if remote != 'origin':
                r.git('remote', 'add', remote, r.remote)
            r.git('switch', '-q', '-c', 'parking')
        self.r = TempRepo(branch=self.BASE, bare_remote=True, prepare=prepare,
                          prepare_key=('test_sync.SyncCase', remote, json.dumps(config)))
        self.addCleanup(self.r.cleanup)
        self.main = self.r.root
        self.repo = os.path.join(self.r.base, 'linked')
        self.r.git('worktree', 'add', '-q', self.repo, self.BASE)
        self.elsewhere = os.path.join(self.r.base, 'elsewhere')
        os.makedirs(self.elsewhere)

    # -- git helpers ------------------------------------------------------------------------
    def git(self, *args):
        return self.r.git(*args, cwd=self.repo)

    def rc(self, *args):
        done = gitcmd.subprocess_run(('git',) + args, cwd=self.repo, capture_output=True,
                                     text=True, env=fixture_env())
        return done.returncode

    def write(self, rel, text):
        self.r.write(rel, text, root=self.repo)

    def commit_all(self, message):
        self.git('commit', '-q', '-a', '-m', message)

    def add_commit(self, rel, text, message):
        self.write(rel, text)
        self.git('add', rel)
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def push_base(self):
        self.git('push', '-q', 'origin', self.BASE)

    def head(self):
        return self.git('rev-parse', 'HEAD')

    def merged(self, target):
        return self.rc('merge-base', '--is-ancestor', target, 'HEAD') == 0

    def trailers_of_head(self):
        return self.git('log', '-1', '--format=%h|%s|%(trailers:key=Co-Authored-By,valueonly)')

    def git_path(self, name):
        return self.git('rev-parse', '--path-format=absolute', '--git-path', name)

    # -- staging ----------------------------------------------------------------------------
    def stage_a_conflict(self, push=True):
        """Both branches rewrite the same row, so the merge cannot pick one."""
        self.git('switch', '-q', '-c', 'work')
        self.write('docs/rows.md', 'A | one, branch wording\nB | two\n')
        self.commit_all('branch edit')
        self.git('switch', '-q', self.BASE)
        self.write('docs/rows.md', 'A | one, base wording\nB | two\n')
        self.commit_all('base edit')
        target = self.head()
        if push:
            self.push_base()
        self.git('switch', '-q', 'work')
        return target

    def stage_clean(self, push=True):
        """A branch `work` behind a base commit that merges cleanly; returns that commit."""
        self.git('switch', '-q', '-c', 'work')
        self.add_commit('docs/branch-only.md', 'branch\n', 'branch edit')
        self.git('switch', '-q', self.BASE)
        target = self.add_commit('docs/base-only.md', 'base\n', 'base edit')
        if push:
            self.push_base()
        self.git('switch', '-q', 'work')
        return target

    # -- running ----------------------------------------------------------------------------
    def sync(self, target, path=None, base=None, model=None):
        return syncbase.sync(path or self.repo, target, model or self.MODEL, '--model', base)

    def out(self, outcome):
        return '\n'.join(outcome.lines)

    def cli(self, *argv, cwd=None, env_model=None, env=None):
        """The entry point out of process; CLAUDE_AGENT_MODEL only when `env_model` sets it."""
        child = env or fixture_env()
        child.pop(syncbase.MODEL_ENV, None)
        if env_model is not None:
            child[syncbase.MODEL_ENV] = env_model
        child['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run([sys.executable, SCRIPT] + list(argv), cwd=cwd or self.repo,
                              env=child, capture_output=True, text=True)


class ConflictedMergeKeepsTheTrailer(SyncCase):
    """The trailer was once written on the clean path and silently not on the conflicted one.
    Driven end to end, not asserted on a string."""

    def test_a_hand_resolved_conflict_commits_with_the_trailer(self):
        done = self.sync(self.stage_a_conflict())
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('CONFLICTS', self.out(done))
        self.write('docs/rows.md', 'A | one, base wording\nB | two\n')
        self.git('add', 'docs/rows.md')
        self.git('commit', '-q', '--no-edit')
        self.assertIn('Claude %s <noreply@anthropic.com>' % self.MODEL, self.trailers_of_head())

    def test_the_prepared_message_carries_it_before_the_commit_is_made(self):
        self.sync(self.stage_a_conflict())
        with open(self.git_path('MERGE_MSG'), encoding='utf-8') as handle:
            prepared = handle.read()
        self.assertIn('Co-Authored-By: Claude %s <noreply@anthropic.com>' % self.MODEL, prepared)
        self.assertTrue(prepared.rstrip().endswith('<noreply@anthropic.com>'), prepared)

    def test_a_trailer_already_in_the_last_paragraph_is_not_written_twice(self):
        self.sync(self.stage_a_conflict())
        path = self.git_path('MERGE_MSG')
        with open(path, encoding='utf-8') as handle:
            once = handle.read()
        self.assertTrue(syncbase.arm_merge_msg(self.repo, self.MODEL))
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(handle.read(), once, 'arming twice must be a no-op')

    def test_arming_without_a_merge_in_progress_reports_failure(self):
        self.git('switch', '-q', '-c', 'work')
        self.assertFalse(syncbase.arm_merge_msg(self.repo, self.MODEL))

    def test_nothing_is_armed_once_the_merge_has_been_committed(self):
        self.sync(self.stage_a_conflict())
        self.write('docs/rows.md', 'A | one, base wording\nB | two\n')
        self.git('add', 'docs/rows.md')
        self.git('commit', '-q', '--no-edit')
        self.assertFalse(os.path.isfile(self.git_path('MERGE_HEAD')))
        self.assertFalse(os.path.isfile(self.git_path('MERGE_MSG')))
        self.assertFalse(syncbase.arm_merge_msg(self.repo, self.MODEL))

    def test_the_conflict_report_says_the_trailer_is_already_there(self):
        self.assertIn('already in the prepared message',
                      self.out(self.sync(self.stage_a_conflict())))

    def test_a_clean_merge_still_carries_it(self):
        done = self.sync(self.stage_clean())
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertIn('Claude %s <noreply@anthropic.com>' % self.MODEL, self.trailers_of_head())

    def test_the_merge_itself_is_unchanged_by_the_message_work(self):
        self.sync(self.stage_a_conflict())
        self.assertEqual(self.rc('rev-parse', '--verify', '--quiet', 'MERGE_HEAD'), 0)
        self.assertEqual(self.git('diff', '--name-only', '--diff-filter=U'), 'docs/rows.md')
        with open(os.path.join(self.repo, 'docs', 'rows.md'), encoding='utf-8') as handle:
            self.assertIn('<<<<<<<', handle.read())


@real_git("merge-ort's directory rename detection")
class DirectoryRenames(SyncCase):
    """Moved out of ConflictedMergeKeepsTheTrailer for the real-git tier."""

    def test_a_file_added_under_a_folder_the_base_moved_lands_in_the_new_folder(self):
        """With merge.directoryRenames=true the file follows the folder."""
        for name in ('a', 'b', 'c'):
            self.write('old/%s.txt' % name, 'file %s\n' % name)
        self.git('add', 'old')
        self.git('commit', '-q', '-m', 'old folder')
        self.git('switch', '-q', '-c', 'work')
        self.add_commit('old/new.txt', 'added on the branch\n', 'branch adds a file')
        self.git('switch', '-q', self.BASE)
        self.git('mv', 'old', 'moved')
        self.git('commit', '-q', '-m', 'base moves the folder')
        target = self.head()
        self.push_base()
        self.git('switch', '-q', 'work')
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'moved', 'new.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'old')))


class MergesExactlyTheNamedHash(SyncCase):
    def test_a_later_base_commit_is_not_merged(self):
        self.git('switch', '-q', '-c', 'work')
        self.add_commit('docs/branch-only.md', 'branch\n', 'branch edit')
        self.git('switch', '-q', self.BASE)
        named = self.add_commit('docs/named.md', 'named\n', 'the commit the coordinator named')
        self.add_commit('docs/later.md', 'later\n', 'the base moved again')
        self.push_base()
        self.git('switch', '-q', 'work')
        done = self.sync(named)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertEqual(self.git('rev-parse', 'HEAD^2'), named)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'docs', 'named.md')))
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'docs', 'later.md')))
        self.assertIn('Claude %s <noreply@anthropic.com>' % self.MODEL, self.trailers_of_head())

    def test_already_contained_is_nothing_to_merge(self):
        target = self.stage_clean()
        self.assertEqual(self.sync(target).code, syncbase.OK)
        head = self.head()
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertIn('nothing to merge', self.out(done))
        self.assertEqual(self.head(), head)


class OnlyAPushedBaseCommit(SyncCase):
    """P27 (refusal log): an agent synced to the LOCAL base hash while a lander's merge on it was
    still unpushed. sync_base fetches first and merges only a hash on the fetched <remote>/<base>."""

    def stage_unpushed_lander_merge(self):
        """`work` behind a pushed base commit; then a lander merges a branch into local base and
        does not push. Returns (pushed hash, local base hash)."""
        pushed = self.stage_clean()
        self.git('switch', '-q', '-c', 'landing', self.BASE)
        self.add_commit('docs/landed.md', 'landed\n', 'the landed branch')
        self.git('switch', '-q', self.BASE)
        self.git('merge', '-q', '--no-ff', '-m', 'lander merge, not pushed', 'landing')
        local = self.head()
        self.git('switch', '-q', 'work')
        return pushed, local

    def test_local_base_ahead_by_an_unpushed_merge_is_refused_the_fetched_hash_accepted(self):
        pushed, local = self.stage_unpushed_lander_merge()
        self.assertNotEqual(pushed, local)
        head = self.head()
        done = self.sync(local)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertIn('is not on origin/%s' % self.BASE, text)
        self.assertIn('`git ls-remote origin %s`' % self.BASE, text)
        self.assertIn('never local %s' % self.BASE, text)
        self.assertEqual(self.head(), head, 'nothing merged')
        self.assertFalse(self.merged(local))
        fetched = self.git('ls-remote', 'origin', self.BASE).split()[0]
        self.assertEqual(fetched, pushed)
        done = self.sync(fetched)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertTrue(self.merged(pushed))
        self.assertFalse(self.merged(local), 'the unpushed lander merge stays out')

    def test_a_tracking_ref_moved_by_hand_is_reset_by_the_fetch(self):
        """Without the fetch, a hand-moved refs/remotes/origin/<base> would vouch for the
        unpushed hash. The forced fetch puts it back where the remote is."""
        pushed, local = self.stage_unpushed_lander_merge()
        self.git('update-ref', 'refs/remotes/origin/%s' % self.BASE, local)
        done = self.sync(local)
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('is not on origin/%s' % self.BASE, self.out(done))
        self.assertEqual(self.git('rev-parse', 'refs/remotes/origin/%s' % self.BASE), pushed)

    def test_a_hash_pushed_from_another_clone_is_found_by_the_fetch(self):
        """The hash is not in this repository until sync_base's own fetch brings it."""
        self.git('switch', '-q', '-c', 'work')
        other = os.path.join(self.r.base, 'other')
        self.r.git('clone', '-q', '-b', self.BASE, self.r.remote, other, cwd=self.r.base)
        for k, v in (('user.name', NAME), ('user.email', EMAIL), ('commit.gpgsign', 'false')):
            self.r.git('config', k, v, cwd=other)
        self.r.write('docs/from-other.md', 'other\n', root=other)
        target = self.r.commit('from another clone', 'docs/from-other.md', cwd=other)
        self.r.git('push', '-q', 'origin', self.BASE, cwd=other)
        self.assertNotEqual(self.rc('cat-file', '-e', target + '^{commit}'), 0, 'not here yet')
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertTrue(self.merged(target))

    def test_a_base_never_pushed_is_refused_as_unfetchable(self):
        target = self.stage_clean(push=False)
        done = self.sync(target, base='nowhere')
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('could not fetch origin nowhere', self.out(done))
        self.assertFalse(self.merged(target))


class UsageAndTarget(SyncCase):
    """A missing --model is a usage error (exit 2); CLAUDE_AGENT_MODEL is the fallback;
    the target is `-C DIR` (default here); a merge in the MAIN checkout is refused naming the -C
    form; an unknown hash is NOT FOUND."""

    def test_no_model_is_a_usage_error_not_a_refusal(self):
        target = self.stage_clean()
        head = self.head()
        done = self.cli(target)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, out)
        self.assertIn('usage:', out)
        self.assertIn('--model', out)
        self.assertNotIn('efused', out)
        self.assertNotIn('conflict_advice()', out, 'the usage error is not the whole docstring')
        self.assertEqual(self.head(), head)

    def test_a_blank_model_is_the_same_usage_error(self):
        target = self.stage_clean()
        done = self.cli(target, '--model', ' ')
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertFalse(self.merged(target))

    def test_the_environment_variable_supplies_the_model(self):
        target = self.stage_clean()
        done = self.cli(target, env_model='Env Model')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('Claude Env Model <noreply@anthropic.com>', self.trailers_of_head())
        self.assertIn('(from CLAUDE_AGENT_MODEL)', done.stdout)

    def test_the_worktree_acted_on_is_printed_first(self):
        done = self.cli(self.stage_clean(), '--model', self.MODEL)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith('worktree  '), done.stdout)
        first = done.stdout.splitlines()[0][len('worktree  '):]
        top = self.git('rev-parse', '--show-toplevel')
        self.assertEqual(os.path.normcase(os.path.realpath(first)),
                         os.path.normcase(os.path.realpath(top)))

    def test_minus_c_merges_into_the_named_worktree_from_anywhere(self):
        target = self.stage_clean()
        done = self.cli('-C', self.repo, target, '--model', self.MODEL, cwd=self.elsewhere)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.merged(target))

    def test_it_acts_only_on_the_named_tree_never_the_cwd_or_the_main_checkout(self):
        target = self.stage_clean()
        main_head = self.r.git('rev-parse', 'HEAD')
        main_status = self.r.git('status', '--porcelain', '--untracked-files=no')
        done = self.cli('-C', self.repo, target, '--model', self.MODEL, cwd=self.main)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.merged(target))
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), main_head)
        self.assertEqual(self.r.git('rev-parse', '--abbrev-ref', 'HEAD'), 'parking')
        self.assertEqual(self.r.git('status', '--porcelain', '--untracked-files=no'), main_status)

    def test_a_path_into_the_worktree_resolves_to_it(self):
        target = self.stage_clean()
        done = self.cli(target, '--model', self.MODEL, '-C', os.path.join(self.repo, 'docs'),
                        cwd=self.elsewhere)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.merged(target))

    def test_a_merge_in_the_main_checkout_is_refused_naming_minus_c(self):
        """-C defaults to here; standing in the main checkout, here is the main checkout."""
        target = self.stage_clean()
        main_head = self.r.git('rev-parse', 'HEAD')
        done = self.cli(target, '--model', self.MODEL, cwd=self.main)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, out)
        self.assertIn('is in the MAIN checkout', out)
        self.assertIn('sync_base.py -C <worktree> ...', out)
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), main_head)

    def test_minus_c_at_the_main_checkout_is_refused(self):
        target = self.stage_clean()
        main_head = self.r.git('rev-parse', 'HEAD')
        done = self.cli('-C', self.main, target, '--model', self.MODEL, cwd=self.elsewhere)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, out)
        self.assertIn('is in the MAIN checkout', out)
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), main_head)

    def test_an_inherited_git_dir_does_not_move_the_merge(self):
        target = self.stage_clean()
        main_head = self.r.git('rev-parse', 'HEAD')
        env = fixture_env()
        env['GIT_DIR'] = os.path.join(self.main, '.git')
        done = self.cli(target, '--model', self.MODEL, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.merged(target))
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', 'HEAD'), 'work')
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), main_head)

    def test_an_empty_minus_c_is_a_usage_error(self):
        done = self.cli('-C', '', 'abc', '--model', self.MODEL)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('USAGE:', done.stderr)

    def test_an_unknown_hash_is_not_found(self):
        self.git('switch', '-q', '-c', 'work')
        done = self.cli('deadbeefdeadbeef', '--model', self.MODEL)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1, out)
        self.assertIn('NOT FOUND', out)
        self.assertNotIn('efused', out)


class Refusals(SyncCase):
    def test_a_hash_that_is_not_on_the_base_is_refused(self):
        self.git('switch', '-q', '-c', 'work')
        tip = self.add_commit('docs/x.md', 'x\n', 'branch only')
        done = self.sync(tip)
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('is not on %s/%s' % (self.REMOTE, self.BASE), self.out(done))

    def test_a_dirty_tree_is_refused_listing_the_changes(self):
        target = self.stage_a_conflict()
        self.write('docs/rows.md', 'dirty\n')
        done = self.sync(target)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertIn('uncommitted changes', text)
        self.assertIn('python tools/commit.py', text)
        self.assertIn(' M docs/rows.md', text)
        self.assertFalse(self.merged(target))


def kill_git(match):
    """A patch of cmd._run under which each git argv `match(argv)` picks exits 1 having printed
    nothing (a git ended from outside, kitlib.git.refs.KILLED_SHAPE); every other git runs as
    before (the spawner captured before the patch: the fake, or real git)."""
    real = gitcmd.spawner()

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        if match(argv):
            return 1, b'', b''
        return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
    return mock.patch.object(gitcmd, '_run', side_effect=run)


def reads_merge_head(argv):
    return argv[1] == 'rev-parse' and 'MERGE_HEAD' in argv


class AKilledGitIsNoAnswer(SyncCase):
    """KIT-GIT-EXIT1-ANSWERS-2: a git ended from outside exits 1 having printed nothing, the shape
    `rev-parse --verify --quiet` gave for "no such ref"; such a kill is no answer, never one."""

    def test_a_killed_merge_head_read_after_a_clean_merge_is_refused_not_a_fast_forward(self):
        target = self.stage_clean()
        before = self.head()
        with kill_git(reads_merge_head):
            done = self.sync(target)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertNotIn('merged %s' % target[:7], text, 'reported merged with nothing committed')
        self.assertNotIn('no regrowth', text)
        self.assertIn('could not be read', text)
        self.assertIn('Nothing was committed', text)
        self.assertEqual(self.head(), before, 'no commit was made')
        self.assertTrue(os.path.isfile(self.git_path('MERGE_HEAD')),
                        'the merge is left in progress, as the refusal says')

    def test_a_killed_merge_msg_path_read_is_refused_and_the_merge_left_as_is(self):
        """MERGE_HEAD is there (a merge to commit) but `rev-parse --git-path MERGE_MSG` is killed:
        merge_msg_path gives '', which open('') would raise on."""
        target = self.stage_clean()
        before = self.head()
        with kill_git(lambda argv: argv[1] == 'rev-parse' and 'MERGE_MSG' in argv):
            done = self.sync(target)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertIn('MERGE_MSG', text)
        self.assertIn('Nothing was committed and the merge is left in progress', text)
        self.assertNotIn('merged %s' % target[:7], text)
        self.assertEqual(self.head(), before, 'no commit was made')
        self.assertTrue(os.path.isfile(self.git_path('MERGE_HEAD')))

    def test_a_killed_merge_head_read_while_arming_says_the_trailer_was_not_written(self):
        # Not red-first: the old read gave the same False for a kill. Kept as a guard.
        with kill_git(reads_merge_head):
            done = self.sync(self.stage_a_conflict())
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertIn('CONFLICTS', text)
        self.assertIn('could NOT be written', text)

    def test_a_killed_read_of_the_target_is_refused_not_reported_not_found(self):
        target = self.stage_clean()
        with kill_git(lambda argv: argv[1] == 'rev-parse' and argv[-1].startswith(target)):
            done = self.sync(target)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertNotIn('NOT FOUND', text)
        self.assertIn('whether %s is a commit here could not be read' % target, text)
        self.assertFalse(self.merged(target))


class DeadCat:
    """A CatFile whose reader died."""
    dead = True
    dead_reason = 'the reader exited'

    def __init__(self, root, check=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def size(self, rev, path):
        return None


class Regrowth(SyncCase):
    """The silent-undo check: a file the base shrank a lot that came out of the merge big again."""

    def stage_regrowth(self):
        """The base cuts the first 90 of 100 lines; the branch appends 40. The hunks do not touch,
        so git merges cleanly - and the file comes out big again."""
        lines = ['line %03d of the long file, padded to a decent width\n' % i for i in range(100)]
        self.add_commit('docs/long.md', ''.join(lines), 'long file')
        self.git('switch', '-q', '-c', 'work')
        extra = ['branch line %03d, also padded to a decent width here\n' % i for i in range(40)]
        self.write('docs/long.md', ''.join(lines + extra))
        self.commit_all('branch grows it')
        self.git('switch', '-q', self.BASE)
        self.write('docs/long.md', ''.join(lines[90:]))
        self.commit_all('base shrinks it')
        target = self.head()
        self.push_base()
        self.git('switch', '-q', 'work')
        return target

    def test_regrowth_is_reported_and_exits_1(self):
        target = self.stage_regrowth()
        done = self.sync(target)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertTrue(self.merged(target), 'the merge stands; only the check failed')
        self.assertIn('REGROWTH - the base deleted text', text)
        self.assertIn('docs/long.md: base shrank it', text)
        self.assertIn('fix: `git checkout %s -- <path>`' % target[:7], text)

    def test_a_clean_merge_says_no_regrowth_and_names_the_gate(self):
        done = self.sync(self.stage_clean())
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertIn('no regrowth. Now rerun your gate: python tools/gate.py', self.out(done))

    def test_a_dead_size_reader_is_not_reported_clean(self):
        target = self.stage_clean()
        with mock.patch.object(syncbase, 'CatFile', DeadCat):
            done = self.sync(target)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.FAILED, text)
        self.assertIn('REGROWTH CHECK FAILED', text)
        self.assertIn('the reader exited', text)
        self.assertNotIn('no regrowth', text)
        self.assertTrue(self.merged(target))


class DefaultsWithNoKitJson(SyncCase):
    """No `.kit.json`: base `main`, remote `origin`, tools `tools` (the kit defaults)."""

    BASE = 'main'
    CONFIG = None

    def test_the_default_base_is_main(self):
        target = self.stage_clean()
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertTrue(self.merged(target))

    def test_the_default_remote_is_origin(self):
        target = self.stage_clean()
        self.r.git('remote', 'rename', 'origin', 'other')
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('could not fetch origin main', self.out(done))


class BaseOption(SyncCase):
    """--base names a branch other than "repo".main_branch."""

    BASE = 'trunk'
    CONFIG = None

    def test_base_trunk_is_used_when_named(self):
        target = self.stage_clean()
        done = self.sync(target, base='trunk')
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertTrue(self.merged(target))

    def test_without_it_the_configured_default_is_used(self):
        done = self.sync(self.stage_clean())
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('could not fetch origin main', self.out(done))

    def test_the_conflict_advice_names_the_base_given(self):
        done = self.sync(self.stage_a_conflict(), base='trunk')
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn("trunk's text wins for a row trunk changed", self.out(done))

    def test_an_empty_or_flag_shaped_base_is_a_usage_error(self):
        target = self.stage_clean()
        for bad in ('', '-x'):
            with self.subTest(bad=bad):
                done = self.cli(target, '--model', self.MODEL, '--base=' + bad)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertFalse(self.merged(target))


class ConfiguredRemote(SyncCase):
    """`.kit.json` "repo".remote: the fetch and the pushed-base rule read <remote>/<base>."""

    REMOTE = 'upstream'
    CONFIG = {'repo': {'main_branch': 'master', 'remote': 'upstream'}}

    def test_the_configured_remote_is_read(self):
        target = self.stage_clean()
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertTrue(self.merged(target))
        self.assertEqual(self.git('rev-parse', 'refs/remotes/upstream/master'), target)

    def test_origin_alone_is_not_enough(self):
        self.r.git('remote', 'remove', 'upstream')
        target = self.stage_clean()
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.FAILED, self.out(done))
        self.assertIn('could not fetch upstream master', self.out(done))
        self.assertFalse(self.merged(target))


class ConfiguredToolsDir(SyncCase):
    """`.kit.json` "repo".tools_dir spells the commands this prints."""

    CONFIG = {'repo': {'main_branch': 'master', 'tools_dir': 'devtools'}}

    def test_the_gate_hint_names_the_tools_dir(self):
        done = self.sync(self.stage_clean())
        self.assertEqual(done.code, syncbase.OK, self.out(done))
        self.assertIn('python devtools/gate.py', self.out(done))

    def test_the_dirty_tree_refusal_names_the_tools_dir(self):
        target = self.stage_clean()
        self.write('docs/rows.md', 'dirty\n')
        self.assertIn('python devtools/commit.py', self.out(self.sync(target)))


class FakeTime(object):
    """time.sleep and time.monotonic for sync(): sleep moves the clock, then runs `on_sleep`."""

    def __init__(self, on_sleep=None):
        self.now, self.slept, self.on_sleep = 0.0, [], on_sleep

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s
        if self.on_sleep is not None:
            self.on_sleep()


class WaitsForALandingInFlight(SyncCase):
    """"landing".in_flight {}: a held kit landing lock before the fetch is waited for; still
    exactly the named hash is merged, and one note says the fetched base is past it."""

    CONFIG = {'repo': {'main_branch': 'master'}, 'landing': {'in_flight': {}}}

    def lock(self):
        return landing.LandingLock.for_config(self.main, load_config(self.main))

    def stage_a_landing(self):
        """`work` behind a pushed target; a lander's merge on master after it, unpushed, its lock
        held. Returns (target, the landing merge, the lock record)."""
        target = self.stage_clean()
        self.git('switch', '-q', '-c', 'landing', self.BASE)
        self.add_commit('docs/landed.md', 'landed\n', 'the landed branch')
        self.git('switch', '-q', self.BASE)
        self.git('merge', '-q', '--no-ff', '-m', 'lander merge, not pushed yet', 'landing')
        merge = self.head()
        self.git('switch', '-q', 'work')
        return target, merge, self.lock().take('full', ['landing'])

    def sync_on(self, target, t, wait=True):
        said = []
        done = syncbase.sync(self.repo, target, self.MODEL, '--model', None, wait=wait,
                             say=said.append, sleep=t.sleep, clock=t.clock)
        return done, self.out(done), said

    def test_it_waits_merges_exactly_the_target_and_notes_the_base_moved_past_it(self):
        target, merge, record = self.stage_a_landing()

        def the_lander_pushes_and_releases():
            self.push_base()
            self.lock().release(record['token'])
        t = FakeTime(the_lander_pushes_and_releases)
        done, text, said = self.sync_on(target, t)
        self.assertEqual(done.code, syncbase.OK, text)
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0].startswith('landing   in flight: the landing lock '), said)
        self.assertIn('landing   done after 10s', text)
        self.assertEqual(self.git('rev-parse', 'HEAD^2'), target)
        self.assertFalse(self.merged(merge), 'only the named hash is merged')
        self.assertIn('note      origin/master is at %s, past %s: ask the coordinator whether to '
                      'sync to it' % (merge[:7], target[:7]), text)
        self.assertEqual(t.slept, [10])

    def test_at_the_bound_it_goes_on_and_names_the_holder(self):
        target, _merge, _record = self.stage_a_landing()
        t = FakeTime()
        done, text, _said = self.sync_on(target, t)
        self.assertEqual(done.code, syncbase.OK, text)
        self.assertIn('landing   still in flight after 100s, going on without it: the landing '
                      'lock ', text)
        self.assertTrue(self.merged(target))
        self.assertNotIn('note      ', text)          # the landing never pushed
        self.assertEqual(sum(t.slept), 100)

    def test_no_wait_says_so_and_never_sleeps(self):
        target, _merge, _record = self.stage_a_landing()
        t = FakeTime()
        done, text, said = self.sync_on(target, t, wait=False)
        self.assertEqual(done.code, syncbase.OK, text)
        self.assertIn('landing   in flight, not waited for (--no-wait): the landing lock ', text)
        self.assertEqual((said, t.slept), ([], []))

    def test_the_cli_takes_no_wait(self):
        target, _merge, _record = self.stage_a_landing()
        done = self.cli(target, '--model', self.MODEL, '--no-wait')
        text = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, text)
        self.assertIn('landing   in flight, not waited for (--no-wait): ', text)
        self.assertTrue(self.merged(target))


class InFlightUnsetNeverWaits(SyncCase):
    """No "landing".in_flight (the default): a held lock is not read, nothing waits, and the
    past-target note is not printed."""

    def test_a_held_lock_is_not_waited_for(self):
        target = self.stage_clean()
        self.git('switch', '-q', self.BASE)
        later = self.add_commit('docs/later.md', 'later\n', 'the base moved again')
        self.push_base()
        self.git('switch', '-q', 'work')
        landing.LandingLock.for_config(self.main, load_config(self.main)).take('full', ['x'])

        def never(_s):
            raise AssertionError('slept with "landing".in_flight unset')
        done = syncbase.sync(self.repo, target, self.MODEL, sleep=never)
        text = self.out(done)
        self.assertEqual(done.code, syncbase.OK, text)
        self.assertNotIn('landing ', text)
        self.assertNotIn('past %s' % target[:7], text)
        self.assertFalse(self.merged(later))


class BadConfiguration(SyncCase):
    """A `.kit.json` that cannot be read is refused (exit 2) before anything is merged."""

    CONFIG = {'repo': {'main_branch': 'master', 'mainbranch': 'typo'}}

    def test_an_unknown_repo_key_is_refused(self):
        target = self.stage_clean()
        done = self.sync(target)
        self.assertEqual(done.code, syncbase.USAGE, self.out(done))
        self.assertIn('sync_base: refused: kit configuration', self.out(done))
        self.assertFalse(self.merged(target))


if __name__ == '__main__':
    unittest.main()
