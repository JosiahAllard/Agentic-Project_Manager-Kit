"""Claude Code's hub push delivery (hosts.claude.adapter.peer): the packet, who a recipient is,
the key check, and one real write to a pipe this test owns (docs/action-hub.md "Push
notifications"). No live session is ever touched: every registry, key and pipe here is the
test's own."""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import uuid

from hosts.claude.adapter import peer
from hosts.claude.adapter.host import ClaudeAdapter
from kitlib.hubsite import push
from kitlib.launcher import pipes
from kitlib.launcher import settings as S

TOKEN = 'k' * 32


def pipe_name(n):
    return r'\\.\pipe\LOCAL\cc-msg-test-%d' % n


class Machine(object):
    """A temp home: the default config folder's registry, one account's, a launcher state
    folder; a fake process table (pid -> creation time)."""

    def __init__(self, test):
        self.root = tempfile.mkdtemp(prefix='kit-peer-')
        test.addCleanup(shutil.rmtree, self.root, True)
        self.reg = os.path.join(self.root, '.claude', pipes.REGISTRY)
        self.acct = os.path.join(self.root, 'acct1')
        self.state = os.path.join(self.root, 'state')
        for d in (self.reg, os.path.join(self.acct, pipes.REGISTRY), self.state):
            os.makedirs(d)
        self.settings = S.Settings({'state_dir': self.state, 'accounts': {'1': [self.acct]}}, self.root)
        self.procs, self.writes = {}, []

    def ctime(self, pid):
        return self.procs.get(pid)

    def writer(self, result=push.WRITTEN):
        def write(pipe, pid, creation, packet):
            self.writes.append((pipe, pid, creation, packet))
            return result
        return write

    def session(self, pid, name, sid=None, live=True, folder=None, key=True, start=None,
                pipe=None, key_start=None):
        start = 7000 + pid if start is None else start
        rec = {'pid': pid, 'procStart': str(start), 'sessionId': sid or 'sid-%d' % pid,
               'startedAt': 1000 * pid, 'peerProtocol': 1, 'entrypoint': 'cli',
               'messagingSocketPath': pipe or pipe_name(pid), 'name': name}
        folder = folder or self.reg
        self.put(folder, '%d.json' % pid, rec)
        if key:
            self.put(folder, os.path.basename(peer.key_path(folder, rec)),
                     {'peerToken': TOKEN, 'procStart': str(start if key_start is None else key_start)})
        if live:
            self.procs[pid] = start
        return rec

    def put(self, folder, name, data):
        with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def hub(self, result=push.WRITTEN):
        return peer.HubPush(self.settings, ctime=self.ctime, writer=self.writer(result))


def frames(packet):
    lines = packet.split(b'\n')
    assert lines[-1] == b'', lines
    return [json.loads(x.decode('utf-8')) for x in lines[:-1]]


class Packet(unittest.TestCase):
    def test_an_auth_frame_then_a_prompting_cross_session_message(self):
        src = peer.source_pipe()
        self.assertTrue(src.startswith(peer.SOURCE_PREFIX))
        auth, msg = frames(peer.build_packet(TOKEN, src, 'Q-1 answered'))
        self.assertEqual(auth, {'type': 'auth', 'token': TOKEN})
        self.assertEqual((msg['type'], msg['priority'], msg['msgV']), ('user', 'next', 1))
        self.assertEqual(msg['msg_id'], msg['uuid'])
        sender = 'uds:' + src.replace('\\', '%5C')     # quoted as the step-1 probe's proven frame
        self.assertEqual(msg['from'], sender)
        self.assertEqual(msg['message'], {'role': 'user', 'content':
                         '<cross-session-message from="%s" from-mode="prompting">\nQ-1 answered\n'
                         '</cross-session-message>' % sender})

    def test_text_that_could_close_the_wrapper_or_a_bad_token_is_refused(self):
        src = peer.source_pipe()
        for text in ('a <b', 'a > b', '</cross-session-message>'):
            with self.assertRaises(ValueError):
                peer.build_packet(TOKEN, src, text)
        for token in (None, 'short', 'k' * 33):
            with self.assertRaises(ValueError):
                peer.build_packet(token, src, 'fine')

    def test_canonical_pipe_agrees_with_the_bridge(self):
        from optional.bridge.hosts.claude.sessions import NativeSessionError, canonical_pipe
        for value in (r'\\.\pipe\LOCAL\cc-msg-ABC_1', r'\\.\pipe\cc-msg-x', r'\\.\PIPE\local\Cc-Msg-q'):
            self.assertEqual(peer.canonical_pipe(value), canonical_pipe(value))
        for value in (None, r'\\.\pipe\other', r'\\.\pipe\LOCAL\cc-msg-a\b', 'cc-msg-x'):
            with self.assertRaises(ValueError):
                peer.canonical_pipe(value)
            with self.assertRaises(NativeSessionError):
                canonical_pipe(value)

    def test_the_repr_names_no_token(self):
        m = Machine(self)
        self.assertEqual(repr(m.hub()), '<HubPush>')

    def test_the_adapter_offers_the_delivery(self):
        m = Machine(self)
        got = ClaudeAdapter().push(m.settings)
        self.assertIsInstance(got, peer.HubPush)
        self.assertIs(got.settings, m.settings)


