"""kitlib.hubsite.api: what each verb writes and answers, the fields it refuses, the error
shape, versions, the views it borrows, wait, pending, snapshot and health. Who may call a route
is tests/hubsite/test_api_roles.py."""
import datetime
import inspect
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from kitlib import hubvalidate
from kitlib.backlog import store as smod
from kitlib.hubsite import api, approvals, derived, push, recordwrite, schema, serve, todosteps
from kitlib.hubsite import backlog as hbacklog
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import HubError, Store
from kitlib.maintlint import result as lint_result
from tests.backlog.helpers import rec
from tests.gitfixture import TempRepo
from tests.home import pm_repo
from tests.hubsite import test_record_write as trw
from tests.hubsite.test_notify_creator import RouteCase

NOW = '2026-01-02T03:04:05+00:00'
CLOCK = lambda: NOW      # noqa: E731
OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]
# ledger ids, built so no file holds a ruling id (tests/scrub)
R1, R7, R8, R9 = ('R-' + n for n in ('0001', '0007', '0008', '0009'))
HK3, HK4, HK25 = ('H-kit-' + n for n in ('0003', '0004', '0025'))


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

    def refused(self, code, field, method, path, role, body=None, query=None, more=(), **kw):
        # more: the error's own keys past code, field and message (a schedule conflict names its id)
        before = self.store.snapshot()
        status, obj = self.call(method, path, role, body, query, **kw)
        self.assertEqual(obj['error']['code'], code, obj)
        self.assertEqual(status, api.STATUS[code])
        self.assertIs(obj['ok'], False)
        self.assertEqual(sorted(obj['error']), sorted(['code', 'field', 'message', *more]))
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
                ('POST', '/questions/Q-404/log', AGENT, {'ruling': R7, 'if_version': 1}),
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
        self.assertEqual((obj['id'], obj['version'], obj['ok']), ('Q-1000', 1, True))
        self.assertEqual(obj['revision'], self.store.revision)
        self.assertIsInstance(obj['warnings'], list)
        d = self.store.get('questions', 'Q-1000')
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
        self.ok('POST', '/questions/%s/log' % qid, AGENT, {'ruling': R7, 'if_version': 3})
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

    def test_a_version_0_question_whose_recommended_is_text_is_logged_at_if_version_0(self):
        # the shape of a question an earlier hub wrote: no `version` (read as 0), answered, and
        # its `recommended` the text of a number or an option's label
        for doc_id, text in (('Q-925', '1'), ('Q-926', 'second')):
            with self.subTest(recommended=text):
                root = tempfile.mkdtemp(prefix='kit-hubapi-legacy-')
                self.addCleanup(shutil.rmtree, root, True)
                legacy = {'title': 't', 'status': 'answered', 'repo': 'kit', 'options': OPTIONS,
                          'recommended': text, 'asked_at': NOW,
                          'answer': {'choice': 1, 'label': 'second', 'at': NOW}}
                os.makedirs(os.path.join(root, 'questions'))
                path = os.path.join(root, 'questions', doc_id + '.json')
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(legacy, f)
                store = Store.open(root, clock=CLOCK)
                self.addCleanup(store.close)
                hub = api.Api(store, clock=CLOCK)
                status, obj = hub.call('POST', BASE + '/questions/%s/log' % doc_id, AGENT,
                                       {'ruling': R7, 'if_version': 0}, None)
                self.assertEqual((status, obj.get('error')), (200, None), obj)
                self.assertEqual((obj['status'], obj['version']), ('logged', 1))
                with open(path, encoding='utf-8') as f:
                    self.assertEqual(json.load(f)['recommended'], 1, 'stored as the number')

    def test_log(self):
        qid = self.question()
        path = '/questions/%s/log' % qid
        self.refused('invalid', 'status', 'POST', path, AGENT,
                     {'ruling': R7, 'if_version': 1})
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'if_version': 2})
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'ruling': ' ', 'if_version': 2})
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'ruling': 7, 'if_version': 2})
        self.refused('invalid', 'if_version', 'POST', path, AGENT, {'ruling': R7})
        self.refused('conflict', 'if_version', 'POST', path, AGENT,
                     {'ruling': R7, 'if_version': 1})
        self.refused('invalid', 'answer', 'POST', path, AGENT,
                     {'ruling': R7, 'if_version': 2, 'answer': {'choice': 1}})
        obj = self.ok('POST', path, AGENT, {'ruling': ' ' + R7 + ' ', 'if_version': 2})
        self.assertEqual((obj['status'], obj['version']), ('logged', 3))
        d = self.store.get('questions', qid)
        self.assertEqual((d['status'], d['ruling'], d['answer']['choice']),
                         ('logged', R7, 0))
        self.refused('invalid', 'status', 'POST', path, AGENT,
                     {'ruling': R8, 'if_version': 3})

    def answered(self):
        qid = self.question()
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        return qid, '/questions/%s/log' % qid

    def test_log_refuses_a_ruling_that_is_not_a_ledger_id(self):
        qid, path = self.answered()
        warning = 'warning: %s Decides is 436 characters (aim for one line)' % HK25
        for text in (warning, 'decision-7', 'R-1', 'H-kit-12', R7 + ' ' + R8):
            with self.subTest(ruling=text[:20]):
                obj = self.refused('invalid', 'ruling', 'POST', path, AGENT,
                                   {'ruling': text, 'if_version': 2})
                self.assertIn('ruling must be a ledger ruling id (the id ledger.py add prints)',
                              obj['error']['message'])
        self.assertEqual(self.store.get('questions', qid)['status'], 'answered')
        self.ok('POST', path, AGENT, {'ruling': HK25, 'if_version': 2})

    def test_log_refuses_a_ledger_id_no_ledger_file_holds(self):
        class Views(object):
            def ruling_exists(self, rid):
                return rid == HK3
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        qid, path = self.answered()
        obj = self.refused('invalid', 'ruling', 'POST', path, AGENT,
                           {'ruling': HK4, 'if_version': 2})
        self.assertIn(HK4 + ' is in no ledger file', obj['error']['message'])
        self.ok('POST', path, AGENT, {'ruling': HK3, 'if_version': 2})

    def test_log_with_a_ledger_that_cannot_be_read_is_unavailable(self):
        class Views(object):
            def ruling_exists(self, rid):
                raise OSError('x')
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        qid, path = self.answered()
        self.refused('unavailable', 'views', 'POST', path, AGENT,
                     {'ruling': R7, 'if_version': 2})

    def test_log_replace_corrects_a_logged_ruling(self):
        qid, path = self.answered()
        self.ok('POST', path, AGENT, {'ruling': R7, 'if_version': 2})
        again = {'ruling': R8, 'if_version': 3}
        obj = self.refused('invalid', 'status', 'POST', path, AGENT, again)
        self.assertIn('replace: true', obj['error']['message'])
        self.refused('invalid', 'replace', 'POST', path, AGENT, dict(again, replace='yes'))
        self.refused('invalid', 'ruling', 'POST', path, AGENT,
                     dict(again, replace=True, ruling='not an id'))
        self.assertEqual(self.store.get('questions', qid)['ruling'], R7)
        obj = self.ok('POST', path, AGENT, dict(again, replace=True))
        self.assertEqual((obj['status'], obj['version']), ('logged', 4))
        d = self.store.get('questions', qid)
        self.assertEqual((d['status'], d['ruling'], d['answer']['choice']), ('logged', R8, 0))

    def test_log_replace_is_refused_for_a_question_not_logged(self):
        qid, path = self.answered()
        self.refused('invalid', 'replace', 'POST', path, AGENT,
                     {'ruling': R7, 'if_version': 2, 'replace': True})

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
        self.refused('invalid', 'status', 'GET', '/questions', OWNER, None, {'status': 'closed'})
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


class TodoStepsTest(ApiCase):
    """POST /todos takes steps and judges an agent's to-do (api.todo_steps), for every client,
    not only the MCP tool."""

    def test_steps_become_commands_after_the_given_ones_and_a_summary(self):
        steps = [{'edit': 'C:\\w\\a.txt', 'old': 'x\ny', 'new': 'z'}, {'run': 'python t.py'}]
        for role in (AGENT, OWNER):
            with self.subTest(role=role):
                obj = self.ok('POST', '/todos', role, {'title': 't', 'detail': 'Why.',
                                                       'commands': ['first'], 'steps': steps})
                d = self.store.get('todos', obj['id'])
                self.assertNotIn('steps', d)
                self.assertEqual(d['commands'], ['first'] + todosteps.render(steps))
                self.assertEqual(d['detail'],
                                 'Why.\n\nreplace 2 lines in C:\\w\\a.txt\nrun python t.py')

    def test_steps_without_a_detail_make_the_summary_the_detail(self):
        steps = [{'open': 'C:\\w\\a.txt', 'line': 3}]
        for extra in ({}, {'detail': ''}, {'detail': None}):
            with self.subTest(extra=extra):
                obj = self.ok('POST', '/todos', AGENT, dict(extra, title='t', steps=steps))
                d = self.store.get('todos', obj['id'])
                self.assertEqual(d['detail'], 'open C:\\w\\a.txt at line 3 in Notepad++')
                self.assertEqual(d['commands'], todosteps.render(steps))

    def test_bad_steps_and_refused_text_are_refused_naming_the_field(self):
        for body, field in (({'steps': [{'open': 'rel.txt'}]}, 'steps'),
                            ({'steps': []}, 'steps'),
                            ({'steps': 'run x'}, 'steps'),
                            ({'steps': [{'edit': 'C:\\w\\a.txt', 'old': 'a\ud800', 'new': 'b'}]},
                             'steps'),
                            ({'steps': [{'run': 'notepad x'}]}, 'steps'),
                            ({'commands': ['dir'], 'steps': [{'run': 'echo a\ud800b'}]}, 'steps'),
                            ({'commands': ['git worktree add x']}, 'commands'),
                            ({'commands': ['notepad C:\\a.txt']}, 'commands'),
                            ({'commands': ['dir'], 'detail': 'Open it and apply the change.'},
                             'detail')):
            with self.subTest(body=body):
                obj = self.refused('invalid', field, 'POST', '/todos', AGENT,
                                   dict(body, title='t'))
                self.assertTrue(obj['error']['message'], obj)

    def test_a_lone_surrogate_in_a_command_is_refused_for_either_role(self):
        for role in (AGENT, OWNER):
            for c in ('echo a\ud800b', '\udc00'):
                with self.subTest(role=role, command=c):
                    obj = self.refused('invalid', 'commands', 'POST', '/todos', role,
                                       {'title': 't', 'commands': ['dir', c]})
                    self.assertIn('commands[1] holds a lone surrogate', obj['error']['message'])
        self.ok('POST', '/todos', AGENT, {'title': 't', 'commands': ['echo \U0001f600']})

    def test_commands_must_be_a_list(self):
        self.refused('invalid', 'commands', 'POST', '/todos', AGENT,
                     {'title': 't', 'commands': 'dir'})

    def test_the_owners_own_text_is_not_judged_by_the_agent_rules(self):
        obj = self.ok('POST', '/todos', OWNER, {'title': 't',
                                                'commands': ['notepad C:\\a.txt'],
                                                'detail': 'Open it and apply the change.'})
        self.assertEqual(self.store.get('todos', obj['id'])['commands'], ['notepad C:\\a.txt'])

    def test_edit_text_is_never_judged(self):
        steps = [{'edit': 'C:\\w\\a.txt', 'old': 'git worktree add x', 'new': 'notepad'}]
        obj = self.ok('POST', '/todos', AGENT, {'title': 't', 'steps': steps})
        self.assertEqual(self.store.get('todos', obj['id'])['commands'],
                         todosteps.render(steps))


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
        for role in (OWNER, AGENT):
            with self.subTest(role=role):
                obj = self.refused('unavailable', 'views', 'GET', '/backlog', role)
                self.assertEqual(obj['error']['message'], 'the backlog view failed: store')
                obj = self.refused('unavailable', 'views', 'GET', '/notices', role)
                self.assertEqual(obj['error']['message'], 'the notices view failed: OSError')
        obj = self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(obj['views']['backlog'], None)
        self.assertEqual(sorted(obj['unavailable']), sorted(api.VIEWS))


class ReadTest(ApiCase):
    def test_pending(self):
        qid = self.question()
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [])
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'],
                         [{'kind': 'question', 'id': qid, 'title': 't', 'repo': 'kit',
                           'at': NOW, 'from': 'pm', 'route': 'pm'}])

    def test_snapshot(self):
        qid = self.question()
        sid = self.story()
        obj = self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(obj['revision'], self.store.revision)
        self.assertEqual(obj['collections']['questions'][qid]['title'], 't')
        self.assertEqual([p['id'] for p in obj['pending']], [sid])
        self.assertEqual(obj['unavailable'], dict.fromkeys(api.VIEWS, 'unavailable'))
        self.assertEqual(obj['views'], dict.fromkeys(api.VIEWS))
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        obj = self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(obj['views']['notices'], [{'text': 'n'}])
        self.assertEqual(obj['views']['backlog'], [{'repo': 'r', 'items': []}])
        self.assertEqual(obj['unavailable'], dict.fromkeys(('repos', 'usage', 'agents'),
                                                           'unavailable'))

    def test_snapshot_carries_the_cards_but_not_the_bodies(self):
        """The Docs tab lists the cards from the snapshot and reads a body with GET pages/{id}
        when it is opened ("Pages"): a body is up to half a MiB, and the page reloads the
        snapshot on every change."""
        body = '<h1>Plan</h1><svg viewBox="0 0 1 1"></svg>'
        self.store.put('pages', 'plan-1', {'title': 'The plan', 'repo': 'kit', 'at': '2026-10-15T00:00:00Z'})
        self.store.put('page_html', 'plan-1', {'html': body})
        for role in (OWNER, AGENT):
            cols = self.ok('GET', '/snapshot', role)['collections']
            self.assertEqual(cols['pages']['plan-1']['title'], 'The plan', role)
            self.assertNotIn('page_html', cols, role)
            self.assertEqual(self.ok('GET', '/pages/plan-1', role)['html'], body, role)
        self.assertEqual(self.store.get('page_html', 'plan-1')['html'], body, 'the store keeps it')


PAGE = '<h1>Plan</h1><svg viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'


