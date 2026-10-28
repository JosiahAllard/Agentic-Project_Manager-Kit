"""kitlib.loadnode.job: the main machine's side of `node.py run`. The link stands in for ssh but
talks to a real kitlib.loadnode.box.Box, so main and node speak the real protocol: a job's output
streams back, a dropped connection reattaches from the last whole frame, a connection that does
not come back is LOST with the lines to follow or stop it, artifacts land in the checkout only
when every one came whole and none is tracked, and the job is cleaned only then."""
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.loadnode import box, job, registry
from tests import gitfixture
from tests.loadnode.test_box import Git
from tests.loadnode.test_registry import KEY
from tests.realgit import real_git

NODE = registry.Node('box1', '192.168.1.50', 'pmnode', KEY, 2, 4096, 22, None, ('my-repo',))
SHA = 'c' * 40
JOB = 'j-20261028T120000Z-abcdef'
LS = ':(literal,icase)'


class FakeLink(object):
    """remote.Link's stand-in: each verb goes to box.serve on a real Box. `cut` holds, per
    streamed call, how many bytes to let through before the connection 'drops' (None: all)."""

    def __init__(self, bx, cut=(), fail_calls=(), garble_fetch=0):
        self.box, self.cut, self.fail_calls = bx, list(cut), set(fail_calls)
        self.garble_fetch = garble_fetch
        self.calls, self.envs = [], []

    def git_env(self, base=None):
        env = dict(base or {})
        env['GIT_SSH_COMMAND'] = 'ssh-stand-in'
        self.envs.append(env)
        return env

    def _serve(self, verb, payload):
        out = io.BytesIO()
        rc = box.serve(verb, io.BytesIO(json.dumps(payload).encode() + b'\n'), out.write, self.box)
        return rc, out.getvalue()

    def call(self, verb, payload, timeout_s=None):
        self.calls.append((verb, payload))
        if verb in self.fail_calls:
            return 255, b'', b'ssh: connect to host 192.168.1.50 port 22: Connection timed out\r\n'
        rc, out = self._serve(verb, payload)
        return rc, out, b''

    def stream(self, verb, payload, on_data, idle_s=None, clock=None):
        self.calls.append((verb, payload))
        cut = self.cut.pop(0) if self.cut else None
        if cut == 'down':
            return 255, b'ssh: connect to host 192.168.1.50 port 22: No route to host\n', False
        _rc, data = self._serve(verb, payload)
        if verb == 'fetch' and self.garble_fetch:
            self.garble_fetch -= 1
            i = data.index(b'F ')
            j = data.index(b'\n', i) + 1
            data = data[:j] + bytes([data[j] ^ 1]) + data[j + 1:]
        if cut is not None:
            data = data[:cut]
        for i in range(0, len(data), 7):                # in small pieces, as a pipe may give them
            if on_data(data[i:i + 7]) is False:
                return -9, b'', False
        return (0 if cut is None else 255), (b'' if cut is None else b'Connection reset\n'), False


