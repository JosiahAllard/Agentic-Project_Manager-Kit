"""kitlib.loadnode.box: the node's side of `node.py run`. Its parameters are checked before anything
is done, a start is refused at the node's most jobs, the runner always leaves a result, follow
streams the log then the result, fetch sends only regular files inside the worktree, and clean
refuses a live job. git, the detached start and process liveness are stand-ins, but for one class
on real git (a bare mirror, a push to it and a worktree from it)."""
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest

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
                    "git-receive-pack '/home/x/pmkit-node/mirrors/r.git'",
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


if __name__ == '__main__':
    unittest.main()
