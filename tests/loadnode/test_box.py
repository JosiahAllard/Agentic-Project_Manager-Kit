"""kitlib.loadnode.box: the node's side of `node.py run`. Its parameters are checked before anything
is done, a start is refused at the node's most jobs, the runner always leaves a result, follow
streams the log then the result, fetch sends only regular files inside the worktree, and clean
refuses a live job. git, the detached start and process liveness are stand-ins, but for one class
on real git (a bare mirror, a push to it and a worktree from it)."""
import datetime
import hashlib
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib.loadnode import box, frames
from kitlib.lock.store import LockError
from tests import gitfixture
from tests.realgit import real_git

SHA = 'a' * 40
JOB = 'j-20261028T120000Z-abcdef'
JOB2 = 'j-20261028T120001Z-012345'


def spec(**over):
    d = {'job': JOB, 'repo': 'my-repo', 'slug': 'my-slug', 'kind': 'engine-test', 'sha': SHA,
         'cmd': ['python', '-c', 'print(1)'], 'artifacts': ['out/*.txt'], 'max_jobs': 1,
         'wait_s': None, 'expect_s': None, 'no_wait': False, 'note': None}
    d.update(over)
    return d


class Git(object):
    """git's stand-in: records each call; rev-parse answers `head`; worktree add makes the folder."""

    def __init__(self, head=SHA, fail=()):
        self.calls, self.head, self.fail = [], head, fail

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        verb = args[2] if args[0] == '--git-dir' else args[0]
        if verb in self.fail:
            return 1, b'', 'fatal: no'
        if verb == 'rev-parse':
            return 0, (self.head + '\n').encode(), ''
        if verb == 'worktree' and args[3] == 'add':
            os.makedirs(args[-2])
        if verb == 'init':
            os.makedirs(args[-1])
        return 0, b'', ''


