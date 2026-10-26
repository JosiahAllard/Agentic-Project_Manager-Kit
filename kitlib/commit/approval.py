"""The owner's approval on the hub, for commit.py --owner-approval hub:A-<n> (docs/commit.md
"Owner approval"). kitlib.commit runs these steps; nothing here prints, exits or raises a refusal:
each answers why it refuses, or None.

  usage_error(req)                  why the flags do not go together, or None
  hub_client(given)                 (client, why): `given`, else the machine's hub
  fetch(client, doc_id)             (document, why) from the hub's snapshot
  state_error(doc_id, doc)          why the document cannot be committed now (consumed, not
                                    approved), or None
  prewrite_error(root, doc_id, doc, paths, head)
                                    why the change cannot be written: the named paths are not
                                    exactly the approval's, HEAD is not on the same content as
                                    its base, a content is not its signed blob, a file is
                                    unchanged, or a file in the work tree is not the approved base
                                    (or a path not a plain file of the work tree); or None
  preverify_error(root, client, doc_id, doc, head)
                                    why the hub does not verify the approval before the write
  write(root, doc) / restore(state) the approved content into the work tree, and back
  staged_error(doc, head, staged, snap, judged)
                                    why what was staged is not the approved blobs on the HEAD
                                    the steps before judged
  consume_commit(root, client, doc_id, value, sha)
                                    (line, why, consumed): consume the approval for the commit
                                    `sha`, then ask the hub to verify it for that commit

The same content: HEAD is the approval's base, or a descendant of it that holds every approved
path exactly as the base does (kitlib.hubsite.approvals.same_content_error, the owner's choice:
the approved paths only, compared). The base may have moved on with other work since the owner
approved; the change written on HEAD is still, path for path, the change the owner saw.

The hub is asked through kitlib.hubsite (approvals.Verifier.verify_on, for the approval's base,
and approvals.consume, the agent's calls); every failure to ask it is a refusal, never a pass.
The commit gate land and the pre-push hook judge is kitlib.guard.ownercommits.violations: the
approved commit has one parent (the approval's base, or a descendant of it with the same
content), changes exactly the approval's files to their approved blobs, and is consumed by the
hub for that commit.
"""
import os

from kitlib.git import cmd
from kitlib.guard import ownercommits as oc

TRAILER = oc.TRAILER
VALUE = 'hub:A-<n>'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def doc_id(value):
    """The approval id of an --owner-approval value, exactly as the hub issues it (A-001), or
    None (oc.APPROVAL_RE)."""
    m = oc.APPROVAL_RE.match(value) if isinstance(value, str) else None
    return m.group(1) if m else None


def trailer(value):
    return '%s: %s' % (TRAILER, value)


def base_route(root, did):
    """The sentence naming the way to commit the approval `did` when HEAD is not on its base's
    content: a worktree made at that base (docs/new-worktree.md "A base commit"), or a new
    approval."""
    return ('Commit it from a worktree at that base: python %s -C %s <area>/<slug> --approval '
            'hub:%s (docs/new-worktree.md "A base commit"), then merge that branch into this one; '
            'or ask the owner again from this HEAD.'
            % (os.path.join(KIT_ROOT, 'tools', 'new_worktree.py'), root, did))


def retry_command(tool, value, sha):
    """The command line (after `python`) that consumes `value` for the commit `sha`."""
    return '%s --owner-approval %s --consume %s' % (tool, value, sha)


def consume_refusal(value, sha, why, consumed):
    """The `refused:` line for a consume_commit of `value` for the commit `sha` that failed:
    `consumed` says whether the hub did consume it (consume_commit)."""
    if consumed:
        return ('refused: %s was consumed by %s, and the hub does not verify it for that commit: '
                '%s.' % (value, sha[:12], why))
    return 'refused: %s was not consumed: %s.' % (value, why)


