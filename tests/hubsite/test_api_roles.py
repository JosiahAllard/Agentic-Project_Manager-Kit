"""kitlib.hubsite.api: who may call each route. One row per ROUTES entry and caller role: the
role the route names gets its answer, the other one gets `forbidden` and changes nothing. A route
added to ROUTES with no row here, or with no role, fails."""
import datetime
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import api
from kitlib.hubsite import approvals as ap
from kitlib.hubsite import skilledits
from kitlib.hubsite.api import AGENT, ANY, APPROVER, BASE, LOCK_WRITE, OWNER
from kitlib.hubsite.store import Store
from tests.gitfixture import TempRepo
from tests.hubsite.httpfix import FINDING

CLOCK = lambda: '2026-01-02T03:04:05+00:00'      # noqa: E731
OPTIONS = [{'label': 'a', 'description': 'b'}]
# ledger ids, built so no file holds a ruling id (tests/scrub)
R7 = 'R-' + '0007'
KEY = 'k' * 64
URL = 'https://github.com/Owner/Repo.git'
GIT = []        # the TempRepo the approvals routes read their bases from (setUpModule)
# the source of skill `builder` the skill_source view serves, and an owner's edit of it
SKILL_SOURCE = '---\nname: builder\n---\nold\n'
SKILL_EDIT = '---\nname: builder\n---\nnew\n'


def setUpModule():
    repo = TempRepo()
    GIT.append(repo)
    ADD_BODY['head'] = repo.initial


def tearDownModule():
    GIT.pop().cleanup()


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

    def skills(self):
        return []

    def skill(self, name):
        return {}

    def skill_source(self, name):
        return {'name': name, 'text': SKILL_SOURCE,
                'blob': skilledits.git_blob(SKILL_SOURCE.encode('utf-8'))}

    def health(self):
        return {'git': 'ok', 'backlog': 'ok'}

    def record(self, repo, id):
        return {}

    def record_template(self, repo):
        return {}

    def record_add(self, repo, id, summary, scopes, effort, ref):
        return {}

    def record_edit(self, repo, id, version, changes, unset, body):
        return {}

    def record_delete(self, repo, id, why):
        return {}

    def record_unblock(self, repo, id, on, reason):
        return {}

    def findings(self):
        return [dict(FINDING)]


class LockDoor(object):
    """A stand-in for kitlib.hubsite.lockdoor.LockDoor: the machine lock's reads and writes,
    answered at once, with no lock store. Each write is recorded in self.wrote as (verb, args,
    remote)."""
    writer = 'owner'

    def __init__(self):
        self.wrote = []

    def revision(self):
        return 7

    def view(self, remote=False):
        return {'lock_revision': 7, 'view': {'remote': remote}, 'writer': self.writer}

    def stats(self, days=None, since=None, until=None):
        return {'range': {'since': since, 'until': until}, 'rows': []}

    def _w(self, verb, args, remote):
        self.wrote.append((verb, args, remote))
        return {'verb': verb}

    def reserve(self, scope, start, end, note, remote=False):
        return self._w('reserve', (scope, start, end, note), remote)

    def hold(self, scope, start, end, note, cpu, gpu, remote=False):
        return self._w('hold', (scope, start, end, note, cpu, gpu), remote)

    def edit(self, hold_id, start, end, note, remote=False):
        return self._w('edit', (hold_id, start, end, note), remote)

    def end_hold(self, hold_id, remote=False):
        return self._w('end_hold', (hold_id,), remote)

    def end_run(self, run_id, remote=False):
        return self._w('end_run', (run_id,), remote)

    def move_to_top(self, run_id, remote=False):
        return self._w('move_to_top', (run_id,), remote)

    def game_mode(self, enabled, remote=False):
        return self._w('game_mode', (enabled,), remote)

    def games(self, text, remote=False):
        return self._w('games', (text,), remote)

    def division(self, fields, remote=False):
        return self._w('division', (fields,), remote)


def seed_question(store, status='open'):
    return store.add('questions', {'status': status, 'title': 't', 'options': OPTIONS}).id


def seed_todo(store):
    return store.add('todos', {'status': 'open', 'title': 't', 'from': 'you'}).id


