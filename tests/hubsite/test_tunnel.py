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
import unittest

from kitlib.hubsite import access, api, auth, httpd, serve, tunnel
from kitlib.hubsite import settings as hub_settings
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
        self.asked.append(team)
        return self.keys

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

    def test_a_valid_token_is_served_as_the_owner(self):
        r = self.ask('GET', HEALTH, token=self.good)
        self.assertEqual(r.status, 200)
        # the store folder is in the owner's answer only
        self.assertEqual(r.json()['store'], os.path.abspath(self.settings.store))
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
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.asked, [ak.TEAM])
        self.assertNoSecret(self.good)


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
        self.assertEqual(sorted(tunnelled.routes), [('GET', HEALTH)])
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


if __name__ == '__main__':
    unittest.main()
