"""kitlib.hubsite.api: the hub's verbs as functions over the store, with no transport in them (the
hub site design, section 2 "HTTP API" and section 9).

  ROUTES     the table every transport iterates: Route(method, pattern, role, handler). A pattern
             is a full path under BASE; a `{name}` segment takes one path segment. role is OWNER,
             AGENT, ANY, APPROVER or LOCK_WRITE: who may call it ("Roles", "Machine lock").
  Api(store, views=None, clock=None, session_keys=None, now=None, approval_key=None,
      checkout=None, lock=None)
             .call(method, path, role, body=None, query=None, via='', raise_unexpected=False,
                   agent_words=False, strong=False) -> (status, object)
             via names the transport (web, desktop-mcp, cli, tunnel, ...); every write records
             it as the document's `via` (`api` when none is given), an answer in `answer.via`
             too. A delete leaves no document to carry it. raise_unexpected: an exception
             that is no HubError is raised to the caller, not answered ("What an agent is
             told"). agent_words: an error answer is worded as for an agent whatever the role
             is, which stays the role for what the request may do.
  match(method, path)    (Route, {name: segment}) or None
  sample_path(route)     the pattern with every `{name}` filled in, for a test that walks ROUTES

Roles. The caller resolves who is asking and passes OWNER or AGENT; this module only enforces.
A role that is neither is refused before the path is looked at, a route whose role is not one of
ROLES is refused for everybody, and the check runs before the handler reads the body. So a route
added without a role answers `forbidden`, never the data.
APPROVER is a route role and never a caller: such a route answers only OWNER with strong=True,
the owner the transport saw log in through Cloudflare Access (kitlib.hubsite.tunnel's gate marks
that request, kitlib.hubsite.httpd passes it on). Every other caller is refused `forbidden`,
field `role`, with NOT_STRONG: the owner token, a browser session paired from it and an agent are
never strong. strong is a call argument, never a field of the body or the query, and it opens no
other route: on OWNER, AGENT and ANY routes the role alone decides. A handler reads it as
Request.strong. POST approvals/{id}/decide is an APPROVER route ("Approvals"), and so are the
lock's reservation and division writes. LOCK_WRITE is a route role too: OWNER, and strong as
well when the call came over the tunnel ("Machine lock").

Approvals (kitlib.hubsite.approvals holds the rules; these routes only carry them). POST approvals
(AGENT) adds a pending request: the base of each file is read with git from this machine's
checkout of the request's repo (Api's checkout; no checkout is `invalid`, field repo, and a base
that is not the file at head is `invalid`, field files), and the unified diff lines the page
shows are stored in the document as `diff`. POST approvals/{id}/decide (APPROVER) writes the
owner's decision, signed with Api's approval_key; POST approvals/{id}/consume (AGENT) marks an
approval consumed by a commit. Both take the `if_version` the caller read and are pinned to it,
and an ApprovalError is answered as a HubError of its code and field. GET approvals/{id}/verify
(AGENT, query repo, head, files as JSON {path: blob} and an optional commit) answers
{ok, valid, why} and never the key or the sig; an unknown id is valid false, approvals.WHY_UNKNOWN.
GET snapshot carries `strong`: true only on the strong owner's call (Request.strong), so the page
shows Approve and Deny as usable; the decide route checks the role itself all the same.

Pages. POST pages (AGENT) publishes a page: the card `pages/<id>` and its body `page_html/<id>`.
The body is {id, title, html, repo?, from?, summary?, refs?, if_version?}. The id is the
caller's (a document id that holds no hub item's or ruling's id, `[QMTPSAR]-<digits>` with no
letter or digit either side, anywhere, so a ref that names a page is never read as an item);
html is judged by kitlib.hubsite.pagecheck (at most PAGE_HTML_MAX bytes of UTF-8, no script, no
outside link or load, no unrendered Mermaid), and a refusal is `invalid`, field html, with
nothing written. Publishing over a page takes the card's `if_version`; without it, or with
another, it is `conflict`. store.PAGES_LOCK is held from the reads to the last write of a publish or a
delete (a page is two documents), so a second publisher reads only after the first has written
both, and with the same pin
it is `conflict` with nothing written. The body is put first and the card second, each pinned to
the version read (0: not there yet), so a card never names a missing body; a card write that
fails after its body leaves an orphan body, which kitlib.hubvalidate warns of and the next
publish overwrites. The card carries title, repo, summary, refs, from (an agent's name, as a
to-do's), `at` and `bytes`. GET pages lists the cards; GET pages/{id} gives {ok, id, html,
version} of the body. GET snapshot leaves `page_html` out: the Docs tab reads a body when it is
opened. POST pages/{id}/delete (OWNER, if_version optional) deletes the card, then the body; a
body delete that fails leaves the card deleted and the body an orphan, as above. One process
lock is enough: Store.open lets one process write a store (`_store.lock`).

Fields. A write route names the body fields it takes; any other field is refused (`invalid`, the
field named), so a body cannot carry a field another role owns (an agent posting a question
cannot post its answer). status, the time stamps and a to-do's `from: you` are set here from the
route and the role, never taken from the body. A write to an existing document is pinned to the
version this module read (or the caller's `if_version`), so a check made on one version is never
applied to another: the loser gets `conflict`. The `from` an agent gives a to-do is a name in
printable ASCII (0x21 to 0x7E, single spaces inside it, the ends trimmed); anything else is
`invalid`, and so is any spelling of `you`. A question's `from` is the session its answer goes
back to: a key of the launcher's sessions.json, read on each add (Api's session_keys), or `pm`,
which is also what an absent one is; any other is `invalid`, and the refusal names the known
keys, never the file. A new to-do's `steps` are made into its commands and detail here and are
not kept (todo_steps); an agent's to-do is judged by the owner's to-do rules (kitlib.hubsite.
todosteps.check) for every client. On the edit routes a null `repo` (and a to-do's
null `detail`) clears the field exactly as "" does; any other null field is read as absent.

Creators. An agent edits (POST questions/{id}/edit, todos/{id}/edit) or withdraws (POST
questions/{id}/delete, todos/{id}/delete) only an open item it added: the body gives the item's
own `from` (`pm` matches an item with none) and the `if_version` it read, both required, and an
owner's to-do (`from: you`) is never an agent's. A wrong `from` is `forbidden`, an item no longer
open is `conflict`, field status, and neither changes it. Every agent shares one token, so the
`from` is a claim: the check stops a mistake, not an agent that gives another's name. An agent's
to-do edit is judged by the owner's to-do rules (todo_steps), the changed fields only; it takes
no `steps`. The owner edits only a to-do of their own (any status) and deletes one of their own
at any status, or any open question or open to-do; the owner never edits an agent's item. A
withdrawal deletes the document: there is no withdrawn status.

Results. status is 200 with the route's object, or the status of STATUS[code] with
{"ok": false, "error": {"code", "message", "field"}}; both carry `revision`. The codes are the
store's HubError codes plus `forbidden`, `unavailable` (a view this server does not have, or one
that failed) and `internal` (anything unexpected). A write answers the revision after it (send:
after its last note), so a client that waits `since` it is not woken by its own write.

What an agent is told. A `store` refusal names files and the store's folder, and an unexpected
error may: the owner gets that text, any other caller gets the fixed phrase of AGENT_SAYS for a
`store` refusal and only the exception's class name for `internal`. The store's `invalid`,
`not_found` and `conflict` messages name a collection, an id and a field and go to both.
A transport whose answers leave this machine calls with agent_words=True: the owner then gets the
agent's wording too (the same status, code and field), so no answer of that transport names the
store's folder or a file path. kitlib.hubsite.httpd does for the tunnel listener.
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
  record(repo, id)                          one record (GET records/{repo}/{id})
  record_template(repo)                     a new record's fields (GET records/{repo}/new)
The two record views differ: what they raise as a HubError is the answer (a 404 for a record or
repository that is not there, a 400 for a name that cannot be one), so their messages are fixed
phrases with no path, no id and nothing the caller sent; only anything else is `unavailable`.
Both are read now, never cached, and neither is part of GET snapshot.
  record_add(repo, id, summary, scopes, effort, ref)               the owner's three writes to
  record_edit(repo, id, version, changes, unset, body, ruling)     a repository's backlog store
  record_delete(repo, id, why, ruling)                             (OWNER routes; kitlib.hubsite.
                                                                   recordwrite)
They raise HubErrors as the record views do; a `store` one carries the store's text, which only
the local owner is given. POST records/{repo}/{id}/delete takes `confirm`, which must repeat the id.

Machine lock. By the owner's decision the hub reads and changes the machine lock in-process,
through `lock`: a door object (kitlib.hubsite.lockdoor.LockDoor on the machine's server; this
module never imports it) with
  revision()                     the lock store's revision
  view(remote=False)             {lock_revision, view, writer}: kitlib.lockui.model to_json's view,
                                 and 'owner' or 'agent': who the lock takes this hub for
  stats(days, since, until)      kitlib.lock.api stats
  reserve, hold, edit, end_hold, end_run, move_to_top, game_mode, games, division
                                 the writes (lockdoor's docstring), each one lock api call, each
                                 taking remote; a refusal is a HubError
Every lock route is closed to an agent (`forbidden`, field role), on every listener: an agent
reads the lock through its own MCP server and changes it with the lock's tools. The reads (GET
lock/view, lock/stats, lock/wait) are OWNER's. The writes, by the owner's decision: on this
machine the owner's login is enough, over the tunnel every lock write needs the strong owner. A
write the decision does not name (a reservation: any hold write; the division) is the strong
owner's on every listener, APPROVER; the others (end a run, move one to the top, game mode, the
games list) are LOCK_WRITE: OWNER with strong, or OWNER on a listener that is not the tunnel
(via is not TUNNEL_VIA), refused with NOT_LOCK_WRITER. A future relay that reaches the local
listener for a remote browser must close that local owner path for its callers.
  POST lock/holds            APPROVER  {kind: reserve, scope: whole|half, start, end (ISO with
                                       offset), note} or {kind: hold, scope, start, end, note,
                                       cpu, gpu} (the Hold dialog's text, read in the PC's zone)
  POST lock/holds/{id}/edit  APPROVER  {start, end, note}: the Edit dialog's text; no note keeps it
  POST lock/holds/{id}/end   APPROVER  {}
  POST lock/division         APPROVER  {fields: {key: text}} or {defaults: true}
  POST lock/runs/{id}/end    LOCK_WRITE {confirm}: confirm repeats the run id
  POST lock/runs/{id}/top    LOCK_WRITE {}
  POST lock/game_mode        LOCK_WRITE {enabled}
  POST lock/games            LOCK_WRITE {text}: the Games dialog's text
A write answers {ok, result} plus the view after it (as GET lock/view; view null when the lock
cannot be read then). The page makes owner holds only; there is no `owner` field. A busy lock is
`conflict` (409). The view answers of the lock routes carry `writes`: {lock, strong, why}, what
this caller may change (lock: the LOCK_WRITE routes, strong: the APPROVER ones too) and, when
one is false, why (WRITES_WHY); a hub the lock takes for an agent may change nothing.
GET lock/view gives the view; `remote` is true only for the tunnel listener's calls (via
TUNNEL_VIA), whose warnings and refusals that can name a file are fixed phrases
(model.REMOTE_SAYS, lockdoor.REMOTE_SAYS); every listener gets the same allow-listed keys. GET
lock/stats takes days (1 to 366), since and until (ISO times with their offset). GET lock/wait
takes since (a lock revision; default the one now) and timeout (seconds, at most WAIT_MAX_S) and
answers the view plus `changed` once the lock's revision differs from since, or at the timeout
with changed false. The lock store has no wake of its own, so the wait looks at its revision
every LOCK_POLL_S, sleeping in Store.wait so that a close of the hub store ends it at once
(`store`). It is a wait for the listener (WAIT_HANDLERS): it takes one of the listener's wait
slots. With no door every route is `unavailable`, field lock; a door that raises anything but a
HubError is `unavailable` with only the exception's class name (_reason), never its text.

Nothing here opens a socket, starts a thread, prints, parses arguments or exits. GET wait (and GET
lock/wait) blocks in Store.wait for at most WAIT_MAX_S; how many may wait at once is the listener's
rule.

PM seen. GET snapshot carries `pm_seen`: {"waited", "pulled", "waiting"}. waited is the time
(the Api's clock) an AGENT-role caller last entered or left GET wait, pulled the time one last
read the pending work (GET pending, or GET wait with pending=1), waiting the number of agent
waits blocked in Store.wait right now. All three live in this Api object, not in the store: a
new Api (a server restart) starts at null, null and 0 until the next agent call. The owner's
own calls never count, nor does a call with `for` a key other than `pm` (another session's
wait for its own answers is not the PM listening). No lock is taken: each note is one assignment, and an open wait is one
key put into and popped from a dict, which the interpreter does as one step each.

Pending for one session. Every pending item carries `from`, the session key it is for
(Api.pending). GET pending and GET wait (with pending=1) take `for=<key>`: only that key's
items are listed (`for=pm`: see Routes), and such a wait is woken only by one of them, a reset or its timeout, so a
coordinator is not woken by another session's answer. A filtered read changes nothing: the
items it leaves out, and the ones it lists, stay pending for every reader until they are
handled (a question until it is logged). Without `for` every item is listed, as before.
GET wait (with pending=1) also takes `except=<id>,<id>`: the ids the caller has already acted
on are left out, and such a wait is woken only by another item, so an answer that waits for the
PM's log is not returned again at once. It too only hides: nothing is marked.

Routes. Every pending item also carries `route`: who is to handle it now. It is its `from` key
while that session can be reached, else `pm`, so the PM is the fallback for a creator that is
gone. An item whose `from` is `pm` is the PM's. Otherwise, once a GET pending or GET wait `for`
its key has handed the item to that key (an agent call), the route is the key for
ACK_WINDOW_S after that first hand-over and `pm` from then on while it is still pending (the
creator clears a to-do with its ack). Before any hand-over the route is the key while the key is
reachable: a wait `for` it is blocked now, or one started or ended within REACH_GRACE_S. `for=pm`
lists the items routed to `pm` and every answered question, whoever it is from (the PM logs each
ruling); a wait `for=pm` is also woken when an item's route turns to `pm` with no write. All of
this lives in this Api object: a new Api (a server restart) routes every item to `pm` until its
creator waits again. As for PM seen, no lock is taken: each note is one dict step, a dict is
copied before it is walked, and a key's map of blocked waits is never removed, so a wait that
opens while another closes is not lost.

Revision. A read's `revision` is taken before the data is copied, so it is never later than the
data: a client that waits `since` it is told of any write the copy missed. GET health gives the
store's folder to the owner only.
"""
import collections
import datetime
import json
import math
import re
import time

