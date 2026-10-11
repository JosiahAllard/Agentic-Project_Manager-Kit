"""kitlib.hubsite.api: what each verb writes and answers, the fields it refuses, the error
shape, versions, the views it borrows, wait, pending, snapshot and health. Who may call a route
is tests/hubsite/test_api_roles.py."""
import inspect
import json
import shutil
import tempfile
import threading
import unittest

from kitlib.hubsite import api
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import HubError, Store

NOW = '2026-01-02T03:04:05+00:00'
CLOCK = lambda: NOW      # noqa: E731
OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]


class Views(object):
    def __init__(self):
        self.calls = []

    def backlog(self, repo='', state='', fresh=False):
        self.calls.append(('backlog', repo, state, fresh))
        return [{'repo': 'r', 'items': []}]

    def completed(self, repo='', days=None):
        self.calls.append(('completed', repo, days))
        return [{'repo': 'r', 'items': []}]

    def notices(self):
        return [{'text': 'n'}]

    def health(self):
        return {'git': 'ok', 'backlog': 'ok', 'ok': False, 'revision': -1}


class ApiCase(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix='kit-hubapi-')
        self.addCleanup(shutil.rmtree, root, True)
        self.store = Store.open(root, clock=CLOCK)
        self.addCleanup(self.store.close)
        self.api = api.Api(self.store, clock=CLOCK)

    def call(self, method, path, role, body=None, query=None, **kw):
        status, obj = self.api.call(method, BASE + path, role, body, query, **kw)
        json.dumps(obj)                         # every answer is JSON-ready
        self.assertIn('revision', obj)
        return status, obj

    def ok(self, method, path, role, body=None, query=None, **kw):
        status, obj = self.call(method, path, role, body, query, **kw)
        self.assertEqual((status, obj.get('error')), (200, None))
        return obj

    def refused(self, code, field, method, path, role, body=None, query=None):
        before = self.store.snapshot()
        status, obj = self.call(method, path, role, body, query)
        self.assertEqual(obj['error']['code'], code, obj)
        self.assertEqual(status, api.STATUS[code])
        self.assertIs(obj['ok'], False)
        self.assertEqual(sorted(obj['error']), ['code', 'field', 'message'])
        if field is not None:
            self.assertEqual(obj['error']['field'], field, obj)
        self.assertEqual(self.store.snapshot(), before, 'a refused call wrote')
        return obj

    def question(self, **kw):
        body = {'title': 't', 'repo': 'kit', 'options': OPTIONS}
        body.update(kw)
        return self.ok('POST', '/questions', AGENT, body)['id']

    def story(self):
        return self.ok('POST', '/stories', OWNER, {'repo': 'kit', 'title': 't', 'story': 's'})['id']


class ModuleTest(unittest.TestCase):
    def test_no_transport_in_the_module(self):
        src = inspect.getsource(api)
        for word in ('import socket', 'import http', 'import threading', 'import argparse',
                     'print(', 'sys.exit', 'import urllib', 'import subprocess'):
            self.assertNotIn(word, src)

    def test_routes_is_plain_data_another_module_can_walk(self):
        self.assertIsInstance(api.ROUTES, tuple)
        for route in api.ROUTES:
            self.assertEqual(route._fields, ('method', 'pattern', 'role', 'handler'))
            self.assertIn(route.method, ('GET', 'POST'))
            found = api.match(route.method, api.sample_path(route))
            self.assertIsNotNone(found)
            self.assertIs(found[0], route)

    def test_match(self):
        route, params = api.match('post', BASE + '/questions/Q-001/answer')
        self.assertEqual((route.pattern, params), (BASE + '/questions/{id}/answer',
                                                   {'id': 'Q-001'}))
        # the literal `send` wins over `{id}`
        self.assertIs(api.match('POST', BASE + '/to_pm/send')[0].handler, api.to_pm_send)
        for method, path in (('GET', '/questions'), ('GET', BASE + '/questions/'),
                             ('GET', BASE + '/questions/Q-001'), ('DELETE', BASE + '/questions'),
                             ('POST', BASE + '/questions//answer'), ('GET', BASE),
                             ('GET', None), (None, BASE + '/questions'),
                             ('POST', BASE + '/questions/a/b/answer')):
            self.assertIsNone(api.match(method, path), (method, path))

    def test_no_relay_route_yet(self):
        for route in api.ROUTES:
            self.assertNotIn('relay', route.pattern)

    def test_every_code_has_a_status(self):
        for code in ('invalid', 'forbidden', 'not_found', 'conflict', 'store'):
            self.assertIn(code, api.STATUS)
        self.assertEqual((api.STATUS['invalid'], api.STATUS['forbidden'], api.STATUS['not_found'],
                          api.STATUS['conflict']), (400, 403, 404, 409))


