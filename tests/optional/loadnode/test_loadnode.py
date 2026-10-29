"""Tests for optional/loadnode/loadnode.py, the node's forced command: it serves the one word
`status` and refuses everything else without running it, starting anything or repeating it.

    python -m unittest tests.optional.loadnode.test_loadnode -v
"""
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'loadnode')
sys.path.insert(0, CODE)
import loadnode  # noqa: E402

# Every way Python starts a program or replaces this one, by name (patched to fail the test).
STARTS_SUBPROCESS = ('Popen', 'run', 'call', 'check_call', 'check_output', 'getoutput',
                     'getstatusoutput')
STARTS_OS = ('system', 'popen', 'startfile', 'execl', 'execle', 'execlp', 'execlpe', 'execv',
             'execve', 'execvp', 'execvpe', 'spawnl', 'spawnle', 'spawnv', 'spawnve', 'posix_spawn',
             'posix_spawnp', 'fork', 'forkpty')

MARK = 'MARK7731'
REFUSED_COMMANDS = (
    '', 'STATUS', ' status', 'status ', 'status x', 'status;id', 'status && id', 'status|id',
    '$(id)', '`id`', 'status\nid', 'status\x00', 'run x', 'serve', 'status --json',
    'status;echo ' + MARK, MARK, 'ѕtatus',
)


def snapshot(store='/home/<user>/.pm-kit/machine-lock', repo='secret-repo'):
    return {
        'schema_version': 1, 'store': store,
        'hardware': {'threads': 8, 'cores': [{'threads': 2}] * 4,
                     'memory_mb': {'total': 15900, 'free': 9100},
                     'gpus': [{'name': 'Radeon 780M', 'vram_mb': 4096, 'vendor': 'amd'}],
                     'problems': ['nvidia-smi missing at ' + store]},
        'layout': {'hardware_change': {'pending': False}},
        'slots': {'cpu': [{'slot': 0, 'holder': 'r1', 'hold': None},
                          {'slot': 1, 'holder': None, 'hold': None},
                          {'slot': 2, 'holder': None, 'hold': 'h1'}],
                  'gpu': [{'slot': 0, 'holder': None, 'hold': None}]},
        'runs': [{'id': 'r1', 'state': 'running', 'repo': repo, 'slug': repo + '-slug',
                  'note': 'a note', 'log': store + '/log.txt'},
                 {'id': 'r2', 'state': 'waiting', 'repo': repo}],
        'queue': {'entries': [], 'error': None}, 'holds': [{'id': 'h1'}], 'holds_error': None,
        'kinds_error': None,
    }


class NoStarts(object):
    """A context: every process start and exec in this process fails the test."""

    def __enter__(self):
        self.patches = []
        for mod, names in ((subprocess, STARTS_SUBPROCESS), (os, STARTS_OS)):
            for name in names:
                if hasattr(mod, name):
                    p = mock.patch.object(mod, name, side_effect=AssertionError(
                        'loadnode started a process: %s.%s' % (mod.__name__, name)))
                    p.start()
                    self.patches.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def _never():
    raise AssertionError('the lock was read for a refused request')


class ServeRefuses(unittest.TestCase):
    """Anything but argv ['serve'] with SSH_ORIGINAL_COMMAND exactly 'status' is refused: exit 2,
    the fixed message, the lock never read, nothing started, the request never repeated."""

    def refused(self, argv, environ):
        out = io.StringIO()
        with NoStarts(), mock.patch.object(loadnode, '_lock_snapshot', side_effect=_never):
            code = loadnode.main(argv, environ, out)
        text = out.getvalue()
        self.assertEqual(code, 2)
        self.assertEqual(text.count('\n'), 1)
        self.assertEqual(json.loads(text), {'protocol': 1, 'ok': False,
                                            'error': loadnode.REFUSED})
        self.assertNotIn(MARK, text)
        return text

    def test_each_command_but_status_is_refused(self):
        for cmd in REFUSED_COMMANDS:
            with self.subTest(cmd=cmd):
                self.refused(['serve'], {'SSH_ORIGINAL_COMMAND': cmd})

    def test_no_original_command_is_refused(self):
        self.refused(['serve'], {})

    def test_an_argument_but_serve_is_refused_even_for_status(self):
        for argv in ([], ['status'], ['serve', 'x'], ['serve', 'status'], ['SERVE'], ['-c', 'x']):
            with self.subTest(argv=argv):
                self.refused(argv, {'SSH_ORIGINAL_COMMAND': 'status'})

    def test_serve_never_calls_its_snapshot_for_a_refusal(self):
        with NoStarts():
            code, reply = loadnode.serve(['serve'], {'SSH_ORIGINAL_COMMAND': 'status;id'}, _never, '1')
        self.assertEqual((code, reply['ok']), (2, False))