class Base(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-box-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.live = set()
        self.git = Git()
        self.started = []
        self.box = box.Box(self.base, runner=['py', 'loadnode.py'], git=self.git,
                           alive=lambda pid, job: job in self.live, detach=self.detach)

    def detach(self, argv, cwd, env):
        self.started.append((argv, cwd, env))
        return 4242, None

    def mirror(self, repo='my-repo'):
        os.makedirs(self.box.mirror(repo), exist_ok=True)

    def made_job(self, job=JOB, live=False, result=None, log=b'', **over):
        d = self.box.job_dir(job)
        os.makedirs(os.path.join(d, 'wt'))
        box._write_json(os.path.join(d, 'spec.json'), spec(job=job, **over))
        box._write_json(os.path.join(d, 'pid.json'), {'pid': 77})
        with open(os.path.join(d, 'log'), 'wb') as f:
            f.write(log)
        if result is not None:
            box._write_json(os.path.join(d, 'result.json'), result)
        if live:
            self.live.add(job)
        return d


class Params(unittest.TestCase):

    def test_a_good_spec_reads_and_every_bad_key_is_named_without_quoting(self):
        self.assertEqual(box.check_spec(spec())['cmd'], ['python', '-c', 'print(1)'])
        for k, v in (('job', 'j-1'), ('job', '../x'), ('repo', '..'), ('repo', 'a/b'),
                     ('slug', ''), ('kind', 'Engine'), ('sha', 'HEAD'), ('sha', 'a' * 39),
                     ('cmd', []), ('cmd', 'python x'), ('cmd', ['']), ('cmd', ['a\x00b']),
                     ('cmd', ['x'] * 257), ('artifacts', ['../SECRET']), ('artifacts', ['/etc/x']),
                     ('artifacts', ['.git/config']), ('artifacts', ['C:/x']), ('artifacts', 'x'),
                     ('artifacts', ['a\\b']), ('max_jobs', 0), ('max_jobs', True), ('wait_s', -1),
                     ('wait_s', 'SECRET'), ('no_wait', 'SECRET'), ('note', 'n' * 201)):
            with self.subTest(key=k, value=str(v)[:20]):
                with self.assertRaises(box.SpecError) as cm:
                    box.check_spec(spec(**{k: v}))
                self.assertIn(k, str(cm.exception))
                self.assertNotIn('SECRET', str(cm.exception))

    def test_globs(self):
        for g in ('out/*.txt', '**/*.bin', 'a.log', 'build/x-1/report.html'):
            self.assertTrue(box.glob_ok(g), g)
        for g in ('', '/x', '\\x', '..', 'a/../b', 'a//b', './a', '.git', 'x/.git/y', 'C:x',
                  'a\x01', 'x' * 201, None):
            self.assertFalse(box.glob_ok(g), g)

    def test_a_glob_is_ascii_only(self):
        """An artifact path is ASCII only (job.artifact_path_ok), so a glob that is not could
        match nothing the node sends back: main refuses it before the run."""
        for g in ('out/caf%s.txt' % chr(0xe9), '%s%s/*' % (chr(0x6587), chr(0x4ef6)),
                  'x%s.bin' % chr(0x200b)):
            self.assertFalse(box.glob_ok(g), g)

    def test_parameters_are_one_capped_json_object_line(self):
        self.assertEqual(box.read_params(io.BytesIO(b'{"job": 1}\n')), {'job': 1})
        for data in (b'', b'{"a":1}\n{"b":2}\n', b'[1]\n', b'\xff\n', b'{' + b' ' * box.SPEC_CAP):
            with self.subTest(data=data[:10]):
                with self.assertRaises(box.SpecError):
                    box.read_params(io.BytesIO(data))


class Serve(Base):

    def serve(self, cmd, stdin=b'', exec_fn=None):
        out = io.BytesIO()
        code = box.serve(cmd, io.BytesIO(stdin), out.write, self.box, exec_fn)
        return code, out.getvalue()

    def test_only_the_one_receive_pack_form_runs_git(self):
        ran = []
        code, _ = self.serve("git-receive-pack 'pmkit-node/mirrors/my-repo.git'", exec_fn=ran.append)
        self.assertEqual(code, 0)
        self.assertEqual(ran, [['git', 'receive-pack', self.box.mirror('my-repo')]])
        hooks = os.path.join(self.base, 'hooks-empty')
        self.assertIn(['--git-dir', self.box.mirror('my-repo'), 'config', 'core.hooksPath', hooks],
                      self.git.calls)
        self.assertIn(['--git-dir', self.box.mirror('my-repo'), 'config', 'receive.fsckObjects',
                       'true'], self.git.calls)
        for cmd in ("git-receive-pack 'pmkit-node/mirrors/../x.git'",
                    "git-receive-pack 'pmkit-node/mirrors/a..b.git'",
                    "git-receive-pack '/srv/x/pmkit-node/mirrors/r.git'",
                    "git-upload-pack 'pmkit-node/mirrors/my-repo.git'",
                    "git-receive-pack 'pmkit-node/mirrors/my-repo.git' x",
                    "git-receive-pack pmkit-node/mirrors/my-repo.git",
                    "git-receive-pack 'pmkit-node/mirrors/my repo.git'",
                    'start x', 'Start', 'follow;id', 'status x', 'run'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.serve(cmd, exec_fn=ran.append)[0])
        self.assertEqual(len(ran), 1)

    def test_bad_parameters_are_refused_before_anything_is_done(self):
        code, out = self.serve('start', json.dumps(spec(sha='HEAD')).encode())
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)['ok'])
        code, out = self.serve('follow', b'{"job": "../../etc"}')
        self.assertEqual(code, 2)
        self.assertEqual(frames.Decoder().feed(out)[0][0], frames.ERROR)
        self.assertEqual((self.git.calls, self.started), ([], []))

    def test_a_start_the_spec_check_refuses_drops_the_pushed_ref(self):
        # main pushes before start: a note over 200 characters or a wait over 7 days is refused
        # here after the push, and nothing else would ever remove refs/node/<job>
        drop = ['--git-dir', self.box.mirror('my-repo'), 'update-ref', '-d', 'refs/node/' + JOB]
        self.mirror()
        for over in ({'note': 'n' * 201}, {'wait_s': box.MAX_WAIT_S + 1}):
            with self.subTest(over=list(over)):
                self.git.calls[:] = []
                code, out = self.serve('start', json.dumps(spec(**over)).encode())
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(out)['ok'])
                self.assertEqual(self.git.calls, [drop])
        self.git.calls[:] = []
        self.made_job(JOB, result={'exit': 0})
        self.assertEqual(self.serve('start', json.dumps(spec(note='n' * 201)).encode())[0], 2)
        self.assertEqual(self.git.calls, [])
        for bad in (spec(job='../x', note='n' * 201), spec(repo='..', note='n' * 201)):
            self.assertEqual(self.serve('start', json.dumps(bad).encode())[0], 2)
        self.assertEqual((self.git.calls, self.started), ([], []))

    def test_start_then_status_counts_it(self):
        self.mirror()
        code, out = self.serve('start', json.dumps(spec()).encode())
        self.assertEqual((code, json.loads(out)), (0, {'ok': True, 'job': JOB, 'pid': 4242}))
        self.live.add(JOB)
        self.assertEqual(self.box.live_count(), 1)


