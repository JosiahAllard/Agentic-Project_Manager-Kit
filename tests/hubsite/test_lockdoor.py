"""The hub's machine lock routes (GET lock/view, lock/stats, lock/wait) and
kitlib.hubsite.lockdoor, the door they read the lock through, in-process. The routes are
the owner's: an agent is refused every one. Listeners bind port 0 on the loopback address; the
real door reads a scratch lock store (tests.lock.helpers), never this machine's."""
import json
import os
import threading
import unittest

from kitlib.hubsite import api, httpd
from kitlib.hubsite.api import BASE, OWNER
from kitlib.hubsite.store import Store as HubStore
from tests.hubsite import httpfix
from tests.hubsite import test_api_roles as roles
from tests.hubsite.httpfix import TIMEOUT, get
from tests.lock import helpers as lh

LOCK_PATHS = ('/lock/stats', '/lock/view', '/lock/wait')


def lock_routes():
    return [r for r in api.ROUTES if roles.key(r)[1].startswith('/lock/')]


def with_query(route, role=OWNER):
    """The path of a lock route as the roles table calls it, with its query."""
    _seed, _body, query = roles.row(route, role)
    path = roles.path(route, 'X-1')
    if query:
        path += '?' + '&'.join('%s=%s' % kv for kv in sorted(query.items()))
    return path


class Door(roles.LockDoor):
    """The stand-in door with a revision a test moves, and a view that can raise."""

    def __init__(self, rev=7, raises=None):
        self.rev = rev
        self.raises = raises
        self.views = []             # the remote of each view() asked for

    def revision(self):
        return self.rev

    def view(self, remote=False):
        if self.raises is not None:
            raise self.raises
        self.views.append(remote)
        return {'lock_revision': self.rev, 'view': {'remote': remote}}


class Getter(object):
    """One client thread blocked in a GET; reply is its answer (or what it raised)."""

    def __init__(self, case, port, path, token):
        self.reply = None
        self.thread = threading.Thread(target=self._one, args=(port, path, token),
                                       name='test-lock-waiter', daemon=True)
        self.thread.start()
        case.addCleanup(self.join)
        case.addCleanup(case.store.close)       # runs first: a wait still blocked ends there

    def _one(self, port, path, token):
        try:
            self.reply = get(port, path, token)
        except Exception as e:      # noqa: BLE001 - the test reads it
            self.reply = e

    def join(self):
        self.thread.join(TIMEOUT * 2)
        return not self.thread.is_alive()


