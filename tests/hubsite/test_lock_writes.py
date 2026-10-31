"""The hub's machine lock writes (POST lock/...) and kitlib.hubsite.lockdoor's write methods.

By the owner's decision: on this machine the owner's login may change the lock, over the tunnel
every lock write needs the strong owner; a write that decision does not name (any hold write, the
division) is the strong owner's everywhere. An agent may change nothing through the hub. The
role rules are tested over a stand-in door (tests.hubsite.test_api_roles.LockDoor); the writes
themselves over the real door on a scratch lock store (tests.lock.helpers), never this
machine's."""
import datetime as dt
import json
import os
import threading
import time
import unittest
from unittest import mock

from kitlib.hubsite import api, httpd, lockdoor
from kitlib.hubsite.api import AGENT, APPROVER, BASE, LOCK_WRITE, OWNER
from kitlib.hubsite.store import Store as HubStore
from kitlib.lock import api as lock_api
from tests.hubsite import httpfix
from tests.hubsite import test_api_roles as roles
from tests.lock import helpers as lh

UTC = dt.timezone.utc
TUNNEL = httpd.TUNNEL_VIA
NO_HOLD = 'h-20261006T120000Z-abcd'
NO_RUN = 'r-20261006T120000Z-abcd'


def write_routes():
    return [r for r in api.ROUTES if r.method == 'POST' and roles.key(r)[1].startswith('/lock/')]


def iso(t):
    return t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


