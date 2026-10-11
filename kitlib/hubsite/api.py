"""kitlib.hubsite.api: the hub's verbs as functions over the store, with no transport in them (the
hub site design, section 2 "HTTP API" and section 9).

  ROUTES     the table every transport iterates: Route(method, pattern, role, handler). A pattern
             is a full path under BASE; a `{name}` segment takes one path segment. role is OWNER,
             AGENT or ANY: who may call it.
  Api(store, views=None, clock=None)
             .call(method, path, role, body=None, query=None, via='') -> (status, object)
  match(method, path)    (Route, {name: segment}) or None
  sample_path(route)     the pattern with every `{name}` filled in, for a test that walks ROUTES

Roles. The caller resolves who is asking and passes OWNER or AGENT; this module only enforces.
A role that is neither is refused before the path is looked at, a route whose role is not one of
ROLES is refused for everybody, and the check runs before the handler reads the body. So a route
added without a role answers `forbidden`, never the data.

Fields. A write route names the body fields it takes; any other field is refused (`invalid`, the
field named), so a body cannot carry a field another role owns (an agent posting a question
cannot post its answer). status, the time stamps and a to-do's `from: you` are set here from the
route and the role, never taken from the body. A write to an existing document is pinned to the
version this module read (or the caller's `if_version`), so a check made on one version is never
applied to another: the loser gets `conflict`.

Results. status is 200 with the route's object, or the status of STATUS[code] with
{"ok": false, "error": {"code", "message", "field"}}; both carry `revision`. The codes are the
store's HubError codes plus `forbidden`, `unavailable` (a view this server does not have) and
`internal` (anything unexpected).

Views. The live backlog and the derived views are other modules; `views` is any object with some
of these methods, each one optional (a missing one answers `unavailable`, and the snapshot lists
it under `unavailable`):
  backlog(repo='', state='', fresh=False)   [{repo, tip, fetched_at, items}]
  completed(repo='', days=None)             [{repo, tip, fetched_at, items}]
  notices()  repos()  usage()  agents()     a list each
  health()                                  a dict merged into GET health (git, backlog, ...)

Nothing here opens a socket, starts a thread, prints, parses arguments or exits. GET wait blocks
in Store.wait for at most WAIT_MAX_S; how many may wait at once is the listener's rule.
"""
import collections
import datetime
import math
import re

from kitlib import hubvalidate

from . import schema
from .store import HubError

BASE = '/api/v1'
API_VERSION = 'v1'
OWNER, AGENT, ANY = 'owner', 'agent', 'any'
ROLES = (OWNER, AGENT, ANY)         # what a route may ask for
CALLERS = (OWNER, AGENT)            # what a caller may be
STATUS = {'invalid': 400, 'forbidden': 403, 'not_found': 404, 'conflict': 409, 'store': 503,
          'unavailable': 503, 'internal': 500}
WAIT_MAX_S = 55
VIEWS = ('backlog', 'completed', 'notices', 'repos', 'usage', 'agents')
OWNER_FROM = 'you'                  # a to-do's or a story's `from` when the owner wrote it
AGENT_FROM = 'pm'                   # a to-do's `from` when an agent gives none
AGENT_STORY_STATUSES = ('review', 'questions', 'added', 'declined')
DAYS_MAX = 3650

Route = collections.namedtuple('Route', 'method pattern role handler')
Request = collections.namedtuple('Request', 'role params body query via')

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('', '0', 'false', 'no', 'off')
_ID_SPLIT = re.compile(r'(\d+)')


def error_body(code, message, field=''):
    return {'ok': False, 'error': {'code': code, 'message': message, 'field': field or ''}}


def _invalid(message, field=''):
    return HubError('invalid', message, field)


def _natural(doc_id):
    return [int(p) if p.isdigit() else p.lower() for p in _ID_SPLIT.split(doc_id)]