class Start(Base):

    def test_it_starts_the_runner_detached_in_the_worktree_at_the_sha(self):
        self.mirror()
        got = self.box.start(box.check_spec(spec()), env={'PATH': '/bin', 'MACHINE_LOCK_RUN': 'x',
                                                          'MACHINE_LOCK_DETACHED': '1'})
        self.assertTrue(got['ok'], got)
        wt = os.path.join(self.box.job_dir(JOB), 'wt')
        self.assertIn(['--git-dir', self.box.mirror('my-repo'), 'worktree', 'add', '--detach',
                       '--quiet', wt, SHA], self.git.calls)
        argv, cwd, env = self.started[0]
        self.assertEqual(argv, ['py', 'loadnode.py', 'job', JOB, '--base', self.base])
        self.assertEqual((cwd, env), (wt, {'PATH': '/bin'}))
        self.assertEqual(box._read_json(os.path.join(self.box.job_dir(JOB), 'pid.json'))['pid'], 4242)

    def test_the_runner_gets_no_ssh_original_command(self):
        # loadnode.py runs as the job runner only when sshd's SSH_ORIGINAL_COMMAND is not set; the
        # runner inherits the env of the `start` call, which has it.
        self.mirror()
        got = self.box.start(box.check_spec(spec()), env={'PATH': '/bin',
                                                          'SSH_ORIGINAL_COMMAND': 'start'})
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.started[0][2], {'PATH': '/bin'})

    def test_a_ref_that_is_not_the_sha_is_refused(self):
        self.mirror()
        self.git.head = 'b' * 40
        got = self.box.start(box.check_spec(spec()))
        self.assertFalse(got['ok'])
        self.assertIn('not the commit', got['error'])
        self.assertEqual(self.started, [])

    def test_no_mirror_is_refused(self):
        self.assertIn('push first', self.box.start(box.check_spec(spec()))['error'])

    def test_the_most_jobs_refuses_busy_and_starts_nothing(self):
        self.mirror()
        self.made_job(JOB2, live=True)
        got = self.box.start(box.check_spec(spec(max_jobs=1)))
        self.assertEqual((got['ok'], got.get('busy'), got.get('jobs')), (False, True, 1))
        self.assertFalse(os.path.exists(self.box.job_dir(JOB)))
        self.assertEqual(self.started, [])
        self.assertTrue(self.box.start(box.check_spec(spec(max_jobs=2)))['ok'])

    def test_a_start_refused_before_its_folder_drops_the_pushed_ref(self):
        # sweep() walks job folders only: a ref left by a refused start would stay forever
        drop = ['--git-dir', self.box.mirror('my-repo'), 'update-ref', '-d', 'refs/node/' + JOB]
        self.mirror()
        self.made_job(JOB2, live=True)
        self.assertTrue(self.box.start(box.check_spec(spec(max_jobs=1))).get('busy'))
        self.assertIn(drop, self.git.calls)
        self.git.calls[:] = []
        self.git.head = 'b' * 40
        self.assertIn('not the commit', self.box.start(box.check_spec(spec(max_jobs=2)))['error'])
        self.assertIn(drop, self.git.calls)

    def test_a_refused_start_of_a_job_that_exists_keeps_its_ref(self):
        self.mirror()
        self.made_job(JOB, result={'exit': 0})
        self.assertIn('exists already', self.box.start(box.check_spec(spec()))['error'])
        self.git.head = 'b' * 40
        self.box.start(box.check_spec(spec()))
        self.assertNotIn('update-ref', [c[2] for c in self.git.calls if c[0] == '--git-dir'])

    def test_an_ended_job_does_not_count(self):
        self.mirror()
        self.made_job(JOB2, live=True, result={'exit': 0})
        self.assertTrue(self.box.start(box.check_spec(spec(max_jobs=1)))['ok'])

    def test_a_runner_that_cannot_start_leaves_a_result(self):
        self.mirror()
        self.box.detach = lambda argv, cwd, env: (None, 'no python')
        got = self.box.start(box.check_spec(spec()))
        self.assertIn('no python', got['error'])
        res = box._read_json(os.path.join(self.box.job_dir(JOB), 'result.json'))
        self.assertEqual((res['exit'], res['how']), (None, 'not started'))

    def test_a_job_id_used_already_is_refused(self):
        self.mirror()
        self.made_job(JOB, result={'exit': 0})
        self.assertIn('exists already', self.box.start(box.check_spec(spec()))['error'])

    def test_old_ended_jobs_are_swept_at_a_start(self):
        self.mirror()
        old = self.made_job(JOB2, result={'exit': 0})
        self.box.clock = lambda: time.time() + box.SWEEP_S + 60
        self.box.start(box.check_spec(spec()))
        self.assertFalse(os.path.exists(old))
        self.assertIn(['--git-dir', self.box.mirror('my-repo'), 'update-ref', '-d',
                       'refs/node/' + JOB2], self.git.calls)


class Follow(Base):

    def follow(self, job=JOB, offset=0, **kw):
        out = io.BytesIO()
        code = self.box.follow(job, offset, out.write, **kw)
        return code, frames.Decoder().feed(out.getvalue())

    def test_the_log_then_the_result(self):
        self.made_job(log=b'hello\n', result={'exit': 3, 'how': 'ended'})
        code, got = self.follow()
        self.assertEqual(code, 0)
        self.assertEqual([k for k, _ in got], ['L', 'R', 'E'])
        self.assertEqual(got[0][1], b'hello\n')
        self.assertEqual(json.loads(got[1][1]), {'exit': 3, 'how': 'ended', 'offset': 6})

    def test_it_starts_at_the_offset(self):
        self.made_job(log=b'0123456789', result={'exit': 0})
        _, got = self.follow(offset=4)
        self.assertEqual(got[0], ('L', b'456789'))

    def test_it_waits_for_more_and_sends_keep_alives(self):
        d = self.made_job(live=True, log=b'a')
        t = [0.0]
        steps = []

        def sleep(s):
            t[0] += 20
            steps.append(s)
            if len(steps) == 2:
                with open(os.path.join(d, 'log'), 'ab') as f:
                    f.write(b'b')
                box._write_json(os.path.join(d, 'result.json'), {'exit': 0})
                self.live.discard(JOB)
        _, got = self.follow(sleep=sleep, clock=lambda: t[0])
        self.assertEqual([k for k, _ in got], ['L', 'K', 'L', 'R', 'E'])

    def test_a_runner_gone_with_no_result_ends_as_vanished(self):
        self.made_job(log=b'x')
        _, got = self.follow()
        self.assertEqual(json.loads(got[-2][1])['how'], 'vanished')

    def test_an_unknown_job_is_an_error_frame(self):
        code, got = self.follow()
        self.assertEqual((code, got[0][0]), (1, 'X'))