class PagesTest(ApiCase):
    def publish(self, doc_id='plan-1', html=PAGE, **kw):
        body = {'id': doc_id, 'title': 'The plan', 'html': html}
        body.update(kw)
        return self.call('POST', '/pages', AGENT, body)

    def test_publish_writes_the_card_and_the_body(self):
        status, obj = self.publish(repo='kit', summary='s', refs=['Q-001'], **{'from': 'planner'})
        self.assertEqual((status, obj['id'], obj['version'], obj['bytes']), (200, 'plan-1', 1,
                                                                             len(PAGE)))
        card = self.store.get('pages', 'plan-1')
        self.assertEqual({k: card[k] for k in ('title', 'repo', 'summary', 'refs', 'from', 'at',
                                               'bytes', 'via')},
                         {'title': 'The plan', 'repo': 'kit', 'summary': 's', 'refs': ['Q-001'],
                          'from': 'planner', 'at': NOW, 'bytes': len(PAGE), 'via': 'api'})
        self.assertEqual(self.store.get('page_html', 'plan-1')['html'], PAGE)
        self.assertNotIn('html', card)
        got = self.ok('GET', '/pages/plan-1', OWNER)
        self.assertEqual((got['id'], got['html'], got['version']), ('plan-1', PAGE, 1))
        self.assertEqual([d['id'] for d in self.ok('GET', '/pages', AGENT)['items']], ['plan-1'])

    def test_the_bytes_are_utf8(self):
        html = '<p>é\U0001f600</p>'
        self.assertEqual(self.publish(html=html)[1]['bytes'], len(html.encode('utf-8')))

    def test_the_size_cap_at_its_edge(self):
        pad = '<p>' + 'x' * (api.PAGE_HTML_MAX - 7) + '</p>'
        self.assertEqual(len(pad.encode('utf-8')), api.PAGE_HTML_MAX)
        self.assertEqual(self.publish('edge', pad)[0], 200)
        over = '<p>' + 'x' * (api.PAGE_HTML_MAX - 6) + '</p>'
        obj = self.refused('invalid', 'html', 'POST', '/pages', AGENT,
                           {'id': 'over', 'title': 't', 'html': over})
        self.assertIn(str(api.PAGE_HTML_MAX), obj['error']['message'])
        # the cap counts bytes, not characters: a two-byte character is two
        wide = '<p>' + 'é' * ((api.PAGE_HTML_MAX - 6) // 2) + 'xx</p>'
        self.assertLess(len(wide), api.PAGE_HTML_MAX)
        self.assertGreater(len(wide.encode('utf-8')), api.PAGE_HTML_MAX)
        self.refused('invalid', 'html', 'POST', '/pages', AGENT,
                     {'id': 'wide', 'title': 't', 'html': wide})
        self.assertIsNone(self.store.get('pages', 'over'))

    def test_a_page_the_check_refuses_writes_nothing(self):
        for html in ('<script>x</script>', '<a href="https://example.com">x</a>',
                     '<div class="mermaid">graph TD; A-->B</div>', '<img src="http://x/y.png">'):
            with self.subTest(html=html):
                self.refused('invalid', 'html', 'POST', '/pages', AGENT,
                             {'id': 'bad', 'title': 't', 'html': html})

    def test_a_per_repo_ledger_id_in_a_page_id_is_refused(self):
        h = 'H' + '-kit-' + '0007'
        for doc_id in (h, 'plan-' + h, h + '-plan', 'x_' + h):
            with self.subTest(id=doc_id):
                obj = self.refused('invalid', 'id', 'POST', '/pages', AGENT,
                                   {'id': doc_id, 'title': 't', 'html': PAGE})
                self.assertIn('holds', obj['error']['message'])
        for doc_id in ('H-kit', 'Hx-kit-0007', 'planH-kit-0007'):
            with self.subTest(id=doc_id):
                self.ok('POST', '/pages', AGENT, {'id': doc_id, 'title': 't', 'html': PAGE})

    def test_ids_shaped_like_items_or_rulings_are_refused(self):
        for doc_id in ('Q-1', 'q-001', 'M-2', 'T-3', 'P-4', 'S-5', 'A-6', 'R-21',
                       'plan-Q-1', 'Q-1.v2', 'R-21-plan', 'x_T-3', 'notes.M-12.draft'):
            with self.subTest(id=doc_id):
                obj = self.refused('invalid', 'id', 'POST', '/pages', AGENT,
                                   {'id': doc_id, 'title': 't', 'html': PAGE})
                self.assertIn('holds', obj['error']['message'])
        for doc_id in ('../x', 'con', 'a b', ''):
            with self.subTest(id=doc_id):
                self.refused('invalid', 'id', 'POST', '/pages', AGENT,
                             {'id': doc_id, 'title': 't', 'html': PAGE})
        # a letter and digits glued to more letters or digits is no ref (hubvalidate._REF)
        for doc_id in ('plan-1', 'xq-1', 'Q-1x', 'v2-plan', 'q1-notes'):
            with self.subTest(id=doc_id):
                self.assertEqual(self.publish(doc_id)[0], 200)

    def test_every_ref_the_validator_finds_in_an_id_is_refused(self):
        for doc_id in ('plan-Q-1', 'Q-1.v2', 'a.P-7', 'S-9_b'):
            with self.subTest(id=doc_id):
                self.assertTrue(hubvalidate._REF.search(doc_id))
                self.refused('invalid', 'id', 'POST', '/pages', AGENT,
                             {'id': doc_id, 'title': 't', 'html': PAGE})

    def test_refused_fields(self):
        for name in ('at', 'bytes', 'via', 'status', 'version'):
            with self.subTest(name=name):
                self.refused('invalid', name, 'POST', '/pages', AGENT,
                             {'id': 'p', 'title': 't', 'html': PAGE, name: 'x'})
        self.refused('invalid', 'title', 'POST', '/pages', AGENT, {'id': 'p', 'html': PAGE})
        self.refused('invalid', 'html', 'POST', '/pages', AGENT, {'id': 'p', 'title': 't'})
        self.refused('invalid', 'refs', 'POST', '/pages', AGENT,
                     {'id': 'p', 'title': 't', 'html': PAGE, 'refs': [1]})
        self.refused('invalid', 'from', 'POST', '/pages', AGENT,
                     {'id': 'p', 'title': 't', 'html': PAGE, 'from': 'you'})

    def test_publishing_over_a_page_takes_its_version(self):
        self.publish()
        self.refused('conflict', 'if_version', 'POST', '/pages', AGENT,
                     {'id': 'plan-1', 'title': 't', 'html': '<p>2</p>'})
        self.refused('conflict', 'if_version', 'POST', '/pages', AGENT,
                     {'id': 'plan-1', 'title': 't', 'html': '<p>2</p>', 'if_version': 2})
        status, obj = self.publish(html='<p>2</p>', if_version=1)
        self.assertEqual((status, obj['version'], obj['html_version']), (200, 2, 2))
        self.assertEqual(self.store.get('page_html', 'plan-1')['html'], '<p>2</p>')
        self.assertNotIn('summary', self.store.get('pages', 'plan-1'))
        self.refused('conflict', 'if_version', 'POST', '/pages', AGENT,
                     {'id': 'new', 'title': 't', 'html': PAGE, 'if_version': 1})
        self.assertEqual(self.publish('new', if_version=0)[0], 200)

    def test_two_publishers_with_one_pin_the_second_writes_nothing(self):
        """B arrives after A has written its body and before A writes its card. Without
        store.PAGES_LOCK, B reads card 1 and body 2, writes its body over A's, and A's card then
        names B's body. With it, B waits for A's two writes, reads card 2 and is refused."""
        self.publish()
        real_put, real_get, lock = self.store.put, self.store.get, threading.Lock()
        b_there, b_thread = threading.Event(), []     # B has read, or waits on the lock

        class Watched(object):
            def __enter__(self):
                if threading.current_thread() in b_thread:
                    b_there.set()
                return lock.__enter__()

            def __exit__(self, *exc):
                return lock.__exit__(*exc)

        def get(coll, doc_id):
            got = real_get(coll, doc_id)
            if coll == 'page_html' and threading.current_thread() in b_thread:
                b_there.set()
            return got

        def put(coll, doc_id, fields, if_version=None):
            r = real_put(coll, doc_id, fields, if_version)
            if coll == 'page_html' and fields['html'] == '<p>A</p>':
                b = threading.Thread(target=lambda: answers.append(
                    self.publish(html='<p>B</p>', title='B', if_version=1)))
                b_thread.append(b)
                b.start()
                self.assertTrue(b_there.wait(10), 'B neither read nor waited')
            return r
        answers = []
        self.store.get, self.store.put = get, put
        with mock.patch.object(api, 'PAGES_LOCK', Watched()):
            a = self.publish(html='<p>A</p>', title='A', if_version=1)
            b_thread[0].join(10)
        self.store.get, self.store.put = real_get, real_put
        self.assertEqual(a[0], 200, a)
        self.assertEqual((answers[0][0], answers[0][1]['error']['code'],
                          answers[0][1]['error']['field']), (409, 'conflict', 'if_version'))
        card, body = self.store.get('pages', 'plan-1'), self.store.get('page_html', 'plan-1')
        self.assertEqual((card['title'], card['version'], body['html'], body['version']),
                         ('A', 2, '<p>A</p>', 2))

    def test_a_body_delete_that_fails_leaves_the_card_deleted_and_an_orphan_body(self):
        self.publish()
        real = self.store.delete

        def delete(coll, doc_id, if_version=None):
            if coll == 'page_html':
                raise HubError('store', 'the disk is full', 'page_html')
            return real(coll, doc_id, if_version)
        self.store.delete = delete
        status, obj = self.call('POST', '/pages/plan-1/delete', OWNER, {'if_version': 1})
        self.store.delete = real
        self.assertEqual((status, obj['error']['code']), (api.STATUS['store'], 'store'))
        self.assertIsNone(self.store.get('pages', 'plan-1'))
        self.assertEqual(self.store.get('page_html', 'plan-1')['html'], PAGE)
        self.assertEqual(self.publish()[0], 200, 'the next publish writes over the orphan')
        self.assertEqual(self.store.get('page_html', 'plan-1')['version'], 2)

    def test_the_body_is_written_before_the_card(self):
        order = []
        real = self.store.put

        def put(coll, doc_id, fields, if_version=None):
            order.append(coll)
            if coll == 'pages':
                raise HubError('store', 'the disk is full', 'pages')
            return real(coll, doc_id, fields, if_version)
        self.store.put = put
        status, obj = self.publish()
        self.assertEqual(order, ['page_html', 'pages'])
        self.assertEqual(obj['error']['code'], 'store')
        # the card write failed: an orphan body (hubvalidate warns of it), never a card with none
        self.assertIsNone(self.store.get('pages', 'plan-1'))
        self.assertEqual(self.store.get('page_html', 'plan-1')['html'], PAGE)
        self.store.put = real
        self.assertEqual(self.publish()[0], 200, 'the next publish writes over the orphan')

    def test_get_of_a_page_with_no_body_is_not_found(self):
        self.store.put('pages', 'card-only', {'title': 't'})
        self.refused('not_found', 'id', 'GET', '/pages/card-only', AGENT)
        self.refused('not_found', 'id', 'GET', '/pages/none', OWNER)

    def test_the_owner_deletes_the_card_then_the_body(self):
        self.publish()
        order = []
        real = self.store.delete

        def delete(coll, doc_id, if_version=None):
            order.append(coll)
            return real(coll, doc_id, if_version)
        self.store.delete = delete
        self.refused('conflict', 'if_version', 'POST', '/pages/plan-1/delete', OWNER,
                     {'if_version': 4})
        self.refused('forbidden', 'role', 'POST', '/pages/plan-1/delete', AGENT)
        obj = self.ok('POST', '/pages/plan-1/delete', OWNER, {'if_version': 1})
        self.assertEqual((obj['deleted'], order), (True, ['pages', 'page_html']))
        self.assertIsNone(self.store.get('pages', 'plan-1'))
        self.assertIsNone(self.store.get('page_html', 'plan-1'))
        self.refused('not_found', 'id', 'POST', '/pages/plan-1/delete', OWNER)

    def test_health(self):
        obj = self.ok('GET', '/health', OWNER)
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


def seeded(case, files):
    """A Store opened on a folder that already holds files ({'coll/name.json': document}): the
    way to have a document the store itself would refuse to write."""
    root = tempfile.mkdtemp(prefix='kit-hubapi-')
    case.addCleanup(shutil.rmtree, root, True)
    for rel, doc in files.items():
        path = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
    store = Store.open(root, clock=CLOCK)
    case.addCleanup(store.close)
    return store


class FixTest(ApiCase):
    """The behaviours the first review asked for."""

    def note(self, **kw):
        body = {'text': 'one'}
        body.update(kw)
        return self.ok('POST', '/to_pm', OWNER, body)['id']

    # ---- the revision of a read
    def test_a_read_revision_is_never_later_than_its_data(self):
        self.question()
        for path, name in (('/questions', 'docs'), ('/pending', 'pending')):
            with self.subTest(path=path):
                real = getattr(self.store, name)

                def late(*a, _real=real):
                    out = _real(*a)             # the data is copied ...
                    self.store.add('inbox', {'text': 'late'})   # ... and another writer gets in
                    return out
                setattr(self.store, name, late)
                obj = self.ok('GET', path, AGENT)
                setattr(self.store, name, real)
                self.assertLess(obj['revision'], self.store.revision)
                # so a client waiting since that revision is told of the write it did not see
                got = self.ok('GET', '/wait', AGENT, None, {'since': str(obj['revision']),
                                                            'timeout': '0'})
                self.assertEqual([c['collection'] for c in got['changes']], ['inbox'])
                self.assertIs(got['timed_out'], False)

    # ---- wait on a closed store
    def test_wait_on_a_closed_store_is_the_stores_refusal(self):
        self.question()
        self.store.close()
        for query in ({'timeout': '0'}, {'timeout': '30'}, {'timeout': '0', 'pending': '1'},
                      {'since': '0', 'timeout': '0'}):
            with self.subTest(query=query):
                status, obj = self.call('GET', '/wait', AGENT, None, query)
                self.assertEqual((status, obj['error']['code']), (503, 'store'))
                self.assertNotIn('timed_out', obj)

    # ---- health
    def test_health_gives_the_store_folder_to_the_owner_only(self):
        class Leaky(object):
            def health(self):
                return {'git': 'ok', 'store': 'the folder, from a view'}
        for views in (None, Views(), Leaky()):
            self.api = api.Api(self.store, views=views, clock=CLOCK)
            with self.subTest(views=views):
                self.assertEqual(self.ok('GET', '/health', OWNER)['store'], self.store.root)
                obj = self.ok('GET', '/health', AGENT)
                self.assertNotIn('store', obj)
                self.assertNotIn(json.dumps(self.store.root), json.dumps(obj))
                self.assertEqual((obj['ok'], obj['version'], obj['revision']),
                                 (True, 'v1', self.store.revision))

    def test_health_with_a_view_that_raises_says_so_briefly(self):
        class Gone(object):
            def health(self):
                raise OSError('cannot read a-folder-on-this-machine')

        class Refused(object):
            def health(self):
                raise HubError('store', 'the backlog store in a-folder-on-this-machine is gone')
        for views, why in ((Gone(), 'OSError'), (Refused(), 'store'), (None, 'unavailable')):
            self.api = api.Api(self.store, views=views, clock=CLOCK)
            for role in (OWNER, AGENT):
                with self.subTest(why=why, role=role):
                    obj = self.ok('GET', '/health', role)
                    self.assertEqual(obj['unavailable'], {'health': why})
                    self.assertEqual((obj['ok'], obj['git'], obj['backlog']), (True, None, None))
                    self.assertNotIn('a-folder-on-this-machine', json.dumps(obj))
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        self.assertEqual(self.ok('GET', '/health', AGENT)['unavailable'], {})

    # ---- snapshot
    def test_snapshot_with_views_that_raise_names_each_with_a_short_reason(self):
        class Broken(Views):
            def backlog(self, **kw):
                raise HubError('store', 'the backlog store in a-folder-on-this-machine is gone')

            def notices(self):
                raise OSError('cannot read a-folder-on-this-machine')

            def repos(self):
                return 1 / 0

            def usage(self):
                return [{'n': 1}]
        self.api = api.Api(self.store, views=Broken(), clock=CLOCK)
        qid = self.question()
        obj = self.ok('GET', '/snapshot', AGENT)
        self.assertEqual(obj['unavailable'], {'backlog': 'store', 'notices': 'OSError',
                                              'repos': 'ZeroDivisionError',
                                              'agents': 'unavailable'})
        self.assertEqual((obj['views']['backlog'], obj['views']['notices'], obj['views']['repos'],
                          obj['views']['usage']), (None, None, None, [{'n': 1}]))
        self.assertEqual(obj['collections']['questions'][qid]['title'], 't')
        self.assertNotIn('a-folder-on-this-machine', json.dumps(obj))

    # ---- to_pm: the reply's ruling
    def test_a_reply_carries_its_ruling(self):
        pid = self.note()
        self.ok('POST', '/to_pm/send', OWNER)
        path = '/to_pm/%s/reply' % pid
        self.refused('invalid', 'ruling', 'POST', path, AGENT, {'reply': 'r', 'ruling': 7})
        for text in ('decision-7', 'D-7', 'R-1', R7 + ' ' + R8):
            with self.subTest(ruling=text):
                obj = self.refused('invalid', 'ruling', 'POST', path, AGENT,
                                   {'reply': 'r', 'ruling': text})
                self.assertIn('ruling must be a ledger ruling id', obj['error']['message'])
        self.assertEqual(self.store.get('to_pm', pid)['status'], 'sent')
        obj = self.ok('POST', path, AGENT, {'reply': 'r', 'ruling': ' ' + HK25 + ' '})
        d = self.store.get('to_pm', pid)
        self.assertEqual((obj['status'], d['status'], d['reply'], d['ruling']),
                         ('done', 'done', 'r', HK25))
        other = self.note()
        self.ok('POST', '/to_pm/send', OWNER)
        self.ok('POST', '/to_pm/%s/reply' % other, AGENT, {'reply': 'r'})
        self.assertNotIn('ruling', self.store.get('to_pm', other))
        # the owner's routes still take no ruling
        self.refused('invalid', 'ruling', 'POST', '/to_pm', OWNER,
                     {'text': 'x', 'ruling': 'decision-7'})

    # ---- via
    def test_every_write_records_the_path_it_came_by(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'}, via='web')['id']
        self.assertEqual(self.store.get('todos', tid)['via'], 'web')
        self.ok('POST', '/todos/%s/done' % tid, OWNER, via='desktop-mcp')
        self.assertEqual(self.store.get('todos', tid)['via'], 'desktop-mcp')
        self.ok('POST', '/todos/%s/ack' % tid, AGENT, via='cli')
        self.assertEqual(self.store.get('todos', tid)['via'], 'cli')
        sid = self.ok('POST', '/stories', OWNER, {'repo': 'kit', 'title': 't', 'story': 's'},
                      via='web')['id']
        self.assertEqual(self.store.get('stories', sid)['via'], 'web')
        self.ok('POST', '/stories/%s' % sid, AGENT, {'status': 'review', 'if_version': 1})
        self.assertEqual(self.store.get('stories', sid)['via'], 'api')     # none given
        qid = self.ok('POST', '/questions', AGENT, {'title': 't', 'options': OPTIONS},
                      via='cli')['id']
        self.assertEqual(self.store.get('questions', qid)['via'], 'cli')
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0}, via='web')
        d = self.store.get('questions', qid)
        self.assertEqual((d['via'], d['answer']['via']), ('web', 'web'))
        self.ok('POST', '/questions/%s/log' % qid, AGENT, {'ruling': R7,
                                                           'if_version': 2}, via='cli')
        d = self.store.get('questions', qid)
        self.assertEqual((d['via'], d['answer']['via']), ('cli', 'web'))
        mid = self.ok('POST', '/inbox', AGENT, {'text': 'x'}, via='cli')['id']
        self.assertEqual(self.store.get('inbox', mid)['via'], 'cli')
        self.ok('POST', '/inbox/%s/read' % mid, OWNER, via='web')
        self.assertEqual(self.store.get('inbox', mid)['via'], 'web')
        pid = self.note()
        self.assertEqual(self.store.get('to_pm', pid)['via'], 'api')
        self.ok('POST', '/to_pm/send', OWNER, via='web')
        self.assertEqual(self.store.get('to_pm', pid)['via'], 'web')
        self.ok('POST', '/to_pm/%s/reply' % pid, AGENT, {'reply': 'r'}, via='cli')
        self.assertEqual(self.store.get('to_pm', pid)['via'], 'cli')

    def test_via_is_the_callers_never_the_bodys(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'})['id']
        for method, path, role, body in (
                ('POST', '/todos', OWNER, {'title': 't', 'via': 'web'}),
                ('POST', '/todos/%s/done' % tid, OWNER, {'via': 'web'}),
                ('POST', '/inbox', AGENT, {'text': 'x', 'via': 'web'}),
                ('POST', '/to_pm', OWNER, {'text': 'x', 'via': 'web'})):
            with self.subTest(path=path):
                self.refused('invalid', 'via', method, path, role, body)

    # ---- to_pm: send all or none
    def test_send_with_one_bad_note_sends_none(self):
        self.store.close()
        self.store = seeded(self, {
            'to_pm/P-001.json': {'status': 'queued', 'kind': 'fyi', 'text': 'good'},
            'to_pm/P-002.json': {'status': 'queued', 'kind': 'fyi'},        # no text
            'to_pm/P-003.json': {'status': 'queued', 'kind': 'fyi', 'text': 'good too'}})
        self.api = api.Api(self.store, clock=CLOCK)
        obj = self.refused('invalid', 'text', 'POST', '/to_pm/send', OWNER)
        self.assertIn('to_pm/P-002', obj['error']['message'])
        self.assertEqual([d['status'] for d in self.store.docs('to_pm').values()], ['queued'] * 3)
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [])
        # mended, they all go
        self.store.update('to_pm', 'P-002', {'text': 'now it has one'})
        self.assertEqual(self.ok('POST', '/to_pm/send', OWNER)['sent'],
                         ['P-001', 'P-002', 'P-003'])

    # ---- from: you
    def test_an_agent_cannot_spell_you_with_characters_nobody_sees(self):
        for name in ('you​', ' YOU ', 'y‍ou', '﻿you', 'Y o u', '‎you‏',
                     'yo­u', '\tyou\n', 'YOU⁠'):
            with self.subTest(name=ascii(name)):
                self.refused('invalid', 'from', 'POST', '/todos', AGENT,
                             {'title': 't', 'commands': ['dir'], 'from': name})
        self.assertEqual(self.store.docs('todos'), {})
        # a name that only holds the word is somebody else
        obj = self.ok('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir'],
                                                'from': 'younger-agent'})
        self.assertEqual(self.store.get('todos', obj['id'])['from'], 'younger-agent')

    # ---- ids with a digit that is not 0-9
    def test_a_list_orders_an_id_with_a_digit_that_is_not_ascii(self):
        self.assertEqual(api._natural('Q-1²'), ['q-', 1, '²'])
        self.assertEqual(api._natural('Q-٣'), ['q-٣'])
        self.assertEqual(api._natural('Q-010'), ['q-', 10, ''])
        self.store.close()
        doc = {'status': 'open', 'title': 't'}
        self.store = seeded(self, {'questions/Q-1².json': doc, 'questions/Q-002.json': doc,
                                   'questions/Q-٣.json': doc, 'questions/Q-001.json': doc})
        self.api = api.Api(self.store, clock=CLOCK)
        self.assertEqual([d['id'] for d in self.ok('GET', '/questions', OWNER)['items']],
                         ['Q-001', 'Q-1²', 'Q-002', 'Q-٣'])

    # ---- a flag that is null
    def test_a_null_flag_is_refused_not_read_as_true(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'})['id']
        self.refused('invalid', 'done', 'POST', '/todos/%s/done' % tid, OWNER, {'done': None})
        self.assertEqual(self.store.get('todos', tid)['status'], 'open')
        mid = self.ok('POST', '/inbox', AGENT, {'text': 'x'})['id']
        self.refused('invalid', 'read', 'POST', '/inbox/%s/read' % mid, OWNER, {'read': None})
        self.assertIs(self.store.get('inbox', mid)['read'], False)
        # no field at all still means yes
        self.assertEqual(self.ok('POST', '/todos/%s/done' % tid, OWNER, {})['status'], 'done')
        self.assertEqual(self.ok('POST', '/todos/%s/done' % tid, OWNER,
                                 {'if_version': None})['status'], 'done')

    # ---- to_pm: the owner edits or deletes a queued note
    def test_the_owner_edits_a_queued_note(self):
        pid = self.note()
        path = '/to_pm/%s/edit' % pid
        obj = self.ok('POST', path, OWNER, {'text': 'new', 'urgent': True, 'kind': 'fyi',
                                            'repo': 'kit'}, via='web')
        self.assertEqual((obj['id'], obj['status'], obj['version']), (pid, 'queued', 2))
        d = self.store.get('to_pm', pid)
        self.assertEqual((d['text'], d['urgent'], d['kind'], d['repo'], d['status'], d['via'],
                          d['created_at']), ('new', True, 'fyi', 'kit', 'queued', 'web', NOW))
        self.ok('POST', path, OWNER, {'urgent': False, 'if_version': 2})
        d = self.store.get('to_pm', pid)
        self.assertEqual((d['text'], d['urgent']), ('new', False))
        self.refused('invalid', 'body', 'POST', path, OWNER, {})
        self.refused('invalid', 'body', 'POST', path, OWNER, {'if_version': 3})
        self.refused('invalid', 'text', 'POST', path, OWNER, {'text': '  '})
        self.refused('invalid', 'text', 'POST', path, OWNER, {'text': 3})
        self.refused('invalid', 'urgent', 'POST', path, OWNER, {'urgent': None})
        self.refused('invalid', 'kind', 'POST', path, OWNER, {'kind': 'order'})
        self.refused('conflict', 'if_version', 'POST', path, OWNER, {'text': 'x', 'if_version': 1})
        for name in ('status', 'reply', 'ruling', 'sent_at', 'done_at', 'created_at', 'via'):
            with self.subTest(field=name):
                self.refused('invalid', name, 'POST', path, OWNER, {'text': 'x', name: 'sent'})

    def test_the_owner_deletes_a_queued_note(self):
        pid, keep = self.note(), self.note(text='two')
        obj = self.ok('POST', '/to_pm/%s/delete' % pid, OWNER, via='web')
        self.assertEqual((obj['id'], obj['deleted'], obj['revision']),
                         (pid, True, self.store.revision))
        self.assertIsNone(self.store.get('to_pm', pid))
        self.assertEqual(list(self.store.docs('to_pm')), [keep])
        self.refused('not_found', 'id', 'POST', '/to_pm/%s/delete' % pid, OWNER)
        self.refused('invalid', 'text', 'POST', '/to_pm/%s/delete' % keep, OWNER, {'text': 'x'})
        self.refused('conflict', 'if_version', 'POST', '/to_pm/%s/delete' % keep, OWNER,
                     {'if_version': 5})
        self.ok('POST', '/to_pm/%s/delete' % keep, OWNER, {'if_version': 1})
        self.assertEqual(self.ok('POST', '/to_pm/send', OWNER)['sent'], [])

    def test_an_agent_cannot_edit_or_delete_a_note(self):
        pid = self.note()
        self.refused('forbidden', 'role', 'POST', '/to_pm/%s/edit' % pid, AGENT, {'text': 'x'})
        self.refused('forbidden', 'role', 'POST', '/to_pm/%s/delete' % pid, AGENT)
        self.assertEqual(self.store.get('to_pm', pid)['text'], 'one')

    def test_a_note_the_pm_has_is_not_edited_or_deleted(self):
        pid = self.note()
        self.ok('POST', '/to_pm/send', OWNER)
        for status in ('sent', 'done'):
            with self.subTest(status=status):
                obj = self.refused('conflict', 'status', 'POST', '/to_pm/%s/edit' % pid, OWNER,
                                   {'text': 'changed'})
                self.assertIn(status, obj['error']['message'])
                self.refused('conflict', 'status', 'POST', '/to_pm/%s/delete' % pid, OWNER)
                self.assertEqual(self.store.get('to_pm', pid)['text'], 'one')
            self.store.update('to_pm', pid, {'status': 'done', 'reply': 'r'})

    def test_edit_or_delete_of_a_note_that_is_not_there(self):
        self.refused('not_found', 'id', 'POST', '/to_pm/P-404/edit', OWNER, {'text': 'x'})
        self.refused('not_found', 'id', 'POST', '/to_pm/P-404/delete', OWNER)

    def test_a_delete_is_pinned_to_the_version_the_check_read(self):
        # the note is sent between this module's read and its delete: it is not deleted
        pid = self.note()
        real = self.store.get

        def get(coll, doc_id):
            d = real(coll, doc_id)
            self.store.get = real
            self.store.update(coll, doc_id, {'status': 'sent'})
            return d
        self.store.get = get
        status, obj = self.call('POST', '/to_pm/%s/delete' % pid, OWNER)
        self.assertEqual((status, obj['error']['code']), (409, 'conflict'))
        self.assertEqual(self.store.get('to_pm', pid)['status'], 'sent')


class SecondFixTest(ApiCase):
    """The behaviours the second review asked for."""

    def note(self, **kw):
        body = {'text': 'one'}
        body.update(kw)
        return self.ok('POST', '/to_pm', OWNER, body)['id']

    def agent_todo(self, name):
        return {'title': 't', 'commands': ['dir'], 'from': name}

    # ---- from: an agent's name is printable ASCII
    def test_an_agents_from_is_printable_ascii(self):
        for name in ('pm\x00', 'you\x00', 'y\x00ou', 'you\x7f', 'pm\x7f', 'yo\u034fu',
                     'you\u3164', 'y\u3164ou', '\u3164', '\uff59\uff4f\uff55',
                     '\uff39\uff2f\uff35', '\u0443\u043eu', 'y\u043eu', 'yo\u03c5',
                     'caf\u00e9', 'p\tm', 'p\nm', 'p  m', 'pm\u200b', 'p\u00a0m'):
            with self.subTest(name=ascii(name)):
                self.refused('invalid', 'from', 'POST', '/todos', AGENT, self.agent_todo(name))
        for name in ('you', 'YOU', 'Y o u', ' yOu ', 'y o U'):
            with self.subTest(name=name):
                self.refused('invalid', 'from', 'POST', '/todos', AGENT, self.agent_todo(name))
        self.assertEqual(self.store.docs('todos'), {})
        for name, stored in (('pm', 'pm'), (' worker 3 ', 'worker 3'), ('PM kit-2 ~!', 'PM kit-2 ~!'),
                             ('your-agent', 'your-agent'), ('  ', 'pm'), ('', 'pm')):
            with self.subTest(name=name):
                tid = self.ok('POST', '/todos', AGENT, self.agent_todo(name))['id']
                self.assertEqual(self.store.get('todos', tid)['from'], stored)

    def test_a_stored_from_that_only_looks_like_you_is_not_the_owners(self):
        self.store.close()
        # done: the owner deletes an agent's to-do only while it is open, their own at any status
        doc = {'status': 'done', 'title': 't', 'commands': ['dir']}
        self.store = seeded(self, {'todos/T-1.json': dict(doc, **{'from': 'Y o u'}),
                                   'todos/T-2.json': dict(doc, **{'from': 'you\u200b'}),
                                   'todos/T-3.json': dict(doc, **{'from': 'you'})})
        self.api = api.Api(self.store, clock=CLOCK)
        for tid in ('T-1', 'T-2'):
            with self.subTest(id=tid):
                self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, OWNER,
                             {'title': 'x'})
                self.refused('conflict', 'status', 'POST', '/todos/%s/delete' % tid, OWNER)
        self.ok('POST', '/todos/T-3/edit', OWNER, {'title': 'x'})
        self.ok('POST', '/todos/T-3/delete', OWNER)

    # ---- the revision a write answers
    def test_send_answers_the_revision_after_its_last_note(self):
        self.note()
        self.note(text='two')
        obj = self.ok('POST', '/to_pm/send', OWNER)
        self.assertEqual(obj['revision'], self.store.revision)
        got = self.ok('GET', '/wait', OWNER, None, {'since': str(obj['revision']),
                                                    'timeout': '0'})
        self.assertEqual((got['timed_out'], got['changes']), (True, []))
        # nothing queued: nothing written, and the revision is the store's still
        self.assertEqual(self.ok('POST', '/to_pm/send', OWNER)['revision'], self.store.revision)

    def test_every_write_answers_the_revision_after_it(self):
        qid = self.question()
        sid = self.story()
        gone = self.story()
        pid, edited, dropped = self.note(), self.note(), self.note()
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'})['id']
        mid = self.ok('POST', '/inbox', AGENT, {'text': 'x'})['id']
        asked, theirs = self.question(), self.question()
        made = self.ok('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir']})['id']
        for method, path, role, body in (
                ('POST', '/questions/%s/edit' % asked, AGENT,
                 {'from': 'pm', 'if_version': 1, 'title': 'u'}),
                ('POST', '/questions/%s/delete' % asked, AGENT, {'from': 'pm', 'if_version': 2}),
                ('POST', '/questions/%s/delete' % theirs, OWNER, None),
                ('POST', '/todos/%s/edit' % made, AGENT,
                 {'from': 'pm', 'if_version': 1, 'title': 'u'}),
                ('POST', '/todos/%s/delete' % made, AGENT, {'from': 'pm', 'if_version': 2}),
                ('POST', '/questions', AGENT, {'title': 't', 'options': OPTIONS}),
                ('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0}),
                ('POST', '/questions/%s/log' % qid, AGENT, {'ruling': R7,
                                                            'if_version': 2}),
                ('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir']}),
                ('POST', '/todos/%s/edit' % tid, OWNER, {'title': 'u'}),
                ('POST', '/todos/%s/done' % tid, OWNER, None),
                ('POST', '/todos/%s/ack' % tid, AGENT, None),
                ('POST', '/todos/%s/delete' % tid, OWNER, None),
                ('POST', '/stories', OWNER, {'repo': 'kit', 'title': 't', 'story': 's'}),
                ('POST', '/stories/%s' % sid, AGENT, {'status': 'review', 'if_version': 1}),
                ('POST', '/stories/%s/withdraw' % gone, OWNER, None),
                ('POST', '/to_pm', OWNER, {'text': 'x'}),
                ('POST', '/to_pm/%s/edit' % edited, OWNER, {'text': 'newer'}),
                ('POST', '/to_pm/%s/delete' % dropped, OWNER, None),
                ('POST', '/to_pm/send', OWNER, None),
                ('POST', '/to_pm/%s/reply' % pid, AGENT, {'reply': 'r'}),
                ('POST', '/inbox', AGENT, {'text': 'x'}),
                ('POST', '/inbox/%s/read' % mid, OWNER, None)):
            with self.subTest(path=path):
                before = self.store.revision
                obj = self.ok(method, path, role, body)
                self.assertGreater(self.store.revision, before, 'the call wrote nothing')
                self.assertEqual(obj['revision'], self.store.revision)

    # ---- a view that fails, on its own route
    def test_a_view_that_raises_is_unavailable_with_a_short_reason(self):
        class Broken(object):
            def backlog(self, **kw):
                raise HubError('store', 'the backlog store in a-folder-on-this-machine is gone',
                               'backlog')

            def completed(self, **kw):
                raise HubError('unavailable', 'nothing in a-folder-on-this-machine', 'views')

            def notices(self):
                raise OSError('cannot read a-folder-on-this-machine')

            def repos(self):
                return 1 / 0

            def usage(self):
                raise KeyError('a-folder-on-this-machine')

            def agents(self):
                raise HubError('invalid', 'a-folder-on-this-machine is no agent list', 'agents')
        self.api = api.Api(self.store, views=Broken(), clock=CLOCK)
        want = {'backlog': 'store', 'completed': 'unavailable', 'notices': 'OSError',
                'repos': 'ZeroDivisionError', 'usage': 'KeyError', 'agents': 'invalid'}
        self.assertEqual(sorted(want), sorted(api.VIEWS))
        for name, why in sorted(want.items()):
            for role in (OWNER, AGENT):
                with self.subTest(view=name, role=role):
                    status, obj = self.call('GET', '/' + name, role)
                    self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                                     (503, 'unavailable', 'views'))
                    self.assertEqual(obj['error']['message'],
                                     'the %s view failed: %s' % (name, why))
                    self.assertNotIn('a-folder-on-this-machine', json.dumps(obj))
        # a bad query is still the caller's mistake, not the view's
        self.refused('invalid', 'fresh', 'GET', '/backlog', AGENT, None, {'fresh': 'maybe'})

    # ---- what an agent is told of a store refusal
    def test_a_store_refusal_gives_its_folder_to_the_owner_only(self):
        self.question()
        root = json.dumps(self.store.root)[1:-1]
        self.store.close()
        for method, path, body, query in (('GET', '/wait', None, {'timeout': '0'}),
                                          ('GET', '/wait', None, {'timeout': '0', 'pending': '1'}),
                                          ('GET', '/questions', None, None),
                                          ('GET', '/pending', None, None),
                                          ('GET', '/snapshot', None, None),
                                          ('POST', '/todos', {'title': 't', 'commands': ['dir']},
                                           None)):
            with self.subTest(path=path, query=query):
                status, obj = self.call(method, path, AGENT, body, query)
                self.assertEqual((status, obj['error']['code']), (503, 'store'))
                self.assertEqual(obj['error']['message'], api.AGENT_SAYS['store'])
                self.assertNotIn(root, json.dumps(obj))
                status, obj = self.call(method, path, OWNER, body, query)
                self.assertEqual((status, obj['error']['code']), (503, 'store'))
                self.assertIn(root, json.dumps(obj))

    def test_an_unexpected_error_gives_its_text_to_the_owner_only(self):
        def docs(coll):
            raise OSError('cannot read a-folder-on-this-machine')
        self.store.docs = docs
        status, obj = self.call('GET', '/questions', AGENT)
        self.assertEqual((status, obj['error']['code'], obj['error']['message']),
                         (500, 'internal', 'OSError'))
        status, obj = self.call('GET', '/questions', OWNER)
        self.assertEqual((status, obj['error']['code']), (500, 'internal'))
        self.assertIn('a-folder-on-this-machine', obj['error']['message'])
        # a role that is neither is told no more than an agent
        status, obj = self.call('GET', '/questions', 'somebody')
        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))

    # ---- agent_words: a transport whose answers leave this machine
    def test_with_agent_words_the_owner_may_do_what_an_owner_may_and_is_told_what_an_agent_is(self):
        # the role is the owner's still: an owner-only write passes, an agent-only one does not
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'}, agent_words=True)['id']
        self.ok('POST', '/todos/%s/done' % tid, OWNER, agent_words=True)
        status, obj = self.call('POST', '/todos/%s/ack' % tid, OWNER, agent_words=True)
        self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                         (403, 'forbidden', 'role'))
        # an error that has no wording of its own for an agent is the same answer
        for method, path, body in (('POST', '/todos', {}), ('GET', '/no-such-route', None),
                                   ('POST', '/todos/T-999/done', None)):
            with self.subTest(path=path):
                plain = self.call(method, path, OWNER, body)
                self.assertGreaterEqual(plain[0], 400)
                self.assertEqual(self.call(method, path, OWNER, body, agent_words=True), plain)
        root = json.dumps(self.store.root)[1:-1]
        self.store.close()
        for method, path, body, query in (('GET', '/todos', None, None),
                                          ('GET', '/snapshot', None, None),
                                          ('GET', '/wait', None, {'timeout': '0'}),
                                          ('POST', '/todos', {'title': 't'}, None),
                                          ('POST', '/todos/%s/delete' % tid, None, None)):
            with self.subTest(path=path, method=method):
                plain = self.call(method, path, OWNER, body, query)
                self.assertIn(root, json.dumps(plain[1]))
                status, obj = self.call(method, path, OWNER, body, query, agent_words=True)
                self.assertEqual((status, obj['error']['code']), (503, 'store'))
                self.assertEqual(obj['error']['message'],
                                 'the hub store is not available: the owner sees why')
                self.assertEqual(obj['error']['field'], plain[1]['error']['field'])
                self.assertNotIn(root, json.dumps(obj))
                self.assertNotIn(os.path.basename(self.store.root), json.dumps(obj))
                # the agent's answer is the one it always was
                self.assertEqual(self.call(method, path, AGENT, body, query, agent_words=True),
                                 self.call(method, path, AGENT, body, query))
        # only False gives the owner the store's own words: a value that is neither is no leak
        for words in (None, 1, 0, 'no', ''):
            with self.subTest(words=repr(words)):
                status, obj = self.call('GET', '/todos', OWNER, agent_words=words)
                self.assertEqual(obj['error']['message'], api.AGENT_SAYS['store'])
        self.assertIn(root, json.dumps(self.call('GET', '/todos', OWNER, agent_words=False)[1]))

    def test_with_agent_words_an_unexpected_error_is_its_class_name_for_the_owner_too(self):
        def docs(coll):
            raise OSError('cannot read a-folder-on-this-machine')
        self.store.docs = docs
        status, obj = self.call('GET', '/questions', OWNER, agent_words=True)
        self.assertEqual((status, obj['error']['code'], obj['error']['message']),
                         (500, 'internal', 'OSError'))
        with self.assertRaises(OSError):        # a transport with a log of its own still gets it
            self.call('GET', '/questions', OWNER, agent_words=True, raise_unexpected=True)

    def test_the_stores_own_invalid_still_tells_an_agent_what_is_wrong(self):
        obj = self.refused('invalid', None, 'POST', '/todos', AGENT, {'title': 't'})
        self.assertIn('handstep-commands', obj['error']['message'])
        self.assertNotIn(json.dumps(self.store.root)[1:-1], json.dumps(obj))

    # ---- the send check and the store
    def test_the_send_check_refuses_exactly_what_the_store_refuses(self):
        good = {'status': 'queued', 'kind': 'fyi', 'text': 'good'}
        notes = {
            'P-001': good,
            'P-002': {'status': 'queued', 'kind': 'fyi'},                      # no text
            'P-003': {'status': 'queued', 'text': 'no kind'},
            'P-004': dict(good, text=7),
            'P-005': dict(good, text=''),
            'P-006': dict(good, kind='order'),
            'P-007': dict(good, kind=None),
            'P-008': dict(good, urgent='yes'),
            'P-009': dict(good, reply=7),
            'P-010': dict(good, refs='Q-999'),
            'P-011': dict(good, refs=['Q-999', 'T-404'], repo='no-such-repo'),  # warnings only
            'P-012': dict(good, repo=3),
            'P-013': dict(good, done_at=5, sent_at=5),
            'P-014': dict(good, surprise={'a': 1}),
            'P-015': dict(good, created_at='yesterday'),
            'P-016': dict(good, text='  '),
        }
        self.store.close()
        self.store = seeded(self, dict(('to_pm/%s.json' % k, v) for k, v in notes.items()))
        _, docs = self.store.snapshot()
        self.assertEqual(sorted(docs['to_pm']), sorted(notes))
        changes = {'status': 'sent', 'sent_at': NOW, 'via': 'web'}
        told = {}
        for pid in sorted(notes):
            why = api._refusal(docs, 'to_pm', pid, changes)
            try:
                self.store.update('to_pm', pid, changes, if_version=api._version(docs['to_pm'][pid]))
                real = None
            except HubError as e:
                real = e
            with self.subTest(id=pid, note=notes[pid]):
                self.assertEqual(why and (why.code, why.field), real and (real.code, real.field))
                if why and real:    # the store's own words, after the note they are about
                    self.assertEqual(why.message, 'to_pm/%s: %s' % (pid, real.message))
            told[pid] = real.field if real else None
        self.assertIsNone(told['P-001'])
        self.assertIsNone(told['P-011'])
        self.assertEqual((told['P-002'], told['P-006']), ('text', 'kind'))
        self.assertGreaterEqual(len([v for v in told.values() if v]), 4, told)

    # ---- a blank ruling
    def test_a_blank_ruling_is_refused_not_stored_empty(self):
        pid = self.note()
        self.ok('POST', '/to_pm/send', OWNER)
        path = '/to_pm/%s/reply' % pid
        for blank in ('', '  ', '\t\n'):
            with self.subTest(ruling=blank):
                self.refused('invalid', 'ruling', 'POST', path, AGENT,
                             {'reply': 'r', 'ruling': blank})
        self.assertEqual(self.store.get('to_pm', pid)['status'], 'sent')
        self.ok('POST', path, AGENT, {'reply': 'r', 'ruling': None})       # null: none given
        self.assertNotIn('ruling', self.store.get('to_pm', pid))

    # ---- the store closes while a wait is open
    def test_a_store_closed_during_a_wait_is_the_stores_refusal(self):
        real = self.store.wait
        for role in (AGENT, OWNER):
            for query in ({'timeout': '30'}, {'timeout': '30', 'pending': '1'}):
                with self.subTest(role=role, query=query):
                    root = tempfile.mkdtemp(prefix='kit-hubapi-')
                    self.addCleanup(shutil.rmtree, root, True)
                    store = Store.open(root, clock=CLOCK)
                    self.addCleanup(store.close)
                    real, entered = store.wait, []

                    def wait(since, timeout, _real=real, _store=store, _entered=entered):
                        _entered.append(self.api.pm_seen()['waiting'])
                        _store.close()          # the server stops while this call waits
                        return _real(since, timeout)
                    store.wait = wait
                    self.api = api.Api(store, clock=CLOCK)
                    status, obj = self.api.call('GET', BASE + '/wait', role, None, query)
                    self.assertEqual((status, obj['error']['code']), (503, 'store'))
                    self.assertNotIn('timed_out', obj)
                    self.assertEqual(entered, [1 if role == AGENT else 0])
                    self.assertEqual(self.api.pm_seen()['waiting'], 0)

    # ---- null and "" on an edit
    def test_a_null_repo_on_an_edit_clears_it_as_an_empty_one_does(self):
        pid = self.note(repo='kit')
        tid = self.ok('POST', '/todos', OWNER, {'title': 't', 'repo': 'kit', 'detail': 'd'})['id']
        for coll, path, doc_id, names in (('to_pm', '/to_pm/%s/edit' % pid, pid, ('repo',)),
                                          ('todos', '/todos/%s/edit' % tid, tid,
                                           ('repo', 'detail'))):
            for name in names:
                for empty in (None, ''):
                    with self.subTest(coll=coll, name=name, value=empty):
                        self.ok('POST', path, OWNER, {name: 'kit'})
                        self.assertEqual(self.store.get(coll, doc_id)[name], 'kit')
                        self.ok('POST', path, OWNER, {name: empty})
                        self.assertEqual(self.store.get(coll, doc_id)[name], '')
        # any other null field is still read as absent: alone it changes nothing
        self.refused('invalid', 'body', 'POST', '/to_pm/%s/edit' % pid, OWNER, {'text': None})
        self.refused('invalid', 'body', 'POST', '/todos/%s/edit' % tid, OWNER, {'title': None})
        self.ok('POST', '/to_pm/%s/edit' % pid, OWNER, {'text': None, 'repo': None})
        self.assertEqual(self.store.get('to_pm', pid)['text'], 'one')