def _version(doc):
    v = doc.get('version', 0)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _segments(path):
    return path.split('/') if isinstance(path, str) else []


def match(method, path):
    """(Route, {name: segment}) for the route that method and path name, else None. A literal
    segment wins over a `{name}` one."""
    if not isinstance(method, str):
        return None
    want, best = _segments(path), None
    for route in ROUTES:
        have = _segments(route.pattern)
        if route.method != method.upper() or len(have) != len(want):
            continue
        params, literal = {}, 0
        for h, w in zip(have, want):
            if h.startswith('{') and h.endswith('}'):
                if not w:
                    break
                params[h[1:-1]] = w
            elif h != w:
                break
            else:
                literal += 1
        else:
            if best is None or literal > best[0]:
                best = (literal, route, params)
    return (best[1], best[2]) if best else None


def sample_path(route, value='X-1'):
    """route.pattern with every `{name}` segment replaced by value."""
    return re.sub(r'\{[^/}]*\}', value, route.pattern)


# ------------------------------------------------------------------ reading a request
def _fields(req, allowed, required=()):
    """The body's fields: an object holding only `allowed` names, a null one read as absent,
    every `required` one present."""
    body = req.body
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _invalid('the request body is a JSON object', 'body')
    for name in sorted(body, key=str):
        if name not in allowed:
            raise _invalid('this route takes no %r field (it takes: %s)'
                           % (name, ', '.join(allowed) or 'none'), str(name))
    out = {k: v for k, v in body.items() if v is not None}
    for name in required:
        v = out.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise _invalid('%s is required' % name, name)
    return out


def _typed(b, name, types, what):
    v = b.get(name)
    if v is None:
        return None
    if (isinstance(v, bool) and bool not in types) or not isinstance(v, types):
        raise _invalid('%s must be %s, not %s' % (name, what, type(v).__name__), name)
    return v


def _text(b, name):
    return _typed(b, name, (str,), 'a string')


def _flag(b, name, default):
    v = _typed(b, name, (bool,), 'true or false')
    return default if v is None else v


def _pin(b, required=False):
    v = _typed(b, 'if_version', (int,), 'a whole number')
    if v is None and required:
        raise _invalid('if_version is required: the version of the document you read',
                       'if_version')
    if v is not None and v < 0:
        raise _invalid('if_version must be a whole number, not %r' % (v,), 'if_version')
    return v


def _q(req, name):
    v = (req.query or {}).get(name)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return ''
    if isinstance(v, bool):
        return '1' if v else '0'
    return str(v).strip()


def _q_flag(req, name):
    v = _q(req, name).lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise _invalid('%s is 1 or 0, not %r' % (name, v), name)


def _q_int(req, name, low, high):
    v = _q(req, name)
    if not v:
        return None
    if not re.match(r'^\d{1,12}$', v) or not low <= int(v) <= high:
        raise _invalid('%s is a whole number from %d to %d, not %r' % (name, low, high, v), name)
    return int(v)


def _when(text, field):
    try:
        t = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        t = None
    if t is None or t.tzinfo is None:
        if field:
            raise _invalid('%s is an ISO time with its offset, not %r' % (field, text), field)
        return None
    return t