# ------------------------------------------------------------------ the routes, a stand-in door
class RoutesCase(httpfix.ListenerCase):
    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.door = Door()
        self.api = api.Api(self.store, views=httpfix.Views(), clock=httpfix.CLOCK, lock=self.door)
        self.api.lock_poll_s = 0.02

    def test_the_lock_routes_are_three_and_all_the_owners(self):
        self.assertEqual(sorted(roles.key(r)[1] for r in lock_routes()), list(LOCK_PATHS))
        for route in lock_routes():
            self.assertEqual((route.method, route.role), ('GET', OWNER), route.pattern)
        self.assertEqual(api.WAIT_HANDLERS, (api.wait, api.lock_wait))

    def test_the_agent_is_refused_every_lock_route_on_the_local_listener(self):
        lis = self.listener()
        self.assertEqual(len(lock_routes()), 3)
        for route in lock_routes():
            path = with_query(route)
            with self.subTest(route=route.pattern):
                r = get(lis.port, path, self.agent)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                self.assertEqual(r.json()['error']['field'], 'role')
                self.assertNotIn('view', r.json())
                r = get(lis.port, path)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                r = get(lis.port, path, self.owner)
                self.assertEqual((r.status, r.json().get('error')), (200, None))
                if not route.pattern.endswith('/stats'):
                    self.assertEqual(r.json()['view'], {'remote': False})
        # the agent's refusals never reached the door
        self.assertEqual(self.door.views, [False, False])
        self.assertEqual(self.errors(), [])

    def test_a_lock_wait_takes_one_of_the_listeners_wait_slots(self):
        lis = self.listener(max_waits=1)
        held = Getter(self, lis.port, BASE + '/lock/wait?since=7&timeout=%d' % TIMEOUT, self.owner)
        self.assertTrue(lis.await_waits(1, TIMEOUT), 'the lock wait never took a slot')
        for path, token in ((BASE + '/lock/wait?since=7', self.owner),
                            (BASE + '/wait?since=%d' % self.store.revision, self.agent)):
            r = get(lis.port, path, token)
            self.assertEqual((r.status, r.code()), (429, httpd.BUSY), path)
        # other reads still answer, and the agent is refused before any slot is asked for
        self.assertEqual(get(lis.port, BASE + '/lock/view', self.owner).status, 200)
        self.assertEqual(get(lis.port, BASE + '/lock/wait?timeout=0', self.agent).status, 403)
        self.assertEqual(lis.waiting, 1)
        # the server's shutdown: drain() does not count the wait, the store's close ends it
        lis.close_door()
        self.assertTrue(lis.drain(TIMEOUT), 'drain counted the lock wait as work in flight')
        self.assertIsNone(held.reply)
        self.store.close()
        self.assertTrue(held.join())
        self.assertEqual((held.reply.status, held.reply.code()), (503, 'store'))
        self.assertTrue(lis.join(TIMEOUT))
        self.assertEqual(lis.waiting, 0)
        self.assertEqual(self.errors(), [])

    def test_a_lock_wait_answers_the_new_view_once_the_lock_moves(self):
        lis = self.listener()
        held = Getter(self, lis.port, BASE + '/lock/wait?since=7&timeout=%d' % TIMEOUT, self.owner)
        self.assertTrue(lis.await_waits(1, TIMEOUT))
        self.assertIsNone(held.reply)
        self.door.rev = 8
        self.assertTrue(held.join())
        self.assertEqual(held.reply.status, 200)
        self.assertEqual((held.reply.json()['changed'], held.reply.json()['lock_revision']),
                         (True, 8))
        self.assertEqual(held.reply.json()['view'], {'remote': False})
        self.assertEqual(lis.waiting, 0)

    def test_a_lock_wait_that_times_out_says_nothing_changed_and_still_gives_the_view(self):
        lis = self.listener()
        for path in ('/lock/wait?since=7&timeout=0', '/lock/wait?timeout=0',
                     '/lock/wait?since=7&timeout=0.05'):
            r = get(lis.port, BASE + path, self.owner)
            self.assertEqual(r.status, 200, path)
            self.assertEqual((r.json()['changed'], r.json()['lock_revision']), (False, 7), path)
            self.assertEqual(r.json()['view'], {'remote': False})
        # a revision the caller has not seen is answered at once
        r = get(lis.port, BASE + '/lock/wait?since=3&timeout=%d' % TIMEOUT, self.owner)
        self.assertEqual((r.status, r.json()['changed']), (200, True))
        self.assertEqual(lis.waiting, 0)

    def test_a_bad_query_is_invalid_and_holds_no_slot(self):
        lis = self.listener(max_waits=1)
        for path, field in (('/lock/wait?timeout=-1', 'timeout'),
                            ('/lock/wait?timeout=nan', 'timeout'),
                            ('/lock/wait?timeout=inf', 'timeout'),
                            ('/lock/wait?timeout=soon', 'timeout'),
                            ('/lock/wait?since=nope', 'since'),
                            ('/lock/wait?since=-1', 'since'),
                            ('/lock/stats?days=0', 'days'),
                            ('/lock/stats?days=367', 'days'),
                            ('/lock/stats?days=x', 'days')):
            with self.subTest(path=path):
                r = get(lis.port, BASE + path, self.owner)
                self.assertEqual((r.status, r.code()), (400, 'invalid'))
                self.assertEqual(r.json()['error']['field'], field)
        self.assertEqual(lis.waiting, 0)
        self.assertEqual(self.errors(), [])

    def test_a_server_with_no_lock_says_unavailable(self):
        lis = self.listener(api.Api(self.store, views=httpfix.Views(), clock=httpfix.CLOCK))
        for p in LOCK_PATHS:
            with self.subTest(path=p):
                r = get(lis.port, BASE + p + '?timeout=0', self.owner)
                self.assertEqual((r.status, r.code()), (503, 'unavailable'))
                self.assertEqual(r.json()['error']['field'], 'lock')
                # still the owner's alone
                r = get(lis.port, BASE + p, self.agent)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertEqual(lis.waiting, 0)
        self.assertEqual(self.errors(), [])

    def test_a_door_that_raises_is_unavailable_and_names_no_path(self):
        self.door.raises = RuntimeError('boom in D:\\secret\\place')
        lis = self.listener()
        for p in ('/lock/view', '/lock/wait?timeout=0'):
            r = get(lis.port, BASE + p, self.owner)
            self.assertEqual((r.status, r.code()), (503, 'unavailable'), p)
            self.assertEqual(r.json()['error']['field'], 'lock')
            self.assertNotIn(b'secret', r.body)
        self.assertEqual(self.errors(), [])

    def test_only_the_tunnel_listeners_word_asks_for_the_remote_view(self):
        self.assertEqual(api.TUNNEL_VIA, httpd.TUNNEL_VIA)
        for via, remote in ((httpd.TUNNEL_VIA, True), ('api', False), ('', False),
                            ('web', False), (None, False)):
            status, obj = self.api.call('GET', BASE + '/lock/view', OWNER, None, {}, via)
            self.assertEqual((status, obj['view']), (200, {'remote': remote}), via)