class OwnTodoTest(ApiCase):
    """The owner's own to-dos: urgent, edit, delete. A to-do an agent gave is not theirs to edit,
    only to delete while it is open (docs/action-hub.md, todos: "the owner can set it on their
    own items")."""

    def mine(self, **kw):
        body = {'title': 't', 'detail': 'd'}
        body.update(kw)
        return self.ok('POST', '/todos', OWNER, body)['id']

    def theirs(self):
        return self.ok('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir']})['id']

    def test_urgent_on_a_new_to_do_is_true_or_false(self):
        tid = self.mine(urgent=True)
        self.assertIs(self.store.get('todos', tid)['urgent'], True)
        for bad in (None, 'yes', 1):
            with self.subTest(urgent=bad):
                self.refused('invalid', 'urgent', 'POST', '/todos', OWNER,
                             {'title': 't', 'urgent': bad})

    def test_the_owner_edits_his_own_to_do(self):
        tid = self.mine()
        obj = self.ok('POST', '/todos/%s/edit' % tid, OWNER, {'urgent': True}, via='web')
        self.assertEqual((obj['id'], obj['version'], obj['status']), (tid, 2, 'open'))
        obj = self.ok('POST', '/todos/%s/edit' % tid, OWNER,
                      {'title': 'new', 'detail': 'more', 'repo': 'kit', 'commands': ['dir'],
                       'urgent': False, 'if_version': 2})
        d = self.store.get('todos', tid)
        self.assertEqual((d['title'], d['detail'], d['repo'], d['commands'], d['urgent']),
                         ('new', 'more', 'kit', ['dir'], False))
        self.assertEqual((d['status'], d['from'], d['via']), ('open', 'you', 'api'))
        self.ok('POST', '/todos/%s/done' % tid, OWNER)
        self.assertEqual(self.ok('POST', '/todos/%s/edit' % tid, OWNER,
                                 {'title': 'later'})['status'], 'done')
        for body, field in (({}, 'body'), (None, 'body'), ({'title': ' '}, 'title'),
                            ({'urgent': None}, 'urgent'), ({'urgent': 'yes'}, 'urgent'),
                            ({'commands': 'dir'}, 'commands'), ({'status': 'done'}, 'status'),
                            ({'from': 'pm'}, 'from'), ({'pm_ack': True}, 'pm_ack')):
            with self.subTest(body=body):
                self.refused('invalid', field, 'POST', '/todos/%s/edit' % tid, OWNER, body)
        self.refused('conflict', 'if_version', 'POST', '/todos/%s/edit' % tid, OWNER,
                     {'title': 'x', 'if_version': 1})

    def test_the_owner_deletes_his_own_to_do(self):
        tid, keep = self.mine(), self.mine()
        self.refused('conflict', 'if_version', 'POST', '/todos/%s/delete' % tid, OWNER,
                     {'if_version': 9})
        self.refused('invalid', 'title', 'POST', '/todos/%s/delete' % tid, OWNER, {'title': 'x'})
        obj = self.ok('POST', '/todos/%s/delete' % tid, OWNER)
        self.assertEqual((obj['id'], obj['deleted']), (tid, True))
        self.assertIsNone(self.store.get('todos', tid))
        self.assertEqual([d['id'] for d in self.ok('GET', '/todos', OWNER)['items']], [keep])

    def test_an_agent_cannot_edit_or_withdraw_the_owners_to_do(self):
        tid = self.mine()
        for name in ('you', 'pm', ''):
            with self.subTest(claimed=name):
                self.refused('invalid' if not name else 'forbidden', 'from', 'POST',
                             '/todos/%s/edit' % tid, AGENT,
                             {'title': 'x', 'from': name, 'if_version': 1})
                self.refused('invalid' if not name else 'forbidden', 'from', 'POST',
                             '/todos/%s/delete' % tid, AGENT, {'from': name, 'if_version': 1})

    def test_a_to_do_an_agent_gave_is_not_the_owners_to_edit_but_is_to_delete_while_open(self):
        tid = self.theirs()
        for body in ({'title': 'x'}, {'urgent': True}):
            self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, OWNER, body)
        self.assertEqual(self.store.get('todos', tid)['title'], 't')
        self.ok('POST', '/todos/%s/done' % tid, OWNER)      # ticking it stays the owner's
        self.refused('conflict', 'status', 'POST', '/todos/%s/delete' % tid, OWNER)
        self.assertEqual(self.store.get('todos', tid)['status'], 'done')
        other = self.theirs()
        self.ok('POST', '/todos/%s/delete' % other, OWNER)
        self.assertIsNone(self.store.get('todos', other))

    def test_a_to_do_with_no_readable_from_is_not_the_owners_to_edit(self):
        self.store = seeded(self, {'todos/T-1.json': {'status': 'done', 'title': 't'},
                                   'todos/T-2.json': {'status': 'done', 'title': 't',
                                                      'from': ['you']}})
        self.api = api.Api(self.store, clock=CLOCK)
        for tid in ('T-1', 'T-2'):
            self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, OWNER,
                         {'title': 'x'})
            self.refused('conflict', 'status', 'POST', '/todos/%s/delete' % tid, OWNER)
        # a list is no agent's name either
        self.refused('forbidden', 'from', 'POST', '/todos/T-2/edit', AGENT,
                     {'title': 'x', 'from': 'you', 'if_version': 0})

    def test_edit_or_delete_of_a_to_do_that_is_not_there(self):
        self.refused('not_found', 'id', 'POST', '/todos/T-404/edit', OWNER, {'title': 'x'})
        self.refused('not_found', 'id', 'POST', '/todos/T-404/delete', OWNER)


class CreatorTest(ApiCase):
    """An agent edits or withdraws an open question or to-do it added, giving the item's `from`
    and the version it read; the owner withdraws any open one (kitlib.hubsite.api "Creators")."""

    def agent_todo(self, who='worker 3', **kw):
        body = {'title': 't', 'detail': 'd', 'commands': ['dir'], 'from': who}
        body.update(kw)
        return self.ok('POST', '/todos', AGENT, body)['id']

    def unchanged(self, coll, doc_id, before):
        self.assertEqual(self.store.get(coll, doc_id), before)

    # ---- questions
    def test_the_asker_edits_its_open_question(self):
        qid = self.question()
        obj = self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                      {'from': 'pm', 'if_version': 1, 'title': 'new', 'context': 'why',
                       'options': [{'label': 'only', 'description': 'd'}], 'recommended': 0,
                       'refs': ['B-1'], 'urgent': True}, via='cli')
        self.assertEqual((obj['id'], obj['version'], obj['status']), (qid, 2, 'open'))
        d = self.store.get('questions', qid)
        self.assertEqual((d['title'], d['context'], d['options'], d['recommended'], d['refs'],
                          d['urgent'], d['status'], d['from'], d['via']),
                         ('new', 'why', [{'label': 'only', 'description': 'd'}], 0, ['B-1'],
                          True, 'open', 'pm', 'cli'))

    def test_a_question_edit_takes_only_its_fields_and_a_pin(self):
        qid = self.question()
        path = '/questions/%s/edit' % qid
        for body, field in (({'from': 'pm', 'if_version': 1}, 'body'),
                            ({'title': 'x', 'if_version': 1}, 'from'),
                            ({'from': 'pm', 'title': 'x'}, 'if_version'),
                            ({'from': 'pm', 'if_version': 1, 'title': ' '}, 'title'),
                            ({'from': 'pm', 'if_version': 1, 'options': 'a'}, 'options'),
                            ({'from': 'pm', 'if_version': 1, 'urgent': 'yes'}, 'urgent'),
                            ({'from': 'pm', 'if_version': 1, 'status': 'answered'}, 'status'),
                            ({'from': 'pm', 'if_version': 1, 'answer': {}}, 'answer'),
                            ({'from': 'pm', 'if_version': 1, 'repo': 'x'}, 'repo'),
                            ({'from': 'pm', 'if_version': 1, 'story': 'S-1'}, 'story')):
            with self.subTest(body=body):
                self.refused('invalid', field, 'POST', path, AGENT, body)
        self.refused('forbidden', 'role', 'POST', path, OWNER,
                     {'from': 'pm', 'if_version': 1, 'title': 'x'})

    def test_only_the_asker_edits_or_withdraws_a_question(self):
        self.api = api.Api(self.store, clock=CLOCK, session_keys=lambda: ['kit-1', 'kit-2'])
        qid = self.question(**{'from': 'kit-1'})
        before = self.store.get('questions', qid)
        for name in ('kit-2', 'pm', 'Kit-1', 'you'):
            with self.subTest(claimed=name):
                obj = self.refused('forbidden', 'from', 'POST', '/questions/%s/edit' % qid,
                                   AGENT, {'from': name, 'if_version': 1, 'title': 'x'})
                self.assertIn('kit-1', obj['error']['message'])
                self.refused('forbidden', 'from', 'POST', '/questions/%s/delete' % qid, AGENT,
                             {'from': name, 'if_version': 1})
        self.unchanged('questions', qid, before)
        self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                {'from': ' kit-1 ', 'if_version': 1, 'title': 'x'})

    def test_a_question_with_no_from_is_the_pms(self):
        self.store = seeded(self, {'questions/Q-1.json': {'status': 'open', 'title': 't',
                                                          'options': OPTIONS}})
        self.api = api.Api(self.store, clock=CLOCK)
        self.refused('forbidden', 'from', 'POST', '/questions/Q-1/edit', AGENT,
                     {'from': 'kit-1', 'if_version': 0, 'title': 'x'})
        self.ok('POST', '/questions/Q-1/edit', AGENT, {'from': 'pm', 'if_version': 0,
                                                       'title': 'x'})

    def test_the_asker_withdraws_its_open_question_and_it_is_gone(self):
        qid, keep = self.question(), self.question()
        self.refused('invalid', 'if_version', 'POST', '/questions/%s/delete' % qid, AGENT,
                     {'from': 'pm'})
        self.refused('invalid', 'from', 'POST', '/questions/%s/delete' % qid, AGENT,
                     {'if_version': 1})
        self.refused('invalid', 'title', 'POST', '/questions/%s/delete' % qid, AGENT,
                     {'from': 'pm', 'if_version': 1, 'title': 'x'})
        obj = self.ok('POST', '/questions/%s/delete' % qid, AGENT, {'from': 'pm', 'if_version': 1})
        self.assertEqual((obj['id'], obj['deleted']), (qid, True))
        self.assertIsNone(self.store.get('questions', qid))
        self.assertEqual([d['id'] for d in self.ok('GET', '/questions', OWNER)['items']], [keep])
        self.assertEqual([p['id'] for p in self.ok('GET', '/pending', AGENT)['items']], [])
        self.refused('not_found', 'id', 'POST', '/questions/%s/delete' % qid, AGENT,
                     {'from': 'pm', 'if_version': 1})

    def test_the_owner_withdraws_an_open_question_and_never_edits_one(self):
        qid = self.question()
        self.refused('invalid', 'from', 'POST', '/questions/%s/delete' % qid, OWNER,
                     {'from': 'pm'})
        self.refused('conflict', 'if_version', 'POST', '/questions/%s/delete' % qid, OWNER,
                     {'if_version': 3})
        self.ok('POST', '/questions/%s/delete' % qid, OWNER, via='web')
        self.assertIsNone(self.store.get('questions', qid))

    def test_an_answered_or_logged_question_is_not_changed(self):
        answered = self.question()
        self.ok('POST', '/questions/%s/answer' % answered, OWNER, {'choice': 0})
        logged = self.question()
        self.ok('POST', '/questions/%s/answer' % logged, OWNER, {'choice': 1})
        self.ok('POST', '/questions/%s/log' % logged, AGENT, {'ruling': R1, 'if_version': 2})
        for qid, version in ((answered, 2), (logged, 3)):
            before = self.store.get('questions', qid)
            with self.subTest(id=qid):
                self.refused('conflict', 'status', 'POST', '/questions/%s/edit' % qid, AGENT,
                             {'from': 'pm', 'if_version': version, 'title': 'x'})
                self.refused('conflict', 'status', 'POST', '/questions/%s/delete' % qid, AGENT,
                             {'from': 'pm', 'if_version': version})
                self.refused('conflict', 'status', 'POST', '/questions/%s/delete' % qid, OWNER)
                self.unchanged('questions', qid, before)

    def test_a_stale_version_changes_nothing(self):
        qid = self.question()
        self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                {'from': 'pm', 'if_version': 1, 'title': 'second'})
        tid = self.agent_todo()
        self.ok('POST', '/todos/%s/edit' % tid, AGENT,
                {'from': 'worker 3', 'if_version': 1, 'title': 'second'})
        for coll, path, body in (
                ('questions', '/questions/%s/edit' % qid, {'title': 'third'}),
                ('questions', '/questions/%s/delete' % qid, {}),
                ('todos', '/todos/%s/edit' % tid, {'title': 'third'}),
                ('todos', '/todos/%s/delete' % tid, {})):
            doc_id = qid if coll == 'questions' else tid
            who = 'pm' if coll == 'questions' else 'worker 3'
            before = self.store.get(coll, doc_id)
            with self.subTest(path=path):
                self.refused('conflict', 'if_version', 'POST', path, AGENT,
                             dict(body, **{'from': who, 'if_version': 1}))
                self.unchanged(coll, doc_id, before)
                self.assertEqual(before['title'], 'second')

    # ---- to-dos
    def test_the_creator_edits_its_open_to_do(self):
        tid = self.agent_todo()
        self.ok('POST', '/todos/%s/ack' % tid, AGENT)
        obj = self.ok('POST', '/todos/%s/edit' % tid, AGENT,
                      {'from': 'worker 3', 'if_version': 2, 'title': 'new', 'detail': None,
                       'commands': ['dir /b'], 'repo': 'kit', 'refs': ['B-1'], 'urgent': True})
        self.assertEqual((obj['version'], obj['status']), (3, 'open'))
        d = self.store.get('todos', tid)
        self.assertEqual((d['title'], d['detail'], d['commands'], d['repo'], d['refs'],
                          d['urgent'], d['from'], d['pm_ack']),
                         ('new', '', ['dir /b'], 'kit', ['B-1'], True, 'worker 3', False))

    def test_an_agent_to_do_edit_is_judged_by_the_to_do_rules(self):
        tid = self.agent_todo()
        before = self.store.get('todos', tid)
        for body, field in (({'commands': ['git worktree add x']}, 'commands'),
                            ({'commands': ['notepad C:\\a.txt']}, 'commands'),
                            ({'detail': 'Open it and apply the change.'}, 'detail'),
                            ({'commands': ['echo a\ud800b']}, 'commands'),
                            ({'commands': 'dir'}, 'commands'),
                            ({'steps': [{'run': 'dir'}]}, 'steps'),
                            ({'commands': []}, None)):
            with self.subTest(body=body):
                self.refused('invalid', field, 'POST', '/todos/%s/edit' % tid, AGENT,
                             dict(body, **{'from': 'worker 3', 'if_version': 1}))
                self.unchanged('todos', tid, before)

    def test_only_the_creator_edits_or_withdraws_a_to_do(self):
        tid = self.agent_todo()
        pm = self.agent_todo('')            # stored as from: pm
        before = self.store.get('todos', tid)
        for name in ('worker 4', 'pm', 'Worker 3', 'you'):
            with self.subTest(claimed=name):
                self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, AGENT,
                             {'from': name, 'if_version': 1, 'title': 'x'})
                self.refused('forbidden', 'from', 'POST', '/todos/%s/delete' % tid, AGENT,
                             {'from': name, 'if_version': 1})
        self.unchanged('todos', tid, before)
        self.ok('POST', '/todos/%s/delete' % pm, AGENT, {'from': 'pm', 'if_version': 1})
        self.ok('POST', '/todos/%s/delete' % tid, AGENT, {'from': 'worker 3', 'if_version': 1})
        self.assertEqual(self.store.docs('todos'), {})

    def test_a_done_to_do_is_not_changed_by_its_creator(self):
        tid = self.agent_todo()
        self.ok('POST', '/todos/%s/done' % tid, OWNER)
        before = self.store.get('todos', tid)
        self.refused('conflict', 'status', 'POST', '/todos/%s/edit' % tid, AGENT,
                     {'from': 'worker 3', 'if_version': 2, 'title': 'x'})
        self.refused('conflict', 'status', 'POST', '/todos/%s/delete' % tid, AGENT,
                     {'from': 'worker 3', 'if_version': 2})
        self.refused('conflict', 'status', 'POST', '/todos/%s/delete' % tid, OWNER)
        self.unchanged('todos', tid, before)


class PmSeenTest(ApiCase):
    """The snapshot says when an agent last waited and last pulled."""

    def setUp(self):
        super().setUp()
        self.now = ['2026-01-02T03:00:00+00:00']
        self.api = api.Api(self.store, clock=lambda: self.now[0])

    def at(self, minute):
        self.now[0] = '2026-01-02T03:%02d:00+00:00' % minute
        return self.now[0]

    def seen(self, role=OWNER):
        return self.ok('GET', '/snapshot', role)['pm_seen']

    def test_nobody_seen_yet_is_null(self):
        self.assertEqual(self.seen(), {'waited': None, 'pulled': None, 'waiting': 0})
        self.assertEqual(self.seen(AGENT), {'waited': None, 'pulled': None, 'waiting': 0})

    def test_an_agents_wait_and_pull_are_noted_with_their_time(self):
        t1 = self.at(1)
        self.ok('GET', '/wait', AGENT, None, {'timeout': '0'})
        self.assertEqual(self.seen(), {'waited': t1, 'pulled': None, 'waiting': 0})
        t2 = self.at(2)
        self.ok('GET', '/pending', AGENT)
        self.assertEqual(self.seen(), {'waited': t1, 'pulled': t2, 'waiting': 0})
        t3 = self.at(3)
        self.ok('GET', '/wait', AGENT, None, {'timeout': '0', 'pending': '1'})
        self.assertEqual(self.seen(), {'waited': t3, 'pulled': t3, 'waiting': 0})
        self.at(4)
        self.assertEqual(self.seen(AGENT), {'waited': t3, 'pulled': t3, 'waiting': 0})

    def test_a_wait_with_work_pending_is_a_wait_and_a_pull(self):
        self.ok('POST', '/to_pm', OWNER, {'text': 'x'})
        self.ok('POST', '/to_pm/send', OWNER)
        t = self.at(5)
        got = self.ok('GET', '/wait', AGENT, None, {'timeout': '30', 'pending': '1'})
        self.assertEqual(len(got['pending']), 1)
        self.assertEqual(self.seen(), {'waited': t, 'pulled': t, 'waiting': 0})

    def test_the_owners_own_calls_are_not_the_pm(self):
        self.at(1)
        self.ok('GET', '/wait', OWNER, None, {'timeout': '0', 'pending': '1'})
        self.ok('GET', '/pending', OWNER)
        self.assertEqual(self.seen(), {'waited': None, 'pulled': None, 'waiting': 0})

    def test_a_refused_call_is_not_seen(self):
        self.at(1)
        self.call('GET', '/wait', AGENT, None, {'timeout': 'x'})
        self.call('GET', '/wait', 'nobody', None, {'timeout': '0'})
        self.call('GET', '/pending', 'nobody')
        self.assertEqual(self.seen(), {'waited': None, 'pulled': None, 'waiting': 0})

    def test_a_wait_in_progress_counts_as_listening(self):
        real, during = self.store.wait, []

        def blocked(since, timeout):
            during.append(self.seen())          # what the page reads while the wait is open
            self.at(9)
            return real(since, 0)
        self.store.wait = blocked
        t1 = self.at(1)
        self.ok('GET', '/wait', AGENT, None, {'timeout': '30'})
        self.assertEqual(during, [{'waited': t1, 'pulled': None, 'waiting': 1}])
        self.assertEqual(self.seen(), {'waited': self.now[0], 'pulled': None, 'waiting': 0})
        del during[:]
        self.ok('GET', '/wait', OWNER, None, {'timeout': '30'})     # the page's own wait
        self.assertEqual(during[0]['waiting'], 0)

    def test_a_wait_that_fails_stops_counting(self):
        def broken(since, timeout):
            raise HubError('store', 'gone')
        self.store.wait = broken
        self.at(1)
        status, _ = self.call('GET', '/wait', AGENT, None, {'timeout': '30'})
        self.assertEqual(status, 503)
        self.assertEqual(self.seen()['waiting'], 0)

    def test_a_new_api_object_starts_at_null(self):
        self.at(1)
        self.ok('GET', '/pending', AGENT)
        self.api = api.Api(self.store, clock=lambda: self.now[0])       # a server restart
        self.assertEqual(self.seen(), {'waited': None, 'pulled': None, 'waiting': 0})


