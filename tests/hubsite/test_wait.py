"""kitlib.hubsite.httpd: the wait gate. Long waits are held open over real sockets; every one is
ended by an event (a store write, the store closing), never by a sleep."""
import threading
import unittest

from kitlib.hubsite import api, httpd
from kitlib.hubsite.api import BASE
from tests.hubsite import httpfix
from tests.hubsite.httpfix import TIMEOUT, get, post

WAIT = BASE + '/wait'
HEALTH = BASE + '/health'


class Waiters(object):
    """Client threads, each blocked in one GET wait."""

    def __init__(self, case, port, token, count, since, timeout=TIMEOUT):
        self.replies = []
        self.failed = []
        self._lock = threading.Lock()
        path = '%s?since=%d&timeout=%d' % (WAIT, since, timeout)
        self.threads = [threading.Thread(target=self._one, args=(port, path, token),
                                         name='test-waiter', daemon=True) for _ in range(count)]
        for t in self.threads:
            t.start()
        case.addCleanup(self.join)
        case.addCleanup(case.store.close)       # runs first: a wait still blocked ends there

    def _one(self, port, path, token):
        try:
            r = get(port, path, token)
        except Exception as e:      # noqa: BLE001 - the test reads it
            with self._lock:
                self.failed.append(repr(e))
            return
        with self._lock:
            self.replies.append(r)

    def join(self):
        for t in self.threads:
            t.join(TIMEOUT * 2)
        return not any(t.is_alive() for t in self.threads)


