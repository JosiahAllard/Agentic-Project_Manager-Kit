"""kitlib.hubsite.httpd: one loopback HTTP listener in front of the hub's Api.

  Listener(addr, api, gate, static_dir=None, host_rule=None, routes=None, via='', log=None,
           max_waits=MAX_WAITS, max_body=MAX_BODY, max_connections=MAX_CONNECTIONS,
           origin_scheme='http', via_header=True, agent_words=False, origin_refused=None)
      .port .url .waiting          the bound port (addr may name port 0), its URL, waits blocked now
      .closing                     close_door() was called
      .start()                     serve on a thread; .stop(timeout) = close_door() then join()
      .close_door()                stop accepting; requests being answered go on ("Closing")
      .drain(timeout)              wait for the requests in flight that are not long waits
      .join(timeout)               wait for every handler thread
      .await_waits(n, timeout)     block until n waits are counted (tests need no sleep)
      .await_idle(n, timeout)      ... until n connections are open with no request read yet
      .await_reads(n, timeout)     ... until n writes are having their body read
  Request   .method .target .path .query .headers .client .port, then .role .strong .via .body
  Response(status, body=b'', content_type=JSON, headers=())
  json_response(status, obj, headers=())    refusal(status, code, message, field='', headers=())
  strong_words(request)            {'strong': True} for the strong owner's request, else {}
  local_hosts(host, port)          the local listener's host_rule
  local_gate(creds)                the local listener's gate, over kitlib.hubsite.auth.Credentials
  pair_routes(creds)               the local listener's pairing routes (PAIR_ROUTE, PAIR_PATH)
  strong_pair_route(creds)         the tunnel listener's strong pairing route (STRONG_PAIR_ROUTE)
  stop_route(creds, request_stop, identity)    the local listener's stop route (STOP_ROUTE)
  static_file(static_dir, path)    the file a URL path names under static_dir, or None

The address. Only LOOPBACK is bound: any other host in `addr` (a name, the empty string, the any
address, an IPv6 one) is BindRefused in code, whatever a setting says. The port is bound by the
constructor, exclusively (a second process cannot share or take it), so a taken port is a
BindRefused at once and `.port` is real before start().

One request, in this order, for every method and every path:
  1. gate(request). It answers a role (auth.OWNER, auth.AGENT), None (no role: the Api refuses
     its routes with `forbidden`; a static file needs none) or a Response, which is sent as it
     stands and ends the request. A listener with no gate refuses everything, and a gate that
     raises is a 500: neither lets a request through. `listener.gate` may be set until start().
     A gate may also set request.strong = True: the caller is the strong owner, the one an
     api.APPROVER route answers. It counts only as True itself and only for auth.OWNER; for
     any other answer it is set back to False, and the Api is told strong=True for that request
     alone (strong_words). kitlib.hubsite.tunnel's gate sets it for a valid Access token, and
     local_gate for the cookie of a strong session alone (auth, "Strong sessions"), which only
     a code from strong_pair_route pairs: the owner token, a browser paired from it and an
     agent are never strong.
  2. host_rule(the Host header or None, the port), when the listener has one: False is a 403.
     local_hosts allows 127.0.0.1:<port> and localhost:<port>, which is what stops a page on
     another origin from reaching this server under a name it controls (DNS rebinding).
  3. The method: GET, HEAD and POST; anything else is a 405. No response carries a CORS header.
  4. A POST is a write: Content-Type application/json (415), exactly one Origin whose scheme is
     the listener's origin_scheme (`http` for the local listener, which speaks nothing else;
     `https` for one that only a TLS front reaches) and whose host equals the Host header (403;
     when the two hosts differ, origin_refused(origin host, Host) is called first, if the
     listener was given one: the tunnel listener logs it, kitlib.hubsite.tunnel.host_note),
     a Content-Length (411) of at most max_body bytes (413, the body is not read) and a body
     that is JSON or empty (400). Once the door is closed (close_door) a write that arrives on
     an open connection, or whose body is still arriving, is a 503 `store`; the rest of its
     body is not waited for. So after close_door() and then a drain() that answered True, no
     write is in flight and none can begin.
  5. Routing: `routes` ({(method, path): fn(request) -> Response}), then every path under /api/
     through Api.call with the request's role, body, query and via, then for GET and HEAD a
     file of static_dir ("/" is INDEX), else 404.
A request line or a header block http.server cannot read, and a request line with no HTTP
version (it carries no headers), is refused before step 1 with http.server's own status (400,
414, 431, 505), a fixed body and no routing. A request answered before all it sent was read (a
refusal ahead of the body, a body over the cap, an unreadable request) is answered first; then
the sending side is closed and what the client still sends is read away, never parsed, for at
most LINGER_S seconds and LINGER_BYTES bytes, so the answer reaches the client and not a reset.

via. A caller names its transport in the VIA_HEADER header (`web`, `cli`, `desktop-mcp`,
`relay:<source>`): a short lower-case token, else the listener's own `via` is used ('' lets the
Api record `api`). A listener made with via_header=False never reads that header: every request
on it carries the listener's own `via`, whatever the client sent. That is the tunnel listener
(kitlib.hubsite.tunnel), whose writes are all recorded as `tunnel`. TUNNEL_VIA is that
listener's word and no client's: a header that says exactly it is read as no header at all, so
a listener that reads the header records its own `via` then (the local one: `api`) and only
the tunnel listener ever records `tunnel`.

Waits. GET wait blocks in the Api for up to api.WAIT_MAX_S. At most max_waits requests may be in
it at once on one listener: the next is refused at once with 429 `busy` and a Retry-After, and
takes no slot. The counters are guarded by one lock that is held for a few statements and never
across a wait, a body read or a response.

Closing. A read on a connection waits for bytes in slices of POLL_S seconds, for at most
SOCKET_TIMEOUT_S in all, and looks at the door between slices. After close_door() a read takes
what has arrived already and then meets the end of the input, so a connection that never sent
its request (a browser's spare connection) ends at once and join() is not kept waiting for it,
and a write whose body is still arriving is refused (step 4) so drain() is not kept waiting
either. What close_door() does not end: a request the Api is still answering, a response a
client is slow to take (SOCKET_TIMEOUT_S a send) and the reading away after an early answer
(LINGER_S).

What a client is told. Every answer is JSON in the Api's error shape, with the Api's codes plus
`busy`. An exception in a handler is a 500 `internal` with a fixed message: its text and its
traceback go to `log` only, as one `error` line. That holds for an error the Api did not expect
too (Api.call is asked to raise it, raise_unexpected), for the owner as for anyone else. A
listener made with agent_words=True asks Api.call for the agent's wording of every error answer
(agent_words), whatever the role: the Api's `store` refusal, whose own text names the store's
folder and its files, is then the fixed phrase of api.AGENT_SAYS for the owner too, with the
status, the code and the field unchanged. That is the tunnel listener, whose answers leave this
machine; the local listener is made without it and tells the owner the error's own text. Every
response closes its connection and carries SECURITY_HEADERS; none names the software.

log(level, text). level is 'access' (one line a request: client address, method, path, status,
role) or 'error'. The path is logged without its query, and no header is ever logged, so a line
holds no token, no cookie and no pairing code. With no `log` nothing is written anywhere.

Static files. Only a regular file below static_dir whose every path segment is plain (letters,
digits, `_`, `-`, `.`, not starting with a dot) and whose extension is in CONTENT_TYPES. The
path is never percent-decoded, so an encoded dot or separator names nothing; `..`, a backslash,
a drive letter, an alternate stream, a device name and a link that leaves the folder name
nothing either. A missing INDEX is a plain 404.

Nothing here reads a setting, opens the store, prints or exits; kitlib.hubsite.serve wires it.
"""
import http.server
import io
import json
import os
import re
import select
import socket
import threading
import time
import traceback
import urllib.parse