class ThirdFixTest(ApiCase):
    def test_a_clock_that_fails_as_a_wait_ends_neither_counts_nor_hides_the_error(self):
        calls = []

        def clock():
            calls.append(1)
            if len(calls) == 2:                 # the stamp as the wait ends
                raise RuntimeError('no clock')
            return CLOCK()

        def broken(since, timeout):
            raise HubError('store', 'gone')
        real = self.store.wait
        self.api = api.Api(self.store, clock=clock)
        self.store.wait = broken
        status, obj = self.call('GET', '/wait', AGENT, None, {'timeout': '30'})
        self.assertEqual((status, obj['error']['code']), (503, 'store'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.api.pm_seen()['waiting'], 0)
        del calls[:]
        self.store.wait = lambda since, timeout: real(since, 0)
        self.ok('GET', '/wait', AGENT, None, {'timeout': '30'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.api.pm_seen(), {'waited': CLOCK(), 'pulled': None, 'waiting': 0})

    def test_an_agents_from_on_an_inbox_item_follows_the_to_do_rule(self):
        path, coll, body = '/inbox', 'inbox', {'text': 'x'}
        for bad in ('you', 'Y o u', ' YOU ', 'уou', 'yｏu', 'pm͏', 'a\x00b',
                    'a  b'):
            with self.subTest(path=path, bad=bad):
                self.refused('invalid', 'from', 'POST', path, AGENT, dict(body, **{'from': bad}))
        with self.subTest(path=path, ok=True):
            did = self.ok('POST', path, AGENT, dict(body, **{'from': ' pm kit-2 '}))['id']
            self.assertEqual(self.store.get(coll, did)['from'], 'pm kit-2')
            did = self.ok('POST', path, AGENT, body)['id']
            self.assertNotIn('from', self.store.get(coll, did))

    def test_an_agents_from_on_a_question_is_a_session_key_or_the_pm(self):
        self.api = api.Api(self.store, clock=CLOCK, session_keys=lambda: {'kit-2'})
        body = {'title': 't', 'options': OPTIONS}
        for bad in ('you', 'Y o u', ' YOU ', 'уou', 'yｏu', 'pm͏', 'a\x00b',
                    'a  b', 'pm kit-2', 'Kit-2'):
            with self.subTest(bad=bad):
                self.refused('invalid', 'from', 'POST', '/questions', AGENT,
                             dict(body, **{'from': bad}))
        did = self.ok('POST', '/questions', AGENT, dict(body, **{'from': ' kit-2 '}))['id']
        self.assertEqual(self.store.get('questions', did)['from'], 'kit-2')
        did = self.ok('POST', '/questions', AGENT, body)['id']
        self.assertEqual(self.store.get('questions', did)['from'], 'pm')

    def test_an_owner_edit_of_an_acked_to_do_makes_it_pending_again(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': 't'})['id']

        def pending():
            return [p['id'] for p in self.ok('GET', '/pending', AGENT)['items']]
        self.assertEqual(pending(), [tid])
        self.ok('POST', '/todos/%s/ack' % tid, AGENT)
        self.assertEqual(pending(), [])
        self.ok('POST', '/todos/%s/edit' % tid, OWNER, {'title': 'changed'})
        self.assertEqual(pending(), [tid])
        self.assertIs(self.store.get('todos', tid)['pm_ack'], False)
        self.ok('POST', '/todos/%s/ack' % tid, AGENT)
        self.assertEqual(pending(), [])

    def test_two_agent_waits_at_once_are_both_counted(self):
        real, entered, answers = self.store.wait, [], []
        both = threading.Event()

        def wait(since, timeout):
            entered.append(1)
            if len(entered) == 2:
                both.set()
            return real(since, timeout)
        self.store.wait = wait

        def waiter():
            answers.append(self.api.call('GET', BASE + '/wait', AGENT, None, {'timeout': '30'}))
        threads = [threading.Thread(target=waiter, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        self.assertTrue(both.wait(20), 'both waits reached the store')
        self.assertEqual(self.api.pm_seen()['waiting'], 2)
        self.assertEqual(answers, [])           # neither has answered: both are blocked
        self.assertEqual(self.ok('GET', '/snapshot', OWNER)['pm_seen']['waiting'], 2)
        self.ok('POST', '/todos', OWNER, {'title': 'wake'})
        for t in threads:
            t.join(20)
            self.assertFalse(t.is_alive())
        self.assertEqual([(s, o['timed_out']) for s, o in answers], [(200, False)] * 2)
        self.assertEqual(self.api.pm_seen()['waiting'], 0)


class ParkedTest(ApiCase):
    def test_the_parked_state_reaches_the_backlog_view(self):
        views = Views()
        self.api = api.Api(self.store, views=views, clock=CLOCK)
        self.ok('GET', '/backlog', OWNER, None, {'state': 'parked'})
        self.ok('GET', '/backlog', OWNER, None, {'repo': 'kit', 'state': 'parked', 'fresh': '1'})
        self.assertEqual(views.calls, [('backlog', '', 'parked', False),
                                       ('backlog', 'kit', 'parked', True)])


class IdGuardTest(ApiCase):
    """Api.doc asks schema.check_id before the store: an id that could not be a file name is
    never looked up. (The store answers None for one today; the guard is what keeps that from
    being the only thing between a path and a file.)"""

    def test_an_id_that_is_no_file_name_never_reaches_the_store(self):
        seen = []
        real = self.store.get
        self.store.get = lambda coll, doc_id: seen.append(doc_id) or real(coll, doc_id)
        for bad in ('..', '..\\..\\x', 'a\\b', '..\\_meta', '.hidden', 'nul', 'x.', 'a b'):
            for method, tail, role, body in (('POST', '/inbox/%s/read', OWNER, None),
                                             ('POST', '/to_pm/%s/delete', OWNER, None),
                                             ('POST', '/questions/%s/answer', OWNER,
                                              {'choice': 0})):
                with self.subTest(id=bad, route=tail):
                    self.refused('not_found', 'id', method, tail % bad, role, body)
        # a `/` makes it another path, which no route has
        for bad in ('../x', 'a/b', '../../_meta'):
            with self.subTest(id=bad):
                self.refused('not_found', 'path', 'POST', '/inbox/%s/read' % bad, OWNER)
        self.assertEqual(seen, [], 'an id that is no file name was looked up in the store')
        mid = self.ok('POST', '/inbox', AGENT, {'text': 'x'})['id']
        self.ok('POST', '/inbox/%s/read' % mid, OWNER)
        self.assertEqual(seen, [mid])


class RecordViews(object):
    """The two record views as kitlib.hubsite.serve.SiteViews gives them: known names answer, the
    rest raise the HubErrors SiteViews maps kitlib.hubsite.backlog's refusals to."""

    def __init__(self):
        self.calls = []

    def record(self, repo, id):
        self.calls.append(('record', repo, id))
        if repo != 'kit':
            raise HubError('not_found', 'repo not known', 'repo')
        if id != 'EX-1':
            raise HubError('not_found', 'no such record', 'id')
        return {'repo': 'kit', 'id': id, 'where': 'open'}

    def record_template(self, repo):
        self.calls.append(('record_template', repo))
        return {'repo': repo, 'fields': []}


class RecordRouteTest(ApiCase):
    def setUp(self):
        ApiCase.setUp(self)
        self.views = RecordViews()
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def test_a_record_and_the_template(self):
        for role in (OWNER, AGENT):
            with self.subTest(role=role):
                self.assertEqual(self.ok('GET', '/records/kit/EX-1', role),
                                 {'repo': 'kit', 'id': 'EX-1', 'where': 'open',
                                  'revision': self.store.revision})
                self.assertEqual(self.ok('GET', '/records/kit/new', role)['fields'], [])
        self.assertEqual(self.views.calls[:2], [('record', 'kit', 'EX-1'),
                                                ('record_template', 'kit')])

    def test_the_literal_new_wins_over_an_id(self):
        self.assertIs(api.match('GET', BASE + '/records/kit/new')[0].handler, api.record_new)
        self.assertIs(api.match('GET', BASE + '/records/kit/NEW')[0].handler, api.record)

    def test_a_record_or_repository_that_is_not_there_is_a_404_naming_nothing_asked(self):
        for role in (OWNER, AGENT):
            with self.subTest(role=role):
                obj = self.refused('not_found', 'id', 'GET', '/records/kit/EX-404', role)
                self.assertEqual(obj['error']['message'], 'no such record')
                obj = self.refused('not_found', 'repo', 'GET', '/records/other/EX-1', role)
                self.assertEqual(obj['error']['message'], 'repo not known')

    def test_a_segment_that_can_be_no_name_is_refused_before_the_view(self):
        for path, field in (('/records/kit/..', 'id'), ('/records/kit/%2e%2e', 'id'),
                            ('/records/kit/a\\b', 'id'), ('/records/kit/.git', 'id'),
                            ('/records/kit/A..B', 'id'), ('/records/kit/' + 'A' * 129, 'id'),
                            ('/records/../EX-1', 'repo'), ('/records/C:/EX-1', 'repo'),
                            ('/records/%2e%2e/new', 'repo'), ('/records/-x/new', 'repo')):
            for role in (OWNER, AGENT):
                with self.subTest(path=path, role=role):
                    obj = self.refused('invalid', field, 'GET', path, role)
                    self.assertEqual(obj['error']['message'], api.SEGMENT_SAYS[field])
        self.assertEqual(self.views.calls, [])
        # a path with more segments is no record route
        self.refused('not_found', 'path', 'GET', '/records/kit/a/b', OWNER)
        self.assertEqual(self.views.calls, [])

    def test_a_view_that_raises_anything_else_is_unavailable_with_its_class_only(self):
        class Broken(object):
            def record(self, repo, id):
                raise OSError('C:\\secret\\folder is gone')

            def record_template(self, repo):
                raise ValueError('C:\\secret\\.kit.json')
        self.api = api.Api(self.store, views=Broken(), clock=CLOCK)
        for path, name, cls in (('/records/kit/EX-1', 'record', 'OSError'),
                                ('/records/kit/new', 'record_template', 'ValueError')):
            for role in (OWNER, AGENT):
                with self.subTest(path=path, role=role):
                    obj = self.refused('unavailable', 'views', 'GET', path, role)
                    self.assertEqual(obj['error']['message'],
                                     'the %s view failed: %s' % (name, cls))

    def test_no_record_view_is_not_available(self):
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        for path in ('/records/kit/EX-1', '/records/kit/new'):
            with self.subTest(path=path):
                self.refused('unavailable', 'views', 'GET', path, AGENT)

    def test_the_record_views_are_not_part_of_the_snapshot(self):
        self.assertNotIn('record', api.VIEWS)
        self.assertNotIn('record_template', api.VIEWS)
        self.ok('GET', '/snapshot', OWNER)
        self.assertEqual(self.views.calls, [])


class BindViews(object):
    """The record view as SiteViews gives it, for a question bound to a record: repo names match
    without case and the record answers with the configured name; DONE-1 is completed; repo
    `down` cannot be read."""

    def __init__(self):
        self.calls = []

    def record(self, repo, id):
        self.calls.append((repo, id))
        if repo.lower() == 'down':
            raise HubError('unavailable', 'git timed out', 'views')
        if repo.lower() != 'kit':
            raise HubError('not_found', 'repo not known', 'repo')
        if id == 'bad..id':
            raise HubError('invalid', 'not a record id', 'id')
        if id not in ('EX-1', 'EX-2', 'DONE-1'):
            raise HubError('not_found', 'no such record', 'id')
        return {'repo': 'kit', 'id': id, 'where': 'completed' if id == 'DONE-1' else 'open'}


class QuestionRecordTest(ApiCase):
    """A question bound to a backlog record: `record` {repo, id}, checked against the configured
    repositories when it is asked and when it is edited (docs/action-hub.md "questions/Q-NNN")."""

    def setUp(self):
        ApiCase.setUp(self)
        self.views = BindViews()
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def doc(self, qid):
        return self.store.docs('questions')[qid]

    def test_a_question_is_bound_to_a_record_by_its_configured_name(self):
        qid = self.question(record={'repo': 'KIT', 'id': 'EX-1'})
        self.assertEqual(self.doc(qid)['record'], {'repo': 'kit', 'id': 'EX-1'})
        self.assertEqual(self.views.calls, [('KIT', 'EX-1')])

    def test_a_question_without_a_record_is_unbound_and_reads_no_backlog(self):
        qid = self.question()
        self.assertNotIn('record', self.doc(qid))
        self.assertEqual(self.views.calls, [])

    def test_each_record_that_cannot_be_bound_is_refused_and_nothing_is_written(self):
        for record, code, says in (
                ('kit:EX-1', 'invalid', 'record is {"repo"'),
                ({'repo': 'kit'}, 'invalid', 'record is {"repo"'),
                ({'repo': 'kit', 'id': 'EX-1', 'more': 1}, 'invalid', 'record is {"repo"'),
                ({'repo': 'kit', 'id': 5}, 'invalid', 'record is {"repo"'),
                ({'repo': ' ', 'id': 'EX-1'}, 'invalid', 'record is {"repo"'),
                ({'repo': 'kit', 'id': 'bad..id'}, 'invalid', 'not a record id'),
                ({'repo': 'elsewhere', 'id': 'EX-1'}, 'not_found',
                 "names repo 'elsewhere', which is not a repository the hub has configured"),
                ({'repo': 'kit', 'id': 'EX-404'}, 'not_found',
                 "repo kit's backlog has no record EX-404"),
                ({'repo': 'kit', 'id': 'DONE-1'}, 'invalid',
                 'record kit:DONE-1 is completed: bind a question to an open record'),
                ({'repo': 'down', 'id': 'EX-1'}, 'unavailable',
                 'record down:EX-1 could not be checked: git timed out')):
            with self.subTest(record=record):
                obj = self.refused(code, 'record', 'POST', '/questions', AGENT,
                                   {'title': 't', 'record': record})
                self.assertIn(says, obj['error']['message'])
        self.assertEqual(self.store.docs('questions'), {})

    def test_a_record_is_refused_when_the_server_has_no_record_view(self):
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        obj = self.refused('unavailable', 'record', 'POST', '/questions', AGENT,
                           {'title': 't', 'record': {'repo': 'kit', 'id': 'EX-1'}})
        self.assertIn('record kit:EX-1 could not be checked', obj['error']['message'])
        self.api = api.Api(self.store, clock=CLOCK)
        self.refused('unavailable', 'record', 'POST', '/questions', AGENT,
                     {'title': 't', 'record': {'repo': 'kit', 'id': 'EX-1'}})

    def test_a_record_view_that_raises_anything_else_is_unavailable_with_its_class_only(self):
        class Broken(object):
            def record(self, repo, id):
                raise OSError('C:\\secret\\folder is gone')
        self.api = api.Api(self.store, views=Broken(), clock=CLOCK)
        obj = self.refused('unavailable', 'record', 'POST', '/questions', AGENT,
                           {'title': 't', 'record': {'repo': 'kit', 'id': 'EX-1'}})
        self.assertIn('OSError', obj['error']['message'])
        self.assertNotIn('secret', obj['error']['message'])

    def test_an_open_question_is_bound_rebound_and_cleared_by_its_asker(self):
        qid = self.question()
        self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                {'from': 'pm', 'if_version': 1, 'record': {'repo': 'kit', 'id': 'EX-1'}})
        self.assertEqual(self.doc(qid)['record'], {'repo': 'kit', 'id': 'EX-1'})
        self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                {'from': 'pm', 'if_version': 2, 'record': {'repo': 'kit', 'id': 'EX-2'}})
        self.assertEqual(self.doc(qid)['record'], {'repo': 'kit', 'id': 'EX-2'})
        for version, cleared in ((3, None), (4, '')):
            with self.subTest(cleared=cleared):
                self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                        {'from': 'pm', 'if_version': version, 'record': cleared})
                self.assertIsNone(self.doc(qid)['record'])
        self.assertEqual(self.views.calls, [('kit', 'EX-1'), ('kit', 'EX-2')])
        self.refused('not_found', 'record', 'POST', '/questions/%s/edit' % qid, AGENT,
                     {'from': 'pm', 'if_version': 5, 'record': {'repo': 'kit', 'id': 'EX-404'}})

    def test_an_edit_refused_for_its_asker_or_its_status_reads_no_backlog(self):
        qid = self.question()
        bind = {'repo': 'kit', 'id': 'EX-1'}
        self.refused('forbidden', 'from', 'POST', '/questions/%s/edit' % qid, AGENT,
                     {'from': 'someone', 'if_version': 1, 'record': bind})
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.refused('conflict', 'status', 'POST', '/questions/%s/edit' % qid, AGENT,
                     {'from': 'pm', 'if_version': 2, 'record': bind})
        self.assertEqual(self.views.calls, [])

    def test_an_unbound_question_from_before_still_edits_and_is_answered(self):
        qid = self.question()
        self.ok('POST', '/questions/%s/edit' % qid, AGENT,
                {'from': 'pm', 'if_version': 1, 'title': 'which'})
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 1})
        self.assertNotIn('record', self.doc(qid))
        self.assertEqual(self.views.calls, [])

    def test_the_schema_types_the_record(self):
        found = schema.check('questions', {'status': 'open', 'title': 't', 'record': 'kit:EX-1'})
        self.assertEqual([f.field for f in found], ['record'])
        self.assertEqual(schema.check('questions', {'status': 'open', 'title': 't',
                                                    'record': None}), [])


class SweepViews(BindViews):
    """The backlog and record views for `questions bind`: repo kit holds a parked record citing
    Q-1000, an owner-blocked one citing Q-1001 in its owner_ask, a parked one citing Q-1002
    (answered), one citing Q-1002 and Q-1003, a parked one citing nothing, a parked one citing
    Q-9999 (no such question), and a blocked one that waits on a tool (not the owner's) yet cites Q-1003 and Q-1004. RECS are
    the record views of the blocked ones; any other id answers as BindViews does."""

    ITEMS = [{'id': 'EX-1', 's': 'parked', 'q': 'Q-1000 (the owner)'},
             {'id': 'EX-2', 's': 'blocked'},
             {'id': 'EX-3', 's': 'parked', 'q': 'Q-1002'},
             {'id': 'EX-4', 's': 'parked', 'q': 'Q-1002 and Q-1003'},
             {'id': 'EX-5', 's': 'parked', 'q': 'later'},
             {'id': 'EX-6', 's': 'parked', 'q': 'Q-9999'},
             {'id': 'EX-7', 's': 'blocked'},
             {'id': 'EX-8', 's': 'open'}]
    OWNER = {'value': 'owner', 'kind': 'owner'}
    RECS = {'EX-2': {'blockers': [OWNER], 'hold': {'owner_ask': 'Q-1001: which one', 'q': None},
                     'body': ''},
            'EX-7': {'blockers': [{'value': 'tool:x', 'kind': 'tool'}],
                     'hold': {'owner_ask': 'Q-1004: not for the owner', 'q': 'Q-1003'},
                     'body': 'Blocked on Q-1004'},
            'EX-12': {'blockers': [{'value': 'owner Q-1003', 'kind': 'owner'}], 'hold': {},
                      'body': ''},
            'EX-13': {'blockers': [OWNER], 'hold': {},
                      'body': 'Blocked on Q-1004 until it is answered\nsee Q-1001'}}

    def backlog(self, repo='', state='', fresh=False):
        self.fresh = getattr(self, 'fresh', []) + [fresh]
        return [{'repo': 'kit', 'items': list(self.ITEMS), 'error': None},
                {'repo': 'down', 'items': [], 'error': 'git timed out'}]

    def record(self, repo, id):
        if id not in self.RECS:
            return BindViews.record(self, repo, id)
        return dict(self.RECS[id], repo='kit', id=id, where='open')


class QuestionBindTest(ApiCase):
    """questions bind: the PM pairs each parked or owner-blocked record with the open question it
    cites (docs/action-hub.md "Binding the open questions")."""

    def setUp(self):
        ApiCase.setUp(self)
        self.views = SweepViews()
        self.views.ITEMS = [dict(i, id=i['id']) for i in SweepViews.ITEMS]
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)
        self.q = [self.question() for _ in range(5)]
        self.assertEqual(self.q, ['Q-1000', 'Q-1001', 'Q-1002', 'Q-1003', 'Q-1004'])
        self.ok('POST', '/questions/Q-1002/answer', OWNER, {'choice': 0})

    def bind(self, **body):
        return self.ok('POST', '/questions/bind', AGENT, dict({'from': 'pm'}, **body))

    def test_a_dry_run_lists_pairs_misses_and_records_citing_several_questions(self):
        before = self.store.snapshot()
        got = self.bind(apply=False)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(got['pairs'], [
            {'question': 'Q-1000', 'record': {'repo': 'kit', 'id': 'EX-1'}, 'bound': False},
            {'question': 'Q-1001', 'record': {'repo': 'kit', 'id': 'EX-2'}, 'bound': False}])
        self.assertEqual(got['multi'], [{'record': {'repo': 'kit', 'id': 'EX-4'},
                                         'questions': ['Q-1002', 'Q-1003']}])
        self.assertEqual(sorted((m['record']['repo'], m['record']['id'] or '', m['why'])
                                for m in got['misses']),
                         [('down', '', 'the repository could not be read'),
                          ('kit', 'EX-3', 'Q-1002 is answered, not open'),
                          ('kit', 'EX-5', 'cites no question'),
                          ('kit', 'EX-6', 'Q-9999 is not a hub question')])
        self.assertEqual((got['applied'], got['skipped'], got['failed']), (False, [], []))
        self.assertEqual(self.views.fresh, [False])     # EX-7 cites two, on no owner block: unread

    def test_apply_reads_the_backlog_fresh(self):
        self.bind(apply=True)
        self.assertEqual(self.views.fresh, [True])

    def test_apply_binds_open_questions_only(self):
        got = self.bind(apply=True)
        self.assertEqual([(p['question'], p['bound']) for p in got['pairs']],
                         [('Q-1000', True), ('Q-1001', True)])
        docs = self.store.docs('questions')
        self.assertEqual(docs['Q-1000']['record'], {'repo': 'kit', 'id': 'EX-1'})
        self.assertEqual(docs['Q-1001']['record'], {'repo': 'kit', 'id': 'EX-2'})
        self.assertNotIn('record', docs['Q-1002'])
        self.assertNotIn('record', docs['Q-1003'])      # only a multi-cite record names it
        again = self.bind(apply=True)       # nothing left to do: both are bound already
        self.assertEqual((again['pairs'], [(s['question'], s['why']) for s in again['skipped']]),
                         ([], [('Q-1000', 'already bound to this record'),
                               ('Q-1001', 'already bound to this record')]))

    def test_a_question_a_multi_cite_record_also_cites_is_bound_to_neither(self):
        self.views.ITEMS.append({'id': 'EX-9', 's': 'parked', 'q': 'Q-1000 and Q-1004'})
        got = self.bind(apply=True)
        self.assertEqual([p['question'] for p in got['pairs']], ['Q-1001'])
        self.assertEqual([(s['question'], s['why']) for s in got['skipped']],
                         [('Q-1000', 'cited by more than one record')])
        self.assertNotIn('record', self.store.docs('questions')['Q-1000'])

    def test_cites_come_from_blocked_on_and_the_blocked_on_body_line(self):
        self.views.ITEMS = [{'id': 'EX-12', 's': 'blocked'}, {'id': 'EX-13', 's': 'blocked'}]
        got = self.bind(apply=False)
        # EX-13's body also says "see Q-1001": only the `Blocked on` line counts
        self.assertEqual([(p['question'], p['record']['id']) for p in got['pairs']],
                         [('Q-1003', 'EX-12'), ('Q-1004', 'EX-13')])
        self.assertEqual(([m['record']['repo'] for m in got['misses']], got['multi']), (['down'], []))

    def test_a_blocked_record_that_cannot_be_read_is_a_miss(self):
        self.views.ITEMS = [{'id': 'EX-11', 's': 'blocked'}]
        got = self.bind(apply=False)
        self.assertEqual([(m['record']['id'], m['why']) for m in got['misses'] if m['record']['repo'] == 'kit'],
                         [('EX-11', 'the record could not be read (not_found)')])
        self.assertEqual(got['pairs'], [])

    def test_a_question_bound_elsewhere_or_cited_twice_is_skipped(self):
        self.ok('POST', '/questions/Q-1000/edit', AGENT, {'from': 'pm', 'if_version': 1,
                                                       'record': {'repo': 'kit', 'id': 'EX-2'}})
        self.views.ITEMS.append({'id': 'EX-8', 's': 'parked', 'q': 'Q-1001'})
        got = self.bind(apply=True)
        self.assertEqual(got['pairs'], [])
        self.assertEqual({(s['question'], s['why']) for s in got['skipped']},
                         {('Q-1000', 'bound to another record'),
                          ('Q-1001', 'cited by more than one record')})
        self.assertNotIn('record', self.store.docs('questions')['Q-1001'])

    def test_a_record_that_fails_its_check_is_reported_and_the_rest_are_bound(self):
        self.views.ITEMS[0]['id'] = 'EX-404'
        got = self.bind(apply=True)
        self.assertEqual([(p['question'], p['bound']) for p in got['pairs']], [('Q-1001', True)])
        self.assertEqual([(f['question'], f['code']) for f in got['failed']],
                         [('Q-1000', 'not_found')])
        self.assertNotIn('record', self.store.docs('questions')['Q-1000'])

    def test_only_the_pm_binds(self):
        for who in ('some-coordinator', ' x '):
            self.refused('forbidden', 'from', 'POST', '/questions/bind', AGENT,
                         {'from': who, 'apply': True})
        self.refused('forbidden', 'role', 'POST', '/questions/bind', OWNER, {'from': 'pm'})
        self.assertNotIn('record', self.store.docs('questions')['Q-1000'])