def seed_agent_todo(store):
    return store.add('todos', {'status': 'open', 'title': 't', 'from': 'pm',
                               'commands': ['dir']}).id


def seed_story(store):
    return store.add('stories', {'status': 'submitted', 'repo': 'r', 'title': 't',
                                 'story': 's'}).id


def seed_to_pm(store, status='sent'):
    return store.add('to_pm', {'status': status, 'kind': 'fyi', 'text': 'x'}).id


def seed_inbox(store):
    return store.add('inbox', {'text': 'x'}).id


def seed_page(store):
    store.put('page_html', 'plan-1', {'html': '<p>x</p>'})
    store.put('pages', 'plan-1', {'title': 't'})
    return 'plan-1'


def seed_approval(store, decision=None):
    now = datetime.datetime.fromisoformat(CLOCK())
    doc = ap.new_doc(URL, 't', 'a' * 40, [{'path': 'x.md', 'base_blob': None,
                                            'content': 'x\n'}], now)
    # an id not yet used, so a store seeded again (tests.hubsite.test_httpd) starts at version 1
    doc_id = 'A-%d' % (len(store.snapshot()[1].get('approvals') or {}) + 1)
    if decision:
        doc.update(ap.decide_fields(KEY, doc_id, doc, decision, now))
    store.put('approvals', doc_id, doc)      # the id the signature covers
    return doc_id


def seed_repo(store):
    store.put('repos', 'r', {'name': 'r'})      # the repo a schedule names must be known
    return 'X-1'


def seed_mark(store):
    store.put('finding_marks', FINDING['fingerprint'], {'status': 'dismissed'})
    return FINDING['fingerprint']


def seed_skill_edit(store, status='draft'):
    return store.add('skill_edits', {
        'status': status, 'skill': 'builder', 'text': SKILL_EDIT,
        'base_blob': skilledits.git_blob(SKILL_SOURCE.encode('utf-8'))}).id


MARK_BODY = {'fingerprint': FINDING['fingerprint'], 'status': 'dismissed'}
ACT_BODY = {'kind': 'act', 'finding': FINDING['fingerprint']}
SCHEDULE_BODY = {'task': 'maint-lint', 'repo': 'r', 'cadence': 'daily', 'at': '04:00'}
ADD_BODY = {'repo': URL, 'title': 't', 'head': None,        # head: setUpModule
            'files': [{'path': 'new.md', 'base_blob': None, 'content': 'x\n'}]}


