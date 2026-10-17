"""kitlib.hubsite.approvals: the owner's approval of an edit to an owner-only path.

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
  consume_fields(doc, commit, now)
                                  the fields a consume writes; ApprovalError
  check(key, doc_id, doc, repo, head, blobs, now)
                                  (True, '') when this change is the approved one, else
                                  (False, why): the server's verify, pure
  remote_id(checkout, remote='origin', run=None)
                                  (repo_id, None) of a checkout's remote, or (None, why)
  read_base(checkout, base_blob, run=None)
                                  (text, None) of a base blob, read with git, or (None, why)
  diff(path, base, content)       the unified diff lines the page shows for one file
  Verifier(client)                .verify(doc_id, repo, head, blobs) -> (ok, why), through the
                                  verify route; fails closed
  request(client, repo, title, head, files, reason=None) / consume(client, doc_id, commit,
  if_version)                     the agent's add and consume calls

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

The signature covers the id, repo, head, every file's (path, base_blob, blob), the decision and
decided_at (canonical): editing any of them, the expiry included, voids it. The content is left
out, as blob binds it. The status is not covered: only `approved` and `consumed` can verify, and
both need a signed approval.

Expiry is read, never written by a timer: a pending request expires PENDING_TTL_S after
created_at; an approval APPROVED_TTL_S after decided_at, consumed or not (the owner's choice:
a consumed approval verifies until it expires, so a land after the commit can still check it).
A time that cannot be read counts as expired. Denied and expired are final.

Routes: the helpers below call ROUTE (POST add), ROUTE/<id>/consume (POST) and
ROUTE/<id>/verify (GET, query repo, head and files as JSON {path: blob}; it answers
{"ok": true, "valid": bool, "why": str} and never the key or the sig). No printing, no exit.
"""
import datetime
import difflib
import hashlib
import hmac
import json
import re

from kitlib.git import cmd

from .api import AGENT
from .client import part as _part

# How long an approval counts, as the owner chose them.
PENDING_TTL_S = 7 * 86400       # a request not decided within 7 days is expired
APPROVED_TTL_S = 24 * 3600      # an approval, consumed or not, lasts 24 hours

PENDING, APPROVED, DENIED = 'pending', 'approved', 'denied'
CONSUMED, EXPIRED = 'consumed', 'expired'
STATUSES = (PENDING, APPROVED, DENIED, CONSUMED, EXPIRED)
APPROVE, DENY = 'approve', 'deny'
DECISIONS = (APPROVE, DENY)
COLLECTION = 'approvals'
ROUTE = '/' + COLLECTION            # below api.BASE

MAX_FILES = 64
MAX_CONTENT_BYTES = 256 * 1024      # one file's content, UTF-8
MAX_PATH = 400
MAX_TITLE = 200
MAX_REASON = 4000

# what check() and Verifier answer when the change is not the approved one
WHY_NO_KEY = 'no approval key'
WHY_UNKNOWN = 'no such approval'
WHY_BAD = 'the approval document is malformed'
WHY_UNSIGNED = 'the approval is not signed by the approval key'
WHY_STATUS = 'the approval is %s'               # % its status at that moment
WHY_REPO = 'the approval is for another repository'
WHY_HEAD = 'the approval is for another base commit'
WHY_FILES = 'the files differ from the approved ones'
WHY_SERVER = 'the hub server cannot say: %s'     # % the error's class
WHY_ANSWER = 'the hub server gave no verdict'

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
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


def canonical(doc_id, doc):
    """The bytes sign() covers: JSON of the id, repo, head, each file's [path, base_blob, blob]
    sorted by path, the decision and decided_at, with sorted keys and no spaces. ValueError for a
    document that has not their shape."""
    files = doc.get('files') if isinstance(doc, dict) else None
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise ValueError('the approval has no files list')
    triples = sorted([f.get('path'), f.get('base_blob'), f.get('blob')] for f in files
                     if isinstance(f.get('path'), str))
    if len(triples) != len(files):
        raise ValueError('a file of the approval has no path')
    body = {'id': doc_id, 'repo': doc.get('repo'), 'head': doc.get('head'), 'files': triples,
            'decision': doc.get('decision'), 'decided_at': doc.get('decided_at')}
    return json.dumps(body, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True).encode('ascii')


def sign(key, doc_id, doc):
    """The signature of the decision in `doc`: HMAC with SHA-256 of canonical() under the
    approval key `key` (auth.read_approval_key), as 64 lowercase hex digits."""
    return hmac.new(key.encode('ascii'), canonical(doc_id, doc), hashlib.sha256).hexdigest()