from . import api as _api
from . import auth

LOOPBACK = '127.0.0.1'
MAX_BODY = 1 << 20              # bytes of one write's body
MAX_WAITS = 32                  # requests blocked in GET wait at once, per listener
MAX_CONNECTIONS = 128           # handler threads at once; a connection past it is closed
MAX_STATIC = 8 << 20            # bytes of one static file
MAX_QUERY_FIELDS = 64
SOCKET_TIMEOUT_S = 30           # one read or write on a connection
POLL_S = 0.1                    # the slice a read waits in before it looks at the door again
ORIGIN_SCHEMES = ('http', 'https')
LINGER_S = 2                   # reading away what a client still sends after its answer,
LINGER_BYTES = 4 << 20          # and never more than this much of it
METHODS = ('GET', 'HEAD', 'POST')
JSON = 'application/json'
INDEX = 'index.html'
VIA_HEADER = 'X-Hub-Via'
TUNNEL_VIA = 'tunnel'               # the tunnel listener's own source: no VIA_HEADER may claim it
PAIR_ROUTE = _api.BASE + '/pair'    # POST, the owner's token: a one-time link for the browser
PAIR_PATH = '/pair'                 # GET ?code=...: redeems it and sets the session cookie
UNPAIR_ROUTE = _api.BASE + '/unpair'    # POST, the owner's token: ends every browser session
# POST on the tunnel listener, the strong owner only: a one-time link that pairs the PC's browser
# as the strong owner on the local listener (strong_pair_route)
STRONG_PAIR_ROUTE = _api.BASE + '/pair/strong'
STOP_ROUTE = _api.BASE + '/stop'    # POST, the owner's token: the server closes (hub.py stop)
BUSY = 'busy'
RETRY_AFTER_S = 1
INTERNAL = 'the server could not answer this request'
UNREADABLE = 'the request could not be read'
CLOSING = 'the server is closing: nothing more is written'
SECURITY_HEADERS = (
    ('X-Content-Type-Options', 'nosniff'),
    ('Cache-Control', 'no-store'),
    ('Referrer-Policy', 'no-referrer'),
    ('X-Frame-Options', 'DENY'),
    ('Cross-Origin-Resource-Policy', 'same-origin'),
)
CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': JSON,
    '.txt': 'text/plain; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
    '.webmanifest': 'application/manifest+json',
}

_SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')
_DEVICES = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % i for i in range(1, 10)]
                     + ['lpt%d' % i for i in range(1, 10)])
_VIA = re.compile(r'[a-z0-9][a-z0-9:._-]{0,39}\Z')
_DIGITS = re.compile(r'[0-9]{1,12}\Z')


class BindRefused(Exception):
    """The listener's address is not LOOPBACK, or its port could not be bound."""


class Request(object):
    """One request as the gate, a route and the Api see it. `headers` is the http.client message
    http.server parsed, itself: every value of a repeated header is in it. `path` is the target
    before its `?`, never percent-decoded. `query` ({name: first value}), `role`, `via` and
    `body` (the decoded JSON of a write, else None) are filled in as the request passes each
    step; a gate sees them empty. `reply_headers` ((name, value) pairs) is what the gate adds to
    whatever answer the request gets, refusals too; they go before the answer's own headers, so
    a header the answer sets as well (a route's Set-Cookie) comes last and wins. `strong` starts
    False; only a gate sets it (module docstring, step 1)."""

    __slots__ = ('method', 'target', 'path', 'query_text', 'query', 'headers', 'client', 'port',
                 'role', 'via', 'body', 'reply_headers', 'strong')

    def __init__(self, method, target, headers, client='', port=None):
        self.method = method
        self.target = target
        self.path, _, self.query_text = target.partition('?')
        self.query = {}
        self.headers = headers
        self.client = client
        self.port = port
        self.role = None
        self.via = ''
        self.body = None
        self.reply_headers = ()
        self.strong = False

    def header(self, name):
        """The header's value when it was sent exactly once, else None."""
        values = self.headers.get_all(name) if self.headers is not None else None
        return values[0] if values and len(values) == 1 else None


class Response(object):
    """What a gate or a route answers: a status, the body's bytes, its content type and extra
    (name, value) headers."""

    __slots__ = ('status', 'body', 'content_type', 'headers')

    def __init__(self, status, body=b'', content_type=JSON, headers=()):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = tuple(headers)


def json_response(status, obj, headers=()):
    body = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return Response(status, body, JSON, headers)


def refusal(status, code, message, field='', headers=()):
    """A refusal in the Api's error shape."""
    return json_response(status, _api.error_body(code, message, field), headers)


def strong_words(request):
    """{'strong': True} for a request its gate marked as the strong owner, else {}: the words a
    route adds to its Api.call (module docstring, step 1)."""
    return {'strong': True} if request.strong is True and request.role == auth.OWNER else {}


def local_hosts(host, port):
    """True when the Host header `host` names this machine's loopback listener at `port`."""
    return isinstance(host, str) and host.lower() in ('%s:%d' % (LOOPBACK, port),
                                                      'localhost:%d' % port)


def local_gate(creds):
    """The local listener's gate: the role `creds` (auth.Credentials) gives the request's
    Authorization header or session cookie, or None. It never answers a Response: a request
    with no credential still reaches the page's files, and the Api refuses it everything else.
    A session cookie that renewed its session (Credentials.seen) is sent again with the
    answer, auth.cookie_header on creds.port, so the browser's copy lives as long. The cookie of
    a strong session marks the request strong (module docstring, step 1); nothing else does."""
    def gate(request):
        cookies = auth.parse_cookies('; '.join(request.headers.get_all('Cookie') or ()))
        seen = creds.seen(request.headers, cookies)
        if seen.renewed is not None:
            request.reply_headers = (('Set-Cookie', auth.cookie_header(seen.renewed,
                                                                       creds.port)),)
        if seen.strong is True and seen.role == auth.OWNER:
            request.strong = True
        return seen.role
    return gate