from kitlib import hubvalidate

from . import pagecheck
from . import schema
from . import todosteps
from .store import PAGES_LOCK, HubError

BASE = '/api/v1'
API_VERSION = 'v1'
OWNER, AGENT, ANY = 'owner', 'agent', 'any'
APPROVER = 'approver'               # a route for the strong owner alone ("Roles")
LOCK_WRITE = 'lock-write'           # a lock write: the owner here, the strong owner remotely
ROLES = (OWNER, AGENT, ANY, APPROVER, LOCK_WRITE)   # what a route may ask for
CALLERS = (OWNER, AGENT)            # what a caller may be
# why a route of role APPROVER refused a caller who is not the strong owner
NOT_STRONG = ('%s %s needs the owner logged in through Cloudflare Access: the owner token, a '
              'browser paired from it and an agent are not enough')
# why a route of role LOCK_WRITE refused a caller
NOT_LOCK_WRITER = ('%s %s is the owner\'s; over the tunnel it needs the owner logged in through '
                   'Cloudflare Access')
# why the page may not write the lock (GET lock/view's `writes`)
WRITES_WHY = {
    'agent': 'the machine lock takes this hub for an agent (it was started under an agent '
             'session): restart the hub from your own console or its scheduled task',
    'tunnel': 'over the tunnel a lock change needs the owner logged in through Cloudflare Access',
    'strong': 'reservations and the division need the strong owner login (a browser paired as '
              'the strong owner, or Cloudflare Access)'}
