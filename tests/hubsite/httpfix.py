"""Fixtures for the listener tests: a store and token files in temp folders, one Listener on
port 0, and a small HTTP client. Nothing here binds a fixed port or leaves the loopback address,
and every listener a test makes is stopped, with its threads, before the test ends."""
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest

from kitlib.hubsite import api, auth, httpd
from kitlib.hubsite.store import Store

CLOCK = lambda: '2026-01-02T03:04:05+00:00'      # noqa: E731
TIMEOUT = 20                    # a client socket never waits longer; no test sleeps
OPTIONS = [{'label': 'a', 'description': 'b'}]
# the one finding the stand-in views show (GET findings), so a finding mark has one to name
FINDING = {'repo': 'r', 'check': 'stale-pull', 'target': 'X-1', 'text': 't', 'fix': 'f',
           'age': None, 'fingerprint': '0123456789abcdef', 'new': False, 'started': None}


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
        return {'git': None, 'backlog': None}

    def record(self, repo, id):
        return {}

    def record_template(self, repo):
        return {}

    def record_add(self, repo, id, summary, scopes, effort, ref):
        return {}

    def record_edit(self, repo, id, version, changes, unset, body, ruling):
        return {}

    def record_delete(self, repo, id, why, ruling):
        return {}

    def record_unblock(self, repo, id, on, reason, ruling):
        return {}

    def findings(self):
        return [dict(FINDING)]


class Reply(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers          # [(name, value)], as sent
        self.body = body

    def header(self, name):
        """Every value of the header, in order."""
        return [v for n, v in self.headers if n.lower() == name.lower()]

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def code(self):
        """The error code of a refusal, or None."""
        return (self.json().get('error') or {}).get('code')


def send(port, method, path, headers=(), body=None, host=None, timeout=TIMEOUT):
    """One request to the loopback listener at `port`. `headers` is [(name, value)]; `host` is
    the Host header (None: the listener's own, False: none at all)."""
    conn = http.client.HTTPConnection(httpd.LOOPBACK, port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is None:
            host = '%s:%d' % (httpd.LOOPBACK, port)
        if host is not False:
            conn.putheader('Host', host)
        for name, value in headers:
            conn.putheader(name, value)
        if body is not None and 'content-length' not in [n.lower() for n, _ in headers]:
            conn.putheader('Content-Length', str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        return Reply(r.status, r.getheaders(), r.read())
    finally:
        conn.close()


def read_all(sock):
    """Everything the listener sends on `sock` until it closes its sending side."""
    chunks = []
    while True:
        got = sock.recv(65536)
        if not got:
            return b''.join(chunks)
        chunks.append(got)


def raw(port, data, timeout=TIMEOUT):
    """Send these bytes, close the sending side, and return everything the listener answers."""
    with socket.create_connection((httpd.LOOPBACK, port), timeout=timeout) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        return read_all(s)


def write_head(port, token, length, path=None):
    """The request line and headers of a good write of `length` body bytes, as bytes."""
    return ('POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n'
            'Content-Type: application/json\r\nOrigin: http://127.0.0.1:%d\r\n'
            'Content-Length: %d\r\n\r\n' % (path or api.BASE + '/todos', port, token, port,
                                            length)).encode('ascii')


def bearer(token):
    return ('Authorization', 'Bearer ' + token)


def write_headers(port, token=None, via=None, host=None):
    """The headers a write must carry, for the listener at `port`."""
    out = [('Content-Type', 'application/json'),
           ('Origin', 'http://%s' % (host or '%s:%d' % (httpd.LOOPBACK, port)))]
    if token:
        out.append(bearer(token))
    if via:
        out.append((httpd.VIA_HEADER, via))
    return out


def post(port, path, obj=None, token=None, via=None, headers=None):
    body = b'' if obj is None else json.dumps(obj).encode('utf-8')
    return send(port, 'POST', path, write_headers(port, token, via) + list(headers or ()), body)


def get(port, path, token=None, headers=()):
    return send(port, 'GET', path, ([bearer(token)] if token else []) + list(headers))


def http_threads():
    """The names of the listener threads alive now."""
    return sorted(t.name for t in threading.enumerate() if t.name.startswith('hub-http'))


class ListenerCase(unittest.TestCase):
    """A store, the two token files, and listeners that are stopped when the test ends."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-hubhttp-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store.open(os.path.join(self.root, 'store'), clock=CLOCK)
        self.addCleanup(self.store.close)
        self.tokens = os.path.join(self.root, 'tokens')
        auth.ensure_tokens(self.tokens)
        self.owner = auth.read_token(self.tokens, auth.OWNER)
        self.agent = auth.read_token(self.tokens, auth.AGENT)
        self.api = api.Api(self.store, views=Views(), clock=CLOCK)
        self.lines = []                 # (level, text) the listeners logged

    def log(self, level, text):
        self.lines.append((level, text))

    def listener(self, api_=None, gate='local', start=True, **kw):
        """A started Listener on a free port, with the local gate and pairing routes unless the
        test gives its own."""
        kw.setdefault('host_rule', httpd.local_hosts)
        kw.setdefault('log', self.log)
        lis = httpd.Listener((httpd.LOOPBACK, 0), api_ or self.api, None, **kw)
        self.addCleanup(self.close, lis)
        if gate == 'local':
            creds = auth.Credentials(self.tokens, port=lis.port)
            lis.gate = httpd.local_gate(creds)
            if 'routes' not in kw:
                lis.routes.update(httpd.pair_routes(creds))
            lis.creds = creds
        else:
            lis.gate = gate
        if start:
            self.assertTrue(lis.start())
        return lis

    def connect(self, lis):
        """An open connection to `lis` that has sent nothing; closed when the test ends."""
        s = socket.create_connection((httpd.LOOPBACK, lis.port), timeout=TIMEOUT)
        self.addCleanup(s.close)
        return s

    def close(self, lis):
        """Stop `lis`: its door, then the store (which ends a wait in flight), then its threads."""
        lis.close_door()
        self.store.close()
        self.assertTrue(lis.join(TIMEOUT), 'a handler thread outlived its listener')

    def errors(self):
        return [text for level, text in self.lines if level == 'error']
