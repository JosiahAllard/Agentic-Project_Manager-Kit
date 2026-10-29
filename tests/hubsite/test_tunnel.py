"""The tunnel listener (kitlib.hubsite.tunnel) and its place in the server (serve.build).

Every hub here is built by serve.build over a temp store with both listeners on port 0 and is
asked over real loopback sockets. No tunnel program is run and nothing leaves the loopback
address: the signing keys are the test keypairs of tests.hubsite.access_keys, handed in through
the Sources stand-ins (access_keys, access_now), and FetchFailureTest gives a real
access.KeyCache a stand-in fetcher. Every hub is closed, and no listener thread outlives its
test."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.hubsite import access, api, auth, httpd, serve, tunnel
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite import store as hub_store
from tests.hubsite import access_keys as ak
from tests.hubsite import httpfix
from tests.hubsite.httpfix import bearer, get, post, send

HEALTH = api.BASE + '/health'
TODOS = api.BASE + '/todos'
PUBLIC = 'hub.example.invalid'          # the name a visitor through the tunnel asks for
PAGE = b'<!doctype html><title>hub</title>'
SCRIPT = b'// the page script\n'
# the one answer the tunnel listener gives a request it does not let in, spelled out: it is what
# a stranger sees, so a change of a word in it is a change this test must be shown
REFUSAL = {'ok': False, 'error': {'code': 'forbidden', 'field': 'access',
                                  'message': 'this listener needs a valid Cloudflare Access '
                                             'token'}}
OTHER_METHODS = ('OPTIONS', 'PUT', 'DELETE', 'PATCH')
# what health says of a tunnel listener that is bound, spelled out: `hub status` prints them
BOUND = 'bound, keys not fetched'
BOUND_FETCH = 'bound, keys not fetched (keys-fetch)'
NOT_MADE = 'off: the tunnel listener could not be made: the server console says why'
# git's words for a failed pass, as a sync gives them: a remote and a folder, both made up
GIT_BRANCH = 'made-up-branch'
GIT_WHY = "fatal: unable to access 'https://git.example.invalid/team/data.git/' from X:\\made-up"
# a route of the tests' own for the strong owner, so the gate is tested apart from what the one
# APPROVER route of api.ROUTES (approvals/{id}/decide) does with a request
DECIDE = api.BASE + '/decide-test'


class _Sync(object):
    """What serve needs of a git sync; `state` is a test's changes to status()."""

    def __init__(self, state=None):
        self.state = dict(state or {})

    def start(self):
        return True

    def stop(self, timeout=None):
        return True

    def sync_once(self):
        return self.status()

    def status(self):
        out = {'running': True, 'branch': GIT_BRANCH, 'remote': 'origin', 'failed': '',
               'detail': '', 'pending_commit': False, 'pending_push': False, 'diverged': False}
        out.update(self.state)
        return out


class _Source(object):
    """A key source that checks tokens from the keys it holds and says of itself what a test
    tells it to: `ready` (an exception: ready() raises it) and `error`."""

    def __init__(self, keys, ready=False, error=''):
        self._keys = keys
        self.said = ready
        self.error = error
        self.asked = 0              # how often ready() was called

    def get(self, kid):
        return self._keys.get(kid)

    def ready(self):
        self.asked += 1
        if isinstance(self.said, Exception):
            raise self.said
        return self.said


class TunnelCase(unittest.TestCase):
    """A temp store, token files and page files; build() gives a hub with the tunnel configured
    for the test team, its keys and its clock stand-ins."""

    def setUp(self):
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        self.root = tempfile.mkdtemp(prefix='kit-hubtunnel-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tokens = os.path.join(self.root, 'tokens')
        self.web = os.path.join(self.root, 'web')
        os.makedirs(self.web)
        for name, data in ((httpd.INDEX, PAGE), ('app.js', SCRIPT)):
            with open(os.path.join(self.web, name), 'wb') as f:
                f.write(data)
        self.access = hub_settings.Access(ak.TEAM, ak.AUD, (ak.EMAIL,))
        self.settings = hub_settings.settings(None)._replace(
            port=0, tunnel_port=0, store=os.path.join(self.root, 'store'), access=self.access)
        self.now = ak.NOW               # the clock a token's times are checked against
        self.keys = {ak.KEY_A.kid: ak.jwk(ak.KEY_A)}
        self.asked = []                 # the team names the key source was made for
        self.lines = []                 # (level, text) the server logged
        self.hub = None
        self.lport = self.tport = None

    def log(self, level, text):
        self.lines.append((level, text))

    def keys_for(self, team):
        # a key source that says it has its keys: a plain dict has no ready() and so never
        # counts as having any (tunnel.Gate.keys), which would make every health here `bound`
        self.asked.append(team)
        return _Source(self.keys, ready=True)

    def build(self, settings=None, start=True, **src):
        src.setdefault('tokens', self.tokens)
        src.setdefault('static_dir', self.web)
        src.setdefault('access_keys', self.keys_for)
        src.setdefault('access_now', lambda: self.now)
        hub = serve.build(settings or self.settings, serve.Sources(**src), log=self.log)
        self.addCleanup(hub.close)
        self.hub = hub
        self.owner = auth.read_token(self.tokens, auth.OWNER)
        self.agent = auth.read_token(self.tokens, auth.AGENT)
        self.lport = hub.listeners[0].port
        self.tport = hub.listeners[1].port if len(hub.listeners) > 1 else None
        if start:
            self.assertIs(hub.start(), True)
        return hub

    def ask(self, method, path, token=None, headers=(), host=PUBLIC, obj=None, origin=None):
        """One request to the tunnel port as the tunnel program would hand it on: the public
        Host, `token` in the Access header when given, and for a POST the headers a write needs
        with an https Origin of that host."""
        headers = list(headers)
        if token is not None:
            headers.append((tunnel.HEADER, token))
        body = None
        if method == 'POST':
            headers += [('Content-Type', httpd.JSON), ('Origin', origin or 'https://' + host)]
            body = json.dumps({} if obj is None else obj).encode('utf-8')
        return send(self.tport, method, path, headers, body, host=host)

    def refused(self, method, path, **kw):
        """Ask the tunnel port and hold the answer to the one fixed refusal."""
        r = self.ask(method, path, **kw)
        what = '%s %s' % (method, path)
        self.assertEqual(r.status, 403, what)
        self.assertEqual(r.header('Set-Cookie'), [], what)
        if method == 'HEAD':
            self.assertEqual(r.body, b'', what)
        else:
            self.assertEqual(r.json(), REFUSAL, what)
        return r

    def refusals(self):
        """The lines the tunnel gate logged for requests it refused."""
        return [(level, text) for level, text in self.lines
                if text.startswith(tunnel.REFUSAL_LINE % '')]

    def tunnel_line(self, reason, level='note'):
        return (level, tunnel.REFUSAL_LINE % reason)

    def assertNoSecret(self, *secrets):
        """No log line and no start-up note holds one of `secrets`, nor the Access settings."""
        said = [text for _, text in self.lines] + list(self.hub.notes)
        for secret in secrets + (ak.TEAM, ak.AUD, ak.EMAIL):
            for text in said:
                self.assertNotIn(secret, text)

    def revision(self):
        return self.hub.store.revision


class TunnelTokenTest(TunnelCase):
    def setUp(self):
        TunnelCase.setUp(self)
        self.build()
        self.good = ak.token(ak.KEY_A)

    def test_a_request_without_the_token_is_refused(self):
        before = self.revision()
        self.refused('GET', '/')
        self.refused('GET', HEALTH)
        self.refused('POST', TODOS, obj={'title': 'never written'})
        # an empty header is no token, and a header sent twice is not "the" header
        self.refused('GET', HEALTH, token='')
        self.refused('GET', HEALTH, token=self.good, headers=[(tunnel.HEADER, self.good)])
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.refusals(), [self.tunnel_line(tunnel.NO_TOKEN)])
        self.assertNoSecret(self.good)

    def test_a_bad_signature_is_refused(self):
        # another payload under the good token's signature
        forged = ak.with_signature(ak.token(ak.KEY_A, ak.claims(exp=ak.NOW + 9999)),
                                   ak.signature_of(self.good))
        self.refused('GET', HEALTH, token=forged)
        self.refused('POST', TODOS, token=forged, obj={'title': 'never written'})
        # another key's signature under the trusted key's name
        borrowed = ak.token(ak.KEY_B, head=ak.header(ak.KEY_A))
        self.refused('GET', '/', token=borrowed)
        # a token rightly signed by a key the team does not publish
        stranger = ak.token(ak.KEY_B)
        self.refused('GET', HEALTH, token=stranger)
        self.assertEqual(self.refusals(), [self.tunnel_line('bad-signature'),
                                           self.tunnel_line('unknown-kid')])
        self.assertEqual(self.hub.store.docs('todos'), {})
        self.assertNoSecret(forged, borrowed, stranger)

    def test_a_wrong_audience_is_refused(self):
        other = ak.token(ak.KEY_A, ak.claims(aud=['another-audience-tag']))
        self.refused('GET', HEALTH, token=other)
        self.refused('POST', TODOS, token=other, obj={'title': 'never written'})
        self.assertEqual(self.refusals(), [self.tunnel_line('wrong-audience')])
        self.assertEqual(self.hub.store.docs('todos'), {})
        self.assertNoSecret(other)

    def test_another_teams_token_and_an_email_off_the_list_are_refused(self):
        team = ak.token(ak.KEY_A, ak.claims(iss='https://another-team.cloudflareaccess.com'))
        self.refused('GET', HEALTH, token=team)
        guest = ak.token(ak.KEY_A, ak.claims(email='guest@example.invalid'))
        self.refused('GET', HEALTH, token=guest)
        self.assertEqual(self.refusals(), [self.tunnel_line('wrong-issuer'),
                                           self.tunnel_line('email-not-allowed')])
        self.assertNoSecret(team, guest, 'guest@example.invalid')

    def test_an_expired_token_is_refused(self):
        old = ak.token(ak.KEY_A, ak.claims(exp=ak.NOW - 1))
        self.refused('GET', HEALTH, token=old)
        # a token that is good now is refused from the second it expires
        self.assertEqual(self.ask('GET', HEALTH, token=self.good).status, 200)
        self.now = ak.NOW + 300
        self.refused('GET', HEALTH, token=self.good)
        self.refused('GET', '/', token=self.good)
        self.assertEqual(self.refusals(), [self.tunnel_line('expired')])
        self.assertNoSecret(old, self.good)

    def test_the_stop_route_with_no_valid_access_token_is_the_access_refusal(self):
        # T-tun-2: the gate answers first, so the stop route is never reached here, not even
        # with the owner's token in the Authorization header
        stranger = ak.token(ak.KEY_B)
        for token in (None, '', stranger):
            for headers in ((), (bearer(self.owner),)):
                with self.subTest(token=token is not None and (token or 'empty'),
                                  owner=bool(headers)):
                    r = self.refused('POST', httpd.STOP_ROUTE, token=token, headers=headers)
                    self.assertEqual(r.json()['error']['field'], 'access')
        self.assertFalse(self.hub.stopping.is_set())
        self.assertEqual(get(self.lport, HEALTH, self.owner).status, 200)
        self.assertNoSecret(stranger)

    def test_a_valid_token_is_served_as_the_owner(self):
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.status, 200)
        # health answers fixed words on this port (TunnelHealthTest): the store is a word here,
        # never its folder. That the caller is the owner is in the access line and in the
        # owner-only write below
        self.assertEqual(r.json()['store'], 'ok')
        self.assertEqual(r.json()['tunnel'], 'on')
        self.assertIn(('access', '127.0.0.1 GET %s 200 owner' % HEALTH), self.lines)
        r = self.ask('GET', '/', token=self.good)
        self.assertEqual((r.status, r.body), (200, PAGE))
        # an owner-only write, with the https Origin of the public name
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'through the tunnel'})
        self.assertEqual(r.status, 200, r.body)
        done = self.ask('POST', '%s/%s/done' % (TODOS, r.json()['id']), token=self.good)
        self.assertEqual(done.status, 200, done.body)
        for reply in (self.ask('GET', TODOS, token=self.good), get(self.lport, TODOS, self.owner)):
            self.assertEqual([d['title'] for d in reply.json()['items']], ['through the tunnel'])
        # the write rules hold here too: the Origin is https and names the Host
        before = self.revision()
        for origin in ('http://' + PUBLIC, 'https://evil.example.invalid'):
            r = self.ask('POST', TODOS, token=self.good, obj={'title': 'x'}, origin=origin)
            self.assertEqual((r.status, r.json()['error']['field']), (403, 'origin'), origin)
        self.assertEqual(self.revision(), before)
        # no pairing here: this port hands out no local session
        self.assertEqual(self.ask('POST', httpd.PAIR_ROUTE, token=self.good).status, 404)
        self.assertEqual(self.ask('GET', httpd.PAIR_PATH + '?code=x', token=self.good).status,
                         404)
        # no stop here either, not even with the owner's token beside the access token: the stop
        # route is on the local listener only
        r = self.ask('POST', httpd.STOP_ROUTE, token=self.good, headers=[bearer(self.owner)])
        self.assertEqual(r.status, 404, r.body)
        self.assertFalse(self.hub.stopping.is_set())
        self.assertEqual(get(self.lport, HEALTH, self.owner).status, 200)
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.asked, [ak.TEAM])
        self.assertNoSecret(self.good)