# ------------------------------------------------------------------ the rules, a stand-in door
class RulesCase(httpfix.ListenerCase):
    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.door = roles.LockDoor()
        self.api = api.Api(self.store, views=httpfix.Views(), clock=httpfix.CLOCK, lock=self.door)

    def call(self, route_or_path, body=None, role=OWNER, via='api', strong=False, words=False):
        path = route_or_path if isinstance(route_or_path, str) else roles.path(route_or_path, 'X-1')
        method = 'GET' if '/lock/view' in path or '/lock/wait' in path else 'POST'
        return self.api.call(method, path, role, body, {'timeout': '0'} if method == 'GET' else {},
                             via, agent_words=words, strong=strong)

    def test_the_write_routes_and_their_roles(self):
        got = {roles.key(r): r.role for r in write_routes()}
        self.assertEqual(got, {
            ('POST', '/lock/holds'): APPROVER, ('POST', '/lock/holds/{id}/edit'): APPROVER,
            ('POST', '/lock/holds/{id}/end'): APPROVER, ('POST', '/lock/division'): APPROVER,
            ('POST', '/lock/runs/{id}/end'): LOCK_WRITE, ('POST', '/lock/runs/{id}/top'): LOCK_WRITE,
            ('POST', '/lock/game_mode'): LOCK_WRITE, ('POST', '/lock/games'): LOCK_WRITE})
        self.assertNotIn(LOCK_WRITE, api.CALLERS)
        self.assertNotIn(api.wait, [r.handler for r in write_routes()])

    def test_who_may_call_each_write(self):
        for route in write_routes():
            body = roles.row(route, OWNER)[1]
            for role in (OWNER, AGENT):
                for via in ('api', 'web', '', TUNNEL):
                    for strong in (False, True):
                        with self.subTest(route=route.pattern, role=role, via=via, strong=strong):
                            before = list(self.door.wrote)
                            status, obj = self.call(route, body, role, via, strong)
                            want = roles.allowed(route, role, via, strong)
                            if want:
                                self.assertEqual((status, obj.get('error')), (200, None))
                                self.assertEqual(len(self.door.wrote), len(before) + 1)
                                self.assertIs(self.door.wrote[-1][2], via == TUNNEL)
                            else:
                                self.assertEqual((status, obj['error']['code'],
                                                  obj['error']['field']), (403, 'forbidden', 'role'))
                                self.assertEqual(self.door.wrote, before, 'a refused call wrote')
                            if role == AGENT:
                                self.assertFalse(want)
                            if route.role == LOCK_WRITE and role == OWNER:
                                self.assertEqual(want, strong or via != TUNNEL)

    def test_the_view_says_what_this_caller_may_change(self):
        why = api.WRITES_WHY
        for path in (BASE + '/lock/view', BASE + '/lock/wait'):
            for via, strong, want in (
                    ('api', False, {'lock': True, 'strong': False, 'why': why['strong']}),
                    ('api', True, {'lock': True, 'strong': True, 'why': ''}),
                    (TUNNEL, True, {'lock': True, 'strong': True, 'why': ''}),
                    (TUNNEL, False, {'lock': False, 'strong': False, 'why': why['tunnel']})):
                with self.subTest(path=path, via=via, strong=strong):
                    status, obj = self.call(path, via=via, strong=strong)
                    self.assertEqual(status, 200, obj)
                    self.assertEqual(obj['writes'], want)
        # a hub the lock takes for an agent may change nothing, strong or not
        self.door.writer = 'agent'
        for via, strong in (('api', False), ('api', True), (TUNNEL, True)):
            status, obj = self.call(BASE + '/lock/view', via=via, strong=strong)
            self.assertEqual(obj['writes'], {'lock': False, 'strong': False,
                                             'why': why['agent']})
            self.assertEqual(obj['writer'], 'agent')

    def test_a_write_answers_its_result_and_the_view_after_it(self):
        status, obj = self.call(BASE + '/lock/game_mode', {'enabled': False})
        self.assertEqual(status, 200, obj)
        self.assertEqual(obj['result'], {'verb': 'game_mode'})
        self.assertEqual(obj['view'], {'remote': False})
        self.assertEqual((obj['lock_revision'], obj['writer'], obj['ok']), (7, 'owner', True))
        self.assertEqual(obj['writes']['lock'], True)
        self.assertEqual(self.door.wrote, [('game_mode', (False,), False)])

    def test_each_body_reaches_the_door_as_given(self):
        S = dict(strong=True)
        cases = (
            ('/lock/holds', {'kind': 'reserve', 'scope': 'half', 'start': 'a', 'end': 'b',
                             'note': 'n'}, ('reserve', ('half', 'a', 'b', 'n'))),
            ('/lock/holds', {'kind': 'hold', 'scope': 'slots', 'end': '+2h', 'cpu': '0-1',
                             'gpu': ''}, ('hold', ('slots', None, '+2h', None, '0-1', ''))),
            ('/lock/holds/H-1/edit', {'end': '18:00'}, ('edit', ('H-1', None, '18:00', None))),
            ('/lock/holds/H-1/edit', {'note': ''}, ('edit', ('H-1', None, None, ''))),
            ('/lock/holds/H-1/end', None, ('end_hold', ('H-1',))),
            ('/lock/division', {'fields': {'cpu_slots': '6'}}, ('division', ({'cpu_slots': '6'},))),
            ('/lock/division', {'defaults': True}, ('division', (None,))),
            ('/lock/runs/R-1/end', {'confirm': 'R-1'}, ('end_run', ('R-1',))),
            ('/lock/runs/R-1/top', {}, ('move_to_top', ('R-1',))),
            ('/lock/game_mode', {'enabled': True}, ('game_mode', (True,))),
            ('/lock/games', {'text': 'A.exe pin\n'}, ('games', ('A.exe pin\n',))),
            ('/lock/games', {'text': None}, ('games', ('',))))
        for path, body, want in cases:
            with self.subTest(path=path, body=body):
                status, obj = self.call(BASE + path, body, **S)
                self.assertEqual(status, 200, obj)
                self.assertEqual(self.door.wrote[-1][:2], want)

    def test_a_bad_body_is_invalid_and_writes_nothing(self):
        S = dict(strong=True)
        cases = (
            ('/lock/holds', {'kind': 'hold', 'scope': 'whole', 'end': '+1h', 'owner': 'agent'},
             'owner'),
            ('/lock/holds', {'kind': 'reserve', 'scope': 'whole', 'start': 'a', 'end': 'b',
                             'cpu': '1'}, 'cpu'),
            ('/lock/holds', {'kind': 'agent', 'scope': 'whole'}, 'kind'),
            ('/lock/holds', {'scope': 'whole'}, 'kind'),
            ('/lock/holds', {'kind': 'hold', 'scope': 3}, 'scope'),
            ('/lock/holds', ['not', 'an', 'object'], 'body'),
            ('/lock/holds/H-1/edit', {'end': 5}, 'end'),
            ('/lock/holds/H-1/edit', {'owner': 'owner'}, 'owner'),
            ('/lock/holds/H-1/end', {'why': 'x'}, 'why'),
            ('/lock/division', {}, 'fields'),
            ('/lock/division', {'fields': {}, 'defaults': True}, 'fields'),
            ('/lock/division', {'fields': 'cpu_slots=6'}, 'fields'),
            ('/lock/division', {'defaults': 'yes'}, 'defaults'),
            ('/lock/runs/R-1/end', {}, 'confirm'),
            ('/lock/runs/R-1/end', {'confirm': 'R-2'}, 'confirm'),
            ('/lock/runs/R-1/top', {'confirm': 'R-1'}, 'confirm'),
            ('/lock/game_mode', {}, 'enabled'),
            ('/lock/game_mode', {'enabled': 'on'}, 'enabled'),
            ('/lock/game_mode', {'enabled': None}, 'enabled'),
            ('/lock/games', {'text': ['A.exe']}, 'text'),
            ('/lock/games', {'rows': []}, 'rows'))
        for path, body, field in cases:
            with self.subTest(path=path, body=body):
                status, obj = self.call(BASE + path, body, **S)
                self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                                 (400, 'invalid', field), obj)
        self.assertEqual(self.door.wrote, [])

    def test_a_server_with_no_lock_says_unavailable(self):
        a = api.Api(self.store, views=httpfix.Views(), clock=httpfix.CLOCK)
        for route in write_routes():
            with self.subTest(route=route.pattern):
                status, obj = a.call('POST', roles.path(route, 'X-1'), OWNER,
                                     roles.row(route, OWNER)[1], {}, 'api', strong=True)
                self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                                 (503, 'unavailable', 'lock'))

    def test_a_door_that_raises_is_unavailable_and_names_no_path(self):
        def boom(*a, **kw):
            raise RuntimeError('boom in D:\\secret\\place')
        self.door.game_mode = boom
        for words in (False, True):
            status, obj = self.call(BASE + '/lock/game_mode', {'enabled': True}, words=words)
            self.assertEqual((status, obj['error']['code']), (503, 'unavailable'))
            self.assertNotIn('secret', json.dumps(obj))

    def test_the_agent_is_refused_every_write_on_the_local_listener(self):
        lis = self.listener()
        for route in write_routes():
            body = roles.row(route, OWNER)[1]
            with self.subTest(route=route.pattern):
                for token, via in ((self.agent, None), (self.agent, TUNNEL), (None, None)):
                    r = httpfix.post(lis.port, roles.path(route, 'X-1'), body, token, via=via)
                    self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                    self.assertNotIn('view', r.json())
                r = httpfix.post(lis.port, roles.path(route, 'X-1'), body, self.owner)
                if route.role == LOCK_WRITE:
                    self.assertEqual((r.status, r.json().get('error')), (200, None))
                else:
                    self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertEqual([w[0] for w in self.door.wrote],
                         ['end_run', 'move_to_top', 'game_mode', 'games'])
        self.assertEqual(self.errors(), [])