class WriteViews(object):
    """The three record writes as SiteViews gives them: each call is kept, and `fail` is raised
    when set."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def _do(self, *call):
        self.calls.append(call)
        if self.fail:
            raise self.fail
        return {'lines': ['DONE']}

    def record_add(self, repo, id, summary, scopes, effort, ref):
        return self._do('add', repo, id, summary, scopes, effort, ref)

    def record_edit(self, repo, id, version, changes, unset, body):
        return self._do('edit', repo, id, version, changes, unset, body)

    def record_delete(self, repo, id, why):
        return self._do('delete', repo, id, why)

    def record_unblock(self, repo, id, on, reason):
        return self._do('unblock', repo, id, on, reason)


class RecordWriteRouteTest(ApiCase):
    def setUp(self):
        ApiCase.setUp(self)
        self.views = WriteViews()
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def test_each_write_hands_the_view_its_fields(self):
        self.assertEqual(self.ok('POST', '/records/kit', OWNER,
                                 {'id': 'EX-2', 'summary': 's', 'scope': 'tools/x.py'}),
                         {'lines': ['DONE'], 'ok': True, 'revision': self.store.revision})
        self.ok('POST', '/records/kit/EX-1/edit', OWNER,
                {'version': 'c:v', 'changes': {'effort': 's', 'scope': ['a', 'b']},
                 'unset': ['ref'], 'body': 'B\n'})
        self.ok('POST', '/records/kit/EX-1/delete', OWNER, {'why': 'w', 'confirm': 'EX-1'})
        self.ok('POST', '/records/kit/EX-1/unblock', OWNER, {})
        self.ok('POST', '/records/kit/EX-1/unblock', OWNER,
                {'on': 'item:EX-2', 'reason': 'landed'})
        self.assertEqual(self.views.calls, [
            ('add', 'kit', 'EX-2', 's', ['tools/x.py'], None, None),
            ('edit', 'kit', 'EX-1', 'c:v', [('effort', ['s']), ('scope', ['a', 'b'])], ['ref'],
             'B\n'),
            ('delete', 'kit', 'EX-1', 'w'),
            ('unblock', 'kit', 'EX-1', None, None),
            ('unblock', 'kit', 'EX-1', 'item:EX-2', 'landed')])

    def test_a_ruling_in_the_body_is_an_unknown_field_and_never_reaches_the_view(self):
        ruling = 'R-' + '9001'          # made up, built so no file holds a ruling id
        for path, body in (('/records/kit/EX-1/edit', {'version': 'c:v', 'ruling': ruling}),
                           ('/records/kit/EX-1/delete', {'why': 'w', 'confirm': 'EX-1',
                                                         'ruling': ruling}),
                           ('/records/kit/EX-1/unblock', {'ruling': ruling})):
            with self.subTest(path=path):
                obj = self.refused('invalid', 'ruling', 'POST', path, OWNER, body)
                self.assertIn("this route takes no 'ruling' field", obj['error']['message'])
        self.assertEqual(self.views.calls, [])

    def test_the_writes_are_the_owners(self):
        for method, path, body in (('POST', '/records/kit', {'id': 'A', 'summary': 's',
                                                             'scope': ['x']}),
                                   ('POST', '/records/kit/A/edit', {'version': 'v'}),
                                   ('POST', '/records/kit/A/delete', {'why': 'w',
                                                                      'confirm': 'A'}),
                                   ('POST', '/records/kit/A/unblock', {'on': 'owner'})):
            with self.subTest(path=path):
                self.refused('forbidden', None, method, path, AGENT, body)
        self.assertEqual(self.views.calls, [])

    def test_a_delete_whose_confirm_is_not_the_id_never_reaches_the_view(self):
        for confirm in ('EX-2', 'ex-1'):
            with self.subTest(confirm=confirm):
                obj = self.refused('invalid', 'confirm', 'POST', '/records/kit/EX-1/delete',
                                   OWNER, {'why': 'w', 'confirm': confirm})
                self.assertEqual(obj['error']['message'],
                                 'confirm must repeat the record id to delete it')
        self.refused('invalid', 'why', 'POST', '/records/kit/EX-1/delete', OWNER,
                     {'confirm': 'EX-1'})
        self.assertEqual(self.views.calls, [])

    def test_a_body_of_the_wrong_shape_never_reaches_the_view(self):
        for path, body, field in (
                ('/records/kit/EX-1/edit', {'version': 'v', 'changes': ['effort']}, 'changes'),
                ('/records/kit/EX-1/edit', {'version': 'v', 'changes': {'effort': 3}},
                 'effort'),
                ('/records/kit/EX-1/edit', {'version': 'v', 'state': 'open'}, None),
                ('/records/kit/EX-1/edit', {'changes': {}}, 'version'),
                ('/records/kit', {'id': 'A', 'summary': 's'}, 'scope'),
                ('/records/kit', {'id': 'A', 'summary': 's', 'scope': [1]}, 'scope'),
                ('/records/kit/EX-1/unblock', {'on': 3}, 'on'),
                ('/records/kit/EX-1/unblock', {'reason': ['r']}, 'reason'),
                ('/records/kit/EX-1/unblock', {'why': 'w'}, 'why'),
                ('/records/kit/EX..1/unblock', {}, 'id')):
            with self.subTest(body=body):
                self.refused('invalid', field, 'POST', path, OWNER, body)
        self.assertEqual(self.views.calls, [])

    def test_a_store_refusal_is_the_owners_text_and_the_fixed_phrase_to_anyone_else(self):
        self.views.fail = HubError('store', 'C:\\repo names no "backlog".store', 'repo')
        body = {'id': 'A', 'summary': 's', 'scope': ['x']}
        obj = self.refused('store', 'repo', 'POST', '/records/kit', OWNER, body)
        self.assertEqual(obj['error']['message'], 'C:\\repo names no "backlog".store')
        status, obj = self.api.call('POST', BASE + '/records/kit', OWNER, body,
                                    agent_words=True)
        self.assertEqual((status, obj['error']['message']), (503, api.AGENT_SAYS['store']))
        self.views.fail = HubError('conflict', 'stale', 'version')
        self.refused('conflict', 'version', 'POST', '/records/kit/A/edit', OWNER,
                     {'version': 'v'})


class ApprovalRouteTest(ApiCase):
    """The approvals routes over a real checkout: TempRepo's one commit holds README.md
    "hello\\n". The rules themselves are tests/hubsite/test_approvals_verify.py."""

    KEY = 'k' * 64
    URL = 'https://github.com/Owner/Repo.git'
    COMMIT = 'c' * 40

    @classmethod
    def setUpClass(cls):
        cls.repo = TempRepo()
        cls.addClassCleanup(cls.repo.cleanup)

    def setUp(self):
        super().setUp()
        self.asked = []

        def checkout(repo_id):
            self.asked.append(repo_id)
            return self.repo.root if repo_id == approvals.repo_id(self.URL) else None
        self.api = api.Api(self.store, clock=CLOCK, approval_key=lambda: self.KEY,
                           checkout=checkout)

    def files(self):
        return [{'path': 'README.md', 'base_blob': approvals.blob_id('hello\n'),
                 'content': 'hello world\n'},
                {'path': 'docs/new.md', 'base_blob': None, 'content': 'fresh\n'}]

    def add(self, **kw):
        body = {'repo': self.URL, 'title': 'Edit the readme', 'head': self.repo.initial,
                'files': self.files(), 'reason': 'asked for'}
        body.update(kw)
        return self.ok('POST', '/approvals', AGENT, body)

    def decide(self, doc_id, decision='approve', version=1):
        return self.ok('POST', '/approvals/%s/decide' % doc_id, OWNER,
                       {'decision': decision, 'if_version': version}, strong=True)

    def verify(self, doc_id, files=None, **query):
        doc = self.store.get('approvals', doc_id)
        q = {'repo': self.URL, 'head': self.repo.initial,
             'files': json.dumps(files if files is not None else
                                 {f['path']: f['blob'] for f in doc['files']})}
        q.update(query)
        return self.ok('GET', '/approvals/%s/verify' % doc_id, AGENT, query=q)

    def test_a_request_is_added_with_its_diff(self):
        obj = self.add()
        self.assertEqual(obj['version'], 1)
        doc = self.store.get('approvals', obj['id'])
        self.assertEqual((doc['status'], doc['repo'], doc['via']),
                         ('pending', 'github.com/Owner/Repo', 'api'))
        self.assertEqual(self.asked, ['github.com/Owner/Repo'])
        self.assertIn('-hello\n', doc['diff'])
        self.assertIn('+hello world\n', doc['diff'])
        self.assertIn('+++ b/docs/new.md\n', doc['diff'])

    def test_a_base_that_is_not_the_file_at_head_is_refused(self):
        files = self.files()
        files[0]['base_blob'] = approvals.blob_id('else\n')
        self.refused('invalid', 'files', 'POST', '/approvals', AGENT,
                     {'repo': self.URL, 'title': 't', 'head': self.repo.initial,
                      'files': files})

    def test_a_repo_with_no_checkout_here_is_refused(self):
        self.refused('invalid', 'repo', 'POST', '/approvals', AGENT,
                     {'repo': 'https://github.com/Owner/Other', 'title': 't',
                      'head': self.repo.initial, 'files': self.files()})
        self.api = api.Api(self.store, clock=CLOCK)         # no checkout given: fails closed
        self.refused('invalid', 'repo', 'POST', '/approvals', AGENT,
                     {'repo': self.URL, 'title': 't', 'head': self.repo.initial,
                      'files': self.files()})

    def test_only_the_strong_owner_decides(self):
        doc_id = self.add()['id']
        self.refused('forbidden', 'role', 'POST', '/approvals/%s/decide' % doc_id, OWNER,
                     {'decision': 'approve', 'if_version': 1})
        self.refused('forbidden', 'role', 'POST', '/approvals/%s/decide' % doc_id, AGENT,
                     {'decision': 'approve', 'if_version': 1}, strong=True)

    def test_an_approval_is_signed_so_check_accepts_it(self):
        doc_id = self.add()['id']
        obj = self.decide(doc_id)
        self.assertEqual((obj['status'], obj['version']), ('approved', 2))
        doc = self.store.get('approvals', doc_id)
        blobs = {f['path']: f['blob'] for f in doc['files']}
        now = datetime.datetime.fromisoformat(NOW)
        self.assertEqual(approvals.check(self.KEY, doc_id, doc, self.URL, self.repo.initial,
                                         blobs, now), (True, ''))

    def test_a_decision_on_a_stale_version_is_a_conflict(self):
        doc_id = self.add()['id']
        self.refused('conflict', 'if_version', 'POST', '/approvals/%s/decide' % doc_id, OWNER,
                     {'decision': 'approve', 'if_version': 7}, strong=True)
        self.refused('invalid', 'if_version', 'POST', '/approvals/%s/decide' % doc_id, OWNER,
                     {'decision': 'approve'}, strong=True)
        self.decide(doc_id, 'deny')
        self.refused('conflict', 'status', 'POST', '/approvals/%s/decide' % doc_id, OWNER,
                     {'decision': 'approve', 'if_version': 2}, strong=True)

    def test_an_approval_is_consumed_once(self):
        doc_id = self.add()['id']
        self.decide(doc_id)
        obj = self.ok('POST', '/approvals/%s/consume' % doc_id, AGENT,
                      {'commit': self.COMMIT, 'if_version': 2})
        self.assertEqual(obj['status'], 'consumed')
        self.assertEqual(self.store.get('approvals', doc_id)['commit'], self.COMMIT)
        self.refused('conflict', 'commit', 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'commit': self.COMMIT, 'if_version': 3})

    def test_a_request_not_approved_is_not_consumed(self):
        doc_id = self.add()['id']
        self.refused('conflict', None, 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'commit': self.COMMIT, 'if_version': 1})

    def test_verify_answers_the_check_and_never_the_sig(self):
        doc_id = self.add()['id']
        self.decide(doc_id)
        obj = self.verify(doc_id)
        self.assertEqual(obj, {'ok': True, 'valid': True, 'why': '',
                               'revision': self.store.revision})
        self.assertNotIn(self.store.get('approvals', doc_id)['sig'], json.dumps(obj))
        files = self.store.get('approvals', doc_id)['files']
        files[0]['blob'] = approvals.blob_id('else\n')     # an edit of the store folder
        self.store.update('approvals', doc_id, {'files': files})
        obj = self.verify(doc_id)
        self.assertEqual((obj['valid'], obj['why']), (False, approvals.WHY_UNSIGNED))

    def test_verify_of_an_unknown_id_or_bad_files_is_not_valid(self):
        obj = self.ok('GET', '/approvals/A-404/verify', AGENT, query={'files': '{}'})
        self.assertEqual((obj['valid'], obj['why']), (False, approvals.WHY_UNKNOWN))
        doc_id = self.add()['id']
        self.decide(doc_id)
        for files in ('not json', '', '["README.md"]'):
            obj = self.ok('GET', '/approvals/%s/verify' % doc_id, AGENT,
                          query={'repo': self.URL, 'head': self.repo.initial, 'files': files})
            self.assertEqual((obj['valid'], obj['why']), (False, approvals.WHY_FILES), files)

    def test_verify_with_a_commit_needs_the_approval_consumed_by_it(self):
        doc_id = self.add()['id']
        self.decide(doc_id)
        self.assertEqual(self.verify(doc_id, commit=self.COMMIT)['why'], approvals.WHY_COMMIT)
        self.assertEqual(self.verify(doc_id, commit='')['why'], approvals.WHY_COMMIT)
        self.assertIs(self.verify(doc_id)['valid'], True)
        self.ok('POST', '/approvals/%s/consume' % doc_id, AGENT,
                {'commit': self.COMMIT, 'if_version': 2})
        self.assertIs(self.verify(doc_id, commit=self.COMMIT)['valid'], True)
        self.assertEqual(self.verify(doc_id, commit='d' * 40)['why'], approvals.WHY_COMMIT)

    def test_the_snapshot_carries_approvals(self):
        doc_id = self.add()['id']
        for role in (OWNER, AGENT):
            cols = self.ok('GET', '/snapshot', role)['collections']
            self.assertEqual(cols['approvals'][doc_id]['title'], 'Edit the readme', role)
            self.assertIn('+hello world\n', cols['approvals'][doc_id]['diff'], role)

    def test_the_snapshot_says_strong_to_the_strong_owner_alone(self):
        self.assertIs(self.ok('GET', '/snapshot', OWNER, strong=True)['strong'], True)
        for role, strong in ((OWNER, False), (AGENT, True), (AGENT, False), (OWNER, 'yes')):
            with self.subTest(role=role, strong=strong):
                self.assertIs(self.ok('GET', '/snapshot', role, strong=strong)['strong'], False)


class OpApprovalRouteTest(ApiCase):
    """The approvals routes for the op kind: one run of a delegated operation (docs/delegations.md
    "Hub approvals"). No checkout is read for it. The rules are tests/hubsite/test_approvals_op.py."""

    KEY = 'k' * 64
    LINE = 'run D-001 dest=dd/out src=dd/in'

    def setUp(self):
        super().setUp()
        self.asked = []

        def checkout(repo_id):
            self.asked.append(repo_id)
            return None
        self.api = api.Api(self.store, clock=CLOCK, approval_key=lambda: self.KEY,
                           checkout=checkout)

    def add(self, **kw):
        body = {'kind': 'op', 'op': 'D-001', 'repo': 'pm', 'line': self.LINE,
                'title': 'Copy the inputs', 'reason': 'asked for', 'what': 'copies a folder'}
        body.update(kw)
        return self.ok('POST', '/approvals', AGENT, body)

    def approved(self):
        doc_id = self.add()['id']
        self.ok('POST', '/approvals/%s/decide' % doc_id, OWNER,
                {'decision': 'approve', 'if_version': 1}, strong=True)
        return doc_id

    def verify(self, doc_id, **query):
        q = {'kind': 'op', 'op': 'D-001', 'repo': 'pm', 'args': approvals.line_sha256(self.LINE)}
        q.update(query)
        return self.ok('GET', '/approvals/%s/verify' % doc_id, AGENT, query=q)

    def test_an_op_request_is_added_with_the_hash_and_no_checkout_or_diff(self):
        doc = self.store.get('approvals', self.add()['id'])
        self.assertEqual((doc['status'], doc['kind'], doc['op'], doc['repo'], doc['line'],
                          doc['args_sha256'], doc['what'], doc['via']),
                         ('pending', 'op', 'D-001', 'pm', self.LINE,
                          approvals.line_sha256(self.LINE), 'copies a folder', 'api'))
        self.assertNotIn('diff', doc)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.store.get('approvals', self.add(repo='*')['id'])['repo'], '*')

    def test_a_bad_op_request_is_refused(self):
        for kw, field in (({'kind': 'other'}, 'kind'), ({'kind': 'OP'}, 'kind'),
                          ({'kind': 7}, 'kind'), ({'op': 'D-1'}, 'op'), ({'repo': 'Pm'}, 'repo'),
                          ({'line': ''}, 'line'), ({'line': 'a\nb'}, 'line'),
                          ({'line': 'robocopy.exe a b'}, 'line'), ({'line': 'run D-002'}, 'line'),
                          ({'head': 'a' * 40}, 'head'), ({'files': []}, 'files'),
                          ({'args_sha256': 'a' * 64}, 'args_sha256')):
            body = {'kind': 'op', 'op': 'D-001', 'repo': 'pm', 'line': self.LINE, 'title': 't'}
            body.update(kw)
            with self.subTest(kw=kw):
                self.refused('invalid', field, 'POST', '/approvals', AGENT, body)

    def test_an_op_field_on_a_change_request_is_refused(self):
        for name in ('op', 'line', 'what', 'args_sha256'):
            self.refused('invalid', name, 'POST', '/approvals', AGENT,
                         {'repo': 'https://github.com/o/r', 'title': 't', 'head': 'a' * 40,
                          'files': [], name: 'x'})

    def test_an_approved_op_verifies_with_its_version_and_never_the_sig(self):
        doc_id = self.approved()
        obj = self.verify(doc_id)
        self.assertEqual(obj, {'ok': True, 'valid': True, 'why': '', 'version': 2,
                               'revision': self.store.revision})
        self.assertNotIn(self.store.get('approvals', doc_id)['sig'], json.dumps(obj))
        for query, why in (({'op': 'D-002'}, approvals.WHY_OP),
                           ({'repo': '*'}, approvals.WHY_REPO),
                           ({'args': approvals.line_sha256('x')}, approvals.WHY_ARGS)):
            obj = self.verify(doc_id, **query)
            self.assertEqual((obj['valid'], obj['why']), (False, why), query)
        obj = self.verify(self.add()['id'])
        self.assertEqual((obj['valid'], obj['why']), (False, approvals.WHY_UNSIGNED))

    def test_an_op_approval_is_not_a_change_and_a_change_query_does_not_pass_it(self):
        doc_id = self.approved()
        obj = self.ok('GET', '/approvals/%s/verify' % doc_id, AGENT,
                      query={'repo': 'pm', 'head': 'a' * 40, 'files': '{}'})
        self.assertEqual((obj['valid'], obj['why']), (False, approvals.WHY_KIND_OP))
        self.refused('conflict', None, 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'commit': 'c' * 40, 'if_version': 2})

    def test_an_op_approval_is_consumed_once_by_its_own_arguments(self):
        doc_id = self.approved()
        args = approvals.line_sha256(self.LINE)
        self.refused('conflict', None, 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'args_sha256': approvals.line_sha256('x'), 'if_version': 2})
        self.refused('invalid', 'args_sha256', 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'args_sha256': args, 'commit': 'c' * 40, 'if_version': 2})
        self.refused('conflict', 'if_version', 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'args_sha256': args, 'if_version': 1})
        obj = self.ok('POST', '/approvals/%s/consume' % doc_id, AGENT,
                      {'args_sha256': args, 'if_version': 2})
        self.assertEqual(obj['status'], 'consumed')
        doc = self.store.get('approvals', doc_id)
        self.assertEqual((doc['status'], doc['consumed_at']), ('consumed', NOW))
        self.refused('conflict', None, 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'args_sha256': args, 'if_version': 3})
        self.assertEqual(self.verify(doc_id)['why'], approvals.WHY_STATUS % 'consumed')

    def test_a_change_approval_is_not_consumed_by_arguments(self):
        now = datetime.datetime.fromisoformat(NOW)
        doc = approvals.new_doc('https://github.com/o/r', 't', 'a' * 40,
                                [{'path': 'x.md', 'base_blob': None, 'content': 'x\n'}], now)
        doc_id = self.store.add('approvals', doc).id
        self.store.update('approvals', doc_id, approvals.decide_fields(
            self.KEY, doc_id, self.store.get('approvals', doc_id), 'approve', now))
        version = self.store.get('approvals', doc_id)['version']
        self.refused('conflict', None, 'POST', '/approvals/%s/consume' % doc_id, AGENT,
                     {'args_sha256': approvals.line_sha256(self.LINE), 'if_version': version})
        obj = self.verify(doc_id)
        self.assertEqual((obj['valid'], obj['why']), (False, approvals.WHY_KIND_FILES))


class PushStatusTest(ApiCase):
    """The snapshot carries the push worker's status: delivery up or down, and what is undelivered.
    The worker runs on its own thread, as in a hub (state is 'down' until start() has finished)."""

    class Deliver(object):
        def __init__(self):
            self.states = {}
            self.calls = []

        def deliver(self, recipient, text):
            self.calls.append(recipient)
            return push.Sent(self.states.get(recipient, push.WRITTEN), None, None, False)

        def is_pm(self, recipient):
            return False

    def setUp(self):
        super().setUp()
        # a running worker reads the PM watcher's heartbeat; the default reader reads the
        # machine's config (the home fence)
        patcher = mock.patch.object(push, 'watcher_heartbeat', lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = api.Api(self.store, clock=CLOCK, session_keys=lambda: {'kit'})
        self.folder = tempfile.mkdtemp(prefix='kit-hubapi-push-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.fake = self.Deliver()
        self.t = [1000000.0]
        self.worker = push.PushWorker(self.store, self.api, self.fake, self.folder, clock=lambda: self.t[0])

    def run_worker(self):
        self.worker.start()
        self.addCleanup(self.worker.stop, 5)

    def status(self):
        return self.ok('GET', '/snapshot', OWNER)['push']

    def until(self, want):
        deadline = threading.Event()
        for _ in range(200):
            got = self.status()
            if all(got.get(k) == v for k, v in want.items()):
                return got
            deadline.wait(0.05)
        self.fail('push status never became %r: %r' % (want, self.status()))

    def answered(self, **kw):
        body = {'title': 't', 'options': OPTIONS}
        body.update(kw)
        qid = self.ok('POST', '/questions', AGENT, body)['id']
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        return qid

    def test_no_worker_is_null(self):
        self.assertIsNone(api.Api(self.store, clock=CLOCK).push_seen())

    def test_a_running_worker_is_up_with_nothing_undelivered(self):
        self.run_worker()
        self.assertEqual(self.status(), {'state': 'up', 'last': None, 'undelivered': 0})

    def test_a_worker_whose_start_failed_is_down(self):
        self.assertEqual(self.status()['state'], 'down', 'made, not started')
        with mock.patch.object(self.store, 'snapshot', side_effect=RuntimeError('no')):
            with self.assertRaises(RuntimeError):
                self.worker.start()
        self.assertEqual(self.status(), {'state': 'down', 'last': None, 'undelivered': 0})

    def test_a_delivery_to_the_pm_not_written_is_down_and_counts_the_item(self):
        self.fake.states['pm'] = push.NO_SESSION
        self.run_worker()
        self.answered()
        self.until({'state': 'down', 'last': push.NO_SESSION, 'undelivered': 1})
        self.fake.states['pm'] = push.WRITTEN
        self.t[0] += push.SCHEDULE_S[0]
        self.until({'state': 'up', 'last': push.WRITTEN, 'undelivered': 0})

    def test_an_ended_asking_session_does_not_make_delivery_down(self):
        self.fake.states['kit'] = push.NO_SESSION
        self.run_worker()
        self.answered(**{'from': 'kit'})
        got = self.until({'last': push.WRITTEN, 'undelivered': 1})
        self.assertEqual(got['state'], 'up', 'the PM heard it; only the ended session did not')

    def test_the_last_state_is_the_pms_whatever_order_the_round_tries(self):
        # poll() on this thread, so the order is fixed: the PM's earlier item makes the PM the
        # first recipient of the round, the ended session the last one tried
        self.fake.states['kit'] = push.NO_SESSION
        self.worker.begin()
        self.answered()
        self.worker.poll()
        self.t[0] += push.SCHEDULE_S[0]
        self.answered(**{'from': 'kit'})
        self.worker.poll()
        self.assertEqual(self.fake.calls, ['pm', 'pm', 'kit'], 'the ended session was tried last')
        got = self.status()
        self.assertEqual((got['last'], got['undelivered']), (push.WRITTEN, 1), got)

    def test_a_worker_whose_thread_ended_is_down(self):
        self.run_worker()
        self.assertEqual(self.status()['state'], 'up')
        self.assertTrue(self.worker.stop(5))
        self.assertEqual(self.status()['state'], 'down')

    def test_a_status_that_raises_is_down_with_its_class_name(self):
        def broken():
            raise RuntimeError('no')
        self.api.push_status = broken
        self.assertEqual(self.status(), {'state': 'down', 'last': 'RuntimeError', 'undelivered': None})

    # the snapshot and health carry the PM watcher's heartbeat as the push worker read it
    def seen(self, role=OWNER):
        return (self.ok('GET', '/snapshot', OWNER)['watcher'], self.ok('GET', '/health', role)['watcher'])

    def test_no_worker_is_null_in_the_snapshot_and_health(self):
        self.api = api.Api(self.store, clock=CLOCK)
        self.assertEqual(self.seen(), (None, None))

    def test_a_worker_says_the_heartbeats_state_and_age(self):
        beat = [None]
        self.worker = push.PushWorker(self.store, self.api, self.fake, self.folder,
                                      clock=lambda: self.t[0], watcher=lambda: beat[0])
        self.worker.begin()
        none = {'state': 'none', 'age': None}
        self.assertEqual(self.seen(), (none, none), 'never read')
        beat[0] = {'pid': 1, 'beat': self.t[0] - 20, 'stream': False, 'started': None}
        self.worker.watch()
        up = {'state': 'up', 'age': 20}
        self.assertEqual(self.seen(AGENT), (up, up))
        self.t[0] += push.WATCHER_STALE_S
        self.worker.watch()
        self.assertEqual(self.seen()[1], {'state': 'down', 'age': push.WATCHER_STALE_S + 20})

    def test_a_status_that_raises_is_down_with_no_age(self):
        def broken():
            raise RuntimeError('no')
        self.api.watcher_status = broken
        down = {'state': 'down', 'age': None}
        self.assertEqual(self.seen(), (down, down))

    # GET health carries the same push value as the snapshot
    def test_health_push_is_null_with_no_worker(self):
        self.api = api.Api(self.store, clock=CLOCK)
        self.assertIsNone(self.ok('GET', '/health', OWNER)['push'])

    def test_health_push_is_up_with_a_running_worker(self):
        self.run_worker()
        want = {'state': 'up', 'last': None, 'undelivered': 0}
        self.assertEqual(self.ok('GET', '/health', AGENT)['push'], want)
        self.assertEqual(self.ok('GET', '/health', OWNER)['push'], self.status())

    def test_health_push_is_down_when_the_worker_died(self):
        self.run_worker()
        self.assertTrue(self.worker.stop(5))
        self.assertEqual(self.ok('GET', '/health', OWNER)['push']['state'], 'down')


class PushedRouteTest(RouteCase):
    """A push written to a session keeps its items routed to it for the ack window, with no wait
    open (module docstring "Routes")."""

    def test_a_written_push_routes_to_the_key_for_the_ack_window_then_to_the_pm(self):
        tid = self.todo()
        self.assertEqual(self.routes(), {tid: 'pm'}, 'never heard from')
        self.api.pushed('kit')
        self.assertEqual(self.routes(), {tid: 'kit'})
        self.assertEqual(self.api.route_change_in(), api.ACK_WINDOW_S)
        self.assertEqual(self.pending(**{'for': 'pm'}), [])
        self.t[0] += api.ACK_WINDOW_S - 1
        self.assertEqual(self.routes(), {tid: 'kit'})
        self.t[0] += 1
        self.assertEqual(self.routes(), {tid: 'pm'})
        self.assertIsNone(self.api.route_change_in())

    def test_a_push_lasts_past_a_shorter_grace(self):
        self.heard()
        tid = self.todo()
        self.api.pushed('kit')
        self.t[0] += api.REACH_GRACE_S + 1
        self.assertLess(api.REACH_GRACE_S + 1, api.ACK_WINDOW_S)
        self.assertEqual(self.routes(), {tid: 'kit'})

    def test_the_pm_and_an_empty_key_are_not_noted(self):
        self.api.pushed('pm')
        self.api.pushed('')
        self.api.pushed(None)
        self.assertEqual(self.api._pushed, {})


class ScheduleTest(ApiCase):
    """POST schedules (the owner's) and GET schedules (anybody's): docs/action-hub.md
    "schedules/<task>-<repo>"."""

    def setUp(self):
        ApiCase.setUp(self)
        self.store.put('repos', 'kit', {'name': 'Kit', 'aliases': ['old-kit']})
        # the machine config's launcher.repos as kitlib.autotasks.tick.launcher_repos gives it
        self.launcher = [('kit', 'C:\\checkouts\\Kit', 'Kit'), ('alpha', 'C:\\checkouts\\Alpha', 'Alpha')]
        self.seen = None
        self.state = {}         # the tick's records by schedule key, as tick.read_state gives them
        self.api = api.Api(self.store, clock=CLOCK, schedule_repos=lambda: list(self.launcher),
                           tick_seen=lambda: self.seen, tick_state=lambda: self.state)

    def body(self, **kw):
        b = {'task': 'maint-lint', 'repo': 'Kit', 'cadence': 'daily', 'at': '04:00'}
        b.update(kw)
        return {k: v for k, v in b.items() if v is not None}

    def test_the_owner_sets_a_schedule_and_both_roles_read_it(self):
        got = self.ok('POST', '/schedules', OWNER, self.body(), via='web')
        self.assertEqual((got['id'], got['version']), ('maint-lint-Kit', 1))
        d = self.store.get('schedules', 'maint-lint-Kit')
        self.assertEqual({k: d.get(k) for k in ('task', 'repo', 'cadence', 'at', 'days', 'set_at',
                                                'via')},
                         {'task': 'maint-lint', 'repo': 'Kit', 'cadence': 'daily', 'at': '04:00',
                          'days': None, 'set_at': NOW, 'via': 'web'})
        for role in (OWNER, AGENT):
            items = self.ok('GET', '/schedules', role)['items']
            self.assertEqual([(i['id'], i['cadence']) for i in items],
                             [('maint-lint-Kit', 'daily')])
        mine = self.ok('GET', '/schedules', AGENT, query={'repo': 'kit'})['items']
        self.assertEqual([i['id'] for i in mine], ['maint-lint-Kit'])
        self.assertEqual(self.ok('GET', '/schedules', AGENT, query={'repo': 'other'})['items'], [])

    def test_every_task_and_cadence_of_the_table(self):
        self.assertEqual(api.SCHEDULE_TASKS, ('maint-lint', 'worktree-sweep', 'agent-temp-sweep'))
        self.assertEqual(api.CADENCES, ('off', 'daily', 'weekly'))
        for task in api.SCHEDULE_TASKS:
            self.ok('POST', '/schedules', OWNER, self.body(task=task, cadence='off', at='00:00'))
        self.assertEqual(sorted(self.store.docs('schedules')),
                         ['agent-temp-sweep-Kit', 'maint-lint-Kit', 'worktree-sweep-Kit'])

    def test_weekly_days_are_stored_once_each_in_week_order(self):
        self.ok('POST', '/schedules', OWNER,
                self.body(cadence='weekly', days=['Sun', 'Mon', 'Sun', 'Wed'], at='23:59'))
        d = self.store.get('schedules', 'maint-lint-Kit')
        self.assertEqual((d['cadence'], d['days'], d['at']),
                         ('weekly', ['Mon', 'Wed', 'Sun'], '23:59'))

    def test_the_repo_is_stored_as_the_name_the_hub_knows(self):
        for given in ('kit', 'KIT', 'old-kit', ' Kit '):
            with self.subTest(repo=given):
                if self.store.get('schedules', 'maint-lint-Kit') is not None:
                    self.store.delete('schedules', 'maint-lint-Kit')
                got = self.ok('POST', '/schedules', OWNER, self.body(repo=given))
                self.assertEqual(got['id'], 'maint-lint-Kit')
                self.assertEqual(self.store.get('schedules', got['id'])['repo'], 'Kit')

    def test_a_session_key_is_stored_as_its_repository_name(self):
        known = lambda: ('Alpha', 'alpha-key')
        known.session_keys = lambda: {'Alpha': ['alpha-key']}
        self.store.known_repos = known
        got = self.ok('POST', '/schedules', OWNER, self.body(repo='alpha-key'))
        self.assertEqual(got['id'], 'maint-lint-Alpha')
        self.assertEqual(self.store.get('schedules', got['id'])['repo'], 'Alpha')

    def test_a_repo_the_store_knows_from_the_config_counts(self):
        self.store.known_repos = lambda: ('Alpha',)
        got = self.ok('POST', '/schedules', OWNER, self.body(repo='alpha'))
        self.assertEqual((got['id'], self.store.get('schedules', got['id'])['repo']),
                         ('maint-lint-Alpha', 'Alpha'))

    def test_each_field_is_judged_and_nothing_is_written(self):
        bad = [
            ({'task': 'format-disk'}, 'task'), ({'task': 3}, 'task'),
            ({'task': 'Maint-Lint'}, 'task'),
            ({'cadence': 'hourly'}, 'cadence'), ({'cadence': ['daily']}, 'cadence'),
            ({'at': '4:00'}, 'at'), ({'at': '24:00'}, 'at'), ({'at': '12:60'}, 'at'),
            ({'at': '04:00:00'}, 'at'), ({'at': '04:00\n'}, 'at'), ({'at': 400}, 'at'),
            ({'repo': 'nobody'}, 'repo'), ({'repo': 'all'}, 'repo'), ({'repo': 'pm'}, 'repo'),
            ({'repo': ['Kit']}, 'repo'),
            ({'cadence': 'weekly'}, 'days'), ({'cadence': 'weekly', 'days': []}, 'days'),
            ({'cadence': 'weekly', 'days': ['Funday']}, 'days'),
            ({'cadence': 'weekly', 'days': ['mon']}, 'days'),
            ({'cadence': 'weekly', 'days': 'Mon'}, 'days'),
            ({'cadence': 'weekly', 'days': [1]}, 'days'),
            ({'cadence': 'daily', 'days': ['Mon']}, 'days'),
            ({'cadence': 'off', 'days': ['Mon']}, 'days'),
            ({'if_version': -1}, 'if_version'), ({'if_version': '1'}, 'if_version'),
            ({'if_version': True}, 'if_version'),
            ({'id': 'x'}, 'id'), ({'via': 'tunnel'}, 'via'), ({'set_at': NOW}, 'set_at'),
            ({'enabled': True}, 'enabled'),
        ]
        for change, field in bad:
            with self.subTest(change=change):
                self.refused('invalid', field, 'POST', '/schedules', OWNER, self.body(**change))
        for missing in ('task', 'repo', 'cadence', 'at'):
            for empty in (None, '', ' '):
                with self.subTest(missing=missing, empty=empty):
                    self.refused('invalid', missing, 'POST', '/schedules', OWNER,
                                 self.body(**{missing: empty}))
        self.refused('invalid', 'body', 'POST', '/schedules', OWNER, ['task'])
        self.assertEqual(self.store.docs('schedules'), {})

    def test_a_repo_whose_name_cannot_be_an_id_is_refused(self):
        self.store.known_repos = lambda: ('my repo',)
        self.refused('invalid', 'repo', 'POST', '/schedules', OWNER, self.body(repo='my repo'))

    def test_setting_it_again_is_pinned_to_the_version_read(self):
        self.ok('POST', '/schedules', OWNER, self.body())
        # the refusal names the schedule there, so the page can open it
        for pin in (None, 2):
            got = self.refused('conflict', 'if_version', 'POST', '/schedules', OWNER,
                               self.body(cadence='off', if_version=pin), more=('id',))
            self.assertEqual(got['error']['id'], 'maint-lint-Kit')
        got = self.ok('POST', '/schedules', OWNER,
                      self.body(cadence='weekly', days=['Fri'], at='03:30', if_version=1))
        self.assertEqual(got['version'], 2)
        d = self.store.get('schedules', 'maint-lint-Kit')
        self.assertEqual((d['cadence'], d['days'], d['at']), ('weekly', ['Fri'], '03:30'))
        # back to daily: the put replaces the document, so the days go with it
        self.ok('POST', '/schedules', OWNER, self.body(if_version=2))
        self.assertNotIn('days', self.store.get('schedules', 'maint-lint-Kit'))
        # a pin on a schedule that is not there
        self.refused('conflict', 'if_version', 'POST', '/schedules', OWNER,
                     self.body(task='worktree-sweep', if_version=1), more=('id',))

    def test_an_agent_is_refused_the_write(self):
        self.refused('forbidden', 'role', 'POST', '/schedules', AGENT, self.body())
        self.refused('forbidden', 'role', 'POST', '/schedules', AGENT, self.body(),
                     agent_words=True)
        self.assertEqual(self.store.docs('schedules'), {})

    def test_a_repo_that_is_not_exactly_one_launcher_checkout_is_refused(self):
        # the hub knows Kit, but the tick could not run it: no checkout, or two (tick.find_repo)
        two = [('kit', 'C:\\checkouts\\Kit', 'Kit'), ('kit2', 'D:\\other\\Kit', 'Kit')]
        for launcher in ([], [('alpha', 'C:\\checkouts\\Alpha', 'Alpha')], two):
            with self.subTest(launcher=launcher):
                self.launcher = launcher
                for task in ('maint-lint', 'worktree-sweep'):
                    obj = self.refused('invalid', 'repo', 'POST', '/schedules', OWNER,
                                       self.body(task=task))
                    self.assertIn("launcher.repos", obj['error']['message'])
                    self.assertIn("'Kit'", obj['error']['message'])
        self.assertEqual(self.store.docs('schedules'), {})
        # exactly one, by its label, its name or its folder's name
        for launcher in ([('kit', 'C:\\checkouts\\Kit', 'Kit')], [('k', 'C:\\x\\y', 'kit')],
                         [('k', 'C:\\x\\KIT', 'other')]):
            with self.subTest(launcher=launcher):
                self.launcher = launcher
                if self.store.get('schedules', 'maint-lint-Kit') is not None:
                    self.store.delete('schedules', 'maint-lint-Kit')
                self.ok('POST', '/schedules', OWNER, self.body())

    def test_off_and_a_machine_wide_task_need_no_checkout(self):
        self.launcher = []
        self.ok('POST', '/schedules', OWNER, self.body(cadence='off'))
        self.ok('POST', '/schedules', OWNER, self.body(task='agent-temp-sweep'))
        self.assertEqual(sorted(self.store.docs('schedules')),
                         ['agent-temp-sweep-Kit', 'maint-lint-Kit'])

    def test_a_launcher_config_that_cannot_be_read_is_refused(self):
        from kitlib.config import ConfigError

        def broken():
            raise ConfigError('launcher.repos is not a table')
        self.api.schedule_repos = broken
        obj = self.refused('invalid', 'repo', 'POST', '/schedules', OWNER, self.body())
        self.assertIn('launcher.repos is not a table', obj['error']['message'])

    def test_a_launcher_config_that_cannot_be_opened_is_refused(self):
        # the tick takes an OSError as an unreadable list too (kitlib/autotasks/tick.py), so the
        # POST answers invalid on repo, not a 500
        def broken():
            raise PermissionError(13, 'Access is denied', 'config.json')
        self.api.schedule_repos = broken
        obj = self.refused('invalid', 'repo', 'POST', '/schedules', OWNER, self.body())
        self.assertIn('Access is denied', obj['error']['message'])
        self.assertEqual(self.store.docs('schedules'), {})

    def test_the_answer_says_the_first_run(self):
        # set at Monday 10:00 local: the first slot strictly after it (kitlib.autotasks.due)
        self.api.clock = lambda: '2026-01-05T10:00:00'
        cases = [
            ({'at': '11:00'}, '2026-01-05T11:00'),
            ({'at': '09:00'}, '2026-01-06T09:00'),
            ({'at': '10:00'}, '2026-01-06T10:00'),                    # not the minute it was set
            ({'cadence': 'weekly', 'days': ['Mon'], 'at': '10:00'}, '2026-01-12T10:00'),
            ({'cadence': 'weekly', 'days': ['Mon'], 'at': '10:01'}, '2026-01-05T10:01'),
            ({'cadence': 'weekly', 'days': ['Wed', 'Sun'], 'at': '08:00'}, '2026-01-07T08:00'),
            ({'cadence': 'weekly', 'days': ['Sun'], 'at': '23:59'}, '2026-01-11T23:59'),
            ({'cadence': 'off'}, None),
        ]
        for change, first in cases:
            with self.subTest(change=change):
                if self.store.get('schedules', 'maint-lint-Kit') is not None:
                    self.store.delete('schedules', 'maint-lint-Kit')
                got = self.ok('POST', '/schedules', OWNER, self.body(**change))
                self.assertEqual(got['first_run'], first)
        # an offset time is taken as local time first, as the tick does
        self.store.delete('schedules', 'maint-lint-Kit')
        self.api.clock = CLOCK
        from kitlib.autotasks import due
        set_at = due.local(NOW)
        got = self.ok('POST', '/schedules', OWNER, self.body(at='%02d:%02d' % (set_at.hour, set_at.minute)))
        self.assertEqual(got['first_run'], (set_at.replace(second=0) + datetime.timedelta(days=1))
                         .isoformat(timespec='minutes'))

    def test_get_says_when_the_tick_was_last_seen(self):
        now = datetime.datetime.fromisoformat(NOW)
        for seen, running in ((None, False),
                              ((now - datetime.timedelta(minutes=16)).isoformat(), True),
                              ((now - datetime.timedelta(seconds=api.TICK_STALE_S)).isoformat(), True),
                              ((now - datetime.timedelta(seconds=api.TICK_STALE_S + 1)).isoformat(), False),
                              ((now - datetime.timedelta(days=3)).isoformat(), False)):
            with self.subTest(seen=seen):
                self.seen = seen
                for role in (OWNER, AGENT):
                    got = self.ok('GET', '/schedules', role)
                    self.assertEqual(got['tick'], {'last': seen, 'running': running,
                                                   'every_s': 15 * 60})
        # the period is the one machine setup registers the tick with, not a copy of it; a few
        # missed ticks before it is said not to run
        from kitlib.machinesetup import extras
        self.assertEqual(api.tick_every_s(), extras.AUTO_EVERY_MINUTES * 60)
        with mock.patch.object(extras, 'AUTO_EVERY_MINUTES', 7):
            self.assertEqual(self.ok('GET', '/schedules', OWNER)['tick']['every_s'], 7 * 60)
        self.assertGreater(api.TICK_STALE_S, 2 * api.tick_every_s())

    def test_get_gives_each_schedule_its_last_run_and_outcome_from_the_tick(self):
        self.ok('POST', '/schedules', OWNER, self.body())
        self.ok('POST', '/schedules', OWNER, self.body(task='worktree-sweep'))
        self.ok('POST', '/schedules', OWNER, self.body(task='agent-temp-sweep'))
        self.state = {
            'maint-lint-Kit': {'at': '2026-01-04T04:00:09', 'what': 'started', 'slot': 'x'},
            'worktree-sweep-Kit': {'at': '2026-01-04T05:00:00', 'what': 'failed', 'why': 'boom'},
            'stray': {'at': '2026-01-04T05:00:00', 'what': 'started'}}
        got = {i['id']: i for i in self.ok('GET', '/schedules', AGENT)['items']}
        self.assertEqual((got['maint-lint-Kit']['last_run'], got['maint-lint-Kit']['last_outcome']),
                         ('2026-01-04T04:00:09', 'started'))
        # a failed record is no run: the outcome says why there is none
        self.assertEqual((got['worktree-sweep-Kit']['last_run'],
                          got['worktree-sweep-Kit']['last_outcome']), (None, 'failed'))
        self.assertEqual((got['agent-temp-sweep-Kit']['last_run'],
                          got['agent-temp-sweep-Kit']['last_outcome']), (None, None))
        # derived, never stored
        self.assertNotIn('last_run', self.store.get('schedules', 'maint-lint-Kit'))
        self.assertNotIn('next_due', self.store.get('schedules', 'maint-lint-Kit'))

    def test_get_gives_each_schedule_its_next_due_slot(self):
        self.api.clock = lambda: '2026-01-05T10:00:00'      # a Monday
        cases = [
            ({'at': '11:00'}, '2026-01-05T11:00'),
            ({'at': '09:00'}, '2026-01-06T09:00'),
            ({'at': '10:00'}, '2026-01-06T10:00'),           # strictly after now
            ({'cadence': 'weekly', 'days': ['Mon'], 'at': '10:00'}, '2026-01-12T10:00'),
            ({'cadence': 'weekly', 'days': ['Wed', 'Sun'], 'at': '08:00'}, '2026-01-07T08:00'),
            ({'cadence': 'weekly', 'days': ['Mon', 'Fri'], 'at': '10:01'}, '2026-01-05T10:01'),
            ({'cadence': 'off'}, None),
        ]
        for change, due_at in cases:
            with self.subTest(change=change):
                if self.store.get('schedules', 'maint-lint-Kit') is not None:
                    self.store.delete('schedules', 'maint-lint-Kit')
                self.ok('POST', '/schedules', OWNER, self.body(**change))
                item = self.ok('GET', '/schedules', OWNER)['items'][0]
                self.assertEqual(item['next_due'], due_at)
        # a stored document due cannot place has none, and the list still answers
        self.store.put('schedules', 'maint-lint-Kit', {'task': 'maint-lint', 'repo': 'Kit',
                                                       'cadence': 'daily', 'at': 'noon'})
        self.assertIsNone(self.ok('GET', '/schedules', OWNER)['items'][0]['next_due'])

    def test_the_default_tick_state_is_the_state_files_records(self):
        bare = api.Api(self.store)
        self.assertIs(bare.tick_state, api.tick_run_state)
        home = tempfile.mkdtemp(prefix='kit-hubapi-home-')
        self.addCleanup(shutil.rmtree, home, True)
        from kitlib.autotasks import tick
        with mock.patch.object(tick, 'state_dir', lambda home_=None: os.path.join(home, 'auto-tasks')):
            self.assertEqual(api.tick_run_state(), {})
            os.makedirs(os.path.join(home, 'auto-tasks'))
            path = os.path.join(home, 'auto-tasks', tick.STATE_NAME)
            with open(path, 'w') as f:
                f.write('not json')
            self.assertEqual(api.tick_run_state(), {})      # unreadable: no records, no error
            tick.write_state(path, {'maint-lint-Kit': {'at': 'T', 'what': 'started'}})
            self.assertEqual(api.tick_run_state(), {'maint-lint-Kit': {'at': 'T', 'what': 'started'}})

    def test_the_default_tick_seen_is_the_state_file_time(self):
        # serve.py passes neither seam: the hub server reads the tick's own files
        bare = api.Api(self.store)
        self.assertIs(bare.schedule_repos, api.launcher_checkouts)
        self.assertIs(bare.tick_seen, api.tick_last_seen)
        home =tempfile.mkdtemp(prefix='kit-hubapi-home-')
        self.addCleanup(shutil.rmtree, home, True)
        from kitlib.autotasks import tick
        with mock.patch.object(tick, 'state_dir', lambda home_=None: os.path.join(home, 'auto-tasks')):
            self.assertIsNone(api.tick_last_seen())
            os.makedirs(os.path.join(home, 'auto-tasks'))
            path = os.path.join(home, 'auto-tasks', tick.STATE_NAME)
            with open(path, 'w') as f:
                f.write('{}')
            os.utime(path, (1767323045, 1767323045))      # 2026-01-02T03:04:05Z
            self.assertEqual(api.tick_last_seen(), '2026-01-02T03:04:05+00:00')


# ------------------------------------------------------------ the maintenance lint's findings
FP, FP2 = '0123456789abcdef', 'fedcba9876543210'
# finding text a check could carry: markup, a script, Windows and relative paths, quotes
HOSTILE = '<script>alert("x")</script> <img src=x onerror=1> D:\\data\\..\\y ../../etc & \'q\''


def finding(**kw):
    out = {'repo': 'kit', 'check': 'stale-pull', 'target': 'KIT-A', 'text': 'pulled, no branch',
           'fix': 'release it', 'age': None, 'fingerprint': FP, 'new': True,
           'started': '2026-01-02T03:04:05Z'}
    out.update(kw)
    return out


class FindingViews(object):
    """The findings view as SiteViews gives it: the rows, or `fail` raised when set."""

    def __init__(self, rows):
        self.rows = rows
        self.fail = None

    def findings(self):
        if self.fail:
            raise self.fail
        return [dict(r) for r in self.rows]


class FindingsTest(ApiCase):
    def setUp(self):
        ApiCase.setUp(self)
        self.views = FindingViews([finding(), finding(fingerprint=FP2, repo='other',
                                                      target='OT-1', new=False)])
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def mark(self, status='dismissed', **kw):
        return self.ok('POST', '/finding_marks', OWNER, dict(kw, fingerprint=FP, status=status))

    def test_findings_lists_the_lint_rows_filtered_by_repo(self):
        obj = self.ok('GET', '/findings', AGENT)
        self.assertEqual(obj['items'], [finding(), finding(fingerprint=FP2, repo='other',
                                                           target='OT-1', new=False)])
        obj = self.ok('GET', '/findings', OWNER, query={'repo': 'OTHER'})
        self.assertEqual([f['fingerprint'] for f in obj['items']], [FP2])
        self.assertNotIn('findings', api.VIEWS)     # never part of the snapshot

    def test_a_marked_finding_is_hidden_until_its_mark_is_deleted(self):
        for status in ('dismissed', 'passed_on', 'done'):
            with self.subTest(status=status):
                obj = self.mark(status, note='seen')
                self.assertEqual(obj['id'], FP)
                ids = [f['fingerprint'] for f in self.ok('GET', '/findings', OWNER)['items']]
                self.assertEqual(ids, [FP2])
                marks = self.ok('GET', '/finding_marks', AGENT)['items']
                self.assertEqual(len(marks), 1)
                m = marks[0]
                self.assertEqual({k: m[k] for k in ('id', 'status', 'repo', 'check', 'target',
                                                    'text', 'note', 'marked_at', 'via')},
                                 {'id': FP, 'status': status, 'repo': 'kit',
                                  'check': 'stale-pull', 'target': 'KIT-A',
                                  'text': 'pulled, no branch', 'note': 'seen', 'marked_at': NOW,
                                  'via': 'api'})
                self.assertEqual(self.ok('GET', '/finding_marks', OWNER,
                                         query={'status': status})['items'][0]['id'], FP)
                self.ok('POST', '/finding_marks/%s/delete' % FP, OWNER, {'if_version': 1})
                ids = [f['fingerprint'] for f in self.ok('GET', '/findings', OWNER)['items']]
                self.assertEqual(ids, [FP, FP2])
                self.assertEqual(self.store.docs('finding_marks'), {})

    def test_marking_again_is_pinned_to_the_version_read(self):
        self.mark()
        self.refused('conflict', 'if_version', 'POST', '/finding_marks', OWNER,
                     {'fingerprint': FP, 'status': 'done'})
        self.refused('conflict', 'if_version', 'POST', '/finding_marks', OWNER,
                     {'fingerprint': FP, 'status': 'done', 'if_version': 7})
        obj = self.mark('done', if_version=1)
        self.assertEqual(obj['version'], 2)
        self.assertEqual(self.store.get('finding_marks', FP)['status'], 'done')
        self.refused('conflict', 'if_version', 'POST', '/finding_marks', OWNER,
                     {'fingerprint': FP2, 'status': 'done', 'if_version': 1})
        self.refused('conflict', 'if_version', 'POST', '/finding_marks/%s/delete' % FP, OWNER,
                     {'if_version': 1})

    def test_a_mark_names_a_finding_the_lint_shows(self):
        self.refused('not_found', 'fingerprint', 'POST', '/finding_marks', OWNER,
                     {'fingerprint': 'a' * 16, 'status': 'done'})
        for bad in ('ABCDEF0123456789', 'a' * 15, 'a' * 17, '../../x', 'con', 7, None):
            with self.subTest(fingerprint=bad):
                self.refused('invalid', 'fingerprint', 'POST', '/finding_marks', OWNER,
                             {'fingerprint': bad, 'status': 'done'})
        for body, field in (({'fingerprint': FP, 'status': 'fixed'}, 'status'),
                            ({'fingerprint': FP}, 'status'),
                            ({'fingerprint': FP, 'status': 'done', 'note': 3}, 'note'),
                            ({'fingerprint': FP, 'status': 'done', 'repo': 'x'}, 'repo'),
                            ({'fingerprint': FP, 'status': 'done', 'text': 'x'}, 'text')):
            with self.subTest(body=body):
                self.refused('invalid', field, 'POST', '/finding_marks', OWNER, body)
        self.refused('not_found', 'id', 'POST', '/finding_marks/%s/delete' % FP, OWNER)
        self.assertEqual(self.store.docs('finding_marks'), {})

    def test_a_failing_or_missing_view_is_unavailable_and_names_nothing(self):
        self.views.fail = OSError('C:\\secret\\maint-lint\\kit\\result.json')
        for method, path, body in (('GET', '/findings', None),
                                   ('POST', '/finding_marks', {'fingerprint': FP,
                                                               'status': 'done'}),
                                   ('POST', '/to_pm', {'kind': 'act', 'finding': FP})):
            with self.subTest(path=path):
                obj = self.refused('unavailable', 'views', method, path, OWNER, body)
                self.assertNotIn('secret', obj['error']['message'])
        self.api = api.Api(self.store, views=None, clock=CLOCK)
        self.refused('unavailable', 'views', 'GET', '/findings', AGENT)

    def test_an_act_note_to_the_pm_is_prefilled_from_the_finding(self):
        obj = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})
        d = self.store.get('to_pm', obj['id'])
        self.assertEqual((d['kind'], d['status'], d['repo'], d['finding'], d['refs']),
                         ('act', 'queued', 'kit', FP, [FP, 'KIT-A']))
        for part in ('stale-pull', 'KIT-A', 'pulled, no branch', 'release it'):
            self.assertIn(part, d['text'])
        self.assertEqual(self.store.get('finding_marks', FP), None)    # the page marks it
        # what the owner gives is kept, the finding's refs first and each once
        obj = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP2, 'repo': 'pm',
                                                'text': 'look at this', 'urgent': True,
                                                'refs': ['Q-3', FP2, 'OT-1']})
        d = self.store.get('to_pm', obj['id'])
        self.assertEqual((d['repo'], d['text'], d['urgent'], d['refs']),
                         ('pm', 'look at this', True, [FP2, 'OT-1', 'Q-3']))
        # a marked finding can still be passed on
        self.mark('passed_on')
        self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})

    def test_an_act_note_needs_a_finding_the_lint_shows(self):
        self.refused('invalid', 'finding', 'POST', '/to_pm', OWNER, {'kind': 'act'})
        self.refused('not_found', 'finding', 'POST', '/to_pm', OWNER,
                     {'kind': 'act', 'finding': 'a' * 16})
        self.refused('invalid', 'finding', 'POST', '/to_pm', OWNER,
                     {'kind': 'act', 'finding': '../x'})
        for kind in ('question', 'fyi'):
            with self.subTest(kind=kind):
                self.refused('invalid', 'finding', 'POST', '/to_pm', OWNER,
                             {'kind': kind, 'text': 't', 'finding': FP})
                self.refused('invalid', 'text', 'POST', '/to_pm', OWNER, {'kind': kind})
        self.refused('invalid', 'refs', 'POST', '/to_pm', OWNER,
                     {'kind': 'act', 'finding': FP, 'refs': ['Q-1', 3]})
        self.refused('invalid', 'refs', 'POST', '/to_pm', OWNER,
                     {'kind': 'act', 'finding': FP, 'refs': ['  ']})
        self.refused('invalid', 'kind', 'POST', '/to_pm', OWNER, {'kind': 'acts', 'text': 't'})
        # a question still takes refs
        obj = self.ok('POST', '/to_pm', OWNER, {'text': 't', 'refs': ['Q-1']})
        self.assertEqual(self.store.get('to_pm', obj['id'])['refs'], ['Q-1'])
        self.assertEqual(self.store.get('to_pm', obj['id'])['kind'], 'question')

    def test_hostile_finding_text_round_trips_as_data(self):
        row = finding(text=HOSTILE, target='..\\..\\Windows\\x', fix='<b>%s</b>' % HOSTILE,
                      check='<i>c</i>', age='C:\\x')
        self.views.rows = [row]
        obj = self.ok('GET', '/findings', OWNER)        # the local owner: the row as it is
        self.assertEqual(json.loads(json.dumps(obj))['items'], [row])
        # anyone else: the same row, the fix's absolute path cut (test_a_path_is_the_local...)
        obj = self.ok('GET', '/findings', AGENT)
        self.assertEqual(json.loads(json.dumps(obj))['items'],
                         [dict(row, fix=row['fix'].replace('D:\\data\\..\\y', '.../y'))])
        self.mark(note=HOSTILE)
        m = self.store.get('finding_marks', FP)
        self.assertEqual((m['text'], m['target'], m['check'], m['note']),
                         (HOSTILE, '..\\..\\Windows\\x', '<i>c</i>', HOSTILE))
        obj = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})
        d = self.store.get('to_pm', obj['id'])
        self.assertIn(HOSTILE, d['text'])
        self.assertEqual(d['refs'], [FP, '..\\..\\Windows\\x'])
        self.assertEqual([f['text'] for f in self.ok('GET', '/to_pm', OWNER)['items']],
                         [d['text']])
        self.assertEqual([f['text'] for f in self.ok('GET', '/to_pm', AGENT)['items']],
                         [d['text'].replace('D:\\data\\..\\y', '.../y')])

    def test_a_path_is_the_local_owners_to_see(self):
        wt = 'Q:\\work\\Repo\\.claude\\worktrees\\my-slug'
        fix = 'python "Q:/kit plugin/tools/x.py" -C Q:/work/Repo drop; see \\\\share\\x\\y'
        self.views.rows = [finding(target=wt, fix=fix, text='worktree my-slug is gone')]
        cut = ('.../my-slug', 'python .../x.py -C .../Repo drop; see .../y')
        local = {}
        remote = ({'role': AGENT}, {'role': OWNER, 'via': 'tunnel', 'agent_words': True},
                  {'role': OWNER, 'agent_words': True}, {'role': OWNER, 'via': 'tunnel'})

        def get(path, role=OWNER, **kw):
            return self.ok('GET', path, role, **kw)['items']

        f = get('/findings', **local)[0]
        self.assertEqual((f['target'], f['fix']), (wt, fix))
        for kw in remote:
            with self.subTest(caller=kw):
                f = get('/findings', **kw)[0]
                self.assertEqual((f['target'], f['fix']), cut)
                self.assertNotIn('Q:', json.dumps(f))
        # a mark keeps the target; only the local owner reads it whole
        self.mark()
        self.assertEqual(get('/finding_marks')[0]['target'], wt)
        for kw in remote:
            with self.subTest(caller=kw):
                self.assertEqual(get('/finding_marks', **kw)[0]['target'], cut[0])
        # an act note: the caller's view in its text, and never a path in its refs
        obj = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})
        d = self.store.get('to_pm', obj['id'])
        self.assertEqual(d['refs'], [FP, '.../my-slug'])
        self.assertIn(wt, d['text'])
        obj = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP}, via='tunnel',
                      agent_words=True)
        d = self.store.get('to_pm', obj['id'])
        self.assertEqual(d['refs'], [FP, '.../my-slug'])
        self.assertNotIn('Q:', d['text'])
        self.assertNotIn('\\\\share', d['text'])
        self.assertIn(cut[1], d['text'])

    # every caller but the local owner (Request.full), as test_a_path_is_the_local_owners_to_see
    REMOTE = ({'role': AGENT}, {'role': OWNER, 'via': 'tunnel', 'agent_words': True},
              {'role': OWNER, 'agent_words': True}, {'role': OWNER, 'via': 'tunnel'})

    def seen_fix(self, fix, **kw):
        self.views.rows = [finding(fix=fix)]
        return self.ok('GET', '/findings', kw.pop('role', OWNER), **kw)['items'][0]['fix']

    def test_a_forward_slash_share_is_cut(self):
        fix = 'copy it to //nas/share/Users/jdoe/out.txt; see https://example.com/a/b'
        for kw in self.REMOTE:
            with self.subTest(caller=kw):
                self.assertEqual(self.seen_fix(fix, **kw),
                                 'copy it to .../out.txt; see https://example.com/a/b')

    def test_a_bare_path_with_a_space_is_cut_whole(self):
        fix = 'run Q:\\home\\Jane Doe\\repo\\x.py -C Z:/w/Repo then D:\\a\\b ../rel/c'
        for kw in self.REMOTE:
            with self.subTest(caller=kw):
                self.assertEqual(self.seen_fix(fix, **kw),
                                 'run .../x.py -C .../Repo then .../b ../rel/c')

    def test_a_doubled_backslash_with_no_share_is_not_cut(self):
        fix = 'split on \\\\ or \\\\n, not \\\\host alone; Q:\\\\home\\\\a\\\\b.py is a path'
        for kw in self.REMOTE:
            with self.subTest(caller=kw):
                self.assertEqual(self.seen_fix(fix, **kw),
                                 'split on \\\\ or \\\\n, not \\\\host alone; .../b.py is a path')

    def test_get_to_pm_cuts_an_act_notes_paths_for_all_but_the_local_owner(self):
        wt = 'Q:\\work\\Repo\\.claude\\worktrees\\my-slug'
        self.views.rows = [finding(target=wt, fix='python Q:/kit/tools/x.py')]
        act = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP,
                                                'refs': ['Q:\\work\\Repo\\x']})['id']
        note = self.ok('POST', '/to_pm', OWNER, {'text': 'a question'})['id']
        made = self.store.get('to_pm', act)
        self.assertIn(wt, made['text'])                     # made locally: the paths whole
        self.mark()
        for kw in self.REMOTE:
            with self.subTest(caller=kw):
                kw = dict(kw)
                role = kw.pop('role')
                got = {d['id']: d for d in self.ok('GET', '/to_pm', role, **kw)['items']}
                cols = self.ok('GET', '/snapshot', role, **kw)['collections']
                self.assertEqual(cols['to_pm'][act],
                                 {k: v for k, v in got[act].items() if k != 'id'})
                self.assertEqual(cols['finding_marks'][FP]['target'], '.../my-slug')
                self.assertNotIn('Q:', json.dumps(got[act]))
                self.assertNotIn('Q:', json.dumps(cols))
                self.assertIn('.../my-slug', got[act]['text'])
                self.assertIn('.../x.py', got[act]['text'])
                self.assertEqual(got[act]['refs'], [FP, '.../my-slug', '.../x'])
                self.assertEqual(got[note]['text'], 'a question')
        cols = self.ok('GET', '/snapshot', OWNER)['collections']   # the local owner: whole
        self.assertEqual(cols['to_pm'][act], made)
        self.assertEqual(cols['finding_marks'][FP]['target'], wt)

    def test_a_sent_act_notes_pending_title_is_cut_for_all_but_the_local_owner(self):
        wt = 'Q:\\work\\Repo\\.claude\\worktrees\\my-slug'
        self.views.rows = [finding(target=wt, fix='python Q:/kit/tools/x.py')]
        act = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})['id']
        note = self.ok('POST', '/to_pm', OWNER, {'text': 'see Q:\\mine\\a.txt'})['id']
        self.assertEqual(self.ok('POST', '/to_pm/send', OWNER)['sent'], [act, note])
        made = self.store.get('to_pm', act)
        self.assertIn(wt, made['text'])                     # stored whole
        whole = made['text'].splitlines()[0]

        def titles(items):
            return {d['id']: d['title'] for d in items if d['kind'] == 'to_pm'}
        for kw in self.REMOTE:
            with self.subTest(caller=kw):
                kw = dict(kw)
                role = kw.pop('role')
                for got in (titles(self.ok('GET', '/pending', role, **kw)['items']),
                            titles(self.ok('GET', '/snapshot', role, **kw)['pending']),
                            titles(self.ok('GET', '/wait', role, query=self.WAIT,
                                           **kw)['pending'])):
                    self.assertNotIn('Q:', got[act])
                    self.assertIn('.../my-slug', got[act])
                    self.assertIn('.../x.py', got[act])
                    self.assertEqual(got[note], 'see Q:\\mine\\a.txt')   # the owner's words
        self.assertEqual(self.store.get('to_pm', act), made)   # nothing stored was cut
        for got in (titles(self.ok('GET', '/pending', OWNER)['items']),
                    titles(self.ok('GET', '/snapshot', OWNER)['pending']),
                    titles(self.ok('GET', '/wait', OWNER, query=self.WAIT)['pending'])):
            self.assertEqual(got[act], whole)               # the local owner: whole

    WAIT = {'pending': '1', 'timeout': '0'}

    def test_an_act_note_deleted_mid_request_keeps_its_title_cut(self):
        # the kind is read with the pending item: a note gone by the time its title is cut
        # (a re-read would find None) is still cut, and no item carries the kind out
        wt = 'Q:\\work\\Repo\\.claude\\worktrees\\my-slug'
        self.views.rows = [finding(target=wt, fix='python Q:/kit/tools/x.py')]
        act = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})['id']
        self.ok('POST', '/to_pm/send', OWNER)
        real = self.store.get

        def gone(collection, doc_id, *a, **k):
            return None if (collection, doc_id) == ('to_pm', act) else real(
                collection, doc_id, *a, **k)
        with mock.patch.object(self.store, 'get', side_effect=gone):
            for kw in self.REMOTE:
                with self.subTest(caller=kw):
                    kw = dict(kw)
                    role = kw.pop('role')
                    for items in (self.ok('GET', '/pending', role, **kw)['items'],
                                  self.ok('GET', '/snapshot', role, **kw)['pending'],
                                  self.ok('GET', '/wait', role, query=self.WAIT,
                                          **kw)['pending']):
                        got = {d['id']: d for d in items}[act]
                        self.assertNotIn('Q:', got['title'])
                        self.assertIn('.../my-slug', got['title'])
                        self.assertNotIn('note', got)
            for items in (self.ok('GET', '/pending', OWNER)['items'],
                          self.ok('GET', '/wait', OWNER, query=self.WAIT)['pending']):
                self.assertNotIn('note', {d['id']: d for d in items}[act])

    def test_a_json_escaped_share_is_cut(self):
        # a share path inside JSON text: each backslash doubled (\\\\nas\\share\\...)
        fix = ('see {"p": "\\\\\\\\nas\\\\share\\\\jdoe\\\\out.txt"}, \\\\\\\\nas\\\\s\\\\a.log'
               ' and \\\\\\\\')
        for kw in self.REMOTE:
            with self.subTest(caller=kw):
                self.assertEqual(self.seen_fix(fix, **kw),
                                 'see {"p": .../out.txt}, .../a.log and \\\\\\\\')

    def runs(self, rows, **kw):
        self.views.lint_runs = lambda: rows
        return self.ok('GET', '/findings', OWNER, **kw)['runs']

    def test_findings_name_a_skipped_run_only_when_the_marker_is_newer(self):
        self.assertEqual(self.ok('GET', '/findings', AGENT)['runs'], [])     # no view
        skip = {'reason': 'busy', 'at': '2026-01-09T03:00:00Z', 'code': 3}
        run = {'repo': 'kit', 'started': '2026-01-02T03:04:05Z', 'ledger': None, 'skip': skip}
        self.assertEqual(self.runs([run]), [{'repo': 'kit', 'started': '2026-01-02T03:04:05Z',
                                             'ledger': None, 'skipped': {
                                                 'reason': 'busy', 'at': skip['at']}}])
        self.assertEqual(self.runs([run], query={'repo': 'OTHER'}), [])
        self.assertEqual(len(self.runs([run], query={'repo': 'KIT'})), 1)
        for bad in (dict(skip, at='2026-01-01T00:00:00Z'),      # a result newer: no skip
                    dict(skip, at=run['started']),
                    dict(skip, at='last tuesday'), dict(skip, at=None),
                    dict(skip, at='2026-01-09T03:00:00'),      # no offset
                    dict(skip, reason=3), 'busy', None):
            with self.subTest(skip=bad):
                self.assertIsNone(self.runs([dict(run, skip=bad)])[0]['skipped'])
        for started, shown in (('?', '?'), (None, None), (5, None)):
            with self.subTest(started=started):
                got = self.runs([dict(run, started=started)])[0]
                self.assertEqual((got['started'], got['skipped']), (shown, None))
        for bad in (dict(run, repo=None), 'kit', None):
            with self.subTest(row=bad):
                self.assertEqual(self.runs([bad]), [])
        self.assertEqual(self.runs('not a list'), [])

        def broken():
            raise OSError('C:\\secret\\skipped.json')
        self.views.lint_runs = broken
        obj = self.ok('GET', '/findings', OWNER)
        self.assertEqual((obj['runs'], len(obj['items'])), ([], 2))

    def test_findings_give_the_ledger_as_whole_counts_only(self):
        led = {'stale_cites': 2, 'conflict_pairs': 0, 'overrides': True, 'superseded': 4,
               'extra': 5, 'notes': ['Q:\\home\\x']}
        got = self.runs([{'repo': 'kit', 'started': None, 'ledger': led, 'skip': None}])
        self.assertEqual(got[0]['ledger'], {'stale_cites': 2, 'conflict_pairs': 0})
        self.assertIsNone(self.runs([{'repo': 'kit', 'ledger': [1]}])[0]['ledger'])

    def test_the_local_owner_sees_every_path_whole(self):
        wt = 'Q:\\home\\Jane Doe\\repo'
        fix = ('copy to //nas/share/jdoe/out.txt; run Q:\\home\\Jane Doe\\repo\\x.py; '
               'split on \\\\')
        self.views.rows = [finding(target=wt, fix=fix)]
        self.assertEqual(self.ok('GET', '/findings', OWNER)['items'], [finding(target=wt,
                                                                                fix=fix)])
        self.mark()
        self.assertEqual(self.ok('GET', '/finding_marks', OWNER)['items'][0]['target'], wt)
        act = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP,
                                                'refs': ['Q:\\home\\Jane Doe\\x']})['id']
        made = self.store.get('to_pm', act)
        got = self.ok('GET', '/to_pm', OWNER)['items']
        self.assertEqual([(d['text'], d['refs']) for d in got], [(made['text'], made['refs'])])
        self.assertIn(fix, made['text'])
        self.assertIn('Q:\\home\\Jane Doe\\x', made['refs'])

    def test_an_edit_never_makes_or_unmakes_an_act_note(self):
        act = self.ok('POST', '/to_pm', OWNER, {'kind': 'act', 'finding': FP})['id']
        note = self.ok('POST', '/to_pm', OWNER, {'kind': 'question', 'text': 't'})['id']
        for pid, kind in ((act, 'question'), (act, 'fyi'), (note, 'act')):
            with self.subTest(id=pid, kind=kind):
                self.refused('invalid', 'kind', 'POST', '/to_pm/%s/edit' % pid, OWNER,
                             {'kind': kind})
        self.assertEqual(self.store.get('to_pm', act)['kind'], 'act')
        self.assertEqual(self.store.get('to_pm', note)['kind'], 'question')
        # the same kind, or any other change, is still an edit
        self.ok('POST', '/to_pm/%s/edit' % act, OWNER, {'kind': 'act', 'text': 'reworded'})
        self.assertEqual(self.store.get('to_pm', act)['text'], 'reworded')
        self.ok('POST', '/to_pm/%s/edit' % note, OWNER, {'kind': 'fyi'})
        self.assertEqual(self.store.get('to_pm', note)['kind'], 'fyi')


class FindingsSourceTest(unittest.TestCase):
    """kitlib.hubsite.derived.findings over a folder of maint-lint result.json files, and
    SiteViews serving it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-findings-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def result(self, repo, rows, started='2026-01-02T03:04:05Z', folder=None):
        lint_result.write(os.path.join(self.root, folder or repo, lint_result.NAME),
                          {'version': 1, 'repo': repo, 'started': started, 'findings': rows})

    def row(self, **kw):
        out = finding(**kw)
        out.pop('started')
        return out

    def test_every_repos_rows_with_the_run_they_came_from(self):
        self.result('kit', [self.row(target='B'), self.row(target='A', fingerprint=FP2)])
        self.result('app', [self.row(repo='app', fingerprint='1' * 16)], started='S')
        got = derived.findings(self.root)
        self.assertEqual([(f['repo'], f['target'], f['fingerprint']) for f in got],
                         [('app', 'KIT-A', '1' * 16), ('kit', 'A', FP2), ('kit', 'B', FP)])
        self.assertEqual(got[0]['started'], 'S')
        self.assertEqual(sorted(got[1]), sorted(finding()))

    def test_what_cannot_be_a_finding_is_skipped_and_odd_values_are_data(self):
        self.result('kit', [self.row(), self.row(fingerprint='../x'), 'not a row',
                            self.row(fingerprint=None),
                            dict(self.row(fingerprint=FP2), secret='C:\\x', text=['a'],
                                 new='yes', repo=None)])
        os.makedirs(os.path.join(self.root, 'broken'))
        with open(os.path.join(self.root, 'broken', lint_result.NAME), 'w') as fh:
            fh.write('{not json')
        os.makedirs(os.path.join(self.root, 'empty'))
        with open(os.path.join(self.root, 'stray.json'), 'w') as fh:
            fh.write('{}')
        got = derived.findings(self.root)
        self.assertEqual([f['fingerprint'] for f in got], [FP, FP2])
        odd = got[1]
        self.assertNotIn('secret', odd)
        self.assertEqual((odd['text'], odd['new'], odd['repo']), (None, False, 'kit'))

    def test_no_folder_is_no_findings(self):
        self.assertEqual(derived.findings(None), [])
        self.assertEqual(derived.findings(os.path.join(self.root, 'missing')), [])

    def test_site_views_serve_the_folder_and_the_api_hides_a_marked_one(self):
        self.result('kit', [self.row(), self.row(fingerprint=FP2)])
        store = Store.open(os.path.join(self.root, 'store'), clock=CLOCK)
        self.addCleanup(store.close)
        views = serve.SiteViews(store, serve.Sources(findings=self.root))
        self.assertEqual(views.findings(), derived.findings(self.root))
        a = api.Api(store, views=views, clock=CLOCK)
        status, obj = a.call('POST', BASE + '/finding_marks', OWNER,
                             {'fingerprint': FP, 'status': 'dismissed'})
        self.assertEqual(status, 200, obj)
        status, obj = a.call('GET', BASE + '/findings', AGENT)
        self.assertEqual([f['fingerprint'] for f in obj['items']], [FP2])
        # a server with no findings folder (no pm_data) shows none
        self.assertEqual(serve.SiteViews(store, serve.Sources()).findings(), [])

    def test_site_views_show_each_lint_run_in_the_findings_runs(self):
        """KIT-HUB-FINDINGS-LINT-RUNS: the lint folder's runs reach GET findings' runs."""
        self.result('kit', [self.row()])
        from kitlib.maintlint import slot
        slot.write_skip(os.path.join(self.root, 'kit', lint_result.NAME),
                        slot.Skipped('busy', slot.EXIT_BUSY, 'busy'),
                        now=datetime.datetime(2026, 1, 9, 3, 0, tzinfo=datetime.timezone.utc))
        store = Store.open(os.path.join(self.root, 'store'), clock=CLOCK)
        self.addCleanup(store.close)
        a = api.Api(store, views=serve.SiteViews(store, serve.Sources(findings=self.root)),
                    clock=CLOCK)
        status, obj = a.call('GET', BASE + '/findings', AGENT)
        self.assertEqual(status, 200, obj)
        self.assertEqual(obj['runs'], [{'repo': 'kit', 'started': '2026-01-02T03:04:05Z',
                                        'ledger': None, 'skipped': {
                                            'reason': 'busy', 'at': '2026-01-09T03:00:00Z'}}])
        bare = api.Api(store, views=serve.SiteViews(store, serve.Sources()), clock=CLOCK)
        self.assertEqual(bare.call('GET', BASE + '/findings', AGENT)[1]['runs'], [])

    def test_the_machine_sources_read_the_pm_data_lint_folder(self):
        home = os.path.join(self.root, 'home')
        os.makedirs(home)
        pm = os.path.join(self.root, 'pm')
        pm_repo(self, record=False)          # the hub's token folder needs the PM-repo pointer
        with mock.patch.object(serve.pmdata, 'machine_settings', lambda h: {'path': pm}):
            src = serve.machine_sources(home, sync=False)
        self.assertEqual(src.findings, os.path.join(pm, lint_result.FOLDER))
        with mock.patch.object(serve.pmdata, 'machine_settings', lambda h: None):
            self.assertIsNone(serve.machine_sources(home, sync=False).findings)