class ErrorTest(ApiCase):
    def test_unknown_route(self):
        self.refused('not_found', 'path', 'GET', '/nothing', OWNER)
        self.refused('not_found', 'path', 'DELETE', '/questions', OWNER)
        self.refused('not_found', 'path', 'POST', '/relay', OWNER, {})

    def test_a_body_that_is_not_an_object(self):
        for body in ([], 'x', 3, True):
            self.refused('invalid', 'body', 'POST', '/questions', AGENT, body)

    def test_a_query_that_is_not_an_object(self):
        self.refused('invalid', 'query', 'GET', '/questions', OWNER, None, 'status=open')

    def test_a_store_error_keeps_its_code_and_field(self):
        qid = self.question()
        self.store.update = lambda *a, **k: (_ for _ in ()).throw(
            HubError('store', 'cannot write', 'disk'))
        status, obj = self.call('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.assertEqual((status, obj['error']), (503, {'code': 'store', 'message': 'cannot write',
                                                        'field': 'disk'}))

    def test_an_unexpected_error_is_an_answer_not_a_traceback(self):
        self.store.docs = lambda coll: 1 / 0
        status, obj = self.call('GET', '/questions', OWNER)
        self.assertEqual((status, obj['error']['code']), (500, 'internal'))
        self.assertIn('ZeroDivisionError', obj['error']['message'])

    def test_a_missing_document(self):
        for method, path, role, body in (
                ('POST', '/questions/Q-404/answer', OWNER, {'choice': 0}),
                ('POST', '/questions/Q-404/log', AGENT, {'ruling': 'decision-7', 'if_version': 1}),
                ('POST', '/todos/T-404/done', OWNER, None),
                ('POST', '/todos/T-404/ack', AGENT, None),
                ('POST', '/stories/S-404', AGENT, {'status': 'review', 'if_version': 1}),
                ('POST', '/stories/S-404/withdraw', OWNER, None),
                ('POST', '/to_pm/P-404/reply', AGENT, {'reply': 'r'}),
                ('POST', '/inbox/M-404/read', OWNER, None),
                ('POST', '/inbox/..%5C..%5Cx/read', OWNER, None),
                ('POST', '/inbox/nul/read', OWNER, None)):
            with self.subTest(path=path):
                self.refused('not_found', 'id', method, path, role, body)