def expires_at(doc):
    """When `doc` stops counting: created_at + PENDING_TTL_S while pending, decided_at +
    APPROVED_TTL_S when approved or consumed; None when it never expires (denied, expired) or a
    time cannot be read (status_at then counts it expired)."""
    status = doc.get('status') if isinstance(doc, dict) else None
    if status == PENDING:
        t = _when(doc.get('created_at'))
        return None if t is None else t + datetime.timedelta(seconds=PENDING_TTL_S)
    if status in (APPROVED, CONSUMED):
        t = _when(doc.get('decided_at'))
        return None if t is None else t + datetime.timedelta(seconds=APPROVED_TTL_S)
    return None


def status_at(doc, now):
    """The status of `doc` at `now` (an aware datetime): EXPIRED for a pending, approved or
    consumed one past expires_at or with a time that cannot be read; its stored status
    otherwise; None for a document with no known status."""
    status = doc.get('status') if isinstance(doc, dict) else None
    if status not in STATUSES:
        return None
    if status in (PENDING, APPROVED, CONSUMED):
        end = expires_at(doc)
        if end is None or now >= end:
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
        if not key:
            raise ApprovalError('there is no approval key to sign with', 'decision', 'conflict')
        signed = dict(doc, **fields)
        try:
            fields['sig'] = sign(key, doc_id, signed)
        except ValueError:
            raise ApprovalError(WHY_BAD, 'files', 'conflict')
    return fields


def consume_fields(doc, commit, now):
    """The fields consuming `doc` for `commit` writes: status, commit and consumed_at. Only an
    approved document that has not expired is consumed, once. ApprovalError (code conflict for
    one that is not approved now)."""
    if not isinstance(commit, str) or not _HEX40.match(commit):
        raise ApprovalError('commit is a commit id of 40 lowercase hex digits', 'commit')
    status = status_at(doc, now)
    if status != APPROVED:
        raise ApprovalError('only an approved approval is consumed; this one is %s'
                            % (status or 'malformed'), 'status', 'conflict')
    return {'status': CONSUMED, 'commit': commit, 'consumed_at': _iso(now)}


def check(key, doc_id, doc, repo, head, blobs, now):
    """(True, '') when the change of `repo` (a remote URL or a repo_id) at base commit `head`
    with the files `blobs` ({path: blob, None for a delete}) is the one `doc` approved and the
    approval still counts at `now`; else (False, one of the WHY_ phrases). A document is trusted
    only once its signature checks under `key`, before any of its fields is compared."""
    if not key:
        return False, WHY_NO_KEY
    if not isinstance(doc, dict):
        return False, WHY_UNKNOWN
    sig = doc.get('sig')
    if doc.get('decision') != APPROVE or not isinstance(sig, str):
        return False, WHY_UNSIGNED
    try:
        good = hmac.compare_digest(sign(key, doc_id, doc).encode('ascii'),
                                   sig.encode('ascii', 'replace'))
    except ValueError:
        return False, WHY_BAD
    if not good:
        return False, WHY_UNSIGNED
    status = status_at(doc, now)
    if status not in (APPROVED, CONSUMED):
        return False, WHY_STATUS % (status or 'malformed')
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


def read_base(checkout, base_blob, run=None):
    """(text, None) of the blob `base_blob` in the git checkout `checkout` (the owner's choice:
    the server reads the base with git for the page's diff), or (None, why). The bytes read must
    hash to `base_blob` and be UTF-8. `run` is kitlib.git.cmd.run or a stand-in."""
    if not isinstance(base_blob, str) or not _HEX40.match(base_blob):
        return None, 'not a blob id'
    out, why = (run or cmd.run)(['cat-file', 'blob', base_blob], checkout)
    if out is None:
        return None, why or 'git could not read the blob'
    if blob_id(out) != base_blob:
        return None, 'git gave other bytes than the blob'
    try:
        return out.decode('utf-8'), None
    except UnicodeDecodeError:
        return None, 'the base is not text'


def diff(path, base, content):
    """The unified diff lines of one file, from `base` (None: a new file) to `content` (None: a
    delete), named a/<path> and b/<path> (/dev/null for the side that is absent)."""
    old = [] if base is None else base.splitlines(keepends=True)
    new = [] if content is None else content.splitlines(keepends=True)
    return list(difflib.unified_diff(old, new, '/dev/null' if base is None else 'a/' + path,
                                     '/dev/null' if content is None else 'b/' + path))


class Verifier(object):
    """Asks the hub server whether a change is the approved one. `client` is a
    kitlib.hubsite.client.Client (or a stand-in with its get()); the call is the agent's.
    verify() fails closed: any error of the call (NotRunning, Refused, ServeError, anything
    else) or an answer without a verdict is (False, why)."""

    def __init__(self, client):
        self.client = client

    def verify(self, doc_id, repo, head, blobs):
        """(True, '') only when the server answers valid true for this change; else (False,
        why)."""
        try:
            files = json.dumps(blobs, sort_keys=True, separators=(',', ':'))
            got = self.client.get('%s/%s/verify' % (ROUTE, _part(doc_id)), AGENT,
                                  {'repo': repo, 'head': head, 'files': files})
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