def usage_error(req):
    """Why --owner-approval / --consume do not go with the rest of `req`, or None."""
    if req.consume is not None and req.owner_approval is None:
        return '--consume needs --owner-approval %s: the approval it consumes' % VALUE
    if req.owner_approval is None:
        return None
    if not doc_id(req.owner_approval):
        return ('--owner-approval takes %s, the approval\'s id as the hub shows it, as %s '
                '(not %r)' % (VALUE, oc.EXAMPLE, req.owner_approval))
    if req.owner_authored:
        return ('--owner-approval and --owner-authored do not go together: the owner commits a '
                'change of their own with --owner-authored, an agent the approved one with '
                '--owner-approval')
    if req.owner_approved:
        return ('--owner-approval and --owner-approved do not go together: --owner-approved is '
                'for the owner\'s own commit')
    if req.consume is not None:
        if req.paths:
            return '--consume takes no paths: it consumes the approval for a commit already made'
        if req.subject or req.b or req.body_file:
            return '--consume takes no -m, -b or --body-file: the commit is made already'
    return None


def _err(e):
    text = str(e)
    return '%s: %s' % (type(e).__name__, text) if text else type(e).__name__


def hub_client(given):
    """(client, None), or (None, why): `given` when not None, else a client of the machine's hub
    (kitlib.hubsite.settings)."""
    if given is not None:
        return given, None
    try:
        from kitlib.hubsite import client as hubclient, settings
        return hubclient.Client(settings.load().port), None
    except Exception as e:          # noqa: BLE001 - no client is no approval
        return None, 'the hub could not be asked (%s)' % _err(e)


def fetch(client, did):
    """(the document of the approval `did`, None) from the hub's snapshot, or (None, why)."""
    try:
        got = client.get('/snapshot')
    except Exception as e:          # noqa: BLE001 - no answer is no approval
        return None, 'the hub could not be asked for hub:%s (%s)' % (did, _err(e))
    colls = got.get('collections') if isinstance(got, dict) else None
    docs = colls.get('approvals') if isinstance(colls, dict) else None
    if not isinstance(docs, dict):
        return None, 'the hub answered without its approvals'
    doc = docs.get(did)
    if doc is None:
        return None, 'the hub has no approval %s' % did
    files = doc.get('files') if isinstance(doc, dict) else None
    if not (isinstance(files, list) and files and all(
            isinstance(f, dict) and isinstance(f.get('path'), str) for f in files)
            and isinstance(doc.get('head'), str)):
        return None, 'hub:%s is malformed' % did
    return doc, None


def state_error(did, doc):
    if doc.get('commit'):
        return ('hub:%s was consumed already by %s: one approval is one commit; ask the owner '
                'again' % (did, str(doc['commit'])[:12]))
    status = doc.get('status')
    if status != 'approved':
        return 'hub:%s is %s: only an approved approval is committed' % (did, status or
                                                                        'malformed')
    return None


def approved_paths(doc):
    return sorted(f['path'] for f in doc['files'])


def _hash_object(root, path):
    r = cmd.call(['hash-object', '--', path], root)
    return cmd.text(r.out).strip() if r.rc == 0 else None


def _path_error(root, rel):
    """Why `rel` is not a plain file path of the work tree `root` to write, or None: no `..`, no
    absolute path, nothing in a `.git` folder, and no symbolic link or junction on the way."""
    parts = rel.split('/')
    if (not rel or rel.startswith('/') or '\\' in rel or ':' in rel
            or any(p in ('', '.', '..') or p.lower() == '.git' for p in parts)):
        return '%s is not a plain path in the work tree' % rel
    top = os.path.realpath(root)
    full = os.path.join(root, *parts)
    if os.path.islink(full):
        return '%s is a symbolic link: an approval writes plain files only' % rel
    parent = os.path.dirname(full)
    while not os.path.lexists(parent):
        parent = os.path.dirname(parent)
    real = os.path.realpath(parent)
    if os.path.normcase(real) != os.path.normcase(top) and not os.path.normcase(real).startswith(
            os.path.normcase(top) + os.sep):
        return '%s is reached through a link out of the work tree' % rel
    return None


