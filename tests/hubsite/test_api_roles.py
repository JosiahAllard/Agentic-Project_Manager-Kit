"""kitlib.hubsite.api: who may call each route. One row per ROUTES entry and caller role: the
role the route names gets its answer, the other one gets `forbidden` and changes nothing. A route
added to ROUTES with no row here, or with no role, fails."""
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import api
from kitlib.hubsite.api import AGENT, ANY, BASE, OWNER
from kitlib.hubsite.store import Store

CLOCK = lambda: '2026-01-02T03:04:05+00:00'      # noqa: E731
OPTIONS = [{'label': 'a', 'description': 'b'}]


class Views(object):
    def backlog(self, repo='', state='', fresh=False):
        return []

    def completed(self, repo='', days=None):
        return []

    def notices(self):
        return []

    def repos(self):
        return []

    def usage(self):
        return []

    def agents(self):
        return []

    def health(self):
        return {'git': 'ok', 'backlog': 'ok'}


def seed_question(store, status='open'):
    return store.add('questions', {'status': status, 'title': 't', 'options': OPTIONS}).id


def seed_todo(store):
    return store.add('todos', {'status': 'open', 'title': 't', 'from': 'you'}).id


def seed_story(store):
    return store.add('stories', {'status': 'submitted', 'repo': 'r', 'title': 't',
                                 'story': 's'}).id


def seed_to_pm(store, status='sent'):
    return store.add('to_pm', {'status': status, 'kind': 'fyi', 'text': 'x'}).id


def seed_inbox(store):
    return store.add('inbox', {'text': 'x'}).id


# (method, path under BASE): (seed -> id or None, body, query). `{id}` takes the seeded id.
ROWS = {
    ('GET', '/questions'): (None, None, None),
    ('POST', '/questions'): (None, {'title': 't', 'options': OPTIONS}, None),
    ('POST', '/questions/{id}/answer'): (seed_question, {'choice': 0}, None),
    ('POST', '/questions/{id}/log'): (lambda s: seed_question(s, 'answered'),
                                      {'ruling': 'decision-7', 'if_version': 1}, None),
    ('GET', '/todos'): (None, None, None),
    ('POST', '/todos'): (None, {'title': 't', 'commands': ['dir']}, None),
    ('POST', '/todos/{id}/done'): (seed_todo, {'done': True}, None),
    ('POST', '/todos/{id}/ack'): (seed_todo, None, None),
    ('GET', '/stories'): (None, None, None),
    ('POST', '/stories'): (None, {'repo': 'r', 'title': 't', 'story': 's'}, None),
    ('POST', '/stories/{id}'): (seed_story, {'status': 'review', 'if_version': 1}, None),
    ('POST', '/stories/{id}/withdraw'): (seed_story, None, None),
    ('GET', '/to_pm'): (None, None, None),
    ('POST', '/to_pm'): (None, {'text': 'x'}, None),
    ('POST', '/to_pm/send'): (lambda s: seed_to_pm(s, 'queued'), None, None),
    ('POST', '/to_pm/{id}/reply'): (seed_to_pm, {'reply': 'r'}, None),
    ('POST', '/to_pm/{id}/edit'): (lambda s: seed_to_pm(s, 'queued'), {'text': 'y'}, None),
    ('POST', '/to_pm/{id}/delete'): (lambda s: seed_to_pm(s, 'queued'), None, None),
    ('POST', '/inbox'): (None, {'text': 'x'}, None),
    ('POST', '/inbox/{id}/read'): (seed_inbox, None, None),
    ('GET', '/backlog'): (None, None, None),
    ('GET', '/completed'): (None, None, None),
    ('GET', '/notices'): (None, None, None),
    ('GET', '/repos'): (None, None, None),
    ('GET', '/usage'): (None, None, None),
    ('GET', '/agents'): (None, None, None),
    ('GET', '/pending'): (None, None, None),
    ('GET', '/wait'): (None, None, {'timeout': '0'}),
    ('GET', '/snapshot'): (None, None, None),
    ('GET', '/health'): (None, None, None),
}


def key(route):
    return route.method, route.pattern[len(BASE):]


