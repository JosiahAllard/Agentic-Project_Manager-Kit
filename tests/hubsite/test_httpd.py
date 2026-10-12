"""kitlib.hubsite.httpd: the loopback listener. Every test binds port 0 on the loopback address,
talks to it over a real socket, and stops what it started."""
import http.client
import json
import os
import socket
import threading
import unittest
from unittest import mock

from kitlib.hubsite import api, auth, httpd
from kitlib.hubsite.api import AGENT, ANY, BASE, OWNER
from tests.hubsite import httpfix
from tests.hubsite import test_api_roles as roles
from tests.hubsite.httpfix import TIMEOUT, get, post, send

HEALTH = BASE + '/health'
TODOS = BASE + '/todos'


class Recorder(object):
    """An Api that records what the listener passed it."""

    def __init__(self):
        self.calls = []
        self.raised = []            # the raise_unexpected of each call

    def call(self, method, path, role, body=None, query=None, via='', raise_unexpected=False):
        self.calls.append({'method': method, 'path': path, 'role': role, 'body': body,
                           'query': query, 'via': via})
        self.raised.append(raise_unexpected)
        return 200, {'ok': True}


class Broken(object):
    def call(self, *a, **kw):
        raise RuntimeError('boom in D:\\secret\\place')


# ------------------------------------------------------------------ the address
class BindCase(httpfix.ListenerCase):
    def test_it_answers_on_the_port_it_bound(self):
        lis = self.listener()
        self.assertGreater(lis.port, 0)
        self.assertEqual(lis.url, 'http://127.0.0.1:%d' % lis.port)
        r = get(lis.port, HEALTH, self.owner)
        self.assertEqual(r.status, 200)
        self.assertIs(r.json()['ok'], True)
        self.assertEqual(r.header('Content-Type'), ['application/json'])

    def test_only_the_loopback_address_is_ever_bound(self):
        for host in ('0.0.0.0', '', 'localhost', '::1', '::', '127.0.0.2', '192.0.2.1',
                     'hub.example', None, 0):
            with self.subTest(host=host):
                with self.assertRaises(httpd.BindRefused):
                    httpd.Listener((host, 0), self.api, None)
        for addr in (None, 'x', (httpd.LOOPBACK,), (httpd.LOOPBACK, '0'), (httpd.LOOPBACK, -1),
                     (httpd.LOOPBACK, 65536), (httpd.LOOPBACK, True), (httpd.LOOPBACK, 0, 0)):
            with self.subTest(addr=addr):
                with self.assertRaises(httpd.BindRefused):
                    httpd.Listener(addr, self.api, None)

    def test_a_taken_port_is_refused_at_once_started_or_not(self):
        for start in (False, True):
            with self.subTest(started=start):
                lis = self.listener(start=start)
                with self.assertRaises(httpd.BindRefused) as c:
                    httpd.Listener((httpd.LOOPBACK, lis.port), self.api, None)
                self.assertIn('127.0.0.1:%d' % lis.port, str(c.exception))

    def test_a_stopped_listener_frees_its_port(self):
        lis = self.listener()
        port = lis.port
        self.assertEqual(get(port, HEALTH, self.owner).status, 200)
        self.assertTrue(lis.stop(TIMEOUT))
        again = httpd.Listener((httpd.LOOPBACK, port), self.api, None)
        self.addCleanup(again.stop, TIMEOUT)
        self.assertEqual(again.port, port)

    def test_start_and_stop_are_safe_to_repeat(self):
        lis = self.listener(start=False)
        self.assertTrue(lis.start())
        self.assertFalse(lis.start())
        self.assertTrue(lis.stop(TIMEOUT))
        self.assertTrue(lis.stop(TIMEOUT))
        self.assertFalse(lis.start(), 'a closed listener started again')

    def test_a_listener_that_never_started_stops_without_hanging(self):
        lis = self.listener(start=False)
        self.assertTrue(lis.stop(TIMEOUT))
        self.assertEqual(httpfix.http_threads(), [])