STATUS = {'invalid': 400, 'forbidden': 403, 'not_found': 404, 'conflict': 409, 'store': 503,
          'unavailable': 503, 'internal': 500}
WAIT_MAX_S = 55
LOCK_POLL_S = 0.5                   # how often GET lock/wait looks at the lock's revision
LOCK_DAYS_MAX = 366                 # GET lock/stats `days`
TUNNEL_VIA = 'tunnel'               # the tunnel listener's via (kitlib.hubsite.httpd): a remote view
ACK_WINDOW_S = 30 * 60              # a creator's time to handle an item handed to it ("Routes")
REACH_GRACE_S = WAIT_MAX_S + 30     # a key is reachable this long after its last wait ("Routes")
ROUTE_SLACK_S = 0.01                # a PM wait looks again this long after a route's turn is due
VIEWS = ('backlog', 'completed', 'notices', 'repos', 'usage', 'agents')
OWNER_FROM = 'you'                  # a to-do's or a story's `from` when the owner wrote it
AGENT_FROM = 'pm'                   # a to-do's `from` when an agent gives none
AGENT_STORY_STATUSES = ('review', 'questions', 'added', 'declined')
DAYS_MAX = 3650
# what a caller who is not the owner is told in place of the store's own message, by code
AGENT_SAYS = {'store': 'the hub store is not available: the owner sees why'}

Route = collections.namedtuple('Route', 'method pattern role handler')
Request = collections.namedtuple('Request', 'role params body query via strong')

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('', '0', 'false', 'no', 'off')
_ID_SPLIT = re.compile(r'([0-9]+)')
_NAME = re.compile(r'[\x21-\x7e]+(?: [\x21-\x7e]+)*')
_SURROGATE = re.compile('[\ud800-\udfff]')     # a lone one: half a character
# A hub item's id or a ruling's anywhere in a page id ("Pages"): hubvalidate._REF's pattern, plus
# A and R, searched, so neither `plan-Q-1` nor `Q-1.v2` holds a ref a reader would follow.
_REF_SHAPED = re.compile(r'(?<![A-Za-z0-9])[QMTPSAR]-\d+(?![A-Za-z0-9])', re.I)
PAGE_HTML_MAX = pagecheck.MAX_BYTES


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


def launcher_keys():
    """The session keys of the launcher's sessions.json (kitlib.launcher), read now; an empty
    set when the launcher's settings or that file cannot be read. HomeFenced (a test that
    reached for the real home) is not caught: a test gives Api its own session_keys."""
    try:
        from kitlib.launcher import settings as lsettings, state as lstate
        return {k for k, _ in lstate.entries(lstate.read_sessions(lsettings.load()))}
    except Exception:               # noqa: BLE001 - unreadable: only the PM's key is known
        return set()