class UnblockStoreTest(trw.Case):
    """recordwrite.unblock and SiteViews' record_unblock on a real ref store."""

    def test_a_record_with_one_blocker_opens(self):
        self.named()
        self.seed(**rec('FOO', state='blocked', blocked_on='item:BAR'), **rec('BAR'))
        out = recordwrite.unblock(self.repo.root, 'FOO')
        self.assertIn('UNBLOCKED FOO - state: open, blocked_on: -', out['lines'])
        self.assertEqual(self.field('FOO', 'state'), ['open'])
        log = self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)
        self.assertIn('Session: owner-hub', log)

    def test_the_store_refusals_are_hub_errors_with_no_path(self):
        self.named()
        self.seed(**rec('FOO', state='blocked', blocked_on=['item:BAR', 'item:BAZ']),
                  **rec('BAR'), **rec('BAZ'), **rec('OPEN'),
                  **rec('OWN', state='blocked', blocked_on='owner', owner_ask='which one?'))
        e = self.refused(lambda: recordwrite.unblock(self.repo.root, 'FOO'), 'invalid')
        self.assertEqual(e.field, 'on')
        self.assertNotIn(':\\', e.message)
        self.assertNotIn('backlog', e.message)
        e = self.refused(lambda: recordwrite.unblock(self.repo.root, 'FOO', on='item:BAR'),
                         'invalid')
        self.assertIn('reason', e.message)
        out = recordwrite.unblock(self.repo.root, 'FOO', on='item:BAR',
                                  reason='BAR landed on main')
        self.assertEqual(self.field('FOO', 'blocked_on'), ['item:BAZ'])
        self.assertTrue(out['lines'])
        e = self.refused(lambda: recordwrite.unblock(self.repo.root, 'OPEN'), 'invalid')
        self.assertIn('nothing to unblock', e.message)
        # an OWNER route: the unblock-owner row is the owner's and the role check ran, so the
        # owner's own hold clears with no ruling
        recordwrite.unblock(self.repo.root, 'OWN')
        self.assertEqual(self.field('OWN', 'state'), ['open'])
        self.refused(lambda: recordwrite.unblock(self.repo.root, 'NOPE'), 'not_found')

    def test_site_views_unblock_then_read_the_repository_again(self):
        self.named()
        self.seed(**rec('FOO', state='blocked', blocked_on='item:BAR'), **rec('BAR'))
        root, reread = self.repo.root, []

        class Backlog(object):
            def _named(self, repo):
                return 'r', root

            def reread(self, name, at):
                reread.append((name, at))

            def record(self, repo, rid):
                return {'repo': repo, 'id': rid}

        views = serve.SiteViews(None, serve.Sources(), backlog_views=Backlog())
        out = views.record_unblock('r', 'FOO', None, None)
        self.assertEqual(out['record'], {'repo': 'r', 'id': 'FOO'})
        self.assertEqual(reread, [('r', root)])
        self.assertEqual(self.field('FOO', 'state'), ['open'])