def pair_routes(creds):
    """The routes that pair the PC's browser, and unpair every one, for a Listener's `routes`.

    POST PAIR_ROUTE, the owner's token only: {"ok", "path", "ttl"}; `path` is PAIR_PATH with a
    fresh one-time code, good for `ttl` seconds. The Authorization header alone must give the
    owner role: a session cookie is refused, so a paired browser (or a script in the page's
    origin) cannot make the code of its own successor and a session ends at auth.SESSION_TTL_S
    unless it is used (auth, "Sessions").
    GET PAIR_PATH?code=...: redeems the code, sets the session cookie (auth.cookie_header on
    creds.port) and sends the browser to "/"; a code that is unknown, expired or already used
    is a 403. A code from strong_pair_route is redeemed here too and pairs a strong session.
    POST UNPAIR_ROUTE, the owner's token only, as PAIR_ROUTE: {"ok": true}; every browser
    session and every pairing code not yet redeemed ends (Credentials.unpair_all)."""
    def owners(request):
        return _bearer_owner(creds, request)

    def new(request):
        if not owners(request):
            return refusal(403, 'forbidden', "only the owner's token may pair a browser, never "
                           'a browser session', 'role')
        code = creds.new_pairing_code()
        return json_response(200, {'ok': True, 'ttl': auth.PAIR_TTL_S,
                                   'path': '%s?code=%s' % (PAIR_PATH, code)})

    def redeem(request):
        value = creds.pair(request.query.get('code', ''))
        if value is None:
            return refusal(403, 'forbidden', 'this pairing link was used already or has run '
                           'out: ask for a new one', 'code')
        return Response(303, b'', JSON, (('Location', '/'),
                                         ('Set-Cookie', auth.cookie_header(value, creds.port))))

    def unpair(request):
        if not owners(request):
            return refusal(403, 'forbidden', "only the owner's token may unpair the browsers, "
                           'never a browser session', 'role')
        creds.unpair_all()
        return json_response(200, {'ok': True})

    return {('POST', PAIR_ROUTE): new, ('GET', PAIR_PATH): redeem,
            ('POST', UNPAIR_ROUTE): unpair}


def strong_pair_route(creds):
    """The route that lets the owner logged in through Cloudflare Access make the PC's browser
    the strong owner on the local listener (auth, "Strong sessions"), for the tunnel listener's
    `routes` alone: the local listener is never given it.

    POST STRONG_PAIR_ROUTE, the strong owner only (the request its gate marked strong;
    strong_words): {"ok", "ttl", "path", "url"}; `path` is PAIR_PATH with a fresh one-time strong
    code (Credentials.new_pairing_code(strong=True)), good for `ttl` seconds, and `url` that path
    on the local listener (LOOPBACK and creds.port; absent for a creds with no port), the link
    to open on the PC itself. Anyone
    else is a 403 `forbidden` on the field `role`, and no code is made."""
    def new(request):
        if not strong_words(request):
            return refusal(403, 'forbidden', 'only the owner logged in through Cloudflare '
                           'Access may pair a browser as the strong owner', 'role')
        code = creds.new_pairing_code(strong=True)
        path = '%s?code=%s' % (PAIR_PATH, code)
        said = {'ok': True, 'ttl': auth.PAIR_TTL_S, 'path': path}
        if isinstance(creds.port, int) and not isinstance(creds.port, bool):
            said['url'] = 'http://%s:%d%s' % (LOOPBACK, creds.port, path)
        return json_response(200, said)

    return {('POST', STRONG_PAIR_ROUTE): new}


def _bearer_owner(creds, request):
    """True when the request's Authorization header alone gives the owner role: role_for with
    no cookies, so a browser session never counts, whatever else came with it."""
    return request.role == auth.OWNER and creds.role_for(request.headers) == auth.OWNER


def stop_route(creds, request_stop, identity):
    """The route that asks the server to stop, for the local listener's `routes` alone: the
    tunnel listener is never given it, so no request from off this machine can reach it.

    POST STOP_ROUTE, the owner's token only, as PAIR_ROUTE (a browser session is refused, so
    neither the page nor a script in its origin can end the server): calls request_stop() and
    answers {"ok": true, "stopping": true} plus `identity` ({"pid", "ctime"} of the serving
    process, so the caller can wait for that process and no other to end). The route only
    asks: the server's own loop closes it (kitlib.hubsite.serve.run), the same clean close as
    Ctrl+C, after this answer is sent."""
    def stop(request):
        if not _bearer_owner(creds, request):
            return refusal(403, 'forbidden', "only the owner's token may stop the server, never "
                           'a browser session', 'role')
        request_stop()
        return json_response(200, dict(identity, ok=True, stopping=True))

    return {('POST', STOP_ROUTE): stop}


def static_file(static_dir, path):
    """The regular file below `static_dir` that the URL path `path` names, as (full path,
    content type), or None (module docstring, "Static files")."""
    if not static_dir or not isinstance(path, str) or not path.startswith('/'):
        return None
    parts = (path[1:] or INDEX).split('/')
    for part in parts:
        if not _SEGMENT.match(part) or '..' in part or part.endswith('.') \
                or part.split('.')[0].lower() in _DEVICES:
            return None
    ctype = CONTENT_TYPES.get(os.path.splitext(parts[-1])[1].lower())
    if ctype is None:
        return None
    try:
        root = os.path.realpath(static_dir)
        full = os.path.realpath(os.path.join(root, *parts))
        inside = os.path.normcase(os.path.commonpath([root, full])) == os.path.normcase(root)
    except (OSError, ValueError):
        return None
    if not inside or os.path.normcase(full) == os.path.normcase(root) or not os.path.isfile(full):
        return None
    return full, ctype