class Fetch(Base):

    def test_regular_files_inside_the_worktree_only(self):
        d = self.made_job(artifacts=['out/*.txt', 'top.bin', 'missing/*'], result={'exit': 0})
        wt = os.path.join(d, 'wt')
        os.makedirs(os.path.join(wt, 'out', 'sub.txt'))       # a folder that matches
        for rel, data in (('out/a.txt', b'A' * 3), ('out/b.txt', b''), ('top.bin', b'\x00\x01')):
            with open(os.path.join(wt, *rel.split('/')), 'wb') as f:
                f.write(data)
        outside = os.path.join(self.base, 'secret.txt')
        with open(outside, 'wb') as f:
            f.write(b'SECRET')
        try:
            os.symlink(outside, os.path.join(wt, 'out', 'link.txt'))
        except (OSError, NotImplementedError):
            pass
        out = io.BytesIO()
        self.assertEqual(self.box.fetch(JOB, out.write), 0)
        got = frames.Decoder().feed(out.getvalue())
        man = json.loads(got[0][1])
        self.assertEqual([f['path'] for f in man['files']], ['out/a.txt', 'out/b.txt', 'top.bin'])
        self.assertEqual(man['files'][0]['sha256'], hashlib.sha256(b'AAA').hexdigest())
        body = b''.join(p for k, p in got if k == 'F')
        self.assertEqual(body, b'AAA\x00\x01')
        self.assertNotIn(b'SECRET', out.getvalue())
        self.assertEqual(got[-1][0], 'E')

    def test_too_many_bytes_is_an_empty_manifest_with_its_error(self):
        d = self.made_job(artifacts=['*.bin'], result={'exit': 0})
        with open(os.path.join(d, 'wt', 'x.bin'), 'wb') as f:
            f.write(b'12345')
        out = io.BytesIO()
        orig = box.MAX_BYTES
        box.MAX_BYTES = 4
        try:
            self.box.fetch(JOB, out.write)
        finally:
            box.MAX_BYTES = orig
        got = frames.Decoder().feed(out.getvalue())
        man = json.loads(got[0][1])
        self.assertEqual(man['files'], [])
        self.assertIn('over', man['error'])
        self.assertEqual([k for k, _ in got], ['M', 'E'])


class CleanAndStop(Base):

    def test_clean_refuses_a_live_job_and_removes_an_ended_one(self):
        d = self.made_job(live=True)
        self.mirror()
        self.assertIn('still running', self.box.clean(JOB)['error'])
        self.assertTrue(os.path.isdir(d))
        self.live.discard(JOB)
        box._write_json(os.path.join(d, 'result.json'), {'exit': 0})
        self.assertEqual(self.box.clean(JOB), {'ok': True, 'gone': True})
        self.assertFalse(os.path.exists(d))
        self.assertIn(['--git-dir', self.box.mirror('my-repo'), 'worktree', 'remove', '--force',
                       os.path.join(d, 'wt')], self.git.calls)
        self.assertEqual(self.box.clean(JOB), {'ok': True, 'gone': True})

    def test_stop_asks_the_lock_run_else_the_runner(self):
        d = self.made_job(live=True)
        box._write_json(os.path.join(d, 'run.json'), {'rid': 'r-x'})
        asked, killed = [], []
        got = self.box.stop(JOB, request_stop=lambda rid: asked.append(rid) or True,
                            kill=lambda *a: killed.append(a))
        self.assertEqual((got['ok'], asked, killed), (True, ['r-x'], []))
        self.assertTrue(os.path.exists(os.path.join(d, 'stop.json')))
        os.remove(os.path.join(d, 'run.json'))
        got = self.box.stop(JOB, request_stop=lambda rid: True, kill=lambda *a: killed.append(a))
        self.assertEqual((got['ok'], killed[0][0]), (True, 77))

    def test_stop_of_an_ended_job_does_nothing(self):
        self.made_job(result={'exit': 0})
        self.assertEqual(self.box.stop(JOB, kill=self.fail), {'ok': True, 'ended': True})


class RunJob(Base):

    def run_job(self, run_command, stop=False):
        d = self.made_job()
        if stop:
            box._write_json(os.path.join(d, 'stop.json'), {})
        seen = {}

        def rc(cmd, kind, repo, slug, **kw):
            seen.update(cmd=cmd, kind=kind, repo=repo, slug=slug, **kw)
            return run_command()
        code = self.box.run_job(JOB, run_command=rc, new_rid=lambda: 'r-1',
                                store_factory=lambda say: ('store', say), chdir=seen.setdefault)
        return code, box._read_json(os.path.join(d, 'result.json')), seen, d

    def test_it_runs_under_the_lock_and_writes_its_result(self):
        code, res, seen, d = self.run_job(lambda: 3)
        self.assertEqual(code, 3)
        self.assertEqual((res['exit'], res['how'], res['sha']), (3, 'ended', SHA))
        self.assertEqual((seen['cmd'], seen['kind'], seen['repo'], seen['rid']),
                         (['python', '-c', 'print(1)'], 'engine-test', 'my-repo', 'r-1'))
        self.assertEqual(seen['env']['PMKIT_NODE_JOB'], JOB)
        self.assertFalse(any(k.startswith('MACHINE_LOCK_') for k in seen['env']))
        self.assertEqual(box._read_json(os.path.join(d, 'run.json')), {'rid': 'r-1'})

    def test_a_busy_lock_is_how_busy(self):
        def busy():
            raise LockError(6, 'busy: no slot now')
        code, res, _seen, d = self.run_job(busy)
        self.assertEqual((code, res['how'], res['exit']), (6, 'busy', 6))
        with open(os.path.join(d, 'log'), encoding='utf-8') as f:
            self.assertIn('busy: no slot now', f.read())

    def test_a_stopped_run_is_how_stopped(self):
        _c, res, _s, _d = self.run_job(lambda: 1, stop=True)
        self.assertEqual(res['how'], 'stopped')

    def test_a_failed_runner_still_leaves_a_result(self):
        def boom():
            raise RuntimeError('x')
        code, res, _s, _d = self.run_job(boom)
        self.assertEqual((code, res['how']), (1, 'error'))

    def test_ctrl_c_from_sigterm_is_how_interrupted(self):
        def interrupted():
            raise KeyboardInterrupt()
        code, res, _s, _d = self.run_job(interrupted)
        self.assertEqual((code, res['how']), (130, 'interrupted'))