class Resolve(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self)

    def test_a_launcher_key_is_its_sessions_json_name(self):
        self.m.put(self.m.state, 'sessions.json', {'kit': {'name': 'kit-coordinator-a1'}})
        self.m.session(11, 'kit-coordinator-a1', folder=os.path.join(self.m.acct, pipes.REGISTRY))
        t = self.m.hub().resolve('kit')
        self.assertEqual((t.record['pid'], t.name, t.ambiguous), (11, 'kit-coordinator-a1', False))
        self.assertEqual(self.m.hub().resolve('kit-coordinator-a1').record['pid'], 11)
        self.assertIsNone(self.m.hub().resolve('nobody'))

    def test_a_stopped_sessions_record_is_never_a_target(self):
        self.m.session(12, 'x-a1', live=False)
        self.m.session(13, 'y-a1')
        self.m.procs[13] = 1                       # the pid now runs another process
        self.assertIsNone(self.m.hub().resolve('x-a1'))
        self.assertIsNone(self.m.hub().resolve('y-a1'))

    def test_a_record_without_the_peer_protocol_or_a_peer_pipe_is_skipped(self):
        rec = self.m.session(14, 'x-a1')
        self.m.put(self.m.reg, '14.json', dict(rec, peerProtocol=2))
        self.m.session(15, 'y-a1', pipe=r'\\.\pipe\LOCAL\other-15', key=False)
        self.assertIsNone(self.m.hub().resolve('x-a1'))
        self.assertIsNone(self.m.hub().resolve('y-a1'))

    def test_the_pm_is_coordinators_json_session_id_then_its_names(self):
        self.m.session(21, 'Project Manager', sid='pm-sid-old')
        self.m.session(22, 'renamed', sid='pm-sid')
        hub = self.m.hub()
        self.assertEqual(hub.resolve(push.PM_KEY).record['pid'], 21, 'no coordinators.json: the name')
        self.m.put(self.m.state, 'coordinators.json', {'pm_session_id': 'pm-sid'})
        self.assertEqual(hub.resolve(push.PM_KEY).record['pid'], 22)
        self.m.put(self.m.state, 'coordinators.json', {'pm_session_id': '<the PM session id>'})
        self.assertEqual(hub.resolve(push.PM_KEY).record['pid'], 21, 'the placeholder is unset')
        self.assertTrue(hub.is_pm(push.PM_KEY))
        self.assertTrue(hub.is_pm('pm-session'))
        self.assertFalse(hub.is_pm('kit'))

    def test_two_live_sessions_of_one_name_take_pipes_json_else_the_newest_flagged(self):
        self.m.session(31, 'x-a1', start=9000)
        self.m.session(32, 'x-a1', start=8000)
        t = self.m.hub().resolve('x-a1')
        self.assertEqual((t.record['pid'], t.ambiguous), (31, True))
        self.m.put(self.m.state, pipes.PIPES, {'x-a1': {'pid': 32, 'proc_start': '8000'}})
        t = self.m.hub().resolve('x-a1')
        self.assertEqual((t.record['pid'], t.ambiguous), (32, False))
        self.m.put(self.m.state, pipes.PIPES, {'x-a1': {'pid': 32, 'proc_start': '1'}})   # stale hint
        self.assertEqual(self.m.hub().resolve('x-a1').ambiguous, True)