# ------------------------------------------------------------------ the real door, a scratch lock
class DoorCase(lh.StoreCase):
    def setUp(self):
        lh.StoreCase.setUp(self)
        from kitlib.hubsite import lockdoor
        self.hub = HubStore.open(os.path.join(self.tmp, 'hub'), clock=httpfix.CLOCK)
        self.addCleanup(self.hub.close)
        self.door = lockdoor.LockDoor(make_store=lambda: self.store)
        self.api = api.Api(self.hub, views=httpfix.Views(), clock=httpfix.CLOCK, lock=self.door)
        self.api.lock_poll_s = 0.02

    def call(self, path, query=None, via='api'):
        return self.api.call('GET', BASE + path, OWNER, None, query or {}, via)

    def no_folder(self, obj):
        text = json.dumps(obj)
        for folder in (self.root, self.tmp):
            self.assertNotIn(folder, text)
            self.assertNotIn(json.dumps(folder)[1:-1], text)

    def test_the_view_is_the_schedule_windows_view_as_json(self):
        self.live_record('r-1', repo='kit', slug='suite', note='my own note',
                         log=os.path.join(self.root, 'r-1.log'))
        status, obj = self.call('/lock/view')
        self.assertEqual(status, 200, obj)
        self.assertEqual(obj['lock_revision'], self.store.revision())
        view = obj['view']
        self.assertEqual(view['revision'], self.store.revision())
        self.assertEqual([r['id'] for r in view['runs']], ['r-1'])
        self.assertEqual(view['runs'][0]['slug'], 'suite')
        self.assertEqual(len(view['cores']['cpu']), self.layout()['cpu_slots'])
        text = json.dumps(obj)
        for key in ('pid', 'ctime', 'tree', 'log', 'session', 'store', 'ancestors'):
            self.assertNotIn('"%s"' % key, text)
        self.assertNotIn('my own note', text)
        self.no_folder(obj)

    def test_over_the_tunnel_a_warning_that_names_a_file_is_a_fixed_phrase(self):
        from kitlib.lockui import model
        os.makedirs(os.path.dirname(self.store.path('queue.json')), exist_ok=True)
        with open(self.store.path('queue.json'), 'w', encoding='utf-8') as f:
            f.write('not a queue')
        status, local = self.call('/lock/view')
        self.assertEqual(status, 200)
        self.assertTrue(any('queue.json' in w for w in local['view']['warnings']),
                        local['view']['warnings'])
        status, remote = self.call('/lock/view', via=httpd.TUNNEL_VIA)
        self.assertEqual(status, 200)
        self.assertIn(model.REMOTE_SAYS['queue'], remote['view']['warnings'])
        self.assertFalse(any('queue.json' in w for w in remote['view']['warnings']))
        self.no_folder(remote)

    def test_stats_reads_the_history_and_refuses_a_bad_time(self):
        status, obj = self.call('/lock/stats', {'days': '7'})
        self.assertEqual(status, 200, obj)
        self.assertEqual(obj['rows'], [])
        self.assertIn('since', obj['range'])
        status, obj = self.call('/lock/stats', {'since': '2026-10-01T00:00:00Z',
                                                'until': '2026-10-02T00:00:00Z'})
        self.assertEqual(status, 200, obj)
        self.assertEqual(obj['range'], {'since': '2026-10-01T00:00:00Z',
                                        'until': '2026-10-02T00:00:00Z'})
        for query, field in (({'since': 'yesterday'}, 'since'), ({'until': '2026-10-02'}, 'until')):
            status, obj = self.call('/lock/stats', query)
            self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                             (400, 'invalid', field), query)

    def test_a_lock_write_wakes_the_wait(self):
        since = self.store.revision()
        got = []
        t = threading.Thread(target=lambda: got.append(self.call(
            '/lock/wait', {'since': str(since), 'timeout': str(TIMEOUT)})), daemon=True)
        t.start()
        self.addCleanup(t.join, TIMEOUT)
        self.store.bump()
        t.join(TIMEOUT)
        self.assertEqual(len(got), 1, 'the wait was not woken by the lock write')
        status, obj = got[0]
        self.assertEqual(status, 200, obj)
        self.assertIs(obj['changed'], True)
        self.assertEqual(obj['lock_revision'], since + 1)
        self.assertEqual(obj['view']['revision'], since + 1)

    def test_a_quiet_lock_times_the_wait_out(self):
        since = self.store.revision()
        status, obj = self.call('/lock/wait', {'since': str(since), 'timeout': '0.1'})
        self.assertEqual(status, 200, obj)
        self.assertIs(obj['changed'], False)
        self.assertEqual(obj['lock_revision'], since)

    def test_closing_the_hub_store_ends_a_lock_wait(self):
        since = self.store.revision()
        got = []
        t = threading.Thread(target=lambda: got.append(self.call(
            '/lock/wait', {'since': str(since), 'timeout': str(TIMEOUT)})), daemon=True)
        t.start()
        self.addCleanup(t.join, TIMEOUT)
        self.hub.close()
        t.join(TIMEOUT)
        self.assertEqual(len(got), 1, 'the wait outlived the hub store')
        status, obj = got[0]
        self.assertEqual((status, obj['error']['code']), (503, 'store'))


if __name__ == '__main__':
    unittest.main()
