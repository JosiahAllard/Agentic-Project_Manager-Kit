"""Zero repo setup, end to end (docs/multi-host-plan.md section 7, step 20's proof; docs/install.md
"What first use writes"): a fresh clone of an unrelated repository, with nothing of the kit
committed in it, runs a PM-to-worker-to-land cycle through the kit's own library calls, and its
history afterwards holds the worker's change and nothing else.

The cycle, in the order a PM and a worker run it:
  1. machine setup's owner step (kitlib.machinesetup.owner.survey, as the default mode plans and
     applies it) stores the owner's email in the machine config, and writes nothing into the
     clone; the machine config's other key the cycle reads is dev_drive, which the owner sets by
     hand (docs/kit-json.md "Machine config keys"): the gate's and the landing's result records go
     under it (docs/gate.md "Result record"), never into the clone;
  2. the PM makes the worker's worktree (kitlib.worktree.new.create, tools/new_worktree.py),
     which runs the first-use repo prep (kitlib.git.prep: info/exclude, core.hooksPath);
  3. the worker commits in the worktree (kitlib.commit.commit, tools/commit.py);
  4. the worker gates the branch --full with no gate configured (kitlib.gate.runner.branch_gate:
     the stack is detected, `source: auto`, and the clone's own unittest tests run), which writes
     the full-gate OK record a landing takes for the tip;
  5. the PM lands it from the main checkout (kitlib.gate.land.Lander, `gate.py land`): merge, the
     merged tree's gate, the push to the remote, the branch cleanup.

What stands in for the machine (the rest is the kit's code as it runs):
  - git is the kit's fake git (tests/__init__.py), so no git hook runs: the kit's pre-push hook
    that core.hooksPath points at is not run by the landing's push here (tests/guard covers it);
  - the machine lock: AnyPlatform replaces kitlib.lock.run.book, as the gate calls it, with a slot
    that starts each step as a plain child process (no job object, no cpu slots, no affinity),
    the landing's one booking included, so it has its turn at once; OnWindows books a scratch store on fake hardware
    (tests/lock/helpers.StoreCase, as tests/gate/test_auto_gate.py's gates do), so the steps run
    in the lock's real job objects, Windows only;
  - the home folder is a temp folder (tests/home.py), the PM-repo pointer names a temp PM
    repository (tests.home.pm_repo) whose .pm-kit holds the machine config, and the user's global
    git identity is a temp global config file;
  - the session's own GIT_CONFIG_* entries (a sandbox's url rewrites, say) are dropped: they are
    not part of the machine a user runs the kit on.
"""
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import commit as kcommit
from kitlib import kitroot
from kitlib import gate as G
from kitlib.config import user as kuser
from kitlib.gate import land, runner
from kitlib.git import cmd as gitcmd
from kitlib.git import prep
from kitlib.git.fake import repo as fakerepo
from kitlib.guard import prepush
from kitlib.lock import store as lockstore
from kitlib.machinesetup import owner as mowner
from kitlib.machinesetup.common import Plan
from kitlib.worktree import new as wtnew
from tests import gitfixture
from tests.gate.fixtures import queue_review, test_env
from tests.home import pm_repo, result_drive, temp_home
from tests.lock.helpers import StoreCase
from tests.realgit import real_git
from tests.platforms import windows_only

OWNER = 'owner@example.invalid'
WORKER = 'worker@example.invalid'

UPSTREAM = {
    'README.md': '# calc\n\nA small calculator.\n',
    'calc.py': 'def add(a, b):\n    return a - b\n',
    'tests/__init__.py': '',
    'tests/test_calc.py': ('import unittest\n\nimport calc\n\n\nclass Add(unittest.TestCase):\n'
                           '    def test_small(self):\n        self.assertEqual(calc.add(0, 0), 0)\n'),
}
FIX = {
    'calc.py': 'def add(a, b):\n    return a + b\n',
    'tests/test_calc.py': ('import unittest\n\nimport calc\n\n\nclass Add(unittest.TestCase):\n'
                           '    def test_small(self):\n        self.assertEqual(calc.add(0, 0), 0)\n\n'
                           '    def test_two(self):\n        self.assertEqual(calc.add(2, 3), 5)\n'),
}
RED = {'tests/test_calc.py': ('import unittest\n\nimport calc\n\n\nclass Add(unittest.TestCase):\n'
                              '    def test_two(self):\n        self.assertEqual(calc.add(2, 3), 5)\n')}
HOOKS = ("hooks: first use points core.hooksPath at the kit's githooks, so every commit and push of "
         "the cycle runs the kit's pre-commit and pre-push hooks, which the fake git does not run")
