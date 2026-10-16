"""kitlib.hubsite.api: what each verb writes and answers, the fields it refuses, the error
shape, versions, the views it borrows, wait, pending, snapshot and health. Who may call a route
is tests/hubsite/test_api_roles.py."""
import inspect
import json
import os
import shutil
import tempfile
import threading
import unittest

from kitlib.hubsite import api, todosteps
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
                                       {'ruling': 'decision-7', 'if_version': 0}, None)
                self.assertEqual((status, obj.get('error')), (200, None), obj)
                self.assertEqual((obj['status'], obj['version']), ('logged', 1))
                with open(path, encoding='utf-8') as f:
                    self.assertEqual(json.load(f)['recommended'], 1, 'stored as the number')

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

    def test_snapshot_carries_pages_and_their_bodies(self):
        """The page's Docs section reads both from the snapshot: a change that trims either must change this."""
        body = '<h1>Plan</h1><svg viewBox="0 0 1 1"></svg>'
        self.store.put('pages', 'plan-1', {'title': 'The plan', 'repo': 'kit', 'at': '2026-10-15T00:00:00Z'})
        self.store.put('page_html', 'plan-1', {'html': body})
        for role in (OWNER, AGENT):
            cols = self.ok('GET', '/snapshot', role)['collections']
            self.assertEqual(cols['pages']['plan-1']['title'], 'The plan', role)
            self.assertEqual(cols['page_html']['plan-1']['html'], body, role)

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
        obj = self.ok('POST', path, AGENT, {'reply': 'r', 'ruling': ' decision-7 '})
        d = self.store.get('to_pm', pid)
        self.assertEqual((obj['status'], d['status'], d['reply'], d['ruling']),
                         ('done', 'done', 'r', 'decision-7'))
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
        self.ok('POST', '/questions/%s/log' % qid, AGENT, {'ruling': 'decision-7',
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
        doc = {'status': 'open', 'title': 't', 'commands': ['dir']}
        self.store = seeded(self, {'todos/T-1.json': dict(doc, **{'from': 'Y o u'}),
                                   'todos/T-2.json': dict(doc, **{'from': 'you\u200b'}),
                                   'todos/T-3.json': dict(doc, **{'from': 'you'})})
        self.api = api.Api(self.store, clock=CLOCK)
        for tid in ('T-1', 'T-2'):
            with self.subTest(id=tid):
                self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, OWNER,
                             {'title': 'x'})
                self.refused('forbidden', 'from', 'POST', '/todos/%s/delete' % tid, OWNER)
        self.ok('POST', '/todos/T-3/edit', OWNER, {'title': 'x'})

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
        for method, path, role, body in (
                ('POST', '/questions', AGENT, {'title': 't', 'options': OPTIONS}),
                ('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0}),
                ('POST', '/questions/%s/log' % qid, AGENT, {'ruling': 'decision-7',
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
    """The owner's own to-dos: urgent, edit, delete. A to-do an agent gave is not theirs to change
    (docs/action-hub.md, todos: "the owner can set it on their own items")."""

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

    def test_an_agent_cannot_edit_or_delete_a_to_do(self):
        for tid in (self.mine(), self.theirs()):
            self.refused('forbidden', 'role', 'POST', '/todos/%s/edit' % tid, AGENT,
                         {'title': 'x'})
            self.refused('forbidden', 'role', 'POST', '/todos/%s/delete' % tid, AGENT)

    def test_a_to_do_an_agent_gave_is_not_the_owners_to_edit_or_delete(self):
        tid = self.theirs()
        for body in ({'title': 'x'}, {'urgent': True}):
            self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, OWNER, body)
        self.refused('forbidden', 'from', 'POST', '/todos/%s/delete' % tid, OWNER)
        self.assertEqual(self.store.get('todos', tid)['title'], 't')
        self.ok('POST', '/todos/%s/done' % tid, OWNER)      # ticking it stays the owner's

    def test_a_to_do_with_no_readable_from_is_nobodys_to_edit_or_delete(self):
        self.store = seeded(self, {'todos/T-1.json': {'status': 'open', 'title': 't'},
                                   'todos/T-2.json': {'status': 'open', 'title': 't',
                                                      'from': ['you']}})
        self.api = api.Api(self.store, clock=CLOCK)
        for tid in ('T-1', 'T-2'):
            self.refused('forbidden', 'from', 'POST', '/todos/%s/edit' % tid, OWNER,
                         {'title': 'x'})
            self.refused('forbidden', 'from', 'POST', '/todos/%s/delete' % tid, OWNER)

    def test_edit_or_delete_of_a_to_do_that_is_not_there(self):
        self.refused('not_found', 'id', 'POST', '/todos/T-404/edit', OWNER, {'title': 'x'})
        self.refused('not_found', 'id', 'POST', '/todos/T-404/delete', OWNER)


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


if __name__ == '__main__':
    unittest.main()