class TunnelHostTest(TunnelCase):
    """What the tunnel listener does with the Host header the tunnel program hands on. It has no
    host rule (reads pass under any name), but a write's Origin must name that Host. The tunnel
    program keeps the visitor's Host unless its route sets an HTTP Host header; these tests pin
    what happens when one rewrites it to the loopback address the route points at."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.build()
        self.good = ak.token(ak.KEY_A)
        self.rewritten = '127.0.0.1:%d' % self.tport

    def test_reads_pass_whatever_the_host(self):
        for host in (PUBLIC, self.rewritten, 'localhost', 'another.example.invalid'):
            with self.subTest(host=host):
                r = self.ask('GET', HEALTH, token=self.good, host=host)
                self.assertEqual((r.status, r.json()['tunnel']), (200, 'on'))
                r = self.ask('GET', '/', token=self.good, host=host)
                self.assertEqual((r.status, r.body), (200, PAGE))
                r = self.ask('GET', TODOS, token=self.good, host=host)
                self.assertEqual(r.status, 200, r.body)
        # and with no Host at all the token still decides: a read is served
        r = self.ask('GET', HEALTH, token=self.good, host=False)
        self.assertEqual(r.status, 200, r.body)
        # it is the token that lets a read in, not the name: without it every one is refused
        for host in (PUBLIC, self.rewritten):
            self.refused('GET', HEALTH, host=host)
        self.assertEqual(self.refusals(), [self.tunnel_line(tunnel.NO_TOKEN)])

    def test_a_write_whose_host_was_rewritten_to_loopback_is_refused_by_the_origin_rule(self):
        before = self.revision()
        # the phone's browser sends the Origin of the public name; the Host it arrives with is
        # the address of the route's service, so the two differ
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'never written'},
                     host=self.rewritten, origin='https://' + PUBLIC)
        self.assertEqual(r.status, 403, r.body)
        self.assertEqual(r.json()['error']['field'], 'origin')
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.hub.store.docs('todos'), {})
        # the token was accepted: the refusal is the write rule's, not the gate's
        self.assertEqual(self.refusals(), [])
        self.assertIn(('access', '127.0.0.1 POST %s 403 owner' % TODOS), self.lines)
        # the same write with the public name kept as Host is served
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'through the tunnel'})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual([d['title'] for d in self.hub.store.docs('todos').values()],
                         ['through the tunnel'])
        self.assertNoSecret(self.good)

    def test_a_write_refused_for_a_rewritten_host_is_logged_once_a_minute_with_both_hosts(self):
        for _ in range(2):
            r = self.ask('POST', TODOS, token=self.good, obj={'title': 'never written'},
                         host=self.rewritten, origin='https://' + PUBLIC)
            self.assertEqual((r.status, r.json()['error']['field']), (403, 'origin'))
        said = [(level, text) for level, text in self.lines if level != 'access']
        self.assertEqual(said, [('error', tunnel.HOST_LINE % (PUBLIC, self.rewritten))])
        self.assertIn('HTTP Host Header', said[0][1])
        # a write whose Origin names its Host, refused for its scheme, says nothing of hosts
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'x'}, origin='http://' + PUBLIC)
        self.assertEqual(r.status, 403)
        self.assertEqual(len([1 for level, _ in self.lines if level != 'access']), 1)
        self.assertNoSecret(self.good)

    def test_a_note_that_raises_leaves_the_refusal_standing_and_the_listener_serving(self):
        called = []

        def broken(origin_host, host):
            called.append((origin_host, host))
            raise RuntimeError('the note failed')
        listener = self.hub.listeners[1]
        self.assertIsNotNone(listener._on_origin)
        listener._on_origin = broken
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'never written'},
                     host=self.rewritten, origin='https://' + PUBLIC)
        self.assertEqual((r.status, r.json()['error']['field']), (403, 'origin'))
        self.assertEqual(called, [(PUBLIC, self.rewritten)])
        self.assertEqual(self.hub.store.docs('todos'), {})
        self.assertEqual([t for level, t in self.lines if level == 'error'], [])
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'after'})
        self.assertEqual(r.status, 200, r.body)

    def test_a_write_with_no_token_never_reaches_the_host_line(self):
        r = self.ask('POST', TODOS, obj={'title': 'never written'}, host=self.rewritten,
                     origin='https://' + PUBLIC)
        self.assertEqual(r.status, 403)
        self.assertEqual(r.json(), REFUSAL)
        self.assertEqual([text for level, text in self.lines if level == 'error'], [])


class HostNoteTest(unittest.TestCase):
    """tunnel.host_note: the throttled line of a write refused because its Origin's host is not
    its Host."""

    def setUp(self):
        self.lines = []
        self.t = 1000.0

    def note(self, log=True):
        return tunnel.host_note(self.log if log else None, clock=lambda: self.t)

    def log(self, level, text):
        self.lines.append((level, text))

    def test_one_line_a_minute_then_the_count_of_the_rest(self):
        note = self.note()
        note(PUBLIC, '127.0.0.1:8791')
        self.t += 10
        note(PUBLIC, '127.0.0.1:8791')
        note('another.example.invalid', '127.0.0.1:8791')
        self.assertEqual(self.lines, [('error', tunnel.HOST_LINE % (PUBLIC, '127.0.0.1:8791'))])
        self.t += tunnel.SAY_EVERY_S
        note(PUBLIC, 'localhost:8791')
        self.assertEqual(self.lines[1:], [('error', tunnel.HOST_LINE % (PUBLIC, 'localhost:8791')
                                           + tunnel.MORE % 2)])

    def test_a_host_is_cut_short_and_holds_only_host_characters(self):
        note = self.note()
        note('evil.example.invalid\r\nforged line', 'a' * 500)
        (level, text), = self.lines
        self.assertNotIn('\n', text)
        self.assertNotIn('\r', text)
        self.assertNotIn(' line', text)
        self.assertNotIn('a' * (tunnel.HOST_CHARS + 1), text)

    def test_the_user_info_of_an_origin_netloc_never_reaches_the_log(self):
        note = self.note()
        note('someone:secret-word@x@' + PUBLIC, 'u@127.0.0.1:8791')
        self.assertEqual(self.lines, [('error', tunnel.HOST_LINE % (PUBLIC, '127.0.0.1:8791'))])
        self.assertNotIn('secret', self.lines[0][1])
        self.assertNotIn('someone', self.lines[0][1])

    def test_no_log_and_a_broken_log_raise_nothing(self):
        self.note(log=False)(PUBLIC, '127.0.0.1:8791')

        def broken(level, text):
            raise OSError('the console is gone')
        tunnel.host_note(broken, clock=lambda: self.t)(PUBLIC, '127.0.0.1:8791')


class NoExceptionTest(TunnelCase):
    def setUp(self):
        TunnelCase.setUp(self)
        self.build()
        self.good = ak.token(ak.KEY_A)

    def test_every_route_static_file_and_health_needs_the_token(self):
        before = self.revision()
        asked = 0
        for route in api.ROUTES:        # a route added later is covered with no edit here
            path = api.sample_path(route)
            for method in (route.method, 'HEAD') + OTHER_METHODS:
                self.refused(method, path)
                asked += 1
        others = ('/', '/app.js', '/' + httpd.INDEX, HEALTH, api.BASE + '/no-such-view',
                  '/no-such-file.html', httpd.PAIR_PATH, httpd.PAIR_ROUTE,
                  httpd.PAIR_PATH + '?code=x', '/?%s=x' % tunnel.HEADER)
        for path in others:
            for method in ('GET', 'HEAD', 'POST') + OTHER_METHODS:
                self.refused(method, path)
                asked += 1
        self.assertGreater(asked, len(api.ROUTES) * 6)
        self.assertEqual(self.revision(), before)
        # every one of them was refused at the gate, with no role, and logged as one line
        lines = [text for level, text in self.lines if level == 'access']
        self.assertEqual(len(lines), asked)
        self.assertEqual([text for text in lines if not text.endswith(' 403 -')], [])
        # one reason for them all, and far fewer lines than requests (one a minute)
        said = self.refusals()
        self.assertEqual(said[0], self.tunnel_line(tunnel.NO_TOKEN))
        self.assertEqual([line for line in said if not line[1].startswith(said[0][1])], [])
        self.assertLess(len(said), 10)
        self.assertEqual([text for level, text in self.lines if level == 'error'], [])
        # the same paths with the token: each is what it is, so the refusals above were the gate's
        for method, path, status in (
                ('GET', '/', 200), ('HEAD', '/', 200), ('GET', '/app.js', 200),
                ('GET', HEALTH, 200), ('GET', api.BASE + '/no-such-view', 404),
                ('GET', '/no-such-file.html', 404), ('OPTIONS', '/', 405),
                ('PUT', TODOS, 405), ('DELETE', TODOS, 405)):
            r = self.ask(method, path, token=self.good)
            self.assertEqual(r.status, status, '%s %s' % (method, path))
        self.assertEqual(self.ask('GET', '/app.js', token=self.good).body, SCRIPT)

    def test_a_local_owner_or_agent_token_does_not_open_the_tunnel_port(self):
        # the browser's session on the local port, made the way the owner makes it
        r = post(self.lport, httpd.PAIR_ROUTE, {}, self.owner)
        self.assertEqual(r.status, 200, r.body)
        r = get(self.lport, r.json()['path'])
        self.assertEqual(r.status, 303)
        cookie = r.header('Set-Cookie')[0].split(';')[0]
        value = cookie.split('=', 1)[1]
        local = [bearer(self.owner)], [bearer(self.agent)], [('Cookie', cookie)]
        for headers in local:           # each opens the local port
            self.assertEqual(get(self.lport, HEALTH, headers=headers).status, 200)
        before = self.revision()
        tried = local + ([('Cookie', '%s=%s' % (auth.cookie_name(self.tport), value))],
                         [bearer(self.owner), ('Cookie', cookie)])
        for headers in tried:           # and none opens the tunnel port, for a read or a write
            for method, path in (('GET', '/'), ('GET', HEALTH), ('GET', TODOS), ('POST', TODOS)):
                self.refused(method, path, headers=headers, obj={'title': 'never written'})
                self.refused(method, path, headers=headers, host='127.0.0.1:%d' % self.tport)
        # a local token is no Access token either
        for token in (self.owner, self.agent, value):
            self.refused('GET', HEALTH, token=token)
        # the Access token is read from its header only
        for headers in ([bearer(self.good)], [('Cookie', 'CF_Authorization=' + self.good)],
                        [('Cookie', '%s=%s' % (tunnel.HEADER, self.good))]):
            self.refused('GET', HEALTH, headers=headers)
        self.refused('GET', '%s?%s=%s' % (HEALTH, tunnel.HEADER, self.good))
        self.assertEqual(self.revision(), before)
        # and beside a valid Access token a local token changes nothing: the caller is the owner
        r = self.ask('GET', HEALTH, token=self.good, headers=[bearer(self.agent)])
        self.assertEqual(r.status, 200)
        self.assertIn('store', r.json())
        self.assertNoSecret(self.owner, self.agent, value, self.good)

    def test_the_local_port_serves_without_an_access_token(self):
        r = get(self.lport, '/')
        self.assertEqual((r.status, r.body), (200, PAGE))
        r = get(self.lport, '/app.js')
        self.assertEqual((r.status, r.body), (200, SCRIPT))
        r = get(self.lport, HEALTH, self.owner)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()['store'], os.path.abspath(self.settings.store))
        self.assertNotIn('store', get(self.lport, HEALTH, self.agent).json())
        self.assertEqual(post(self.lport, TODOS, {'title': 'at the PC'}, self.owner).status, 200)
        # the owner's browser pairs as before, and is asked for nothing new
        r = post(self.lport, httpd.PAIR_ROUTE, {}, self.owner)
        self.assertEqual(r.status, 200, r.body)
        r = get(self.lport, r.json()['path'])
        self.assertEqual(r.status, 303)
        cookie = r.header('Set-Cookie')[0].split(';')[0]
        self.assertEqual(get(self.lport, HEALTH, headers=[('Cookie', cookie)]).status, 200)
        # its own rules are as they were: a foreign Host is still refused there
        r = send(self.lport, 'GET', '/', host=PUBLIC)
        self.assertEqual((r.status, r.json()['error']['field']), (403, 'host'))
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.asked, [ak.TEAM])     # made once at start-up, never asked here

    def test_the_access_header_is_ignored_on_the_local_port(self):
        access_header = [(tunnel.HEADER, self.good)]
        # alone it is no credential there
        r = get(self.lport, HEALTH, headers=access_header)
        self.assertEqual((r.status, r.json()['error']['field']), (403, 'role'))
        self.assertEqual(get(self.lport, TODOS, headers=access_header).status, 403)
        r = post(self.lport, TODOS, {'title': 'never written'}, headers=access_header)
        self.assertEqual(r.status, 403)
        self.assertEqual(self.hub.store.docs('todos'), {})
        # beside the agent's token the caller stays the agent
        r = get(self.lport, HEALTH, self.agent, headers=access_header)
        self.assertEqual(r.status, 200)
        self.assertNotIn('store', r.json())
        # and a bad one takes nothing from the owner
        for bad in ('not-a-token', ak.token(ak.KEY_A, ak.claims(exp=ak.NOW - 1)), ''):
            r = get(self.lport, HEALTH, self.owner, headers=[(tunnel.HEADER, bad)])
            self.assertEqual(r.status, 200, bad)
            self.assertIn('store', r.json())
        r = get(self.lport, '/', headers=[(tunnel.HEADER, 'not-a-token')])
        self.assertEqual((r.status, r.body), (200, PAGE))
        self.assertEqual(self.refusals(), [])       # the gate of the tunnel port never ran


class StartTest(TunnelCase):
    def off(self, access_settings, reason, noted=True):
        """A hub built with these Access settings has no tunnel listener, says `reason`, and its
        local listener serves."""
        settings = self.settings._replace(access=access_settings)
        with self.assertRaises(tunnel.TunnelConfigError) as cm:
            tunnel.start(settings, None)
        self.assertEqual((str(cm.exception), cm.exception.unset), (reason, not noted))
        hub = self.build(settings)
        self.assertEqual(len(hub.listeners), 1)
        self.assertEqual(hub.notes, ['the tunnel listener is off: ' + reason] if noted else [])
        self.assertEqual(hub.tunnel, 'off: ' + reason)
        r = get(self.lport, HEALTH, self.owner)
        self.assertEqual((r.status, r.json()['tunnel']), (200, 'off: ' + reason))
        self.assertEqual(self.asked, [], 'a key source was made for a tunnel that is off')
        self.assertIs(hub.close(), True)
        return hub

    def test_the_tunnel_port_refuses_to_start_without_a_team_name(self):
        self.off(self.access._replace(team=''), tunnel.NO_TEAM)
        self.assertEqual(tunnel.NO_TEAM, 'no Access team name is set (hub.access.team)')

    def test_the_tunnel_port_refuses_to_start_with_a_team_name_that_is_no_dns_label(self):
        for team in ('two.labels', 'kit-test-team.cloudflareaccess.com', 'a team', '-x'):
            with self.subTest(team=team):
                with self.assertRaises(tunnel.TunnelConfigError) as cm:
                    tunnel.start(self.settings._replace(access=self.access._replace(team=team)),
                                 None)
                self.assertEqual((str(cm.exception), cm.exception.unset),
                                 (tunnel.BAD_TEAM, False))
        self.off(self.access._replace(team='two.labels'), tunnel.BAD_TEAM)

    def test_the_tunnel_port_refuses_to_start_without_an_audience_tag(self):
        self.off(self.access._replace(aud=''), tunnel.NO_AUD)
        self.assertEqual(tunnel.NO_AUD, 'no Access audience tag is set (hub.access.aud)')

    def test_the_tunnel_port_refuses_to_start_without_an_allowed_email(self):
        self.off(self.access._replace(emails=()), tunnel.NO_EMAILS)
        self.assertEqual(tunnel.NO_EMAILS, 'no allowed email is set (hub.access.emails)')

    def test_no_reason_names_a_value_of_the_settings(self):
        for over in ({'team': ''}, {'aud': ''}, {'emails': ()}, {'team': 'two.labels'}):
            with self.assertRaises(tunnel.TunnelConfigError) as cm:
                tunnel.check(self.access._replace(**over))
            for value in (ak.TEAM, ak.AUD, ak.EMAIL, 'two.labels'):
                self.assertNotIn(value, str(cm.exception), over)
        self.assertIsNone(tunnel.check(self.access))

    def test_the_local_port_still_starts_when_the_tunnel_port_refuses(self):
        hub = self.build(self.settings._replace(access=self.access._replace(aud='')))
        self.assertEqual(len(hub.listeners), 1)
        self.assertIsNone(self.tport)
        r = get(self.lport, '/')
        self.assertEqual((r.status, r.body), (200, PAGE))
        self.assertEqual(get(self.lport, HEALTH, self.owner).status, 200)
        r = post(self.lport, TODOS, {'title': 'the local port serves'}, self.owner)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual([text for level, text in self.lines if level == 'error'], [])
        self.assertIs(hub.close(), True)

    def test_the_local_port_still_starts_whatever_making_the_tunnel_listener_raises(self):
        secret = 'a-made-up-secret-word'
        for error in (RuntimeError(secret), ValueError(secret), OSError(5, secret),
                      KeyError(secret), TypeError(secret), access.AccessError('keys-fetch', secret)):
            kind = type(error).__name__
            with self.subTest(error=kind):
                del self.lines[:]
                made = []

                def raiser(team, error=error):
                    made.append(team)
                    raise error

                hub = self.build(access_keys=raiser)
                self.assertEqual(made, [ak.TEAM], 'the key source was not asked for')
                self.assertEqual(len(hub.listeners), 1)
                self.assertIsNone(self.tport)
                # one fixed phrase, in the status and in health, for both roles; the console is
                # told the class of what was raised and nothing of its text
                self.assertEqual(hub.tunnel, NOT_MADE)
                self.assertEqual(hub.notes,
                                 ['the tunnel listener is off: it could not be made (%s)' % kind])
                for token in (self.owner, self.agent):
                    r = get(self.lport, HEALTH, token)
                    self.assertEqual((r.status, r.json()['tunnel']), (200, NOT_MADE))
                    self.assertNotIn(secret, r.body.decode('utf-8'))
                    self.assertIn('tunnel: ' + NOT_MADE, serve.status_lines(r.json()))
                self.assertNoSecret(secret)
                # the local listener serves as it does with no tunnel
                r = get(self.lport, '/')
                self.assertEqual((r.status, r.body), (200, PAGE))
                r = post(self.lport, TODOS, {'title': 'the local port serves'}, self.owner)
                self.assertEqual(r.status, 200, r.body)
                self.assertEqual([text for level, text in self.lines if level == 'error'], [])
                self.assertIs(hub.close(), True)
        self.assertEqual(serve.TUNNEL_OFF % serve.TUNNEL_FAILED, NOT_MADE)

    def test_both_listeners_bind_loopback_only(self):
        hub = self.build()
        self.assertEqual(len(hub.listeners), 2)
        self.assertNotEqual(self.lport, self.tport)
        for lis in hub.listeners:
            self.assertIsInstance(lis, httpd.Listener)      # which refuses any other address
            self.assertEqual(lis._server.socket.getsockname(), ('127.0.0.1', lis.port))
            self.assertEqual(lis.url, 'http://127.0.0.1:%d' % lis.port)
        self.assertEqual((hub.port, hub.url), (self.lport, hub.listeners[0].url))
        self.assertEqual(httpfix.http_threads(),
                         sorted('hub-http-%d' % lis.port for lis in hub.listeners))
        # the tunnel listener differs from the local one in what the plan names, and no more
        local, tunnelled = hub.listeners
        self.assertIs(local.host_rule, httpd.local_hosts)
        self.assertIsNone(tunnelled.host_rule)
        self.assertEqual((local.origin_scheme, tunnelled.origin_scheme), ('http', 'https'))
        # its one route besides health hands an Access login a strong pairing code; the local
        # listener never has that route, and the tunnel's redeems no code
        self.assertEqual(sorted(tunnelled.routes),
                         [('GET', HEALTH), ('POST', httpd.STRONG_PAIR_ROUTE)])
        self.assertNotIn(('POST', httpd.STRONG_PAIR_ROUTE), local.routes)
        self.assertIn(('GET', httpd.PAIR_PATH), local.routes)
        self.assertEqual(hub.notes, ['the tunnel listener is on %s: every request there needs '
                                     'a Cloudflare Access token' % tunnelled.url])
        self.assertNoSecret()

    def test_health_reports_why_the_tunnel_port_is_off(self):
        # nothing set at all: off, and no start-up line, as this is how most hubs run
        self.off(hub_settings.Access(), tunnel.UNSET, noted=False)
        self.assertEqual(tunnel.UNSET, 'no Access settings are set (hub.access)')

    def test_health_says_on_for_both_roles_and_on_both_ports(self):
        hub = self.build()
        self.assertEqual(hub.tunnel, 'on')
        good = ak.token(ak.KEY_A)
        for r in (get(self.lport, HEALTH, self.owner), get(self.lport, HEALTH, self.agent),
                  self.ask('GET', HEALTH, token=good)):
            self.assertEqual((r.status, r.json()['tunnel']), (200, 'on'))
        # a caller with no role is told nothing
        self.assertNotIn('tunnel', get(self.lport, HEALTH).json())

    def test_a_key_source_with_no_ready_is_bound_on_both_ports_and_checks_tokens_all_the_same(self):
        hub = self.build(access_keys=lambda team: self.keys)    # a dict: get(kid) and no more
        good = ak.token(ak.KEY_A)
        for _ in range(2):
            self.assertEqual(hub.tunnel, BOUND)
            for r in (get(self.lport, HEALTH, self.owner), get(self.lport, HEALTH, self.agent),
                      self.ask('GET', HEALTH, token=good)):
                self.assertEqual((r.status, r.json()['tunnel']), (200, BOUND))
            # the dict's keys check tokens all the same: a good one writes, a forged one does not
            r = self.ask('POST', TODOS, token=good, obj={'title': 'through the tunnel'})
            self.assertEqual(r.status, 200, r.body)
            self.refused('POST', TODOS, token=ak.token(ak.KEY_B, head=ak.header(ak.KEY_A)),
                         obj={'title': 'never written'})
        self.assertIn('tunnel: ' + BOUND,
                      serve.status_lines(get(self.lport, HEALTH, self.owner).json()))

    def test_the_agent_is_told_why_it_is_off_in_fixed_words(self):
        self.build(self.settings._replace(access=self.access._replace(team='')))
        r = get(self.lport, HEALTH, self.agent)
        self.assertEqual((r.status, r.json()['tunnel']), (200, 'off: ' + tunnel.NO_TEAM))
        for value in (ak.AUD, ak.EMAIL):
            self.assertNotIn(value, r.body.decode('utf-8'))


class FetchFailureTest(TunnelCase):
    """The tunnel gate over a real access.KeyCache whose fetcher and clock are stand-ins."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.clock = [0.0]              # the cache's own (monotonic) clock
        self.fetches = []               # (team, timeout) of every fetch the cache made
        self.failing = False
        self.cache = None
        self.good = ak.token(ak.KEY_A)

    def fetch(self, team, timeout):
        self.fetches.append((team, timeout))
        if self.failing:
            raise OSError('the network is down at %s' % PUBLIC)
        return ak.document(ak.KEY_A)

    def cached(self, team):
        self.asked.append(team)
        self.cache = access.KeyCache(team, fetch=self.fetch, clock=lambda: self.clock[0])
        return self.cache

    def test_the_tunnel_port_refuses_when_keys_cannot_be_fetched_and_none_are_cached(self):
        self.failing = True
        self.build(access_keys=self.cached)
        self.assertEqual(self.fetches, [], 'the keys were fetched before any request')
        self.refused('GET', HEALTH, token=self.good)
        self.assertEqual(self.fetches, [(ak.TEAM, access.FETCH_TIMEOUT_S)])
        # the next request does not hammer the key source, and is refused as well
        self.refused('GET', '/', token=self.good)
        self.refused('POST', TODOS, token=self.good, obj={'title': 'never written'})
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.refusals(), [
            ('error', 'the tunnel listener refused a request: keys-unavailable (keys-fetch)')])
        self.assertEqual(self.hub.store.docs('todos'), {})
        # the local port is not the tunnel's hostage
        self.assertEqual(get(self.lport, HEALTH, self.owner).status, 200)
        # once the keys can be fetched again the same token is served
        self.failing = False
        self.clock[0] += access.REFETCH_S + 1
        self.assertEqual(self.ask('GET', HEALTH, token=self.good).status, 200)
        self.assertEqual(len(self.fetches), 2)
        self.assertNoSecret(self.good, PUBLIC)

    def test_the_tunnel_port_keeps_serving_from_cached_keys_when_a_refetch_fails(self):
        self.build(access_keys=self.cached)
        self.assertEqual(self.ask('GET', HEALTH, token=self.good).status, 200)
        self.assertEqual(len(self.fetches), 1)
        # the keys are due again, and the fetch fails: the cached ones still serve
        self.failing = True
        self.clock[0] = access.KEY_TTL_S + 1
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual((len(self.fetches), self.cache.error), (2, 'keys-fetch'))
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'on cached keys'})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.refusals(), [])
        # a forged token gains nothing from the failure
        self.refused('GET', HEALTH, token=ak.token(ak.KEY_B, head=ak.header(ak.KEY_A)))
        self.assertEqual(self.refusals(), [self.tunnel_line('bad-signature')])
        # but not for ever: keys older than the limit are no keys
        self.clock[0] = access.STALE_LIMIT_S + 1
        self.refused('GET', HEALTH, token=self.good)
        self.assertEqual(len(self.fetches), 3)
        self.assertEqual(self.refusals()[1:], [
            ('error', 'the tunnel listener refused a request: keys-stale (keys-fetch)')])
        self.assertNoSecret(self.good, PUBLIC)

    def said(self, hub, phrase):
        """Hub.tunnel, the local listener's health for both roles and `hub status` say `phrase`
        of the tunnel listener."""
        self.assertEqual(hub.tunnel, phrase)
        for token in (self.owner, self.agent):
            r = get(self.lport, HEALTH, token)
            self.assertEqual((r.status, r.json()['tunnel']), (200, phrase))
            self.assertIn('tunnel: ' + phrase, serve.status_lines(r.json()))
            text = r.body.decode('utf-8')
            for word in (PUBLIC, 'network is down', 'OSError', 'Traceback'):
                self.assertNotIn(word, text)

    def test_health_says_on_only_once_the_keys_were_fetched_and_fetches_none_itself(self):
        hub = self.build(access_keys=self.cached)
        self.said(hub, BOUND)
        self.assertEqual(self.fetches, [], 'health fetched the keys')
        # the first token through the tunnel fetches them: on, on both ports
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual((r.status, r.json()['tunnel']), (200, 'on'))
        self.said(hub, 'on')
        self.assertEqual(len(self.fetches), 1)
        # keys past the limit serve nobody, so they are no keys to health either. The last
        # fetch was good: there is no reason to give
        self.clock[0] = access.STALE_LIMIT_S + 1
        self.said(hub, BOUND)
        self.assertEqual(len(self.fetches), 1, 'health fetched the keys')

    def test_health_says_bound_and_a_fixed_word_while_the_key_fetch_fails(self):
        self.failing = True
        hub = self.build(access_keys=self.cached)
        self.refused('GET', HEALTH, token=self.good)
        self.assertEqual(len(self.fetches), 1)
        self.said(hub, BOUND_FETCH)
        # when the next attempt is due health still makes none: a token does
        self.clock[0] += access.REFETCH_S + 1
        self.said(hub, BOUND_FETCH)
        self.assertEqual(len(self.fetches), 1, 'health fetched the keys')
        self.failing = False
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual((r.status, r.json()['tunnel']), (200, 'on'))
        self.said(hub, 'on')
        self.assertEqual(len(self.fetches), 2)
        self.assertNoSecret(self.good, PUBLIC)

    def test_health_says_on_while_cached_keys_serve_through_a_failing_refetch(self):
        hub = self.build(access_keys=self.cached)
        self.assertEqual(self.ask('GET', HEALTH, token=self.good).status, 200)
        self.failing = True
        self.clock[0] = access.KEY_TTL_S + 1
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual((r.status, r.json()['tunnel']), (200, 'on'))
        self.assertEqual((len(self.fetches), self.cache.error), (2, 'keys-fetch'))
        self.said(hub, 'on')
        # past the limit they serve nobody: bound, with the word of the fetch that fails
        self.clock[0] = access.STALE_LIMIT_S + 1
        self.said(hub, BOUND_FETCH)
        self.assertEqual(len(self.fetches), 2, 'health fetched the keys')

    def test_health_does_not_wait_for_a_key_fetch_that_is_out(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(team, timeout):
            self.fetches.append((team, timeout))
            entered.set()
            release.wait(httpfix.TIMEOUT)
            return ak.document(ak.KEY_A)

        hub = self.build(access_keys=lambda team: access.KeyCache(team, fetch=fetch))
        answers = []
        client = threading.Thread(
            target=lambda: answers.append(self.ask('GET', HEALTH, token=self.good)))
        self.addCleanup(client.join, httpfix.TIMEOUT)
        self.addCleanup(release.set)
        client.start()
        self.assertTrue(entered.wait(httpfix.TIMEOUT), 'the token asked for no key')
        # the fetch is out and the token's request waits on it: health answers all the same
        self.said(hub, BOUND)
        self.assertEqual((answers, release.is_set()), ([], False), 'health waited for the fetch')
        self.assertEqual(len(self.fetches), 1)
        release.set()
        client.join(httpfix.TIMEOUT)
        self.assertEqual([(r.status, r.json()['tunnel']) for r in answers], [(200, 'on')])
        self.said(hub, 'on')
        self.assertEqual(len(self.fetches), 1)


class KeysSaidTest(TunnelCase):
    """What health says of the keys, over a key source that says of itself what the test tells
    it to. It checks tokens all the while, so the tunnel listener's own health can be asked."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.source = _Source(self.keys, ready=False, error='keys-fetch')
        self.build(access_keys=lambda team: self.source)
        self.good = ak.token(ak.KEY_A)

    def local(self):
        out = []
        for token in (self.owner, self.agent):
            r = get(self.lport, HEALTH, token)
            self.assertEqual(r.status, 200, r.body)
            out.append(r.json()['tunnel'])
        self.assertEqual(out[0], out[1], 'the two roles were told different things')
        self.assertEqual(out[0], self.hub.tunnel)
        return out[0]

    def through(self):
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.status, 200, r.body)
        return r.json()['tunnel']

    def test_the_reason_is_the_local_listeners_and_never_the_tunnels(self):
        self.assertEqual(self.local(), BOUND_FETCH)
        self.assertEqual(self.through(), BOUND)
        self.assertEqual(self.ask('GET', HEALTH, token=self.good).json(), {
            'ok': True, 'version': 'v1', 'store': 'ok', 'git': 'off', 'tunnel': BOUND})
        self.assertEqual((serve.TUNNEL_BOUND, serve.TUNNEL_BOUND_WHY % 'keys-fetch'),
                         (BOUND, BOUND_FETCH))
        self.source.said = True
        self.assertEqual((self.local(), self.through()), ('on', 'on'))

    def test_only_a_reason_of_access_is_shown(self):
        for error in ('a made-up reason at %s' % PUBLIC, 'keys-fetch and more', 'Keys-Fetch', '',
                      None, 7, ['keys-fetch'], RuntimeError('keys-fetch')):
            with self.subTest(error=repr(error)):
                self.source.error = error
                self.assertEqual((self.local(), self.through()), (BOUND, BOUND))
        for reason in access.REASONS:
            with self.subTest(reason=reason):
                self.source.error = reason
                self.assertEqual(self.local(), '%s (%s)' % (BOUND, reason))
                self.assertEqual(self.through(), BOUND)

    def test_only_a_key_source_that_says_true_has_keys(self):
        self.source.error = ''
        for said in (False, None, 1, 'yes', [True], RuntimeError('a secret word at %s' % PUBLIC)):
            with self.subTest(said=repr(said)):
                self.source.said = said
                self.assertEqual((self.local(), self.through()), (BOUND, BOUND))
        self.source.said = True
        self.assertEqual((self.local(), self.through()), ('on', 'on'))
        self.assertEqual([text for level, text in self.lines if level == 'error'], [])
        self.assertNoSecret(self.good, PUBLIC)

    def test_health_asks_the_key_source_whether_it_is_ready_and_nothing_else(self):
        asked = []
        self.source.get = lambda kid: asked.append(kid)
        before = self.source.asked
        self.assertEqual(self.local(), BOUND_FETCH)
        self.assertEqual(asked, [], 'health asked the key source for a key')
        self.assertGreater(self.source.asked, before)


class TunnelHealthTest(TunnelCase):
    """GET health on the two listeners of one hub whose git sync says what the test tells it."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.sync = _Sync()
        self.build(sync=lambda *args, **kw: self.sync)
        self.good = ak.token(ak.KEY_A)

    def test_through_the_tunnel_health_is_fixed_words_and_nothing_else(self):
        self.sync.state.update(failed='push', detail=GIT_WHY, pending_push=True)
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body,
                         b'{"ok":true,"version":"v1","store":"ok","git":"failed","tunnel":"on"}')
        # the same request at the local listener names the folder, the branch and git's reason
        local = get(self.lport, HEALTH, self.owner).json()
        self.assertEqual(local['store'], os.path.abspath(self.settings.store))
        self.assertEqual((local['git']['branch'], local['git']['detail']), (GIT_BRANCH, GIT_WHY))
        # a sync that works, and whatever else a sync says of itself
        self.sync.state.update(failed='', detail='')
        self.assertEqual(self.ask('GET', HEALTH, token=self.good).json()['git'], 'ok')
        for failed in ('commit', 'fetch', GIT_WHY, 7):
            self.sync.state.update(failed=failed, detail=GIT_WHY)
            r = self.ask('GET', HEALTH, token=self.good)
            self.assertEqual(r.json(), {'ok': True, 'version': 'v1', 'store': 'ok',
                                        'git': 'failed', 'tunnel': 'on'}, failed)
        self.assertNoSecret(self.good)

    def test_the_local_listeners_health_is_what_it_was(self):
        self.sync.state.update(failed='push', detail=GIT_WHY)
        store = os.path.abspath(self.settings.store)
        status, obj = self.hub.api.call('GET', HEALTH, auth.OWNER)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(obj), ['backlog', 'backlog_at', 'backlog_error', 'backlog_timer',
                                       'code', 'git', 'jobs', 'ok', 'push', 'revision', 'store',
                                       'unavailable', 'version', 'watcher'])
        # byte for byte: the Api's own answer, git's reason for the owner, and `tunnel` last
        obj['git']['detail'] = GIT_WHY
        obj['tunnel'] = 'on'
        r = get(self.lport, HEALTH, self.owner)
        self.assertEqual(r.body, httpd.json_response(200, obj).body)
        self.assertEqual((r.json()['store'], r.json()['git']), (store, self.sync.status()))
        # the agent is told neither the folder nor git's reason
        status, obj = self.hub.api.call('GET', HEALTH, auth.AGENT)
        obj['tunnel'] = 'on'
        r = get(self.lport, HEALTH, self.agent)
        self.assertEqual(r.body, httpd.json_response(200, obj).body)
        self.assertNotIn('store', r.json())
        self.assertNotIn('detail', r.json()['git'])
        self.assertEqual(r.json()['git']['failed'], 'push')

    def test_with_no_git_sync_the_tunnel_is_told_off_in_one_word(self):
        self.hub.close()
        self.build()
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.json(), {'ok': True, 'version': 'v1', 'store': 'ok', 'git': 'off',
                                    'tunnel': 'on'})
        self.assertEqual(get(self.lport, HEALTH, self.owner).json()['git'],
                         {'running': False, 'off': serve.SYNC_OFF})

    def test_a_health_that_cannot_be_answered_says_so_in_fixed_words(self):
        secret = 'cannot read X:\\made-up\\store'

        class Api(object):
            answer = None

            def call(self, *args, **kw):
                return self.answer

        stand_in = Api()
        route = serve.health_route(stand_in, None, 'on', fixed=True)
        request = httpd.Request('GET', HEALTH, None, '127.0.0.1', 0)
        request.role = auth.OWNER
        for answer, status, code in (
                ((503, api.error_body('store', secret, 'path')), 503, 'store'),
                ((403, api.error_body('forbidden', secret, 'role')), 403, 'forbidden'),
                ((500, api.error_body('a made-up code', secret)), 500, 'internal'),
                ((500, {'ok': False, 'error': secret}), 500, 'internal'),
                ((500, secret), 500, 'internal'),
                ((200, [secret]), 500, 'internal'),
                ((200, None), 500, 'internal')):
            with self.subTest(answer=answer):
                stand_in.answer = answer
                r = route(request)
                self.assertEqual(r.status, status)
                self.assertEqual(json.loads(r.body.decode('utf-8')), {
                    'ok': False, 'error': {'code': code, 'field': '',
                                           'message': 'health could not be answered'}})
        # and a 200 says of git what it can tell, never what it was given
        for git, word in ((None, 'unknown'), ('failed', 'unknown'), ([], 'unknown'),
                          ({'off': secret}, 'off'), ({'failed': secret}, 'failed'), ({}, 'ok')):
            stand_in.answer = (200, {'ok': True, 'git': git, 'store': secret, 'version': secret})
            r = route(request)
            self.assertEqual(json.loads(r.body.decode('utf-8')), {
                'ok': True, 'version': 'v1', 'store': 'ok', 'git': word, 'tunnel': 'on'}, git)

    def test_health_tells_the_api_strong_only_for_a_strong_request(self):
        class Api(object):
            def __init__(self):
                self.kw = []

            def call(self, *args, **kw):
                self.kw.append(kw)
                return 200, {'ok': True, 'git': None}

        for fixed in (False, True):
            with self.subTest(fixed=fixed):
                stand_in = Api()
                route = serve.health_route(stand_in, None, 'on', fixed=fixed)
                request = httpd.Request('GET', HEALTH, None, '127.0.0.1', 0)
                request.role = auth.OWNER
                route(request)
                for strong in (1, 'yes', None):
                    request.strong = strong
                    route(request)
                request.strong = True
                route(request)
                self.assertEqual(stand_in.kw, [{'raise_unexpected': True}] * 4 + [
                    {'raise_unexpected': True, 'strong': True}])

    def test_a_health_view_that_raises_is_still_a_200_of_fixed_words_through_the_tunnel(self):
        secret = 'a secret word at X:\\made-up'

        def broken():
            raise RuntimeError(secret)

        self.hub.views.health = broken          # a view that fails: the Api answers all the same
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.json(), {'ok': True, 'version': 'v1', 'store': 'ok', 'git': 'unknown',
                                    'tunnel': 'on'})
        self.assertNotIn('RuntimeError', r.body.decode('utf-8'))

    def test_a_health_that_raises_through_the_tunnel_is_the_listeners_fixed_answer(self):
        secret = 'a secret word at X:\\made-up'
        calls = []

        def broken(*args, **kw):
            calls.append(args[:2])
            raise RuntimeError(secret)

        self.hub.api.call = broken              # the Api itself fails: there is no answer to reword
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(calls, [('GET', HEALTH)])
        self.assertEqual(r.status, 500)
        self.assertEqual(r.json(), {'ok': False, 'error': {
            'code': 'internal', 'field': '',
            'message': 'the server could not answer this request'}})
        text = r.body.decode('utf-8')
        for word in (secret, 'secret', 'made-up', 'RuntimeError', 'Traceback', 'broken',
                     os.path.basename(self.root), '"tunnel"', '"store"'):
            self.assertNotIn(word, text)
        # the caller was let in as the owner: the answer is the listener's, not the gate's
        self.assertEqual(self.refusals(), [])
        self.assertIn(('access', '127.0.0.1 GET %s 500 owner' % HEALTH), self.lines)


