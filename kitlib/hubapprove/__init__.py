"""An agent's request for the owner's approval of a change, built from a git checkout
(docs/action-hub.md "approvals/A-NNN"): what `tools/hub.py approvals add` and the hub MCP tool
hub_approval_request post.

build() reads each proposed file from a content file of its own, byte for byte (UTF-8, no
line-ending change), so its blob is the one git stores when tools/commit.py --owner-approval writes
it. The file's base is its blob at `head` in the checkout, or none when the file is absent there (a
new file): what the server's read_base and the consumer's prewrite check expect. The work tree is
never read for the proposed content: the consumer needs it still at the base.

Refused here, with nothing posted (RequestError): a checkout that is not a git work tree, a repo
the checkout's origin cannot name, a head that is not 40 hex digits or not a commit of the
checkout, a path that is not a plain path of the repository, a path that is not an owner approval's
to carry (owner_paths_error: a render source in a kit checkout, which tools/commit.py
--owner-approval refuses, or a path the checkout's owner rules do not lock, `.kit.json`
"owner".paths as kitlib.commit reads them), a path at head that is not a file, a
new file the work tree already has, a file at head whose work tree copy git does not hash to its
base (the consumer's prewrite check would refuse either), a content file that cannot be read, is
not UTF-8 or is over the size cap, a file whose content is its base (the consumer refuses a change
that changes nothing), content git would store as another blob (`hash-object --path=<path>
--stdin`: core.autocrlf or a text, eol or filter attribute changes it on staging, and the
consumer's staged check would refuse it), and whatever
kitlib.hubsite.approvals.new_doc refuses, in its words (its limits: MAX_FILES, MAX_CONTENT_BYTES,
MAX_PATH, MAX_TITLE, MAX_REASON). A delete cannot be requested: every file has content.

request() only posts the request (POST /approvals); it never decides or consumes one. The
client's errors propagate as they are. No argparse, no printing, no exits (AGENTS.md).
"""
import datetime
import os
import re

from kitlib import config as kconfig
from kitlib.content import oncommit
from kitlib.git import batch, cmd
from kitlib.hubsite import approvals as ap

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')


class RequestError(Exception):
    """A request that is not posted. `field` names the argument that is wrong."""

    def __init__(self, message, field=None):
        Exception.__init__(self, message)
        self.message = message
        self.field = field


def read_content(path):
    """The text of the content file `path`: its bytes as UTF-8, line endings kept. RequestError
    when it cannot be read, is over ap.MAX_CONTENT_BYTES or is not UTF-8."""
    try:
        with open(path, 'rb') as f:
            data = f.read(ap.MAX_CONTENT_BYTES + 1)     # one past the cap: a huge file is not read
    except OSError as e:
        raise RequestError('cannot read the content file %s: %s' % (path, type(e).__name__),
                           'files')
    if len(data) > ap.MAX_CONTENT_BYTES:
        raise RequestError('the content file %s is over %d bytes'
                           % (path, ap.MAX_CONTENT_BYTES), 'files')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise RequestError('the content file %s is not UTF-8 text' % path, 'files')


def owner_paths_error(top, paths):
    """None when every one of `paths` (repo-relative, `/`) is one an owner approval may carry,
    else the refusal naming the others: a render source when `top` is a kit checkout (the test
    kitlib.commit._approval_first applies: oncommit.wants_render and oncommit.is_kit), and a path
    the owner rules of `top`'s `.kit.json` (kconfig.load(top).owner_rules(), as kitlib.commit
    reads them) do not lock. RequestError when the configuration is refused."""
    try:
        rules = kconfig.load(top).owner_rules()
    except kconfig.ConfigError as e:
        raise RequestError('the kit configuration of %s is refused: %s' % (top, e), 'checkout')
    kit = oncommit.is_kit(top)
    rendered = [p for p in paths if kit and oncommit.wants_render([p])]
    ordinary = [p for p in paths if p not in rendered and not rules.is_locked(p)]
    if not rendered and not ordinary:
        return None
    named = []
    if rendered:
        named.append('%s (a render source: tools/commit.py --owner-approval refuses it, since '
                     'its rendered files would be committed unapproved)' % ', '.join(rendered))
    if ordinary:
        named.append('%s (not owner-only: .kit.json "owner".paths does not lock it)'
                     % ', '.join(ordinary))
    return ('an owner approval carries owner-only files only, and this request names %s. Post '
            'the owner-only files alone in the request; commit the rest in an ordinary commit '
            '(tools/commit.py), before or after the approved one' % ' and '.join(named))