# ------------------------------------------------------------------ the real door, a scratch lock
class DoorWriteCase(lh.StoreCase):
    ACTOR = lh.OWNER

    def setUp(self):
        lh.StoreCase.setUp(self)
        self.hub = HubStore.open(os.path.join(self.tmp, 'hub'), clock=httpfix.CLOCK)
        self.addCleanup(self.hub.close)
        self.door = lockdoor.LockDoor(make_store=lambda: self.store, actor=self.ACTOR)
        self.api = api.Api(self.hub, views=httpfix.Views(), clock=httpfix.CLOCK, lock=self.door)

    def post(self, path, body=None, via='api', strong=True):
        return self.api.call('POST', BASE + path, OWNER, body, {}, via, strong=strong)

    def ok(self, path, body=None, via='api', strong=True):
        status, obj = self.post(path, body, via, strong)
        self.assertEqual(status, 200, obj)
        return obj

    def refused(self, path, body, status, code, field=None, via='api'):
        got, obj = self.post(path, body, via)
        self.assertEqual((got, obj['error']['code']), (status, code), obj)
        if field is not None:
            self.assertEqual(obj['error']['field'], field)
        return obj

    def holds(self):
        return lock_api.snapshot(self.store)['holds']

    def reserve(self, via='api', scope='whole', note=''):
        now = dt.datetime.now(UTC)
        return self.ok('/lock/holds', {'kind': 'reserve', 'scope': scope,
                                       'start': iso(now + dt.timedelta(hours=1)),
                                       'end': iso(now + dt.timedelta(hours=2)), 'note': note},
                       via)

    def no_folder(self, obj):
        text = json.dumps(obj)
        for folder in (self.root, self.tmp):
            self.assertNotIn(folder, text)
            self.assertNotIn(json.dumps(folder)[1:-1], text)