class WaitCase(httpfix.ListenerCase):
    def test_the_gate_holds_32_waits_and_refuses_the_33rd_at_once(self):
        self.assertEqual(httpd.MAX_WAITS, 32)
        self.assertEqual(api.WAIT_MAX_S, 55)
        lis = self.listener()
        self.assertEqual(lis.max_waits, 32)
        since = self.store.revision
        held = Waiters(self, lis.port, self.agent, 32, since)
        self.assertTrue(lis.await_waits(32, TIMEOUT), 'only %d waits arrived' % lis.waiting)
        self.assertEqual(lis.waiting, 32)
        # the 33rd is refused while the 32 are still held, and takes no slot
        for token in (self.agent, self.owner):
            r = get(lis.port, '%s?since=%d&timeout=%d' % (WAIT, since, TIMEOUT), token)
            self.assertEqual((r.status, r.code()), (429, httpd.BUSY))
            self.assertEqual(r.header('Retry-After'), [str(httpd.RETRY_AFTER_S)])
            self.assertEqual(r.json()['error']['field'], 'wait')
        self.assertEqual(lis.waiting, 32)
        self.assertEqual(held.replies, [])
        # nothing is locked across a wait: every other route still answers, a write included
        self.assertEqual(get(lis.port, HEALTH, self.agent).status, 200)
        self.assertEqual(get(lis.port, BASE + '/snapshot', self.owner).status, 200)
        self.assertEqual(get(lis.port, '/').status, 404)
        self.assertEqual(lis.waiting, 32)
        self.assertEqual(held.replies, [])
        # one write wakes all 32
        r = post(lis.port, BASE + '/todos', {'title': 't'}, self.owner)
        self.assertEqual(r.status, 200)
        self.assertTrue(held.join(), 'a wait was not woken by the write')
        self.assertEqual(held.failed, [])
        self.assertEqual([x.status for x in held.replies], [200] * 32)
        for x in held.replies:
            self.assertIs(x.json()['timed_out'], False)
            self.assertGreater(x.json()['revision'], since)
        self.assertEqual(lis.waiting, 0)
        # and the slots are free again
        r = get(lis.port, WAIT + '?timeout=0', self.agent)
        self.assertEqual((r.status, r.json()['timed_out']), (200, True))
        self.assertEqual(self.errors(), [])

    def test_the_cap_is_the_listeners_own(self):
        lis = self.listener(max_waits=2)
        other = self.listener(max_waits=1)
        since = self.store.revision
        held = Waiters(self, lis.port, self.agent, 2, since)
        self.assertTrue(lis.await_waits(2, TIMEOUT))
        self.assertEqual(get(lis.port, '%s?since=%d' % (WAIT, since), self.agent).status, 429)
        # another listener on the same Api counts its own
        there = Waiters(self, other.port, self.agent, 1, since)
        self.assertTrue(other.await_waits(1, TIMEOUT))
        self.assertEqual((lis.waiting, other.waiting), (2, 1))
        self.assertEqual(get(other.port, '%s?since=%d' % (WAIT, since), self.agent).status, 429)
        self.assertEqual(post(lis.port, BASE + '/todos', {'title': 't'}, self.owner).status, 200)
        self.assertTrue(held.join())
        self.assertTrue(there.join())
        self.assertEqual([x.status for x in held.replies + there.replies], [200] * 3)

    def test_a_wait_that_answers_at_once_holds_no_slot(self):
        lis = self.listener(max_waits=1)
        self.assertEqual(post(lis.port, BASE + '/todos', {'title': 't'}, self.owner).status, 200)
        for _ in range(5):
            r = get(lis.port, WAIT + '?since=0&timeout=%d' % TIMEOUT, self.agent)
            self.assertEqual((r.status, r.json()['timed_out']), (200, False))
            r = get(lis.port, WAIT + '?timeout=0', self.owner)
            self.assertEqual((r.status, r.json()['timed_out']), (200, True))
        self.assertEqual(lis.waiting, 0)

    def test_a_caller_with_no_role_takes_no_slot(self):
        lis = self.listener(max_waits=1)
        since = self.store.revision
        for _ in range(3):
            r = get(lis.port, '%s?since=%d&timeout=%d' % (WAIT, since, TIMEOUT))
            self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertEqual(lis.waiting, 0)
        held = Waiters(self, lis.port, self.agent, 1, since)
        self.assertTrue(lis.await_waits(1, TIMEOUT), 'a refused caller kept the slot')
        self.assertEqual(post(lis.port, BASE + '/todos', {'title': 't'}, self.owner).status, 200)
        self.assertTrue(held.join())
        self.assertEqual([x.status for x in held.replies], [200])

    def test_a_refused_wait_gives_its_slot_back(self):
        lis = self.listener(max_waits=1)
        for _ in range(3):
            r = get(lis.port, WAIT + '?since=nope', self.agent)
            self.assertEqual((r.status, r.code()), (400, 'invalid'))
            r = get(lis.port, WAIT + '?timeout=-1', self.agent)
            self.assertEqual(r.status, 400)
        self.assertEqual(lis.waiting, 0)

    def test_closing_the_store_ends_every_wait(self):
        lis = self.listener()
        since = self.store.revision
        held = Waiters(self, lis.port, self.agent, 5, since)
        self.assertTrue(lis.await_waits(5, TIMEOUT))
        lis.close_door()                # the door first, as the server's shutdown does
        self.assertTrue(lis.drain(TIMEOUT), 'drain counted the waits as work in flight')
        self.assertEqual((lis.waiting, held.replies), (5, []))
        self.assertFalse(lis.join(0), 'the waits ended before the store closed')
        self.store.close()
        self.assertTrue(held.join())
        self.assertTrue(lis.join(TIMEOUT))
        self.assertEqual(held.failed, [])
        for x in held.replies:
            # whichever side of the close it saw, it is an answer and never a 500
            self.assertIn(x.status, (200, 503))
            if x.status == 503:
                self.assertEqual(x.code(), 'store')
        self.assertEqual(lis.waiting, 0)
        self.assertEqual(self.errors(), [])

    def test_no_thread_outlives_its_listener(self):
        lis = self.listener()
        since = self.store.revision
        held = Waiters(self, lis.port, self.agent, 3, since)
        self.assertTrue(lis.await_waits(3, TIMEOUT))
        self.assertEqual(get(lis.port, HEALTH, self.owner).status, 200)
        self.assertIn('hub-http-%d' % lis.port, httpfix.http_threads())
        self.assertGreaterEqual(httpfix.http_threads().count('hub-http'), 3)
        lis.close_door()
        self.store.close()
        self.assertTrue(lis.join(TIMEOUT))
        self.assertTrue(held.join())
        self.assertEqual(httpfix.http_threads(), [])


if __name__ == '__main__':
    unittest.main()