# ------------------------------------------------------------------ the gate and the host
class GateCase(httpfix.ListenerCase):
    def test_the_token_decides_the_role(self):
        lis = self.listener()
        owner = get(lis.port, HEALTH, self.owner)
        agent = get(lis.port, HEALTH, self.agent)
        self.assertEqual((owner.status, agent.status), (200, 200))
        self.assertIn('store', owner.json())                # the owner's alone (api.health)
        self.assertNotIn('store', agent.json())
        for token in (None, 'x' * 43, self.owner[:-1], self.owner + 'x'):
            with self.subTest(token=token):
                r = get(lis.port, HEALTH, token)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))

    def test_the_gate_sees_the_header_message_itself(self):
        seen = []

        def gate(request):
            seen.append(request.headers)
            return OWNER
        lis = self.listener(gate=gate)
        send(lis.port, 'GET', HEALTH, [('X-Twice', 'a'), ('X-Twice', 'b')])
        self.assertIsInstance(seen[0], http.client.HTTPMessage)
        self.assertEqual(seen[0].get_all('X-Twice'), ['a', 'b'])

    def test_a_doubled_authorization_header_gives_no_role(self):
        lis = self.listener()
        r = send(lis.port, 'GET', HEALTH, [httpfix.bearer(self.owner), httpfix.bearer(self.owner)])
        self.assertEqual((r.status, r.code()), (403, 'forbidden'))

    def test_the_gate_runs_before_anything_else_for_every_method(self):
        seen = []
        rec = Recorder()

        def gate(request):
            seen.append((request.method, request.path, request.role, request.body))
            return httpd.refusal(403, 'forbidden', 'the gate said no', 'gate')
        static = os.path.join(self.root, 'static')
        os.makedirs(static)
        with open(os.path.join(static, 'index.html'), 'w') as f:
            f.write('<p>page</p>')
        lis = self.listener(rec, gate=gate, static_dir=static,
                            routes={('GET', '/special'): lambda request: self.fail('routed')})
        sent = []
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'BREW'):
            for path in (HEALTH, '/', '/special', '/nothing/here', TODOS + '?x=1'):
                with self.subTest(method=method, path=path):
                    # a write that breaks every write rule: the gate must still answer first
                    body = b'{not json' if method in ('POST', 'PUT', 'PATCH') else None
                    r = send(lis.port, method, path, [('Content-Type', 'text/plain')], body,
                             host='evil.example')
                    sent.append((method, path.partition('?')[0], None, None))
                    self.assertEqual(r.status, 403)
                    if method != 'HEAD':
                        self.assertEqual(r.json()['error']['field'], 'gate')
        self.assertEqual(seen, sent)
        self.assertEqual(rec.calls, [])
        self.assertEqual(self.errors(), [])

    def test_a_listener_with_no_gate_refuses_everything(self):
        rec = Recorder()
        lis = self.listener(rec, gate=None)
        for method, path in (('GET', HEALTH), ('GET', '/'), ('POST', TODOS)):
            with self.subTest(method=method, path=path):
                r = send(lis.port, method, path, [httpfix.bearer(self.owner)])
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertEqual(rec.calls, [])

    def test_a_gate_that_raises_lets_nothing_through(self):
        rec = Recorder()

        def gate(request):
            raise RuntimeError('gate broke in D:\\secret\\place')
        lis = self.listener(rec, gate=gate)
        r = get(lis.port, HEALTH, self.owner)
        self.assertEqual((r.status, r.code()), (500, 'internal'))
        self.assertEqual(r.json()['error']['message'], httpd.INTERNAL)
        self.assertNotIn(b'secret', r.body)
        self.assertEqual(rec.calls, [])
        self.assertEqual(len(self.errors()), 1)
        self.assertIn('gate broke', self.errors()[0])       # the log, and only the log

    def test_a_gate_answer_that_is_no_role_is_no_role(self):
        for answer in (None, '', ANY, 'Owner', 'admin', True, 1, ['owner'], ('owner',)):
            with self.subTest(answer=repr(answer)):
                rec = Recorder()
                lis = self.listener(rec, gate=lambda request, a=answer: a)
                send(lis.port, 'GET', HEALTH)
                self.assertEqual([c['role'] for c in rec.calls], [None])
                lis.close_door()
        # and the real Api refuses a caller with no role
        lis = self.listener(gate=lambda request: None)
        r = get(lis.port, HEALTH, self.owner)
        self.assertEqual((r.status, r.code()), (403, 'forbidden'))

    def test_the_role_and_the_via_reach_the_api(self):
        rec = Recorder()
        lis = self.listener(rec, via='relay:test')
        get(lis.port, BASE + '/backlog?repo=kit&fresh=1&repo=other', self.agent)
        post(lis.port, TODOS, {'title': 't'}, self.owner, via='web')
        post(lis.port, TODOS, None, self.owner)
        self.assertEqual(rec.calls, [
            {'method': 'GET', 'path': BASE + '/backlog', 'role': AGENT, 'body': None,
             'query': {'repo': 'kit', 'fresh': '1'}, 'via': 'relay:test'},
            {'method': 'POST', 'path': TODOS, 'role': OWNER, 'body': {'title': 't'},
             'query': {}, 'via': 'web'},
            {'method': 'POST', 'path': TODOS, 'role': OWNER, 'body': None, 'query': {},
             'via': 'relay:test'}])

    def test_a_via_that_is_no_plain_token_is_not_passed_on(self):
        rec = Recorder()
        lis = self.listener(rec)
        for said in ('WEB', 'we b', 'web\tx', '-web', 'w' * 41, '', 'web;drop'):
            with self.subTest(via=said):
                post(lis.port, TODOS, None, self.owner, headers=[(httpd.VIA_HEADER, said)])
                self.assertEqual(rec.calls[-1]['via'], '')
        post(lis.port, TODOS, None, self.owner,
             headers=[(httpd.VIA_HEADER, 'web'), (httpd.VIA_HEADER, 'cli')])
        self.assertEqual(rec.calls[-1]['via'], '', 'a doubled header named the transport')
        for said in ('web', 'cli', 'desktop-mcp', 'relay:phone_1.2'):
            with self.subTest(via=said):
                post(lis.port, TODOS, None, self.owner, via=said)
                self.assertEqual(rec.calls[-1]['via'], said)

    def test_the_via_is_recorded_on_the_document(self):
        lis = self.listener()
        tid = post(lis.port, TODOS, {'title': 't'}, self.owner, via='web').json()['id']
        self.assertEqual(self.store.get('todos', tid)['via'], 'web')
        r = post(lis.port, '%s/%s/ack' % (TODOS, tid), None, self.agent, via='cli')
        self.assertEqual(r.status, 200)
        self.assertEqual(self.store.get('todos', tid)['via'], 'cli')
        tid = post(lis.port, TODOS, {'title': 't'}, self.owner).json()['id']
        self.assertEqual(self.store.get('todos', tid)['via'], 'api')

    def test_only_this_listeners_own_host_names_are_answered(self):
        lis = self.listener()
        port = lis.port
        for host in ('127.0.0.1:%d' % port, 'localhost:%d' % port, 'LOCALHOST:%d' % port):
            with self.subTest(host=host):
                r = send(port, 'GET', HEALTH, [httpfix.bearer(self.owner)], host=host)
                self.assertEqual(r.status, 200)
        for host in ('evil.example', 'evil.example:%d' % port, '127.0.0.1', 'localhost',
                     '127.0.0.1:%d' % (port + 1), '127.0.0.1:%d.evil.example' % port,
                     'localhost.evil.example:%d' % port, '[::1]:%d' % port, '0.0.0.0:%d' % port,
                     ' ', False):
            with self.subTest(host=host):
                for path in (HEALTH, '/'):
                    r = send(port, 'GET', path, [httpfix.bearer(self.owner)], host=host)
                    self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                    self.assertEqual(r.json()['error']['field'], 'host')
        r = send(port, 'GET', HEALTH, [httpfix.bearer(self.owner), ('Host', 'evil.example')])
        self.assertEqual(r.status, 403, 'a second Host header was let through')

    def test_local_hosts(self):
        self.assertTrue(httpd.local_hosts('127.0.0.1:8790', 8790))
        self.assertTrue(httpd.local_hosts('LocalHost:8790', 8790))
        for host in (None, '', '127.0.0.1', '127.0.0.1:8791', 'localhost:08790', b'localhost:8790',
                     'localhost:8790 ', 'user@localhost:8790'):
            self.assertFalse(httpd.local_hosts(host, 8790), host)

    def test_other_methods_are_refused_and_nothing_allows_another_origin(self):
        rec = Recorder()
        lis = self.listener(rec)
        for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS', 'TRACE', 'CONNECT', 'BREW'):
            with self.subTest(method=method):
                r = send(lis.port, method, TODOS, httpfix.write_headers(lis.port, self.owner)
                         + [('Access-Control-Request-Method', 'POST')])
                self.assertEqual((r.status, r.code()), (405, 'invalid'))
                self.assertEqual(r.header('Allow'), ['GET, HEAD, POST'])
                self.assertEqual([n for n, _ in r.headers if n.lower().startswith('access-')], [])
        self.assertEqual(rec.calls, [])

    def test_every_answer_closes_and_says_little(self):
        lis = self.listener()
        for r in (get(lis.port, HEALTH, self.owner), get(lis.port, HEALTH), get(lis.port, '/'),
                  post(lis.port, TODOS, {'title': 't'}, self.owner),
                  send(lis.port, 'BREW', '/'), send(lis.port, 'GET', '/', host='evil.example')):
            with self.subTest(status=r.status):
                names = sorted(n.lower() for n, _ in r.headers)
                self.assertEqual(names, sorted([
                    'cache-control', 'connection', 'content-length', 'content-type',
                    'cross-origin-resource-policy', 'referrer-policy', 'x-content-type-options',
                    'x-frame-options'] + (['allow'] if r.status == 405 else [])))
                self.assertEqual(r.header('Connection'), ['close'])
                self.assertEqual(r.header('X-Content-Type-Options'), ['nosniff'])
                self.assertEqual(r.header('Cache-Control'), ['no-store'])
                self.assertEqual(r.header('Content-Length'), [str(len(r.body))])