class DoorWriteTest(DoorWriteCase):
    def test_a_reserve_is_an_owner_hold_made_by_the_hub(self):
        obj = self.reserve(note='deep work')
        hold = obj['result']['hold']
        self.assertEqual((hold['owner'], hold['scope'], hold['note'], hold['by']),
                         ('owner', 'whole', 'deep work', lockdoor.LOCAL_ACTOR))
        self.assertEqual(sorted(hold), sorted(lockdoor.HOLD_KEYS))
        self.assertEqual([h['id'] for h in self.holds()], [hold['id']])
        # the answer carries the view after the write, which shows the hold
        self.assertEqual(obj['lock_revision'], self.store.revision())
        self.assertIn(hold['id'], [h['id'] for h in obj['view']['holds']])
        self.assertEqual(obj['writer'], 'owner')
        obj = self.reserve(via=httpd.TUNNEL_VIA, scope='half')
        self.assertEqual(obj['result']['hold']['by'], lockdoor.REMOTE_ACTOR)
        self.assertEqual(obj['result']['hold']['note'], 'reserve window')
        self.no_folder(obj)

    def test_the_view_and_a_write_answer_carry_the_fields_the_schedule_page_reads(self):
        # index.html's Schedule reads these keys of GET lock/view and of the view after a write
        # (tests/hubsite/pagedom.py stubs them); a missing key would blank the page, so pin them here.
        want = {'now', 'cores', 'strip', 'warnings', 'runs', 'queue_items', 'holds', 'agent_holds'}
        status, got = self.api.call('GET', BASE + '/lock/view', OWNER, None, {}, 'api', strong=True)
        self.assertEqual(status, 200, got)
        self.assertEqual({'lock_revision', 'view', 'writer', 'writes'} - set(got), set())
        self.assertEqual((got['writer'], got['writes']), ('owner', {'lock': True, 'strong': True, 'why': ''}))
        self.assertEqual(want - set(got['view']), set())
        self.assertIsInstance(got['view']['queue_items'], list)
        self.assertIsInstance(got['view']['now'], str)
        wrote = self.reserve()
        self.assertEqual({'ok', 'result', 'lock_revision', 'view', 'writer', 'writes'} - set(wrote), set())
        self.assertEqual(want - set(wrote['view']), set())
        self.assertEqual(wrote['writes'], got['writes'])
        self.assertGreater(wrote['lock_revision'], got['lock_revision'])

    def test_a_reserve_needs_times_with_their_offset(self):
        for body, field in (({'start': '2026-10-08T12:00', 'end': '2026-10-08T13:00Z'}, 'start'),
                            ({'start': '2026-10-08T12:00Z', 'end': 'later'}, 'end'),
                            ({'start': '2026-10-08T12:00Z'}, 'end'),
                            ({'start': '2026-10-08T13:00Z', 'end': '2026-10-08T12:00Z'},
                             'scope')):
            with self.subTest(body=body):
                self.refused('/lock/holds', dict(body, kind='reserve', scope='whole'), 400,
                             'invalid', field)
        self.refused('/lock/holds', {'kind': 'reserve', 'scope': 'slots',
                                     'start': '2026-10-08T12:00Z', 'end': '2026-10-08T13:00Z'},
                     400, 'invalid', 'scope')
        self.assertEqual(self.holds(), [])

    def test_a_hold_from_the_dialogs_text_is_read_in_this_pcs_zone(self):
        obj = self.ok('/lock/holds', {'kind': 'hold', 'scope': 'half', 'start': '+1h',
                                      'end': '+3h', 'note': 'build'})
        hold = obj['result']['hold']
        start = dt.datetime.fromisoformat(hold['start'].replace('Z', '+00:00'))
        end = dt.datetime.fromisoformat(hold['end'].replace('Z', '+00:00'))
        # an end of +3h is three hours after the start, not after now
        self.assertAlmostEqual((end - start).total_seconds(), 3 * 3600, delta=5)
        self.assertAlmostEqual((start - dt.datetime.now(UTC)).total_seconds(), 3600, delta=60)
        self.assertEqual((hold['scope'], hold['note']), ('half', 'build'))
        # a bare HH:MM is this PC's local time
        # (tomorrow's offset, not today's: the clocks may change overnight)
        local = (dt.datetime.now() + dt.timedelta(days=1)).replace(
            hour=14, minute=0, second=0, microsecond=0).astimezone()
        day = local.strftime('%Y-%m-%d')
        obj = self.ok('/lock/holds', {'kind': 'hold', 'scope': 'whole',
                                      'start': day + ' 14:00', 'end': day + ' 15:00'})
        self.assertEqual(obj['result']['hold']['start'], iso(local))
        for body in ({'end': 'soon'}, {'end': ''}, {'scope': 'slots', 'end': '+1h'},
                     {'scope': 'slots', 'end': '+1h', 'cpu': 'x'}):
            with self.subTest(body=body):
                self.refused('/lock/holds', dict({'kind': 'hold', 'scope': 'whole'}, **body),
                             400, 'invalid', 'hold')
        self.assertEqual(len(self.holds()), 2)

    def test_edit_and_end_a_hold(self):
        hid = self.reserve(note='first')['result']['hold']['id']
        obj = self.ok('/lock/holds/%s/edit' % hid, {'note': 'moved'})
        self.assertEqual(obj['result']['hold']['note'], 'moved')
        # an edit with no note keeps the hold's own
        obj = self.ok('/lock/holds/%s/edit' % hid, {'end': '+5h'})
        self.assertEqual(obj['result']['hold']['note'], 'moved')
        self.refused('/lock/holds/%s/edit' % hid, {}, 400, 'invalid', 'edit')
        self.refused('/lock/holds/%s/edit' % hid, {'note': 'moved'}, 400, 'invalid', 'edit')
        self.refused('/lock/holds/%s/edit' % hid, {'end': 'whenever'}, 400, 'invalid', 'edit')
        obj = self.ok('/lock/holds/%s/end' % hid, {})
        self.assertEqual(obj['result']['hold']['id'], hid)
        self.assertEqual(self.holds(), [])
        self.refused('/lock/holds/%s/end' % hid, {}, 404, 'not_found', 'id')
        self.refused('/lock/holds/%s/edit' % hid, {'note': 'x'}, 404, 'not_found', 'id')

    def test_an_id_the_lock_does_not_make_never_reaches_a_path(self):
        for bad in ('nope', '..', 'h-..%5c..', NO_RUN):
            with self.subTest(id=bad):
                self.refused('/lock/holds/%s/end' % bad, {}, 400, 'invalid', 'id')
                self.refused('/lock/holds/%s/edit' % bad, {'note': 'x'}, 400, 'invalid', 'id')
        for bad in ('nope', NO_HOLD):
            with self.subTest(id=bad):
                self.refused('/lock/runs/%s/end' % bad, {'confirm': bad}, 400, 'invalid', 'id')
                self.refused('/lock/runs/%s/top' % bad, {}, 400, 'invalid', 'id')
        self.refused('/lock/holds/%s/end' % NO_HOLD, {}, 404, 'not_found', 'id')
        self.refused('/lock/runs/%s/end' % NO_RUN, {'confirm': NO_RUN}, 404, 'not_found', 'id')
        self.refused('/lock/runs/%s/top' % NO_RUN, {}, 404, 'not_found', 'id')

    def test_end_a_live_run(self):
        self.live_record(NO_RUN)
        obj = self.ok('/lock/runs/%s/end' % NO_RUN, {'confirm': NO_RUN}, strong=False)
        self.assertEqual(obj['result'], {'id': NO_RUN, 'requested': True})

    def test_game_mode_and_the_games_list(self):
        obj = self.ok('/lock/games', {'text': '# mine\nGame.exe nopin My Game\nOther.exe pin\n'},
                      strong=False)
        self.assertEqual(obj['result']['games'],
                         [{'exe': 'Game.exe', 'name': 'My Game', 'pin': False},
                          {'exe': 'Other.exe', 'name': None, 'pin': True}])
        obj = self.ok('/lock/game_mode', {'enabled': True}, strong=False)
        self.assertEqual(obj['result'], {'enabled': True})
        self.assertIs(lock_api.snapshot(self.store)['game']['enabled'], True)
        self.assertIs(self.ok('/lock/game_mode', {'enabled': False})['result']['enabled'], False)

    def test_the_division_and_back_to_the_defaults(self):
        obj = self.ok('/lock/division', {'fields': {'cpu_slots': '6', 'half_cpu_slots': ''}})
        self.assertIs(obj['result']['applied'], True)
        self.assertEqual(lock_api.snapshot(self.store)['layout']['cpu_slots'], 6)
        self.refused('/lock/division', {'fields': {'cpu_slots': 'six'}}, 400, 'invalid', 'fields')
        self.refused('/lock/division', {'fields': {'colour': '1'}}, 400, 'invalid', 'fields')
        self.refused('/lock/division', {'fields': {'cpu_slots': 6}}, 400, 'invalid', 'fields')
        self.ok('/lock/division', {'defaults': True})
        L = lock_api.snapshot(self.store)['layout']
        self.assertEqual(L['division'], L['default_division'])


