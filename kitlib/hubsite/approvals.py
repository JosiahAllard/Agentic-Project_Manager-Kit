"""kitlib.hubsite.approvals: the owner's approval of an edit to an owner-only path, or of one
run of a delegated operation (the op kind, below).

An agent asks the owner to approve an exact change: the files it will commit, each with the blob
it starts from and the content it will write. The owner decides it on the hub page, only as the
strong owner (kitlib.hubsite.auth, "Strong sessions"; api.APPROVER). An approved request is
signed with the approval key (auth.read_approval_key), so a document edited in the store folder
by anyone without the key no longer verifies. The commit and land steps ask the server whether
their change is the approved one (Verifier) and fail closed on any doubt.

  PENDING_TTL_S, APPROVED_TTL_S   how long a request waits for a decision, and how long an
                                  approval lasts (the owner's choice: 7 days, 24 hours)
  blob_id(content)                git's blob id of the content (SHA-1 object format)
  repo_id(url)                    a remote URL as the repository's identity: credentials,
                                  scheme and `.git` dropped, the host lower-cased
  as_repo_id(value)               a repo_id kept as it is, a remote URL made one
  check_path(path)                why a path cannot be approved, or None
  new_doc(repo, title, head, files, now, reason=None)
                                  the pending document of a request; ApprovalError
  canonical(doc_id, doc)          the bytes a decision's signature covers
  sign(key, doc_id, doc)          that signature: HMAC with SHA-256 under the approval key, hex
  expires_at(doc)                 when the document stops counting, or None (never or unknown)
  status_at(doc, now)             its status at `now`, expiry applied; None for a bad document
  decide_fields(key, doc_id, doc, decision, now)
                                  the fields a decision writes; ApprovalError
  consume_fields(key, doc_id, doc, commit, now)
                                  the fields a consume writes, for a signed approval never
                                  consumed; ApprovalError
  check(key, doc_id, doc, repo, head, blobs, now, commit=None)
                                  (True, '') when this change is the approved one (given a
                                  commit, also consumed by that commit), else (False, why):
                                  the server's verify, pure
  remote_id(checkout, remote='origin', run=None)
                                  (repo_id, None) of a checkout's remote, or (None, why)
  read_base(checkout, head, path, base_blob, cat=None)
                                  (text, None) of a file at head, read with git and checked to
                                  be base_blob, or (None, why)
  diff(path, base, content)       the unified diff lines the page shows for one file
  same_content_error(checkout, base, parent, paths, call=None)
                                  why a change made on the commit `parent` is not made on the
                                  same content as on the approved base `base`, for `paths`, or
                                  None (the same-content rule, below)
  Verifier(client)                .verify(doc_id, repo, head, blobs, commit=None) -> (ok, why),
                                  through the verify route; fails closed. .verify_on(checkout,
                                  doc_id, repo, parent, blobs, commit=None, base=None): the same
                                  for a change made on the commit `parent`, which may descend
                                  from the approved base (the same-content rule)
  request(client, repo, title, head, files, reason=None) / consume(client, doc_id, commit,
  if_version)                     the agent's add and consume calls

The op kind: one run of a delegated operation (docs/delegations.md "Hub approvals").
  PENDING_OP_TTL_S                how long an op request waits for a decision (24 hours)
  line_sha256(line)               the hex SHA-256 of a run line's UTF-8 bytes
  new_op_doc(op, repo, line, title, now, reason=None, what=None)
                                  the pending document of an op request; ApprovalError
  check_op(key, doc_id, doc, op, repo, args_sha256, now)
                                  (True, '') when `doc` approves this run of `op` and was not
                                  consumed, else (False, why): the server's op verify, pure
  consume_op_fields(key, doc_id, doc, args_sha256, now)
                                  the fields an op consume writes; ApprovalError
  request_op(client, op, repo, line, title, reason=None, what=None) / verify_op(client, doc_id,
  op, repo, args_sha256) -> (ok, why, version) / consume_op(client, doc_id, args_sha256,
  if_version)                     the agent's calls; verify_op fails closed
check() and consume_fields() refuse an op document (WHY_KIND_OP), and check_op() and
consume_op_fields() a file one (WHY_KIND_FILES).

The document (collection `approvals`, ids A-<n>):
  status      pending, approved, denied, consumed or expired (STATUSES)
  repo        repo_id of the repository's remote (the owner's choice: the remote URL)
  title, reason
  head        the commit the change applies to (40 hex)
  files       [{path, base_blob, blob, content}] sorted by path: base_blob is the blob at
              `head` (None for a new file), blob is blob_id(content) (None with content None,
              a delete); the server computes blob, never takes it from the caller
  created_at  ISO time with offset
  decision, decided_at, sig      once decided (sig on an approval only)
  commit, consumed_at            once consumed

An op document has no head, files or diff; it holds instead:
  kind        'op' (a document with no kind is a file approval)
  op          the delegation id D-nnn (OP_ID), and repo its repo name or `*` (OP_REPO)
  line        the run line the runner will spawn, the arguments filled in; what, the
              operation's `what`, shown beside it
  args_sha256 line_sha256(line), computed here, never taken from the caller
  consumed_at once consumed (an op consume writes no commit)
Its signature covers the id, kind, op, repo, args_sha256, the decision and decided_at; the line
is bound through args_sha256, and check_op refuses a line that no longer hashes to it. A pending
op request expires PENDING_OP_TTL_S after created_at, an approval APPROVED_TTL_S after
decided_at; only `approved` verifies: a consumed op approval was used once and is done.

A file approval's signature covers the id, repo, head, every file's (path, base_blob, blob), the
decision and decided_at (canonical): editing any of them, the expiry included, voids it. The content is left
out, as blob binds it. The status is not covered: only `approved` and `consumed` can verify, and
both need a signed approval.

The same-content rule (docs/commit.md "Owner approval"): the server's check compares `head` with
the signed head, the approved base, exactly, and never reads git. A change made on another commit
P counts as made on the approved base B when same_content_error(checkout, B, P, paths) is None:
B is an ancestor of P (`git merge-base --is-ancestor`), and every one of `paths` (the paths the
change touches) has the same tree entry at P as at B: the same mode, type and object id, or no
entry at either. The client proves that with git, then asks the server to verify the change for
B (Verifier.verify_on). So the change against P is, path for path, the change the owner saw
against B: a path edited, created, removed, renamed or given another mode between B and P
refuses. Owner-only paths the change does not touch are not compared (the owner's choice: the
approved paths only).

Expiry is read, never written by a timer: a pending request expires PENDING_TTL_S after
created_at; an approval APPROVED_TTL_S after decided_at, consumed or not (the owner's choice:
a consumed approval verifies until it expires, so a land after the commit can still check it).
A time that cannot be read, a decision dated after now and a naive now count as expired. Denied
and expired are final.

Routes (kitlib.hubsite.api): the helpers below call ROUTE (POST add), ROUTE/<id>/consume (POST)
and ROUTE/<id>/verify (GET, query repo, head, files as JSON {path: blob} and an optional commit;
it answers {"ok": true, "valid": bool, "why": str} and never the key or the sig); the owner's
ROUTE/<id>/decide (POST) is the page's. No printing, no exit.
"""
import datetime
import difflib
import hashlib
import hmac
import json
import re

