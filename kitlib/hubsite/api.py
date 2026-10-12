"""kitlib.hubsite.api: the hub's verbs as functions over the store, with no transport in them (the
hub site design, section 2 "HTTP API" and section 9).

  ROUTES     the table every transport iterates: Route(method, pattern, role, handler). A pattern
             is a full path under BASE; a `{name}` segment takes one path segment. role is OWNER,
             AGENT or ANY: who may call it.
  Api(store, views=None, clock=None)
             .call(method, path, role, body=None, query=None, via='', raise_unexpected=False)
                 -> (status, object)
             via names the transport (web, desktop-mcp, cli, ...); every write records it
             as the document's `via` (`api` when none is given), an answer in `answer.via`
             too. A delete leaves no document to carry it. raise_unexpected: an exception
             that is no HubError is raised to the caller, not answered ("What an agent is
             told").
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
applied to another: the loser gets `conflict`. The `from` an agent gives a to-do is a name in
printable ASCII (0x21 to 0x7E, single spaces inside it, the ends trimmed); anything else is
`invalid`, and so is any spelling of `you`. On the two edit routes a null `repo` (and a to-do's
null `detail`) clears the field exactly as "" does; any other null field is read as absent.

Results. status is 200 with the route's object, or the status of STATUS[code] with
{"ok": false, "error": {"code", "message", "field"}}; both carry `revision`. The codes are the
store's HubError codes plus `forbidden`, `unavailable` (a view this server does not have, or one
that failed) and `internal` (anything unexpected). A write answers the revision after it (send:
after its last note), so a client that waits `since` it is not woken by its own write.

What an agent is told. A `store` refusal names files and the store's folder, and an unexpected
error may: the owner gets that text, any other caller gets the fixed phrase of AGENT_SAYS for a
`store` refusal and only the exception's class name for `internal`. The store's `invalid`,
`not_found` and `conflict` messages name a collection, an id and a field and go to both.
A transport that has a log of its own calls with raise_unexpected=True: the unexpected error is
then raised to it and no answer is made here, so the transport can log the cause and send a
fixed message to every caller, the owner too (kitlib.hubsite.httpd does).

Views. The live backlog and the derived views are other modules; `views` is any object with some
of these methods, each one optional. A missing one answers `unavailable` on its own route, and
so does one that raises, with a short reason (a HubError's code, else the exception's class
name; never a message or a path). GET snapshot and GET health never fail for a view: the part
is null and `unavailable` maps its name to that same reason:
  backlog(repo='', state='', fresh=False)   [{repo, fetched_at, items}]
  completed(repo='', days=None)             [{repo, fetched_at, items}]
  notices()  repos()  usage()  agents()     a list each
  health()                                  a dict merged into GET health (git, backlog, ...)

Nothing here opens a socket, starts a thread, prints, parses arguments or exits. GET wait blocks
in Store.wait for at most WAIT_MAX_S; how many may wait at once is the listener's rule.

PM seen. GET snapshot carries `pm_seen`: {"waited", "pulled", "waiting"}. waited is the time
(the Api's clock) an AGENT-role caller last entered or left GET wait, pulled the time one last
read the pending work (GET pending, or GET wait with pending=1), waiting the number of agent
waits blocked in Store.wait right now. All three live in this Api object, not in the store: a
new Api (a server restart) starts at null, null and 0 until the next agent call. The owner's
own calls never count. No lock is taken: each note is one assignment, and an open wait is one
key put into and popped from a dict, which the interpreter does as one step each.

Revision. A read's `revision` is taken before the data is copied, so it is never later than the
data: a client that waits `since` it is told of any write the copy missed. GET health gives the
store's folder to the owner only.
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
# what a caller who is not the owner is told in place of the store's own message, by code
AGENT_SAYS = {'store': 'the hub store is not available: the owner sees why'}

Route = collections.namedtuple('Route', 'method pattern role handler')
Request = collections.namedtuple('Request', 'role params body query via')

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('', '0', 'false', 'no', 'off')
_ID_SPLIT = re.compile(r'([0-9]+)')
_NAME = re.compile(r'[\x21-\x7e]+(?: [\x21-\x7e]+)*')


def error_body(code, message, field=''):
    return {'ok': False, 'error': {'code': code, 'message': message, 'field': field or ''}}


def _invalid(message, field=''):
    return HubError('invalid', message, field)


def _natural(doc_id):
    # split on a group: text at the even places, ASCII digits at the odd ones
    return [int(p) if i % 2 else p.lower() for i, p in enumerate(_ID_SPLIT.split(doc_id))]


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
def _fields(req, allowed, required=(), flags=(), blank=()):
    """The body's fields: an object holding only `allowed` names, a null one read as absent
    (refused for a name in `flags`: null is neither true nor false; read as "" for a name in
    `blank`), every `required` one present."""
    body = req.body
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _invalid('the request body is a JSON object', 'body')
    for name in sorted(body, key=str):
        if name not in allowed:
            raise _invalid('this route takes no %r field (it takes: %s)'
                           % (name, ', '.join(allowed) or 'none'), str(name))
    for name in flags:
        if name in body and not isinstance(body[name], bool):
            raise _invalid('%s must be true or false, not %s'
                           % (name, 'null' if body[name] is None else type(body[name]).__name__),
                           name)
    out = {k: ('' if v is None else v) for k, v in body.items() if v is not None or k in blank}
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


def _via(req):
    return req.via or 'api'


def _reason(e):
    """Why a view gave nothing, short and with no path in it: a HubError's code, else the
    exception's class name."""
    return e.code if isinstance(e, HubError) else type(e).__name__