def _for(d, for_key):
    """Whether pending item d (with `from` and `route`) is listed for for_key: every item with
    none; for AGENT_FROM, the ones routed to it and every question (the PM logs each ruling);
    for another key, its own."""
    if not for_key:
        return True
    if for_key == AGENT_FROM:
        return d['route'] == AGENT_FROM or d['kind'] == 'question'
    return d['from'] == for_key


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
    string (default: the store's clock); `session_keys` gives the launcher's session keys as an
    iterable, called afresh on each use (default: launcher_keys); `now` gives monotonic seconds
    for the routes (default: time.monotonic). `approval_key()` gives the approval key or None,
    read on each decide and verify; `checkout(repo_id)` gives the root of this machine's checkout
    of that repository or None ("Approvals"); both default to None, which no approval passes.
    `lock` is the machine lock's door or None ("Machine lock")."""

    def __init__(self, store, views=None, clock=None, session_keys=None, now=None,
                 approval_key=None, checkout=None, lock=None):
        self.store = store
        self.views = views
        self.lock = lock
        self.lock_poll_s = LOCK_POLL_S
        self.clock = clock or store.clock
        self.approval_key = approval_key or (lambda: None)
        self.checkout = checkout or (lambda repo: None)
        self._session_keys = session_keys or launcher_keys
        self._seen = {'waited': None, 'pulled': None}
        self._waits = {}            # one key per agent wait that is blocked right now
        # routes (module docstring "Routes"): one dict step per note, no lock
        self.now = now or time.monotonic
        self.ack_window_s = ACK_WINDOW_S
        self.reach_grace_s = REACH_GRACE_S
        self._listening = {}        # session key -> {wait token: True} for its blocked waits
        self._heard = {}            # session key -> `now` its last wait started or ended
        self._handed = {}           # (kind, id) -> (session key, `now` of the first hand-over)

    def call(self, method, path, role, body=None, query=None, via='', raise_unexpected=False,
             agent_words=False, strong=False):
        """(status, object) for one request. role is OWNER or AGENT as the caller resolved it;
        body is the decoded JSON body or None; query is {name: value}; via names the transport
        (web, cli, ...) and is recorded on every write. With raise_unexpected an exception that
        is no HubError is raised, not answered: the caller logs it and words its own 500. With
        agent_words=True an error answer says what an agent is told (module docstring), to the
        owner too; only False leaves the owner the error's own text, so a value that is neither
        is read as True. The role decides what the request may do either way. strong=True says
        the caller is the owner logged in through Cloudflare Access (module docstring, "Roles");
        it counts only for OWNER, and any value but True is not strong."""
        # read before the handler copies any data: a write in between then has a later
        # revision, so a client waiting since this one is told of it
        revision = self.store.revision
        full = role == OWNER and agent_words is False       # the error's own text: paths and all
        strong = strong is True and role == OWNER
        try:
            status, obj = 200, self._call(method, path, role, body, query, via, strong)
        except HubError as e:
            said = e.message if full else AGENT_SAYS.get(e.code, e.message)
            status, obj = STATUS.get(e.code, 500), error_body(e.code, said, e.field)
        except Exception as e:      # noqa: BLE001 - a transport gets an answer, never a traceback
            if raise_unexpected:
                raise
            said = '%s: %s' % (type(e).__name__, e) if full else type(e).__name__
            status, obj = 500, error_body('internal', said)
        obj.setdefault('revision', revision)
        return status, obj

    def _call(self, method, path, role, body, query, via, strong):
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
        if route.role == APPROVER:
            if not (role == OWNER and strong is True):
                raise HubError('forbidden', NOT_STRONG % (route.method, route.pattern), 'role')
        elif route.role == LOCK_WRITE:
            if not (role == OWNER and (strong is True or via != TUNNEL_VIA)):
                raise HubError('forbidden', NOT_LOCK_WRITER % (route.method, route.pattern),
                               'role')
        elif route.role != ANY and route.role != role:
            raise HubError('forbidden', '%s %s is for the %s, not the %s'
                           % (route.method, route.pattern, route.role, role), 'role')
        if query is not None and not isinstance(query, dict):
            raise _invalid('the query is an object of names and values', 'query')
        out = route.handler(self, Request(role, params, body, query, via if isinstance(via, str)
                                          else '', strong))
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

    def session_keys(self):
        """The keys a question may come from: the launcher's session keys, read now, and
        AGENT_FROM (the PM's), which is known even when they cannot be read."""
        try:
            keys = {k for k in self._session_keys() if isinstance(k, str)}
        except Exception:           # noqa: BLE001 - unreadable: only the PM's key is known
            keys = set()
        return keys | {AGENT_FROM}

    def pending(self, for_key=''):
        """The pending items, each with the session key it is for as `from`: a question's or a
        to-do's own `from` when that is a session key, else AGENT_FROM (so the owner's to-dos,
        free-text names, stories and notes are the PM's), and the key that is to handle it now
        as `route` (module docstring "Routes"). With for_key, only the items for that key; for
        AGENT_FROM, the items routed to it and every question. Reading never changes an item:
        what a filtered read leaves out stays pending for every other reader."""
        return [d for d in self._routed()[0] if _for(d, for_key)]

    def route_change_in(self):
        """Seconds until the route of a pending item next turns to AGENT_FROM with no write
        (a hand-over's window or a key's grace running out), or None when none will."""
        at = self._routed()[1]
        return None if at is None else max(0.0, at - self.now())

    def _routed(self):
        """([item], `now` of the next route change or None): the pending items with `from` and
        `route`. Hand-overs of items no longer pending are forgotten here."""
        found, keys, out = self.store.pending(), None, []
        for p in found:
            d = p._asdict()
            sender = d.pop('sender')
            mine = sender and sender not in (AGENT_FROM, OWNER_FROM)
            if mine and keys is None:
                keys = self.session_keys()          # read once, and only when it can matter
            d['from'] = sender if mine and sender in keys else AGENT_FROM
            out.append(d)
        now, flips = self.now(), []
        live = {(d['kind'], d['id']) for d in out}
        for gone in [k for k in list(self._handed) if k not in live]:
            self._handed.pop(gone, None)
        for d in out:
            d['route'], flip = self._route(d, now)
            if flip is not None:
                flips.append(flip)
        return out, (min(flips) if flips else None)

    def _route(self, d, now):
        """(route, `now` it turns to AGENT_FROM or None) of item d."""
        key = d['from']
        if key == AGENT_FROM:
            return AGENT_FROM, None
        handed = self._handed.get((d['kind'], d['id']))
        if handed is not None and handed[0] == key:
            until = handed[1] + self.ack_window_s
            return (key, until) if now < until else (AGENT_FROM, None)
        if self._listening.get(key):
            return key, None
        heard = self._heard.get(key)
        if heard is not None and now < heard + self.reach_grace_s:
            return key, heard + self.reach_grace_s
        return AGENT_FROM, None

    def listen(self, req, opened=None, closed=None):
        """Note that a wait `for` a session key other than AGENT_FROM started (`opened`, a
        token for a wait that now blocks), ended (`closed`, that token) or was answered at once
        (neither): the key is reachable (module docstring "Routes"). The owner's calls are not
        noted."""
        key = _q(req, 'for')
        if req.role != AGENT or key in ('', AGENT_FROM):
            return
        self._heard[key] = self.now()
        waits = self._listening.setdefault(key, {})
        if opened is not None:
            waits[opened] = True
        if closed is not None:
            waits.pop(closed, None)

    def handed(self, req, items):
        """Note that items went to the session key the request is `for` (an agent's call, a key
        other than AGENT_FROM): each of that key's own is handed over, the first time only. An
        item it now holds within the ack window gets the key as its `route` in `items` too, so
        the answer says who has it after this call, not before."""
        key = _q(req, 'for')
        if req.role != AGENT or key in ('', AGENT_FROM):
            return
        now = self.now()
        for d in items:
            if d.get('from') == key:
                got = self._handed.setdefault((d['kind'], d['id']), (key, now))
                if got[0] == key and now < got[1] + self.ack_window_s:
                    d['route'] = key

    def handed_to(self, kind, doc_id):
        """The session key item (kind, doc_id) was first handed over to by handed(), or None.
        Read-only, one dict read: kitlib.hubsite.push calls it from its own thread to tell that
        the session a push went to picked the item up."""
        got = self._handed.get((kind, doc_id))
        return got[0] if got is not None else None

    def seen(self, req, what, opened=None, closed=None):
        """Note that an agent (a PM session) just waited or pulled (`what`). `opened` is a key
        for a wait that now blocks, `closed` the key of one that ended. The owner's calls are
        not noted, nor another session's (`for` a key other than AGENT_FROM). An ended wait
        stops counting before the clock is read, and a clock that fails then leaves the earlier
        time: it never hides how the wait itself ended."""
        if req.role != AGENT or _q(req, 'for') not in ('', AGENT_FROM):
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
    # the session the answer goes back to: a launcher session key, read now, or the PM's
    who = (b.get('from') or '').strip() or AGENT_FROM
    if who != AGENT_FROM:
        keys = api.session_keys()
        if who not in keys:
            raise _invalid('from %s is not a session the hub knows: a question comes from a '
                           'launcher session key (known: %s)'
                           % (ascii(who), ', '.join(sorted(keys))), 'from')
    b['from'] = who
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


def todo_steps(b, judge=True):
    """The fields b of a new to-do with its `steps` (kitlib.hubsite.todosteps) made into text:
    their commands after the given ones, their summary at the end of the detail (or the detail,
    when there is none), and no `steps` field left. Refused (`invalid`, the field named): a
    `commands` that is not a list, a given command holding a lone surrogate, a bad step and, when
    `judge` (an agent's to-do or edit), text the owner's to-do rules forbid (todosteps.check).
    The rules judge the text as given, before any step is rendered: the owner's text is never
    judged."""
    b = dict(b)
    steps = b.pop('steps', None)
    commands = _typed(b, 'commands', (list,), 'a list of strings')
    for i, c in enumerate(commands or ()):
        m = _SURROGATE.search(c) if isinstance(c, str) else None
        if m:
            raise _invalid('commands[%d] holds a lone surrogate (U+%04X) at character %d, half '
                           'of a character that cannot be written'
                           % (i, ord(m.group()), m.start()), 'commands')
    try:
        if judge:
            todosteps.check(dict(b, steps=steps))
        if steps is not None:
            b['commands'] = list(commands or []) + todosteps.render(steps)
            lines = '\n'.join(todosteps.summary(steps))
            b['detail'] = (b['detail'].rstrip() + '\n\n' + lines) if b.get('detail') else lines
    except (todosteps.StepError, todosteps.Refused) as e:
        raise _invalid(str(e), e.field)
    return b


def todos_add(api, req):
    owner = req.role == OWNER
    names = ('title', 'detail', 'commands', 'steps', 'repo', 'refs', 'urgent')
    b = _fields(req, names if owner else names + ('from',), required=('title',),
                flags=('urgent',))
    for name in ('title', 'detail', 'repo', 'from'):
        _text(b, name)
    b = todo_steps(b, judge=not owner)
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
    out = api.change('todos', doc_id, d, changes, req, changes)
    # a reopened or re-done to-do is pending afresh: an old hand-over must not outlive it when
    # no read ran in between (_routed forgets only what it sees gone)
    api._handed.pop(('todo', doc_id), None)
    return out


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


def _creator(b, doc_id, d, what):
    """An agent's edit or withdrawal: refused (`forbidden`, field from) unless the `from` the
    agent gives is the item's own (AGENT_FROM when the item has none) and the item is not the
    owner's. All agents share one token, so this is a claim checked against the item: it stops a
    mistake, not an agent that gives another's name."""
    stored = d.get('from') or AGENT_FROM
    claimed = (b.get('from') or '').strip()
    if stored == OWNER_FROM or claimed != stored:
        raise HubError('forbidden', '%s is from %s: an agent can %s only an item it added, '
                       'giving that item\'s from (%s was given)'
                       % (doc_id, stored, what, ascii(claimed)), 'from')


def _still_open(doc_id, d, what):
    """Refused (`conflict`, field status) once the item is no longer open: an answered, logged
    or done item is the owner's record and is not changed under it."""
    if d.get('status') != 'open':
        raise HubError('conflict', '%s is %s: only an open item can be %s'
                       % (doc_id, d.get('status'), what), 'status')


def _withdrawn(api, coll, doc_id, d):
    r = api.store.delete(coll, doc_id, if_version=_version(d))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def questions_edit(api, req):
    names = ('title', 'context', 'options', 'recommended', 'refs', 'urgent')
    b = _fields(req, names + ('from', 'if_version'), required=('from',), flags=('urgent',))
    _pin(b, required=True)
    for name in ('from', 'title', 'context'):
        _text(b, name)
    _typed(b, 'options', (list,), 'a list of {label, description}')
    changes = {k: v for k, v in b.items() if k not in ('from', 'if_version')}
    if not changes:
        raise _invalid('give at least one of %s to change' % ', '.join(names), 'body')
    if 'title' in changes and not changes['title'].strip():
        raise _invalid('title cannot be made empty', 'title')
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    _creator(b, doc_id, d, 'edit')
    _still_open(doc_id, d, 'edited')
    return api.change('questions', doc_id, d, changes, req, {'status': 'open'})


def questions_delete(api, req):
    owner = req.role == OWNER
    b = _fields(req, ('if_version',) if owner else ('from', 'if_version'),
                required=() if owner else ('from',))
    _pin(b, required=not owner)
    _text(b, 'from')
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    if not owner:
        _creator(b, doc_id, d, 'withdraw')
    _still_open(doc_id, d, 'withdrawn')
    return _withdrawn(api, 'questions', doc_id, d)


def todos_edit(api, req):
    owner = req.role == OWNER
    names = ('title', 'detail', 'commands', 'repo', 'refs', 'urgent')
    b = _fields(req, names + (('if_version',) if owner else ('from', 'if_version')),
                required=() if owner else ('from',), flags=('urgent',),
                blank=('repo', 'detail'))
    _pin(b, required=not owner)
    for name in ('title', 'detail', 'repo', 'from'):
        _text(b, name)
    for name in ('commands', 'refs'):
        _typed(b, name, (list,), 'a list')
    changes = {k: v for k, v in b.items() if k not in ('from', 'if_version')}
    if not changes:
        raise _invalid('give at least one of %s to change' % ', '.join(names), 'body')
    if 'title' in changes and not changes['title'].strip():
        raise _invalid('title cannot be made empty', 'title')
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    if owner:
        _own(doc_id, d, 'edit')
    else:
        _creator(b, doc_id, d, 'edit')
        _still_open(doc_id, d, 'edited')
        # the text an agent gives is judged by the rules its add was (todo_steps)
        changes = todo_steps(changes, judge=True)
    if d.get('pm_ack'):             # the PM read the old text: the changed one is pending again
        changes['pm_ack'] = False
    return api.change('todos', doc_id, d, changes, req, {'status': d.get('status')})


def todos_delete(api, req):
    owner = req.role == OWNER
    b = _fields(req, ('if_version',) if owner else ('from', 'if_version'),
                required=() if owner else ('from',))
    _pin(b, required=not owner)
    _text(b, 'from')
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    if not owner:
        _creator(b, doc_id, d, 'withdraw')
        _still_open(doc_id, d, 'withdrawn')
    elif d.get('from') != OWNER_FROM:
        # the owner deletes a to-do of their own at any status, an agent's only while open
        _still_open(doc_id, d, 'deleted')
    return _withdrawn(api, 'todos', doc_id, d)


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


# approvals: imported here, not at the top, as kitlib.hubsite.approvals imports this module
def _approval(make, *args):
    from . import approvals as ap
    try:
        return make(*args)
    except ap.ApprovalError as e:
        raise HubError(e.code, e.message, e.field)


def approvals_add(api, req):
    from . import approvals as ap
    b = _fields(req, ('repo', 'title', 'head', 'files', 'reason'))
    doc = _approval(ap.new_doc, b.get('repo'), b.get('title'), b.get('head'), b.get('files'),
                    _when(api.clock(), ''), b.get('reason'))
    root = api.checkout(doc['repo'])
    if not root:
        raise _invalid('this machine has no checkout of %s' % doc['repo'], 'repo')
    lines = []
    for f in doc['files']:
        base, why = ap.read_base(root, doc['head'], f['path'], f['base_blob'])
        if why:
            raise _invalid('%s: %s' % (f['path'], why), 'files')
        lines.extend(ap.diff(f['path'], base, f['content']))
    doc['diff'] = lines
    return api.add(ap.COLLECTION, doc, req)


def approvals_decide(api, req):
    from . import approvals as ap
    b = _fields(req, ('decision', 'if_version'))
    _pin(b, required=True)
    doc_id = req.params['id']
    d = api.doc(ap.COLLECTION, doc_id, b)
    changes = _approval(ap.decide_fields, api.approval_key(), doc_id, d, b.get('decision'),
                        _when(api.clock(), ''))
    return api.change(ap.COLLECTION, doc_id, d, changes, req, {'status': changes['status']})


def approvals_consume(api, req):
    from . import approvals as ap
    b = _fields(req, ('commit', 'if_version'))
    _pin(b, required=True)
    doc_id = req.params['id']
    d = api.doc(ap.COLLECTION, doc_id, b)
    changes = _approval(ap.consume_fields, api.approval_key(), doc_id, d, b.get('commit'),
                        _when(api.clock(), ''))
    return api.change(ap.COLLECTION, doc_id, d, changes, req, {'status': changes['status']})


def approvals_verify(api, req):
    from . import approvals as ap
    doc_id = req.params['id']
    d = api.store.get(ap.COLLECTION, doc_id) if schema.check_id(doc_id) is None else None
    if d is None:
        return {'ok': True, 'valid': False, 'why': ap.WHY_UNKNOWN}
    try:
        blobs = json.loads(_q(req, 'files'))
    except ValueError:
        blobs = None                # check() answers WHY_FILES
    # a commit given, even empty, is checked: only an absent one leaves it out
    commit = _q(req, 'commit') if 'commit' in (req.query or {}) else None
    valid, why = ap.check(api.approval_key(), doc_id, d, _q(req, 'repo'), _q(req, 'head'), blobs,
                          _when(api.clock(), ''), commit)
    return {'ok': True, 'valid': valid, 'why': why}


def backlog(api, req):
    return {'repos': api.shown('backlog', repo=_q(req, 'repo'), state=_q(req, 'state'),
                               fresh=_q_flag(req, 'fresh'))}


def completed(api, req):
    return {'repos': api.shown('completed', repo=_q(req, 'repo'),
                               days=_q_int(req, 'days', 1, DAYS_MAX))}


# A record route's path segments, checked before the view is asked: what a repository name or a
# record id may be (kitlib.hubsite.backlog checks each again, more narrowly). The refusal is a
# fixed phrase: it never repeats the segment.
_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
SEGMENT_SAYS = {'repo': 'not a repository name', 'id': 'not a record id'}


def _segment(req, name):
    value = req.params.get(name)
    if not isinstance(value, str) or not _SEGMENT.match(value) or '..' in value:
        raise HubError('invalid', SEGMENT_SAYS[name], name)
    return value


def _record_view(api, name, *args):
    """The record view `name` called with args: its HubError (not_found, invalid, unavailable,
    each with a fixed phrase) is the answer; anything else it raises is `unavailable` with the
    short reason. Unlike `shown`, a not_found stays a 404."""
    fn = api._view(name)
    try:
        return fn(*args)
    except HubError:
        raise
    except Exception as e:      # noqa: BLE001 - a view is another module's code
        raise HubError('unavailable', 'the %s view failed: %s' % (name, _reason(e)), 'views')


def record(api, req):
    return _record_view(api, 'record', _segment(req, 'repo'), _segment(req, 'id'))


def record_new(api, req):
    return _record_view(api, 'record_template', _segment(req, 'repo'))


# ---- the owner's record writes (docs/action-hub.md "The record page")
def _strings(b, name):
    """b[name] as a list of strings: absent is [], one string is a list of one."""
    v = b.get(name)
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise _invalid('%s must be a list of strings' % name, name)
    return v


def record_add(api, req):
    repo = _segment(req, 'repo')
    b = _fields(req, ('id', 'summary', 'scope', 'effort', 'ref'), ('id', 'summary', 'scope'))
    out = _record_view(api, 'record_add', repo, _text(b, 'id'), _text(b, 'summary'),
                       _strings(b, 'scope'), _text(b, 'effort'), _text(b, 'ref'))
    return dict(out, ok=True)


def record_edit(api, req):
    repo, rid = _segment(req, 'repo'), _segment(req, 'id')
    b = _fields(req, ('version', 'changes', 'unset', 'body', 'ruling'), ('version',))
    changes = _typed(b, 'changes', (dict,), 'an object of field names and values') or {}
    out = _record_view(api, 'record_edit', repo, rid, _text(b, 'version'),
                       [(k, _strings(changes, k)) for k in changes], _strings(b, 'unset'),
                       _text(b, 'body'), _text(b, 'ruling'))
    return dict(out, ok=True)


def record_delete(api, req):
    repo, rid = _segment(req, 'repo'), _segment(req, 'id')
    b = _fields(req, ('why', 'confirm', 'ruling'), ('why', 'confirm'))
    if _text(b, 'confirm') != rid:
        raise _invalid('confirm must repeat the record id to delete it', 'confirm')
    out = _record_view(api, 'record_delete', repo, rid, _text(b, 'why'), _text(b, 'ruling'))
    return dict(out, ok=True)
# ---- end of the owner's record writes


def _listing(name):
    def handler(api, req):
        return {'items': api.shown(name)}
    handler.__name__ = name
    return handler


def pending(api, req):
    items = api.pending(_q(req, 'for'))
    api.seen(req, 'pulled')
    api.handed(req, items)
    return {'items': items}


def _timeout(req):
    """A wait's `timeout` query: seconds, WAIT_MAX_S when not given; not capped here."""
    raw = _q(req, 'timeout')
    try:
        timeout = float(raw) if raw else float(WAIT_MAX_S)
    except ValueError:
        timeout = float('nan')
    if not math.isfinite(timeout) or timeout < 0:
        raise _invalid('timeout is a number of seconds from 0 to %d, not %r' % (WAIT_MAX_S, raw),
                       'timeout')
    return timeout


def wait(api, req):
    since = _q_int(req, 'since', 0, 10 ** 12)
    if since is None:
        since = api.store.revision
    timeout = _timeout(req)
    want = _q_flag(req, 'pending')
    for_key = _q(req, 'for')
    if for_key and not want:
        raise _invalid('for picks among the pending items: give pending=1 with it', 'for')
    skip = {s.strip() for s in _q(req, 'except').split(',') if s.strip()}
    if skip and not want:
        raise _invalid('except leaves out pending items: give pending=1 with it', 'except')
    filtered = bool(for_key or skip)

    def mine():
        return [p for p in api.pending(for_key) if p['id'] not in skip]
    # pending is read whether asked for or not: it is the read that refuses a closed store,
    # which Store.wait answers at once and with nothing (a client would loop on it)
    found = mine()
    items = found if want else []
    if want:
        api.seen(req, 'pulled')
    if items:
        api.seen(req, 'waited')
        api.listen(req)
        ch = api.store.changes_since(since)
    else:
        key = object()
        api.seen(req, 'waited', opened=key)
        api.listen(req, opened=key)
        try:
            if filtered:
                ch = _wait_for(api, since, min(timeout, WAIT_MAX_S), mine,
                               for_key == AGENT_FROM)
            else:
                ch = api.store.wait(since, min(timeout, WAIT_MAX_S))
        finally:
            api.seen(req, 'waited', closed=key)
            api.listen(req, closed=key)
        found = mine()
        items = found if want else []
    api.handed(req, items)
    # a filtered wait ends on its own items, a reset or the timeout, never on other writes
    quiet = not ch.reset and not items and (filtered or not ch.events)
    return {'revision': ch.revision, 'reset': ch.reset,
            'timed_out': quiet,
            'changes': [{'collection': e.collection, 'id': e.id, 'op': e.op, 'status': e.status}
                        for e in ch.events],
            'pending': items}


def _wait_for(api, since, timeout, mine, routes=False):
    """Store.wait until mine() (the pending items this wait is for: its `for` key, less its
    `except` ids) is not empty, the store resets, or timeout seconds pass; a write that leaves
    it empty waits on. With routes (a wait for the PM) it also looks again when an item's route
    next turns to the PM, which no write announces. The changes since `since`, all of them."""
    end = time.monotonic() + timeout
    at = since
    while True:
        left = max(0.0, end - time.monotonic())
        if routes:
            flip = api.route_change_in()
            if flip is not None:
                left = min(left, flip + ROUTE_SLACK_S)
        ch = api.store.wait(at, left)
        if ch.reset:
            return ch
        if mine() or time.monotonic() >= end:
            break
        at = ch.revision
    return api.store.changes_since(since)


def pages_list(api, req):
    return {'items': api.items('pages', req)}


def pages_publish(api, req):
    b = _fields(req, ('id', 'title', 'html', 'repo', 'from', 'summary', 'refs', 'if_version'),
                required=('id', 'title', 'html'))
    for name in ('id', 'title', 'html', 'repo', 'from', 'summary'):
        _text(b, name)
    pin = _pin(b)
    doc_id = b['id']
    why = schema.check_id(doc_id)
    if why:
        raise _invalid(why, 'id')
    shaped = _REF_SHAPED.search(doc_id)
    if shaped:
        raise _invalid('a page id cannot hold a hub item\'s or a ruling\'s id (%s holds %s): a ref '
                       'that names it would be read as that item' % (doc_id, shaped.group()), 'id')
    refs = _typed(b, 'refs', (list,), 'a list of strings')
    if refs is not None and not all(isinstance(r, str) for r in refs):
        raise _invalid('refs must be a list of strings', 'refs')
    why = pagecheck.check(b['html'])
    if why:
        raise _invalid(why, 'html')
    with PAGES_LOCK:
        return _pages_write(api, req, b, doc_id, pin)


def _pages_write(api, req, b, doc_id, pin):
    """pages_publish from the reads on, called with PAGES_LOCK held."""
    card, body = api.store.get('pages', doc_id), api.store.get('page_html', doc_id)
    if card is not None and pin is None:
        raise HubError('conflict', 'pages/%s is at version %d: give it as if_version to publish '
                       'over it' % (doc_id, _version(card)), 'if_version')
    if card is not None and pin != _version(card):
        raise HubError('conflict', 'pages/%s is at version %d, not %d'
                       % (doc_id, _version(card), pin), 'if_version')
    if card is None and pin:
        raise HubError('conflict', 'there is no pages/%s at version %d: it is not there'
                       % (doc_id, pin), 'if_version')
    size = len(b['html'].encode('utf-8'))
    fields = {k: b[k] for k in ('title', 'repo', 'summary', 'refs') if k in b}
    fields.update({'from': _agent_from(b.get('from') or ''), 'at': api.clock(), 'bytes': size,
                   'via': _via(req)})
    # the body first, each write pinned to what was read under the lock: a card never names a
    # body that is not there, and a second publisher reads only after both writes ("Pages")
    wrote = api.store.put('page_html', doc_id, {'html': b['html'], 'via': _via(req)},
                          if_version=_version(body) if body is not None else 0)
    r = api.store.put('pages', doc_id, fields, if_version=_version(card) if card is not None else 0)
    return api.written(r, {'bytes': size, 'html_version': wrote.version})


def pages_get(api, req):
    doc_id = req.params['id']
    d = api.doc('page_html', doc_id)
    return {'ok': True, 'id': doc_id, 'html': d.get('html'), 'version': _version(d)}


def pages_delete(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    with PAGES_LOCK:        # no publish runs between the reads and the two deletes
        d = api.doc('pages', doc_id, b)
        r = api.store.delete('pages', doc_id, if_version=_version(d))
        body = api.store.get('page_html', doc_id)   # the card first: it never names no body
        if body is not None:
            # A body delete that fails (the store's own error: under the lock no publish can
            # move its version) leaves an orphan body with no card. The card stays deleted:
            # hubvalidate warns of the orphan, no card offers it, and the next publish of
            # this id writes over it ("Pages").
            r = api.store.delete('page_html', doc_id, if_version=_version(body))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def snapshot(api, req):
    revision, docs = api.store.snapshot()
    docs.pop('page_html', None)     # a body is read on its own (GET pages/{id}, "Pages")
    views, missing = {}, {}
    for name in VIEWS:
        try:
            views[name] = api.view(name)
        except Exception as e:      # noqa: BLE001 - the first paint shows what there is
            views[name] = None
            missing[name] = _reason(e)
    return {'revision': revision, 'collections': docs,
            'pending': api.pending(),
            'views': views, 'unavailable': missing, 'pm_seen': api.pm_seen(),
            'strong': req.strong is True}


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


def _remote(req):
    return req.via == TUNNEL_VIA


def _lock(api, read, *args, **kw):
    """read(door, ...) on the Api's lock door ("Machine lock"), a read or a write. No door, or a door that raises
    anything but a HubError, is `unavailable` (field lock), named by the exception's class
    only. HomeFenced and other BaseExceptions pass."""
    door = api.lock
    if door is None:
        raise HubError('unavailable', 'this server has no machine lock', 'lock')
    try:
        return read(door, *args, **kw)
    except HubError:
        raise
    except Exception as e:          # noqa: BLE001 - any lock failure is the lock being unavailable
        raise HubError('unavailable', 'the machine lock could not be reached (%s)' % _reason(e),
                       'lock')


def _writes(req, view):
    """What this caller may change from the page ("Machine lock"): {lock, strong, why}. lock: the
    LOCK_WRITE routes; strong: the APPROVER ones as well; why: the reason one is false, or ''."""
    if not isinstance(view, dict) or view.get('writer') != 'owner':
        return {'lock': False, 'strong': False, 'why': WRITES_WHY['agent']}
    strong = req.role == OWNER and req.strong is True
    lock = strong or (req.role == OWNER and not _remote(req))
    why = '' if strong else WRITES_WHY['strong'] if lock else WRITES_WHY['tunnel']
    return {'lock': lock, 'strong': strong, 'why': why}


def _viewed(api, req):
    """The lock door's view for this caller, with `writes`."""
    out = dict(_lock(api, lambda door: door.view(remote=_remote(req))))
    out['writes'] = _writes(req, out)
    return out


def lock_view(api, req):
    return _viewed(api, req)


def _wrote(api, req, write):
    """write(door, remote) -> its result; the answer {ok, result} plus the view after it (view
    null, writes all false, when the lock cannot be read after the write)."""
    remote = _remote(req)
    result = _lock(api, write, remote)
    try:
        out = _viewed(api, req)
    except HubError:
        out = {'lock_revision': None, 'view': None, 'writer': None,
               'writes': {'lock': False, 'strong': False, 'why': ''}}
    out.update(ok=True, result=result)
    return out


def _confirmed(req, b):
    if b.get('confirm') != req.params['id']:
        raise _invalid('confirm must repeat the run id %s' % req.params['id'], 'confirm')


def _strs(b, names):
    return [_text(b, n) for n in names]


def lock_hold_add(api, req):
    b = _fields(req, ('kind', 'scope', 'start', 'end', 'note', 'cpu', 'gpu'), ('kind', 'scope'))
    kind = _text(b, 'kind')
    if kind == 'reserve':
        scope, start, end, note = _strs(b, ('scope', 'start', 'end', 'note'))
        if b.get('cpu') or b.get('gpu'):
            raise _invalid('a reserve takes no slots: it is a whole or half hold', 'cpu')
        return _wrote(api, req, lambda door, remote: door.reserve(scope, start, end, note,
                                                                  remote=remote))
    if kind == 'hold':
        scope, start, end, note, cpu, gpu = _strs(b, ('scope', 'start', 'end', 'note', 'cpu',
                                                      'gpu'))
        return _wrote(api, req, lambda door, remote: door.hold(scope, start, end, note, cpu, gpu,
                                                               remote=remote))
    raise _invalid('kind is reserve or hold, not %r' % (kind,), 'kind')


def lock_hold_edit(api, req):
    b = _fields(req, ('start', 'end', 'note'))
    start, end, note = _strs(b, ('start', 'end', 'note'))
    hid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.edit(hid, start, end, note, remote=remote))


def lock_hold_end(api, req):
    _fields(req, ())
    hid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.end_hold(hid, remote=remote))