# ------------------------------------------------------------------ writes
class WriteCase(httpfix.ListenerCase):
    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.lis = self.listener()
        self.port = self.lis.port
        self.body = json.dumps({'title': 't'}).encode('utf-8')

    def write(self, headers, body=None, host=None, path=TODOS):
        before = self.store.revision
        r = send(self.port, 'POST', path, [httpfix.bearer(self.owner)] + headers,
                 self.body if body is None else body, host=host)
        if r.status != 200:
            self.assertEqual(self.store.revision, before, 'a refused write wrote')
        return r

    def good(self, host=None):
        return [('Content-Type', 'application/json'),
                ('Origin', 'http://' + (host or '127.0.0.1:%d' % self.port))]

    def test_a_write_with_the_right_headers_is_written(self):
        r = self.write(self.good())
        self.assertEqual(r.status, 200)
        self.assertEqual(self.store.get('todos', r.json()['id'])['title'], 't')
        r = self.write([('Content-Type', 'Application/JSON; charset=utf-8'), self.good()[1]])
        self.assertEqual(r.status, 200)
        host = 'localhost:%d' % self.port
        self.assertEqual(self.write(self.good(host), host=host).status, 200)

    def test_the_content_type_must_be_json(self):
        origin = self.good()[1]
        for ctype in (None, 'text/plain', 'application/x-www-form-urlencoded',
                      'multipart/form-data; boundary=x', 'application/jsonx', 'json', ''):
            with self.subTest(ctype=ctype):
                r = self.write([origin] + ([('Content-Type', ctype)] if ctype is not None else []))
                self.assertEqual((r.status, r.code()), (415, 'invalid'))
                self.assertEqual(r.json()['error']['field'], 'content-type')
        r = self.write(self.good() + [('Content-Type', 'application/json')])
        self.assertEqual(r.status, 415, 'a doubled Content-Type was let through')

    def test_the_origin_must_name_the_host_the_request_came_to(self):
        ctype = self.good()[0]
        port = self.port
        for origin in (None, 'null', '', 'http://evil.example', 'http://evil.example:%d' % port,
                       'http://127.0.0.1', 'http://127.0.0.1:%d' % (port + 1),
                       'http://localhost:%d' % port,          # not the Host this request names
                       'http://127.0.0.1:%d.evil.example' % port,
                       'http://evil.example@127.0.0.1:%d' % port,
                       'ftp://127.0.0.1:%d' % port, '127.0.0.1:%d' % port,
                       '//127.0.0.1:%d' % port, 'http://[::1'):
            with self.subTest(origin=origin):
                r = self.write([ctype] + ([('Origin', origin)] if origin is not None else []))
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                self.assertEqual(r.json()['error']['field'], 'origin')
        r = self.write(self.good() + [('Origin', 'http://evil.example')])
        self.assertEqual(r.status, 403, 'a doubled Origin was let through')

    def test_the_origin_scheme_is_the_listeners_own_and_no_other(self):
        ctype = self.good()[0]
        # the local listener speaks http only: an https page is another origin
        r = self.write([ctype, ('Origin', 'https://127.0.0.1:%d' % self.port)])
        self.assertEqual((r.status, r.json()['error']['field']), (403, 'origin'))
        self.assertEqual(self.lis.origin_scheme, 'http')
        # a listener behind a TLS front takes https and nothing else
        front = self.listener(origin_scheme='https')
        head = [httpfix.bearer(self.owner), ctype]
        before = self.store.revision
        for scheme in ('http', 'HTTP', 'ftp'):
            with self.subTest(scheme=scheme):
                r = send(front.port, 'POST', TODOS,
                         head + [('Origin', '%s://127.0.0.1:%d' % (scheme, front.port))], self.body)
                self.assertEqual((r.status, r.json()['error']['field']), (403, 'origin'))
        self.assertEqual(self.store.revision, before)
        r = send(front.port, 'POST', TODOS,
                 head + [('Origin', 'https://127.0.0.1:%d' % front.port)], self.body)
        self.assertEqual(r.status, 200)
        # a scheme that is neither is no listener at all: it could only refuse or wave through
        for bad in ('', 'ftp', 'HTTPS', None, 'http,https'):
            with self.subTest(origin_scheme=bad):
                with self.assertRaises(ValueError):
                    httpd.Listener((httpd.LOOPBACK, 0), self.api, None, origin_scheme=bad)

    def test_a_session_cookie_does_not_carry_a_write_from_another_origin(self):
        code = post(self.port, httpd.PAIR_ROUTE, None, self.owner).json()['path']
        cookie = get(self.port, code).header('Set-Cookie')[0].split(';')[0]
        before = self.store.revision
        headers = [('Cookie', cookie), ('Content-Type', 'application/json')]
        r = send(self.port, 'POST', TODOS, headers + [('Origin', 'http://evil.example')], self.body)
        self.assertEqual((r.status, r.json()['error']['field']), (403, 'origin'))
        r = send(self.port, 'POST', TODOS, headers, self.body)
        self.assertEqual(r.status, 403)
        r = send(self.port, 'POST', TODOS, [('Cookie', cookie), ('Content-Type', 'text/plain'),
                                            ('Origin', 'http://127.0.0.1:%d' % self.port)],
                 self.body)
        self.assertEqual(r.status, 415)
        self.assertEqual(self.store.revision, before)
        r = send(self.port, 'POST', TODOS, headers + [self.good()[1]], self.body)
        self.assertEqual(r.status, 200)

    def test_a_write_needs_a_length_and_the_length_has_a_cap(self):
        r = send(self.port, 'POST', TODOS, [httpfix.bearer(self.owner)] + self.good())
        self.assertEqual((r.status, r.json()['error']['field']), (411, 'content-length'))
        r = self.write(self.good() + [('Transfer-Encoding', 'chunked')])
        self.assertEqual(r.status, 411)
        for bad in ('abc', '-1', '1.5', '0x10', '1 1', ''):
            with self.subTest(length=bad):
                r = send(self.port, 'POST', TODOS, [httpfix.bearer(self.owner)] + self.good()
                         + [('Content-Length', bad)])
                self.assertEqual((r.status, r.code()), (400, 'invalid'))
        r = send(self.port, 'POST', TODOS, [httpfix.bearer(self.owner)] + self.good()
                 + [('Content-Length', '5'), ('Content-Length', '5')], b'{}   ')
        self.assertEqual(r.status, 411, 'a doubled Content-Length was let through')

    def test_a_body_over_the_cap_is_refused_unread(self):
        lis = self.listener(max_body=64)
        head = [httpfix.bearer(self.owner)] + httpfix.write_headers(lis.port)
        ok = json.dumps({'title': 't' * (64 - len(json.dumps({'title': ''})))}).encode('utf-8')
        self.assertEqual(len(ok), 64)
        self.assertEqual(send(lis.port, 'POST', TODOS, head, ok).status, 200)
        before = self.store.revision
        # the length alone refuses it: no body is sent, and the answer does not wait for one
        r = send(lis.port, 'POST', TODOS, head + [('Content-Length', '65')])
        self.assertEqual((r.status, r.code()), (413, 'invalid'))
        r = send(lis.port, 'POST', TODOS, head + [('Content-Length', '9' * 12)])
        self.assertEqual(r.status, 413)
        self.assertEqual(self.store.revision, before)
        self.assertEqual(httpd.MAX_BODY, 1 << 20)
        self.assertEqual(self.lis.max_body, httpd.MAX_BODY)

    def test_a_body_shorter_than_its_length_is_refused(self):
        head = ('POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n'
                'Content-Type: application/json\r\nOrigin: http://127.0.0.1:%d\r\n'
                'Content-Length: 40\r\n\r\n' % (TODOS, self.port, self.owner, self.port))
        before = self.store.revision
        answer = httpfix.raw(self.port, head.encode('ascii') + b'{"title":')
        self.assertTrue(answer.startswith(b'HTTP/1.1 400 '), answer[:40])
        self.assertIn(b'"field":"body"', answer)
        self.assertEqual(self.store.revision, before)

    def test_a_body_that_is_not_json_is_refused(self):
        for body in (b'{not json', b'\xff\xfe{}', b'{"title": "t"} trailing', b'[' * 200000,
                     b'{"a":' * 50000 + b'1' + b'}' * 50000, b'{"title": NaN}',
                     b'{"title": "t", "n": -Infinity}'):
            with self.subTest(body=body[:20]):
                r = self.write(self.good(), body)
                self.assertEqual((r.status, r.code()), (400, 'invalid'))
                self.assertEqual(r.json()['error']['field'], 'body')
        self.assertEqual(self.errors(), [])

    def test_an_empty_body_is_no_body(self):
        tid = self.write(self.good()).json()['id']
        for body in (b'', b'  \r\n '):
            with self.subTest(body=body):
                r = send(self.port, 'POST', '%s/%s/ack' % (TODOS, tid),
                         httpfix.write_headers(self.port, self.agent), body)
                self.assertEqual(r.status, 200)

    def test_a_json_body_the_api_refuses_is_the_apis_refusal(self):
        for body in (b'[1]', b'"text"', b'7', b'{"nope": 1}'):
            with self.subTest(body=body):
                r = self.write(self.good(), body)
                self.assertEqual((r.status, r.code()), (400, 'invalid'))

    def test_a_refused_write_is_answered_even_when_its_body_was_never_read(self):
        # refused by the host rule, before the body: the refusal still arrives whole
        big = b'{"title": "' + b't' * 200000 + b'"}'
        for _ in range(5):
            r = self.write(self.good(), big, host='evil.example')
            self.assertEqual((r.status, r.json()['error']['field']), (403, 'host'))
        # and so does a refusal for a body over the cap that was sent all the same
        lis = self.listener(max_body=64)
        for _ in range(5):
            r = send(lis.port, 'POST', TODOS, httpfix.write_headers(lis.port, self.owner), big)
            self.assertEqual(r.status, 413)
        self.assertEqual(self.errors(), [])