from kitlib import hubvalidate
from kitlib.git import batch, cmd

from .api import AGENT
from .client import part as _part

# How long an approval counts, as the owner chose them.
PENDING_TTL_S = 7 * 86400       # a request not decided within 7 days is expired
APPROVED_TTL_S = 24 * 3600      # an approval, consumed or not, lasts 24 hours
PENDING_OP_TTL_S = 24 * 3600    # an op request not decided within 24 hours is expired

PENDING, APPROVED, DENIED = 'pending', 'approved', 'denied'
CONSUMED, EXPIRED = 'consumed', 'expired'
STATUSES = tuple(hubvalidate.STATUSES['approvals'])     # the single source
APPROVE, DENY = 'approve', 'deny'
DECISIONS = (APPROVE, DENY)
COLLECTION = 'approvals'
ROUTE = '/' + COLLECTION            # below api.BASE

MAX_FILES = 64
MAX_CONTENT_BYTES = 256 * 1024      # one file's content, UTF-8
MAX_PATH = 400
MAX_TITLE = 200
MAX_REASON = 4000
MAX_LINE = 4000                     # an op's run line

KIND_OP = 'op'
OP_ID = re.compile(r'D-[0-9]{3,}\Z')                     # kitlib.delegations.schema.ID
OP_REPO = re.compile(r'(?:[a-z0-9][a-z0-9-]*|\*)\Z')     # kitlib.delegations.schema.REPO
# One NAME=VALUE of a run line: kitlib.delegations.schema.ARG_NAME and ARG_CHARS
RUN_ARG = re.compile(r'(?:[a-z][a-z0-9_]{0,30})=(?:[A-Za-z0-9._:/\\-]{1,200})\Z')

# what check() and Verifier answer when the change is not the approved one
WHY_NO_KEY = 'no approval key'
WHY_UNKNOWN = 'no such approval'
WHY_BAD = 'the approval document is malformed'
WHY_UNSIGNED = 'the approval is not signed by the approval key'
WHY_STATUS = 'the approval is %s'               # % its status at that moment
WHY_REPO = 'the approval is for another repository'
WHY_HEAD = 'the approval is for another base commit'
WHY_FILES = 'the files differ from the approved ones'
WHY_COMMIT = 'the approval was not consumed by this commit'
WHY_SERVER = 'the hub server cannot say: %s'     # % the error's class
WHY_ANSWER = 'the hub server gave no verdict'
WHY_KIND_OP = 'the approval is for an operation, not a change'
WHY_KIND_FILES = 'the approval is for a change, not an operation'
WHY_OP = 'the approval is for another operation'
WHY_ARGS = 'the approval is for other arguments'

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HOST = re.compile(r'[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z')
_SCP = re.compile(r'(?:[^@/\s]+@)?([^:/\s]+):(?!//)(\S+)\Z')
_SCHEMES = {'https': 443, 'http': 80, 'ssh': 22, 'git': 9418}