def lock_division(api, req):
    b = _fields(req, ('fields', 'defaults'), flags=('defaults',))
    defaults = _flag(b, 'defaults', False)
    fields = _typed(b, 'fields', (dict,), 'an object')
    if defaults == (fields is not None):
        raise _invalid('give the division fields, or defaults true, not both', 'fields')
    return _wrote(api, req, lambda door, remote: door.division(None if defaults else fields,
                                                               remote=remote))


def lock_run_end(api, req):
    b = _fields(req, ('confirm',), ('confirm',))
    _confirmed(req, b)
    rid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.end_run(rid, remote=remote))


def lock_run_top(api, req):
    _fields(req, ())
    rid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.move_to_top(rid, remote=remote))


def lock_game_mode(api, req):
    b = _fields(req, ('enabled',), ('enabled',), flags=('enabled',))
    enabled = b['enabled']
    return _wrote(api, req, lambda door, remote: door.game_mode(enabled, remote=remote))


def lock_games(api, req):
    b = _fields(req, ('text',), blank=('text',))
    text = _text(b, 'text') or ''
    return _wrote(api, req, lambda door, remote: door.games(text, remote=remote))


def lock_stats(api, req):
    days = _q_int(req, 'days', 1, LOCK_DAYS_MAX)
    since, until = _q(req, 'since') or None, _q(req, 'until') or None
    return _lock(api, lambda door: door.stats(days=days, since=since, until=until))