# ------------------------------------------------------------------ the api behind it
class ApiCase(httpfix.ListenerCase):
    def test_every_route_answers_its_role_and_never_a_500(self):
        lis = self.listener()
        for route in api.ROUTES:
            seed, body, query = roles.ROWS[roles.key(route)]
            for role, token in ((OWNER, self.owner), (AGENT, self.agent)):
                with self.subTest(route=route.method + ' ' + route.pattern, role=role):
                    doc_id = seed(self.store) if seed else 'X-1'
                    path = route.pattern.replace('{id}', doc_id)
                    if query:
                        path += '?' + '&'.join('%s=%s' % kv for kv in sorted(query.items()))
                    if route.method == 'POST':
                        r = post(lis.port, path, body, token)
                    else:
                        r = get(lis.port, path, token)
                    if route.role in (role, ANY):
                        self.assertEqual((r.status, r.json().get('error')), (200, None))
                    else:
                        self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                    self.assertIn('revision', r.json())
        self.assertEqual(self.errors(), [])

    def test_no_route_answers_a_caller_without_a_credential(self):
        lis = self.listener()
        before = self.store.snapshot()
        for route in api.ROUTES:
            with self.subTest(route=route.method + ' ' + route.pattern):
                path = route.pattern.replace('{id}', 'X-1')
                r = post(lis.port, path, {}) if route.method == 'POST' else get(lis.port, path)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertEqual(self.store.snapshot(), before)

    def test_a_missing_view_is_unavailable_never_a_500(self):
        lis = self.listener(api.Api(self.store, views=None, clock=httpfix.CLOCK))
        for name in api.VIEWS:
            with self.subTest(view=name):
                r = get(lis.port, '%s/%s' % (BASE, name), self.agent)
                self.assertEqual((r.status, r.code()), (503, 'unavailable'))
                self.assertEqual(r.json()['error']['field'], 'views')
        snap = get(lis.port, BASE + '/snapshot', self.owner)
        self.assertEqual(snap.status, 200)
        self.assertEqual(sorted(snap.json()['unavailable']), sorted(api.VIEWS))
        health = get(lis.port, HEALTH, self.agent)
        self.assertEqual(health.status, 200)
        self.assertEqual(list(health.json()['unavailable']), ['health'])
        self.assertEqual(self.errors(), [])

    def test_a_view_that_raises_is_unavailable_never_a_500(self):
        class Raising(httpfix.Views):
            def backlog(self, repo='', state='', fresh=False):
                raise OSError('cannot read D:\\secret\\place')
        lis = self.listener(api.Api(self.store, views=Raising(), clock=httpfix.CLOCK))
        r = get(lis.port, BASE + '/backlog', self.owner)
        self.assertEqual((r.status, r.code()), (503, 'unavailable'))
        self.assertNotIn(b'secret', r.body)

    def test_a_closed_store_is_a_503(self):
        lis = self.listener()
        self.store.close()
        r = get(lis.port, BASE + '/todos', self.agent)
        self.assertEqual((r.status, r.code()), (503, 'store'))
        self.assertEqual(self.errors(), [])

    def test_unknown_paths_and_head(self):
        lis = self.listener()
        for path in (BASE + '/nothing', BASE, '/api', '/api/', '/api/v2/health', '/nothing',
                     BASE + '/health/'):
            with self.subTest(path=path):
                r = get(lis.port, path, self.owner)
                self.assertEqual((r.status, r.code()), (404, 'not_found'))
        r = send(lis.port, 'HEAD', HEALTH, [httpfix.bearer(self.owner)])
        self.assertEqual((r.status, r.body), (404, b''))
        self.assertNotEqual(r.header('Content-Length'), ['0'])

    def test_too_many_query_fields_are_refused(self):
        lis = self.listener()
        many = '&'.join('a%d=1' % i for i in range(httpd.MAX_QUERY_FIELDS + 1))
        r = get(lis.port, HEALTH + '?' + many, self.owner)
        self.assertEqual((r.status, r.json()['error']['field']), (400, 'query'))

    def test_an_exception_becomes_a_fixed_500_and_a_log_line(self):
        lis = self.listener(Broken(), routes={
            ('GET', '/wrong'): lambda request: 'not a response',
            ('GET', '/raises'): lambda request: 1 // 0})
        for path in (HEALTH, '/wrong', '/raises'):
            with self.subTest(path=path):
                del self.lines[:]
                r = get(lis.port, path, self.owner)
                self.assertEqual(r.status, 500)
                self.assertEqual(r.json(), api.error_body('internal', httpd.INTERNAL))
                for word in (b'Traceback', b'secret', b'boom', b'File ', b'.py', b'Error'):
                    self.assertNotIn(word, r.body)
                self.assertEqual(len(self.errors()), 1)
                self.assertIn('Traceback', self.errors()[0])

    def test_an_error_the_real_api_did_not_expect_is_the_fixed_500_for_the_owner_too(self):
        lis = self.listener()
        place = os.path.join(self.store.root, 'a-folder-on-this-machine')

        def docs(coll):
            raise OSError('cannot read %s' % place)
        self.store.docs = docs
        for role, token in ((OWNER, self.owner), (AGENT, self.agent)):
            with self.subTest(role=role):
                del self.lines[:]
                r = get(lis.port, BASE + '/questions', token)
                self.assertEqual(r.status, 500)
                self.assertEqual(r.json(), api.error_body('internal', httpd.INTERNAL))
                for word in (b'a-folder-on-this-machine', b'OSError', b'Traceback', b'cannot read',
                             os.path.basename(self.root).encode('ascii')):
                    self.assertNotIn(word, r.body)
                # the cause is in the log, once
                self.assertEqual(len(self.errors()), 1)
                for word in ('GET %s/questions failed' % BASE, 'Traceback', 'OSError',
                             'a-folder-on-this-machine'):
                    self.assertIn(word, self.errors()[0])
        # the Api's own refusals are still the Api's answer, and no error line
        del self.lines[:]
        r = get(lis.port, BASE + '/questions')
        self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertIn('revision', r.json())
        self.assertEqual(self.errors(), [])

    def test_the_api_is_asked_to_raise_what_it_did_not_expect(self):
        rec = Recorder()
        lis = self.listener(rec)
        self.assertEqual(get(lis.port, HEALTH, self.owner).status, 200)
        self.assertEqual(post(lis.port, TODOS, {'title': 't'}, self.agent).status, 200)
        self.assertEqual(rec.raised, [True, True])

    def test_a_request_http_server_cannot_read_gets_the_fixed_answer(self):
        rec = Recorder()
        lis = self.listener(rec)
        for data in (b'GARBAGE\r\n\r\n', b'GET / HTTP/9.9\r\n\r\n', b'GET /\r\n\r\n',
                     b'GET /' + b'a' * 70000 + b' HTTP/1.1\r\n\r\n',
                     b'GET / HTTP/1.1\r\n' + b'X-H: 1\r\n' * 200 + b'\r\n'):
            with self.subTest(data=data[:24]):
                answer = httpfix.raw(lis.port, data)
                self.assertIn(b'"ok":false', answer)
                for word in (b'Traceback', b'Python', b'BaseHTTP', b'<html'):
                    self.assertNotIn(word, answer)
        self.assertEqual(rec.calls, [])
        self.assertEqual(self.errors(), [])


# ------------------------------------------------------------------ closing
class CloseCase(httpfix.ListenerCase):
    def test_a_connection_with_no_request_does_not_keep_a_closing_listener(self):
        lis = self.listener()
        s = self.connect(lis)                   # a browser's spare connection: it sends nothing
        self.assertTrue(lis.await_idle(1, TIMEOUT))
        self.assertFalse(lis.join(0))
        self.assertTrue(lis.drain(0), 'a connection with no request counted as one in flight')
        lis.close_door()
        self.assertTrue(lis.join(TIMEOUT), 'an idle connection outlived the closed door')
        self.assertEqual(s.recv(100), b'')      # closed, with nothing said
        self.assertFalse(lis.await_idle(1, 0))
        self.assertEqual(self.errors(), [])

    def test_a_write_still_arriving_when_the_door_closes_is_refused_and_writes_nothing(self):
        lis = self.listener()
        before = self.store.revision
        self.assertFalse(lis.closing)
        s = self.connect(lis)
        s.sendall(httpfix.write_head(lis.port, self.owner, 40) + b'{"title":')
        self.assertTrue(lis.await_reads(1, TIMEOUT))
        self.assertFalse(lis.drain(0))
        lis.close_door()
        self.assertTrue(lis.drain(TIMEOUT), 'a body that never came kept the drain waiting')
        answer = httpfix.read_all(s)
        self.assertTrue(answer.startswith(b'HTTP/1.1 503 '), answer[:40])
        self.assertIn(httpd.CLOSING.encode('ascii'), answer)
        self.assertIn(b'"code":"store"', answer)
        s.close()
        self.assertTrue(lis.join(TIMEOUT))
        self.assertFalse(lis.await_reads(1, 0))
        self.assertEqual(self.store.revision, before)
        self.assertEqual(self.errors(), [])

    def test_a_request_that_arrived_before_the_door_closed_is_still_answered(self):
        held, go = threading.Event(), threading.Event()
        creds = auth.Credentials(self.tokens)

        def gate(request):
            held.set()
            go.wait(TIMEOUT)
            return creds.role_for(request.headers)
        lis = self.listener(gate=gate)
        got = []
        t = threading.Thread(target=lambda: got.append(get(lis.port, HEALTH, self.agent)))
        t.start()
        self.addCleanup(t.join, TIMEOUT)
        self.addCleanup(go.set)
        self.assertTrue(held.wait(TIMEOUT))
        lis.close_door()
        self.assertFalse(lis.drain(0))
        go.set()
        self.assertTrue(lis.drain(TIMEOUT))
        t.join(TIMEOUT)
        self.assertEqual([r.status for r in got], [200])


# ------------------------------------------------------------------ the limits
class LimitCase(httpfix.ListenerCase):
    def unread(self, lis):
        """A connection whose request was answered before its body came (the host rule refuses
        it): the answer was read to its end and the connection is still open."""
        s = self.connect(lis)
        s.sendall(('POST %s HTTP/1.1\r\nHost: evil.example\r\nContent-Length: 10\r\n\r\n'
                   % TODOS).encode('ascii'))
        answer = httpfix.read_all(s)
        self.assertTrue(answer.startswith(b'HTTP/1.1 403 '), answer[:40])
        return s

    def test_a_connection_past_the_cap_is_closed_unanswered(self):
        rec = Recorder()
        lis = self.listener(rec, max_connections=1)
        first = self.connect(lis)
        self.assertTrue(lis.await_idle(1, TIMEOUT))
        second = self.connect(lis)
        self.assertEqual(second.recv(100), b'')         # closed, and nothing said
        self.assertEqual(httpfix.http_threads().count('hub-http'), 1)
        self.assertFalse(lis.await_idle(2, 0))
        first.close()
        self.assertTrue(lis.join(TIMEOUT))
        # the slot is free again
        self.assertEqual(get(lis.port, HEALTH, self.owner).status, 200)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(httpd.MAX_CONNECTIONS, 128)
        self.assertEqual(self.listener(start=False).max_connections, httpd.MAX_CONNECTIONS)

    def test_a_wait_that_raises_gives_its_slot_back(self):
        lis = self.listener(Broken(), max_waits=1)
        for _ in range(3):
            r = get(lis.port, BASE + '/wait?since=0', self.agent)
            self.assertEqual(r.status, 500)     # never the 429 of a slot that was kept
            self.assertEqual(lis.waiting, 0)
        self.assertEqual(len(self.errors()), 3)

    def test_the_port_is_bound_exclusively_and_no_resolver_is_asked(self):
        with mock.patch.object(socket, 'getfqdn',
                               side_effect=AssertionError('the resolver was asked')):
            lis = self.listener(start=False)
        self.assertEqual(lis._server.server_name, httpd.LOOPBACK)
        self.assertEqual(lis._server.server_port, lis.port)
        sock = lis._server.socket
        self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
        self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)

    def test_a_silent_connection_and_a_stalled_body_end_at_the_socket_timeout(self):
        self.assertEqual((httpd._Handler.timeout, httpd.SOCKET_TIMEOUT_S), (30, 30))
        with mock.patch.object(httpd._Handler, 'timeout', 1):
            lis = self.listener()
            s = self.connect(lis)
            self.assertEqual(s.recv(100), b'')          # dropped, the door still open
            self.assertTrue(lis.join(TIMEOUT))
            before = self.store.revision
            s = self.connect(lis)
            s.sendall(httpfix.write_head(lis.port, self.owner, 40) + b'{"title":')
            answer = httpfix.read_all(s)                # the client never closed its side
            self.assertTrue(answer.startswith(b'HTTP/1.1 400 '), answer[:40])
            self.assertIn(b'"field":"body"', answer)
            s.close()
            self.assertTrue(lis.join(TIMEOUT))
        self.assertFalse(lis.closing)
        self.assertEqual(self.store.revision, before)
        self.assertEqual(self.errors(), [])

    def test_the_reading_away_after_an_early_answer_ends_at_its_byte_cap(self):
        with mock.patch.object(httpd, 'LINGER_S', 3600), mock.patch.object(httpd, 'LINGER_BYTES', 4):
            lis = self.listener()
            s = self.unread(lis)
            self.assertFalse(lis.join(0))               # still reading away what may come
            s.sendall(b'0123456789')
            self.assertTrue(lis.join(TIMEOUT), 'more than the cap was read away')
        self.assertEqual(self.errors(), [])

    def test_the_reading_away_after_an_early_answer_ends_at_its_time_limit(self):
        self.assertEqual((httpd.LINGER_S, httpd.LINGER_BYTES), (2, 4 << 20))
        with mock.patch.object(httpd, 'LINGER_S', 0.05):
            lis = self.listener()
            self.unread(lis)                            # silent, and still connected
            self.assertTrue(lis.join(TIMEOUT), 'a silent client kept its handler')
        self.assertEqual(self.errors(), [])


# ------------------------------------------------------------------ pairing
class PairCase(httpfix.ListenerCase):
    def test_only_the_owner_gets_a_pairing_link(self):
        lis = self.listener()
        for token in (None, self.agent, 'x' * 43):
            with self.subTest(token=token):
                r = post(lis.port, httpd.PAIR_ROUTE, None, token)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                self.assertNotIn(b'code=', r.body)
        r = post(lis.port, httpd.PAIR_ROUTE, None, self.owner)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()['ttl'], auth.PAIR_TTL_S)
        self.assertTrue(r.json()['path'].startswith(httpd.PAIR_PATH + '?code='))
        # a GET, or a write without the write headers, makes no link
        self.assertEqual(get(lis.port, httpd.PAIR_ROUTE, self.owner).status, 404)
        r = send(lis.port, 'POST', httpd.PAIR_ROUTE, [httpfix.bearer(self.owner),
                                                     ('Content-Type', 'application/json')], b'')
        self.assertEqual(r.status, 403)

    def test_a_link_pairs_one_browser_once(self):
        lis = self.listener()
        path = post(lis.port, httpd.PAIR_ROUTE, None, self.owner).json()['path']
        r = get(lis.port, path)
        self.assertEqual(r.status, 303)
        self.assertEqual(r.header('Location'), ['/'])
        cookies = r.header('Set-Cookie')
        self.assertEqual(len(cookies), 1)
        name, _, value = cookies[0].split(';')[0].partition('=')
        self.assertEqual(name, auth.cookie_name(lis.port))
        self.assertEqual(cookies[0], auth.cookie_header(value, lis.port))
        self.assertIn('HttpOnly', cookies[0])
        self.assertIn('SameSite=Strict', cookies[0])
        # the cookie is the owner
        r = get(lis.port, HEALTH, headers=[('Cookie', 'other=1; %s=%s' % (name, value))])
        self.assertEqual(r.status, 200)
        self.assertIn('store', r.json())
        r = get(lis.port, HEALTH, headers=[('Cookie', 'other=1'),
                                           ('Cookie', '%s=%s' % (name, value))])
        self.assertEqual(r.status, 200, 'the cookie in a second Cookie header was not read')
        # the link is spent
        again = get(lis.port, path)
        self.assertEqual((again.status, again.code()), (403, 'forbidden'))
        self.assertEqual(again.header('Set-Cookie'), [])
        for bad in (httpd.PAIR_PATH, httpd.PAIR_PATH + '?code=', httpd.PAIR_PATH + '?code=nope',
                    httpd.PAIR_PATH + '?other=' + value):
            with self.subTest(path=bad):
                r = get(lis.port, bad)
                self.assertEqual((r.status, r.header('Set-Cookie')), (403, []))
        # another listener's cookie name is not this one's
        r = get(lis.port, HEALTH, headers=[('Cookie', '%s=%s' % (auth.cookie_name(1), value))])
        self.assertEqual(r.status, 403)

    def test_a_session_cookie_alone_gets_no_pairing_link(self):
        lis = self.listener()
        path = post(lis.port, httpd.PAIR_ROUTE, None, self.owner).json()['path']
        cookie = ('Cookie', get(lis.port, path).header('Set-Cookie')[0].split(';')[0])
        # the session is the owner's everywhere else
        self.assertIn('store', get(lis.port, HEALTH, headers=[cookie]).json())
        r = post(lis.port, httpd.PAIR_ROUTE, None, headers=[cookie])
        self.assertEqual((r.status, r.code()), (403, 'forbidden'))
        self.assertEqual(r.json()['error']['field'], 'role')
        self.assertNotIn(b'code=', r.body)
        # nor with a token that is not the owner's beside it
        for token in (self.agent, 'x' * 43):
            with self.subTest(token=token):
                r = post(lis.port, httpd.PAIR_ROUTE, None, token, headers=[cookie])
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                self.assertNotIn(b'code=', r.body)
        # the owner's token still pairs, with the cookie beside it or without
        r = post(lis.port, httpd.PAIR_ROUTE, None, self.owner, headers=[cookie])
        self.assertEqual(r.status, 200, r.body)
        self.assertTrue(r.json()['path'].startswith(httpd.PAIR_PATH + '?code='))

    def test_a_listener_given_no_pairing_routes_pairs_nobody(self):
        lis = self.listener(routes={})
        r = post(lis.port, httpd.PAIR_ROUTE, None, self.owner)
        self.assertEqual((r.status, r.code()), (404, 'not_found'))
        self.assertEqual(get(lis.port, httpd.PAIR_PATH + '?code=x').status, 404)