class ApprovalError(Exception):
    """A request, decision or consume that cannot be made. `field` names what is wrong; `code`
    is the Api's error code it maps to (invalid or conflict)."""

    def __init__(self, message, field='', code='invalid'):
        Exception.__init__(self, message)
        self.message = message
        self.field = field
        self.code = code


def blob_id(content):
    """git's blob id (SHA-1 object format) of `content`: bytes as they are, a str as its UTF-8
    bytes, with no line-ending change."""
    data = content.encode('utf-8') if isinstance(content, str) else bytes(content)
    return hashlib.sha1(b'blob %d\x00' % len(data) + data).hexdigest()


def repo_id(url):
    """The identity of the repository at remote `url`: `host[:port]/path`, the host lower-cased,
    a default port, credentials, the scheme, a trailing `/` and `.git` dropped. An https, http,
    ssh or git URL and the scp form (`user@host:path`) of one repository give the same id.
    ValueError for anything else (a local path, file://, a URL with a query); its message never
    holds the URL, which may carry a password."""
    if not isinstance(url, str) or not url.strip() or any(c.isspace() for c in url.strip()):
        raise ValueError('the remote is not a URL')
    url = url.strip()
    if '://' in url:
        scheme, _, rest = url.partition('://')
        scheme = scheme.lower()
        if scheme not in _SCHEMES:
            raise ValueError('the remote is not an https, http, ssh or git URL')
        if '?' in rest or '#' in rest:
            raise ValueError('the remote URL has a query or a fragment')
        authority, slash, path = rest.partition('/')
        host = authority.rpartition('@')[2]
        host, colon, port = host.partition(':')
        if colon:
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError('the remote URL has a bad port')
            port = int(port)
        if not slash:
            raise ValueError('the remote URL names no repository')
    else:
        m = _SCP.match(url)
        if m is None or len(m.group(1)) < 2:        # one letter: a Windows drive, a local path
            raise ValueError('the remote is not a network URL')
        host, path, port, scheme = m.group(1), m.group(2), '', 'ssh'
    host = host.lower()
    if not _HOST.match(host):
        raise ValueError('the remote URL has a bad host')
    path = path.strip('/')
    if path.lower().endswith('.git'):
        path = path[:-4].rstrip('/')
    parts = path.split('/')
    if not path or any(p in ('', '.', '..') for p in parts):
        raise ValueError('the remote URL names no repository')
    where = host if port in ('', _SCHEMES[scheme]) else '%s:%d' % (host, port)
    return '%s/%s' % (where, '/'.join(parts))


def as_repo_id(value):
    """The repo_id of `value`: a repo_id (`host[:port]/path`, the host with a dot) read as an
    https URL's host and path, so a repo_id is kept as it is; anything else read by repo_id.
    ValueError as repo_id's."""
    if isinstance(value, str) and '://' not in value and '@' not in value \
            and '.' in value.partition('/')[0]:
        try:
            return repo_id('https://' + value)
        except ValueError:
            pass
    return repo_id(value)


def check_path(path):
    """Why `path` cannot be a file of an approval, or None: it must be relative to the repository
    top, with `/` between parts, none of them empty, `.`, `..` or `.git`, none ending in a dot or
    a space (Windows would read another name), no `\\`, `:` or control character."""
    if not isinstance(path, str) or not path:
        return 'a path is a non-empty string'
    if len(path) > MAX_PATH:
        return 'a path is at most %d characters' % MAX_PATH
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or '\\' in path or ':' in path:
        return 'a path has no backslash, colon or control character'
    for part in path.split('/'):
        if part in ('', '.', '..') or part.lower() == '.git':
            return 'a path is relative to the repository top, with no empty, ., .. or .git part'
        if part.endswith(('.', ' ')):
            return 'no part of a path ends in a dot or a space'
    return None


def _iso(now):
    return now.isoformat(timespec='seconds')