class TunnelSourceTest(TunnelCase):
    """A write through the tunnel listener is recorded with the source `tunnel`."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.build()
        self.good = ak.token(ak.KEY_A)

    def via(self, r):
        self.assertEqual(r.status, 200, r.body)
        return self.hub.store.get('todos', r.json()['id'])['via']

    def test_a_write_through_the_tunnel_is_recorded_as_tunnel_whatever_the_client_says(self):
        for said in (None, 'web', 'cli', 'api', 'relay:phone', 'tunnel', 'WEB', ''):
            with self.subTest(said=said):
                headers = [] if said is None else [(httpd.VIA_HEADER, said)]
                r = self.ask('POST', TODOS, token=self.good, headers=headers,
                             obj={'title': 'through the tunnel'})
                self.assertEqual(self.via(r), 'tunnel')
        # sent twice: still not read
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'twice'},
                     headers=[(httpd.VIA_HEADER, 'web'), (httpd.VIA_HEADER, 'cli')])
        self.assertEqual(self.via(r), 'tunnel')
        # a later change of the document through the tunnel is recorded so too
        tid = post(self.lport, TODOS, {'title': 'at the PC'}, self.owner, via='web').json()['id']
        self.assertEqual(self.hub.store.get('todos', tid)['via'], 'web')
        r = self.ask('POST', '%s/%s/done' % (TODOS, tid), token=self.good,
                     headers=[(httpd.VIA_HEADER, 'web')])
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.hub.store.get('todos', tid)['via'], 'tunnel')
        # how: the tunnel listener names the source itself, the local one still asks the client
        self.assertEqual(tunnel.VIA, 'tunnel')
        local, tunnelled = self.hub.listeners
        self.assertEqual((local.via, local.via_header), ('', True))
        self.assertEqual((tunnelled.via, tunnelled.via_header), ('tunnel', False))

    def test_a_local_write_is_recorded_as_before(self):
        for said, recorded in (('web', 'web'), ('cli', 'cli'), (None, 'api'), ('tunnel', 'api')):
            with self.subTest(said=said):
                r = post(self.lport, TODOS, {'title': 'at the PC'}, self.owner, via=said)
                self.assertEqual(self.via(r), recorded)

    def test_a_local_write_that_claims_the_tunnel_is_recorded_as_api(self):
        # `tunnel` says a write came in through the tunnel listener: no client of the local one
        # may claim it, in any spelling the header would be read in
        for said in ('tunnel', ' tunnel', 'tunnel ', '\ttunnel'):
            for token in (self.owner, self.agent):
                with self.subTest(said=said, agent=token is self.agent):
                    r = post(self.lport, TODOS, {'title': 'at the PC', 'commands': ['dir']},
                             token, headers=[(httpd.VIA_HEADER, said)])
                    self.assertEqual(self.via(r), 'api')
        # a later change of a document claims it no better
        tid = post(self.lport, TODOS, {'title': 'at the PC'}, self.owner, via='web').json()['id']
        r = post(self.lport, '%s/%s/done' % (TODOS, tid), None, self.owner, via='tunnel')
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.hub.store.get('todos', tid)['via'], 'api')
        # every other source a client names is recorded as it was
        for said in ('web', 'cli', 'desktop-mcp', 'relay:phone', 'tunnels', 'tunnel:phone'):
            with self.subTest(said=said):
                r = post(self.lport, TODOS, {'title': 'at the PC'}, self.owner, via=said)
                self.assertEqual(self.via(r), said)
        # and through the tunnel listener a write is still recorded as `tunnel`
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'through the tunnel'})
        self.assertEqual(self.via(r), 'tunnel')


class TunnelErrorWordsTest(TunnelCase):
    """An error answer through the tunnel listener is worded as for an agent: its caller is the
    owner for what a request may do, and no answer there names the store folder or a file."""

    STORE_SAYS = 'the hub store is not available: the owner sees why'

    def setUp(self):
        TunnelCase.setUp(self)
        self.build()
        self.good = ak.token(ak.KEY_A)
        self.folder = os.path.abspath(self.settings.store)

    def assertNoPath(self, r):
        """The answer `r` names no folder and no file of this machine."""
        text = r.body.decode('utf-8')
        for word in (self.folder, json.dumps(self.folder)[1:-1], self.root,
                     json.dumps(self.root)[1:-1], os.path.basename(self.root),
                     hub_store.META_NAME, 'Errno'):
            self.assertNotIn(word, text)
        # and what it says in words holds nothing shaped like a path
        said = (r.json().get('error') or {}).get('message', '')
        for mark in ('\\', ':/'):
            self.assertNotIn(mark, said)

    def assertStoreRefusal(self, r):
        self.assertEqual(r.status, 503, r.body)
        error = r.json()['error']
        self.assertEqual((error['code'], error['message']), ('store', self.STORE_SAYS))
        self.assertIs(r.json()['ok'], False)
        self.assertNoPath(r)
        return error

    def test_a_closed_store_is_told_in_the_agents_words_through_the_tunnel(self):
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'before the close'})
        self.assertEqual(r.status, 200, r.body)
        tid = r.json()['id']
        self.hub.store.close()
        # a read and a write, as the owner the valid token makes of the caller
        for method, path, obj in (('GET', TODOS, None), ('GET', api.BASE + '/snapshot', None),
                                  ('POST', TODOS, {'title': 'never written'}),
                                  ('POST', '%s/%s/done' % (TODOS, tid), None)):
            with self.subTest(method=method, path=path):
                error = self.assertStoreRefusal(self.ask(method, path, token=self.good, obj=obj))
                # the same request at the local listener: the same status, code and field, and
                # the store's own words for the owner, which name its folder
                if method == 'GET':
                    local = get(self.lport, path, self.owner)
                else:
                    local = post(self.lport, path, obj, self.owner)
                self.assertEqual((local.status, local.json()['error']['code'],
                                  local.json()['error']['field']), (503, 'store', error['field']))
                self.assertIn(self.folder, local.json()['error']['message'])
        self.assertIn(('access', '127.0.0.1 GET %s 503 owner' % TODOS), self.lines)
        # health on this port was fixed words already
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertNoPath(r)
        self.assertEqual(api.AGENT_SAYS['store'], self.STORE_SAYS)
        self.assertNoSecret(self.good)

    def test_a_store_that_cannot_be_written_is_told_in_the_agents_words_through_the_tunnel(self):
        meta = os.path.join(self.folder, hub_store.META_NAME)
        replace = hub_store._replace
        failed = []

        def denied(path, text):
            failed.append(path)
            raise OSError(13, 'Permission denied', path)

        before = self.revision()
        hub_store._replace = denied
        try:
            through = self.ask('POST', TODOS, token=self.good, obj={'title': 'never written'})
            local = post(self.lport, TODOS, {'title': 'never written'}, self.owner)
        finally:
            hub_store._replace = replace
        self.assertEqual(failed, [meta, meta], 'the write did not reach the failing file')
        error = self.assertStoreRefusal(through)
        # the local listener's owner is told the file and the system's reason, as before
        self.assertEqual((local.status, local.json()['error']['code'],
                          local.json()['error']['field']), (503, 'store', error['field']))
        # (the system's text holds the path as Python writes it: repr, each backslash doubled)
        for word in (hub_store.META_NAME, 'Permission denied', repr(meta)):
            self.assertIn(word, local.json()['error']['message'])
        self.assertEqual(self.revision(), before)
        # nothing is broken for good: the next write through the tunnel is served
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'written'})
        self.assertEqual(r.status, 200, r.body)

    def test_the_caller_is_still_the_owner_and_other_errors_keep_their_own_words(self):
        r = self.ask('POST', TODOS, token=self.good, obj={'title': 'through the tunnel'})
        self.assertEqual(r.status, 200, r.body)
        tid = r.json()['id']
        # owner-only passes, agent-only is refused: the wording changed nobody's role
        r = self.ask('POST', '%s/%s/edit' % (TODOS, tid), token=self.good, obj={'title': 'edited'})
        self.assertEqual(r.status, 200, r.body)
        r = self.ask('POST', '%s/%s/ack' % (TODOS, tid), token=self.good)
        self.assertEqual((r.status, r.json()['error']['code']), (403, 'forbidden'))
        # an error with no agent wording is the answer the local listener gives its owner
        for method, path, obj in (('POST', TODOS, {}), ('GET', api.BASE + '/no-such-route', None),
                                  ('POST', TODOS + '/T-999/done', None)):
            with self.subTest(method=method, path=path):
                r = self.ask(method, path, token=self.good, obj=obj)
                if method == 'GET':
                    local = get(self.lport, path, self.owner)
                else:
                    local = post(self.lport, path, obj, self.owner)
                self.assertGreaterEqual(r.status, 400)
                self.assertEqual((r.status, r.json()), (local.status, local.json()))
                self.assertNoPath(r)
        # how: the tunnel listener asks the Api for the agent's words, the local one does not
        local, tunnelled = self.hub.listeners
        self.assertEqual((local.agent_words, tunnelled.agent_words), (False, True))


class UnreadableRequestTest(TunnelCase):
    """A request the tunnel listener cannot read: the listener's own fixed answer, before any
    gate, route or file."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.build()
        self.good = ak.token(ak.KEY_A)

    def head(self, path, extra=b'', line=None):
        """A request for `path` with everything a served one has: the public Host and a valid
        Access token."""
        line = line or ('GET %s HTTP/1.1' % path).encode('ascii')
        return (line + b'\r\n' + ('Host: %s\r\n%s: %s\r\n' % (PUBLIC, tunnel.HEADER, self.good))
                .encode('ascii') + extra + b'\r\n')

    def test_a_request_that_cannot_be_read_gets_the_fixed_body_and_reaches_nothing(self):
        # the same requests, readable, are served: what follows is refused for its form alone
        for path, body in (('/', PAGE), ('/app.js', SCRIPT)):
            answer = httpfix.raw(self.tport, self.head(path))
            self.assertTrue(answer.startswith(b'HTTP/1.1 200 '), answer[:40])
            self.assertTrue(answer.endswith(body))
        self.assertIn(b'"tunnel":"on"', httpfix.raw(self.tport, self.head(HEALTH)))
        del self.lines[:]
        before = self.revision()
        calls = []
        tunnelled = self.hub.listeners[1]
        health = tunnelled.routes[('GET', HEALTH)]
        tunnelled.routes[('GET', HEALTH)] = lambda request: calls.append('route') or health(request)
        ask = self.hub.api.call
        self.hub.api.call = lambda *args, **kw: calls.append('api') or ask(*args, **kw)
        fixed = {'ok': False, 'error': {'code': 'invalid', 'field': 'request',
                                        'message': 'the request could not be read'}}
        long_header = b'X-Pad: ' + b'a' * 70000 + b'\r\n'
        cases = (
            ('a request line that is no request', b'GARBAGE\r\n\r\n', 400),
            ('a request line of one word, with headers', self.head('/', line=b'GET'), 400),
            ('a version that is none', self.head('/', line=b'GET / HTTP/9.9'), 505),
            # a request line with no version is HTTP/0.9, whose answer has no status line and
            # no header: the fixed body is all of it
            ('a request line with no version', self.head('/', line=b'GET /'), None),
            ('health with no version', self.head(HEALTH, line=('GET ' + HEALTH).encode()), None),
            ('a request line too long', self.head('/' + 'a' * 70000), 414),
            ('one header too long', self.head('/', long_header), 431),
            ('one header too long, health', self.head(HEALTH, long_header), 431),
            ('too many headers', self.head('/app.js', b'X-H: 1\r\n' * 200), 431),
        )
        for what, data, status in cases:
            with self.subTest(what=what):
                answer = httpfix.raw(self.tport, data)
                if status is None:
                    body = answer
                else:
                    head, _, body = answer.partition(b'\r\n\r\n')
                    self.assertTrue(head.startswith(b'HTTP/1.1 %d ' % status), head[:60])
                self.assertEqual(json.loads(body.decode('utf-8')), fixed)
                for word in (PAGE, SCRIPT, b'Traceback', b'Python', b'BaseHTTP', b'<html',
                             b'"tunnel"', b'"store"'):
                    self.assertNotIn(word, answer)
        self.assertEqual(calls, [], 'an unreadable request reached a route or the Api')
        self.assertEqual(self.revision(), before)
        # refused before the gate: no role, no line of the gate's, and no error
        self.assertEqual(self.refusals(), [])
        self.assertEqual([text for level, text in self.lines if level == 'error'], [])
        lines = [text for level, text in self.lines if level == 'access']
        self.assertEqual(len(lines), len(cases))
        for text in lines:
            self.assertRegex(text, r'^127\.0\.0\.1 - - \d{3} -$')
        self.assertNoSecret(self.good)