# ------------------------------------------------------------------ the Api
class Api(object):
    """The hub's verbs over `store` (a kitlib.hubsite.store.Store). `views` gives the derived
    views (module docstring) or is None; `clock` gives the time stamp of a write as an ISO
    string (default: the store's clock)."""

    def __init__(self, store, views=None, clock=None):
        self.store = store
        self.views = views
        self.clock = clock or store.clock

    def call(self, method, path, role, body=None, query=None, via=''):
        """(status, object) for one request. role is OWNER or AGENT as the caller resolved it;
        body is the decoded JSON body or None; query is {name: value}; via names the transport
        (web, cli, ...) and is recorded on an answer."""
        try:
            status, obj = 200, self._call(method, path, role, body, query, via)
        except HubError as e:
            status, obj = STATUS.get(e.code, 500), error_body(e.code, e.message, e.field)
        except Exception as e:      # noqa: BLE001 - a transport gets an answer, never a traceback
            status, obj = 500, error_body('internal', '%s: %s' % (type(e).__name__, e))
        if 'revision' not in obj:
            obj['revision'] = self.store.revision
        return status, obj

    def _call(self, method, path, role, body, query, via):
        if not isinstance(role, str) or role not in CALLERS:
            raise HubError('forbidden', 'this request carries no owner or agent credential',
                           'role')
        found = match(method, path)
        if found is None:
            raise HubError('not_found', 'there is no route %s %s' % (method, path), 'path')
        route, params = found
        if route.role not in ROLES:
            raise HubError('forbidden', 'the route %s %s names no role, so nobody may call it'
                           % (route.method, route.pattern), 'role')
        if route.role != ANY and route.role != role:
            raise HubError('forbidden', '%s %s is for the %s, not the %s'
                           % (route.method, route.pattern, route.role, role), 'role')
        if query is not None and not isinstance(query, dict):
            raise _invalid('the query is an object of names and values', 'query')
        out = route.handler(self, Request(role, params, body, query, via if isinstance(via, str)
                                          else ''))
        if not isinstance(out, dict):
            raise HubError('internal', 'the handler of %s gave no object' % route.pattern)
        return out

    # -------------------------------------------------------------- helpers for handlers
    def doc(self, coll, doc_id, b=None):
        """The document, HubError not_found when there is none, conflict when the body pins a
        version it is not at."""
        d = self.store.get(coll, doc_id) if schema.check_id(doc_id) is None else None
        if d is None:
            raise HubError('not_found', 'there is no %s/%s' % (coll, doc_id), 'id')
        pin = (b or {}).get('if_version')
        if pin is not None and pin != _version(d):
            raise HubError('conflict', '%s/%s is at version %d, not %d'
                           % (coll, doc_id, _version(d), pin), 'if_version')
        return d

    def change(self, coll, doc_id, doc, changes, extra=None):
        """Merge changes into the document, pinned to the version of `doc` (the copy the
        handler judged), and give the write's result object."""
        r = self.store.update(coll, doc_id, changes, if_version=_version(doc))
        return self.written(r, extra)

    def written(self, r, extra=None):
        out = {'ok': True, 'id': r.id, 'version': r.version, 'revision': r.revision,
               'warnings': [f._asdict() for f in r.warnings]}
        out.update(extra or {})
        return out

    def items(self, coll, req, since=False):
        """The documents of coll as a list, each with its `id`, filtered by the query's status
        and repo (and since, a time, where the route takes it)."""
        allowed = hubvalidate.STATUSES.get(coll, ())
        status, repo = _q(req, 'status'), _q(req, 'repo').lower()
        if status == 'all':
            status = ''
        if status and status not in allowed:
            raise _invalid('status is one of %s, all; not %r' % (', '.join(allowed), status),
                           'status')
        after = _when(_q(req, 'since'), 'since') if since and _q(req, 'since') else None
        out = []
        for doc_id, d in self.store.docs(coll).items():
            if status and d.get('status') != status:
                continue
            if repo and str(d.get('repo') or '').strip().lower() != repo:
                continue
            if after is not None:
                at = _when(d.get('updated_at'), '') or _when(d.get('asked_at'), '')
                if at is None or at < after:
                    continue
            d['id'] = doc_id
            out.append(d)
        return sorted(out, key=lambda d: _natural(d['id']))

    def view(self, name, **kw):
        fn = getattr(self.views, name, None) if self.views is not None else None
        if not callable(fn):
            raise HubError('unavailable', 'the %s view is not available on this server' % name,
                           'views')
        return fn(**kw)

    def pending(self):
        return [p._asdict() for p in self.store.pending()]


