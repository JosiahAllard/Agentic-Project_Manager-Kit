"""client.Client.wait against a stand-in hub on port 0 that takes a poll and holds it past the
client's socket time (serve.NoAnswer): the wait asks again and ends on the item the next poll
answers, until its own timeout; an answer that is not the hub's still ends it. api.WAIT_MAX_S
and client.WAIT_MARGIN_S are made small so a held poll costs half a second."""
import json
import socket
import threading
import unittest
from unittest import mock

from kitlib.hubsite import api, client, httpd, serve
from tests.hubsite import httpfix

TURN_S = 0.2
MARGIN_S = 0.3                  # the client's socket gives up after TURN_S + MARGIN_S
HIT = {'ok': True, 'timed_out': False, 'revision': 3, 'reset': False, 'changes': [],
       'pending': [{'id': 'Q-1', 'kind': 'question'}]}


class FakeHub(object):
    """Answers connection n by plan[n] (the last one repeats): 'hold' reads the request and
    sends nothing until the client has closed it, then sends HIT late; 'hit' answers HIT;
    'junk' answers 200 with a body that is not JSON."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.paths, self.late = [], []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((httpd.LOOPBACK, 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.sock.close()
        self.thread.join(httpfix.TIMEOUT)

    def serve(self):
        n = 0
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(httpfix.TIMEOUT)
                head = b''
                while b'\r\n\r\n' not in head:
                    got = conn.recv(4096)
                    if not got:
                        break
                    head += got
                self.paths.append(head.split(b' ')[1].decode('ascii') if head else '')
                how = self.plan[min(n, len(self.plan) - 1)]
                n += 1
                if how == 'hold':
                    try:
                        gone = conn.recv(1) == b''      # the client closed it: its time is up
                    except OSError:
                        gone = True
                    self.late.append(gone)
                    self.send(conn, json.dumps(HIT).encode('utf-8'))
                elif how == 'hit':
                    self.send(conn, json.dumps(HIT).encode('utf-8'))
                else:
                    self.send(conn, b'<html>not the hub</html>')

    @staticmethod
    def send(conn, body):
        try:
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                         b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
        except OSError:
            pass


class NoAnswerCase(unittest.TestCase):
    def setUp(self):
        case = httpfix.ListenerCase('setUp')
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.tokens = case.tokens
        for target, name, value in ((api, 'WAIT_MAX_S', TURN_S),
                                    (client, 'WAIT_MARGIN_S', MARGIN_S)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def fake(self, plan):
        hub = FakeHub(plan)
        self.addCleanup(hub.close)
        return hub, client.Client(hub.port, tokens=self.tokens)

    def test_a_poll_held_past_the_socket_time_is_asked_again_and_the_wait_ends_on_the_item(self):
        hub, c = self.fake(['hold', 'hold', 'hit'])
        got = c.wait(since=2, pending=True, for_key='pm')
        self.assertEqual(got['pending'], HIT['pending'])
        self.assertEqual(hub.late, [True, True])        # both held polls outlived the client's
        poll = api.BASE + '/wait?for=pm&pending=1&since=2&timeout=%g' % TURN_S
        self.assertEqual(hub.paths, [poll, poll, poll])  # asked again from the same revision

    def test_a_held_poll_is_no_answer_a_serve_error_with_the_text_of_before(self):
        hub, c = self.fake(['hold'])
        with self.assertRaises(serve.NoAnswer) as e:
            serve.ask(hub.port, 'GET', api.BASE + '/wait', c.token(client.ANY), timeout=0.3)
        self.assertIsInstance(e.exception, serve.ServeError)
        self.assertEqual(str(e.exception),
                         'port %d did not answer as the hub does: TimeoutError' % hub.port)

    def test_polls_that_are_never_answered_end_at_the_waits_own_timeout(self):
        hub, c = self.fake(['hold'])
        with self.assertRaises(serve.NoAnswer):
            c.wait(timeout=0.1, pending=True)
        # the turn of 0.1 s, then one of 0 s begun past the end: no third poll
        self.assertEqual(len(hub.paths), 2, hub.paths)
        self.assertEqual(hub.paths[1], api.BASE + '/wait?pending=1&timeout=0')

    def test_an_answer_that_is_not_the_hubs_still_ends_the_wait(self):
        hub, c = self.fake(['junk'])
        with self.assertRaises(serve.ServeError) as e:
            c.wait(pending=True)
        self.assertNotIsInstance(e.exception, serve.NoAnswer)
        self.assertEqual(len(hub.paths), 1)


if __name__ == '__main__':
    unittest.main()