class TunnelStrongTest(TunnelCase):
    """The owner who passed the tunnel gate is the strong owner, who alone may call a route of
    role api.APPROVER; the local listener's owner token is not."""

    def setUp(self):
        TunnelCase.setUp(self)
        self.calls = []

        def handler(a, req):
            self.calls.append((req.role, req.strong, req.via))
            return {'ok': True}

        route = api.Route('POST', DECIDE, api.APPROVER, handler)
        patcher = mock.patch.object(api, 'ROUTES', api.ROUTES + (route,))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.build()
        self.good = ak.token(ak.KEY_A)

    def test_a_valid_access_token_reaches_an_approver_route(self):
        r = self.ask('POST', DECIDE, token=self.good, obj={})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.calls, [(auth.OWNER, True, tunnel.VIA)])

    def test_the_local_listener_never_reaches_an_approver_route(self):
        for token in (self.owner, self.agent):
            r = post(self.lport, DECIDE, {}, token=token)
            self.assertEqual((r.status, r.code(), r.json()['error']['field']),
                             (403, 'forbidden', 'role'), r.body)
        # an Access token is no credential on the local port, beside the owner token or alone
        for token in (self.owner, None):
            r = post(self.lport, DECIDE, {}, token=token, headers=[(tunnel.HEADER, self.good)])
            self.assertEqual((r.status, r.code(), r.json()['error']['field']),
                             (403, 'forbidden', 'role'), r.body)
        self.assertEqual(self.calls, [])

    def test_a_refused_tunnel_request_reaches_nothing(self):
        self.refused('POST', DECIDE, obj={})
        self.refused('POST', DECIDE, token=self.good + 'x', obj={})
        self.refused('POST', DECIDE, obj={}, headers=[bearer(self.owner)])
        self.assertEqual(self.calls, [])