class ServeAnswers(unittest.TestCase):

    def test_status_is_the_projection_of_the_lock(self):
        out = io.StringIO()
        with NoStarts(), mock.patch.object(loadnode, '_lock_snapshot', return_value=snapshot()):
            code = loadnode.main(['serve'], {'SSH_ORIGINAL_COMMAND': 'status'}, out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        got = json.loads(text)
        self.assertEqual(got['lock'], {'cpu_slots': 3, 'cpu_free': 1, 'gpu_slots': 1, 'gpu_free': 1,
                                       'running': 1, 'waiting': 1, 'holds': 1, 'pending': False,
                                       'unreadable': []})
        self.assertEqual(got['hardware'], {'threads': 8, 'cores': 4,
                                           'memory_mb': {'total': 15900, 'free': 9100},
                                           'gpus': [{'name': 'Radeon 780M', 'vram_mb': 4096}],
                                           'problems': 1})
        for secret in ('pm-kit', 'secret-repo', 'a note', '<user>'):
            self.assertNotIn(secret, text)

    def test_a_lock_that_cannot_be_read_is_exit_1_with_its_kind_only(self):
        def broken():
            raise OSError('cannot open /home/<user>/secret-path')
        code, reply = loadnode.serve(['serve'], {'SSH_ORIGINAL_COMMAND': 'status'}, broken, '1')
        self.assertEqual(code, 1)
        self.assertFalse(reply['ok'])
        self.assertIn('OSError', reply['error'])
        self.assertNotIn('secret-path', reply['error'])

    def test_the_version_is_the_kits(self):
        code, reply = loadnode.serve(['serve'], {'SSH_ORIGINAL_COMMAND': 'status'}, snapshot)
        with open(os.path.join(loadnode.KIT_ROOT, 'kit.json'), encoding='utf-8') as f:
            want = json.load(f)['version']
        self.assertEqual((code, reply['kit_version']), (0, want))


class FakeBox(object):
    """The Box's stand-in: records what reached it."""

    def __init__(self, live=0):
        self.calls, self.live = [], live

    def live_count(self):
        return self.live

    def mirror_for_push(self, repo):
        self.calls.append(('mirror', repo))
        return '/m/' + repo + '.git'

    def clean(self, job):
        self.calls.append(('clean', job))
        return {'ok': True, 'gone': True}


class RunWords(unittest.TestCase):
    """The run words go to kitlib.loadnode.box; `job` is the detached runner and never served."""

    JOB = 'j-20261028T120000Z-abcdef'

    def main(self, argv, cmd, stdin=b'', box=None, exec_fn=None):
        out, binout = io.StringIO(), io.BytesIO()
        env = {} if cmd is None else {'SSH_ORIGINAL_COMMAND': cmd}
        with mock.patch.object(loadnode, '_lock_snapshot', return_value=snapshot()):
            code = loadnode.main(argv, env, out, io.BytesIO(stdin), box or FakeBox(), exec_fn,
                                 binout)
        return code, out.getvalue(), binout.getvalue()

    def test_a_run_word_reaches_the_box(self):
        box = FakeBox()
        code, text, data = self.main(['serve'], 'clean', json.dumps({'job': self.JOB}).encode(), box)
        self.assertEqual((code, text, json.loads(data)), (0, '', {'ok': True, 'gone': True}))
        self.assertEqual(box.calls, [('clean', self.JOB)])

    def test_the_push_form_execs_git_receive_pack_on_the_mirror_only(self):
        box, ran = FakeBox(), []
        code, _t, _d = self.main(['serve'], "git-receive-pack 'pmkit-node/mirrors/r1.git'", box=box,
                                 exec_fn=ran.append)
        self.assertEqual((code, ran), (0, [['git', 'receive-pack', '/m/r1.git']]))
        for cmd in ("git-upload-pack 'pmkit-node/mirrors/r1.git'", 'start x', 'git-receive-pack',
                    "git-receive-pack 'pmkit-node/mirrors/../r1.git'", 'job ' + self.JOB):
            with self.subTest(cmd=cmd):
                code, text, data = self.main(['serve'], cmd, box=box, exec_fn=ran.append)
                self.assertEqual((code, data), (2, b''))
                self.assertEqual(json.loads(text)['error'], loadnode.REFUSED)
        self.assertEqual(len(ran), 1)

    def test_status_names_the_words_it_serves_and_its_live_jobs(self):
        code, text, _d = self.main(['serve'], 'status', box=FakeBox(live=2))
        got = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(got['jobs'], 2)
        self.assertEqual(sorted(got['verbs']),
                         sorted(['status', 'start', 'gate', 'land', 'follow', 'fetch', 'clean',
                             'stop']))

    def test_job_is_never_served_over_ssh(self):
        with mock.patch('kitlib.loadnode.box.job_main', side_effect=AssertionError('ran')):
            code, text, _d = self.main(['job', self.JOB], 'status')
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(text)['error'], loadnode.REFUSED)

    def test_job_without_ssh_runs_the_runner(self):
        with mock.patch('kitlib.loadnode.box.job_main', return_value=5) as jm:
            code, _t, _d = self.main(['job', self.JOB, '--base', os.path.abspath(os.sep + 'b')], None)
        self.assertEqual(code, 5)
        self.assertEqual(jm.call_args[0][:2], (self.JOB, os.path.abspath(os.sep + 'b')))
        from kitlib.loadnode import gaterecord
        self.assertIs(jm.call_args[1]['verify'], gaterecord.read_record)   # a gate job's record

    def test_the_runner_reads_a_land_jobs_record_with_landgate(self):
        with mock.patch('kitlib.loadnode.box.job_main', return_value=0) as jm:
            self.main(['job', self.JOB], None)
        from kitlib.loadnode import landgate
        self.assertIs(jm.call_args[1]['verify_land'], landgate.read_record)

    def test_landgate_is_never_served_over_ssh(self):
        with mock.patch('kitlib.loadnode.landgate.node_main', side_effect=AssertionError('ran')):
            code, text, _d = self.main(['landgate', self.JOB], 'status')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(text)['error'], loadnode.REFUSED)
            code, text, _d = self.main(['serve'], 'landgate ' + self.JOB)
            self.assertEqual((code, json.loads(text)['error']), (2, loadnode.REFUSED))

    def test_landgate_without_ssh_runs_the_land_jobs_merged_gate(self):
        b = os.path.abspath(os.sep + 'b')
        with mock.patch('kitlib.loadnode.landgate.node_main', return_value=4) as nm:
            code, _t, _d = self.main(['landgate', self.JOB, '--base', b], None)
        self.assertEqual(code, 4)
        self.assertEqual(nm.call_args[0], (self.JOB, b))

    def test_the_box_compares_land_specs_with_this_kits_files(self):
        from kitlib.gate import reuse
        with mock.patch.object(reuse, 'kit_files_hash', return_value='e' * 64):
            self.assertEqual(loadnode._box(os.path.abspath(os.sep + 'b')).kit_files(), 'e' * 64)

    def test_base_must_be_one_absolute_folder(self):
        self.assertEqual(loadnode.split_base(['serve']), (['serve'], None))
        self.assertEqual(loadnode.split_base(['serve', '--base', os.path.abspath('x')]),
                         (['serve'], os.path.abspath('x')))
        for argv in (['serve', '--base'], ['serve', '--base', 'rel'],
                     ['serve', '--base', os.path.abspath('x'), '--base', os.path.abspath('y')]):
            with self.subTest(argv=argv):
                self.assertEqual(loadnode.split_base(argv), (None, None))
        code, text, _d = self.main(['serve', '--base', 'rel'], 'clean', b'{}')
        self.assertEqual((code, json.loads(text)['error']), (2, loadnode.REFUSED))


class RunsIsolated(unittest.TestCase):
    """Run as sshd runs it (python -I -B <path> serve): it finds the kit with no help."""

    def test_a_refusal_from_its_own_process(self):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['SSH_ORIGINAL_COMMAND'] = 'status;echo ' + MARK
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(CODE, 'loadnode.py'), 'serve'],
                           stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=60,
                           cwd=HERE)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(json.loads(r.stdout.decode()), {'protocol': 1, 'ok': False,
                                                         'error': loadnode.REFUSED})
        self.assertNotIn(MARK.encode(), r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
