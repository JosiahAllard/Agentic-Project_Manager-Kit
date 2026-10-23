"""kitlib.hubsite.client: the client of the hub server on this machine.

It asks the loopback listener only (127.0.0.1:<port>), through serve.ask: http.client with no
proxy, so a token never leaves this machine. urllib is used for the query string alone. The
token of a call is read from its file by auth.read_token each time, never kept and never put in
a message: the owner's for a route only the owner may call, the agent's for the rest.

Nothing here prints or exits. A call answers the JSON object the server gave, or raises:
  serve.NotRunning    nothing listens on the port                      exit code 3
  Refused             the server answered with a refusal: its `code`,  exit code 1
                      `message`, `field`, HTTP `status` and whole
                      `body` are attributes
  serve.ServeError    anything else: no token file, an answer that is  exit code 1
                      not the hub's
exit_code(e) gives the number for one of these; OK (0), REFUSED (1), USAGE (2), NOT_RUNNING (3)
and TIMED_OUT (4) are the exit codes of tools/hub.py, here as data. TIMED_OUT is never raised:
Client.wait() answers with `timed_out` true.

Client(port, tokens, via=serve.VIA_CLI)
  get(path, role, query), post(path, role, body)   one call; `path` is below api.BASE
  listing(collection, status, repo)     the items of GET /<collection>
  pending(for_key)                      the items of GET /pending (for_key: only that session
                                        key's)
  inbox()                               the inbox documents, from GET /snapshot (the api has no
                                        route that lists them)
  page(id)                              a published page's body, from GET /pages/<id>
  version(collection, id)               the version of one document now, for a write that pins it
  wait(since, timeout, pending, for_key) the long-poll: GET /wait again and again, each for at
                                        most api.WAIT_MAX_S, until the server answers with a
                                        change, a pending item or a reset, or `timeout` seconds
                                        passed (None: no limit); with for_key, until a pending
                                        item of that session key; a poll the server took and did
                                        not answer in its time (serve.NoAnswer) is asked again
"""
import time
import urllib.parse

from . import api as _api
from . import auth, serve

OK, REFUSED, USAGE, NOT_RUNNING, TIMED_OUT = 0, 1, 2, 3, 4
OWNER, AGENT, ANY = _api.OWNER, _api.AGENT, _api.ANY
WAIT_MARGIN_S = 10              # the socket waits this much longer than the server was asked to
REFUSAL_UNKNOWN = 'not the expected answer'


class Refused(serve.ServeError):
    """The server answered a call with a refusal."""

    def __init__(self, status, body):
        err = body.get('error') if isinstance(body, dict) else None
        err = err if isinstance(err, dict) else {}
        self.status = status
        self.body = body
        self.code = str(err.get('code') or status)
        self.message = str(err.get('message') or REFUSAL_UNKNOWN)
        self.field = err.get('field')
        serve.ServeError.__init__(self, '%s: %s' % (self.code, self.message))


def exit_code(error):
    """The exit code of tools/hub.py for an exception a call raised."""
    return NOT_RUNNING if isinstance(error, serve.NotRunning) else REFUSED


def _path(path, query=None):
    pairs = [(k, v) for k, v in sorted((query or {}).items()) if v not in (None, '')]
    return _api.BASE + path + ('?' + urllib.parse.urlencode(pairs) if pairs else '')


def part(doc_id):
    """`doc_id` as one segment of a path."""
    return urllib.parse.quote(str(doc_id), safe='')


class Client(object):
    """`tokens`: the folder of the token files (default auth.folder()). `timeout`: seconds one
    call waits for its answer. `ask`: serve.ask or a stand-in. `via`: the name a write gives
    as its source (serve.VIA_CLI; the hub MCP server gives serve.VIA_MCP); a stand-in `ask` is
    passed it as a keyword only when it is not VIA_CLI."""

    def __init__(self, port, tokens=None, timeout=serve.ASK_TIMEOUT_S, ask=None,
                 clock=time.monotonic, via=serve.VIA_CLI):
        self.port = port
        self.tokens = tokens if tokens is not None else auth.folder()
        self.timeout = timeout
        self._ask = ask or serve.ask
        self._clock = clock
        self.via = via

    def token(self, role):
        """The token a call of a `role` route carries. ServeError when its file is absent."""
        if role == ANY:
            token = (auth.read_token(self.tokens, AGENT)
                     or auth.read_token(self.tokens, OWNER))
            role = 'agent or owner'
        else:
            token = auth.read_token(self.tokens, role)
        if not token:
            raise serve.ServeError('no %s token in %s: run hub.py setup' % (role, self.tokens))
        return token

    def call(self, method, path, role, body=None, query=None, timeout=None):
        extra = {} if self.via == serve.VIA_CLI else {'via': self.via}
        status, obj = self._ask(self.port, method, _path(path, query), self.token(role), body,
                                self.timeout if timeout is None else timeout, **extra)
        if status != 200 or obj.get('ok') is False:
            raise Refused(status, obj)
        return obj

    def get(self, path, role=ANY, query=None, timeout=None):
        return self.call('GET', path, role, None, query, timeout)

    def post(self, path, role, body=None):
        return self.call('POST', path, role, {} if body is None else body)

    def listing(self, collection, status=None, repo=None, role=ANY):
        got = self.get('/' + collection, role, {'status': status, 'repo': repo})
        return got.get('items') or []

    def pending(self, for_key=None):
        """The pending items, each with `from`; with for_key only that session key's."""
        return self.get('/pending', ANY, {'for': for_key or None}).get('items') or []

    def inbox(self):
        docs = (self.get('/snapshot').get('collections') or {}).get('inbox') or {}
        return [dict(docs[k], id=k) for k in sorted(docs, key=_api._natural)]

    def page(self, doc_id):
        """{id, html, version} of a page's body (GET /pages/<id>); Refused (not_found) when
        it has none."""
        return self.get('/pages/' + part(doc_id))

    def version(self, collection, doc_id):
        """Refused (not_found) when the collection holds no such document."""
        for item in self.listing(collection, status='all'):
            if item.get('id') == doc_id:
                return item.get('version')
        raise Refused(404, {'ok': False, 'error': {
            'code': 'not_found', 'message': 'no %s in %s' % (doc_id, collection), 'field': 'id'}})

    def wait(self, since=None, timeout=None, pending=False, for_key=None, except_ids=None):
        """The last answer of GET /wait: {revision, reset, timed_out, changes, pending}.
        `timed_out` is true only when `timeout` seconds passed with nothing to say. for_key
        (with pending) waits for that session key's pending items only; except_ids (with
        pending) leaves out the items with those ids, ones the caller has already acted on."""
        end = None if timeout is None else self._clock() + max(0.0, float(timeout))
        while True:
            left = _api.WAIT_MAX_S if end is None else end - self._clock()
            turn = max(0.0, min(float(left), float(_api.WAIT_MAX_S)))
            try:
                got = self.get('/wait', ANY, {'since': since, 'timeout': '%g' % turn,
                                              'pending': 1 if pending else None,
                                              'for': for_key or None,
                                              'except': ','.join(except_ids or ()) or None},
                               timeout=turn + WAIT_MARGIN_S)
            except serve.NoAnswer:
                # the server took the poll and held it past the socket's time: ask again, from
                # the same revision, until the wait's own time is spent (a turn begun past it)
                if end is not None and left <= 0:
                    raise
                continue
            if not got.get('timed_out'):
                return got
            if end is not None and self._clock() >= end:
                return got
            since = got.get('revision', since)