def lock_wait(api, req):
    since = _q_int(req, 'since', 0, 10 ** 12)
    timeout = min(_timeout(req), float(WAIT_MAX_S))
    api.store.check_open()
    if since is None:
        since = _lock(api, lambda door: door.revision())
    end = time.monotonic() + timeout
    # the lock store wakes nobody: look at its revision every lock_poll_s, sleeping in the hub
    # store's wait so that closing the hub ends this wait at once
    while _lock(api, lambda door: door.revision()) == since:
        left = end - time.monotonic()
        if left <= 0:
            break
        api.store.wait(api.store.revision, min(api.lock_poll_s, left))
        api.store.check_open()
    out = _viewed(api, req)
    out['changed'] = out.get('lock_revision') != since
    return out


def _r(method, path, role, handler):
    return Route(method, BASE + path, role, handler)


ROUTES = (
    _r('GET', '/questions', ANY, questions_list),
    _r('POST', '/questions', AGENT, questions_add),
    _r('POST', '/questions/{id}/answer', OWNER, questions_answer),
    _r('POST', '/questions/{id}/log', AGENT, questions_log),
    _r('POST', '/questions/{id}/edit', AGENT, questions_edit),
    _r('POST', '/questions/{id}/delete', ANY, questions_delete),
    _r('GET', '/todos', ANY, todos_list),
    _r('POST', '/todos', ANY, todos_add),
    _r('POST', '/todos/{id}/done', OWNER, todos_done),
    _r('POST', '/todos/{id}/ack', AGENT, todos_ack),
    _r('POST', '/todos/{id}/edit', ANY, todos_edit),
    _r('POST', '/todos/{id}/delete', ANY, todos_delete),
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
    _r('GET', '/pages', ANY, pages_list),
    _r('POST', '/pages', AGENT, pages_publish),
    _r('GET', '/pages/{id}', ANY, pages_get),
    _r('POST', '/pages/{id}/delete', OWNER, pages_delete),
    _r('POST', '/approvals', AGENT, approvals_add),
    _r('POST', '/approvals/{id}/decide', APPROVER, approvals_decide),
    _r('POST', '/approvals/{id}/consume', AGENT, approvals_consume),
    _r('GET', '/approvals/{id}/verify', AGENT, approvals_verify),
    _r('GET', '/backlog', ANY, backlog),
    _r('GET', '/completed', ANY, completed),
    _r('GET', '/records/{repo}/new', ANY, record_new),
    _r('GET', '/records/{repo}/{id}', ANY, record),
    _r('POST', '/records/{repo}', OWNER, record_add),
    _r('POST', '/records/{repo}/{id}/edit', OWNER, record_edit),
    _r('POST', '/records/{repo}/{id}/delete', OWNER, record_delete),
    _r('GET', '/notices', ANY, _listing('notices')),
    _r('GET', '/repos', ANY, _listing('repos')),
    _r('GET', '/usage', ANY, _listing('usage')),
    _r('GET', '/agents', ANY, _listing('agents')),
    _r('GET', '/pending', ANY, pending),
    _r('GET', '/wait', ANY, wait),
    _r('GET', '/snapshot', ANY, snapshot),
    _r('GET', '/health', ANY, health),
    _r('GET', '/lock/view', OWNER, lock_view),
    _r('GET', '/lock/stats', OWNER, lock_stats),
    _r('GET', '/lock/wait', OWNER, lock_wait),
    _r('POST', '/lock/holds', APPROVER, lock_hold_add),
    _r('POST', '/lock/holds/{id}/edit', APPROVER, lock_hold_edit),
    _r('POST', '/lock/holds/{id}/end', APPROVER, lock_hold_end),
    _r('POST', '/lock/division', APPROVER, lock_division),
    _r('POST', '/lock/runs/{id}/end', LOCK_WRITE, lock_run_end),
    _r('POST', '/lock/runs/{id}/top', LOCK_WRITE, lock_run_top),
    _r('POST', '/lock/game_mode', LOCK_WRITE, lock_game_mode),
    _r('POST', '/lock/games', LOCK_WRITE, lock_games),
)
# the handlers that block: the listener counts each call to one as a wait (kitlib.hubsite.httpd)
WAIT_HANDLERS = (wait, lock_wait)