class AgentHubTest(DoorWriteCase):
    """A hub the lock takes for an agent (started under an agent session): its view says so, and
    every write is refused before the lock is asked, with nothing written."""
    ACTOR = dict(lh.OWNER, role='agent', session='s-1', host='claude')

    def test_the_view_says_writer_agent_and_every_write_is_refused(self):
        status, obj = self.api.call('GET', BASE + '/lock/view', OWNER, None, {}, 'api',
                                    strong=True)
        self.assertEqual(status, 200, obj)
        self.assertEqual(obj['writer'], 'agent')
        self.assertEqual(obj['writes'], {'lock': False, 'strong': False,
                                         'why': api.WRITES_WHY['agent']})
        before = self.store.revision()
        with mock.patch.object(lock_api, 'hold_create') as made, \
                mock.patch.object(lock_api, 'set_game_mode') as mode:
            for route in write_routes():
                with self.subTest(route=route.pattern):
                    body = roles.row(route, OWNER)[1]
                    if roles.key(route)[1] == '/lock/holds':
                        now = dt.datetime.now(UTC)
                        body = dict(body, start=iso(now + dt.timedelta(hours=1)),
                                    end=iso(now + dt.timedelta(hours=2)))
                    path = roles.path(route, NO_HOLD if '/holds/' in route.pattern else NO_RUN)
                    if body.get('confirm'):
                        body = dict(body, confirm=NO_RUN)
                    status, obj = self.api.call('POST', path, OWNER, body, {}, 'api', strong=True)
                    self.assertEqual((status, obj['error']['code']), (403, 'forbidden'), obj)
                    self.assertEqual(obj['error']['message'], lockdoor.WHY_AGENT)
            self.assertEqual((made.call_count, mode.call_count), (0, 0))
        self.assertEqual(self.store.revision(), before)
        self.assertEqual(self.holds(), [])