class QuestionTest(ApiCase):
    def test_add(self):
        obj = self.ok('POST', '/questions', AGENT, {
            'title': 'Which?', 'repo': 'kit', 'from': 'pm', 'context': 'c', 'options': OPTIONS,
            'recommended': 1, 'refs': ['S-001'], 'urgent': True, 'story': 'S-001'})
        self.assertEqual((obj['id'], obj['version'], obj['ok']), ('Q-001', 1, True))
        self.assertEqual(obj['revision'], self.store.revision)
        self.assertIsInstance(obj['warnings'], list)
        d = self.store.get('questions', 'Q-001')
        self.assertEqual((d['status'], d['asked_at'], d['title'], d['recommended'], d['urgent']),
                         ('open', NOW, 'Which?', 1, True))
        self.assertNotIn('answer', d)

    def test_add_refuses_the_fields_the_owner_or_the_api_sets(self):
        for name, value in (('status', 'answered'), ('answer', {'choice': 0}),
                            ('ruling', 'decision-7'), ('asked_at', 'x'), ('id', 'Q-900'),
                            ('version', 9), ('updated_at', 'x'), ('nonsense', 1)):
            with self.subTest(field=name):
                self.refused('invalid', name, 'POST', '/questions', AGENT,
                             {'title': 't', 'options': OPTIONS, name: value})
        self.assertEqual(self.store.docs('questions'), {})

    def test_add_checks_its_fields(self):
        self.refused('invalid', 'title', 'POST', '/questions', AGENT, {'options': OPTIONS})
        self.refused('invalid', 'title', 'POST', '/questions', AGENT, {'title': '  '})
        self.refused('invalid', 'title', 'POST', '/questions', AGENT, {'title': 3})
        self.refused('invalid', 'options', 'POST', '/questions', AGENT,
                     {'title': 't', 'options': 'a'})
        self.refused('invalid', None, 'POST', '/questions', AGENT,
                     {'title': 't', 'options': ['not an option']})
        self.refused('invalid', 'urgent', 'POST', '/questions', AGENT,
                     {'title': 't', 'urgent': 'yes'})

    def test_answer_with_an_option(self):
        qid = self.question()
        obj = self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 1, 'note': ' n '},
                      via='web')
        want = {'choice': 1, 'label': 'second', 'note': 'n', 'at': NOW, 'via': 'web'}
        self.assertEqual((obj['status'], obj['answer'], obj['version']), ('answered', want, 2))
        d = self.store.get('questions', qid)
        self.assertEqual((d['status'], d['answer']), ('answered', want))

    def test_answer_other_needs_a_note(self):
        qid = self.question()
        self.refused('invalid', 'note', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 'other'})
        self.refused('invalid', 'note', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 'other', 'note': '   '})
        obj = self.ok('POST', '/questions/%s/answer' % qid, OWNER,
                      {'choice': 'other', 'note': 'neither'})
        self.assertEqual(obj['answer'], {'choice': 'other', 'label': 'Other', 'note': 'neither',
                                         'at': NOW, 'via': 'api'})

    def test_answer_refuses_a_choice_that_is_no_option(self):
        qid = self.question()
        for choice in (2, -1, True, False, '0', 1.0, None, [0], 'Other'):
            with self.subTest(choice=choice):
                self.refused('invalid', 'choice', 'POST', '/questions/%s/answer' % qid, OWNER,
                             {'choice': choice})
        self.refused('invalid', 'choice', 'POST', '/questions/%s/answer' % qid, OWNER, {})

    def test_answer_refuses_other_fields(self):
        qid = self.question()
        for name in ('status', 'answer', 'ruling', 'label', 'at', 'via', 'title', 'options'):
            with self.subTest(field=name):
                self.refused('invalid', name, 'POST', '/questions/%s/answer' % qid, OWNER,
                             {'choice': 0, name: 'x'})

    def test_answer_again_until_logged(self):
        qid = self.question()
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        obj = self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 1})
        self.assertEqual((obj['answer']['label'], obj['version']), ('second', 3))
        self.ok('POST', '/questions/%s/log' % qid, AGENT, {'ruling': 'decision-7', 'if_version': 3})
        self.refused('invalid', 'status', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 0})

    def test_answer_with_a_stale_version(self):
        qid = self.question()
        self.refused('conflict', 'if_version', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 0, 'if_version': 7})
        self.refused('invalid', 'if_version', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 0, 'if_version': '1'})
        self.refused('invalid', 'if_version', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 0, 'if_version': -1})
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0, 'if_version': 1})

    def test_a_write_is_pinned_to_the_version_the_check_read(self):
        # another writer gets in between this module's read and its write: the loser is told
        qid = self.question()
        real = self.store.get

        def get(coll, doc_id):
            d = real(coll, doc_id)
            self.store.get = real
            self.store.update(coll, doc_id, {'status': 'logged', 'ruling': 'decision-7'})
            return d
        self.store.get = get
        status, obj = self.call('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.assertEqual((status, obj['error']['code']), (409, 'conflict'))
        self.assertEqual(self.store.get('questions', qid)['status'], 'logged')
        self.assertNotIn('answer', self.store.get('questions', qid))

    def test_log(self):
        qid = self.question()
        path = '/questions/%s/log' % qid
        self.refused('invalid', 'status', 'POST', path, AGENT,
                     {'ruling': 'decision-7', 'if_version': 1})
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'if_version': 2})
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'ruling': ' ', 'if_version': 2})
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'ruling': 7, 'if_version': 2})
        self.refused('invalid', 'if_version', 'POST', path, AGENT, {'ruling': 'decision-7'})
        self.refused('conflict', 'if_version', 'POST', path, AGENT,
                     {'ruling': 'decision-7', 'if_version': 1})
        self.refused('invalid', 'answer', 'POST', path, AGENT,
                     {'ruling': 'decision-7', 'if_version': 2, 'answer': {'choice': 1}})
        obj = self.ok('POST', path, AGENT, {'ruling': ' decision-7 ', 'if_version': 2})
        self.assertEqual((obj['status'], obj['version']), ('logged', 3))
        d = self.store.get('questions', qid)
        self.assertEqual((d['status'], d['ruling'], d['answer']['choice']),
                         ('logged', 'decision-7', 0))
        self.refused('invalid', 'status', 'POST', path, AGENT,
                     {'ruling': 'decision-8', 'if_version': 3})

    def test_list(self):
        a = self.question(repo='Kit')
        b = self.question(repo='other')
        self.ok('POST', '/questions/%s/answer' % b, OWNER, {'choice': 0})
        ids = lambda q=None: [d['id'] for d in      # noqa: E731
                              self.ok('GET', '/questions', AGENT, None, q)['items']]
        self.assertEqual(ids(), [a, b])
        self.assertEqual(ids({'status': 'all'}), [a, b])
        self.assertEqual(ids({'status': 'open'}), [a])
        self.assertEqual(ids({'status': ['answered']}), [b])
        self.assertEqual(ids({'status': 'logged'}), [])
        self.assertEqual(ids({'repo': 'kit'}), [a])
        self.assertEqual(ids({'repo': 'kit', 'status': 'answered'}), [])
        self.assertEqual(ids({'since': '2026-01-02T03:04:05+00:00'}), [a, b])
        self.assertEqual(ids({'since': '2026-01-02T03:04:06+00:00'}), [])
        self.assertEqual(ids({'unknown': 'x'}), [a, b])
        item = self.ok('GET', '/questions', OWNER)['items'][0]
        self.assertEqual((item['title'], item['version'], item['status']), ('t', 1, 'open'))
        self.refused('invalid', 'status', 'GET', '/questions', OWNER, None, {'status': 'done'})
        self.refused('invalid', 'since', 'GET', '/questions', OWNER, None, {'since': 'monday'})
        self.refused('invalid', 'since', 'GET', '/questions', OWNER, None,
                     {'since': '2026-01-02T03:04:05'})

    def test_list_is_in_id_order(self):
        for n in (2, 10, 1):
            self.store.put('questions', 'Q-%03d' % n, {'status': 'open', 'title': 't'})
        self.store.put('questions', 'Q-1000', {'status': 'open', 'title': 't'})
        self.assertEqual([d['id'] for d in self.ok('GET', '/questions', OWNER)['items']],
                         ['Q-001', 'Q-002', 'Q-010', 'Q-1000'])