class Crash(BaseException):
    """The process dies here: no Exception, so nothing in the hub catches it."""


class FileViews(BindViews):
    """BindViews with the record write a bound answer is filed by. Each call is kept in
    `filed`; `lines` is what the record holds, {(record id, qid): at}, kept as plan_answer keeps
    it (an older `at` changes nothing). `fail`: the HubError each call raises before it writes;
    `crash`: raise Crash after the write; `during`: run once inside the next call, before its
    write (a second caller racing it)."""

    def __init__(self):
        BindViews.__init__(self)
        self.filed, self.lines = [], {}
        self.fail, self.crash, self.during = None, False, None

    def record_answer(self, repo, id, qid, at, title, label, note, via, other, waiting):
        self.filed.append((repo, id, qid, at, title, label, note, via, other, waiting))
        if self.fail is not None:
            raise self.fail
        during, self.during = self.during, None
        if during is not None:
            during()
        held = self.lines.get((id, qid))
        if held is None or held < at:
            self.lines[(id, qid)] = at
        if self.crash:
            raise Crash()
        return {'lines': ['ANSWERED %s on %s (%s)' % (id, qid, at)]}


class FilingTest(ApiCase):
    """An answer to a question bound to a record is filed into the record (record_answer) and
    the question marked `filed`; a failure leaves it `answered`, with `filing`, for the PM; a
    crash anywhere is mended by POST questions/file (docs/action-hub.md "A question bound to a
    record")."""

    def setUp(self):
        ApiCase.setUp(self)
        self.views = FileViews()
        self.times = iter('2026-01-02T03:04:%02d+00:00' % s for s in range(5, 60))
        self.now = next(self.times)
        self.api = api.Api(self.store, views=self.views, clock=lambda: self.now)

    def tick(self):
        self.now = next(self.times)

    def doc(self, qid):
        return self.store.get('questions', qid)

    def bound(self, rid='EX-1'):
        return self.question(record={'repo': 'kit', 'id': rid})

    def answer(self, qid, choice=0, **kw):
        body = dict({'choice': choice}, **kw)
        return self.ok('POST', '/questions/%s/answer' % qid, OWNER, body)

    def refile(self, body=None):
        return self.ok('POST', '/questions/file', AGENT, body if body is not None else {})

    def pending(self):
        return [p['id'] for p in self.ok('GET', '/pending', AGENT)['items']]

    def filed_pending(self):
        """The pending questions that say they are filed (the asker still reads them)."""
        return [p['id'] for p in self.ok('GET', '/pending', AGENT)['items'] if p.get('filed')]

    def test_a_crash_after_the_answer_is_stored_is_filed_by_the_retry(self):
        qid = self.bound()
        with mock.patch.object(api, 'file_answer', side_effect=Crash):
            with self.assertRaises(Crash):
                self.api.call('POST', BASE + '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.assertEqual(self.doc(qid)['status'], 'answered')
        self.assertEqual(self.views.filed, [])
        self.assertEqual(self.pending(), [qid])
        got = self.refile()
        self.assertEqual((got['filed'], got['failed']), ([qid], []))
        self.assertEqual(self.views.lines, {('EX-1', qid): self.doc(qid)['answer']['at']})
        self.assertEqual(self.doc(qid)['status'], 'filed')
        self.assertEqual(self.filed_pending(), [qid], 'pending for its asker, as filed')
        self.assertEqual(self.refile()['filed'], [], 'a filed question is not filed again')

    def test_an_answer_is_filed_at_once_and_stays_pending_as_filed(self):
        other = self.bound()
        qid = self.bound()
        self.tick()
        got = self.answer(qid, note='  why  ')
        d = self.doc(qid)
        self.assertEqual((got['status'], d['status'], d['filed_at']), ('filed', 'filed', self.now))
        self.assertIsNone(d['filing'])
        self.assertEqual((got['version'], got['filing']['filed']), (d['version'], True))
        self.assertEqual(got['revision'], self.store.revision)
        self.assertEqual(self.views.filed, [('kit', 'EX-1', qid, self.now, 't', 'first', 'why',
                                             'api', False, [other])])
        self.assertEqual(self.filed_pending(), [qid])
        self.assertEqual([q['id'] for q in self.ok('GET', '/questions', AGENT,
                                                   query={'status': 'filed'})['items']], [qid])
        self.answer(other, 'other', note='neither')
        self.assertEqual(self.views.filed[-1][5:], ('Other', 'neither', 'api', True, []))

    def test_a_filed_answer_stays_for_its_asker_until_it_is_done_or_logged(self):
        # no wait of the asker's is blocked when the owner answers: a later one still gets it
        mono = [1000.0]
        self.api = api.Api(self.store, views=self.views, clock=lambda: self.now,
                           session_keys=lambda: {'kit-1'}, now=lambda: mono[0])
        logs, dones = (self.question(**{'from': 'kit-1', 'record': {'repo': 'kit', 'id': rid}})
                       for rid in ('EX-1', 'EX-2'))
        mine = self.bound('EX-1')
        for qid in (logs, dones, mine):
            self.answer(qid)
            self.assertEqual(self.doc(qid)['status'], 'filed')

        def wait(key):
            got = self.ok('GET', '/wait', AGENT, query={'pending': '1', 'for': key, 'timeout': '0'})
            return [(p['id'], p.get('filed')) for p in got['pending']]
        self.assertEqual(wait('kit-1'), [(logs, True), (dones, True)])
        # the PM: its own filed question; the asker's only once the asker cannot be reached
        self.assertEqual(wait('pm'), [(mine, True)])
        mono[0] += api.ACK_WINDOW_S
        self.assertEqual(wait('pm'), [(logs, True), (dones, True), (mine, True)])
        self.ok('POST', '/questions/%s/log' % logs, AGENT,
                {'ruling': R7, 'if_version': self.doc(logs)['version']})
        self.ok('POST', '/questions/%s/done' % dones, AGENT,
                {'why': 'filed in EX-2', 'if_version': self.doc(dones)['version']})
        self.assertEqual((self.doc(logs)['status'], self.doc(dones)['status']), ('logged', 'done'))
        self.assertEqual(wait('kit-1'), [])
        self.assertEqual(wait('pm'), [(mine, True)])

    def test_a_crash_after_the_record_write_is_mended_by_the_retry(self):
        qid = self.bound()
        self.views.crash = True
        with self.assertRaises(Crash):
            self.api.call('POST', BASE + '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        self.assertEqual(self.doc(qid)['status'], 'answered')
        self.assertEqual(self.views.lines, {('EX-1', qid): self.now})
        self.views.crash = False
        self.assertEqual(self.refile({'id': qid})['filed'], [qid])
        self.assertEqual(self.views.lines, {('EX-1', qid): self.now})
        self.assertEqual(len(self.views.filed), 2)
        self.assertEqual(self.doc(qid)['status'], 'filed')

    def test_a_failure_stays_pending_with_why_and_the_retry_files_it(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'C:\\work\\repo is busy', '')
        got = self.answer(qid)
        d = self.doc(qid)
        self.assertEqual((got['status'], d['status']), ('answered', 'answered'))
        self.assertEqual(d['filing'], {'at': self.now, 'code': 'store',
                                       'message': api.FILING_SAYS['store']})
        self.assertEqual(got['version'], d['version'])
        # the local owner is told the store's own text; the document, which goes over the
        # tunnel, and an agent's retry never carry it
        self.assertEqual(got['filing']['message'], 'C:\\work\\repo is busy')
        self.assertEqual(self.refile()['failed'], [{'id': qid, 'code': 'store',
                                                    'message': api.FILING_SAYS['store']}])
        self.assertEqual(self.pending(), [qid])
        self.views.fail = None
        self.tick()
        self.assertEqual(self.refile()['filed'], [qid])
        d = self.doc(qid)
        self.assertEqual((d['status'], d['filing'], d['filed_at']), ('filed', None, self.now))

    def test_a_retry_that_fails_the_same_way_moves_no_version(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'C:\\work\\repo is busy', '')
        self.answer(qid)
        first = self.doc(qid)
        revision = self.store.revision
        for _ in range(3):              # questions/file, as the hub start runs it, and by id
            self.tick()
            self.assertEqual(self.refile()['failed'], [{'id': qid, 'code': 'store',
                                                        'message': api.FILING_SAYS['store']}])
            self.refile({'id': qid})
        self.assertEqual(len(self.views.filed), 7, 'each retry did try the record')
        self.assertEqual(self.doc(qid), first, 'nothing written: the first `at` is kept')
        self.assertEqual(self.store.revision, revision)
        # the version the asker read still holds for its log
        self.ok('POST', '/questions/%s/log' % qid, AGENT,
                {'ruling': R7, 'if_version': first['version']})
        self.assertEqual(self.doc(qid)['status'], 'logged')

    def test_a_retry_that_fails_another_way_marks_the_new_failure(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'busy', '')
        self.answer(qid)
        first = self.doc(qid)
        self.tick()
        self.views.fail = HubError('unavailable', 'no view', 'views')
        self.assertEqual(self.refile()['failed'][0]['code'], 'unavailable')
        d = self.doc(qid)
        self.assertEqual(d['filing'], {'at': self.now, 'code': 'unavailable',
                                       'message': api.FILING_SAYS['unavailable']})
        self.assertGreater(d['version'], first['version'])

    def test_a_repo_with_no_store_is_left_to_the_pm_and_swept_with_no_write(self):
        qid = self.bound()
        self.views.fail = HubError('no_store', 'C:\\pm\\data names no store', 'repo')
        got = self.answer(qid)
        d = self.doc(qid)
        self.assertEqual((got['status'], d['status']), ('answered', 'answered'))
        self.assertEqual(d['filing'], {'at': self.now, 'code': 'no_store',
                                       'message': api.FILING_SAYS['no_store']})
        self.assertIn('the PM handles this answer', d['filing']['message'])
        self.assertNotIn('tries again', d['filing']['message'])
        self.assertEqual(self.pending(), [qid])
        tried = len(self.views.filed)
        self.tick()
        self.assertEqual(self.refile()['failed'], [{'id': qid, 'code': 'no_store',
                                                    'message': api.FILING_SAYS['no_store']}])
        self.assertEqual(len(self.views.filed), tried + 1, 'the sweep tries it again')
        self.assertEqual(self.doc(qid), d, 'a repeat writes nothing')
        # once the repository names a store the next sweep (or the hub start) files it
        self.views.fail = None
        self.tick()
        self.assertEqual(self.refile()['filed'], [qid])
        self.assertEqual(self.doc(qid)['status'], 'filed')

    def test_a_success_after_a_failure_marks_it_filed_with_a_new_version(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'busy', '')
        self.answer(qid)
        failed = self.doc(qid)
        self.views.fail = None
        self.tick()
        self.assertEqual(self.refile()['filed'], [qid])
        d = self.doc(qid)
        self.assertEqual((d['status'], d['filing'], d['filed_at']), ('filed', None, self.now))
        self.assertGreater(d['version'], failed['version'])

    def test_a_repeat_failure_on_a_question_that_moved_meanwhile_is_moved(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'busy', '')
        self.answer(qid)
        first = self.doc(qid)
        record_answer = self.views.record_answer

        def racing(*args):
            # another call writes the question between this retry's read and its mark
            self.views.record_answer = record_answer
            self.store.update('questions', qid, {'via': 'race'})
            return record_answer(*args)
        self.views.record_answer = racing
        self.assertEqual(api.file_answer(self.api, qid),
                         {'id': qid, 'filed': False, 'moved': True})
        d = self.doc(qid)
        self.assertEqual((d['filing'], d['via']), (first['filing'], 'race'))
        self.assertGreater(d['version'], first['version'])

    def test_a_record_closed_meanwhile_falls_back_to_the_pm(self):
        qid = self.bound()
        self.views.fail = HubError('not_found', 'no such record', 'id')
        self.answer(qid)
        d = self.doc(qid)
        self.assertEqual((d['status'], d['filing']['code']), ('answered', 'not_found'))
        self.assertIn('the PM handles this answer', d['filing']['message'])
        self.assertEqual(self.pending(), [qid])
        self.ok('POST', '/questions/%s/log' % qid, AGENT,
                {'ruling': R7, 'if_version': d['version']})
        self.assertEqual(self.doc(qid)['status'], 'logged')
        self.assertEqual(self.refile(), {'ok': True, 'filed': [], 'failed': [],
                                         'revision': self.store.revision})
        self.assertEqual(len(self.views.filed), 1, 'a logged question is never filed')

    def test_a_server_with_no_record_write_leaves_the_answer_to_the_pm(self):
        self.api = api.Api(self.store, views=BindViews(), clock=lambda: self.now)
        qid = self.bound()
        got = self.answer(qid)
        self.assertEqual((got['status'], got['filing']['code']), ('answered', 'unavailable'))
        self.assertEqual(self.doc(qid)['filing']['code'], 'unavailable')
        self.assertEqual(self.pending(), [qid])

    def test_two_filers_racing_write_the_record_once_and_mark_it_once(self):
        qid = self.bound()
        raced = []
        self.views.during = lambda: raced.append(self.refile())
        got = self.answer(qid)
        self.assertEqual(raced[0]['filed'], [qid])
        self.assertEqual(got['filing'], {'id': qid, 'filed': False, 'moved': True})
        d = self.doc(qid)
        self.assertEqual((d['status'], d['version']), ('filed', 3), 'asked, answered, filed')
        self.assertEqual(self.views.lines, {('EX-1', qid): self.now})

    def test_a_re_answer_while_the_first_is_filed_is_filed_by_its_own_call(self):
        qid = self.bound()
        first = self.now

        def again():
            self.tick()
            self.answer(qid, 1)
        self.views.during = again
        got = self.answer(qid)
        self.assertTrue(got['filing']['moved'])
        d = self.doc(qid)
        self.assertEqual((d['status'], d['answer']['label']), ('filed', 'second'))
        self.assertEqual([f[3] for f in self.views.filed], [first, self.now])
        self.assertEqual(self.views.lines, {('EX-1', qid): self.now}, 'the older never wins')

    def test_a_filed_question_answered_again_is_filed_again(self):
        qid = self.bound()
        self.answer(qid)
        self.tick()
        self.views.fail = HubError('store', 'busy', '')
        got = self.answer(qid, 1)
        d = self.doc(qid)
        self.assertEqual((got['status'], d['status'], d['filed_at']), ('answered', 'answered', None))
        self.assertEqual(d['filing']['code'], 'store')
        self.views.fail = None
        self.tick()
        self.assertEqual(self.answer(qid, 0)['status'], 'filed')
        d = self.doc(qid)
        self.assertEqual((d['filing'], d['filed_at'], d['answer']['label']),
                         (None, self.now, 'first'))
        self.assertEqual(self.views.lines, {('EX-1', qid): self.now})

    def test_an_unbound_question_is_never_filed(self):
        qid = self.question()
        got = self.answer(qid)
        self.assertNotIn('filing', got)
        self.assertEqual(self.doc(qid)['status'], 'answered')
        self.assertNotIn('filing', self.doc(qid))
        self.assertEqual(self.refile({'id': qid})['filed'], [])
        self.assertEqual(self.views.filed, [])
        self.refused('not_found', 'id', 'POST', '/questions/file', AGENT, {'id': 'Q-404'})
        self.refused('invalid', 'why', 'POST', '/questions/file', AGENT, {'why': 'x'})

    def test_the_hub_start_files_what_is_left_and_logs_each_failure(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'C:\\work\\repo is busy', '')
        self.answer(qid)
        said = []

        class Timer(object):
            def start(self):
                pass

        hub = serve.Hub(None, self.store, self.api, self.views, None, [], None, Timer(), [],
                        log=lambda level, text: said.append((level, text)))
        hub._refile()
        self.assertEqual(said, [('note', '%s not filed: %s' % (qid, api.FILING_SAYS['store']))])
        self.views.fail = None
        self.assertTrue(hub.start())
        for t in threading.enumerate():
            if t.name == 'hub-refile':
                t.join(30)
        self.assertEqual(self.doc(qid)['status'], 'filed')
        self.assertEqual(len(said), 1)


class StateViews(FileViews):
    """FileViews whose record view carries the record's `state:` line as read_record gives it
    (fields, [key, value] pairs): `states` {id: state} (none: no state line), and `moves`
    {id: state}, the state a filing leaves (what plan_answer does to the record). `blind`: the
    record view raises after the first read; `bare`: it gives no fields."""

    def __init__(self):
        FileViews.__init__(self)
        self.states, self.moves = {}, {}
        self.blind = self.bare = False

    def record(self, repo, id):
        got = BindViews.record(self, repo, id)
        if self.blind and len(self.calls) > 1:
            raise HubError('unavailable', 'git timed out', 'views')
        if not self.bare:
            state = self.states.get(id)
            got['fields'] = [['id', id]] + ([['state', state]] if state else [])
        return got

    def record_answer(self, repo, id, *rest):
        out = FileViews.record_answer(self, repo, id, *rest)
        if id in self.moves:
            self.states[id] = self.moves.pop(id)
        return out


class FiledAsTest(ApiCase):
    """A filed question's `filed_as`: what the answer did to its record, from the record's
    state read before and after the filing (docs/action-hub.md "Push notifications")."""

    def setUp(self):
        ApiCase.setUp(self)
        self.views = StateViews()
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def filed_as(self, rid, state, moved=None, choice=0, **kw):
        self.views.states[rid] = state
        if moved is not None:
            self.views.moves[rid] = moved
        qid = self.qid = self.question(record={'repo': 'kit', 'id': rid})
        self.views.calls[:] = []
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, dict({'choice': choice}, **kw))
        d = self.store.get('questions', qid)
        self.assertEqual(d['status'], 'filed')
        return d['filed_as']

    def test_an_answer_into_a_pulled_record_is_filed_as_pulled(self):
        self.assertEqual(self.filed_as('EX-1', 'pulled'), 'pulled')
        self.assertEqual(self.views.calls, [('kit', 'EX-1'), ('kit', 'EX-1')], 'before and after')

    def test_the_answer_that_unparks_or_unblocks_its_record_is_filed_as_freed(self):
        self.assertEqual(self.filed_as('EX-1', 'parked', 'open'), 'freed')
        self.assertEqual(self.filed_as('EX-2', 'blocked', 'open'), 'freed')

    def test_a_free_text_answer_that_unparks_its_record_is_filed_as_freed(self):
        self.assertEqual(self.filed_as('EX-1', 'parked', 'open', choice='other', note='neither'),
                         'freed')

    def test_an_answer_that_leaves_its_record_waiting_is_filed_as_held(self):
        self.assertEqual(self.filed_as('EX-1', 'parked'), 'held', 'another question waits')
        self.assertEqual(self.filed_as('EX-2', 'blocked'), 'held', 'another blocker is left')

    def test_an_answer_into_an_open_record_is_filed_as_held(self):
        # no state line reads as open: nothing waited on the answer, nobody is woken
        self.assertEqual(self.filed_as('EX-1', None), 'held')

    def test_a_record_that_cannot_be_read_leaves_filed_as_unknown(self):
        self.views.blind = True
        self.assertIsNone(self.filed_as('EX-1', 'parked', 'open'), 'read after: refused')
        self.views.blind = False
        self.views.bare = True
        self.assertIsNone(self.filed_as('EX-2', 'parked', 'open'), 'a view with no fields')

    def test_a_re_answer_drops_what_the_first_filing_did(self):
        self.assertEqual(self.filed_as('EX-1', 'parked', 'open'), 'freed')
        qid = self.qid
        self.views.fail = HubError('store', 'busy', '')
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 1})
        d = self.store.get('questions', qid)
        self.assertEqual((d['status'], d['filed_as']), ('answered', None))