# ------------------------------------------------------------------ handlers
def questions_list(api, req):
    return {'items': api.items('questions', req, since=True)}


def questions_add(api, req):
    b = _fields(req, ('repo', 'from', 'title', 'context', 'options', 'recommended', 'refs',
                      'urgent', 'story'), required=('title',))
    for name in ('repo', 'from', 'title', 'context', 'story'):
        _text(b, name)
    _typed(b, 'options', (list,), 'a list of {label, description}')
    b.setdefault('options', [])
    b.update(status='open', asked_at=api.clock())
    return api.written(api.store.add('questions', b))


def questions_answer(api, req):
    b = _fields(req, ('choice', 'note', 'if_version'), required=('choice',))
    _pin(b)
    note = (_text(b, 'note') or '').strip()
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    if d.get('status') not in ('open', 'answered'):
        raise _invalid('%s is %s: only an open or an answered question takes an answer'
                       % (doc_id, d.get('status')), 'status')
    choice = b['choice']
    options = d.get('options') if isinstance(d.get('options'), list) else []
    if choice == 'other':
        if not note:
            raise _invalid('the choice "other" needs a note saying what', 'note')
        label = 'Other'
    elif isinstance(choice, int) and not isinstance(choice, bool) and 0 <= choice < len(options):
        o = options[choice]
        label = o.get('label') if isinstance(o, dict) and isinstance(o.get('label'), str) else ''
    else:
        raise _invalid('choice is an index into the %d option(s) or "other", not %r'
                       % (len(options), choice), 'choice')
    answer = {'choice': choice, 'label': label, 'note': note, 'at': api.clock(),
              'via': req.via or 'api'}
    return api.change('questions', doc_id, d, {'status': 'answered', 'answer': answer},
                      {'status': 'answered', 'answer': answer})


def questions_log(api, req):
    b = _fields(req, ('ruling', 'if_version'), required=('ruling',))
    _pin(b, required=True)
    ruling = _text(b, 'ruling').strip()
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    if d.get('status') != 'answered':
        raise _invalid('%s is %s: only an answered question is logged'
                       % (doc_id, d.get('status')), 'status')
    return api.change('questions', doc_id, d, {'status': 'logged', 'ruling': ruling},
                      {'status': 'logged'})


def todos_list(api, req):
    return {'items': api.items('todos', req)}


def todos_add(api, req):
    owner = req.role == OWNER
    names = ('title', 'detail', 'commands', 'repo', 'refs', 'urgent')
    b = _fields(req, names if owner else names + ('from',), required=('title',))
    for name in ('title', 'detail', 'repo', 'from'):
        _text(b, name)
    if owner:
        b['from'] = OWNER_FROM
    else:
        b['from'] = (b.get('from') or '').strip() or AGENT_FROM
        if b['from'].lower() == OWNER_FROM:
            raise _invalid('from: %s marks a to-do the owner wrote; an agent cannot use it'
                           % OWNER_FROM, 'from')
    b.update(status='open', added_at=api.clock())
    return api.written(api.store.add('todos', b))


def todos_done(api, req):
    b = _fields(req, ('done', 'if_version'))
    _pin(b)
    done = _flag(b, 'done', True)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    changes = {'status': 'done' if done else 'open', 'done_at': api.clock() if done else None}
    return api.change('todos', doc_id, d, changes, changes)


