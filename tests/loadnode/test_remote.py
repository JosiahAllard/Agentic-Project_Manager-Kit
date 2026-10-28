"""kitlib.loadnode.remote: the ssh call is a fixed argument list with the host key pinned, and
every way a node can fail is told apart. No test starts ssh: a runner stands in for it."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.loadnode import registry, remote, reply
from tests.loadnode.test_registry import KEY

NODE = registry.Node('box1', '192.168.1.50', 'pmnode', KEY, 1, 12288, 2222, '~/.ssh/pmkit')
PLAIN = registry.Node('box2', 'node2.lan', 'pmnode', KEY, 2, 4096, 22, None)
SSH = r'C:\Windows\System32\OpenSSH\ssh.exe'
HOME = os.path.join(tempfile.gettempdir(), 'kit-home-loadnode')     # only joined, never read


def ok_reply(**lock):
    lk = {'cpu_slots': 4, 'cpu_free': 3, 'gpu_slots': 0, 'gpu_free': 0, 'running': 1,
          'waiting': 0, 'holds': 0, 'pending': False, 'unreadable': []}
    lk.update(lock)
    return reply.dumps({'protocol': 1, 'ok': True, 'kit_version': '0.4.0', 'lock': lk,
                        'hardware': {'threads': 8, 'cores': 4,
                                     'memory_mb': {'total': 15900, 'free': 9100},
                                     'gpus': [], 'problems': 0}}).encode() + b'\n'


class Runner(object):
    """Stands in for ssh: records the argv and the pinned known_hosts file it was given."""

    def __init__(self, rc=0, out=b'', err=b'', raises=None):
        self.rc, self.out, self.err, self.raises = rc, out, err, raises
        self.argv = self.known = self.known_path = None

    def __call__(self, argv, timeout_s):
        self.argv, self.timeout = argv, timeout_s
        opt = next(a for a in argv if a.startswith('UserKnownHostsFile='))
        self.known_path = opt[len('UserKnownHostsFile="'):-1]
        with open(self.known_path, encoding='ascii') as f:
            self.known = f.read()
        if self.raises:
            raise self.raises
        return self.rc, self.out, self.err


class Argv(unittest.TestCase):

    def test_the_whole_argument_list(self):
        argv = remote.ssh_argv(NODE, SSH, r'C:\Temp\kh 1.txt', home=HOME)
        self.assertEqual(argv, [
            SSH, '-T', '-F', 'none',
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'HostKeyAlias=pmkit-node-box1',
            '-o', 'UserKnownHostsFile="C:\\Temp\\kh 1.txt"',
            '-o', 'GlobalKnownHostsFile=none', '-o', 'CheckHostIP=no', '-o', 'UpdateHostKeys=no',
            '-o', 'ConnectTimeout=8',
            '-i', os.path.join(HOME, '.ssh', 'pmkit'), '-o', 'IdentitiesOnly=yes',
            '-l', 'pmnode', '-p', '2222', '--', '192.168.1.50', 'status'])

    def test_no_identity_file_leaves_ssh_its_default(self):
        argv = remote.ssh_argv(PLAIN, SSH, r'C:\Temp\kh.txt')
        self.assertNotIn('-i', argv)
        self.assertEqual(argv[-5:], ['-p', '22', '--', 'node2.lan', 'status'])

    def test_the_command_is_the_one_word_status(self):
        self.assertEqual(remote.ssh_argv(PLAIN, SSH, 'kh')[-1], 'status')
        self.assertEqual(remote.COMMAND, 'status')

    def test_a_known_hosts_path_ssh_would_read_differently_is_refused(self):
        for p in (r'C:\Temp\a"b', r'C:\Temp\%d'):
            with self.subTest(path=p), self.assertRaises(ValueError):
                remote.ssh_argv(PLAIN, SSH, p)

    def test_the_pin_is_the_registry_key_under_the_alias(self):
        self.assertEqual(remote.known_hosts_line(NODE), 'pmkit-node-box1 %s\n' % KEY)


class Query(unittest.TestCase):

    def query(self, runner, node=PLAIN):
        r = remote.query(node, runner=runner, ssh=SSH, timeout_s=7)
        self.assertEqual(r.name, node.name)
        return r

    def test_an_ok_reply(self):
        run = Runner(out=ok_reply())
        r = self.query(run)
        self.assertEqual((r.state, r.detail), (remote.OK, ''))
        self.assertEqual(r.reply['lock']['cpu_free'], 3)
        self.assertEqual(run.timeout, 7)

    def test_the_pinned_file_holds_only_the_key_and_is_removed_after(self):
        run = Runner(out=ok_reply())
        self.query(run)
        self.assertEqual(run.known, 'pmkit-node-box2 %s\n' % KEY)
        self.assertFalse(os.path.exists(run.known_path))

    def test_the_pinned_file_is_removed_after_a_failure_too(self):
        run = Runner(raises=subprocess.TimeoutExpired('ssh', 7))
        self.assertEqual(self.query(run).state, remote.TIMED_OUT)
        self.assertFalse(os.path.exists(run.known_path))

    def test_a_changed_host_key_is_its_own_state(self):
        for err in (b'Host key verification failed.\r\n',
                    b'@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n'):
            with self.subTest(err=err):
                r = self.query(Runner(rc=255, err=err))
                self.assertEqual(r.state, remote.HOST_KEY)

    def test_unreachable_keeps_only_a_clean_last_line(self):
        r = self.query(Runner(rc=255, err=b'debug\nssh: connect to host node2.lan port 22: '
                                         b'Connection timed out\x1b[31m\r\n'))
        self.assertEqual(r.state, remote.UNREACHABLE)
        self.assertEqual(r.detail, 'ssh: connect to host node2.lan port 22: Connection timed out[31m')

    def test_replies_that_are_not_status_replies(self):
        for out in (b'', b'hello\n', b'{"protocol": 2, "ok": true}\n', ok_reply() + ok_reply(),
                    b'\xff\xfe\n', b'x' * (remote.OUT_CAP + 1), ok_reply(cpu_free=5)):
            with self.subTest(out=out[:40]):
                r = self.query(Runner(out=out))
                self.assertEqual((r.state, r.reply), (remote.BAD_REPLY, None))

    def test_an_ok_reply_with_a_failing_exit_is_bad(self):
        self.assertEqual(self.query(Runner(rc=1, out=ok_reply())).state, remote.BAD_REPLY)

    def test_the_nodes_own_refusal_is_a_node_error(self):
        out = reply.dumps(reply.refusal('the machine lock cannot be read here (OSError)')).encode()
        r = self.query(Runner(rc=1, out=out))
        self.assertEqual(r.state, remote.NODE_ERROR)
        self.assertIn('OSError', r.detail)

    def test_no_ssh_and_a_start_that_fails(self):
        with mock.patch.object(remote, 'ssh_exe', return_value=None):
            self.assertEqual(remote.query(PLAIN, runner=Runner()).state, remote.NO_SSH)
        self.assertEqual(self.query(Runner(raises=FileNotFoundError(2, 'not found'))).state,
                         remote.NO_SSH)

    def test_query_all_keeps_the_registry_order(self):
        run = Runner(out=ok_reply())
        got = remote.query_all([NODE, PLAIN], runner=run, ssh=SSH, home=HOME)
        self.assertEqual([r.name for r in got], ['box1', 'box2'])
        self.assertEqual(remote.query_all([], runner=run), [])


class DefaultRunner(unittest.TestCase):
    """default_runner starts a real child (Python, never ssh)."""

    def test_output_past_the_cap_is_cut_and_still_shows_as_too_long(self):
        rc, out, err = remote.default_runner(
            [sys.executable, '-c', 'import sys; sys.stdout.write("x" * %d)' % (remote.OUT_CAP * 3)], 60)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), remote.OUT_CAP + 1)
        self.assertEqual(remote.classify('n', rc, out, err).state, remote.BAD_REPLY)

    def test_a_child_past_its_time_is_ended(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            remote.default_runner([sys.executable, '-c', 'import time; time.sleep(60)'], 1)

    def test_a_child_gets_no_stdin(self):
        rc, out, err = remote.default_runner(
            [sys.executable, '-c', 'import sys, json; print(json.dumps(sys.stdin.read()))'], 60)
        self.assertEqual((rc, json.loads(out)), (0, ''))


if __name__ == '__main__':
    unittest.main()