class TodoTest(ApiCase):
    def test_the_owner_adds_a_todo_as_you(self):
        obj = self.ok('POST', '/todos', OWNER, {'title': 't', 'detail': 'd', 'repo': 'kit',
                                                'urgent': True})
        d = self.store.get('todos', obj['id'])
        self.assertEqual((d['from'], d['status'], d['added_at'], d['urgent']),
                         ('you', 'open', NOW, True))
        self.refused('invalid', 'from', 'POST', '/todos', OWNER, {'title': 't', 'from': 'pm'})

    def test_an_agent_cannot_add_a_todo_as_the_owner(self):
        for name in ('you', 'You', ' YOU '):
            self.refused('invalid', 'from', 'POST', '/todos', AGENT,
                         {'title': 't', 'commands': ['dir'], 'from': name})
        obj = self.ok('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir']})
        self.assertEqual(self.store.get('todos', obj['id'])['from'], 'pm')
        obj = self.ok('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir'],
                                                'from': 'coordinator'})
        self.assertEqual(self.store.get('todos', obj['id'])['from'], 'coordinator')

    def test_an_agent_todo_with_no_commands_is_the_stores_refusal(self):
        obj = self.refused('invalid', None, 'POST', '/todos', AGENT, {'title': 't'})
        self.assertIn('handstep-commands', obj['error']['message'])

    def test_add_refuses_the_fields_the_api_sets(self):
        for role in (OWNER, AGENT):
            for name in ('status', 'done_at', 'pm_ack', 'added_at', 'id'):
                with self.subTest(role=role, field=name):
                    self.refused('invalid', name, 'POST', '/todos', role,
                                 {'title': 't', 'commands': ['dir'], name: 'done'})
        self.refused('invalid', 'title', 'POST', '/todos', OWNER, {})

    def test_done_and_undone(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'})['id']
        obj = self.ok('POST', '/todos/%s/done' % tid, OWNER)
        self.assertEqual((obj['status'], obj['done_at']), ('done', NOW))
        self.assertEqual(self.store.get('todos', tid)['status'], 'done')
        obj = self.ok('POST', '/todos/%s/done' % tid, OWNER, {'done': False})
        d = self.store.get('todos', tid)
        self.assertEqual((obj['status'], d['status'], d.get('done_at')), ('open', 'open', None))
        self.refused('invalid', 'done', 'POST', '/todos/%s/done' % tid, OWNER, {'done': 'yes'})
        self.refused('invalid', 'status', 'POST', '/todos/%s/done' % tid, OWNER,
                     {'status': 'done'})
        self.refused('conflict', 'if_version', 'POST', '/todos/%s/done' % tid, OWNER,
                     {'done': True, 'if_version': 1})

    def test_ack(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'})['id']
        self.assertEqual([p['id'] for p in self.ok('GET', '/pending', AGENT)['items']], [tid])
        self.refused('invalid', 'pm_ack', 'POST', '/todos/%s/ack' % tid, AGENT, {'pm_ack': False})
        obj = self.ok('POST', '/todos/%s/ack' % tid, AGENT)
        self.assertIs(obj['pm_ack'], True)
        self.assertIs(self.store.get('todos', tid)['pm_ack'], True)
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [])

    def test_list(self):
        a = self.ok('POST', '/todos', OWNER, {'title': 'a', 'repo': 'kit'})['id']
        b = self.ok('POST', '/todos', OWNER, {'title': 'b'})['id']
        self.ok('POST', '/todos/%s/done' % b, OWNER)
        self.assertEqual([d['id'] for d in self.ok('GET', '/todos', AGENT)['items']], [a, b])
        self.assertEqual([d['id'] for d in self.ok('GET', '/todos', AGENT, None,
                                                   {'status': 'done'})['items']], [b])
        self.assertEqual([d['id'] for d in self.ok('GET', '/todos', AGENT, None,
                                                   {'repo': 'KIT'})['items']], [a])
        self.refused('invalid', 'status', 'GET', '/todos', AGENT, None, {'status': 'answered'})


class StoryTest(ApiCase):
    def test_add(self):
        obj = self.ok('POST', '/stories', OWNER, {'repo': 'kit', 'title': 't', 'story': 's',
                                                  'urgent': True})
        self.assertEqual((obj['id'], obj['status']), ('S-001', 'submitted'))
        d = self.store.get('stories', 'S-001')
        self.assertEqual((d['status'], d['from'], d['submitted_at'], d['urgent']),
                         ('submitted', 'you', NOW, True))
        for name in ('repo', 'title', 'story'):
            body = {'repo': 'kit', 'title': 't', 'story': 's'}
            del body[name]
            self.refused('invalid', name, 'POST', '/stories', OWNER, body)
        for name in ('status', 'reviewer', 'backlog_id', 'from', 'submitted_at'):
            self.refused('invalid', name, 'POST', '/stories', OWNER,
                         {'repo': 'kit', 'title': 't', 'story': 's', name: 'added'})

    def test_an_agent_moves_a_story(self):
        sid = self.story()
        path = '/stories/%s' % sid
        obj = self.ok('POST', path, AGENT, {'status': 'review', 'reviewer': 'coordinator',
                                            'if_version': 1})
        self.assertEqual((obj['status'], obj['version']), ('review', 2))
        self.ok('POST', path, AGENT, {'status': 'questions', 'review_note': 'n',
                                      'if_version': 2})
        self.ok('POST', path, AGENT, {'status': 'added', 'backlog_id': 'B-1', 'if_version': 3})
        d = self.store.get('stories', sid)
        self.assertEqual((d['status'], d['reviewer'], d['review_note'], d['backlog_id'],
                          d['added_at']), ('added', 'coordinator', 'n', 'B-1', NOW))
        self.assertNotIn('if_version', d)
        self.assertNotIn('closed_at', d)

    def test_declined_is_closed(self):
        sid = self.story()
        self.ok('POST', '/stories/%s' % sid, AGENT, {'status': 'declined', 'closed_note': 'why',
                                                     'if_version': 1})
        d = self.store.get('stories', sid)
        self.assertEqual((d['status'], d['closed_note'], d['closed_at']),
                         ('declined', 'why', NOW))

    def test_an_agent_cannot_set_the_owners_statuses_or_fields(self):
        sid = self.story()
        path = '/stories/%s' % sid
        for status in ('submitted', 'withdrawn', 'done', '', 3):
            with self.subTest(status=status):
                self.refused('invalid', 'status', 'POST', path, AGENT,
                             {'status': status, 'if_version': 1})
        self.refused('invalid', 'status', 'POST', path, AGENT, {'if_version': 1})
        self.refused('invalid', 'if_version', 'POST', path, AGENT, {'status': 'review'})
        self.refused('conflict', 'if_version', 'POST', path, AGENT,
                     {'status': 'review', 'if_version': 4})
        for name in ('story', 'title', 'repo', 'from', 'urgent', 'submitted_at', 'closed_at'):
            with self.subTest(field=name):
                self.refused('invalid', name, 'POST', path, AGENT,
                             {'status': 'review', 'if_version': 1, name: 'x'})

    def test_withdraw_only_while_submitted(self):
        sid = self.story()
        obj = self.ok('POST', '/stories/%s/withdraw' % sid, OWNER)
        d = self.store.get('stories', sid)
        self.assertEqual((obj['status'], d['status'], d['closed_at']),
                         ('withdrawn', 'withdrawn', NOW))
        self.refused('invalid', 'status', 'POST', '/stories/%s/withdraw' % sid, OWNER)
        self.refused('invalid', 'status', 'POST', '/stories/%s' % sid, AGENT,
                     {'status': 'review', 'if_version': 2})
        other = self.story()
        self.ok('POST', '/stories/%s' % other, AGENT, {'status': 'review', 'if_version': 1})
        self.refused('invalid', 'status', 'POST', '/stories/%s/withdraw' % other, OWNER)
        self.assertEqual([d['id'] for d in self.ok('GET', '/stories', OWNER, None,
                                                   {'status': 'withdrawn'})['items']], [sid])


class ToPmTest(ApiCase):
    def test_queue_send_reply(self):
        a = self.ok('POST', '/to_pm', OWNER, {'text': 'one'})
        b = self.ok('POST', '/to_pm', OWNER, {'text': 'two', 'kind': 'fyi', 'repo': 'kit',
                                              'urgent': True})
        self.assertEqual((a['id'], a['status'], b['id']), ('P-001', 'queued', 'P-002'))
        d = self.store.get('to_pm', 'P-001')
        self.assertEqual((d['status'], d['kind'], d['created_at']), ('queued', 'question', NOW))
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [])
        self.refused('invalid', 'status', 'POST', '/to_pm/P-001/reply', AGENT, {'reply': 'r'})
        obj = self.ok('POST', '/to_pm/send', OWNER)
        self.assertEqual((obj['sent'], obj['sent_at']), (['P-001', 'P-002'], NOW))
        self.assertEqual(self.store.get('to_pm', 'P-002')['status'], 'sent')
        self.assertEqual(self.store.get('to_pm', 'P-002')['sent_at'], NOW)
        self.assertEqual([p['id'] for p in self.ok('GET', '/pending', AGENT)['items']],
                         ['P-001', 'P-002'])
        obj = self.ok('POST', '/to_pm/send', OWNER)
        self.assertEqual((obj['sent'], obj['sent_at']), ([], None))
        obj = self.ok('POST', '/to_pm/P-001/reply', AGENT, {'reply': 'r'})
        d = self.store.get('to_pm', 'P-001')
        self.assertEqual((obj['status'], d['status'], d['reply'], d['done_at']),
                         ('done', 'done', 'r', NOW))
        self.refused('invalid', 'status', 'POST', '/to_pm/P-001/reply', AGENT, {'reply': 'again'})
        self.ok('POST', '/to_pm/P-002/reply', AGENT)
        self.assertEqual([d['id'] for d in self.ok('GET', '/to_pm', OWNER, None,
                                                   {'status': 'done'})['items']],
                         ['P-001', 'P-002'])

    def test_refused_fields(self):
        self.refused('invalid', 'text', 'POST', '/to_pm', OWNER, {})
        self.refused('invalid', 'kind', 'POST', '/to_pm', OWNER, {'text': 'x', 'kind': 'order'})
        for name in ('status', 'reply', 'sent_at', 'done_at', 'ruling'):
            self.refused('invalid', name, 'POST', '/to_pm', OWNER, {'text': 'x', name: 'sent'})
        self.refused('invalid', 'ids', 'POST', '/to_pm/send', OWNER, {'ids': ['P-001']})
        self.ok('POST', '/to_pm', OWNER, {'text': 'x'})
        self.ok('POST', '/to_pm/send', OWNER)
        for name in ('status', 'text', 'kind'):
            self.refused('invalid', name, 'POST', '/to_pm/P-001/reply', AGENT,
                         {'reply': 'r', name: 'queued'})
        self.refused('invalid', 'reply', 'POST', '/to_pm/P-001/reply', AGENT, {'reply': 3})