class Base(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-job-node-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.top = tempfile.mkdtemp(prefix='kit-job-top-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.notices = os.path.join(self.base, 'notices')
        self.result = {'exit': 0, 'how': 'ended'}
        self.log = b'line one\nline two\n'
        self.files = {}
        self.node_git = Git(head=SHA)
        self.box = box.Box(os.path.join(self.base, 'node'), runner=['py'], git=self.node_git,
                           alive=lambda pid, j: False, detach=self.detach)
        self.said, self.output, self.pushes, self.tracked = [], bytearray(), [], set()
        self.sleeps, self.t = [], [0.0]

    def detach(self, argv, cwd, env):
        """The job 'runs' at once: its log, its files and its result."""
        j = argv[argv.index('job') + 1]
        with open(self.box._p(j, 'log'), 'wb') as f:
            f.write(self.log)
        for rel, data in self.files.items():
            full = os.path.join(cwd, *rel.split('/'))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as f:
                f.write(data)
        if self.result is not None:
            box._write_json(self.box._p(j, 'result.json'), dict(self.result, sha=SHA))
        return 4242, None

    def git(self, args, cwd, env=None, timeout=None):
        if 'push' in args:
            self.pushes.append((args, cwd, env))
            os.makedirs(self.box.mirror('my-repo'), exist_ok=True)
            return (1, b'', self.push_fails) if self.push_fails else (0, b'', '')
        if args[0] == 'ls-files':                   # as git does for :(literal,icase)
            asked = {a[len(LS):].lower() for a in args if a.startswith(LS)}
            hit = sorted(t for t in self.tracked if t.lower() in asked)
            return 0, ''.join(h + '\0' for h in hit).encode(), ''
        raise AssertionError('unexpected git %s' % args)

    push_fails = ''          # git's stderr for a push that fails

    def sleep(self, s):
        self.sleeps.append(s)
        self.t[0] += s

    def node_job(self, link=None, artifacts=(), reattach_s=60, **kw):
        self.link = link or FakeLink(self.box)
        return job.NodeJob(NODE, self.link, self.top, SHA, 'my-repo', 'my-slug', 'engine-test',
                           ['python', 'x.py'], artifacts, say=self.said.append,
                           out=self.output.extend, git=self.git, notice_dir=self.notices,
                           reattach_s=reattach_s, sleep=self.sleep, clock=lambda: self.t[0],
                           job=JOB, tool='node.py', **kw)

    def notice(self, phase):
        p = os.path.join(self.notices, '%s-%s.json' % (JOB, phase))
        if not os.path.exists(p):
            return None
        with open(p, encoding='utf-8') as f:
            return json.load(f)

    def verbs(self):
        return [v for v, _p in self.link.calls]


class Run(Base):

    def test_push_start_follow_clean_and_the_exit_code(self):
        self.result = {'exit': 3, 'how': 'ended'}
        code = self.node_job().run()
        self.assertEqual(code, 3)
        self.assertEqual(bytes(self.output), self.log)
        self.assertEqual(self.verbs(), ['start', 'follow', 'clean'])
        args, cwd, env = self.pushes[0]
        self.assertEqual(args, ['-c', 'protocol.version=0', 'push', '--quiet',
                                '192.168.1.50:pmkit-node/mirrors/my-repo.git',
                                '%s:refs/node/%s' % (SHA, JOB)])
        self.assertNotIn('--no-verify', args)
        self.assertEqual((cwd, env['GIT_SSH_COMMAND'], env['GIT_TERMINAL_PROMPT']),
                         (self.top, 'ssh-stand-in', '0'))
        start = self.link.calls[0][1]
        self.assertEqual((start['sha'], start['max_jobs'], start['cmd']), (SHA, 2, ['python', 'x.py']))
        self.assertFalse(os.path.exists(self.box.job_dir(JOB)))

    def test_notices_bracket_the_window_on_the_node(self):
        self.node_job().run()
        start, end = self.notice('start'), self.notice('end')
        self.assertEqual((start['id'], start['phase'], start['kind'], start['from']),
                         ('L-%s-start' % JOB, 'start', 'lock-window', 'load node box1'))
        self.assertIn('on node box1: my-repo/my-slug (engine-test)', start['title'])
        self.assertEqual((end['id'], end['run'], end['repo']), ('L-%s-end' % JOB, JOB, 'my-repo'))
        self.assertIn('exit 0', end['text'])

    def test_no_notice_folder_writes_none(self):
        self.notices = None
        self.assertEqual(self.node_job().run(), 0)

    def test_a_job_the_node_would_refuse_is_never_pushed(self):
        # the node checks the spec only at start, after the push: main checks it first
        for over in ({'note': 'n' * (box.NOTE_CAP + 1)}, {'wait_s': box.MAX_WAIT_S + 1}):
            with self.subTest(over=list(over)):
                nj = self.node_job(**over)
                with self.assertRaises(job.JobError) as cm:
                    nj.run()
                self.assertEqual((cm.exception.code, cm.exception.fallback), (job.EXIT_USAGE, False))
                self.assertIn(list(over)[0], cm.exception.message)
                self.assertEqual((self.pushes, self.link.calls, nj.started), ([], [], False))

    def test_a_push_that_fails_falls_back_and_starts_nothing(self):
        self.push_fails = 'fatal: the remote end hung up'
        with self.assertRaises(job.JobError) as cm:
            self.node_job().run()
        self.assertEqual((cm.exception.code, cm.exception.fallback), (job.EXIT_NODE, True))
        self.assertIn('hung up', cm.exception.message)
        self.assertEqual(self.verbs(), [])
        self.assertIsNone(self.notice('start'))

    def test_a_failed_push_names_its_cause_not_gits_closing_lines(self):
        tail = ('fatal: Could not read from remote repository.' + chr(10) + chr(10)
                + 'Please make sure you have the correct access rights' + chr(10)
                + 'and the repository exists.' + chr(10))
        for err, want in (('Connection timed out during banner exchange' + chr(10) + tail,
                           'Connection timed out during banner exchange'),
                          ('Host key verification failed.' + chr(10) + tail,
                           "the node's host key is not the one in the registry"),
                          (tail, 'Could not read from remote repository.')):
            self.push_fails = err
            with self.assertRaises(job.JobError) as cm:
                self.node_job().run()
            self.assertEqual(cm.exception.message, 'the push to box1 failed: ' + want)

    def test_a_node_at_its_most_jobs_is_busy_and_falls_back(self):
        for j in ('j-20261028T110000Z-000001', 'j-20261028T110000Z-000002'):
            os.makedirs(self.box.job_dir(j))
            box._write_json(self.box._p(j, 'pid.json'), {'pid': 1})
        self.box.alive = lambda pid, j: True
        with self.assertRaises(job.JobError) as cm:
            self.node_job().run()
        self.assertEqual((cm.exception.code, cm.exception.fallback), (job.EXIT_BUSY, True))
        self.assertIsNone(self.notice('start'))

    def test_a_node_with_no_reply_or_an_old_kit_is_not_usable(self):
        with self.assertRaises(job.JobError) as cm:
            self.node_job(FakeLink(self.box, fail_calls={'start'})).run()
        self.assertEqual((cm.exception.code, cm.exception.fallback), (job.EXIT_NODE, True))
        self.assertIn('Connection timed out', cm.exception.message)

        class Old(FakeLink):
            def call(self, verb, payload, timeout_s=None):
                return 2, b'{"protocol":1,"ok":false,"error":"refused: not a command this node serves"}\n', b''
        with self.assertRaises(job.JobError) as cm:
            self.node_job(Old(self.box)).run()
        self.assertIn('update the kit', cm.exception.message)

    def test_a_busy_lock_on_the_node_cleans_and_falls_back(self):
        self.result = {'exit': 6, 'how': 'busy', 'why': 'busy: no slot now'}
        with self.assertRaises(job.JobError) as cm:
            self.node_job().run()
        self.assertEqual((cm.exception.code, cm.exception.fallback), (job.EXIT_BUSY, True))
        self.assertEqual(self.verbs()[-1], 'clean')
        self.assertIsNotNone(self.notice('end'))

    def test_a_runner_gone_with_no_result_is_exit_10(self):
        self.result = None
        self.assertEqual(self.node_job().run(), job.EXIT_NODE)
        self.assertIn('vanished', ' '.join(self.said))


class Reattach(Base):

    def test_a_drop_mid_frame_reattaches_from_the_last_whole_frame(self):
        self.log = b'A' * 50
        # 'L 50\n' is 5 bytes: the first call stops inside the L frame, the second gets it all
        link = FakeLink(self.box, cut=[20, 'down', None])
        code = self.node_job(link).run()
        self.assertEqual(code, 0)
        self.assertEqual(bytes(self.output), self.log)
        follows = [p for v, p in link.calls if v == 'follow']
        self.assertEqual([p['offset'] for p in follows], [0, 0, 0])
        self.assertEqual(self.sleeps, [2, 4])
        self.assertIn('reattaching from byte 0', self.said[1])

    def test_it_resumes_at_the_offset_it_had(self):
        self.log = b'x' * 10
        # the 'L 10' header line and its 10 bytes are 15: the first call drops after that frame
        link = FakeLink(self.box, cut=[15, None])
        self.assertEqual(self.node_job(link).run(), 0)
        follows = [p['offset'] for v, p in link.calls if v == 'follow']
        self.assertEqual(follows, [0, 10])
        self.assertEqual(bytes(self.output), b'x' * 10)
        self.assertEqual(self.sleeps, [2])

    def test_a_connection_that_does_not_come_back_is_lost_and_says_how_to_follow(self):
        link = FakeLink(self.box, cut=['down'] * 50)
        code = self.node_job(link, reattach_s=60).run()
        self.assertEqual(code, job.EXIT_LOST)
        self.assertLessEqual(sum(self.sleeps), 60)
        self.assertEqual(self.sleeps[:5], [2, 4, 8, 16, 30])
        text = '\n'.join(self.said)
        self.assertIn('LOST:', text)
        self.assertIn('node.py follow box1 %s' % JOB, text)
        self.assertIn('node.py stop box1 %s' % JOB, text)
        self.assertNotIn('clean', self.verbs())
        self.assertIn('lost its connection', self.notice('end')['text'])

    def test_the_follow_line_names_the_artifacts_so_a_follow_does_not_drop_them(self):
        nj = self.node_job(FakeLink(self.box, cut=['down'] * 50), artifacts=['out/*.txt', 'a.bin'],
                           reattach_s=10)
        self.assertEqual(nj.run(), job.EXIT_LOST)
        self.assertIn('node.py follow box1 %s -C "%s" --artifact "out/*.txt" --artifact "a.bin"'
                      % (JOB, self.top), ' '.join(self.said))

    def test_no_such_job_on_the_node_is_exit_10(self):
        nj = self.node_job()
        with self.assertRaises(job.JobError) as cm:
            nj.follow()
        self.assertEqual(cm.exception.code, job.EXIT_NODE)


class Artifacts(Base):

    def test_the_files_land_at_their_paths_and_only_then_is_it_cleaned(self):
        self.files = {'out/a.txt': b'aaa', 'out/deep/b.txt': b'', 'top.bin': b'\x00\xff'}
        code = self.node_job(artifacts=['out/**/*.txt', 'top.bin']).run()
        self.assertEqual(code, 0)
        for rel, data in self.files.items():
            with open(os.path.join(self.top, *rel.split('/')), 'rb') as f:
                self.assertEqual(f.read(), data)
        self.assertEqual(self.verbs(), ['start', 'follow', 'fetch', 'clean'])
        self.assertIn('3 artifacts copied', ' '.join(self.said))

    def test_a_tracked_file_is_never_overwritten_and_nothing_is_copied(self):
        self.files = {'a.txt': b'new', 'b.txt': b'new'}
        self.tracked = {'b.txt'}
        nj = self.node_job(artifacts=['*.txt'])
        self.assertEqual(nj.run(), job.EXIT_ARTIFACTS)
        self.assertEqual(os.listdir(self.top), [])
        self.assertNotIn('clean', self.verbs())
        self.assertTrue(os.path.isfile(os.path.join(nj.staging, 'a.txt')))
        self.assertIn(nj.staging, ' '.join(self.said))
        shutil.rmtree(nj.staging, True)

    def test_a_tracked_file_in_another_case_is_never_overwritten(self):
        self.files = {'build/CONFIG.H': b'new'}
        self.tracked = {'build/config.h'}
        nj = self.node_job(artifacts=['build/*'])
        self.assertEqual(nj.run(), job.EXIT_ARTIFACTS)
        self.assertIn('build/config.h is tracked', ' '.join(self.said))
        self.assertEqual(os.listdir(self.top), [])
        shutil.rmtree(nj.staging, True)

    def leftovers(self):
        return [os.path.join(d, n) for d, _ds, ns in os.walk(self.top) for n in ns
                if n.startswith('.pmkit-node-')]

    def test_a_copy_that_fails_part_way_puts_the_checkout_back_and_is_exit_13(self):
        # an OSError while placing (here the move of the second file, as for a locked file) once
        # escaped run() and node.py took it for "the node could not be used"
        self.files = {'a.txt': b'new a', 'b.txt': b'new b', 'sub/c.txt': b'new c'}
        with open(os.path.join(self.top, 'a.txt'), 'wb') as f:
            f.write(b'untracked a')
        real_replace = os.replace

        def replace(src, dst):
            if os.path.basename(dst) == 'b.txt':
                raise PermissionError(13, 'The process cannot access the file', dst)
            return real_replace(src, dst)
        nj = self.node_job(artifacts=['*.txt', 'sub/*.txt'])
        with mock.patch.object(job.os, 'replace', replace):
            self.assertEqual(nj.run(), job.EXIT_ARTIFACTS)
        with open(os.path.join(self.top, 'a.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'untracked a')
        self.assertEqual(sorted(os.listdir(self.top)), ['a.txt'])
        self.assertEqual(self.leftovers(), [])
        text = ' '.join(self.said)
        self.assertIn('b.txt could not be copied into the checkout', text)
        self.assertIn('no artifact was copied', text)
        self.assertNotIn('clean', self.verbs())
        self.assertTrue(os.path.isfile(os.path.join(nj.staging, 'b.txt')))
        shutil.rmtree(nj.staging, True)

    def test_an_untracked_file_that_cannot_be_moved_aside_copies_nothing(self):
        self.files = {'a.txt': b'new a', 'b.txt': b'new b'}
        with open(os.path.join(self.top, 'b.txt'), 'wb') as f:
            f.write(b'untracked b')
        real_replace = os.replace

        def replace(src, dst):
            if dst.endswith('.old'):
                raise PermissionError(13, 'The process cannot access the file', src)
            return real_replace(src, dst)
        nj = self.node_job(artifacts=['*.txt'])
        with mock.patch.object(job.os, 'replace', replace):
            self.assertEqual(nj.run(), job.EXIT_ARTIFACTS)
        with open(os.path.join(self.top, 'b.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'untracked b')
        self.assertEqual(sorted(os.listdir(self.top)), ['b.txt'])
        self.assertEqual(self.leftovers(), [])
        self.assertIn('b.txt could not be copied into the checkout', ' '.join(self.said))
        shutil.rmtree(nj.staging, True)

    def test_a_file_where_a_folder_must_be_copies_nothing(self):
        self.files = {'a.txt': b'new a', 'out/b.txt': b'new b'}
        with open(os.path.join(self.top, 'out'), 'wb') as f:
            f.write(b'untracked file named out')
        nj = self.node_job(artifacts=['*.txt', 'out/*.txt'])
        self.assertEqual(nj.run(), job.EXIT_ARTIFACTS)
        self.assertEqual(sorted(os.listdir(self.top)), ['out'])
        with open(os.path.join(self.top, 'out'), 'rb') as f:
            self.assertEqual(f.read(), b'untracked file named out')
        self.assertIn('out/b.txt could not be copied', ' '.join(self.said))
        shutil.rmtree(nj.staging, True)

    def test_an_untracked_file_is_replaced_and_no_copy_is_left_beside_it(self):
        self.files = {'a.txt': b'new a', 'deep/er/b.txt': b'new b'}
        with open(os.path.join(self.top, 'a.txt'), 'wb') as f:
            f.write(b'old a')
        self.assertEqual(self.node_job(artifacts=['*.txt', '**/b.txt']).run(), 0)
        with open(os.path.join(self.top, 'a.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'new a')
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_job_keeps_its_own_exit_when_artifacts_fail(self):
        self.result = {'exit': 3, 'how': 'ended'}
        self.files = {'a.txt': b'x'}
        self.tracked = {'a.txt'}
        nj = self.node_job(artifacts=['*.txt'])
        self.assertEqual(nj.run(), 3)
        shutil.rmtree(nj.staging, True)

    def test_a_damaged_file_is_fetched_again(self):
        self.files = {'a.txt': b'abc'}
        link = FakeLink(self.box, garble_fetch=1)
        self.assertEqual(self.node_job(link, artifacts=['a.txt']).run(), 0)
        self.assertEqual([v for v in self.verbs() if v == 'fetch'], ['fetch', 'fetch'])
        with open(os.path.join(self.top, 'a.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'abc')

    def test_damaged_twice_is_exit_13(self):
        self.files = {'a.txt': b'abc'}
        nj = self.node_job(FakeLink(self.box, garble_fetch=2), artifacts=['a.txt'])
        self.assertEqual(nj.run(), job.EXIT_ARTIFACTS)
        self.assertIn('sha256', ' '.join(self.said))
        self.assertFalse(os.path.exists(os.path.join(self.top, 'a.txt')))
        shutil.rmtree(nj.staging, True)

    def test_a_manifest_naming_what_main_did_not_ask_for_is_refused(self):
        nj = self.node_job(artifacts=['out/*.txt'])
        for files in ([{'path': 'other.txt', 'size': 0, 'sha256': 'e' * 64}],
                      [{'path': '../x.txt', 'size': 0, 'sha256': 'e' * 64}],
                      [{'path': 'out/a.txt', 'size': -1, 'sha256': 'e' * 64}],
                      [{'path': 'out/a.txt', 'size': 0, 'sha256': 'e' * 64}] * 2,
                      'x'):
            with self.subTest(files=str(files)[:40]):
                with self.assertRaises(ValueError):
                    nj._manifest(json.dumps({'files': files, 'error': None}).encode())


class Paths(unittest.TestCase):

    def test_glob_match_is_globs(self):
        for g, p, want in (('out/*.txt', 'out/a.txt', True), ('out/*.txt', 'out/x/a.txt', False),
                           ('**/*.txt', 'a.txt', True), ('**/*.txt', 'a/b/c.txt', True),
                           ('out/**', 'out/a/b', True), ('a?.bin', 'ab.bin', True),
                           ('*.TXT', 'a.txt', False), ('[ab].c', 'b.c', True)):
            with self.subTest(glob=g, path=p):
                self.assertEqual(job.glob_match(g, p), want)

    def test_a_path_main_will_not_write(self):
        for p in ('a.txt', 'out/deep/b.bin', 'x.y.z', 'a~b.txt', 'notes~.txt'):
            self.assertTrue(job.artifact_path_ok(p), p)
        for p in ('', '/a', 'a/../b', './a', 'a//b', '.git/x', '.GIT/config', 'a\\b', 'C:x', 'a:b',
                  'NUL', 'con.txt', 'a/COM1', 'a.', 'a ', 'a*b', 'a\x01', None, 'x' * 1025,
                  'build/CONFIG~1.H', 'PROGRA~1/x', 'a/b~12', 'docs/ünits.md',
                  'İ/a.txt', 'a b'):
            self.assertFalse(job.artifact_path_ok(p), p)


class TheCheckout(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)

    def test_top_head_and_tracked_changes(self):
        self.repo.write('a.txt', 'one\n')
        sha = self.repo.commit('add a', 'a.txt')
        sub = os.path.join(self.repo.root, 'sub')
        os.makedirs(sub)
        co = job.checkout(sub)
        self.assertEqual((os.path.normcase(co.top), co.sha, co.dirty),
                         (os.path.normcase(os.path.normpath(self.repo.root)), sha, False))
        self.repo.write('new.txt', 'untracked\n')
        self.assertFalse(job.checkout(self.repo.root).dirty)
        self.repo.write('a.txt', 'two\n')
        self.assertTrue(job.checkout(self.repo.root).dirty)

    def test_outside_a_work_tree_is_usage(self):
        d = tempfile.mkdtemp(prefix='kit-job-nogit-')
        self.addCleanup(shutil.rmtree, d, True)
        with self.assertRaises(job.JobError) as cm:
            job.checkout(d, git=lambda args, cwd: (128, b'', 'fatal: not a git repository'))
        self.assertEqual(cm.exception.code, job.EXIT_USAGE)


@real_git('git ls-files with :(literal) pathspecs: the tracked check on a real index')
class PlaceOnRealGit(unittest.TestCase):

    def test_a_tracked_file_is_found_by_the_default_git(self):
        repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(repo.cleanup)
        repo.write('out/a.txt', 'tracked\n')
        repo.commit('add', 'out/a.txt')
        nj = job.NodeJob(NODE, None, repo.root, SHA, 'my-repo', 's', 'k', ['x'], ['out/*'])
        nj.staging = tempfile.mkdtemp(prefix='kit-job-stage-')
        self.addCleanup(shutil.rmtree, nj.staging, True)
        for rel in ('out/a.txt', 'out/b.txt'):
            os.makedirs(os.path.join(nj.staging, 'out'), exist_ok=True)
            with open(os.path.join(nj.staging, *rel.split('/')), 'w') as f:
                f.write('new\n')
        why = nj._place(['out/a.txt', 'out/b.txt'])
        self.assertIn('out/a.txt is tracked', why)
        self.assertFalse(os.path.exists(os.path.join(repo.root, 'out', 'b.txt')))
        self.assertIsNone(nj._place(['out/b.txt']))
        with open(os.path.join(repo.root, 'out', 'b.txt')) as f:
            self.assertEqual(f.read(), 'new\n')

    def test_a_tracked_file_named_in_another_case_is_found(self):
        # NTFS opens build/CONFIG.H as the tracked build/config.h
        repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(repo.cleanup)
        repo.write('build/config.h', 'tracked\n')
        repo.commit('add', 'build/config.h')
        nj = job.NodeJob(NODE, None, repo.root, SHA, 'my-repo', 's', 'k', ['x'], ['build/*'])
        nj.staging = tempfile.mkdtemp(prefix='kit-job-stage-')
        self.addCleanup(shutil.rmtree, nj.staging, True)
        os.makedirs(os.path.join(nj.staging, 'BUILD'))
        with open(os.path.join(nj.staging, 'BUILD', 'CONFIG.H'), 'w') as f:
            f.write('new\n')
        self.assertIn('build/config.h is tracked', nj._place(['BUILD/CONFIG.H']))
        with open(os.path.join(repo.root, 'build', 'config.h')) as f:
            self.assertEqual(f.read(), 'tracked\n')

    def test_a_tracked_file_named_in_another_unicode_case_is_never_overwritten(self):
        # git's :(literal,icase) folds ASCII only (git 2.55: it misses docs/Ünits.md for
        # docs/ünits.md) while NTFS opens one as the other: a non-ASCII path is refused
        repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(repo.cleanup)
        repo.write('docs/Ünits.md', 'tracked\n')
        repo.commit('add', 'docs/Ünits.md')
        nj = job.NodeJob(NODE, None, repo.root, SHA, 'my-repo', 's', 'k', ['x'], ['docs/*'])
        with self.assertRaises(ValueError):
            nj._manifest(json.dumps({'files': [{'path': 'docs/ünits.md', 'size': 4,
                                                'sha256': 'e' * 64}], 'error': None}).encode())
        with open(os.path.join(repo.root, 'docs', 'Ünits.md')) as f:
            self.assertEqual(f.read(), 'tracked\n')


if __name__ == '__main__':
    unittest.main()