def _when(text):
    """An ISO time with its offset as an aware datetime, or None."""
    if not isinstance(text, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _text(value, field, limit, required):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise ApprovalError('%s is %stext of at most %d characters'
                            % (field, 'non-empty ' if required else '', limit), field)
    return value


def new_doc(repo, title, head, files, now, reason=None):
    """The pending document of a request (no id: the store gives it). `repo` is a remote URL or a
    repo_id; `files` is a list of {path, base_blob, content}: base_blob the file's blob at `head`
    (None: a new file), content its new text (None: a delete). Any `blob` a caller sends is
    ignored: blob_id(content) is computed here. `now` is an aware datetime. ApprovalError names
    the field that is wrong."""
    try:
        repo = as_repo_id(repo)
    except ValueError as e:
        raise ApprovalError(str(e), 'repo')
    title = _text(title, 'title', MAX_TITLE, True)
    reason = _text(reason, 'reason', MAX_REASON, False)
    if not isinstance(head, str) or not _HEX40.match(head):
        raise ApprovalError('head is a commit id of 40 lowercase hex digits', 'head')
    if not isinstance(files, list) or not files:
        raise ApprovalError('files is a non-empty list', 'files')
    if len(files) > MAX_FILES:
        raise ApprovalError('one approval holds at most %d files' % MAX_FILES, 'files')
    out, seen = [], set()
    for f in files:
        if not isinstance(f, dict):
            raise ApprovalError('each file is an object {path, base_blob, content}', 'files')
        path, base, content = f.get('path'), f.get('base_blob'), f.get('content')
        why = check_path(path)
        if why is not None:
            raise ApprovalError(why, 'files')
        if path.lower() in seen:            # Windows: one file, whatever the case
            raise ApprovalError('a path is named twice: %s' % path, 'files')
        seen.add(path.lower())
        if base is not None and (not isinstance(base, str) or not _HEX40.match(base)):
            raise ApprovalError('base_blob of %s is a blob id of 40 lowercase hex digits, or '
                                'null for a new file' % path, 'files')
        if content is not None:
            if not isinstance(content, str):
                raise ApprovalError('content of %s is text, or null for a delete' % path, 'files')
            try:
                size = len(content.encode('utf-8'))
            except UnicodeEncodeError:
                raise ApprovalError('content of %s is not valid text' % path, 'files')
            if size > MAX_CONTENT_BYTES:
                raise ApprovalError('content of %s is over %d bytes' % (path, MAX_CONTENT_BYTES),
                                    'files')
        elif base is None:
            raise ApprovalError('%s is neither in the base commit nor given content' % path,
                                'files')
        out.append({'path': path, 'base_blob': base,
                    'blob': None if content is None else blob_id(content), 'content': content})
    out.sort(key=lambda f: f['path'])
    doc = {'status': PENDING, 'repo': repo, 'title': title, 'head': head, 'files': out,
           'created_at': _iso(now)}
    if reason is not None:
        doc['reason'] = reason
    return doc


def line_sha256(line):
    """The hex SHA-256 of the run line `line` (text), as its UTF-8 bytes."""
    return hashlib.sha256(line.encode('utf-8')).hexdigest()


def _is_op(doc):
    """True for an op document, False for a file one (no kind); ValueError for another kind."""
    kind = doc.get('kind') if isinstance(doc, dict) else None
    if kind is None:
        return False
    if kind == KIND_OP:
        return True
    raise ValueError('the approval is of no known kind')


def new_op_doc(op, repo, line, title, now, reason=None, what=None):
    """The pending document of a request to run the delegated operation `op` (D-nnn) of `repo`
    (its repo name or `*`) once, as the run line `line`: `run D-nnn` and `name=value` for each
    argument, sorted by name, as kitlib.delegations.run.run_line writes it. No id: the
    store gives it. `now` is an aware datetime. args_sha256 is computed here. ApprovalError names
    the field that is wrong."""
    if not isinstance(op, str) or not OP_ID.match(op):
        raise ApprovalError('op is a delegation id D-nnn', 'op')
    if not isinstance(repo, str) or not OP_REPO.match(repo):
        raise ApprovalError("repo is the delegation's repo name or *", 'repo')
    line = _text(line, 'line', MAX_LINE, True)
    words = line.split(' ')
    names = [w.split('=', 1)[0] for w in words[2:]]
    if (words[:2] != ['run', op] or not all(RUN_ARG.match(w) for w in words[2:])
            or names != sorted(set(names))):
        raise ApprovalError('line is the run line `run %s NAME=VALUE ...`, one space apart, '
                            'each name once and sorted' % op, 'line')
    title = _text(title, 'title', MAX_TITLE, True)
    reason = _text(reason, 'reason', MAX_REASON, False)
    what = _text(what, 'what', MAX_REASON, False)
    doc = {'status': PENDING, 'kind': KIND_OP, 'op': op, 'repo': repo, 'title': title,
           'line': line, 'args_sha256': line_sha256(line), 'created_at': _iso(now)}
    if what is not None:
        doc['what'] = what
    if reason is not None:
        doc['reason'] = reason
    return doc


def canonical(doc_id, doc):
    """The bytes sign() covers: JSON of the id, repo, head, each file's [path, base_blob, blob]
    sorted by path, the decision and decided_at, with sorted keys and no spaces; for an op
    document, of the id, kind, op, repo, args_sha256, the decision and decided_at. ValueError
    for a document that has not their shape, that names a path twice or is of no known kind."""
    if _is_op(doc):
        if not all(isinstance(doc.get(k), str) for k in ('op', 'repo', 'args_sha256')):
            raise ValueError('the op approval has no op, repo or args_sha256')
        body = {'id': doc_id, 'kind': KIND_OP, 'op': doc['op'], 'repo': doc['repo'],
                'args_sha256': doc['args_sha256'], 'decision': doc.get('decision'),
                'decided_at': doc.get('decided_at')}
        return json.dumps(body, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True).encode('ascii')
    files = doc.get('files') if isinstance(doc, dict) else None
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise ValueError('the approval has no files list')
    triples = sorted(([f.get('path'), f.get('base_blob'), f.get('blob')] for f in files
                      if isinstance(f.get('path'), str)), key=lambda t: t[0])
    if len(triples) != len(files):
        raise ValueError('a file of the approval has no path')
    if len({t[0] for t in triples}) != len(triples):
        raise ValueError('a path of the approval is named twice')
    body = {'id': doc_id, 'repo': doc.get('repo'), 'head': doc.get('head'), 'files': triples,
            'decision': doc.get('decision'), 'decided_at': doc.get('decided_at')}
    return json.dumps(body, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True).encode('ascii')


def sign(key, doc_id, doc):
    """The signature of the decision in `doc`: HMAC with SHA-256 of canonical() under the
    approval key `key` (auth.read_approval_key), as 64 lowercase hex digits."""
    return hmac.new(key.encode('ascii'), canonical(doc_id, doc), hashlib.sha256).hexdigest()


def expires_at(doc):
    """When `doc` stops counting: created_at + PENDING_TTL_S (PENDING_OP_TTL_S for an op
    document) while pending, decided_at + APPROVED_TTL_S when approved or consumed; None when it
    never expires (denied, expired) or a time cannot be read (status_at then counts it
    expired)."""
    status = doc.get('status') if isinstance(doc, dict) else None
    if status == PENDING:
        t = _when(doc.get('created_at'))
        life = PENDING_OP_TTL_S if doc.get('kind') == KIND_OP else PENDING_TTL_S
        return None if t is None else t + datetime.timedelta(seconds=life)
    if status in (APPROVED, CONSUMED):
        t = _when(doc.get('decided_at'))
        return None if t is None else t + datetime.timedelta(seconds=APPROVED_TTL_S)
    return None


def status_at(doc, now):
    """The status of `doc` at `now` (an aware datetime): EXPIRED for a pending, approved or
    consumed one past expires_at or with a time that cannot be read, for an approved or consumed
    one decided after `now`, and for any of them when `now` is naive; its stored status
    otherwise; None for a document with no known status."""
    status = doc.get('status') if isinstance(doc, dict) else None
    if status not in STATUSES:
        return None
    if status in (PENDING, APPROVED, CONSUMED):
        end = expires_at(doc)
        try:
            if end is None or now >= end:
                return EXPIRED
            if status != PENDING and now < _when(doc.get('decided_at')):
                return EXPIRED
        except TypeError:           # a naive `now` cannot be compared: no time, no approval
            return EXPIRED
    return status


def decide_fields(key, doc_id, doc, decision, now):
    """The fields the owner's decision writes on `doc`: status, decision, decided_at and, for
    APPROVE, sig. Only a pending document that has not expired is decided; an approval needs the
    approval key. ApprovalError (code conflict for a document no longer pending)."""
    if decision not in DECISIONS:
        raise ApprovalError('decision is approve or deny', 'decision')
    status = status_at(doc, now)
    if status is None:
        raise ApprovalError(WHY_BAD, 'status', 'conflict')
    if status != PENDING:
        raise ApprovalError('only a pending approval is decided; this one is %s' % status,
                            'status', 'conflict')
    fields = {'status': APPROVED if decision == APPROVE else DENIED, 'decision': decision,
              'decided_at': _iso(now)}
    if decision == APPROVE:
        if doc.get('kind') == KIND_OP and not _line_holds(doc):
            raise ApprovalError(WHY_BAD, 'line', 'conflict')
        if not key:
            raise ApprovalError('there is no approval key to sign with', 'decision', 'conflict')
        signed = dict(doc, **fields)
        try:
            fields['sig'] = sign(key, doc_id, signed)
        except ValueError:
            raise ApprovalError(WHY_BAD, 'files', 'conflict')
    return fields


def withdraw_check(doc, now):
    """Only a pending request that has not expired is withdrawn by its requester: ApprovalError
    (code conflict, field status) for any other status, the owner's record by then."""
    status = status_at(doc, now)
    if status != PENDING:
        raise ApprovalError('only a pending approval can be withdrawn, not one that is %s'
                            % (status or 'malformed'), 'status', 'conflict')


def _line_holds(doc):
    """True when the op document `doc` holds a line that hashes to its args_sha256: the line the
    owner reads is the one the signature binds."""
    line = doc.get('line')
    return isinstance(line, str) and line_sha256(line) == doc.get('args_sha256')


def _signed(key, doc_id, doc):
    """'' when `doc` is an approval signed under `key`, else the WHY_ phrase of why not."""
    if not key:
        return WHY_NO_KEY
    if not isinstance(doc, dict):
        return WHY_UNKNOWN
    sig = doc.get('sig')
    if doc.get('decision') != APPROVE or not isinstance(sig, str):
        return WHY_UNSIGNED
    try:
        good = hmac.compare_digest(sign(key, doc_id, doc).encode('ascii'),
                                   sig.encode('ascii', 'replace'))
    except ValueError:
        return WHY_BAD
    return '' if good else WHY_UNSIGNED


def consume_fields(key, doc_id, doc, commit, now):
    """The fields consuming `doc` for `commit` writes: status, commit and consumed_at. Only an
    approval signed under `key` that is approved now and was never consumed is consumed (the
    status is not signed, so a stored commit counts too). ApprovalError (code conflict for one
    that cannot be consumed now)."""
    if not isinstance(commit, str) or not _HEX40.match(commit):
        raise ApprovalError('commit is a commit id of 40 lowercase hex digits', 'commit')
    why = _signed(key, doc_id, doc)
    if why:
        raise ApprovalError(why, 'sig', 'conflict')
    if doc.get('kind') is not None:      # signed, so of a known kind: an op
        raise ApprovalError(WHY_KIND_OP, 'kind', 'conflict')
    if doc.get('commit'):
        raise ApprovalError('the approval was consumed already by %s' % doc['commit'],
                            'commit', 'conflict')
    status = status_at(doc, now)
    if status != APPROVED:
        raise ApprovalError('only an approved approval is consumed; this one is %s'
                            % (status or 'malformed'), 'status', 'conflict')
    return {'status': CONSUMED, 'commit': commit, 'consumed_at': _iso(now)}


def check(key, doc_id, doc, repo, head, blobs, now, commit=None):
    """(True, '') when the change of `repo` (a remote URL or a repo_id) at base commit `head`
    with the files `blobs` ({path: blob, None for a delete}) is the one `doc` approved and the
    approval still counts at `now`; else (False, one of the WHY_ phrases). Given a `commit`, the
    approval must also be consumed, and by that commit (WHY_COMMIT). A document is trusted only
    once its signature checks under `key`, before any of its fields is compared."""
    why = _signed(key, doc_id, doc)
    if why:
        return False, why
    if doc.get('kind') is not None:      # signed, so of a known kind: an op
        return False, WHY_KIND_OP
    status = status_at(doc, now)
    if status not in (APPROVED, CONSUMED):
        return False, WHY_STATUS % (status or 'malformed')
    if commit is not None and (status != CONSUMED or doc.get('commit') != commit):
        return False, WHY_COMMIT
    try:
        if as_repo_id(repo) != doc.get('repo'):
            return False, WHY_REPO
    except ValueError:
        return False, WHY_REPO
    if not isinstance(head, str) or head != doc.get('head'):
        return False, WHY_HEAD
    approved = {f['path']: f.get('blob') for f in doc['files']}
    if not isinstance(blobs, dict) or blobs != approved:
        return False, WHY_FILES
    return True, ''


def check_op(key, doc_id, doc, op, repo, args_sha256, now):
    """(True, '') when `doc` is an op approval signed under `key`, approved and never consumed at
    `now`, for the operation `op` of `repo` run as the line whose line_sha256 is `args_sha256`;
    else (False, one of the WHY_ phrases). The signature is checked before any field is
    compared."""
    why = _signed(key, doc_id, doc)
    if why:
        return False, why
    if doc.get('kind') != KIND_OP:
        return False, WHY_KIND_FILES
    if doc.get('consumed_at') is not None:
        return False, WHY_STATUS % CONSUMED
    status = status_at(doc, now)
    if status != APPROVED:
        return False, WHY_STATUS % (status or 'malformed')
    if not _line_holds(doc):
        return False, WHY_BAD
    if op != doc['op']:
        return False, WHY_OP
    if repo != doc['repo']:
        return False, WHY_REPO
    if not isinstance(args_sha256, str) or args_sha256 != doc['args_sha256']:
        return False, WHY_ARGS
    return True, ''


def consume_op_fields(key, doc_id, doc, args_sha256, now):
    """The fields using the op approval `doc` once writes: status and consumed_at. Only an op
    approval signed under `key`, approved now, never consumed and for the arguments
    `args_sha256` is consumed. ApprovalError (code conflict for one that cannot be consumed
    now)."""
    if not isinstance(args_sha256, str) or not _HEX64.match(args_sha256):
        raise ApprovalError('args_sha256 is 64 lowercase hex digits', 'args_sha256')
    why = _signed(key, doc_id, doc)
    if why:
        raise ApprovalError(why, 'sig', 'conflict')
    if doc.get('kind') != KIND_OP:
        raise ApprovalError(WHY_KIND_FILES, 'kind', 'conflict')
    if doc.get('consumed_at') is not None:
        raise ApprovalError('the approval was consumed already at %s' % doc['consumed_at'],
                            'status', 'conflict')
    status = status_at(doc, now)
    if status != APPROVED:
        raise ApprovalError('only an approved approval is consumed; this one is %s'
                            % (status or 'malformed'), 'status', 'conflict')
    if args_sha256 != doc.get('args_sha256'):
        raise ApprovalError(WHY_ARGS, 'args_sha256', 'conflict')
    if not _line_holds(doc):
        raise ApprovalError(WHY_BAD, 'line', 'conflict')
    return {'status': CONSUMED, 'consumed_at': _iso(now)}


def remote_id(checkout, remote='origin', run=None):
    """(repo_id, None) of `remote` of the git checkout `checkout`, or (None, why). `run` is
    kitlib.git.cmd.run_text or a stand-in. The why never holds the URL."""
    text, why = (run or cmd.run_text)(['remote', 'get-url', remote], checkout)
    if text is None:
        return None, why or 'git could not read the remote'
    try:
        return repo_id(text), None
    except ValueError as e:
        return None, str(e)


def read_base(checkout, head, path, base_blob, cat=None):
    """(text, None) of the file `path` at commit `head` in the git checkout `checkout` (the
    owner's choice: the server reads the base with git for the page's diff), or (None, why). The
    file must be the blob `base_blob` (40 hex) and UTF-8; with base_blob None (a new file) it must
    be absent at head, and the answer is (None, None). `head` must be a commit of the checkout.
    One `git cat-file --batch`: `cat` is kitlib.git.batch.cat_file or a stand-in."""
    if not isinstance(head, str) or not _HEX40.match(head):
        return None, 'not a commit id'
    if check_path(path) is not None:
        return None, 'not a path that can be approved'
    if base_blob is not None and (not isinstance(base_blob, str) or not _HEX40.match(base_blob)):
        return None, 'not a blob id'
    commit, spec = head + '^{commit}', head + ':' + path
    got, why = (cat or batch.cat_file)([commit, spec], checkout)
    if got is None:
        return None, why or 'git could not read the base'
    if got.get(commit) is None:
        return None, 'head is not a commit in the checkout'
    obj = got.get(spec)
    if base_blob is None:
        return (None, None) if obj is None else (None, 'the file exists at head')
    if obj is None:
        return None, 'the file is not at head'
    if obj[0] != 'blob' or blob_id(obj[1]) != base_blob:
        return None, 'the base differs from the file at head'
    try:
        return obj[1].decode('utf-8'), None
    except UnicodeDecodeError:
        return None, 'the base is not text'


def diff(path, base, content):
    """The unified diff lines of one file, from `base` (None: a new file) to `content` (None: a
    delete), named a/<path> and b/<path> (/dev/null for the side that is absent)."""
    old = [] if base is None else base.splitlines(keepends=True)
    new = [] if content is None else content.splitlines(keepends=True)
    return list(difflib.unified_diff(old, new, '/dev/null' if base is None else 'a/' + path,
                                     '/dev/null' if content is None else 'b/' + path))


_NOREPLACE = ['--no-replace-objects']     # replace refs do not travel: judge the real objects


def _entries(checkout, rev, paths, call):
    """({path: (mode, type, object id)} of `paths` at the commit `rev`, a path with no entry left
    out, None), or (None, why). One `git ls-tree` without -r: a path that is a folder shows as its
    tree entry."""
    r = call(_NOREPLACE + ['ls-tree', '-z', '--full-tree', rev, '--'] + list(paths), checkout)
    if r.rc != 0:
        return None, 'git ls-tree %s failed: %s' % (rev[:12], r.err or 'exit %s' % r.rc)
    wanted, found = set(paths), {}
    for row in cmd.text(r.out).split('\0'):
        meta, tab, name = row.partition('\t')
        if tab and name in wanted:
            found[name] = tuple(meta.split())
    return found, None


def same_content_error(checkout, base, parent, paths, call=None):
    """None when a change made on the commit `parent` of the git checkout `checkout` is made on
    the same content as on the approval's base commit `base`, for every one of `paths`; else why
    not (the same-content rule, module docstring). `base` == `parent` is None at once. Otherwise
    `base` must be an ancestor of `parent`, and each path must have the same tree entry (mode,
    type, object id) at both, or none at either; the first that differs is named. Any git failure
    is a why: it fails closed. `call` is kitlib.git.cmd.call or a stand-in."""
    if isinstance(base, str) and base == parent:
        return None
    if not (isinstance(base, str) and _HEX40.match(base) and isinstance(parent, str)
            and _HEX40.match(parent)):
        return 'the base and the commit the change is made on are not both commit ids'
    paths = sorted(set(paths))
    if not paths:
        return 'the change touches no path'
    call = call or cmd.call
    r = call(_NOREPLACE + ['merge-base', '--is-ancestor', base, parent], checkout)
    if r.rc == 1:
        return 'the approved base %s is not an ancestor of %s' % (base[:12], parent[:12])
    if r.rc != 0:
        return 'git merge-base failed: %s' % (r.err or 'exit %s' % r.rc)
    at_base, why = _entries(checkout, base, paths, call)
    if at_base is None:
        return why
    at_parent, why = _entries(checkout, parent, paths, call)
    if at_parent is None:
        return why
    for p in paths:
        if at_base.get(p) != at_parent.get(p):
            return '%s at %s is not as the approved base %s has it' % (p, parent[:12], base[:12])
    return None


class Verifier(object):
    """Asks the hub server whether a change is the approved one. `client` is a
    kitlib.hubsite.client.Client (or a stand-in with its get()); the call is the agent's.
    verify() fails closed: any error of the call (NotRunning, Refused, ServeError, anything
    else) or an answer without a verdict is (False, why); verify_on() too."""

    def __init__(self, client):
        self.client = client

    def approved_head(self, doc_id):
        """The `head` the hub's snapshot gives the approval `doc_id` (40 hex), or None on any
        failure. Not trusted as it stands: verify() for it checks it against the signature."""
        try:
            got = self.client.get('/snapshot')
        except Exception:           # noqa: BLE001 - no answer is no base
            return None
        colls = got.get('collections') if isinstance(got, dict) else None
        docs = colls.get('approvals') if isinstance(colls, dict) else None
        doc = docs.get(doc_id) if isinstance(docs, dict) else None
        head = doc.get('head') if isinstance(doc, dict) else None
        return head if isinstance(head, str) and _HEX40.match(head) else None

    def verify_on(self, checkout, doc_id, repo, parent, blobs, commit=None, base=None):
        """verify() for a change made on the commit `parent` of `checkout`: `blobs` and `commit`
        as verify() takes them. `base`: the approval's base as the caller read it, or None to ask
        the server for `parent` first and, only when it answers WHY_HEAD, the snapshot for the
        base. When the base is not `parent`, same_content_error must be None for the paths of
        `blobs` (else (False, WHY_HEAD and why)), and the server must verify the change for the
        base: its signature binds the base, so a wrong one never verifies."""
        if base is None:
            ok, why = self.verify(doc_id, repo, parent, blobs, commit=commit)
            if ok or why != WHY_HEAD:
                return ok, why
            base = self.approved_head(doc_id)
            if base is None or base == parent:
                return False, WHY_HEAD
        if base != parent:
            why = same_content_error(checkout, base, parent,
                                     blobs if isinstance(blobs, dict) else ())
            if why:
                return False, '%s: %s' % (WHY_HEAD, why)
        return self.verify(doc_id, repo, base, blobs, commit=commit)

    def verify(self, doc_id, repo, head, blobs, commit=None):
        """(True, '') only when the server answers valid true for this change (and, given a
        `commit`, the approval was consumed by it: check's commit); else (False, why)."""
        if commit is not None and not commit:   # the client drops an empty value: unbound
            return False, WHY_COMMIT
        try:
            files = json.dumps(blobs, sort_keys=True, separators=(',', ':'))
            query = {'repo': repo, 'head': head, 'files': files}
            if commit is not None:
                query['commit'] = commit
            got = self.client.get('%s/%s/verify' % (ROUTE, _part(doc_id)), AGENT, query)
        except Exception as e:      # fail closed: no answer is no approval
            return False, WHY_SERVER % type(e).__name__
        if not isinstance(got, dict) or got.get('valid') is not True:
            why = got.get('why') if isinstance(got, dict) else None
            return False, why if isinstance(why, str) and why else WHY_ANSWER
        return True, ''


def request(client, repo, title, head, files, reason=None):
    """Add a request for the owner (POST ROUTE, the agent's) and answer the server's reply; the
    client's errors propagate. `files` as new_doc takes them."""
    body = {'repo': repo, 'title': title, 'head': head, 'files': files}
    if reason is not None:
        body['reason'] = reason
    return client.post(ROUTE, AGENT, body)


def consume(client, doc_id, commit, if_version):
    """Mark the approval `doc_id` consumed by `commit` (POST ROUTE/<id>/consume, the agent's),
    pinned to `if_version`; the client's errors propagate."""
    return client.post('%s/%s/consume' % (ROUTE, _part(doc_id)), AGENT,
                       {'commit': commit, 'if_version': if_version})


def request_op(client, op, repo, line, title, reason=None, what=None):
    """Add a request to run `op` once as `line` (POST ROUTE with kind op, the agent's) and answer
    the server's reply; the client's errors propagate."""
    body = {'kind': KIND_OP, 'op': op, 'repo': repo, 'line': line, 'title': title}
    if reason is not None:
        body['reason'] = reason
    if what is not None:
        body['what'] = what
    return client.post(ROUTE, AGENT, body)


def verify_op(client, doc_id, op, repo, args_sha256):
    """(True, '', version) only when the server answers valid true for this run of `op` and
    gives the document's version (a whole number); else (False, why, None). Fails closed as
    Verifier does."""
    try:
        got = client.get('%s/%s/verify' % (ROUTE, _part(doc_id)), AGENT,
                         {'kind': KIND_OP, 'op': op, 'repo': repo, 'args': args_sha256})
    except Exception as e:      # fail closed: no answer is no approval
        return False, WHY_SERVER % type(e).__name__, None
    if not isinstance(got, dict) or got.get('valid') is not True:
        why = got.get('why') if isinstance(got, dict) else None
        return False, why if isinstance(why, str) and why else WHY_ANSWER, None
    version = got.get('version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        return False, WHY_ANSWER, None
    return True, '', version


def consume_op(client, doc_id, args_sha256, if_version):
    """Mark the op approval `doc_id` used (POST ROUTE/<id>/consume, the agent's), pinned to
    `if_version`; the client's errors propagate."""
    return client.post('%s/%s/consume' % (ROUTE, _part(doc_id)), AGENT,
                       {'args_sha256': args_sha256, 'if_version': if_version})