class InboxTest(ApiCase):
    def test_add_and_read(self):
        obj = self.ok('POST', '/inbox', AGENT, {'text': 'x', 'title': 't', 'repo': 'kit',
                                                'from': 'pm', 'ref': 'Q-001', 'asked': 'a',
                                                'refs': ['Q-001']})
        self.assertEqual(obj['id'], 'M-001')
        d = self.store.get('inbox', 'M-001')
        self.assertEqual((d['at'], d['read'], d['text']), (NOW, False, 'x'))
        obj = self.ok('POST', '/inbox/M-001/read', OWNER)
        d = self.store.get('inbox', 'M-001')
        self.assertEqual((obj['read'], d['read'], d['read_at']), (True, True, NOW))
        self.ok('POST', '/inbox/M-001/read', OWNER, {'read': False})
        d = self.store.get('inbox', 'M-001')
        self.assertEqual((d['read'], d.get('read_at')), (False, None))

    def test_refused_fields(self):
        self.refused('invalid', 'text', 'POST', '/inbox', AGENT, {'title': 't'})
        for name in ('read', 'read_at', 'at', 'status'):
            self.refused('invalid', name, 'POST', '/inbox', AGENT, {'text': 'x', name: True})
        self.ok('POST', '/inbox', AGENT, {'text': 'x'})
        self.refused('invalid', 'text', 'POST', '/inbox/M-001/read', OWNER, {'text': 'changed'})
        self.refused('invalid', 'read', 'POST', '/inbox/M-001/read', OWNER, {'read': 1})