def _query(text):
    """{name: first value} of a query string; ValueError for one with too many fields."""
    found = urllib.parse.parse_qs(text, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS)
    return {name: values[0] for name, values in found.items()}


def _no_constant(name):
    """json.loads would read NaN and Infinity, which are not JSON: a body with one is refused."""
    raise ValueError('%s is not JSON' % name)


def _shown(text, limit=200):
    """`text` as a log line may carry it: ASCII, no control character, cut at `limit`."""
    return ascii(str(text))[1:-1][:limit]


class _Server(http.server.ThreadingHTTPServer):
    """The socket and the accept loop of one Listener."""
    allow_reuse_address = False     # on Windows the option would let a second process bind too
    allow_reuse_port = False
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(self, addr, handler, listener):
        self.listener = listener
        http.server.ThreadingHTTPServer.__init__(self, addr, handler)

    def server_bind(self):
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        # no socket.getfqdn here (HTTPServer's own asks the resolver, which can take seconds)
        self.server_name = LOOPBACK
        self.server_port = self.server_address[1]

    def process_request(self, request, client_address):
        self.listener._spawn(self, request, client_address)

    def work(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:           # noqa: BLE001 - a dropped connection ends no thread loudly
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def handle_error(self, request, client_address):
        self.listener._say('error', 'connection from %s ended: %s'
                           % (_shown(client_address[0] if client_address else '-'),
                              traceback.format_exc(limit=4).strip().splitlines()[-1]))


class _Input(io.RawIOBase):
    """What one connection sends, read so that a closing listener is not kept waiting (module
    docstring, "Closing"). A read waits for bytes in slices of POLL_S seconds, `timeout` seconds
    in all (then socket.timeout, as a plain socket read raises); once the listener's door is
    closed it takes only what has arrived and then answers the end of the input. The read polls
    because a shutdown() from the closing thread does not wake a read that is already waiting on
    Windows."""

    def __init__(self, sock, listener, timeout):
        io.RawIOBase.__init__(self)
        self._sock = sock
        self._listener = listener
        self._timeout = timeout

    def readable(self):
        return True

    def readinto(self, into):
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            closing = self._listener.closing
            if closing:
                wait = 0
            elif deadline is None:
                wait = POLL_S
            else:
                wait = min(POLL_S, deadline - time.monotonic())
                if wait <= 0:
                    raise socket.timeout('timed out')
            if select.select([self._sock], [], [], wait)[0]:
                return self._sock.recv_into(into)
            if closing:
                return 0


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = SOCKET_TIMEOUT_S
    server_version = 'hub'
    sys_version = ''

    def __getattr__(self, name):
        # every method, known or not, reaches _handle: the gate answers before the method is read
        if name.startswith('do_'):
            return self._handle
        raise AttributeError(name)

    def setup(self):
        http.server.BaseHTTPRequestHandler.setup(self)
        listener = self.server.listener
        self.rfile.close()          # http.server's own reader would sit in one recv for `timeout`
        self.rfile = io.BufferedReader(_Input(self.connection, listener, self.timeout))
        self._idle = True           # no request read yet (Listener.await_idle)
        listener._mark('idle', 1)

    def finish(self):
        self._seen()
        http.server.BaseHTTPRequestHandler.finish(self)

    def _seen(self):
        """This connection's request was read, or never will be: it is idle no longer."""
        if self.__dict__.get('_idle'):
            self._idle = False
            self.server.listener._mark('idle', -1)

    def handle_expect_100(self):
        return True                 # no 100 Continue: the answer itself tells the client

    def log_message(self, format, *args):     # noqa: A002 - http.server's own signature
        pass                        # http.server would write to stderr; the listener logs

    def send_error(self, code, message=None, explain=None):
        """http.server's own refusals (a request line or headers it cannot read): a fixed body."""
        self._seen()
        self._finish(refusal(code, 'invalid', UNREADABLE, 'request'), None)
        self._hang_up(self.server.listener)

    def _handle(self):
        listener = self.server.listener
        self._seen()
        if self.request_version == 'HTTP/0.9' or not hasattr(self.headers, 'get_all'):
            # a request line with no version: it has no headers, so no gate could judge it
            return self.send_error(400)
        request = Request(self.command or '', self.path or '', self.headers,
                          self.client_address[0] if self.client_address else '', listener.port)
        # a request that says it carries a body has unread input until _read_body took it all
        self._unread = bool(self.headers.get_all('Content-Length')
                            or self.headers.get_all('Transfer-Encoding'))
        listener._enter()
        try:
            try:
                response = self._answer(listener, request)
                if not isinstance(response, Response):
                    raise TypeError('a route answered %s, not a Response' % type(response).__name__)
            except Exception:       # noqa: BLE001 - the client gets an answer, the log the cause
                listener._say('error', '%s %s failed:\n%s' % (
                    _shown(request.method, 20), _shown(request.path), traceback.format_exc()))
                response = refusal(500, 'internal', INTERNAL)
            self._finish(response, request)
        finally:
            listener._leave()
        if self._unread:
            self._hang_up(listener)

    def _hang_up(self, listener):
        """After an answer to a request that was not read to its end: close the sending side,
        then read away what the client still sends, for at most LINGER_S seconds and
        LINGER_BYTES bytes. Closing with input unread would reset the connection, and the reset
        can reach the client before the answer does."""
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        deadline = time.monotonic() + LINGER_S
        left = LINGER_BYTES
        try:
            while left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                got = self.connection.recv(65536)
                if not got:
                    break
                left -= len(got)
        except OSError:
            pass

    def _finish(self, response, request):
        self.close_connection = True
        client = self.client_address[0] if self.client_address else '-'
        if request is None:
            line = '%s - - %d -' % (_shown(client), response.status)
        else:
            line = '%s %s %s %d %s' % (_shown(client), _shown(request.method, 20),
                                       _shown(request.path), response.status, request.role or '-')
        # logged before it is sent: a client that has its answer finds the line written
        self.server.listener._say('access', line)
        try:
            self.send_response_only(response.status)
            self.send_header('Content-Type', response.content_type)
            self.send_header('Content-Length', str(len(response.body)))
            extra = request.reply_headers if request is not None else ()
            for name, value in SECURITY_HEADERS + tuple(extra) + response.headers:
                self.send_header(name, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            if getattr(self, 'command', None) != 'HEAD' and response.body:
                self.wfile.write(response.body)
            self.wfile.flush()
        except OSError:
            pass                    # the client went away: nothing to tell it

    def _answer(self, listener, request):
        # 1. the gate, before anything is read from the method, the path or the body
        gate = listener.gate
        if gate is None:
            return refusal(403, 'forbidden', 'this listener accepts no request', 'role')
        answer = gate(request)
        if isinstance(answer, Response):
            return answer
        request.role = answer if answer in auth.ROLES else None
        # a gate's strong mark holds for the owner alone, and only as True itself
        request.strong = request.strong is True and request.role == auth.OWNER
        # 2. the name the client reached this server under
        host = request.header('Host')
        if listener.host_rule is not None and not listener.host_rule(host, listener.port):
            return refusal(403, 'forbidden', 'this server does not answer under that host name',
                           'host')
        # 3. the method
        if request.method not in METHODS:
            return refusal(405, 'invalid', 'the method must be one of %s' % ', '.join(METHODS),
                           'method', (('Allow', ', '.join(METHODS)),))
        if not request.path.startswith('/'):
            return refusal(400, 'invalid', 'the request target must be a path', 'path')
        try:
            request.query = _query(request.query_text)
        except ValueError:
            return refusal(400, 'invalid', 'the query has too many fields', 'query')
        request.via = self._via(listener)
        # 4. a write's own rules
        if request.method == 'POST':
            # read after _enter() counted this request: a write that passes here is one drain()
            # waits for, and one that arrives later is refused (Listener.closing)
            if listener.closing:
                return refusal(503, 'store', CLOSING, 'store')
            refused = self._read_body(listener, request, host)
            if refused is not None:
                return refused
        # 5. routing
        route = listener.routes.get((request.method, request.path))
        if route is not None:
            return route(request)
        if request.path.startswith('/api/'):
            return self._api(listener, request)
        if request.method in ('GET', 'HEAD'):
            found = static_file(listener.static_dir, request.path)
            if found is not None:
                try:
                    with open(found[0], 'rb') as f:
                        body = f.read(MAX_STATIC + 1)
                except OSError:
                    body = None
                if body is not None and len(body) <= MAX_STATIC:
                    return Response(200, body, found[1])
        return refusal(404, 'not_found', 'there is nothing at this path', 'path')

    def _via(self, listener):
        if not listener.via_header:
            return listener.via     # this port names the source itself: the client is not asked
        said = self.headers.get_all(VIA_HEADER) or ()
        # TUNNEL_VIA is what the tunnel listener records, and that listener never comes here: a
        # client that says it is recorded as one that said nothing
        if len(said) == 1 and _VIA.match(said[0].strip()) and said[0].strip() != TUNNEL_VIA:
            return said[0].strip()
        return listener.via

    def _read_body(self, listener, request, host):
        """Check a write's headers and read its body into request.body; a Response when the
        write is refused, else None."""
        ctype = request.header('Content-Type')
        if ctype is None or ctype.split(';')[0].strip().lower() != JSON:
            return refusal(415, 'invalid', 'a write must be sent as Content-Type: %s' % JSON,
                           'content-type')
        origin = request.header('Origin')
        try:
            parts = urllib.parse.urlsplit(origin) if origin else None
        except ValueError:
            parts = None
        if parts is None or not host or parts.scheme != listener.origin_scheme \
                or parts.netloc.lower() != host.lower():
            if parts is not None and host and parts.netloc.lower() != host.lower():
                listener._origin_refused(parts.netloc, host)
            return refusal(403, 'forbidden', 'a write must carry an Origin header that names '
                           'this server, the host of its Host header', 'origin')
        if self.headers.get_all('Transfer-Encoding'):
            return refusal(411, 'invalid', 'a write must be sent with a Content-Length',
                           'content-length')
        length = request.header('Content-Length')
        if length is None:
            return refusal(411, 'invalid', 'a write must be sent with a Content-Length',
                           'content-length')
        if not _DIGITS.match(length.strip()):
            return refusal(400, 'invalid', 'Content-Length must be a whole number',
                           'content-length')
        size = int(length)
        if size > listener.max_body:
            return refusal(413, 'invalid', 'the body is over the %d bytes one write may carry'
                           % listener.max_body, 'body')
        listener._mark('reads', 1)
        try:
            raw = self.rfile.read(size) if size else b''
        except OSError:             # the body did not arrive within SOCKET_TIMEOUT_S
            raw = None
        finally:
            listener._mark('reads', -1)
        whole = raw is not None and len(raw) == size
        if whole:
            self._unread = False
        if listener.closing:
            # the door closed while the body was arriving (the read then ends at once): nothing
            # is written, whatever part of it came. A write that passes here was counted by
            # _enter() before the door closed, so drain() waits for it.
            return refusal(503, 'store', CLOSING, 'store')
        if not whole:
            return refusal(400, 'invalid', 'the body is shorter than its Content-Length', 'body')
        if not raw.strip():
            return None
        try:
            request.body = json.loads(raw.decode('utf-8'), parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return refusal(400, 'invalid', 'the body must be JSON in UTF-8', 'body')
        return None

    def _api(self, listener, request):
        api = listener.api
        found = _api.match(request.method, request.path)
        waits = found is not None and found[0].handler is _api.wait and request.role is not None
        if waits and not listener._wait_enter():
            return refusal(429, BUSY, 'this server already holds its %d waits: ask again in a '
                           'moment' % listener.max_waits, 'wait',
                           (('Retry-After', str(RETRY_AFTER_S)),))
        # agent_words is passed only by a listener made with it, and strong only for a request
        # its gate marked, so the local listener's call is the one it always was
        words = {'agent_words': True} if listener.agent_words else {}
        words.update(strong_words(request))
        try:
            # raise_unexpected: an error the Api did not expect comes here as an exception, so
            # _handle logs its cause and answers the fixed 500, to the owner as to anyone
            status, obj = api.call(request.method, request.path, request.role, request.body,
                                   request.query, request.via, raise_unexpected=True, **words)
        finally:
            if waits:
                listener._wait_leave()
        return json_response(status, obj)


class Listener(object):
    """One loopback HTTP listener (module docstring). `addr` is (LOOPBACK, port); port 0 takes a
    free one. `api` is a kitlib.hubsite.api.Api (anything with its `call`)."""

    def __init__(self, addr, api, gate, static_dir=None, host_rule=None, routes=None, via='',
                 log=None, max_waits=MAX_WAITS, max_body=MAX_BODY,
                 max_connections=MAX_CONNECTIONS, origin_scheme='http', via_header=True,
                 agent_words=False, origin_refused=None):
        if origin_scheme not in ORIGIN_SCHEMES:
            raise ValueError('origin_scheme is one of %s, not %r'
                             % (', '.join(ORIGIN_SCHEMES), origin_scheme))
        try:
            host, port = addr
        except (TypeError, ValueError):
            raise BindRefused('the listener address must be (%r, port), not %r' % (LOOPBACK, addr))
        if host != LOOPBACK:
            raise BindRefused('the hub listens on %s only, never on %r' % (LOOPBACK, host))
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise BindRefused('the listener port must be a number from 0 to 65535, not %r'
                              % (port,))
        self.api = api
        self.gate = gate
        self.static_dir = static_dir
        self.host_rule = host_rule
        self.routes = dict(routes or {})
        self.via = via if isinstance(via, str) else ''
        self.via_header = via_header is True        # anything else: the header is never read
        # anything but False: every error answer of the Api in the agent's words
        self.agent_words = agent_words is not False
        self.max_waits = max_waits
        self.max_body = max_body
        self.max_connections = max_connections
        self.origin_scheme = origin_scheme
        self._on_origin = origin_refused if callable(origin_refused) else None
        self._log = log
        self._cond = threading.Condition(threading.Lock())    # the counters and _threads
        self._active = 0            # requests being answered
        self._waits = 0             # of those, the ones counted as a wait
        # connections with no request read yet; writes whose body is being read
        self._marks = {'idle': 0, 'reads': 0}
        self._threads = []
        self._life = threading.Lock()                         # start() and close_door()
        self._thread = None
        self._closed = False
        try:
            self._server = _Server((host, port), _Handler, self)
        except OSError as e:
            raise BindRefused('cannot listen on %s:%d: %s' % (host, port, e.strerror or e))
        bound = self._server.server_address
        if bound[0] != LOOPBACK:    # never expected: bind() was given LOOPBACK
            self._server.server_close()
            raise BindRefused('the listener was bound to %r, not %s' % (bound[0], LOOPBACK))
        self.port = bound[1]
        self.url = 'http://%s:%d' % (LOOPBACK, self.port)

    # ---- the counters -------------------------------------------------------------------------

    @property
    def waiting(self):
        with self._cond:
            return self._waits

    @property
    def closing(self):
        """True once close_door() was called: no connection is accepted and no write begins."""
        return self._closed

    def _enter(self):
        with self._cond:
            self._active += 1

    def _leave(self):
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def _wait_enter(self):
        """Count one more wait; False, and nothing counted, when max_waits are counted already."""
        with self._cond:
            if self._waits >= self.max_waits:
                return False
            self._waits += 1
            self._cond.notify_all()
            return True

    def _wait_leave(self):
        with self._cond:
            self._waits -= 1
            self._cond.notify_all()

    def await_waits(self, n, timeout=None):
        """True once at least `n` waits are counted, False when `timeout` seconds pass first."""
        with self._cond:
            return self._cond.wait_for(lambda: self._waits >= n, timeout)

    def _mark(self, what, by):
        with self._cond:
            self._marks[what] += by
            self._cond.notify_all()

    def await_idle(self, n, timeout=None):
        """True once at least `n` connections are open whose request has not been read (none
        sent, or its line and headers still arriving), False when `timeout` seconds pass first."""
        with self._cond:
            return self._cond.wait_for(lambda: self._marks['idle'] >= n, timeout)

    def await_reads(self, n, timeout=None):
        """True once at least `n` writes are having their body read, False when `timeout`
        seconds pass first."""
        with self._cond:
            return self._cond.wait_for(lambda: self._marks['reads'] >= n, timeout)

    def drain(self, timeout=None):
        """True once no request is being answered except the counted waits (which end when the
        store closes or their time is up), False when `timeout` seconds pass first."""
        with self._cond:
            return self._cond.wait_for(lambda: self._active - self._waits <= 0, timeout)

    # ---- the threads --------------------------------------------------------------------------

    def _origin_refused(self, origin_host, host):
        """Tell origin_refused, when the listener has one, of a write refused because the host
        of its Origin is not its Host. What it raises is dropped: the refusal stands."""
        if self._on_origin is None:
            return
        try:
            self._on_origin(origin_host, host)
        except Exception:           # noqa: BLE001 - a note that fails must not fail a request
            pass

    def _say(self, level, text):
        if self._log is None:
            return
        try:
            self._log(level, text)
        except Exception:           # noqa: BLE001 - a log that fails must not fail a request
            pass

    def _spawn(self, server, request, client_address):
        """Answer one accepted connection on a thread of its own (the accept loop calls this);
        a connection past max_connections is closed unanswered."""
        thread = None
        with self._cond:
            self._threads = [t for t in self._threads if t.is_alive()]
            if len(self._threads) < self.max_connections:
                thread = threading.Thread(target=server.work, args=(request, client_address),
                                          name='hub-http', daemon=True)
                thread.start()      # under the lock: join() never meets a thread not yet started
                self._threads.append(thread)
        if thread is None:
            server.shutdown_request(request)

    def start(self):
        """Serve on a thread named hub-http-<port>. False, and nothing started, when it is
        serving already or its door was closed."""
        with self._life:
            if self._thread is not None or self._closed:
                return False
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={'poll_interval': 0.1},
                                            name='hub-http-%d' % self.port, daemon=True)
            self._thread.start()
            return True

    def close_door(self):
        """Stop accepting and free the port; requests being answered go on, a connection whose
        request has not arrived ends, and a write still arriving is refused (module docstring,
        "Closing"). Safe to call twice, and on a listener that never started."""
        with self._life:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None:      # shutdown() waits for serve_forever, so only when it ran
            self._server.shutdown()
            thread.join()
        self._server.server_close()

    def join(self, timeout=None):
        """Wait up to `timeout` seconds in all (None: until done) for every handler thread; True
        when none is left running."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            threads = list(self._threads)
        for t in threads:
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in threads)

    def stop(self, timeout=None):
        """close_door(), then join(timeout). A wait in flight ends only when the store closes or
        its time is up: close the store first, or give a timeout."""
        self.close_door()
        return self.join(timeout)