def build(checkout, title, files, repo=None, head=None, reason=None, now=None, run=None,
          cat=None):
    """The body of POST /approvals for the change `files`, a list of (path, content_file): path
    relative to the repository top with `/`, content_file the file holding its proposed text.
    `repo` defaults to the checkout's origin (ap.remote_id), `head` to its HEAD. `run` is
    kitlib.git.cmd.run_text and `cat` kitlib.git.batch.cat_file, or stand-ins. RequestError for
    anything refused (module docstring); nothing is posted."""
    run, cat = run or cmd.run_text, cat or batch.cat_file
    top, why = run(['rev-parse', '--show-toplevel'], checkout)
    if not top:
        raise RequestError('%s is not a git work tree: %s' % (checkout, why or 'no top'),
                           'checkout')
    if repo is None:
        repo, why = ap.remote_id(top, run=run)
        if repo is None:
            raise RequestError('the checkout has no origin remote to name the repository: %s'
                               % why, 'repo')
    if head is None:
        head, why = run(['rev-parse', '--verify', 'HEAD^{commit}'], top)
        if not head:
            raise RequestError('the checkout has no HEAD commit: %s' % (why or 'none'), 'head')
    if not isinstance(head, str) or not _HEX40.match(head):
        raise RequestError('head is a commit id of 40 lowercase hex digits', 'head')
    if not isinstance(files, list) or not files:
        raise RequestError('give at least one file', 'files')
    for path, _ in files:
        why = ap.check_path(path)
        if why is not None:
            raise RequestError('%s: %s' % (path, why), 'files')
    why = owner_paths_error(top, [path for path, _ in files])
    if why is not None:
        raise RequestError(why, 'files')
    commit = head + '^{commit}'
    specs = [commit] + [head + ':' + path for path, _ in files]
    got, why = cat(specs, top)
    if got is None:
        raise RequestError('git could not read head %s: %s' % (head, why or 'no answer'), 'head')
    if got.get(commit) is None:
        raise RequestError('head %s is not a commit in the checkout' % head, 'head')
    out = []
    for path, content_file in files:
        obj = got.get(head + ':' + path)
        if obj is not None and obj[0] != 'blob':
            raise RequestError('%s is a %s at head, not a file' % (path, obj[0]), 'files')
        base = None if obj is None else ap.blob_id(obj[1])
        full = os.path.join(top, *path.split('/'))
        if base is None and os.path.lexists(full):
            # the consumer refuses a new file the work tree already has (another case of a
            # tracked name on Windows, or an untracked file)
            raise RequestError('%s is not at head but is in the work tree' % path, 'files')
        if base is not None:
            # the consumer's prewrite check: the work tree's file, as git would store it, is
            # its base at head
            hashed = None
            if os.path.isfile(full) and not os.path.islink(full):
                hashed, why = run(['hash-object', '--', path], top)
            if hashed != base:
                raise RequestError('%s in the work tree is not the file at head %s; the '
                                   'consumer would refuse the approval. Put it back to head '
                                   'first.' % (path, head[:12]), 'files')
        content = read_content(content_file)
        blob = ap.blob_id(content)
        if blob == base:
            raise RequestError('%s: the content is the file at head; the request would change '
                               'nothing' % path, 'files')
        r = cmd.call(['hash-object', '--path=' + path, '--stdin'], top,
                     stdin=content.encode('utf-8'))
        staged = cmd.text(r.out).strip() if r.rc == 0 else None
        if staged != blob:
            # the consumer's staged check compares the blob git stages (its clean filter,
            # core.autocrlf, .gitattributes text or eol) with the approved one
            raise RequestError('%s: git would not store the content as it is (%s); a line-'
                               'ending or filter attribute changes it on staging, so the '
                               'commit would be refused. Give the content as git stores it.'
                               % (path, 'it would store %s' % staged[:12] if staged
                                  else r.err or 'hash-object failed'), 'files')
        out.append({'path': path, 'base_blob': base, 'content': content})
    body = {'repo': repo, 'title': title, 'head': head, 'files': out}
    if reason is not None:
        body['reason'] = reason
    try:
        ap.new_doc(repo, title, head, out, now or datetime.datetime.now(datetime.timezone.utc),
                   reason)
    except ap.ApprovalError as e:
        raise RequestError(e.message, e.field)
    return body


def request(client, body):
    """Post the request `body` (build's) for the owner (approvals.request, the agent's route) and
    answer the server's reply: {ok, id, version, ...}. The client's errors propagate."""
    return ap.request(client, body['repo'], body['title'], body['head'], body['files'],
                      body.get('reason'))