def todos_ack(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    return api.change('todos', doc_id, d, {'pm_ack': True}, {'pm_ack': True})


def stories_list(api, req):
    return {'items': api.items('stories', req)}


def stories_add(api, req):
    b = _fields(req, ('repo', 'title', 'story', 'urgent'), required=('repo', 'title', 'story'))
    for name in ('repo', 'title', 'story'):
        _text(b, name)
    b.update({'status': 'submitted', 'from': OWNER_FROM, 'submitted_at': api.clock()})
    return api.written(api.store.add('stories', b), {'status': 'submitted'})


def stories_set(api, req):
    b = _fields(req, ('status', 'reviewer', 'review_note', 'backlog_id', 'closed_note',
                      'if_version'), required=('status',))
    _pin(b, required=True)
    for name in ('status', 'reviewer', 'review_note', 'backlog_id', 'closed_note'):
        _text(b, name)
    status = b['status']
    if status not in AGENT_STORY_STATUSES:
        raise _invalid('status is one of %s, not %r (submitted and withdrawn are the owner\'s)'
                       % (', '.join(AGENT_STORY_STATUSES), status), 'status')
    doc_id = req.params['id']
    d = api.doc('stories', doc_id, b)
    if d.get('status') == 'withdrawn':
        raise _invalid('%s was withdrawn by the owner' % doc_id, 'status')
    changes = {k: v for k, v in b.items() if k != 'if_version'}
    if status == 'added':
        changes['added_at'] = api.clock()
    if status == 'declined':
        changes['closed_at'] = api.clock()
    return api.change('stories', doc_id, d, changes, {'status': status})


def stories_withdraw(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('stories', doc_id, b)
    if d.get('status') != 'submitted':
        raise _invalid('%s is %s: only a submitted story can be withdrawn'
                       % (doc_id, d.get('status')), 'status')
    return api.change('stories', doc_id, d, {'status': 'withdrawn', 'closed_at': api.clock()},
                      {'status': 'withdrawn'})


def to_pm_list(api, req):
    return {'items': api.items('to_pm', req)}


def to_pm_add(api, req):
    b = _fields(req, ('kind', 'repo', 'urgent', 'text'), required=('text',))
    for name in ('kind', 'repo', 'text'):
        _text(b, name)
    b.setdefault('kind', 'question')
    b.update(status='queued', created_at=api.clock(), sent_at=None, reply=None, done_at=None)
    return api.written(api.store.add('to_pm', b), {'status': 'queued'})


def to_pm_send(api, req):
    _fields(req, ())
    sent, at = [], api.clock()
    for d in api.items('to_pm', Request(req.role, {}, None, {'status': 'queued'}, req.via)):
        doc_id = d.pop('id')
        api.store.update('to_pm', doc_id, {'status': 'sent', 'sent_at': at},
                         if_version=_version(d))
        sent.append(doc_id)
    return {'ok': True, 'sent': sent, 'sent_at': at if sent else None}


def to_pm_reply(api, req):
    b = _fields(req, ('reply', 'if_version'))
    _pin(b)
    reply = _text(b, 'reply')
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    if d.get('status') != 'sent':
        raise _invalid('%s is %s: only a sent item is replied to' % (doc_id, d.get('status')),
                       'status')
    return api.change('to_pm', doc_id, d,
                      {'status': 'done', 'done_at': api.clock(), 'reply': reply},
                      {'status': 'done'})


def inbox_add(api, req):
    b = _fields(req, ('ref', 'asked', 'text', 'title', 'repo', 'from', 'refs'),
                required=('text',))
    for name in ('ref', 'asked', 'text', 'title', 'repo', 'from'):
        _text(b, name)
    b.update(at=api.clock(), read=False)
    return api.written(api.store.add('inbox', b))


def inbox_read(api, req):
    b = _fields(req, ('read', 'if_version'))
    _pin(b)
    read = _flag(b, 'read', True)
    doc_id = req.params['id']
    d = api.doc('inbox', doc_id, b)
    changes = {'read': read, 'read_at': api.clock() if read else None}
    return api.change('inbox', doc_id, d, changes, changes)


def backlog(api, req):
    return {'repos': api.view('backlog', repo=_q(req, 'repo'), state=_q(req, 'state'),
                              fresh=_q_flag(req, 'fresh'))}


def completed(api, req):
    return {'repos': api.view('completed', repo=_q(req, 'repo'),
                              days=_q_int(req, 'days', 1, DAYS_MAX))}


def _listing(name):
    def handler(api, req):
        return {'items': api.view(name)}
    handler.__name__ = name
    return handler


def pending(api, req):
    return {'items': api.pending()}


def wait(api, req):
    since = _q_int(req, 'since', 0, 10 ** 12)
    if since is None:
        since = api.store.revision
    raw = _q(req, 'timeout')
    try:
        timeout = float(raw) if raw else float(WAIT_MAX_S)
    except ValueError:
        timeout = float('nan')
    if not math.isfinite(timeout) or timeout < 0:
        raise _invalid('timeout is a number of seconds from 0 to %d, not %r' % (WAIT_MAX_S, raw),
                       'timeout')
    want = _q_flag(req, 'pending')
    items = api.pending() if want else []
    if items:
        ch = api.store.changes_since(since)
    else:
        ch = api.store.wait(since, min(timeout, WAIT_MAX_S))
        items = api.pending() if want else []
    return {'revision': ch.revision, 'reset': ch.reset,
            'timed_out': not ch.events and not ch.reset and not items,
            'changes': [{'collection': e.collection, 'id': e.id, 'op': e.op, 'status': e.status}
                        for e in ch.events],
            'pending': items}


def snapshot(api, req):
    revision, docs = api.store.snapshot()
    views, missing = {}, []
    for name in VIEWS:
        try:
            views[name] = api.view(name)
        except Exception:           # noqa: BLE001 - the first paint shows what there is
            views[name] = None
            missing.append(name)
    return {'revision': revision, 'collections': docs,
            'pending': [p._asdict() for p in api.store.pending()],
            'views': views, 'unavailable': missing}


def health(api, req):
    out = {'git': None, 'backlog': None}
    try:
        extra = api.view('health')
    except HubError:
        extra = None
    if isinstance(extra, dict):
        out.update(extra)
    out.update(ok=True, version=API_VERSION, revision=api.store.revision, store=api.store.root)
    return out


def _r(method, path, role, handler):
    return Route(method, BASE + path, role, handler)


ROUTES = (
    _r('GET', '/questions', ANY, questions_list),
    _r('POST', '/questions', AGENT, questions_add),
    _r('POST', '/questions/{id}/answer', OWNER, questions_answer),
    _r('POST', '/questions/{id}/log', AGENT, questions_log),
    _r('GET', '/todos', ANY, todos_list),
    _r('POST', '/todos', ANY, todos_add),
    _r('POST', '/todos/{id}/done', OWNER, todos_done),
    _r('POST', '/todos/{id}/ack', AGENT, todos_ack),
    _r('GET', '/stories', ANY, stories_list),
    _r('POST', '/stories', OWNER, stories_add),
    _r('POST', '/stories/{id}', AGENT, stories_set),
    _r('POST', '/stories/{id}/withdraw', OWNER, stories_withdraw),
    _r('GET', '/to_pm', ANY, to_pm_list),
    _r('POST', '/to_pm', OWNER, to_pm_add),
    _r('POST', '/to_pm/send', OWNER, to_pm_send),
    _r('POST', '/to_pm/{id}/reply', AGENT, to_pm_reply),
    _r('POST', '/inbox', AGENT, inbox_add),
    _r('POST', '/inbox/{id}/read', OWNER, inbox_read),
    _r('GET', '/backlog', ANY, backlog),
    _r('GET', '/completed', ANY, completed),
    _r('GET', '/notices', ANY, _listing('notices')),
    _r('GET', '/repos', ANY, _listing('repos')),
    _r('GET', '/usage', ANY, _listing('usage')),
    _r('GET', '/agents', ANY, _listing('agents')),
    _r('GET', '/pending', ANY, pending),
    _r('GET', '/wait', ANY, wait),
    _r('GET', '/snapshot', ANY, snapshot),
    _r('GET', '/health', ANY, health),
)
