"""The api's handoffs routes (docs/action-hub.md "handoffs/C-NNN"): a session records that it
checkpointed and stands down, beside its ready reply to the PM; the hub stamps the time, keeps
every record and reads back the newest of each session."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import hubvalidate
from kitlib.hubsite import api, schema
from kitlib.hubsite.api import AGENT, ANY, BASE, OWNER
from kitlib.hubsite.store import Store

STATE = r'Z:\AgentTemp\kit-coordinator\state-kit-1.md'


class Clock(object):
    """A clock that moves a second on each read."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return '2026-01-02T03:04:%02d+00:00' % self.n


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix='kit-handoffs-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.clock = Clock()
        self.store = Store.open(os.path.join(folder, 'store'), clock=self.clock)
        self.addCleanup(self.store.close)
        self.keys = {'kit-1', 'kit-2'}
        self.api = api.Api(self.store, clock=self.clock, session_keys=lambda: self.keys)

    def call(self, method, path, role, body=None, query=None, via=''):
        status, obj = self.api.call(method, BASE + path, role, body, query, via=via)
        json.dumps(obj)
        return status, obj

    def post(self, who='kit-1', kind='handoff', path=STATE, role=AGENT):
        return self.call('POST', '/handoffs', role, {'from': who, 'kind': kind,
                                                      'state_path': path})


class WriteTest(Case):
    def test_a_record_holds_the_key_kind_path_and_the_hubs_time(self):
        status, obj = self.post(kind='restart', path='  %s ' % STATE)
        self.assertEqual(status, 200, obj)
        doc_id = obj['id']
        self.assertEqual(doc_id, 'C-001')
        doc = self.store.docs('handoffs')[doc_id]
        self.assertEqual({k: doc[k] for k in ('from', 'kind', 'state_path')},
                         {'from': 'kit-1', 'kind': 'restart', 'state_path': STATE})
        self.assertRegex(doc['at'], r'^2026-01-02T03:04:\d\d\+00:00$')
        self.assertEqual(schema.check('handoffs', doc), [])

    def test_the_pm_key_is_known_and_each_kind_is_taken(self):
        for kind in api.HANDOFF_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(self.post(who='pm', kind=kind)[0], 200)
        self.assertEqual(api.HANDOFF_KINDS, ('handoff', 'restart', 'stop'))

    def test_the_id_prefix_is_its_own(self):
        self.assertEqual(hubvalidate.REF_COLLECTIONS['C'], 'handoffs')
        self.assertEqual(schema.ID_PREFIX['handoffs'], 'C')

    def test_what_the_route_refuses(self):
        body = {'from': 'kit-1', 'kind': 'stop', 'state_path': STATE}
        cases = [(dict(body, kind='pause'), 'kind'),
                 (dict(body, kind=''), 'kind'),
                 ({'from': 'kit-1', 'state_path': STATE}, 'kind'),
                 (dict(body, state_path=''), 'state_path'),
                 (dict(body, state_path='   '), 'state_path'),
                 (dict(body, state_path='state-kit-1.md'), 'state_path'),
                 (dict(body, state_path=STATE + '\nx'), 'state_path'),
                 (dict(body, state_path=STATE + '\ud800'), 'state_path'),
                 (dict(body, state_path=7), 'state_path'),
                 (dict(body, **{'from': 'nobody'}), 'from'),
                 (dict(body, **{'from': ''}), 'from'),
                 ({'kind': 'stop', 'state_path': STATE}, 'from'),
                 (dict(body, at='2020-01-01T00:00:00+00:00'), 'at'),
                 (dict(body, extra=1), 'extra')]
        for b, field in cases:
            with self.subTest(body=b):
                status, obj = self.call('POST', '/handoffs', AGENT, b)
                self.assertEqual((status, obj['error']['field']), (400, field), obj)
        self.assertEqual(self.store.docs('handoffs'), {})

    def test_an_unknown_key_is_refused_as_a_questions_is_naming_the_known_ones(self):
        status, obj = self.post(who='kit-9')
        self.assertEqual(status, 400)
        said = obj['error']['message']
        self.assertIn('a hand-off record comes from a launcher session key', said)
        self.assertIn('kit-1, kit-2, pm', said)

    def test_unreadable_session_keys_leave_only_the_pm_known(self):
        def unreadable():
            raise OSError('sessions.json: ' + STATE)
        self.api = api.Api(self.store, clock=self.clock, session_keys=unreadable)
        status, obj = self.post()
        self.assertEqual((status, obj['error']['field']), (400, 'from'))
        self.assertIn('could not be read (OSError)', obj['error']['message'])
        self.assertNotIn('sessions.json', obj['error']['message'])
        self.assertEqual(self.post(who='pm')[0], 200)

    def test_the_owner_cannot_write_one(self):
        self.assertEqual(self.post(role=OWNER)[0], 403)
        self.assertEqual(api.match('GET', BASE + '/handoffs')[0].role, ANY)