class ViewsTest(ApiCase):
    def test_no_views_is_a_clear_not_available(self):
        for path in ('/backlog', '/completed', '/notices', '/repos', '/usage', '/agents'):
            with self.subTest(path=path):
                obj = self.refused('unavailable', 'views', 'GET', path, AGENT)
                self.assertIn('not available', obj['error']['message'])
                self.assertIn(path[1:], obj['error']['message'])

    def test_a_view_the_object_lacks_is_not_available(self):
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        self.assertEqual(self.ok('GET', '/notices', OWNER)['items'], [{'text': 'n'}])
        self.refused('unavailable', 'views', 'GET', '/usage', OWNER)

    def test_backlog_and_completed_pass_their_query(self):
        views = Views()
        self.api = api.Api(self.store, views=views, clock=CLOCK)
        self.assertEqual(self.ok('GET', '/backlog', AGENT)['repos'], [{'repo': 'r', 'items': []}])
        self.ok('GET', '/backlog', AGENT, None, {'repo': 'kit', 'state': 'next', 'fresh': '1'})
        self.ok('GET', '/completed', OWNER)
        self.ok('GET', '/completed', OWNER, None, {'repo': 'kit', 'days': '7'})
        self.assertEqual(views.calls, [('backlog', '', '', False), ('backlog', 'kit', 'next', True),
                                       ('completed', '', None), ('completed', 'kit', 7)])
        self.refused('invalid', 'fresh', 'GET', '/backlog', AGENT, None, {'fresh': 'maybe'})
        for days in ('0', '-1', 'x', '1.5', '99999'):
            self.refused('invalid', 'days', 'GET', '/completed', AGENT, None, {'days': days})

    def test_a_view_that_fails_is_an_answer(self):
        class Broken(object):
            def backlog(self, **kw):
                raise HubError('store', 'the backlog store is not reachable', 'backlog')

            def notices(self):
                raise OSError('gone')
        self.api = api.Api(self.store, views=Broken(), clock=CLOCK)
        self.refused('store', 'backlog', 'GET', '/backlog', OWNER)
        self.refused('internal', None, 'GET', '/notices', OWNER)
        obj = self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(obj['views']['backlog'], None)
        self.assertEqual(obj['unavailable'], list(api.VIEWS))