def prewrite_error(root, did, doc, paths, head):
    """Why the approved change cannot be written into `root` for a commit of `paths` on `head`,
    or None (module docstring)."""
    from kitlib.hubsite import approvals as ap
    want = approved_paths(doc)
    if sorted(paths) != want:
        return ('the paths named are not the approval\'s: named %s; hub:%s holds %s. Name '
                'exactly its files.' % (', '.join(sorted(paths)), did, ', '.join(want)))
    for p in want:
        why = _path_error(root, p)
        if why:
            return why
    if head != doc['head']:
        why = ap.same_content_error(root, doc['head'], head, want)
        if why:
            return ('the approval is for base %s, and HEAD is %s: %s. %s'
                    % (doc['head'][:12], (head or 'unborn')[:12], why, base_route(root, did)))
    unchanged = []
    for f in doc['files']:
        content = f.get('content')
        if content is not None and not isinstance(content, str):
            return 'the content of %s is not text' % f['path']
        if (None if content is None else ap.blob_id(content)) != f.get('blob'):
            return 'the content of %s is not the approved blob' % f['path']
        if f.get('blob') == f.get('base_blob'):
            unchanged.append(f['path'])
    if unchanged:
        return 'the approval changes nothing in: %s' % ', '.join(unchanged)
    for f in doc['files']:
        full = os.path.join(root, *f['path'].split('/'))
        base = f.get('base_blob')
        if base is None:
            drift = os.path.lexists(full)
        else:
            drift = not os.path.isfile(full) or _hash_object(root, f['path']) != base
        if drift:
            return ('%s is not the approved base: the work tree has %s since the approval was '
                    'asked. Put it back, or ask the owner again.'
                    % (f['path'], 'it' if base is None else 'changed it'))
    return None


def preverify_error(root, client, did, doc, head):
    """Why the hub does not verify the approval as it stands, before anything is written (its
    signature, status and repository, for a change on `head` with the approval's own blobs:
    approvals.Verifier.verify_on, with the approval's base), or None."""
    from kitlib.hubsite import approvals as ap
    repo, why = ap.remote_id(root)
    if repo is None:
        return 'the repository has no remote to name it: %s' % why
    ok, why = ap.Verifier(client).verify_on(root, did, repo, head,
                                            {f['path']: f.get('blob') for f in doc['files']},
                                            base=doc['head'])
    return None if ok else 'hub:%s refused: %s' % (did, why)


def staged_error(doc, head, staged, snap, judged):
    """Why what is staged is not the approved change, or None. `head`: HEAD as read for the
    commit; `judged`: the HEAD prewrite_error and preverify_error judged; `staged`: {path: status
    letter} this call commits; `snap`: their index entries ({path: (mode, oid)}, None for a
    deletion; kitlib.commit.index_snapshot)."""
    if head != judged:
        return 'HEAD is %s, not %s, the commit the approval was checked on' % (
            (head or 'unborn')[:12], (judged or 'unborn')[:12])
    want = approved_paths(doc)
    if sorted(staged) != want:
        return 'the staged paths are %s, not the approval\'s %s' % (
            ', '.join(sorted(staged)) or 'none', ', '.join(want))
    for f in doc['files']:
        entry = snap.get(f['path'])
        if entry and entry[0] not in oc.REGULAR_MODES:
            return ('the staged %s has mode %s: an approval holds regular files only (100644 or '
                    '100755)' % (f['path'], entry[0]))
        if (entry[1] if entry else None) != f.get('blob'):
            return ('the staged %s is not the approved blob (a git filter or .gitattributes '
                    'changed it on staging)' % f['path'])
    return None


def write(root, doc):
    """((saved, made), None) once every approved file is written (UTF-8 bytes, no line-ending
    change) or deleted; else (None, why) with the work tree restored. `saved`: [(full path, its
    bytes before or None)]; `made`: the folders this call created."""
    saved, made = [], []
    try:
        for f in doc['files']:
            full = os.path.join(root, *f['path'].split('/'))
            old = None
            if os.path.lexists(full):
                with open(full, 'rb') as fh:
                    old = fh.read()
            saved.append((full, old))
            if f.get('content') is None:
                os.remove(full)
                continue
            missing, parent = [], os.path.dirname(full)
            while not os.path.isdir(parent):
                missing.append(parent)
                parent = os.path.dirname(parent)
            for folder in reversed(missing):
                os.mkdir(folder)
                made.append(folder)
            with open(full, 'wb') as fh:
                fh.write(f['content'].encode('utf-8'))
    except OSError as e:
        failed = restore((saved, made))
        return None, 'the approved content could not be written (%s)%s' % (
            _err(e), '; and could not be put back: %s' % '; '.join(failed) if failed else '')
    return (saved, made), None