@real_git('git receive-pack and worktrees of a bare mirror: the fake lacks both')
class OnRealGit(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.base = tempfile.mkdtemp(prefix='kit-box-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.box = box.Box(self.base, runner=['py'], alive=lambda pid, job: False,
                           detach=lambda argv, cwd, env: (99, None))

    def test_a_push_to_the_mirror_then_a_worktree_at_it_then_clean(self):
        self.repo.write('hello.txt', 'hi\n')
        sha = self.repo.commit('add hello', 'hello.txt')
        m = self.box.mirror_for_push('my-repo')
        self.repo.git('push', '-q', m, '%s:refs/node/%s' % (sha, JOB))
        got = self.box.start(box.check_spec(spec(sha=sha)))
        self.assertTrue(got['ok'], got)
        wt = os.path.join(self.box.job_dir(JOB), 'wt')
        with open(os.path.join(wt, 'hello.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'hi\n')
        box._write_json(os.path.join(self.box.job_dir(JOB), 'result.json'), {'exit': 0})
        self.assertTrue(self.box.clean(JOB)['gone'])
        self.assertEqual(self.repo.git('--git-dir', m, 'for-each-ref', 'refs/node/'), '')
        self.assertEqual(os.path.normcase(self.repo.git('--git-dir', m, 'config', 'core.hooksPath')),
                         os.path.normcase(os.path.join(self.base, 'hooks-empty')))
        self.assertEqual(self.repo.git('--git-dir', m, 'config', 'receive.fsckObjects'), 'true')



# -- gate jobs (S3) --------------------------------------------------------------------------------

BASE = 'b' * 40


def gate_spec(**over):
    d = {'job': JOB, 'repo': 'my-repo', 'slug': 'my-slug', 'sha': SHA, 'base': BASE,
         'remote': 'origin', 'main': 'main', 'max_jobs': 1, 'note': None}
    d.update(over)
    return d


def job_epoch(job):
    return datetime.datetime.strptime(job[2:17], '%Y%m%dT%H%M%S').replace(
        tzinfo=datetime.timezone.utc).timestamp()


class RefGit(Git):
    """Git's stand-in that answers rev-parse per ref (`refs`, else `head`; None: no such ref) and
    for-each-ref with `listed`."""

    def __init__(self, refs=None, listed=b'', **kw):
        Git.__init__(self, **kw)
        self.refs, self.listed = dict(refs or {}), listed

    def __call__(self, args, cwd=None):
        verb = args[2] if args[0] == '--git-dir' else args[0]
        if verb == 'rev-parse':
            self.calls.append(list(args))
            got = self.refs.get(args[-1][:-len('^{commit}')], self.head)
            return (1, b'', '') if got is None else (0, (got + '\n').encode(), '')
        if verb == 'for-each-ref':
            self.calls.append(list(args))
            return 0, self.listed, ''
        return Git.__call__(self, args, cwd)


class GateBase(Base):

    def setUp(self):
        Base.setUp(self)
        self.git = RefGit({'refs/node/' + JOB: SHA, 'refs/node/%s-base' % JOB: BASE})
        self.box.git = self.git

    def drops(self, job=JOB):
        m = self.box.mirror('my-repo')
        return [['--git-dir', m, 'update-ref', '-d', 'refs/node/' + job],
                ['--git-dir', m, 'update-ref', '-d', 'refs/node/%s-base' % job]]

    def made_gate_job(self, job=JOB, result=None, live=False):
        d = self.box.job_dir(job)
        os.makedirs(os.path.join(d, 'gate', 'my-repo'))
        box._write_json(os.path.join(d, 'spec.json'), box.check_gate_spec(gate_spec(job=job)))
        box._write_json(os.path.join(d, 'pid.json'), {'pid': 77})
        if result is not None:
            box._write_json(os.path.join(d, 'result.json'), result)
        if live:
            self.live.add(job)
        return d


class GateParams(unittest.TestCase):

    def test_the_gate_verb_is_served_and_runs_need_only_the_run_verbs(self):
        # a node with an older kit (no gate verb) still takes node.py run jobs
        self.assertIn(box.GATE_VERB, box.VERBS)
        self.assertNotIn(box.GATE_VERB, box.RUN_VERBS)
        self.assertEqual(box.RUN_VERBS, ('start', 'follow', 'fetch', 'clean', 'stop'))

    def test_a_good_gate_spec_reads_and_every_bad_key_is_named_without_quoting(self):
        got = box.check_gate_spec(dict(gate_spec(), cmd=['rm', '-rf', '/'], kind='x'))
        self.assertEqual((got['gate'], got['base'], got['main'], got['remote']),
                         (True, BASE, 'main', 'origin'))
        self.assertNotIn('cmd', got)                 # the node runs its own gate.py, nothing sent
        self.assertEqual(box.check_gate_spec(gate_spec(remote=''))['remote'], '')
        for k, v in (('job', 'j-1'), ('repo', '..'), ('slug', 'a/b'), ('sha', 'HEAD'),
                     ('base', 'SECRET'), ('base', 'a' * 39), ('remote', 'or igin'),
                     ('remote', '-x'), ('remote', None), ('main', '../x'), ('main', 'a..b'),
                     ('main', 'x.lock'), ('main', ''), ('main', 'a/'), ('main', 'SECRET a'),
                     ('max_jobs', 0), ('max_jobs', True), ('note', 'n' * 201)):
            with self.subTest(key=k, value=str(v)[:20]):
                with self.assertRaises(box.SpecError) as cm:
                    box.check_gate_spec(gate_spec(**{k: v}))
                self.assertIn(k, str(cm.exception))
                self.assertNotIn('SECRET', str(cm.exception))

    def test_branch_names(self):
        for name in ('main', 'master', 'release/2.x', 'dev_1.0'):
            self.assertTrue(box.branch_ok(name), name)
        for name in ('', '/main', 'a//b', 'a..b', 'x.lock', '-x', 'a b', 'a~1', 'a^', None):
            self.assertFalse(box.branch_ok(name), name)


class GateStart(GateBase):

    def test_a_gate_gets_its_own_repo_with_the_base_and_a_linked_worktree_at_the_sha(self):
        self.mirror()
        got = self.box.gate(box.check_gate_spec(gate_spec()), env={'PATH': '/bin',
                                                                   'MACHINE_LOCK_RUN': 'x'})
        self.assertTrue(got['ok'], got)
        d, m = self.box.job_dir(JOB), self.box.mirror('my-repo')
        r, tree = os.path.join(d, 'repo.git'), os.path.join(d, 'gate', 'my-repo')
        made = [c for c in self.git.calls if c[0] == 'init' or c[:2] == ['--git-dir', r]]
        self.assertEqual(made, [
            ['init', '--bare', '--quiet', r],
            ['--git-dir', r, 'config', 'core.hooksPath', os.path.join(self.base, 'hooks-empty')],
            ['--git-dir', r, 'update-ref', 'refs/heads/main', BASE],
            ['--git-dir', r, 'update-ref', 'refs/remotes/origin/main', BASE],
            ['--git-dir', r, 'worktree', 'add', '--detach', '--quiet', tree, SHA]])
        with open(os.path.join(r, 'objects', 'info', 'alternates'), encoding='utf-8') as f:
            self.assertEqual(f.read(), os.path.join(m, 'objects') + '\n')
        self.assertNotIn('worktree', [c[2] for c in self.git.calls if c[:2] == ['--git-dir', m]])
        argv, cwd, env = self.started[0]
        self.assertEqual((argv[-4:-2], cwd, env), (['job', JOB], tree, {'PATH': '/bin'}))
        self.assertIs(box._read_json(os.path.join(d, 'spec.json'))['gate'], True)

    def test_no_remote_sets_only_the_main_branch(self):
        self.mirror()
        self.assertTrue(self.box.gate(box.check_gate_spec(gate_spec(remote='')))['ok'])
        refs = [c[3] for c in self.git.calls if 'update-ref' in c]
        self.assertEqual(refs, ['refs/heads/main'])

    def test_a_base_ref_that_is_not_the_base_is_refused_and_both_refs_dropped(self):
        self.mirror()
        self.git.refs['refs/node/%s-base' % JOB] = 'c' * 40
        got = self.box.gate(box.check_gate_spec(gate_spec()))
        self.assertIn('not the base asked for', got['error'])
        self.assertFalse(os.path.exists(self.box.job_dir(JOB)))
        for drop in self.drops():
            self.assertIn(drop, self.git.calls)
        self.assertEqual(self.started, [])

    def test_a_busy_gate_drops_both_refs(self):
        self.mirror()
        self.made_job(JOB2, live=True)
        self.assertTrue(self.box.gate(box.check_gate_spec(gate_spec())).get('busy'))
        for drop in self.drops():
            self.assertIn(drop, self.git.calls)

    def test_a_start_drops_no_base_ref(self):
        self.mirror()
        self.made_job(JOB2, live=True)
        self.assertTrue(self.box.start(box.check_spec(spec(max_jobs=1))).get('busy'))
        self.assertIn(self.drops()[0], self.git.calls)
        self.assertNotIn(self.drops()[1], self.git.calls)

    def test_a_repo_that_cannot_be_made_leaves_a_result_and_starts_nothing(self):
        self.mirror()
        self.git.fail = ('init',)
        got = self.box.gate(box.check_gate_spec(gate_spec()))
        self.assertIn('could not be made', got['error'])
        res = box._read_json(os.path.join(self.box.job_dir(JOB), 'result.json'))
        self.assertEqual(res['how'], 'not started')
        self.assertEqual(self.started, [])

    def test_serve_takes_the_gate_verb_and_a_refused_gate_spec_drops_both_refs(self):
        self.mirror()
        out = io.BytesIO()
        code = box.serve('gate', io.BytesIO(json.dumps(gate_spec()).encode() + b'\n'), out.write,
                         self.box)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(json.loads(out.getvalue())['ok'])
        out = io.BytesIO()
        code = box.serve('gate', io.BytesIO(json.dumps(gate_spec(job=JOB2, base='SECRET')).encode()
                                            + b'\n'), out.write, self.box)
        self.assertEqual(code, box.EXIT_USAGE)
        self.assertNotIn(b'SECRET', out.getvalue())
        for drop in self.drops(JOB2):
            self.assertIn(drop, self.git.calls)

    def test_clean_of_a_gate_job_drops_its_base_ref_and_its_repo(self):
        self.mirror()
        d = self.made_gate_job(result={'exit': 0})
        os.makedirs(os.path.join(d, 'repo.git'))
        self.assertEqual(self.box.clean(JOB), {'ok': True, 'gone': True})
        self.assertFalse(os.path.exists(d))
        for drop in self.drops():
            self.assertIn(drop, self.git.calls)
        self.assertNotIn('remove', [c[3] for c in self.git.calls if 'worktree' in c])


class SweepRefs(GateBase):
    """A push whose start never came (main died, ssh failed before the start went out) leaves
    refs/node/<job> with no job folder; a start sweeps it once its job id is a week old."""

    def setUp(self):
        GateBase.setUp(self)
        self.mirror()
        self.made_job(JOB2, result={'exit': 0})
        self.git.listed = ('refs/node/{0}\nrefs/node/{0}-base\nrefs/node/{1}\nrefs/node/odd\n'
                           .format(JOB, JOB2).encode())

    def deleted(self):
        return [c[-1] for c in self.git.calls if 'update-ref' in c]

    def test_old_refs_no_folder_owns_are_dropped(self):
        self.box.clock = lambda: job_epoch(JOB) + box.SWEEP_S + 60
        self.box.sweep_refs()
        self.assertEqual(self.deleted(), ['refs/node/' + JOB, 'refs/node/%s-base' % JOB])

    def test_young_refs_are_kept_for_a_start_on_its_way(self):
        self.box.clock = lambda: job_epoch(JOB) + 60
        self.box.sweep_refs()
        self.assertEqual(self.deleted(), [])

    def test_a_start_sweeps_them(self):
        self.box.clock = lambda: job_epoch(JOB) + box.SWEEP_S + 60
        self.git.refs['refs/node/' + JOB2] = SHA
        self.box.start(box.check_spec(spec(job=JOB2)))
        self.assertIn('refs/node/%s-base' % JOB, self.deleted())


class FakePopen(object):

    def __init__(self, codes, interrupt=False, slow=False):
        # interrupt: True (or a count) raises KeyboardInterrupt from that many waits
        self.codes, self.interrupt, self.slow = list(codes), int(interrupt), slow
        self.signals, self.killed, self.args = [], False, None

    def __call__(self, argv, cwd=None, env=None):
        self.args = (argv, cwd, env)
        return self

    def wait(self, timeout=None):
        if self.interrupt:
            self.interrupt -= 1
            raise KeyboardInterrupt()
        if self.slow and timeout is not None and not self.killed:
            raise RuntimeError('timed out')
        return self.codes.pop(0)

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


class RunGate(GateBase):

    def run_gate(self, popen, verify=None, stop=False):
        d = self.made_gate_job()
        if stop:
            box._write_json(os.path.join(d, 'stop.json'), {})
        spec_ = box._read_json(os.path.join(d, 'spec.json'))
        seen = []

        def verify_(root, commit):
            seen.append((root, commit))
            return verify(root, commit) if verify else ({'commit': commit, 'verdict': 'PASS'}, None)
        with mock.patch.dict(os.environ, {'MACHINE_LOCK_RUN': 'r-outer', 'KEEP': '1'}):
            code = self.box.run_gate(JOB, spec_, popen=popen, verify=verify_)
        return code, box._read_json(os.path.join(d, 'result.json')), seen

    def test_it_runs_the_node_kits_own_gate_full_and_sends_the_record(self):
        p = FakePopen([0])
        code, res, seen = self.run_gate(p)
        tree = os.path.join(self.box.job_dir(JOB), 'gate', 'my-repo')
        argv, cwd, env = p.args
        self.assertEqual(argv, [sys.executable, '-B', os.path.join(box.KIT_ROOT, 'tools', 'gate.py'),
                                '-C', tree, '--full'])
        self.assertEqual(cwd, tree)
        self.assertEqual((env['PMKIT_NODE_JOB'], env['KEEP']), (JOB, '1'))
        self.assertFalse(any(k.startswith('MACHINE_LOCK_') for k in env))   # gate.py books it
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(tree, SHA)])
        self.assertEqual((res['exit'], res['how'], res['sha'], res['gate']), (0, 'ended', SHA, True))
        self.assertEqual((res['record'], res['record_why']), ({'commit': SHA, 'verdict': 'PASS'}, None))

    def test_a_pass_with_no_record_says_why(self):
        code, res, _ = self.run_gate(FakePopen([0]), verify=lambda r, c: (None, 'no gate-OK record'))
        self.assertEqual((code, res['record'], res['record_why']), (0, None, 'no gate-OK record'))

    def test_a_record_of_another_commit_is_not_sent(self):
        code, res, _ = self.run_gate(FakePopen([0]),
                                     verify=lambda r, c: ({'commit': 'c' * 40}, None))
        self.assertEqual((res['record'], res['record_why']), (None, 'the record names another commit'))

    def test_with_no_verify_a_pass_sends_no_record_and_says_why(self):
        d = self.made_gate_job()
        spec_ = box._read_json(os.path.join(d, 'spec.json'))
        code = self.box.run_gate(JOB, spec_, popen=FakePopen([0]))
        res = box._read_json(os.path.join(d, 'result.json'))
        self.assertEqual((code, res['record']), (0, None))
        self.assertIn('reads no gate-OK record', res['record_why'])

    def test_a_verify_that_raises_still_leaves_a_result(self):
        def boom(r, c):
            raise ValueError('bad json')
        _c, res, _ = self.run_gate(FakePopen([0]), verify=boom)
        self.assertIn('bad json', res['record_why'])

    def test_a_failed_gate_reads_no_record(self):
        code, res, seen = self.run_gate(FakePopen([1]))
        self.assertEqual((code, res['exit'], res['record'], seen), (1, 1, None, []))

    def test_a_stop_is_passed_on_as_ctrl_c(self):
        p = FakePopen([0], interrupt=True)
        code, res, seen = self.run_gate(p, stop=True)
        self.assertEqual(p.signals, [getattr(signal, 'SIGINT', 2)])
        self.assertEqual((code, res['exit'], res['how'], seen), (130, 130, 'stopped', []))

    def test_a_gate_that_ignores_ctrl_c_is_killed(self):
        p = FakePopen([-9], interrupt=True, slow=True)
        code, res, _ = self.run_gate(p)
        self.assertTrue(p.killed)
        self.assertEqual((code, res['how']), (-9, 'interrupted'))

    def test_a_second_stop_during_the_wait_kills_it_and_still_leaves_a_result(self):
        p = FakePopen([-9], interrupt=2)
        code, res, seen = self.run_gate(p, stop=True)
        self.assertTrue(p.killed)
        self.assertEqual(p.signals, [getattr(signal, 'SIGINT', 2)])
        self.assertEqual((code, res['exit'], res['how'], seen), (-9, -9, 'stopped', []))

    def test_a_gate_that_cannot_start_leaves_a_result(self):
        def popen(argv, cwd=None, env=None):
            raise FileNotFoundError('no python')
        code, res, _ = self.run_gate(popen)
        self.assertEqual((code, res['how']), (1, 'error'))
        self.assertIn('could not start', res['why'])

    def test_run_job_hands_a_gate_spec_to_run_gate_and_never_runs_a_command(self):
        self.made_gate_job()
        verify = object()
        with mock.patch.object(box.Box, 'run_gate', return_value=4) as rg:
            self.assertEqual(self.box.run_job(JOB, run_command=self.fail, verify=verify), 4)
        self.assertEqual(rg.call_args[0][0], JOB)
        self.assertIs(rg.call_args[1]['verify'], verify)


@real_git('a per-job bare repo with alternates and its linked worktree: the fake lacks both')
class GateOnRealGit(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.base = tempfile.mkdtemp(prefix='kit-box-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.box = box.Box(self.base, runner=['py'], alive=lambda pid, job: False,
                           detach=lambda argv, cwd, env: (99, None))

    def test_the_gate_tree_is_at_the_sha_with_the_base_as_its_default_base_then_clean(self):
        from kitlib.git import refs
        self.repo.write('a.txt', 'base\n')
        base = self.repo.commit('base', 'a.txt')
        self.repo.write('b.txt', 'work\n')
        sha = self.repo.commit('work', 'b.txt')
        m = self.box.mirror_for_push('my-repo')
        self.repo.git('push', '-q', m, '%s:refs/node/%s' % (sha, JOB),
                      '%s:refs/node/%s-base' % (base, JOB))
        got = self.box.gate(box.check_gate_spec(gate_spec(sha=sha, base=base)))
        self.assertTrue(got['ok'], got)
        tree = os.path.join(self.box.job_dir(JOB), 'gate', 'my-repo')
        with open(os.path.join(tree, 'b.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'work\n')
        self.assertEqual(refs.head(tree)[0], sha)
        self.assertEqual(refs.default_base(tree, 'origin', 'main'),
                         ('refs/remotes/origin/main', None))
        self.assertEqual(refs.resolve('refs/remotes/origin/main', tree)[0], base)
        self.assertEqual(self.repo.git('--git-dir', m, 'worktree', 'list', '--porcelain').count(
            'worktree '), 1)                          # the mirror's own: no gate tree in it
        box._write_json(os.path.join(self.box.job_dir(JOB), 'result.json'), {'exit': 0})
        self.assertTrue(self.box.clean(JOB)['gone'])
        self.assertEqual(self.repo.git('--git-dir', m, 'for-each-ref', 'refs/node/'), '')

    def test_a_ref_no_start_came_for_is_swept_a_week_on(self):
        self.repo.write('a.txt', 'x\n')
        sha = self.repo.commit('c', 'a.txt')
        m = self.box.mirror_for_push('my-repo')
        self.repo.git('push', '-q', m, '%s:refs/node/%s' % (sha, JOB))
        self.box.clock = lambda: job_epoch(JOB) + box.SWEEP_S + 60
        self.box.sweep()
        self.assertEqual(self.repo.git('--git-dir', m, 'for-each-ref', 'refs/node/'), '')


if __name__ == '__main__':
    unittest.main()