class ReadTest(ApiCase):
    def test_pending(self):
        qid = self.question()
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [])
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'],
                         [{'kind': 'question', 'id': qid, 'title': 't', 'repo': 'kit',
                           'at': NOW}])

    def test_snapshot(self):
        qid = self.question()
        sid = self.story()
        obj = self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(obj['revision'], self.store.revision)
        self.assertEqual(obj['collections']['questions'][qid]['title'], 't')
        self.assertEqual([p['id'] for p in obj['pending']], [sid])
        self.assertEqual(obj['unavailable'], list(api.VIEWS))
        self.assertEqual(obj['views'], dict.fromkeys(api.VIEWS))
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        obj = self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(obj['views']['notices'], [{'text': 'n'}])
        self.assertEqual(obj['views']['backlog'], [{'repo': 'r', 'items': []}])
        self.assertEqual(obj['unavailable'], ['repos', 'usage', 'agents'])

    def test_health(self):
        obj = self.ok('GET', '/health', AGENT)
        self.assertEqual((obj['ok'], obj['version'], obj['revision'], obj['store'], obj['git'],
                          obj['backlog']),
                         (True, 'v1', self.store.revision, self.store.root, None, None))
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        obj = self.ok('GET', '/health', AGENT)
        # the view adds what it knows and cannot overrule what the store says
        self.assertEqual((obj['ok'], obj['revision'], obj['git'], obj['backlog']),
                         (True, self.store.revision, 'ok', 'ok'))

    def test_wait_times_out_with_nothing(self):
        obj = self.ok('GET', '/wait', AGENT, None, {'timeout': '0'})
        self.assertEqual(obj, {'revision': 0, 'reset': False, 'timed_out': True, 'changes': [],
                               'pending': []})

    def test_wait_gives_what_changed_since(self):
        qid = self.question()
        obj = self.ok('GET', '/wait', AGENT, None, {'since': '0', 'timeout': '0'})
        self.assertEqual((obj['revision'], obj['timed_out'], obj['reset']), (1, False, False))
        self.assertEqual(obj['changes'], [{'collection': 'questions', 'id': qid, 'op': 'add',
                                           'status': 'open'}])
        obj = self.ok('GET', '/wait', AGENT, None, {'since': '99', 'timeout': '0'})
        self.assertEqual((obj['reset'], obj['timed_out']), (True, False))

    def test_wait_with_pending_returns_at_once(self):
        qid = self.question()
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        seen = []
        self.store.wait = lambda since, timeout: seen.append(timeout)
        obj = self.ok('GET', '/wait', AGENT, None, {'pending': '1', 'timeout': '55'})
        self.assertEqual(([p['id'] for p in obj['pending']], obj['timed_out'], seen),
                         ([qid], False, []))

    def test_wait_never_blocks_longer_than_the_cap(self):
        seen = []
        real = self.store.wait

        def wait(since, timeout):
            seen.append((since, timeout))
            return real(since, 0)
        self.store.wait = wait
        self.question()
        for raw in ('3600', '1e9', '55', '', '2.5'):
            self.ok('GET', '/wait', AGENT, None, {'timeout': raw} if raw else None)
        self.assertEqual(seen, [(1, 55), (1, 55), (1, 55), (1, 55), (1, 2.5)])
        self.assertEqual(api.WAIT_MAX_S, 55)
        for raw in ('-1', 'soon', 'nan', 'inf'):
            self.refused('invalid', 'timeout', 'GET', '/wait', AGENT, None, {'timeout': raw})
        self.refused('invalid', 'since', 'GET', '/wait', AGENT, None, {'since': '-1'})
        self.refused('invalid', 'pending', 'GET', '/wait', AGENT, None, {'pending': 'all'})

    def test_wait_wakes_on_a_write(self):
        out = []
        t = threading.Thread(target=lambda: out.append(
            self.api.call('GET', BASE + '/wait', AGENT, None, {'timeout': '30', 'pending': '1'})))
        t.start()
        sid = self.story()
        t.join(30)
        self.assertFalse(t.is_alive())
        status, obj = out[0]
        self.assertEqual((status, obj['timed_out'], [p['id'] for p in obj['pending']]),
                         (200, False, [sid]))


if __name__ == '__main__':
    unittest.main()
