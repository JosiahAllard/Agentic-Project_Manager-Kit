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
checkout, a path that is not a plain path of the repository, a path at head that is not a file, a
new file the work tree already has (the consumer would refuse it), a
content file that cannot be read, is not UTF-8 or is over the size cap, a file whose content is its
base (the consumer refuses a change that changes nothing), and whatever
kitlib.hubsite.approvals.new_doc refuses, in its words (its limits: MAX_FILES, MAX_CONTENT_BYTES,
MAX_PATH, MAX_TITLE, MAX_REASON). A delete cannot be requested: every file has content.

request() only posts the request (POST /approvals); it never decides or consumes one. The
client's errors propagate as they are. No argparse, no printing, no exits (AGENTS.md).
"""
import datetime
import os
import re

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
        if base is None and os.path.lexists(os.path.join(top, *path.split('/'))):
            # the consumer refuses a new file the work tree already has (another case of a
            # tracked name on Windows, or an untracked file)
            raise RequestError('%s is not at head but is in the work tree' % path, 'files')
        content = read_content(content_file)
        if ap.blob_id(content) == base:
            raise RequestError('%s: the content is the file at head; the request would change '
                               'nothing' % path, 'files')
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