# (method, path under BASE): (seed -> id or None, body, query). `{id}` takes the seeded id,
# `{repo}` the name REPO (`path`).
ROWS = {
    ('GET', '/questions'): (None, None, None),
    ('POST', '/questions'): (None, {'title': 't', 'options': OPTIONS, 'class': 'go'}, None),
    ('POST', '/questions/{id}/answer'): (seed_question, {'choice': 0}, None),
    ('POST', '/questions/{id}/log'): (lambda s: seed_question(s, 'answered'),
                                      {'if_version': 1}, None),
    ('POST', '/questions/{id}/done'): (lambda s: seed_question(s, 'answered'),
                                       {'why': 'a one-off go', 'if_version': 1}, None),
    ('POST', '/questions/file'): (lambda s: seed_question(s, 'answered'), {}, None),
    ('POST', '/questions/bind'): (None, {'from': 'pm'}, None),
    ('GET', '/todos'): (None, None, None),
    ('POST', '/todos'): (None, {'title': 't', 'commands': ['dir']}, None),
    ('POST', '/todos/{id}/done'): (seed_todo, {'done': True}, None),
    ('POST', '/todos/{id}/ack'): (seed_todo, None, None),
    ('POST', '/questions/{id}/edit'): (seed_question,
                                       {'from': 'pm', 'title': 'u', 'if_version': 1}, None),
    # an ANY route whose two roles take different bodies: a row for each
    ('POST', '/questions/{id}/delete'): {OWNER: (seed_question, None, None),
                                         AGENT: (seed_question, {'from': 'pm', 'if_version': 1},
                                                 None)},
    ('POST', '/todos/{id}/edit'): {OWNER: (seed_todo, {'title': 'u'}, None),
                                   AGENT: (seed_agent_todo,
                                           {'from': 'pm', 'title': 'u', 'if_version': 1}, None)},
    ('POST', '/todos/{id}/delete'): {OWNER: (seed_todo, None, None),
                                     AGENT: (seed_agent_todo, {'from': 'pm', 'if_version': 1},
                                             None)},
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
    ('GET', '/landings'): (None, None, None),
    ('POST', '/landings'): (None, {'repo': 'r', 'branch': 'kit/x', 'tip': 'a' * 40,
                                   'main': 'b' * 40}, None),
    ('GET', '/pages'): (None, None, None),
    ('POST', '/pages'): (None, {'id': 'plan-1', 'title': 't', 'html': '<p>x</p>'}, None),
    ('GET', '/pages/{id}'): (seed_page, None, None),
    ('POST', '/pages/{id}/delete'): (seed_page, None, None),
    ('GET', '/schedules'): (None, None, None),
    ('POST', '/schedules'): (seed_repo, SCHEDULE_BODY, None),
    ('GET', '/findings'): (None, None, None),
    ('GET', '/finding_marks'): (None, None, None),
    ('POST', '/finding_marks'): (None, MARK_BODY, None),
    ('POST', '/finding_marks/{id}/delete'): (seed_mark, None, None),
    ('POST', '/approvals'): (None, ADD_BODY, None),
    ('POST', '/approvals/{id}/decide'): (seed_approval, {'decision': 'approve', 'if_version': 1},
                                         None),
    ('POST', '/approvals/{id}/consume'): (lambda s: seed_approval(s, ap.APPROVE),
                                          {'commit': 'c' * 40, 'if_version': 1}, None),
    ('POST', '/approvals/{id}/delete'): (seed_approval, {'from': 'pm', 'if_version': 1}, None),
    ('GET', '/approvals/{id}/verify'): (lambda s: seed_approval(s, ap.APPROVE), None,
                                        {'repo': URL, 'head': 'a' * 40, 'files': '{}'}),
    ('GET', '/backlog'): (None, None, None),
    ('GET', '/completed'): (None, None, None),
    ('GET', '/records/{repo}/new'): (None, None, None),
    ('GET', '/records/{repo}/{id}'): (None, None, None),
    ('POST', '/records/{repo}'): (None, {'id': 'A', 'summary': 's', 'scope': ['x']}, None),
    ('POST', '/records/{repo}/{id}/edit'): (None, {'version': 'v', 'changes': {'effort': 's'}},
                                            None),
    ('POST', '/records/{repo}/{id}/delete'): (None, {'why': 'w', 'confirm': 'X-1'}, None),
    ('POST', '/records/{repo}/{id}/unblock'): (None, {}, None),
    ('GET', '/notices'): (None, None, None),
    ('GET', '/repos'): (None, None, None),
    ('GET', '/usage'): (None, None, None),
    ('GET', '/agents'): (None, None, None),
    ('GET', '/skills'): (None, None, None),
    ('GET', '/skills/{name}'): (None, None, None),
    ('GET', '/skills/{name}/source'): (None, None, None),
    ('GET', '/skill_edits'): (None, None, None),
    ('POST', '/skill_edits'): (None, {'skill': 'builder', 'text': SKILL_EDIT}, None),
    ('GET', '/skill_edits/{id}'): (seed_skill_edit, None, None),
    ('POST', '/skill_edits/{id}/edit'): (seed_skill_edit, {'text': SKILL_EDIT, 'if_version': 1},
                                         None),
    ('POST', '/skill_edits/{id}/withdraw'): (seed_skill_edit, None, None),
    ('POST', '/skill_edits/{id}/done'): (
        lambda s: seed_skill_edit(s, 'submitted'),
        {'branch': 'kit/x', 'blob': skilledits.git_blob(SKILL_EDIT.encode('utf-8')),
         'if_version': 1}, None),
    ('POST', '/skill_edits/{id}/fail'): (lambda s: seed_skill_edit(s, 'submitted'),
                                         {'why': 'w', 'if_version': 1}, None),
    ('GET', '/pending'): (None, None, None),
    ('GET', '/wait'): (None, None, {'timeout': '0'}),
    ('GET', '/snapshot'): (None, None, None),
    ('GET', '/health'): (None, None, None),
    ('GET', '/lock/view'): (None, None, None),
    ('GET', '/lock/stats'): (None, None, {'days': '7'}),
    ('GET', '/lock/wait'): (None, None, {'since': '6', 'timeout': '0'}),
    ('POST', '/lock/holds'): (None, {'kind': 'reserve', 'scope': 'whole',
                                     'start': '2026-10-08T12:00:00Z',
                                     'end': '2026-10-08T13:00:00Z'}, None),
    ('POST', '/lock/holds/{id}/edit'): (None, {'note': 'n'}, None),
    ('POST', '/lock/holds/{id}/end'): (None, {}, None),
    ('POST', '/lock/division'): (None, {'defaults': True}, None),
    ('POST', '/lock/runs/{id}/end'): (None, {'confirm': 'X-1'}, None),
    ('POST', '/lock/runs/{id}/top'): (None, {}, None),
    ('POST', '/lock/game_mode'): (None, {'enabled': True}, None),
    ('POST', '/lock/games'): (None, {'text': 'Game.exe'}, None),
}