def _agent_from(text):
    """The `from` of a to-do an agent adds: text trimmed, AGENT_FROM when that leaves nothing.
    Only printable ASCII with single spaces inside is a name (so no character a reader cannot
    see or tell from another), and no spelling of OWNER_FROM is an agent's."""
    name = text.strip()
    if not name:
        return AGENT_FROM
    if not _NAME.fullmatch(name):
        raise _invalid('from is a name in printable ASCII with single spaces inside, not %s'
                       % ascii(name), 'from')
    if name.replace(' ', '').casefold() == OWNER_FROM:
        raise _invalid('from: %s marks a to-do the owner wrote; an agent cannot use it'
                       % OWNER_FROM, 'from')
    return name


# ------------------------------------------------------------------ the Api
class Api(object):
    """The hub's verbs over `store` (a kitlib.hubsite.store.Store). `views` gives the derived
    views (module docstring) or is None; `clock` gives the time stamp of a write as an ISO
    string (default: the store's clock)."""

    def __init__(self, store, views=None, clock=None):
        self.store = store
        self.views = views
        self.clock = clock or store.clock
        self._seen = {'waited': None, 'pulled': None}
        self._waits = {}            # one key per agent wait that is blocked right now

    def call(self, method, path, role, body=None, query=None, via='', raise_unexpected=False):
        """(status, object) for one request. role is OWNER or AGENT as the caller resolved it;
        body is the decoded JSON body or None; query is {name: value}; via names the transport
        (web, cli, ...) and is recorded on every write. With raise_unexpected an exception that
        is no HubError is raised, not answered: the caller logs it and words its own 500."""
        # read before the handler copies any data: a write in between then has a later
        # revision, so a client waiting since this one is told of it
        revision = self.store.revision
        try:
            status, obj = 200, self._call(method, path, role, body, query, via)
        except HubError as e:
            said = e.message if role == OWNER else AGENT_SAYS.get(e.code, e.message)
            status, obj = STATUS.get(e.code, 500), error_body(e.code, said, e.field)
        except Exception as e:      # noqa: BLE001 - a transport gets an answer, never a traceback
            if raise_unexpected:
                raise
            said = '%s: %s' % (type(e).__name__, e) if role == OWNER else type(e).__name__
            status, obj = 500, error_body('internal', said)
        obj.setdefault('revision', revision)
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

    def add(self, coll, fields, req, extra=None):
        """A new document of fields, with the path the write came by (`via`)."""
        return self.written(self.store.add(coll, dict(fields, via=_via(req))), extra)

    def change(self, coll, doc_id, doc, changes, req, extra=None):
        """Merge changes into the document, pinned to the version of `doc` (the copy the
        handler judged), record the path the write came by (`via`), and give the write's
        result object."""
        r = self.store.update(coll, doc_id, dict(changes, via=_via(req)),
                              if_version=_version(doc))
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

    def _view(self, name):
        fn = getattr(self.views, name, None) if self.views is not None else None
        if not callable(fn):
            raise HubError('unavailable', 'the %s view is not available on this server' % name,
                           'views')
        return fn

    def view(self, name, **kw):
        """What the view gives; whatever it raises is the caller's to answer for."""
        return self._view(name)(**kw)

    def shown(self, name, **kw):
        """view() for a route that is that one view: a view that raises is `unavailable` with
        the short reason, never its message."""
        fn = self._view(name)
        try:
            return fn(**kw)
        except Exception as e:      # noqa: BLE001 - a view is another module's code
            raise HubError('unavailable', 'the %s view failed: %s' % (name, _reason(e)), 'views')

    def pending(self):
        return [p._asdict() for p in self.store.pending()]

    def seen(self, req, what, opened=None, closed=None):
        """Note that an agent (a PM session) just waited or pulled (`what`). `opened` is a key
        for a wait that now blocks, `closed` the key of one that ended. The owner's calls are
        not noted. An ended wait stops counting before the clock is read, and a clock that
        fails then leaves the earlier time: it never hides how the wait itself ended."""
        if req.role != AGENT:
            return
        if closed is not None:
            self._waits.pop(closed, None)
            try:
                self._seen[what] = self.clock()
            except Exception:       # noqa: BLE001 - the wait's own answer or error stands
                pass
            return
        self._seen[what] = self.clock()
        if opened is not None:
            self._waits[opened] = True

    def pm_seen(self):
        return {'waited': self._seen['waited'], 'pulled': self._seen['pulled'],
                'waiting': len(self._waits)}