# What first use may write: local git settings only, never a tracked or untracked file.
EXCLUDES = ('/.claude/worktrees/', '/.codex/worktrees/')


def _machine_env():
    """os.environ without the session's GIT_CONFIG_COUNT/KEY/VALUE entries and its git identity
    (GIT_AUTHOR_*, GIT_COMMITTER_*): a machine's identity is its global config, which the cycle sets."""
    return {k: v for k, v in os.environ.items()
            if not (k == 'GIT_CONFIG_COUNT' or k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_',
                                                             'GIT_AUTHOR_', 'GIT_COMMITTER_')))}


class Cycle(object):
    """The clone and the cycle; a subclass says how the machine lock is held (book_kw)."""

    def make_clone(self):
        patch = mock.patch.dict(os.environ, _machine_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, gitfixture.fixture_env())
        patch.start()
        self.addCleanup(patch.stop)
        self.home = temp_home(self)
        self.pm = pm_repo(self)
        self.drive = result_drive(self)         # dev_drive: the result records' folder
        self.base = os.path.realpath(tempfile.mkdtemp(prefix='kit-zero-'))
        self.addCleanup(gitfixture.rmtree, self.base)
        # The user's own git identity, as a machine has it: global, never in the repository.
        glob = os.path.join(self.base, 'global.gitconfig')
        with open(glob, 'w', encoding='utf-8', newline='\n') as f:
            f.write('[user]\n\tname = Worker\n\temail = %s\n' % WORKER)
        patch = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': glob})
        patch.start()
        self.addCleanup(patch.stop)
        self.remote = os.path.join(self.base, 'remote.git')
        self.make_upstream()
        self.clone = os.path.join(self.base, 'clone')
        self.git('clone', '-q', self.remote, self.clone, cwd=self.base)
        self.env = dict(test_env(), GIT_CONFIG_GLOBAL=glob)
        self.logs = os.path.join(self.base, 'logs')

    def git(self, *args, cwd=None):
        r = gitfixture.gitcmd.subprocess_run(('git',) + args, cwd=cwd or self.clone,
                                             capture_output=True, text=True, encoding='utf-8',
                                             errors='replace', env=dict(os.environ))
        if r.returncode != 0:
            raise AssertionError('git %s: %s' % (' '.join(args), r.stderr))
        return r.stdout.strip()

    def make_upstream(self):
        """An unrelated project on a bare remote: a module with a bug, its unittest tests, a
        README. Nothing of the kit."""
        up = os.path.join(self.base, 'upstream')
        os.makedirs(up)
        self.git('init', '-q', '-b', 'main', up, cwd=self.base)
        self.write(up, UPSTREAM)
        self.git('add', '--', *UPSTREAM, cwd=up)
        self.git('commit', '-q', '-m', 'calc', cwd=up)
        self.git('init', '-q', '--bare', '-b', 'main', self.remote, cwd=self.base)
        self.git('push', '-q', self.remote, 'main', cwd=up)

    def write(self, root, files):
        for rel, text in files.items():
            path = os.path.join(root, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)

    def status(self, cwd=None):
        return self.git('status', '--porcelain', '--untracked-files=all', cwd=cwd)

    def remote_git(self, *args):
        return self.git('--git-dir', self.remote, *args, cwd=self.base)

    def make_worktree(self, branch):
        made = wtnew.create(self.clone, branch)
        self.assertEqual(made.code, wtnew.OK, made.lines)
        wt = os.path.join(self.clone, '.claude', 'worktrees', branch.split('/', 1)[1])
        self.assertTrue(os.path.isdir(wt), made.lines)
        return wt

    def worker_commit(self, wt, files, subject):
        self.write(wt, files)
        done = kcommit.commit(kcommit.request(wt, subject, sorted(files), model='Test 1'))
        self.assertEqual(done.code, kcommit.OK, done.lines)
        return self.git('rev-parse', 'HEAD', cwd=wt)

    def test_a_fresh_clone_runs_the_cycle_with_nothing_of_the_kit_committed(self):
        start = self.git('rev-parse', 'HEAD')
        tracked = sorted(self.git('ls-files').split())
        config_before = self.git('config', '--local', '--list')
        # 1. machine setup: the owner's email, in the machine config only (the default mode's
        # owner step, planned and applied as machine_setup.py --owner-email ... --apply does)
        plan = Plan(Plan.FRESH_SECTIONS)
        mowner.survey(plan, types.SimpleNamespace(home=self.home, kit=kitroot.root(), host=None,
                                                  owner_email=OWNER))
        self.assertEqual((len(plan.changes), plan.refusals), (1, []), plan.lines())
        for change in plan.changes:
            change.do()
        self.assertEqual(kuser.owner_email(self.home), OWNER)
        cfg = kconfig.load(self.clone, home=self.home)      # no .kit.json: the defaults
        self.assertEqual((cfg.main_branch, cfg.remote, cfg.owner_rules().owner_email),
                         ('main', 'origin', OWNER))
        self.assertEqual(self.status(), '')
        self.assertEqual(self.git('config', '--local', '--list'), config_before)
        # 2. the PM makes the worker's worktree; the first-use prep writes local settings only
        wt = self.make_worktree('kit/fix-add')
        with open(os.path.join(self.clone, '.git', 'info', 'exclude'), encoding='utf-8') as f:
            exclude = f.read().splitlines()
        for line in EXCLUDES:
            self.assertIn(line, exclude)
        self.assertEqual(self.git('config', '--local', '--get', 'core.hooksPath'), prep.kit_githooks())
        self.assertEqual(self.status(), '')               # the worktree folder is not untracked
        # 3. the worker commits its change in the worktree
        tip = self.worker_commit(wt, FIX, 'Fix add')
        self.assertEqual(self.git('log', '-1', '--format=%ae', cwd=wt), WORKER)
        self.pre_commit_passed(wt)
        # ... and pushes its branch: the worktree shares the clone's remote (decision 23)
        self.git('push', '-q', 'origin', 'kit/fix-add', cwd=wt)
        self.assertEqual(self.remote_git('rev-parse', 'kit/fix-add'), tip)
        # 4. the worker's gate: no gate configured, the clone's own tests detected and run, --full
        #    (the record a landing takes for the tip)
        gated = runner.branch_gate(wt, full=True, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(gated.code, G.OK, gated.lines)
        self.assertEqual(gated.source, 'auto')
        self.assertEqual([(r['name'], r['status']) for r in gated.results], [('python-tests', 'PASS')])
        self.assertEqual(self.status(), '')
        # 5. the coordinator queues the review's LAND verdict; the PM lands it: merge, gate the
        #    merged tree, push, clean up the branch
        self.assertEqual(queue_review(self.clone, 'kit/fix-add', tip), ['kit/fix-add'])
        landed = land.Lander(self.clone, 'kit/fix-add', start, tip, git_env=self.env,
                             book_kw=self.book_kw, log_dir=self.logs).run()
        self.assertEqual(landed.code, G.OK, landed.lines)
        self.assertIn('source: auto (auto-detected gate: python; a "gate" section in .kit.json or '
                      'tools/gate.json replaces it)', landed.lines)
        self.assertTrue(landed.lines[-1].startswith('LANDED kit/fix-add on main at %s' % landed.merge),
                        landed.lines)
        self.assertEqual(self.remote_git('rev-parse', 'main'), self.git('rev-parse', 'HEAD'))
        self.pre_push_passed(landed.merge, start)
        # The result records are under dev_drive; the clone's git dir holds none.
        for rec in (gated.result_record, landed.result_record):
            self.assertTrue(os.path.isfile(rec), rec)
            self.assertTrue(os.path.normcase(rec).startswith(os.path.normcase(self.drive) + os.sep),
                            rec)
        self.assertFalse(os.path.exists(os.path.join(self.clone, '.git', 'kit-gate-result')))
        # The remote's history past the upstream commit changes the worker's files and no other.
        changed = set(self.remote_git('log', '--format=', '--name-only', '%s..main' % start).split())
        self.assertEqual(changed, set(FIX))
        self.assertEqual(sorted(self.remote_git('ls-tree', '-r', '--name-only', 'main').split()), tracked)
        self.assertEqual(sorted(self.git('ls-files').split()), tracked)
        self.assertEqual(self.status(), '')
        self.assertFalse(os.path.exists(wt), landed.lines)
        self.assertEqual(self.remote_git('branch', '--list', 'kit/fix-add'), '')
        # What the whole cycle left in the clone's own config: core.hooksPath, and nothing else.
        added = set(self.git('config', '--local', '--list').splitlines()) - set(config_before.splitlines())
        self.assertEqual({l.split('=', 1)[0].lower() for l in added}, {'core.hookspath'}, added)
        # Every commit the remote's main gained is the worker's change or the landing's merge.
        authors = set(self.remote_git('log', '--format=%ae', '%s..main' % start).split())
        self.assertTrue(authors and authors <= {WORKER}, authors)

    def test_the_detected_gate_holds_a_red_branch_back(self):
        """The tests the gate found are the clone's own: a branch that leaves them red does not
        pass, and nothing is pushed."""
        before = self.remote_git('rev-parse', 'main')
        wt = self.make_worktree('kit/red')
        self.worker_commit(wt, RED, 'A test of add')
        gated = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(gated.code, G.FAILED, gated.lines)
        self.assertTrue(gated.verdict.startswith('GATE FAILED: python-tests'), gated.verdict)
        self.assertEqual(self.remote_git('rev-parse', 'main'), before)
        self.assertEqual(self.status(), '')


class _Launcher(object):
    """A slot's launcher with no lock: each step a plain child process."""
    env = None

    def __init__(self, env):
        self.env = env

    def start(self, argv, give_back=False, **kw):
        return subprocess.Popen(argv, **kw)

    def wait(self, p):
        return p.wait()


class _Slot(object):
    id = 'none'

    def __init__(self, env):
        self.launcher = _Launcher(env)

    def held_text(self):
        return 'held: no machine lock (tests/zerosetup)'

    def release(self, code, why):
        pass

    def stop_asked(self):
        return False            # no lock, so nothing can ask

    def stop_daemons(self):
        pass

    def note_tree(self, tree):
        pass                    # no lock record to note the tested tree on

    def end_tree(self):
        pass

    def watch_stop(self):
        return None             # no window monitor: nothing was watched

    def window_lines(self, fails=False):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AnyPlatform(Cycle, unittest.TestCase):
    """The cycle with kitlib.lock.run.book replaced (no job, no cpu slots), on the fake git: runs
    on any platform; the landing's turn is that booking, so it has it at once. The fake git runs no
    hook and refuses a commit or a push that has one, so it is told there is none and the hooks
    it was asked for are recorded; each is then run here on what the cycle did (pre_commit_passed,
    pre_push_passed)."""

    def setUp(self):
        self.make_clone()
        self.hooks_asked = []

        def hook_exists(repo, name):
            self.hooks_asked.append(name)
            return False
        patch = mock.patch.object(fakerepo.Repo, 'hook_exists', hook_exists)
        patch.start()
        self.addCleanup(patch.stop)
        booked = []

        def book(kind, repo, slug, env=None, **kw):
            booked.append(kind)
            return _Slot(env)
        patch = mock.patch.object(runner.lockrun, 'book', book)
        patch.start()
        self.addCleanup(patch.stop)
        self.booked = booked
        # The machine store's own root is asked of Windows: a scratch path stands in, never used.
        patch = mock.patch.object(lockstore, 'real_root', lambda: os.path.join(self.base, 'machine-store'))
        patch.start()
        self.addCleanup(patch.stop)
        # A scratch lock store: under the kit's tests the machine's store is fenced (Store.path raises
        # HomeFenced, kitlib/lock/store.py), so the gates this test books cannot use it.
        self.book_kw = {'env': self.env, 'store': lockstore.Store(root=os.path.join(self.base, 'scratch-store'))}

    def pre_commit_passed(self, wt):
        """The commit asked for its pre-commit hook; the kit's, run in the worktree as git runs it
        (by sh, at the work tree's root), passes: the repository has no .kit.json naming the ledger
        check and is not a kit checkout. On real git (KIT_FAKE_GIT=0) git ran the hook itself, as
        in OnWindows, and the fake's hook_exists is never asked."""
        if not gitcmd.fake_active():
            return
        self.assertIn('pre-commit', self.hooks_asked)
        sh = shutil.which('sh')
        if sh is None:
            self.skipTest('no sh to run the pre-commit hook with')
        r = subprocess.run([sh, os.path.join(prep.kit_githooks(), 'pre-commit')], cwd=wt,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def pre_push_passed(self, merge, start):
        """The landing's push asked for its pre-push hook; the kit's (kitlib.guard.prepush), given
        the line git hands it for that push, allows it: a fast-forward of main. On real git git
        ran the hook itself."""
        if not gitcmd.fake_active():
            return
        self.assertIn('pre-push', self.hooks_asked)
        line = 'refs/heads/main %s refs/heads/main %s' % (merge, start)
        self.assertEqual(prepush.check(self.clone, [line], remote='origin', url=self.remote), (True, None))


@real_git(HOOKS)
@windows_only('the lock is Windows only')
class OnWindows(Cycle, StoreCase):
    """The cycle with the steps run in the machine lock's real job objects, on a scratch store."""

    def setUp(self):
        StoreCase.setUp(self)
        self.confine(1)
        self.make_clone()
        self.book_kw = {'cores': 1, 'store': self.store, 'env': self.env}

    def pre_commit_passed(self, wt):
        """Real git ran the kit's pre-commit hook: the commit went through it."""

    def pre_push_passed(self, merge, start):
        """Real git ran the kit's pre-push hook: the push went through it."""


if __name__ == '__main__':
    unittest.main()