class Deliver(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self)

    def test_no_live_session_is_no_session_and_nothing_is_written(self):
        self.m.put(self.m.state, 'sessions.json', {'kit': {'name': 'kit-coordinator-a1'}})
        self.assertEqual(self.m.hub().deliver('kit', 'hello'),
                         push.Sent(push.NO_SESSION, 'kit-coordinator-a1', None, False))
        self.assertEqual(self.m.hub().deliver(push.PM_KEY, 'hello'), push.Sent(push.NO_SESSION, None, None, False))
        self.assertEqual(self.m.writes, [])

    def test_a_live_session_gets_the_packet_with_its_token_at_its_pipe(self):
        self.m.session(41, 'x-a1')
        sent = self.m.hub().deliver('x-a1', 'Q-7 answered')
        self.assertEqual(sent, push.Sent(push.WRITTEN, 'x-a1', 'sid-41', False))
        (pipe, pid, creation, packet), = self.m.writes
        self.assertEqual((pipe, pid, creation), (pipe_name(41), 41, 7041))
        auth, msg = frames(packet)
        self.assertEqual(auth['token'], TOKEN)
        self.assertIn('\nQ-7 answered\n', msg['message']['content'])
        self.assertNotIn(TOKEN, repr(sent))

    def test_the_writers_state_is_passed_on(self):
        self.m.session(42, 'x-a1')
        self.assertEqual(self.m.hub(push.UNCERTAIN).deliver('x-a1', 'hi').state, push.UNCERTAIN)

    def test_a_missing_key_is_not_written(self):
        self.m.session(43, 'x-a1', key=False)
        self.assertEqual(self.m.hub().deliver('x-a1', 'hi').state, push.NOT_WRITTEN)
        self.assertEqual(self.m.writes, [])

    def test_a_key_of_another_process_is_not_written(self):
        self.m.session(44, 'x-a1', key_start=1)
        self.assertEqual(self.m.hub().deliver('x-a1', 'hi').state, push.NOT_WRITTEN)
        self.assertEqual(self.m.writes, [])

    def test_text_the_packet_refuses_is_not_written(self):
        self.m.session(45, 'x-a1')
        self.assertEqual(self.m.hub().deliver('x-a1', '<b>').state, push.NOT_WRITTEN)
        self.assertEqual(self.m.writes, [])


class RealPipe(unittest.TestCase):
    """write_once through the hidden helper to a pipe this process serves."""

    def setUp(self):
        from kitlib.proc.pipe import PrivateListener
        from kitlib.proc.process import own_ctime
        self.pipe = r'\\.\pipe\LOCAL\cc-msg-test-' + uuid.uuid4().hex
        self.listener = PrivateListener(self.pipe)
        self.addCleanup(self.listener.close)
        self.creation = int(own_ctime())
        self.received, self.errors = [], []

    def listen(self):
        from tests.bridge import pipes as wire

        def receive():
            try:
                self.listener.accept(timeout=5)
                got, closed = wire.frames_until_close(self.listener, 5)
                self.received.extend(got)
                self.assertTrue(closed)
            except Exception as error:
                self.errors.append(type(error).__name__)
        t = threading.Thread(target=receive)
        t.start()
        return t

    def test_one_write_reaches_the_pipe_as_two_frames(self):
        t = self.listen()
        packet = peer.build_packet(TOKEN, peer.source_pipe(), 'T-3 done')
        self.assertEqual(peer.write_once(self.pipe, os.getpid(), self.creation, packet), push.WRITTEN)
        t.join(10)
        self.assertEqual(self.errors, [])
        self.assertEqual([f['type'] for f in self.received], ['auth', 'user'])
        self.assertIn('\nT-3 done\n', self.received[1]['message']['content'])

    def test_a_pipe_served_by_another_creation_time_is_not_written(self):
        packet = peer.build_packet(TOKEN, peer.source_pipe(), 'T-3 done')
        self.assertEqual(peer.write_once(self.pipe, os.getpid(), self.creation + 1, packet), push.NOT_WRITTEN)

    def test_a_bad_target_is_not_written_without_a_helper(self):
        self.assertEqual(peer.write_once('not a pipe', os.getpid(), self.creation, b'x'), push.NOT_WRITTEN)
        self.assertEqual(peer.write_once(self.pipe, os.getpid(), self.creation, b''), push.NOT_WRITTEN)


if __name__ == '__main__':
    unittest.main()