class RolesCase(unittest.TestCase):
    def fresh(self, seed):
        root = tempfile.mkdtemp(prefix='kit-hubapi-')
        self.addCleanup(shutil.rmtree, root, True)
        store = Store.open(root, clock=CLOCK)
        self.addCleanup(store.close)
        doc_id = seed(store) if seed else 'X-1'
        return store, api.Api(store, views=Views(), clock=CLOCK), doc_id

    def test_every_route_has_a_role_and_a_row(self):
        self.assertTrue(api.ROUTES)
        for route in api.ROUTES:
            with self.subTest(route=route.method + ' ' + route.pattern):
                self.assertIn(route.role, (OWNER, AGENT, ANY))
                self.assertTrue(route.pattern.startswith(BASE + '/'))
                self.assertTrue(callable(route.handler))
                self.assertIn(key(route), ROWS, 'a route with no row in the role table')
        self.assertEqual(len({key(r) for r in api.ROUTES}), len(api.ROUTES), 'a route twice')
        self.assertEqual(sorted(ROWS), sorted(key(r) for r in api.ROUTES),
                         'a row for a route that is gone')

    def test_one_row_per_route_and_role(self):
        for route in api.ROUTES:
            seed, body, query = ROWS[key(route)]
            for role in (OWNER, AGENT):
                with self.subTest(route=route.method + ' ' + route.pattern, role=role):
                    store, a, doc_id = self.fresh(seed)
                    before = store.snapshot()
                    path = route.pattern.replace('{id}', doc_id)
                    status, obj = a.call(route.method, path, role, body, query)
                    if route.role in (role, ANY):
                        self.assertEqual((status, obj.get('error')), (200, None))
                    else:
                        self.assertEqual(status, 403)
                        self.assertEqual(obj['error']['code'], 'forbidden')
                        self.assertIs(obj['ok'], False)
                        self.assertEqual(store.snapshot(), before, 'a refused call wrote')

    def test_the_roles_the_design_names(self):
        want = {('POST', '/questions'): AGENT, ('POST', '/questions/{id}/answer'): OWNER,
                ('POST', '/questions/{id}/log'): AGENT, ('POST', '/todos'): ANY,
                ('POST', '/todos/{id}/done'): OWNER, ('POST', '/todos/{id}/ack'): AGENT,
                ('POST', '/stories'): OWNER, ('POST', '/stories/{id}'): AGENT,
                ('POST', '/stories/{id}/withdraw'): OWNER, ('POST', '/to_pm'): OWNER,
                ('POST', '/to_pm/send'): OWNER, ('POST', '/to_pm/{id}/reply'): AGENT,
                ('POST', '/to_pm/{id}/edit'): OWNER, ('POST', '/to_pm/{id}/delete'): OWNER,
                ('POST', '/inbox'): AGENT, ('POST', '/inbox/{id}/read'): OWNER}
        have = {key(r): r.role for r in api.ROUTES}
        for k, role in sorted(want.items()):
            with self.subTest(route=k):
                self.assertEqual(have.get(k), role)
        for k, role in sorted(have.items()):
            if k[0] == 'GET':
                with self.subTest(route=k):
                    self.assertEqual(role, ANY)
            else:
                self.assertIn(k, want, 'a write route the role table does not pin')

    def test_a_route_with_no_role_is_closed_to_everybody(self):
        calls = []

        def handler(a, req):
            calls.append(req)
            return {'secret': True}
        for bad in (None, '', 'everyone', 'Owner', ['owner'], True):
            routes = api.ROUTES + (api.Route('GET', BASE + '/open', bad, handler),)
            with mock.patch.object(api, 'ROUTES', routes):
                for role in (OWNER, AGENT):
                    with self.subTest(route_role=bad, role=role):
                        _, a, _ = self.fresh(None)
                        status, obj = a.call('GET', BASE + '/open', role)
                        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
        self.assertEqual(calls, [])

    def test_a_caller_that_is_neither_owner_nor_agent_is_refused(self):
        for route in api.ROUTES:
            seed, body, query = ROWS[key(route)]
            for role in (None, '', ANY, 'Owner', 'OWNER', 'owner ', 'admin', True, 1,
                         ['owner'], ('owner',), {'owner': 1}):
                with self.subTest(route=route.method + ' ' + route.pattern, role=repr(role)):
                    store, a, doc_id = self.fresh(seed)
                    before = store.snapshot()
                    status, obj = a.call(route.method, route.pattern.replace('{id}', doc_id),
                                         role, body, query)
                    self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
                    self.assertEqual(store.snapshot(), before)

    def test_the_role_is_checked_before_the_body_and_the_document(self):
        _, a, _ = self.fresh(None)
        for method, path, role in (('POST', '/questions/Q-404/answer', AGENT),
                                   ('POST', '/questions', OWNER),
                                   ('POST', '/stories/S-404', OWNER)):
            with self.subTest(path=path):
                status, obj = a.call(method, BASE + path, role, 'not an object')
                self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))

    def test_the_role_cannot_come_from_the_body_or_the_query(self):
        store, a, doc_id = self.fresh(seed_question)
        for body, query in (({'choice': 0, 'role': OWNER}, None),
                            ({'choice': 0}, {'role': OWNER})):
            status, obj = a.call('POST', BASE + '/questions/%s/answer' % doc_id, AGENT, body,
                                 query)
            self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
        self.assertEqual(store.get('questions', doc_id)['status'], 'open')


if __name__ == '__main__':
    unittest.main()