# ------------------------------------------------------------------ handlers
def questions_list(api, req):
    return {'items': api.items('questions', req, since=True)}


def questions_add(api, req):
    b = _fields(req, ('repo', 'from', 'title', 'context', 'options', 'recommended', 'refs',
                      'urgent', 'story'), required=('title',))
    for name in ('repo', 'from', 'title', 'context', 'story'):
        _text(b, name)
    if 'from' in b:                 # the route is an agent's: the same rule as a to-do's from
        b['from'] = _agent_from(b['from'])
    _typed(b, 'options', (list,), 'a list of {label, description}')
    b.setdefault('options', [])
    b.update(status='open', asked_at=api.clock())
    return api.add('questions', b, req)


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
              'via': _via(req)}
    return api.change('questions', doc_id, d, {'status': 'answered', 'answer': answer}, req,
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
    return api.change('questions', doc_id, d, {'status': 'logged', 'ruling': ruling}, req,
                      {'status': 'logged'})


def todos_list(api, req):
    return {'items': api.items('todos', req)}


def todos_add(api, req):
    owner = req.role == OWNER
    names = ('title', 'detail', 'commands', 'repo', 'refs', 'urgent')
    b = _fields(req, names if owner else names + ('from',), required=('title',),
                flags=('urgent',))
    for name in ('title', 'detail', 'repo', 'from'):
        _text(b, name)
    if owner:
        b['from'] = OWNER_FROM
    else:
        b['from'] = _agent_from(b.get('from') or '')
    b.update(status='open', added_at=api.clock())
    return api.add('todos', b, req)


def todos_done(api, req):
    b = _fields(req, ('done', 'if_version'), flags=('done',))
    _pin(b)
    done = _flag(b, 'done', True)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    changes = {'status': 'done' if done else 'open', 'done_at': api.clock() if done else None}
    return api.change('todos', doc_id, d, changes, req, changes)


def todos_ack(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    return api.change('todos', doc_id, d, {'pm_ack': True}, req, {'pm_ack': True})


def _own(doc_id, d, what):
    who = d.get('from')
    if who != OWNER_FROM:           # exactly what todos_add writes for the owner
        raise HubError('forbidden', '%s is from %s: the owner can %s only a to-do the owner '
                       'added (from: %s)' % (doc_id, who or 'nobody', what, OWNER_FROM), 'from')


def todos_edit(api, req):
    b = _fields(req, ('title', 'detail', 'commands', 'repo', 'refs', 'urgent', 'if_version'),
                flags=('urgent',), blank=('repo', 'detail'))
    _pin(b)
    for name in ('title', 'detail', 'repo'):
        _text(b, name)
    for name in ('commands', 'refs'):
        _typed(b, name, (list,), 'a list')
    changes = {k: v for k, v in b.items() if k != 'if_version'}
    if not changes:
        raise _invalid('give at least one of title, detail, commands, repo, refs, urgent to '
                       'change', 'body')
    if 'title' in changes and not changes['title'].strip():
        raise _invalid('title cannot be made empty', 'title')
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    _own(doc_id, d, 'edit')
    if d.get('pm_ack'):             # the PM read the old text: the changed one is pending again
        changes['pm_ack'] = False
    return api.change('todos', doc_id, d, changes, req, {'status': d.get('status')})


def todos_delete(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    _own(doc_id, d, 'delete')
    r = api.store.delete('todos', doc_id, if_version=_version(d))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def stories_list(api, req):
    return {'items': api.items('stories', req)}


def stories_add(api, req):
    b = _fields(req, ('repo', 'title', 'story', 'urgent'), required=('repo', 'title', 'story'))
    for name in ('repo', 'title', 'story'):
        _text(b, name)
    b.update({'status': 'submitted', 'from': OWNER_FROM, 'submitted_at': api.clock()})
    return api.add('stories', b, req, {'status': 'submitted'})


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
    return api.change('stories', doc_id, d, changes, req, {'status': status})


def stories_withdraw(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('stories', doc_id, b)
    if d.get('status') != 'submitted':
        raise _invalid('%s is %s: only a submitted story can be withdrawn'
                       % (doc_id, d.get('status')), 'status')
    return api.change('stories', doc_id, d, {'status': 'withdrawn', 'closed_at': api.clock()},
                      req, {'status': 'withdrawn'})


def to_pm_list(api, req):
    return {'items': api.items('to_pm', req)}


def to_pm_add(api, req):
    b = _fields(req, ('kind', 'repo', 'urgent', 'text'), required=('text',))
    for name in ('kind', 'repo', 'text'):
        _text(b, name)
    b.setdefault('kind', 'question')
    b.update(status='queued', created_at=api.clock(), sent_at=None, reply=None, done_at=None)
    return api.add('to_pm', b, req, {'status': 'queued'})


def _queued(doc_id, d, what):
    if d.get('status') != 'queued':
        raise HubError('conflict', '%s is %s: only a queued note, one not yet sent to the PM, '
                       'can be %s' % (doc_id, d.get('status'), what), 'status')


def to_pm_edit(api, req):
    b = _fields(req, ('kind', 'repo', 'urgent', 'text', 'if_version'), flags=('urgent',),
                blank=('repo',))
    _pin(b)
    for name in ('kind', 'repo', 'text'):
        _text(b, name)
    changes = {k: v for k, v in b.items() if k != 'if_version'}
    if not changes:
        raise _invalid('give at least one of kind, repo, urgent, text to change', 'body')
    if 'text' in changes and not changes['text'].strip():
        raise _invalid('text cannot be made empty', 'text')
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    _queued(doc_id, d, 'edited')
    return api.change('to_pm', doc_id, d, changes, req, {'status': 'queued'})


def to_pm_delete(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    _queued(doc_id, d, 'deleted')
    r = api.store.delete('to_pm', doc_id, if_version=_version(d))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def _refusal(docs, coll, doc_id, changes):
    """Why the store would refuse to send a queued note: to merge changes (the status `sent`,
    its time, via) into coll/doc_id of docs (a snapshot's collections), as a HubError, or None.
    It is the schema half of the store's rule (Store._validate), asked before anything is
    written. The other half, a new hubvalidate error or REFUSED finding, cannot arise here: of
    a to_pm note hubvalidate judges the status (`sent` is one), its refs and its repo (warnings
    both), and the changes touch none of the last two."""
    new = dict(docs[coll][doc_id], **changes)
    errors = schema.check(coll, new, doc_id)
    if errors:
        return HubError('invalid', '%s/%s: %s' % (coll, doc_id,
                                                  '; '.join(f.message for f in errors)),
                        errors[0].field)
    return None


def to_pm_send(api, req):
    _fields(req, ())
    at = api.clock()
    changes = {'status': 'sent', 'sent_at': at, 'via': _via(req)}
    _, docs = api.store.snapshot()
    queued = sorted((i for i, d in docs['to_pm'].items() if d.get('status') == 'queued'),
                    key=_natural)
    for doc_id in queued:           # every one is checked before any is sent: a bad one sends none
        why = _refusal(docs, 'to_pm', doc_id, changes)
        if why:
            raise why
    out = {'ok': True, 'sent': queued, 'sent_at': at if queued else None}
    for doc_id in queued:
        r = api.store.update('to_pm', doc_id, changes,
                             if_version=_version(docs['to_pm'][doc_id]))
        out['revision'] = r.revision
    return out


def to_pm_reply(api, req):
    b = _fields(req, ('reply', 'ruling', 'if_version'))
    _pin(b)
    changes = {'status': 'done', 'done_at': api.clock(), 'reply': _text(b, 'reply')}
    if _text(b, 'ruling') is not None:
        changes['ruling'] = b['ruling'].strip()
        if not changes['ruling']:
            raise _invalid('ruling, when given, names the decision: it cannot be blank', 'ruling')
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    if d.get('status') != 'sent':
        raise _invalid('%s is %s: only a sent item is replied to' % (doc_id, d.get('status')),
                       'status')
    return api.change('to_pm', doc_id, d, changes, req, {'status': 'done'})


def inbox_add(api, req):
    b = _fields(req, ('ref', 'asked', 'text', 'title', 'repo', 'from', 'refs'),
                required=('text',))
    for name in ('ref', 'asked', 'text', 'title', 'repo', 'from'):
        _text(b, name)
    if 'from' in b:                 # the route is an agent's: the same rule as a to-do's from
        b['from'] = _agent_from(b['from'])
    b.update(at=api.clock(), read=False)
    return api.add('inbox', b, req)


def inbox_read(api, req):
    b = _fields(req, ('read', 'if_version'), flags=('read',))
    _pin(b)
    read = _flag(b, 'read', True)
    doc_id = req.params['id']
    d = api.doc('inbox', doc_id, b)
    changes = {'read': read, 'read_at': api.clock() if read else None}
    return api.change('inbox', doc_id, d, changes, req, changes)


def backlog(api, req):
    return {'repos': api.shown('backlog', repo=_q(req, 'repo'), state=_q(req, 'state'),
                               fresh=_q_flag(req, 'fresh'))}


def completed(api, req):
    return {'repos': api.shown('completed', repo=_q(req, 'repo'),
                               days=_q_int(req, 'days', 1, DAYS_MAX))}


def _listing(name):
    def handler(api, req):
        return {'items': api.shown(name)}
    handler.__name__ = name
    return handler


def pending(api, req):
    items = api.pending()
    api.seen(req, 'pulled')
    return {'items': items}


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
    # pending is read whether asked for or not: it is the read that refuses a closed store,
    # which Store.wait answers at once and with nothing (a client would loop on it)
    found = api.pending()
    items = found if want else []
    if want:
        api.seen(req, 'pulled')
    if items:
        api.seen(req, 'waited')
        ch = api.store.changes_since(since)
    else:
        key = object()
        api.seen(req, 'waited', opened=key)
        try:
            ch = api.store.wait(since, min(timeout, WAIT_MAX_S))
        finally:
            api.seen(req, 'waited', closed=key)
        found = api.pending()
        items = found if want else []
    return {'revision': ch.revision, 'reset': ch.reset,
            'timed_out': not ch.events and not ch.reset and not items,
            'changes': [{'collection': e.collection, 'id': e.id, 'op': e.op, 'status': e.status}
                        for e in ch.events],
            'pending': items}


def snapshot(api, req):
    revision, docs = api.store.snapshot()
    views, missing = {}, {}
    for name in VIEWS:
        try:
            views[name] = api.view(name)
        except Exception as e:      # noqa: BLE001 - the first paint shows what there is
            views[name] = None
            missing[name] = _reason(e)
    return {'revision': revision, 'collections': docs,
            'pending': [p._asdict() for p in api.store.pending()],
            'views': views, 'unavailable': missing, 'pm_seen': api.pm_seen()}


def health(api, req):
    out, missing = {'git': None, 'backlog': None}, {}
    try:
        extra = api.view('health')
    except Exception as e:          # noqa: BLE001 - health answers whatever a view does
        extra, missing = None, {'health': _reason(e)}
    if isinstance(extra, dict):
        out.update(extra)
    out.pop('store', None)
    out.update(ok=True, version=API_VERSION, revision=api.store.revision, unavailable=missing)
    if req.role == OWNER:           # where the store is on this machine is the owner's to know
        out['store'] = api.store.root
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
    _r('POST', '/todos/{id}/edit', OWNER, todos_edit),
    _r('POST', '/todos/{id}/delete', OWNER, todos_delete),
    _r('GET', '/stories', ANY, stories_list),
    _r('POST', '/stories', OWNER, stories_add),
    _r('POST', '/stories/{id}', AGENT, stories_set),
    _r('POST', '/stories/{id}/withdraw', OWNER, stories_withdraw),
    _r('GET', '/to_pm', ANY, to_pm_list),
    _r('POST', '/to_pm', OWNER, to_pm_add),
    _r('POST', '/to_pm/send', OWNER, to_pm_send),
    _r('POST', '/to_pm/{id}/reply', AGENT, to_pm_reply),
    _r('POST', '/to_pm/{id}/edit', OWNER, to_pm_edit),
    _r('POST', '/to_pm/{id}/delete', OWNER, to_pm_delete),
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