class StandingRuleTest(ApiCase):
    """The owner's standing-rule box: `standing` true with an answer to a question bound to a
    record, or alone on one answered already (the promote): `standing` {at, via}, so the PM
    logs it (docs/action-hub.md "A standing rule")."""

    def setUp(self):
        ApiCase.setUp(self)
        self.views = FileViews()
        self.times = iter('2026-01-02T03:04:%02d+00:00' % s for s in range(5, 60))
        self.now = next(self.times)
        self.mono = [1000.0]
        self.api = api.Api(self.store, views=self.views, clock=lambda: self.now,
                           session_keys=lambda: {'kit-1'}, now=lambda: self.mono[0])
        # the asker is heard (a wait), so a plain filed answer is its alone
        self.ok('GET', '/wait', AGENT, query={'pending': '1', 'for': 'kit-1', 'timeout': '0'})

    def tick(self):
        self.now = next(self.times)

    def doc(self, qid):
        return self.store.get('questions', qid)

    def bound(self, rid='EX-1'):
        return self.question(**{'from': 'kit-1', 'record': {'repo': 'kit', 'id': rid}})

    def answer(self, qid, body):
        return self.ok('POST', '/questions/%s/answer' % qid, OWNER, body)

    def for_pm(self):
        return [p['id'] for p in self.ok('GET', '/pending', AGENT, query={'for': 'pm'})['items']]

    def for_asker(self):
        return [p['id'] for p in self.ok('GET', '/pending', AGENT,
                                         query={'for': 'kit-1'})['items']]

    def test_the_box_makes_a_filed_answer_a_standing_rule_the_pm_reads(self):
        plain, rule = self.bound(), self.bound('EX-2')
        self.answer(plain, {'choice': 0})
        self.tick()
        got = self.answer(rule, {'choice': 1, 'note': 'always', 'standing': True})
        d = self.doc(rule)
        self.assertEqual((got['status'], d['status']), ('filed', 'filed'))
        self.assertEqual(d['standing'], {'at': self.now, 'via': 'api'})
        self.assertNotIn('standing', self.doc(plain))
        self.assertEqual(len(self.views.filed), 2, 'a standing answer is filed like any other')
        # the asker still reads both; the PM reads the standing one while the asker is reachable
        self.assertEqual(self.for_asker(), [plain, rule])
        self.assertEqual(self.for_pm(), [rule])
        flags = {p['id']: p.get('standing') for p in self.ok('GET', '/pending', AGENT)['items']}
        self.assertEqual(flags, {plain: None, rule: True})

    def test_a_re_answer_without_the_box_drops_the_standing_rule(self):
        qid = self.bound()
        self.answer(qid, {'choice': 0, 'standing': True})
        self.tick()
        self.answer(qid, {'choice': 1, 'standing': False})
        self.assertIsNone(self.doc(qid)['standing'])
        self.assertEqual(self.for_pm(), [])
        self.tick()
        self.answer(qid, {'choice': 0, 'standing': True})
        self.tick()
        self.answer(qid, {'choice': 0})
        self.assertIsNone(self.doc(qid)['standing'])

    def test_the_box_is_refused_for_a_question_bound_to_no_record(self):
        qid = self.question()
        self.refused('invalid', 'standing', 'POST', '/questions/%s/answer' % qid, OWNER,
                     {'choice': 0, 'standing': True})
        self.refused('invalid', 'standing', 'POST', '/questions/%s/answer' % self.bound(), OWNER,
                     {'choice': 0, 'standing': 'yes'})

    def test_a_promote_keeps_the_answer_given_and_files_nothing_again(self):
        qid = self.bound()
        self.answer(qid, {'choice': 1, 'note': 'why'})
        before = self.doc(qid)
        self.assertEqual(self.for_pm(), [], 'a plain filed answer is its asker\'s')
        self.tick()
        got = self.answer(qid, {'standing': True, 'if_version': before['version']})
        d = self.doc(qid)
        self.assertEqual(d['standing'], {'at': self.now, 'via': 'api'})
        self.assertEqual((d['status'], d['answer'], d['filed_at']),
                         ('filed', before['answer'], before['filed_at']))
        self.assertEqual((got['version'], d['version']), (before['version'] + 1,) * 2)
        self.assertEqual(len(self.views.filed), 1, 'not filed again')
        self.assertEqual(self.for_pm(), [qid])

    def test_an_answer_whose_filing_failed_can_be_promoted(self):
        qid = self.bound()
        self.views.fail = HubError('store', 'busy', '')
        self.answer(qid, {'choice': 0})
        self.assertEqual(self.doc(qid)['status'], 'answered')
        self.answer(qid, {'standing': True})
        self.assertEqual(self.doc(qid)['standing']['via'], 'api')

    def test_a_promote_is_refused_unless_an_answer_bound_to_a_record_is_there(self):
        path = '/questions/%s/answer'
        opened = self.bound()
        self.refused('invalid', 'status', 'POST', path % opened, OWNER, {'standing': True})
        unbound = self.question()
        self.answer(unbound, {'choice': 0})
        self.refused('invalid', 'record', 'POST', path % unbound, OWNER, {'standing': True})
        rule = self.bound('EX-2')
        self.answer(rule, {'choice': 0, 'standing': True})
        self.refused('invalid', 'standing', 'POST', path % rule, OWNER, {'standing': True})
        self.refused('invalid', 'note', 'POST', path % rule, OWNER,
                     {'standing': True, 'note': 'x'})
        self.refused('invalid', 'choice', 'POST', path % rule, OWNER, {'standing': False})
        self.refused('invalid', 'choice', 'POST', path % rule, OWNER, {})

    def test_a_standing_rule_is_logged_by_the_pm_never_marked_done(self):
        qid = self.bound()
        self.answer(qid, {'choice': 0, 'standing': True})
        v = self.doc(qid)['version']
        self.refused('invalid', 'standing', 'POST', '/questions/%s/done' % qid, AGENT,
                     {'why': 'filed in EX-1', 'if_version': v})
        self.ok('POST', '/questions/%s/log' % qid, AGENT, {'ruling': R9, 'if_version': v})
        self.assertEqual(self.doc(qid)['status'], 'logged')
        self.assertEqual((self.for_pm(), self.for_asker()), ([], []))


class AnswerStoreTest(trw.Case):
    """The owner's answer filed through SiteViews' record_answer into a real ref store."""

    def setUp(self):
        trw.Case.setUp(self)
        self.named()
        self.seed(**rec('FOO', state='parked', q='Q-1000', parked_at='2026-10-01'))
        root, self.reread = self.repo.root, []
        reread = self.reread

        class Backlog(object):
            def _named(self, repo):
                return 'r', root

            def reread(self, name, at):
                reread.append((name, at))

            def record(self, repo, rid):
                return {'repo': repo, 'id': rid, 'where': 'open'}

        self.backlog = Backlog()
        self.views = serve.SiteViews(None, serve.Sources(), backlog_views=self.backlog)
        hub = tempfile.mkdtemp(prefix='kit-hubapi-')
        self.addCleanup(shutil.rmtree, hub, True)
        self.store = Store.open(hub, clock=CLOCK)
        self.addCleanup(self.store.close)
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def test_site_views_file_the_answer_then_read_the_repository_again(self):
        out = self.views.record_answer('r', 'FOO', 'Q-1000', NOW, 'Which one?', 'first', '', 'web',
                                       False, [])
        self.assertTrue(any('UNPARKED' in line for line in out['lines']), out['lines'])
        self.assertEqual(self.reread, [('r', self.repo.root)])
        self.assertEqual(self.field('FOO', 'answer'), ['Q-1000 ' + NOW])

    def test_the_owner_answer_route_files_into_the_record_and_marks_it_filed(self):
        status, got = self.api.call('POST', BASE + '/questions', AGENT,
                                    {'title': 'Which one?', 'options': OPTIONS,
                                     'record': {'repo': 'r', 'id': 'FOO'}})
        self.assertEqual((status, got['id']), (200, 'Q-1000'), got)
        status, got = self.api.call('POST', BASE + '/questions/Q-1000/answer', OWNER,
                                    {'choice': 1, 'note': 'go'}, via='web')
        self.assertEqual((status, got['status']), (200, 'filed'), got)
        self.assertEqual(self.store.get('questions', 'Q-1000')['status'], 'filed')
        self.assertEqual(self.field('FOO', 'state'), ['open'])
        self.assertEqual(self.field('FOO', 'answer'), ['Q-1000 ' + NOW])
        body = self.read('FOO')['body']
        self.assertIn('Answer: second', body)
        self.assertIn('Via: web', body)
        # the retry finds nothing left, and filing the record again would change nothing
        self.assertEqual(self.api.call('POST', BASE + '/questions/file', AGENT, {})[1]['filed'],
                         [])

    def test_a_free_text_answer_frees_the_record_it_unparks(self):
        # the record read through the real read_record, before and after the real filing
        root = self.repo.root
        self.backlog.record = lambda repo, rid: hbacklog.read_record('r', root, rid)
        self.api.call('POST', BASE + '/questions', AGENT,
                      {'title': 'Which one?', 'options': OPTIONS,
                       'record': {'repo': 'r', 'id': 'FOO'}})
        status, got = self.api.call('POST', BASE + '/questions/Q-1000/answer', OWNER,
                                    {'choice': 'other', 'note': 'neither'}, via='web')
        self.assertEqual((status, got['status']), (200, 'filed'), got)
        self.assertEqual(self.field('FOO', 'state'), ['open'])
        self.assertEqual(self.store.get('questions', 'Q-1000')['filed_as'], 'freed')


class AnswerNoStoreTest(trw.Case):
    """An answer bound to a record of a repository whose .kit.json names no backlog store: the
    implicit store is refused as `no_store`, which the filing leaves to the PM."""

    def setUp(self):
        trw.Case.setUp(self)        # not named(): the implicit default store
        root = self.repo.root

        class Backlog(object):
            def _named(self, repo):
                return 'r', root

            def reread(self, name, at):
                pass

            def record(self, repo, rid):
                return {'repo': repo, 'id': rid, 'where': 'open'}

        self.views = serve.SiteViews(None, serve.Sources(), backlog_views=Backlog())
        hub = tempfile.mkdtemp(prefix='kit-hubapi-')
        self.addCleanup(shutil.rmtree, hub, True)
        self.store = Store.open(hub, clock=CLOCK)
        self.addCleanup(self.store.close)
        self.api = api.Api(self.store, views=self.views, clock=CLOCK)

    def test_the_owner_answer_is_left_to_the_pm_with_the_no_store_phrase(self):
        before = self.repo.git('for-each-ref', smod.STORE_REF)
        self.api.call('POST', BASE + '/questions', AGENT,
                      {'title': 'Which one?', 'options': OPTIONS,
                       'record': {'repo': 'r', 'id': 'FOO'}})
        status, got = self.api.call('POST', BASE + '/questions/Q-1000/answer', OWNER,
                                    {'choice': 1}, via='web')
        self.assertEqual((status, got['status'], got['filing']['code']),
                         (200, 'answered', 'no_store'), got)
        d = self.store.get('questions', 'Q-1000')
        self.assertEqual((d['filing']['code'], d['filing']['message']),
                         ('no_store', api.FILING_SAYS['no_store']))
        self.assertEqual(self.repo.git('for-each-ref', smod.STORE_REF), before)
        version = d['version']
        # the sweep tries it again and says so, and the repeat moves no version
        self.assertEqual(self.api.call('POST', BASE + '/questions/file', AGENT, {})[1]['failed'],
                         [{'id': 'Q-1000', 'code': 'no_store',
                           'message': api.FILING_SAYS['no_store']}])
        self.assertEqual(self.store.get('questions', 'Q-1000')['version'], version)


if __name__ == '__main__':
    unittest.main()