def restore(state):
    """Put back every file `write` saved and remove the folders it made; the failures, as text."""
    saved, made = state
    failed = []
    for full, old in reversed(saved):
        try:
            if old is None:
                if os.path.lexists(full):
                    os.remove(full)
            else:
                with open(full, 'wb') as fh:
                    fh.write(old)
        except OSError as e:
            failed.append('%s (%s)' % (full, _err(e)))
    for folder in reversed(made):
        try:
            os.rmdir(folder)
        except OSError:
            pass                    # not empty: something else is in it now
    return failed


def commit_facts(root, sha):
    """((parents, Owner-Approval values), None) of the commit `sha`, or (None, why)."""
    r = cmd.call(oc.NOREPLACE + ['show', '-s', '--format=%%P%%x00%%(trailers:key=%s,valueonly,'
                                 'separator=%%x01)' % TRAILER, sha, '--'], root)
    if r.rc != 0:
        return None, 'git show %s failed: %s' % (sha[:12], r.err or 'exit %s' % r.rc)
    parents, _nul, values = cmd.text(r.out).partition('\0')
    return (parents.split(), [v.strip() for v in values.split('\x01') if v.strip()]), None


def consume_commit(root, client, did, value, sha):
    """(the `approval` line, None, True) once the hub has consumed `did` for the commit `sha` (40
    hex) and verifies it for that commit; else (None, why, consumed): `consumed` True when the hub
    did consume it for `sha` (now or before) and only the verify for the commit failed, so a retry
    (--consume) asks that verify again. The commit must carry exactly `Owner-Approval: <value>`
    and have one parent, on the same content as the approval's base; the hub checks the rest (its
    base and files) before the consume (Verifier.verify_on, no commit) and after it (with the
    commit)."""
    from kitlib.hubsite import approvals as ap
    facts, why = commit_facts(root, sha)
    if facts is None:
        return None, why, False
    parents, values = facts
    if values != [value]:
        return None, 'the commit %s does not carry %s alone' % (sha[:12], trailer(value)), False
    if len(parents) != 1:
        return None, 'the commit %s is %s: an approved commit has one parent' % (
            sha[:12], 'a merge' if parents else 'a root commit'), False
    changed, why = oc.plain_changes(root, [sha])
    if changed is None:
        return None, why, False
    blobs, why = oc.blobs_at(root, sha, changed[sha])
    if blobs is None:
        return None, why, False
    odd = oc.irregular(blobs)
    if odd:
        return None, 'the commit %s holds %s' % (sha[:12], odd), False
    repo, why = ap.remote_id(root)
    if repo is None:
        return None, 'the repository has no remote to name it: %s' % why, False
    doc, why = fetch(client, did)
    if doc is None:
        return None, why, False
    verifier = ap.Verifier(client)
    if doc.get('commit') != sha:
        if doc.get('commit'):
            return None, state_error(did, doc), False
        ok, why = verifier.verify_on(root, did, repo, parents[0], blobs, base=doc['head'])
        if not ok:
            return None, 'hub:%s refused: %s' % (did, why), False
        version = doc.get('version', 0)
        try:
            ap.consume(client, did, sha, version if isinstance(version, int) else 0)
        except Exception as e:      # noqa: BLE001 - the commit stands; the caller says so
            return None, 'the hub did not consume hub:%s (%s)' % (did, _err(e)), False
    ok, why = verifier.verify_on(root, did, repo, parents[0], blobs, commit=sha, base=doc['head'])
    if not ok:
        return None, 'hub:%s refused: %s' % (did, why), True
    return 'approval  hub:%s consumed by %s' % (did, sha[:12]), None, True