class RefusalTest(DoorWriteCase):
    """The lock's refusals as the hub answers them: busy is 409, a store failure is unavailable,
    and over the tunnel nothing that names a folder is sent."""

    def answer(self, error, via='api'):
        got = {'ok': False, 'error': error, 'snapshot': {'secret': 'C:\\lock\\store'}}
        with mock.patch.object(lock_api, 'set_game_mode', return_value=got):
            return self.post('/lock/game_mode', {'enabled': True}, via)

    def test_each_code(self):
        for code, status, hub_code, field in (
                ('invalid', 400, 'invalid', 'enabled'), ('forbidden', 403, 'forbidden', 'lock'),
                ('not_found', 404, 'not_found', 'id'), ('conflict', 409, 'conflict', 'lock'),
                ('busy', 409, 'conflict', 'lock'), ('store', 503, 'unavailable', 'lock'),
                ('something new', 503, 'unavailable', 'lock')):
            with self.subTest(code=code):
                got, obj = self.answer({'code': code, 'message': 'why', 'field':
                                        'enabled' if code == 'invalid' else None})
                self.assertEqual((got, obj['error']['code'], obj['error']['field']),
                                 (status, hub_code, field))
                self.assertNotIn('secret', json.dumps(obj))
                if code == 'busy':
                    self.assertIn('busy', obj['error']['message'])

    def test_over_the_tunnel_a_folder_is_never_named(self):
        path = os.path.join(self.root, 'queue.json')
        for code, said in (('store', lockdoor.REMOTE_SAYS['store']),
                           ('conflict', lockdoor.REMOTE_SAYS['path']),
                           ('invalid', lockdoor.REMOTE_SAYS['path'])):
            with self.subTest(code=code):
                error = {'code': code, 'message': '%s could not be written' % path}
                got, local = self.answer(error)
                self.assertIn(path, local['error']['message'])
                got, remote = self.answer(error, via=httpd.TUNNEL_VIA)
                self.assertIn(said, remote['error']['message'])
                self.no_folder(remote)
                for unc in ('\\\\server\\share\\x', 'D:/lock/x'):
                    got, remote = self.answer({'code': code, 'message': 'at ' + unc},
                                              via=httpd.TUNNEL_VIA)
                    self.assertNotIn(unc, json.dumps(remote))

    def test_a_note_that_names_a_folder_stays_on_this_machine(self):
        result = {'hold': {'id': NO_HOLD, 'owner': 'owner', 'secret': 1},
                  'notes': ['overlaps a run', 'see C:\\lock\\store\\x.json']}
        with mock.patch.object(lock_api, 'hold_end',
                               return_value={'ok': True, 'result': result, 'snapshot': None}):
            _, local = self.post('/lock/holds/%s/end' % NO_HOLD)
            _, remote = self.post('/lock/holds/%s/end' % NO_HOLD, via=httpd.TUNNEL_VIA)
        self.assertEqual(local['result']['notes'], result['notes'])
        self.assertEqual(remote['result']['notes'],
                         ['overlaps a run', lockdoor.REMOTE_SAYS['path']])
        for obj in (local, remote):
            self.assertNotIn('secret', obj['result']['hold'])


class SerialTest(DoorWriteCase):
    def test_the_hubs_writes_run_one_at_a_time(self):
        inside, most, lock = [0], [0], threading.Lock()
        real = lock_api.set_game_mode

        def slow(actor, enabled, store=None):
            with lock:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
            time.sleep(0.05)
            try:
                return real(actor, enabled, store=store)
            finally:
                with lock:
                    inside[0] -= 1
        got = []
        with mock.patch.object(lock_api, 'set_game_mode', side_effect=slow):
            threads = [threading.Thread(target=lambda i=i: got.append(self.post(
                '/lock/game_mode', {'enabled': bool(i % 2)}, strong=False)), daemon=True)
                for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(httpfix.TIMEOUT)
        self.assertEqual(sorted(s for s, _ in got), [200] * 4)
        self.assertEqual(most[0], 1, 'two hub writes ran at once')


if __name__ == '__main__':
    unittest.main()
