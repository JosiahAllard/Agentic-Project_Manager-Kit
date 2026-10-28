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
                                            'error': 'refused: only status is served'})
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
                                                         'error': 'refused: only status is served'})
        self.assertNotIn(MARK.encode(), r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