REPO = 'r'


def key(route):
    return route.method, route.pattern[len(BASE):]


def row(route, role):
    """(seed, body, query) of route for the caller role."""
    r = ROWS[key(route)]
    return r.get(role, r[OWNER]) if isinstance(r, dict) else r


def allowed(route, role, via='', strong=False):
    """Whether a caller of role (via that listener, strong or not) passes route's role."""
    if route.role == ANY:
        return True
    if route.role == APPROVER:
        return role == OWNER and strong is True
    if route.role == LOCK_WRITE:
        return role == OWNER and (strong is True or via != api.TUNNEL_VIA)
    return route.role == role


def path(route, doc_id):
    """route.pattern with its `{id}` and `{repo}` segments filled in."""
    return route.pattern.replace('{id}', doc_id).replace('{repo}', REPO).replace('{name}', 'builder')


class RolesCase(unittest.TestCase):
    def fresh(self, seed):
        root = tempfile.mkdtemp(prefix='kit-hubapi-')
        self.addCleanup(shutil.rmtree, root, True)
        store = Store.open(root, clock=CLOCK)
        self.addCleanup(store.close)
        doc_id = seed(store) if seed else 'X-1'
        return store, api.Api(store, views=Views(), clock=CLOCK, approval_key=lambda: KEY,
                              checkout=lambda repo: GIT[0].root, lock=LockDoor(),
                              schedule_repos=lambda: [('r', root, 'r')],
                              tick_seen=lambda: None, tick_state=lambda: {},
                              kit_blob=lambda branch, path: skilledits.git_blob(
                                  SKILL_EDIT.encode('utf-8'))), doc_id

    def test_every_route_has_a_role_and_a_row(self):
        self.assertTrue(api.ROUTES)
        for route in api.ROUTES:
            with self.subTest(route=route.method + ' ' + route.pattern):
                self.assertIn(route.role, (OWNER, AGENT, ANY, APPROVER, LOCK_WRITE))
                self.assertTrue(route.pattern.startswith(BASE + '/'))
                self.assertTrue(callable(route.handler))
                self.assertIn(key(route), ROWS, 'a route with no row in the role table')
        self.assertEqual(len({key(r) for r in api.ROUTES}), len(api.ROUTES), 'a route twice')
        self.assertEqual(sorted(ROWS), sorted(key(r) for r in api.ROUTES),
                         'a row for a route that is gone')

    def test_one_row_per_route_and_role(self):
        for route in api.ROUTES:
            for role in (OWNER, AGENT):
                seed, body, query = row(route, role)
                with self.subTest(route=route.method + ' ' + route.pattern, role=role):
                    store, a, doc_id = self.fresh(seed)
                    before = store.snapshot()
                    status, obj = a.call(route.method, path(route, doc_id), role, body, query)
                    if allowed(route, role):
                        self.assertEqual((status, obj.get('error')), (200, None))
                    else:
                        self.assertEqual(status, 403)
                        self.assertEqual(obj['error']['code'], 'forbidden')
                        self.assertIs(obj['ok'], False)
                        self.assertEqual(store.snapshot(), before, 'a refused call wrote')

    def test_the_roles_the_design_names(self):
        want = {('POST', '/questions'): AGENT, ('POST', '/questions/{id}/answer'): OWNER,
                ('POST', '/questions/{id}/log'): AGENT, ('POST', '/questions/{id}/done'): AGENT,
                ('POST', '/questions/file'): AGENT, ('POST', '/questions/bind'): AGENT,
                ('POST', '/todos'): ANY,
                ('POST', '/todos/{id}/done'): OWNER, ('POST', '/todos/{id}/ack'): AGENT,
                ('POST', '/questions/{id}/edit'): AGENT, ('POST', '/questions/{id}/delete'): ANY,
                ('POST', '/todos/{id}/edit'): ANY, ('POST', '/todos/{id}/delete'): ANY,
                ('POST', '/stories'): OWNER, ('POST', '/stories/{id}'): AGENT,
                ('POST', '/stories/{id}/withdraw'): OWNER, ('POST', '/to_pm'): OWNER,
                ('POST', '/to_pm/send'): OWNER, ('POST', '/to_pm/{id}/reply'): AGENT,
                ('POST', '/to_pm/{id}/edit'): OWNER, ('POST', '/to_pm/{id}/delete'): OWNER,
                ('POST', '/inbox'): AGENT, ('POST', '/inbox/{id}/read'): OWNER,
                # a landing's event is posted by tools/land.py, an agent; the owner only reads it
                ('POST', '/landings'): AGENT,
                ('POST', '/pages'): AGENT, ('POST', '/pages/{id}/delete'): OWNER,
                # the owner sets the kit's automatic tasks; an agent only reads them
                ('GET', '/schedules'): ANY, ('POST', '/schedules'): OWNER,
                ('POST', '/records/{repo}'): OWNER, ('POST', '/records/{repo}/{id}/edit'): OWNER,
                ('POST', '/records/{repo}/{id}/delete'): OWNER,
                ('POST', '/records/{repo}/{id}/unblock'): OWNER,
                # the maintenance lint's findings: anyone reads them; only the owner marks one
                ('GET', '/findings'): ANY, ('GET', '/finding_marks'): ANY,
                ('POST', '/finding_marks'): OWNER, ('POST', '/finding_marks/{id}/delete'): OWNER,
                # a skill edit's text is the owner's alone; an agent only commits it and says so
                ('POST', '/skill_edits'): OWNER, ('POST', '/skill_edits/{id}/edit'): OWNER,
                ('POST', '/skill_edits/{id}/withdraw'): OWNER,
                ('POST', '/skill_edits/{id}/done'): AGENT,
                ('POST', '/skill_edits/{id}/fail'): AGENT,
                ('POST', '/approvals'): AGENT, ('POST', '/approvals/{id}/decide'): APPROVER,
                ('POST', '/approvals/{id}/consume'): AGENT,
                ('POST', '/approvals/{id}/delete'): AGENT,
                ('GET', '/approvals/{id}/verify'): AGENT,
                # the machine lock's reads are the owner's: an agent reads the lock through its
                # own MCP server, never through the hub (the owner's decision)
                ('GET', '/lock/view'): OWNER, ('GET', '/lock/stats'): OWNER,
                ('GET', '/lock/wait'): OWNER,
                # the lock's writes: on this machine the owner's login, over the tunnel the
                # strong owner's; a reservation (any hold write) and the division, which that
                # decision does not name, are the strong owner's everywhere
                ('POST', '/lock/holds'): APPROVER, ('POST', '/lock/holds/{id}/edit'): APPROVER,
                ('POST', '/lock/holds/{id}/end'): APPROVER, ('POST', '/lock/division'): APPROVER,
                ('POST', '/lock/runs/{id}/end'): LOCK_WRITE,
                ('POST', '/lock/runs/{id}/top'): LOCK_WRITE,
                ('POST', '/lock/game_mode'): LOCK_WRITE, ('POST', '/lock/games'): LOCK_WRITE}
        have = {key(r): r.role for r in api.ROUTES}
        for k, role in sorted(want.items()):
            with self.subTest(route=k):
                self.assertEqual(have.get(k), role)
        for k, role in sorted(have.items()):
            if k in want:
                continue
            if k[0] == 'GET':
                with self.subTest(route=k):
                    self.assertEqual(role, ANY)
            else:
                self.assertIn(k, want, 'a write route the role table does not pin')

    def test_an_agent_cannot_set_a_schedule(self):
        # a good body for a known repo, by any listener, strong or not: the role alone decides
        for via, strong in (('', False), ('cli', False), (api.TUNNEL_VIA, False),
                            (api.TUNNEL_VIA, True)):
            with self.subTest(via=via, strong=strong):
                store, a, _ = self.fresh(seed_repo)
                before = store.snapshot()
                status, obj = a.call('POST', BASE + '/schedules', AGENT, dict(SCHEDULE_BODY),
                                     via=via, strong=strong)
                self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                                 (403, 'forbidden', 'role'))
                self.assertEqual(store.snapshot(), before, 'a refused call wrote')
                self.assertEqual(store.docs('schedules'), {})
                # and it reads them
                status, obj = a.call('GET', BASE + '/schedules', AGENT)
                self.assertEqual((status, obj['items']), (200, []))

    def test_an_agent_cannot_mark_a_finding_unblock_a_record_or_pass_one_on(self):
        # every write of the lint findings slice, by any listener, strong or not, with a body
        # the owner's call takes: the role alone decides, and nothing is written
        writes = (('/finding_marks', None, MARK_BODY),
                  ('/finding_marks/%s/delete', seed_mark, None),
                  ('/records/r/X-1/unblock', None, {'on': 'owner', 'reason': 'r'}),
                  ('/to_pm', None, ACT_BODY))
        for via, strong in (('', False), ('cli', False), (api.TUNNEL_VIA, False),
                            (api.TUNNEL_VIA, True)):
            for where, seed, body in writes:
                with self.subTest(via=via, strong=strong, path=where):
                    store, a, doc_id = self.fresh(seed)
                    before = store.snapshot()
                    path = BASE + (where % doc_id if '%s' in where else where)
                    status, obj = a.call('POST', path, AGENT, dict(body or {}), via=via,
                                         strong=strong)
                    self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                                     (403, 'forbidden', 'role'))
                    self.assertEqual(store.snapshot(), before, 'a refused call wrote')
                    # the owner's same call is answered
                    store, a, doc_id = self.fresh(seed)
                    status, obj = a.call('POST', path, OWNER, dict(body or {}), via=via)
                    self.assertEqual((status, obj.get('error')), (200, None))
        # and an agent reads both lists
        _, a, _ = self.fresh(seed_mark)
        for where in ('/findings', '/finding_marks'):
            status, obj = a.call('GET', BASE + where, AGENT)
            self.assertEqual(status, 200, obj)

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
            seed, body, query = row(route, AGENT)
            for role in (None, '', ANY, 'Owner', 'OWNER', 'owner ', 'admin', True, 1,
                         ['owner'], ('owner',), {'owner': 1}):
                with self.subTest(route=route.method + ' ' + route.pattern, role=repr(role)):
                    store, a, doc_id = self.fresh(seed)
                    before = store.snapshot()
                    status, obj = a.call(route.method, path(route, doc_id), role, body, query)
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