class ReadTest(Case):
    def test_the_newest_record_of_each_session_sorted_by_key(self):
        self.post(who='kit-2', kind='handoff', path=r'C:\a\one.md')
        self.post(who='kit-1', kind='handoff', path=r'C:\a\two.md')
        self.post(who='kit-2', kind='stop', path=r'C:\a\three.md')
        status, obj = self.call('GET', '/handoffs', AGENT)
        self.assertEqual(status, 200, obj)
        self.assertEqual([(d['id'], d['from'], d['kind'], d['state_path']) for d in obj['items']],
                         [('C-002', 'kit-1', 'handoff', r'C:\a\two.md'),
                          ('C-003', 'kit-2', 'stop', r'C:\a\three.md')])
        self.assertEqual(len(self.store.docs('handoffs')), 3, 'every record is kept')

    def test_from_reads_one_sessions_newest_and_an_unknown_one_reads_none(self):
        self.post(who='kit-1', kind='handoff')
        self.post(who='kit-1', kind='restart')
        self.post(who='kit-2', kind='stop')
        status, obj = self.call('GET', '/handoffs', OWNER, query={'from': 'kit-1'})
        self.assertEqual([(d['id'], d['kind']) for d in obj['items']], [('C-002', 'restart')])
        self.assertEqual(self.call('GET', '/handoffs', AGENT, query={'from': 'x'})[1]['items'],
                         [])

    def test_newest_orders_ids_by_number_not_text(self):
        docs = {'C-%03d' % n: {'from': 'k', 'kind': 'stop', 'state_path': 'C:\\%d' % n}
                for n in (9, 10, 1000)}
        docs['C-1001'] = {'kind': 'stop'}              # no from: left out
        got = api.newest_handoffs(docs)
        self.assertEqual(list(got), ['k'])
        self.assertEqual((got['k']['id'], got['k']['state_path']), ('C-1000', 'C:\\1000'))

    def test_over_the_tunnel_the_state_path_is_cut(self):
        self.post()
        obj = self.call('GET', '/handoffs', OWNER, via=api.TUNNEL_VIA)[1]
        self.assertEqual(obj['items'][0]['state_path'], '.../state-kit-1.md')
        obj = self.call('GET', '/handoffs', OWNER)[1]
        self.assertEqual(obj['items'][0]['state_path'], STATE)
        # a path the finding regex stops short in (a backtick) is still cut to its file name
        self.post(who='kit-2', path=r'Z:\Agent`Temp\a b\state-kit-2.md')
        items = self.call('GET', '/handoffs', AGENT, via=api.TUNNEL_VIA)[1]['items']
        self.assertEqual([d['state_path'] for d in items],
                         ['.../state-kit-1.md', '.../state-kit-2.md'])

    def test_a_record_is_never_pending_and_the_snapshot_leaves_them_out(self):
        self.post()
        self.assertEqual(self.call('GET', '/pending', AGENT)[1]['items'], [])
        status, obj = self.call('GET', '/snapshot', OWNER)
        self.assertEqual(status, 200)
        self.assertNotIn('handoffs', obj['collections'])


if __name__ == '__main__':
    unittest.main()