# ------------------------------------------------------------------ the log
class LogCase(httpfix.ListenerCase):
    def test_a_line_per_request_with_no_secret_in_it(self):
        lis = self.listener()
        path = post(lis.port, httpd.PAIR_ROUTE, None, self.owner).json()['path']
        code = path.partition('code=')[2]
        cookie = get(lis.port, path).header('Set-Cookie')[0].split(';')[0]
        value = cookie.partition('=')[2]
        get(lis.port, HEALTH + '?token=' + self.owner, self.agent)
        get(lis.port, HEALTH, headers=[('Cookie', cookie)])
        get(lis.port, '/' + self.owner, self.owner)         # not a secret's place, but logged
        httpfix.raw(lis.port, ('BREW /x\x7fy HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                               'Authorization: Bearer %s\r\n\r\n'
                               % (lis.port, self.owner)).encode('ascii'))
        httpfix.raw(lis.port, b'GARBAGE ' + self.owner.encode('ascii') + b'\r\n\r\n')
        lis.stop(TIMEOUT)
        access = [text for level, text in self.lines if level == 'access']
        self.assertEqual(access[:4], [
            '127.0.0.1 POST %s 200 owner' % httpd.PAIR_ROUTE,
            '127.0.0.1 GET %s 303 -' % httpd.PAIR_PATH,
            '127.0.0.1 GET %s 200 agent' % HEALTH,
            '127.0.0.1 GET %s 200 owner' % HEALTH])
        self.assertEqual(access[5], '127.0.0.1 BREW /x\\x7fy 405 owner')
        self.assertEqual(access[6], '127.0.0.1 - - 400 -')
        self.assertEqual(len(access), 7)
        text = '\n'.join(t for _, t in self.lines)
        for secret in (self.agent, code, value, cookie, 'Bearer', 'Cookie'):
            self.assertNotIn(secret, text)
        # a path is the one place a caller's text is logged: it is cut and has no control byte
        self.assertEqual(text.count(self.owner), 1)
        self.assertEqual(self.errors(), [])

    def test_a_long_path_is_cut_and_a_failing_log_fails_nothing(self):
        def log(level, text):
            self.lines.append((level, text))
            raise RuntimeError('the log broke')
        lis = self.listener(log=log)
        r = get(lis.port, '/' + 'a' * 5000, self.owner)
        self.assertEqual(r.status, 404)
        self.assertLess(len(self.lines[0][1]), 300)

    def test_with_no_log_nothing_is_written(self):
        lis = self.listener(log=None)
        self.assertEqual(get(lis.port, HEALTH, self.owner).status, 200)
        self.assertEqual(self.lines, [])