# a value of `strong` that is not exactly True: none of them makes a caller strong
NOT_TRUE = (False, None, 0, 1, '', 'true', 'yes', [True], (True,), {'strong': True})


class ApproverRoleTest(unittest.TestCase):
    """APPROVER: a route for the owner who logged in through Cloudflare Access. Each test adds a
    route of its own, so the role's rule is tested apart from the approvals decide route's."""

    fresh = RolesCase.fresh

    def approver_route(self, calls):
        def handler(a, req):
            calls.append(req)
            return {'ok': True, 'strong': req.strong}
        return api.Route('POST', BASE + '/decide-test', api.APPROVER, handler)

    def call(self, role, strong, body=None, **kw):
        calls = []
        routes = api.ROUTES + (self.approver_route(calls),)
        with mock.patch.object(api, 'ROUTES', routes):
            _, a, _ = self.fresh(None)
            if strong is _ABSENT:
                status, obj = a.call('POST', BASE + '/decide-test', role, body, **kw)
            else:
                status, obj = a.call('POST', BASE + '/decide-test', role, body, strong=strong,
                                     **kw)
        return status, obj, calls

    def test_approver_is_a_route_role_and_never_a_caller(self):
        self.assertIn(api.APPROVER, api.ROLES)
        self.assertNotIn(api.APPROVER, api.CALLERS)
        self.assertNotIn(api.APPROVER, (OWNER, AGENT, ANY))
        status, obj, calls = self.call(api.APPROVER, True)
        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
        self.assertEqual(calls, [])

    def test_only_the_strong_owner_reaches_an_approver_route(self):
        status, obj, calls = self.call(OWNER, True)
        self.assertEqual(status, 200, obj)
        self.assertEqual((obj['ok'], obj['strong']), (True, True))
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].strong, True)
        self.assertEqual(calls[0].role, OWNER)

    def test_the_owner_who_is_not_strong_is_refused(self):
        for strong in NOT_TRUE + (_ABSENT,):
            for words in ({}, {'agent_words': True}):
                with self.subTest(strong=repr(strong), words=words):
                    status, obj, calls = self.call(OWNER, strong, {'x': 1}, **words)
                    self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                                     (403, 'forbidden', 'role'))
                    self.assertIn('Access', obj['error']['message'])
                    self.assertEqual(calls, [], 'the handler ran for a caller who is not strong')

    def test_an_agent_is_refused_strong_or_not(self):
        for strong in (True,) + NOT_TRUE + (_ABSENT,):
            with self.subTest(strong=repr(strong)):
                status, obj, calls = self.call(AGENT, strong)
                self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
                self.assertEqual(calls, [])

    def test_strong_opens_no_other_route(self):
        # strong adds nothing to what a role may do on an OWNER, AGENT or ANY route
        _, a, _ = self.fresh(None)
        status, obj = a.call('POST', BASE + '/questions', OWNER,
                             {'title': 't', 'options': OPTIONS}, strong=True)
        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
        store, a, doc_id = self.fresh(seed_todo)
        status, obj = a.call('POST', BASE + '/todos/%s/done' % doc_id, AGENT, {'done': True},
                             strong=True)
        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
        self.assertEqual(store.get('todos', doc_id)['status'], 'open')
        # and a caller who is not strong keeps every route it had
        for strong in (True, False):
            store, a, doc_id = self.fresh(seed_todo)
            status, obj = a.call('POST', BASE + '/todos/%s/done' % doc_id, OWNER,
                                 {'done': True}, strong=strong)
            self.assertEqual((status, obj.get('error')), (200, None))

    def test_strong_is_never_read_from_the_body_or_the_query(self):
        for body, query in (({'strong': True}, None), (None, {'strong': '1'}),
                            ({'strong': True}, {'strong': 'true'})):
            with self.subTest(body=body, query=query):
                status, obj, calls = self.call(OWNER, _ABSENT, body, query=query)
                self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
                self.assertEqual(calls, [])

    def test_every_handler_sees_whether_its_caller_is_strong(self):
        seen = []

        def handler(a, req):
            seen.append(req.strong)
            return {'ok': True}
        routes = api.ROUTES + (api.Route('GET', BASE + '/strong-test', ANY, handler),)
        with mock.patch.object(api, 'ROUTES', routes):
            _, a, _ = self.fresh(None)
            for role, strong in ((OWNER, True), (OWNER, False), (AGENT, True), (OWNER, 1)):
                a.call('GET', BASE + '/strong-test', role, strong=strong)
            a.call('GET', BASE + '/strong-test', OWNER)
        self.assertEqual(seen, [True, False, False, False, False])


_ABSENT = object()                      # the call names no `strong` at all


if __name__ == '__main__':
    unittest.main()