class _Request(object):
    """What the gate reads of a request: the one header."""

    def __init__(self, token=None):
        self.token = token
        self.read = []

    def header(self, name):
        self.read.append(name)
        return self.token


class _Broken(object):
    def get(self, kid):
        raise RuntimeError('a secret word in an error')


class GateTest(unittest.TestCase):
    """tunnel.gate on its own: what it answers and what it logs."""

    def setUp(self):
        self.access = hub_settings.Access(ak.TEAM, ak.AUD, (ak.EMAIL,))
        self.keys = {ak.KEY_A.kid: ak.jwk(ak.KEY_A)}
        self.clock = [1000.0]           # the clock the log lines are spaced by
        self.lines = []
        self.good = ak.token(ak.KEY_A)

    def log(self, level, text):
        self.lines.append((level, text))

    def gate(self, **kw):
        kw.setdefault('log', self.log)
        kw.setdefault('keys', self.keys)
        kw.setdefault('now', lambda: ak.NOW)
        keys = kw.pop('keys')
        return tunnel.gate(self.access, keys, clock=lambda: self.clock[0], **kw)

    def assertRefusal(self, answer):
        self.assertIsInstance(answer, httpd.Response)
        self.assertEqual(answer.status, 403)
        self.assertEqual(json.loads(answer.body.decode('utf-8')), REFUSAL)
        self.assertEqual(tuple(answer.headers), ())

    def test_it_answers_the_owner_role_or_the_one_refusal(self):
        gate = self.gate()
        request = _Request(self.good)
        self.assertEqual(gate(request), auth.OWNER)
        self.assertEqual(request.read, [tunnel.HEADER])     # the one header, and nothing else
        self.assertEqual(tunnel.HEADER, 'Cf-Access-Jwt-Assertion')
        self.assertEqual(self.lines, [])
        for token in (None, '', 'x', 'a.b.c', self.good + 'x', self.good[:-2],
                      ak.token(ak.KEY_A, ak.claims(exp=ak.NOW - 1)), 7, b'bytes', [self.good]):
            self.assertRefusal(gate(_Request(token)))
        for _, text in self.lines:
            self.assertNotIn(self.good, text)

    def test_a_valid_token_marks_the_request_strong_and_nothing_else_does(self):
        gate = self.gate()
        request = _Request(self.good)
        self.assertEqual(gate(request), auth.OWNER)
        self.assertIs(request.strong, True)
        for token in (None, '', 'x', self.good + 'x',
                      ak.token(ak.KEY_A, ak.claims(exp=ak.NOW - 1))):
            request = _Request(token)
            self.assertRefusal(gate(request))
            self.assertFalse(hasattr(request, 'strong'), repr(token))
        gate.stop()
        request = _Request(self.good)
        self.assertRefusal(gate(request))
        self.assertFalse(hasattr(request, 'strong'), 'a stopped gate marked a request strong')

    def test_a_request_that_cannot_be_marked_strong_is_refused(self):
        class Fixed(_Request):
            @property
            def strong(self):
                return False

        self.assertRefusal(self.gate()(Fixed(self.good)))
        self.assertEqual(self.lines, [
            ('error', tunnel.REFUSAL_LINE % tunnel.GATE_ERROR + ' (AttributeError)')])

    def test_the_clock_of_the_gate_is_asked_for_every_token(self):
        times = [ak.NOW]
        gate = self.gate(now=lambda: times[0])
        self.assertEqual(gate(_Request(self.good)), auth.OWNER)
        times[0] = ak.NOW + 300
        self.assertRefusal(gate(_Request(self.good)))
        times[0] = 'not a time'
        self.assertRefusal(gate(_Request(self.good)))
        # a token at fault is a note; a fault of the server's own (here its clock) is an error
        self.assertEqual(self.lines, [('note', tunnel.REFUSAL_LINE % 'expired'),
                                      ('error', tunnel.REFUSAL_LINE % 'bad-clock')])

    def test_a_reason_is_logged_once_a_minute_with_the_count_of_the_rest(self):
        gate = self.gate()
        first = tunnel.REFUSAL_LINE % tunnel.NO_TOKEN
        for _ in range(3):
            self.assertRefusal(gate(_Request()))
        self.assertEqual(self.lines, [('note', first)])
        # another reason has its own line at once
        old = ak.token(ak.KEY_A, ak.claims(exp=ak.NOW - 1))
        self.assertRefusal(gate(_Request(old)))
        self.assertEqual(self.lines[1:], [('note', tunnel.REFUSAL_LINE % 'expired')])
        self.clock[0] += tunnel.SAY_EVERY_S - 1
        self.assertRefusal(gate(_Request()))
        self.assertEqual(len(self.lines), 2)
        self.clock[0] += 1
        self.assertRefusal(gate(_Request()))
        self.assertEqual(self.lines[2:], [('note', first + tunnel.MORE % 3)])
        self.assertEqual(tunnel.MORE % 3,
                         '; 3 more were refused for this reason since the last line')
        # and the count starts again
        self.clock[0] += tunnel.SAY_EVERY_S
        self.assertRefusal(gate(_Request()))
        self.assertEqual(self.lines[3:], [('note', first)])

    def test_an_error_in_the_key_source_is_the_refusal_and_an_error_line(self):
        gate = self.gate(keys=_Broken())
        self.assertRefusal(gate(_Request(self.good)))
        self.assertEqual(self.lines, [
            ('error', tunnel.REFUSAL_LINE % tunnel.GATE_ERROR + ' (RuntimeError)')])

    def test_a_log_that_fails_or_is_absent_changes_no_answer(self):
        def broken(level, text):
            raise OSError('the console is gone')

        for log in (broken, None):
            gate = self.gate(log=log)
            self.assertRefusal(gate(_Request()))
            self.assertRefusal(gate(_Request('x')))
            self.assertEqual(gate(_Request(self.good)), auth.OWNER)

    def test_a_stopped_gate_refuses_every_request_and_reads_none(self):
        gate = self.gate()
        self.assertEqual(gate(_Request(self.good)), auth.OWNER)
        self.assertIs(gate.stop(), True)
        for token in (self.good, None, 'x', self.good):
            request = _Request(token)
            self.assertRefusal(gate(request))
            self.assertEqual(request.read, [], 'a stopped gate read the token')
        self.assertIs(gate.stop(), True)                    # a second stop is the same stop
        self.assertRefusal(gate(_Request(self.good)))
        self.assertEqual(tunnel.CLOSING, 'closing')
        self.assertEqual(self.lines, [('note', 'the tunnel listener refused a request: closing')])

    def test_a_stop_refuses_the_request_held_on_a_key_fetch_at_once_and_admits_none(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(team, timeout):
            entered.set()
            release.wait(httpfix.TIMEOUT)
            return ak.document(ak.KEY_A)                    # the right keys, after the stop

        gate = self.gate(keys=access.KeyCache(ak.TEAM, fetch=fetch))
        answers = []
        held = threading.Thread(target=lambda: answers.append(gate(_Request(self.good))))
        self.addCleanup(held.join, httpfix.TIMEOUT)
        self.addCleanup(release.set)
        held.start()
        self.assertTrue(entered.wait(httpfix.TIMEOUT), 'the token asked for no key')
        self.assertEqual(answers, [])
        started = time.monotonic()
        self.assertIs(gate.stop(), True)
        held.join(httpfix.TIMEOUT)
        waited = time.monotonic() - started
        self.assertFalse(held.is_alive(), 'the held request outlived the stop')
        self.assertFalse(release.is_set())                  # the fetch is still out
        self.assertLess(waited, access.FETCH_DEADLINE_S / 3.0)
        self.assertEqual(len(answers), 1)
        self.assertRefusal(answers[0])
        self.assertEqual(gate.keys(), (False, ''))
        # the fetch ends with the right keys: they open nothing
        release.set()
        for thread in threading.enumerate():
            if thread.name == 'hub-access-keys':
                thread.join(httpfix.TIMEOUT)
                self.assertFalse(thread.is_alive())
        self.assertRefusal(gate(_Request(self.good)))
        self.assertEqual(gate.keys(), (False, ''))
        self.assertEqual(self.lines, [('note', tunnel.REFUSAL_LINE % tunnel.CLOSING)])

    def test_a_token_whose_check_ends_after_the_stop_is_refused(self):
        case = self

        class Late(object):
            """Hands out the right key, after the gate was stopped."""
            asked = 0

            def get(self, kid):
                self.asked += 1
                gate.stop()
                return case.keys.get(kid)

        late = Late()
        gate = self.gate(keys=late)
        self.assertRefusal(gate(_Request(self.good)))
        self.assertEqual(late.asked, 1)
        self.assertEqual(self.lines, [('note', tunnel.REFUSAL_LINE % tunnel.CLOSING)])

    def test_a_key_source_whose_stop_fails_still_leaves_the_gate_shut(self):
        case = self

        class Stubborn(object):
            stopped = 0

            def get(self, kid):
                return case.keys.get(kid)

            def stop(self):
                self.stopped += 1
                raise RuntimeError('a secret word in an error')

        source = Stubborn()
        gate = self.gate(keys=source)
        self.assertEqual(gate(_Request(self.good)), auth.OWNER)
        self.assertIs(gate.stop(), True)
        self.assertEqual(source.stopped, 1)
        self.assertRefusal(gate(_Request(self.good)))
        self.assertEqual(self.lines, [('note', tunnel.REFUSAL_LINE % tunnel.CLOSING)])

    def test_keys_says_whether_there_are_keys_and_fetches_none(self):
        self.assertEqual(self.gate().keys(), (False, ''))   # a dict has no ready(): it cannot say
        fetches, failing, clock = [], [True], [0.0]

        def fetch(team, timeout):
            fetches.append(team)
            if failing[0]:
                raise OSError('the network is down at %s' % PUBLIC)
            return ak.document(ak.KEY_A)

        cache = access.KeyCache(ak.TEAM, fetch=fetch, clock=lambda: clock[0])
        gate = self.gate(keys=cache)
        self.assertEqual(gate.keys(), (False, ''))
        self.assertEqual(fetches, [], 'keys() fetched the keys')
        self.assertRefusal(gate(_Request(self.good)))
        self.assertEqual((gate.keys(), len(fetches)), ((False, 'keys-fetch'), 1))
        clock[0] += access.REFETCH_S + 1                    # an attempt is due: keys() makes none
        self.assertEqual((gate.keys(), len(fetches)), ((False, 'keys-fetch'), 1))
        failing[0] = False
        self.assertEqual(gate(_Request(self.good)), auth.OWNER)
        self.assertEqual((gate.keys(), len(fetches)), ((True, ''), 2))
        clock[0] += access.STALE_LIMIT_S + 1                # keys too old to serve are no keys
        self.assertEqual((gate.keys(), len(fetches)), ((False, ''), 2))
        gate.stop()
        self.assertEqual(gate.keys(), (False, ''))

    def test_keys_never_raises_and_only_true_is_ready(self):
        self.assertEqual(self.gate(keys=_Broken()).keys(), (False, ''))     # no ready(): no keys
        source = _Source(self.keys)
        gate = self.gate(keys=source)
        for said in (False, None, 1, 'yes', RuntimeError('a secret word')):
            source.said = said
            for error in ('', 'keys-fetch at %s' % PUBLIC, None, 7):
                source.error = error
                self.assertEqual(gate.keys(), (False, ''), (said, error))
        source.said = False
        for reason in access.REASONS:
            source.error = reason
            self.assertEqual(gate.keys(), (False, reason))
        source.said = True
        self.assertEqual(gate.keys(), (True, ''))
        self.assertEqual(self.lines, [])

    def test_a_key_source_with_no_ready_never_says_it_has_keys(self):
        case = self

        class Plain(object):
            """Checks tokens, and has nothing to say of itself."""

            def get(self, kid):
                return case.keys.get(kid)

        class Flag(Plain):
            ready = True                # a value, not a ready(): it says nothing either

        class Failed(Plain):
            error = 'keys-fetch'        # no ready(), and a reason: the reason is still told

        for source, said in ((self.keys, (False, '')), (Plain(), (False, '')),
                             (Flag(), (False, '')), (Failed(), (False, 'keys-fetch'))):
            with self.subTest(source=type(source).__name__):
                gate = self.gate(keys=source)
                self.assertEqual(gate.keys(), said)
                # its tokens are checked all the same, and a good one changes no word of it
                self.assertEqual(gate(_Request(self.good)), auth.OWNER)
                self.assertRefusal(gate(_Request(self.good + 'x')))
                self.assertEqual(gate.keys(), said)


class TunnelLockTest(TunnelCase):
    """The machine lock routes over the tunnel: only the Access token reaches them (the owner),
    and the view it gets is the remote one (fixed phrases for warnings that name a path)."""
    LOCK = (api.BASE + '/lock/view', api.BASE + '/lock/stats?days=7',
            api.BASE + '/lock/wait?timeout=0')

    def setUp(self):
        from tests.hubsite import test_api_roles as roles
        TunnelCase.setUp(self)
        self.build(lock=roles.LockDoor())
        self.good = ak.token(ak.KEY_A)

    def test_every_lock_route_is_the_access_owners_alone(self):
        self.assertEqual(sorted(r.pattern for r in api.ROUTES if r.pattern.startswith(
            api.BASE + '/lock/') and r.method == 'GET'), sorted(p.split('?')[0] for p in self.LOCK))
        for path in self.LOCK:
            with self.subTest(path=path):
                # no token, and the local listener's bearer tokens (agent and owner alike)
                self.refused('GET', path)
                self.refused('GET', path, headers=[bearer(self.agent)])
                self.refused('GET', path, headers=[bearer(self.owner)])
                # the agent is refused on the local listener too
                r = get(self.lport, path, self.agent)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'), path)
                r = self.ask('GET', path, token=self.good)
                self.assertEqual((r.status, r.json().get('error')), (200, None), path)

    def test_the_tunnel_gets_the_remote_view_and_the_local_owner_does_not(self):
        r = self.ask('GET', api.BASE + '/lock/view', token=self.good)
        self.assertEqual((r.status, r.json()['view']), (200, {'remote': True}))
        r = self.ask('GET', api.BASE + '/lock/wait?timeout=0', token=self.good)
        self.assertEqual((r.status, r.json()['view']), (200, {'remote': True}))
        # a client of the local listener cannot claim to be the tunnel
        r = get(self.lport, api.BASE + '/lock/view', self.owner,
                headers=[(httpd.VIA_HEADER, httpd.TUNNEL_VIA)])
        self.assertEqual((r.status, r.json().get('view')), (200, {'remote': False}))
        r = get(self.lport, api.BASE + '/lock/view', self.owner)
        self.assertEqual((r.status, r.json()['view']), (200, {'remote': False}))



class TunnelLockWriteTest(TunnelCase):
    """The machine lock's writes over both listeners: the Access owner (strong) reaches every
    one through the tunnel; on the local listener the owner token reaches the LOCK_WRITE ones
    and no reservation or division write; an agent reaches none on either."""

    def setUp(self):
        from tests.hubsite import test_api_roles as roles
        self.roles = roles
        TunnelCase.setUp(self)
        self.door = roles.LockDoor()
        self.build(lock=self.door)
        self.good = ak.token(ak.KEY_A)

    def writes(self):
        out = []
        for route in api.ROUTES:
            if route.method == 'POST' and route.pattern.startswith(api.BASE + '/lock/'):
                _seed, body, _query = self.roles.row(route, api.OWNER)
                out.append((route, self.roles.path(route, 'X-1'), body))
        return out

    def test_every_lock_write_over_the_tunnel_is_the_access_owners(self):
        writes = self.writes()
        self.assertEqual(len(writes), 8)
        for route, path, body in writes:
            with self.subTest(route=route.pattern):
                self.refused('POST', path, obj=body)
                self.refused('POST', path, obj=body, headers=[bearer(self.agent)])
                self.refused('POST', path, obj=body, headers=[bearer(self.owner)])
                before = len(self.door.wrote)
                r = self.ask('POST', path, token=self.good, obj=body)
                self.assertEqual((r.status, r.json().get('error')), (200, None))
                self.assertEqual(len(self.door.wrote), before + 1)
                self.assertIs(self.door.wrote[-1][2], True, 'a tunnel write that is not remote')
                self.assertEqual(r.json()['writes'], {'lock': True, 'strong': True, 'why': ''})

    def test_on_the_local_listener_the_owner_token_is_not_strong(self):
        for route, path, body in self.writes():
            with self.subTest(route=route.pattern):
                before = len(self.door.wrote)
                r = post(self.lport, path, body, self.agent)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                # a local client that names the tunnel is still the local listener's
                r = post(self.lport, path, body, self.owner, via=httpd.TUNNEL_VIA)
                if route.role == api.LOCK_WRITE:
                    self.assertEqual((r.status, r.json().get('error')), (200, None))
                    self.assertIs(self.door.wrote[-1][2], False)
                    self.assertEqual(r.json()['writes']['lock'], True)
                    self.assertEqual(r.json()['writes']['strong'], False)
                else:
                    self.assertEqual(route.role, api.APPROVER)
                    self.assertEqual((r.status, r.code()), (403, 'forbidden'))
                    self.assertEqual(len(self.door.wrote), before)

if __name__ == '__main__':
    unittest.main()
