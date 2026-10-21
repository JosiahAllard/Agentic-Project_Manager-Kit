"""kitlib.landed (tools/landed.py): one read-only look at whether a branch has landed. Ported from
the PM kit's test_landed.py; the cases that tested retired mechanisms or unported keys are listed
in docs/landed.md "Not ported".

Every case builds its own repository and bare origin (tests/gitfixture.py), its own gate log
folder and its own landing lock; nothing reaches the network, the real gate logs or a real lock.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import kitlib.landed as landed
from kitlib.config import load
from kitlib.config import user as kuser
from kitlib.git import cmd
from kitlib.git import defaults as gitdefaults
from kitlib.records import gate_ok, landing
from tests import gitfixture
from tests.home import machine_config

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'landed.py')
LOGS = 'gate-logs'


class Fixture(unittest.TestCase):
    """`repo` (branch MAIN pushed to its bare origin, then `topic` with one commit, checked out)."""

    MAIN = 'main'
    BRANCH = 'topic'

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        branch = self.BRANCH

        def prepare(fx):            # built once per process per (MAIN, BRANCH); each test gets a copy
            fx.git('switch', '-q', '-c', branch)
            fx.write('b.txt', 'topic work\n')
            fx.commit('topic work', 'b.txt')
        self.fx = gitfixture.TempRepo(branch=self.MAIN, bare_remote=True, prepare=prepare,
                                      prepare_key=('test_landed.Fixture', branch))
        self.addCleanup(self.fx.cleanup)
        self.tmp = os.path.realpath(self.fx.base)
        self.root = os.path.realpath(self.fx.root)
        # The gate's default log base lives in the kit temp (kitlib.config.user.work_temp): for a
        # checkout on the drive of the shared temp (%LOCALAPPDATA%/Temp on Windows,
        # tempfile.gettempdir() elsewhere), that shared temp; both point here. No machine config
        # (an empty home) sets temp_dir.
        os.environ['LOCALAPPDATA'] = os.path.join(self.tmp, 'localappdata')
        self.temp = os.path.join(self.tmp, 'localappdata', 'Temp')
        os.makedirs(self.temp)
        for patch in (mock.patch.object(tempfile, 'tempdir', self.temp),
                      mock.patch.object(kuser, 'home_dir', return_value=self.tmp)):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, *args, cwd=None):
        return self.fx.git(*args, cwd=cwd or self.root)

    def commit(self, rel, text, cwd=None):
        self.fx.write(rel, text + '\n', root=cwd or self.root)
        return self.fx.commit(text, rel, cwd=cwd or self.root)

    def land_locally(self):
        self.git('switch', '-q', self.MAIN)
        self.git('merge', '-q', '--no-ff', '-m', 'merge', self.BRANCH)

    def push(self, remote='origin'):
        self.git('push', '-q', remote, self.MAIN)

    def config(self, **sections):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump(sections, fh)

    def log(self, rel, text, mtime=None, base=None):
        path = os.path.join(base or os.path.join(self.root, LOGS), *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def look(self, ref=None, root=None):
        got = landed.look(root or self.root, ref or self.BRANCH)
        return got.code, '\n'.join(got.lines)

    def cli(self, *args, cwd=None):
        # The child's home is the one setUp gives this process (home_dir): it looks up the kit temp
        # too, and under the home fence (docs/test-tools.md "Home fence") the real home is refused.
        done = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.root,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env({'USERPROFILE': self.tmp, 'HOME': self.tmp}),
                              timeout=120)
        return done.returncode, (done.stdout or '') + (done.stderr or '')


class Containment(Fixture):

    def test_an_unmerged_branch_is_not_yet(self):
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('contained  no in origin/main', out)
        self.assertTrue(out.rstrip().endswith('NOT YET'), out)

    def test_merged_but_not_pushed_is_not_yet_and_says_so(self):
        self.land_locally()
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('merged into local main, not on origin/main yet', out)

    def test_merged_and_pushed_is_landed(self):
        self.land_locally()
        self.push()
        code, out = self.look()
        self.assertEqual(code, landed.LANDED, out)
        self.assertIn('remote     ', out)
        self.assertIn('(ls-remote origin)', out)
        self.assertIn('contained  YES in origin/main', out)
        self.assertTrue(out.rstrip().endswith('LANDED'), out)

    def test_without_a_remote_the_local_main_branch_decides(self):
        self.git('remote', 'remove', 'origin')
        self.assertEqual(self.look()[0], landed.NOT_YET)
        self.land_locally()
        code, out = self.look()
        self.assertEqual(code, landed.LANDED, out)
        self.assertIn('local main (no remote `origin`)', out)

    def test_an_unreachable_remote_falls_back_to_the_last_fetch(self):
        self.land_locally()
        self.push()
        self.git('fetch', '-q', 'origin')
        self.git('remote', 'set-url', 'origin', os.path.join(self.tmp, 'gone.git'))
        code, out = self.look()
        self.assertEqual(code, landed.LANDED, out)
        self.assertIn('origin unreachable: last fetched refs/remotes/origin/main', out)

    def test_a_remote_sha_this_clone_lacks_is_not_judged(self):
        other = os.path.join(self.tmp, 'other')
        self.git('clone', '-q', '-b', self.MAIN, self.fx.remote, other, cwd=self.tmp)
        for k, v in (('user.name', gitfixture.NAME), ('user.email', gitfixture.EMAIL)):
            self.git('config', k, v, cwd=other)
        self.commit('c.txt', 'elsewhere', cwd=other)
        self.git('push', '-q', 'origin', self.MAIN, cwd=other)
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('contained  UNKNOWN', out)
        self.assertIn('`git fetch origin`, then re-run', out)

    def test_a_commit_sha_is_accepted(self):
        tip = self.git('rev-parse', 'HEAD')
        self.land_locally()
        self.push()
        self.assertEqual(self.look(tip)[0], landed.LANDED)
        self.assertEqual(self.look(tip[:10])[0], landed.LANDED)

    def test_a_ref_that_does_not_resolve_cannot_be_answered(self):
        code, out = self.look('no-such-branch')
        self.assertEqual(code, landed.CANNOT, out)
        self.assertIn('CANNOT ANSWER', out)

    def test_a_killed_read_of_the_local_main_is_unknown_not_no_such_branch(self):
        """A git ended from outside exits 1 having printed nothing, the
        shape `rev-parse --verify --quiet` gave for "no such ref"."""
        self.git('remote', 'remove', 'origin')
        real = cmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'rev-parse' and argv[-1].startswith('refs/heads/main'):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        with mock.patch.object(cmd, '_run', side_effect=run):
            code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertNotIn('no such branch here', out)
        self.assertIn('main (UNKNOWN: ', out)
        self.assertIn('contained  UNKNOWN: local main could not be read', out)

    def kill(self, match):
        real = cmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if match(argv):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        return mock.patch.object(cmd, '_run', side_effect=run)

    def test_a_killed_remote_lookup_cannot_answer_never_lets_the_local_main_decide(self):
        """Any failure of `remote get-url` read as "no remote", so a
        kill let the local main, which holds an unpushed merge, answer LANDED (exit 0)."""
        self.land_locally()
        with self.kill(lambda argv: argv[1:3] == ['remote', 'get-url']):
            code, out = self.look()
        self.assertEqual(code, landed.CANNOT, out)
        self.assertIn('CANNOT ANSWER: whether this clone has the remote `origin` could not be '
                      'read', out)
        self.assertNotIn('LANDED', out)

    def test_a_misnamed_remote_cannot_answer_and_names_the_remotes_there_are(self):
        """`remote get-url origin` exits 2 (no such remote) while the clone has `upstream`: the
        configured remote is misnamed, so the local main, which holds an unpushed merge, must
        not decide (kitlib/backlog/store.py find_remote's rule)."""
        self.land_locally()
        self.git('remote', 'rename', 'origin', 'upstream')
        code, out = self.look()
        self.assertEqual(code, landed.CANNOT, out)
        self.assertIn('there is no remote `origin`, but this clone has upstream', out)
        self.assertNotIn('LANDED', out)

    def test_a_missing_remote_with_a_killed_remote_listing_cannot_answer(self):
        """`remote get-url origin` exits 2 (origin renamed away), and the `git remote` listing
        that would tell "no remote at all" from "misnamed" is killed (exit 1, no output): no
        answer, never the local main deciding."""
        self.land_locally()
        self.git('remote', 'rename', 'origin', 'upstream')
        with self.kill(lambda argv: argv[1:] == ['remote']):
            code, out = self.look()
        self.assertEqual(code, landed.CANNOT, out)
        self.assertIn('there is no remote `origin`, and the remotes there are could not be '
                      'listed', out)
        self.assertNotIn('local main (no remote', out)
        self.assertNotIn('LANDED', out)

    def test_a_killed_ancestry_read_is_unknown_never_no(self):
        """refs.is_ancestor's None (a kill) was `bool()`-ed into "contained  no"."""
        self.land_locally()
        self.push()
        with self.kill(lambda argv: argv[1] == 'rev-list'):
            code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertNotIn('contained  no', out)
        self.assertIn('contained  UNKNOWN: whether origin/main holds ', out)
        self.assertIn('printed nothing', out)

    def test_an_unread_local_main_is_shown_unknown_while_the_remote_decides(self):
        self.land_locally()
        self.push()
        with self.kill(lambda argv: argv[1] == 'rev-parse'
                       and argv[-1].startswith('refs/heads/main')):
            code, out = self.look()
        self.assertEqual(code, landed.LANDED, out)
        self.assertIn('main (UNKNOWN: ', out)
        self.assertIn('contained  YES in origin/main', out)

    def test_a_killed_read_of_the_ref_says_so(self):
        real = cmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'rev-parse' and argv[-1].startswith(self.BRANCH):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        with mock.patch.object(cmd, '_run', side_effect=run):
            code, out = self.look()
        self.assertEqual(code, landed.CANNOT, out)
        self.assertIn('`topic` could not be read', out)
        self.assertNotIn('does not resolve', out)

    def test_it_only_reads(self):
        self.land_locally()
        before = (self.git('for-each-ref'), self.git('status', '--porcelain'))
        lock = landing.LandingLock.for_config(self.root, load(self.root))
        self.look()
        self.assertEqual((self.git('for-each-ref'), self.git('status', '--porcelain')), before)
        self.assertFalse(os.path.exists(lock.path))
        self.assertEqual(os.listdir(self.temp), [])


class TheMainBranchAndRemoteComeFromConfig(Fixture):

    MAIN = 'master'

    def test_repo_main_branch_and_remote(self):
        self.git('remote', 'rename', 'origin', 'upstream')
        self.config(repo={'main_branch': 'master', 'remote': 'upstream'})
        self.land_locally()
        self.push('upstream')
        code, out = self.look()
        self.assertEqual(code, landed.LANDED, out)
        self.assertIn('contained  YES in upstream/master', out)

    def test_the_default_main_is_the_repository_s_own(self):
        # no repo.main_branch: read from the repository, here master (docs/kit-json.md "With no file")
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertNotIn('no such branch here', out)
        self.assertIn('origin/master', out)


class Verdicts(Fixture):
    """The gate log folders (kitlib.gate.runner.log_base / log_folder): FAILED only from evidence."""

    def setUp(self):
        super().setUp()
        self.config(landing={'log_dir': LOGS})

    def test_no_log_is_not_yet(self):
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('verdict    none found under %s' % os.path.join(self.root, LOGS), out)

    def test_a_failed_gate_is_failed(self):
        self.log('topic/20261003-120000-1/gate.log', 'step PASS\nGATE FAILED: tests (exit 1)\n')
        code, out = self.look()
        self.assertEqual(code, landed.FAILED, out)
        self.assertIn('verdict    GATE FAILED: tests (exit 1)', out)
        self.assertTrue(out.rstrip().endswith('FAILED'), out)

    def test_a_passing_gate_is_still_not_yet(self):
        self.log('topic/20261003-120000-1/gate.log', 'GATE OK (3 steps)\n')
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('verdict    GATE OK (3 steps)', out)

    def test_the_landings_verdicts_count(self):
        self.log('land-topic/1/out.log', 'NOT LANDED: the merged gate failed\n')
        self.assertEqual(self.look()[0], landed.FAILED)
        self.log('land-topic/2/out.log', 'STUCK: push failed\n', mtime=4000000000)
        self.assertEqual(self.look()[0], landed.FAILED)
        self.log('land-topic/3/out.log', 'LANDED topic on main\n', mtime=4000000100)
        self.assertEqual(self.look()[0], landed.NOT_YET)    # the push is what decides LANDED

    def test_a_conflict_is_failed(self):
        self.log('land-topic/1/out.log', 'CONFLICT topic with main %s: the author merges main\n'
                 % ('a' * 40))
        code, out = self.look()
        self.assertEqual(code, landed.FAILED, out)
        self.assertIn('verdict    CONFLICT topic with main', out)

    def test_the_newest_log_with_a_verdict_wins(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n', mtime=4000000000)
        self.log('topic/2/gate.log', 'GATE OK\n', mtime=4000000100)
        self.log('topic/3/build.log', 'no verdict in this one\n', mtime=4000000200)
        self.assertEqual(self.look()[0], landed.NOT_YET)
        self.log('topic/4/gate.log', 'GATE OK\nretry\nGATE FAILED: y\n', mtime=4000000300)
        self.assertEqual(self.look()[0], landed.FAILED)     # the last match in the file

    def test_a_failure_older_than_the_tip_is_stale(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n', mtime=1000)
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('older than the tip: stale', out)

    def test_another_branchs_failure_is_not_read(self):
        self.log('other/1/gate.log', 'GATE FAILED: x\n')
        self.log('flat-other.log', 'GATE FAILED: x\n')
        self.assertEqual(self.look()[0], landed.NOT_YET)

    def test_a_line_that_only_mentions_a_verdict_is_not_one(self):
        self.log('topic/1/gate.log', 'echo GATE FAILED: x\n  GATE FAILED: indented\n')
        self.assertEqual(self.look()[0], landed.NOT_YET)

    def test_a_flat_log_dir_is_matched_by_file_name(self):
        self.log('land-topic-20261003.log', 'GATE FAILED: x\n')
        self.assertEqual(self.look()[0], landed.FAILED)

    def test_a_branch_with_a_slash_uses_the_gates_folder_name(self):
        self.git('switch', '-q', '-c', 'feat/x')
        self.log('feat-x/1/gate.log', 'GATE FAILED: x\n')
        self.assertEqual(self.look('feat/x')[0], landed.FAILED)

    def test_a_sha_names_no_branch_log(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n')
        code, out = self.look(self.git('rev-parse', 'HEAD'))
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('not read: a sha names no branch log', out)

    def test_landed_wins_over_an_old_failure(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n')
        self.land_locally()
        self.push()
        self.assertEqual(self.look()[0], landed.LANDED)


class TheDefaultLogDirIsTheGates(Fixture):
    """No "landing".log_dir: `<temp>/<repo name>-gate`, as kitlib.gate.runner.log_base; with no
    "repo".name the name is read from the remote's URL (docs/kit-json.md "With no file")."""

    def gate_dir(self, base=None):
        return os.path.join(base or self.temp, '%s-gate' % gitdefaults.repo_name(self.root))

    def test_the_temp_repo_gate_folder(self):
        base = self.gate_dir()
        self.log('topic/1/gate.log', 'GATE FAILED: x\n', base=base)
        code, out = self.look()
        self.assertEqual(code, landed.FAILED, out)
        self.assertIn(base, out)

    def test_repo_name_names_the_folder(self):
        self.config(repo={'name': 'Kit Repo'})
        base = os.path.join(self.temp, 'Kit-Repo-gate')
        self.log('topic/1/gate.log', 'GATE FAILED: x\n', base=base)
        self.assertEqual(self.look()[0], landed.FAILED)

    def test_a_kit_temp_off_the_shared_temp_is_read_and_so_is_the_shared_temp(self):
        """A checkout on its own drive logs to that drive's kit temp; a gate that could not make
        its folder there, or ran before, logged to the shared temp: both are read."""
        devdrive = os.path.join(self.tmp, 'devdrive')
        with mock.patch.object(kuser, 'work_temp', return_value=devdrive):
            self.log('topic/1/gate.log', 'GATE FAILED: x\n', base=self.gate_dir(devdrive))
            code, out = self.look()
            self.assertEqual(code, landed.FAILED, out)
            self.assertIn(self.gate_dir(devdrive), out)
            os.remove(os.path.join(self.gate_dir(devdrive), 'topic', '1', 'gate.log'))
            self.log('topic/2/gate.log', 'GATE FAILED: y\n', base=self.gate_dir())
            code, out = self.look()
            self.assertEqual(code, landed.FAILED, out)
            self.assertIn(self.gate_dir(), out)

    def test_a_machine_config_that_cannot_be_read_still_answers_from_the_shared_temp(self):
        """work_temp refuses a bad machine config; landed reads the shared temp's base and says
        why, instead of a traceback that exits 1 (NOT_YET) for ever."""
        machine_config(self.tmp, '{"temp_dir": "relative"}\n', case=self)
        self.log('topic/1/gate.log', 'GATE FAILED: x\n', base=self.gate_dir())
        code, out = self.look()
        self.assertEqual(code, landed.FAILED, out)
        self.assertIn('note       gate logs:', out)
        self.assertIn('temp_dir must be an absolute path', out)

    def test_a_worktree_gates_logs_are_read_from_the_main_checkout(self):
        self.git('switch', '-q', self.MAIN)
        wt = os.path.realpath(self.fx.worktree('wt', branch='feat/y'))
        self.commit('y.txt', 'y work', cwd=wt)
        self.log('feat-y/1/gate.log', 'GATE FAILED: x\n', base=self.gate_dir())
        self.assertEqual(self.look('feat/y')[0], landed.FAILED)
        self.assertEqual(self.look('feat/y', root=wt)[0], landed.FAILED)

    def test_logs_under_the_folder_name_are_still_read(self):
        """Before the name was read from the remote's URL, a gate with no "repo".name logged under
        its checkout's folder name: those logs are read too."""
        self.git('switch', '-q', self.MAIN)
        wt = os.path.realpath(self.fx.worktree('wt', branch='feat/y'))
        self.commit('y.txt', 'y work', cwd=wt)
        self.log('feat-y/1/gate.log', 'GATE FAILED: x\n', base=os.path.join(self.temp, 'wt-gate'))
        self.assertEqual(self.look('feat/y')[0], landed.FAILED)
        self.log('topic/1/gate.log', 'GATE FAILED: x\n',
                 base=os.path.join(self.temp, '%s-gate' % os.path.basename(self.root)))
        self.assertEqual(self.look()[0], landed.FAILED)


class TheLandingLock(Fixture):

    def setUp(self):
        super().setUp()
        self.config(landing={'log_dir': LOGS})
        self.lock = landing.LandingLock.for_config(self.root, load(self.root))

    def test_a_free_lock_is_shown(self):
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('lock       free (%s)' % self.lock.path, out)

    def test_a_held_lock_turns_a_failure_into_not_yet(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n')
        record = self.lock.take('full', ['topic'])
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('lock       landing lock: HELD - lane full, branches', out)
        self.assertIn('NOT YET (a landing holds the lock)', out)
        self.lock.release(record['token'])
        code, out = self.look()
        self.assertEqual(code, landed.FAILED, out)
        self.assertIn('lock       free', out)

    def test_a_file_that_is_no_lock_record_is_held(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n')
        with open(self.lock.path, 'w', encoding='utf-8') as fh:
            fh.write('lander-7 phase=gate\n')
        code, out = self.look()
        self.assertEqual(code, landed.NOT_YET, out)
        self.assertIn('lock       HELD: %s cannot be read' % self.lock.path, out)

    def test_the_lock_is_the_common_dirs_from_every_worktree(self):
        self.log('topic/1/gate.log', 'GATE FAILED: x\n')
        self.config(landing={'log_dir': LOGS, 'lock_file': 'git:repo-landing.lock'})
        lock = landing.LandingLock.for_config(self.root, load(self.root))
        record = lock.take('full', ['topic'])
        self.addCleanup(lock.release, record['token'])
        linked = os.path.join(self.tmp, 'linked')
        self.git('worktree', 'add', '-q', '--detach', linked)
        with open(os.path.join(linked, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump({'landing': {'log_dir': LOGS, 'lock_file': 'git:repo-landing.lock'}}, fh)
        for root in (self.root, linked):
            code, out = self.look(root=root)
            self.assertEqual(code, landed.NOT_YET, out)
            self.assertIn('landing lock: HELD - lane full', out)


class TheGateOkRecord(Fixture):
    """Information only: whether the tip has a gate-OK record (kitlib.records.gate_ok)."""

    def test_none_and_a_record(self):
        tip = self.git('rev-parse', 'HEAD')
        code, out = self.look()
        self.assertIn('gate-ok    none for %s' % tip[:12], out)
        gate_ok.record_pass(self.root, tip, {}, {'version': 'test'}, 'repo', gate_ok.utc(), [])
        code2, out = self.look()
        self.assertEqual(code2, code, out)
        self.assertIn('gate-ok    a gate-OK record for %s' % tip[:12], out)


class TheConfigRefusesWhatItCannotUse(Fixture):

    def refused(self, **sections):
        self.config(**sections)
        code, out = self.look()
        self.assertEqual(code, landed.CANNOT, out)
        self.assertIn('landed: refused: kit configuration:', out)
        return out

    def test_an_unknown_key(self):
        self.assertIn('wait', self.refused(landing={'wait': '60'}))
        self.assertIn('verdict_pattern', self.refused(landing={'verdict_pattern': '^GATE FAILED'}))

    def test_a_non_string_value(self):
        self.assertIn('log_dir', self.refused(landing={'log_dir': 3}))
        self.assertIn('lock_file', self.refused(landing={'lock_file': 3}))

    def test_a_log_dir_outside_the_repository(self):
        self.assertIn('log_dir', self.refused(landing={'log_dir': os.path.join(self.tmp, 'logs')}))
        self.assertIn('log_dir', self.refused(landing={'log_dir': '../logs'}))

    def test_a_lock_file_that_is_not_one_file_in_the_git_dir(self):
        self.assertIn('must be git:<name>', self.refused(landing={'lock_file': 'landing.lock'}))
        for bad in ('git:', 'git:../escape.lock'):
            self.assertIn('lock', self.refused(landing={'lock_file': bad}))


class TheCommandLine(Fixture):

    def test_exit_codes_and_minus_c(self):
        code, out = self.cli(self.BRANCH)
        self.assertEqual(code, 1, out)
        self.assertIn('NOT YET', out)
        code, out = self.cli('-C', self.root, 'no-such-branch', cwd=self.tmp)
        self.assertEqual(code, 2, out)
        self.assertIn('CANNOT ANSWER', out)
        code, out = self.cli('-C', os.path.join(self.root, 'sub-missing'), self.BRANCH, cwd=self.tmp)
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: -C:', out)
        self.land_locally()
        self.push()
        code, out = self.cli('-C', self.root, self.BRANCH, cwd=self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('LANDED', out)

    def test_usage(self):
        for args in ([], ['--wait', 'topic'], ['']):
            code, out = self.cli(*args)
            self.assertEqual(code, 2, (args, out))
            self.assertIn('USAGE:', out)
        code, out = self.cli(self.BRANCH, cwd=self.tmp)
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: -C:', out)


if __name__ == '__main__':
    unittest.main()