# ------------------------------------------------------------------ static files
class StaticCase(httpfix.ListenerCase):
    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.static = os.path.join(self.root, 'site', 'web')
        os.makedirs(os.path.join(self.static, 'sub'))
        self.put('app.js', 'js')
        self.put('sub/x.css', 'css')
        self.put('.hidden.js', 'hidden')
        self.put('secret.token', 'token')
        self.put('notes.md', 'md')
        self.put('../outside.js', 'outside')
        self.put('../../top.js', 'top')

    def put(self, rel, text):
        with open(os.path.join(self.static, *rel.split('/')), 'w', newline='') as f:
            f.write(text)

    def test_static_file_names_only_plain_files_of_the_folder(self):
        full = os.path.realpath(os.path.join(self.static, 'app.js'))
        self.assertEqual(httpd.static_file(self.static, '/app.js'),
                         (full, 'text/javascript; charset=utf-8'))
        self.assertEqual(httpd.static_file(self.static, '/sub/x.css')[1],
                         'text/css; charset=utf-8')
        self.assertIsNone(httpd.static_file(self.static, '/'))          # no index yet
        self.put('index.html', '<p>page</p>')
        self.assertEqual(httpd.static_file(self.static, '/')[1], 'text/html; charset=utf-8')
        drive = os.path.splitdrive(self.static)[0]
        for path in ('/../outside.js', '/sub/../../outside.js', '/..%2foutside.js',
                     '/%2e%2e/outside.js', '/%2E%2E%5Coutside.js', '/..\\outside.js',
                     '/sub\\x.css', '/sub/..\\..\\outside.js', '/../../top.js', '/..', '/.',
                     '/./app.js', '//app.js', '/sub//x.css', '/sub/', '/sub', '/app.js/',
                     '/C:/app.js', '/C:app.js', '/' + drive + '/app.js',
                     '/' + self.static.replace('\\', '/') + '/app.js',
                     self.static + '\\app.js', '\\app.js', 'app.js', '', None, 7,
                     '/app.js::$DATA', '/app.js:stream.js', '/app.js.', '/app.js ', '/app%2ejs',
                     '/app.js%00.html', '/app.js\x00.html', '/APP.JS~1', '/.hidden.js',
                     '/secret.token', '/notes.md', '/missing.js', '/app', '/nul.js', '/NUL.html',
                     '/con.js', '/com1.css', '/lpt9.txt', '/aux', '/sub/nul.js', '/a..b.js',
                     '/app.js?x=1', '/app.js#x', '/sub/x.css;v=1', '/~/app.js', '/$Recycle.js',
                     '/\u00e4pp.js', '/app.js\n'):
            with self.subTest(path=path):
                self.assertIsNone(httpd.static_file(self.static, path))
        for folder in (None, '', os.path.join(self.root, 'absent')):
            with self.subTest(folder=folder):
                self.assertIsNone(httpd.static_file(folder, '/app.js'))

    def test_a_link_that_leaves_the_folder_names_nothing(self):
        try:
            import _winapi
            _winapi.CreateJunction(os.path.dirname(self.static), os.path.join(self.static, 'out'))
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction could be made here: %s' % type(e).__name__)
        self.assertTrue(os.path.isfile(os.path.join(self.static, 'out', 'outside.js')))
        self.assertIsNone(httpd.static_file(self.static, '/out/outside.js'))
        self.assertIsNotNone(httpd.static_file(self.static, '/out/web/app.js'),
                             'a link that comes back inside the folder is the folder')

    def test_the_page_is_served_without_a_credential_and_the_rest_is_not(self):
        lis = self.listener(static_dir=self.static)
        r = get(lis.port, '/')
        self.assertEqual((r.status, r.code()), (404, 'not_found'))      # no index yet
        self.assertEqual(self.errors(), [])
        self.put('index.html', '<p>page</p>')
        r = get(lis.port, '/')
        self.assertEqual((r.status, r.body), (200, b'<p>page</p>'))
        self.assertEqual(r.header('Content-Type'), ['text/html; charset=utf-8'])
        self.assertEqual(r.header('X-Content-Type-Options'), ['nosniff'])
        r = get(lis.port, '/sub/x.css?v=3', self.agent)
        self.assertEqual((r.status, r.body), (200, b'css'))
        head = send(lis.port, 'HEAD', '/app.js')
        self.assertEqual((head.status, head.body, head.header('Content-Length')),
                         (200, b'', ['2']))
        for path in ('/../outside.js', '/sub/../../outside.js', '/..%2foutside.js',
                     '/%2e%2e/outside.js', '/..\\outside.js', '/../../store/_meta.json',
                     '/../../tokens/owner.token', '/secret.token', '/.hidden.js', '/sub',
                     '/app.js::$DATA', '/nul.js', '/index.html/', '/missing.js'):
            with self.subTest(path=path):
                r = get(lis.port, path, self.owner)
                self.assertEqual((r.status, r.code()), (404, 'not_found'))
        r = post(lis.port, '/app.js', None, self.owner)
        self.assertEqual(r.status, 404, 'a write was answered with a file')
        self.assertEqual(self.errors(), [])

    def test_no_static_folder_is_a_plain_404(self):
        lis = self.listener()
        for path in ('/', '/index.html', '/app.js'):
            r = get(lis.port, path, self.owner)
            self.assertEqual((r.status, r.code()), (404, 'not_found'))
        self.assertEqual(self.errors(), [])

    def test_a_file_over_the_size_cap_is_not_served(self):
        self.put('big.txt', 'x' * 100)
        lis = self.listener(static_dir=self.static)
        self.assertEqual(get(lis.port, '/big.txt').status, 200)
        old, httpd.MAX_STATIC = httpd.MAX_STATIC, 99
        self.addCleanup(setattr, httpd, 'MAX_STATIC', old)
        self.assertEqual(get(lis.port, '/big.txt').status, 404)


if __name__ == '__main__':
    unittest.main()
