"""Remove a SCRATCH worktree: a detached tree an agent made under its temp folder (red evidence run
against an older commit), whose work is disposable (docs/worktree-remove.md "Tools", `--scratch`).
tools/worktree_remove.py `<path> --scratch` is the entry point. Nothing here prints.

`remove_scratch(top, argument)` removes the tree only when every one of these holds, checked in
this order before anything is touched:

- the path is under a scratch root (`roots`: AGENT_TEMP, TEMP, TMP and the system temp folder
  (kitlib.config.user.system_temp, never the machine config's `temp_dir`), read from the environment at run time; a root that is not an absolute
  existing folder, is reached through a link, is a drive root, the home folder or above it, or
  holds the main checkout is no root), no link or junction is on the way, and it is not inside
  the main checkout or a worktree folder (a workstream worktree keeps the slug form and its own
  checks). The roots come from the caller's environment: they scope the verb, they do not protect
  the tree; the checks below do;
- it is a worktree registered to this repository, not locked, detached, holding no other
  registered worktree;
- its HEAD, and every commit in its HEAD reflog, is reachable from a branch, remote-tracking
  branch or tag (`reachable`): a commit no ref reaches cannot be told apart from work, so it is
  refused (a ref that does not peel to a commit is skipped; a failed read proves nothing);
- nothing holds it, no link is inside it, and its ignored files are build output only (the
  workstream verbs' own rules, kitlib.worktree.remove);
- every uncommitted file is already in git at the SAME path (`preserved`): its content, and its
  staged blob when it has one, equal `<ref>:<path>` for some ref under refs/heads or refs/remotes.
  A rename, copy, conflict, type change, quoted path or nested repository is refused.

Then `restore` checks each file again, resets the index, checks the tracked ones out of HEAD
(literal pathspecs) and deletes the untracked ones, each checked once more just before (`_delete`:
its content, and no link on the way); `unmoved` reads HEAD and the reflog again; and the tree goes
through kitlib.worktree.remove.git_remove (its last status read, then a plain `git worktree
remove`: never forced). A status, list, reflog or hash that cannot be read refuses.
"""
import os

from kitlib.config import user as kuser

from . import holders, remove
from .holders import canon

OK, REFUSED = remove.OK, remove.REFUSED
ROOT_VARS = ('AGENT_TEMP', 'TEMP', 'TMP')
REF_SPACES = ('refs/heads', 'refs/remotes')
NOTHING = 'nothing was removed'


def _strictly_under(path, root):
    p, r = canon(path).rstrip('\\/'), canon(root).rstrip('\\/')
    return p.startswith(r + os.sep)


def roots(environ, main):
    """`(roots, dropped)`: the scratch roots named in `environ` (absolute, normalised), and for
    each named folder that is no root, `NAME=<path>: why`."""
    env = os.environ if environ is None else environ
    named = [(k, env.get(k)) for k in ROOT_VARS]
    try:
        named.append(('system temp', kuser.system_temp(env)))
    except RuntimeError:
        pass
    home = env.get('USERPROFILE') or os.path.expanduser('~')
    out, dropped, seen = [], [], set()
    for name, value in named:
        if not value:
            continue
        why = None
        p = os.path.normpath(value)
        if not os.path.isabs(p):
            why = 'not an absolute path'
        elif not os.path.isdir(p):
            why = 'not an existing folder'
        elif os.path.normcase(os.path.realpath(p)) != os.path.normcase(os.path.abspath(p)):
            why = 'reached through a link or junction'
        elif os.path.dirname(p.rstrip('\\/')) == p.rstrip('\\/') or os.path.dirname(p) == p:
            why = 'a drive root'
        elif home and (canon(home) == canon(p) or _strictly_under(home, p)):
            why = 'the home folder or above it'
        elif canon(main) == canon(p) or _strictly_under(main, p):
            why = 'it holds the main checkout'
        if why:
            dropped.append('%s=%s: %s' % (name, value, why))
            continue
        if canon(p) not in seen:
            seen.add(canon(p))
            out.append(p)
    return out, dropped


def target(ctx, argument, rts, dropped):
    """`(path, entry)` for the registered scratch worktree `argument` names, or the exit status
    after saying why not (a missing folder: OK)."""
    path = os.path.abspath(os.path.join(ctx.cwd, argument))
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(path):
        ctx.say('refused   %s: a link or junction is on the way to it (it resolves to %s); %s'
                % (path, os.path.realpath(path), NOTHING))
        return REFUSED
    if canon(path) == canon(ctx.main) or _strictly_under(path, ctx.main):
        ctx.say('refused   %s is inside the main checkout %s: a workstream worktree, never a'
                ' scratch one; use the slug form without --scratch; %s' % (path, ctx.main, NOTHING))
        return REFUSED
    home = next((h for h in remove.homes_of(ctx) if _strictly_under(path, h)), None)
    if home:
        ctx.say('refused   %s is inside the worktree folder %s: a workstream worktree, never a'
                ' scratch one; use the slug form without --scratch; %s' % (path, home, NOTHING))
        return REFUSED
    if not rts:
        ctx.say('refused   %s: no scratch root (AGENT_TEMP, TEMP, TMP or the system temp folder)'
                ' is usable%s; %s' % (path, ': ' + '; '.join(dropped) if dropped else '', NOTHING))
        return REFUSED
    if not any(_strictly_under(path, r) for r in rts):
        ctx.say('refused   %s is not under a scratch root (%s); %s'
                % (path, '; '.join(rts), NOTHING))
        return REFUSED
    if not os.path.isdir(path):
        ctx.say('nothing   %s does not exist' % path)
        return OK
    try:
        entries = remove.worktree_entries(remove.worktree_listing(ctx.main))
    except remove.GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s); %s' % (path, e, NOTHING))
        return REFUSED
    entry = next((e for e in entries if canon(e['path']) == canon(path)), None)
    if entry is None:
        ctx.say('refused   %s is not a registered worktree of %s; this tool never deletes a'
                ' folder git does not know; %s' % (path, ctx.main, NOTHING))
        return REFUSED
    inner = [e['path'] for e in entries if _strictly_under(e['path'], path)]
    if inner:
        ctx.say('refused   %s holds the registered worktree %s; %s'
                % (path, '; '.join(inner), NOTHING))
        return REFUSED
    if entry['locked'] is not None:
        ctx.say('refused   %s is locked (%s); %s' % (path, entry['locked'] or 'no reason', NOTHING))
        return REFUSED
    if entry['branch']:
        ctx.say('refused   %s is on branch %s: a scratch worktree is detached; %s'
                % (path, entry['branch'], NOTHING))
        return REFUSED
    if not entry['head']:
        ctx.say('refused   %s: git reported no HEAD for it; %s' % (path, NOTHING))
        return REFUSED
    return path, entry


PER_WORKTREE = ('refs/worktree', 'refs/bisect', 'refs/rewritten')


def reflog(path):
    """`([commit ids in the tree's HEAD reflog, oldest first, then the tips of its per-worktree
    refs (PER_WORKTREE)], None)` or `(None, why)`: every commit only the tree's git dir may keep,
    which `git worktree remove` deletes with the tree. The reflog file (`<its git dir>/logs/HEAD`)
    is read here: no reflog file is no entries, as `git log -g` would print none, and the
    per-worktree refs are read all the same. That holds only
    for the files ref format: a reftable repository never has the file, so any other format
    refuses. A per-worktree ref that is not a commit refuses."""
    args = ('rev-parse', '--show-ref-format')
    rc, out, err = remove.git(path, *args)
    if rc != 0 or not out:
        return None, remove.failure(args, rc, err)
    if out != 'files':
        return None, ('its refs are stored as %s, whose reflog this verb cannot read (only the'
                      ' files format keeps logs/HEAD)' % out)
    args = ('rev-parse', '--absolute-git-dir')
    rc, out, err = remove.git(path, *args)
    if rc != 0 or not out:
        return None, remove.failure(args, rc, err)
    log = os.path.join(out, 'logs', 'HEAD')
    try:
        with open(log, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ''               # no reflog: its per-worktree refs are still read below
    except OSError as e:
        return None, 'its HEAD reflog %s could not be read (%s)' % (log, e)
    oids = []
    for line in text.splitlines():
        pair = line.split('\t', 1)[0].split(' ')[:2]
        if len(pair) != 2 or not all(len(o) in (40, 64) and all(c in '0123456789abcdef' for c in o)
                                     for o in pair):
            return None, 'its HEAD reflog %s has a line this verb cannot read: %r' % (log, line)
        oids.extend(o for o in pair if o.strip('0') and o not in oids)
    # A per-worktree ref (refs/worktree, refs/bisect, refs/rewritten) lives in the tree's git dir
    # and goes with it: its commit must be kept by a shared ref, as a reflog entry must.
    args = ('for-each-ref', '--format=%(objectname) %(objecttype) %(refname)', *PER_WORKTREE)
    rc, out, err = remove.git(path, *args)
    if rc != 0:
        return None, remove.failure(args[:1], rc, err)
    for line in out.splitlines():
        p = line.split(' ', 2)
        if len(p) != 3 or p[1] != 'commit':
            return None, ('its per-worktree ref %s is not a commit (%r), which this verb cannot'
                          ' check' % (p[-1], line))
        if p[0] not in oids:
            oids.append(p[0])
    return oids, None


def _reaches(ctx, refs, oid):
    """`(ref, None)` for a ref whose history holds `oid`; `(None, None)` when no ref does;
    `(None, why)` when no ref proved it and a read failed (fail closed: never a NO)."""
    for tip, name in refs:          # a tip at the commit answers without a walk
        if tip == oid:
            return name, None
    failed = None
    for _, name in refs:
        # exit 1 is git's NO; anything else is a failed read: the next ref may still prove it.
        rc, _, err = remove.git(ctx.main, 'merge-base', '--is-ancestor', oid, name)
        if rc == 0:
            return name, None
        if rc != 1 and failed is None:
            failed = remove.failure(('merge-base', '--is-ancestor', oid[:10], name), rc, err)
    return None, failed


def reachable(ctx, path, head, logged):
    """The first ref (refs/heads, refs/remotes, refs/tags) whose history holds `head`, or None
    after saying why the tree is refused. Every commit in `logged` (its HEAD reflog) must be
    reached too: a commit made in the tree and left by a checkout is work no ref keeps."""
    def unknown(oid, why):
        ctx.say('refused   %s: whether a ref reaches %s is unknown (%s); %s'
                % (path, oid[:10], why, NOTHING))

    args = ('for-each-ref', '--format=%(objectname) %(objecttype) %(refname)', 'refs/heads',
            'refs/remotes', 'refs/tags')
    rc, out, err = remove.git(ctx.main, *args)
    if rc != 0:
        unknown(head, remove.failure(args[:1], rc, err))
        return None
    # A ref to a blob or tree holds no commit; a tag object may peel to one (merge-base peels it).
    refs = [(p[0], p[2]) for p in (line.split(' ', 2) for line in out.splitlines())
            if len(p) == 3 and p[1] in ('commit', 'tag')]
    ref, why = _reaches(ctx, refs, head)
    if ref is None:
        if why:
            unknown(head, why)
            return None
        rc, count, _ = remove.git(ctx.main, 'rev-list', '--count', head, '--not', '--branches',
                                  '--remotes', '--tags')
        ctx.say('refused   %s: no ref reaches its detached HEAD %s (%s commit(s) of its own would'
                ' be lost); whether to drop them is the owner\'s call; %s'
                % (path, head[:10], count if rc == 0 and count.isdigit() else 'an unknown number of',
                   NOTHING))
        return None
    for oid in logged:
        if oid == head:
            continue
        got, why = _reaches(ctx, refs, oid)
        if got is None:
            if why:
                unknown(oid, why)
            else:
                ctx.say('refused   %s: its HEAD reflog or a per-worktree ref holds %s, which no'
                        ' ref reaches (a commit'
                        ' made there and left behind); whether to drop it is the owner\'s call; %s'
                        % (path, oid[:10], NOTHING))
            return None
    return ref


# One git command line per batch of paths: Windows refuses a command line over 32767 characters
# (WinError 206), so a batch stays well under it.
BATCH_CHARS = 24000
BATCH_PATHS = 500


def _batches(rels):
    batch, size = [], 0
    for rel in rels:
        cost = len(rel) + 3  # the path, its quotes and a space
        if batch and (size + cost > BATCH_CHARS or len(batch) >= BATCH_PATHS):
            yield batch
            batch, size = [], 0
        batch.append(rel)
        size += cost
    if batch:
        yield batch


def hashes(tree, rels):
    """`({rel: blob id of the file as git would store it}, None)` or `(None, why)`."""
    if not rels:
        return {}, None
    result = {}
    for batch in _batches(rels):
        # Paths after `--` are files, never options; hash-object applies the path's filters (autocrlf).
        args = ('hash-object', '--') + tuple(batch)
        rc, out, err = remove.git(tree, *args)
        got = out.splitlines()
        if rc != 0 or len(got) != len(batch):
            return None, remove.failure(args[:1], rc, err)
        result.update(zip(batch, (g.strip() for g in got)))
    return result, None


def staged(tree, rels):
    """`({rel: staged blob id}, None)` or `(None, why)`. The whole index is listed and filtered
    here: a path given to ls-files would be a pathspec, and a `*` in a name a glob."""
    if not rels:
        return {}, None
    args = ('ls-files', '-s')
    rc, out, err = remove.git(tree, *args)
    if rc != 0:
        return None, remove.failure(args, rc, err)
    got, want = {}, set(rels)
    for line in out.splitlines():
        meta, _, rel = line.partition('\t')
        parts = meta.split()
        if rel in want and len(parts) == 3 and parts[2] == '0':
            got[rel] = parts[1]
    if set(got) != want:
        return None, 'git ls-files -s did not list every staged file (%s)' % ', '.join(
            sorted(set(rels) - set(got)))
    return got, None


def found(ctx, wants):
    """`({(rel, blob): ref}, None)` for each wanted (rel, blob) some ref under REF_SPACES holds
    at `rel`; `(None, why)` when git cannot say."""
    rc, out, err = remove.git(ctx.main, 'for-each-ref', '--format=%(refname)', *REF_SPACES)
    if rc != 0:
        return None, remove.failure(('for-each-ref',), rc, err)
    refs = [r.strip() for r in out.splitlines() if r.strip()]
    asks = [(ref, rel, blob) for rel, blob in wants for ref in refs]
    if not asks:
        return {}, None
    # One `<ref>:<path>` per line; each answer is `<oid> <type> <size>` or `<name> missing`.
    args = ('cat-file', '--batch-check')
    rc, out, err = remove.git(ctx.main, *args, stdin=''.join(
        '%s:%s\n' % (ref, rel) for ref, rel, _ in asks).encode('utf-8'))
    answers = out.splitlines()
    if rc != 0 or len(answers) != len(asks):
        return None, remove.failure(args, rc, err)
    hits = {}
    for (ref, rel, blob), answer in zip(asks, answers):
        parts = answer.split()
        if len(parts) == 3 and parts[0] == blob and parts[1] == 'blob' and (rel, blob) not in hits:
            hits[(rel, blob)] = ref
    return hits, None


def preserved(ctx, path, work):
    """`(plan, None)` when every status line in `work` is content git holds at that path, else
    `(None, why)`. plan: rels to reset (`staged`), to check out of HEAD (`checkout`), to delete
    (`delete`), the worktree blobs and staged blobs checked (`blobs`, `index`) and where each was
    found (`where`)."""
    plan = {'staged': [], 'checkout': [], 'delete': [], 'blobs': {}, 'index': {}, 'where': []}
    on_disk, in_index = [], []
    for line in work:
        xy, rel = line[:2], line[3:]
        if rel.startswith('"') or ' -> ' in rel:
            return None, '%r is a quoted path or a rename' % line
        if xy == '??' and rel.endswith('/'):
            return None, '%s is a folder git does not list into (a nested repository)' % rel
        if xy == '??':
            on_disk.append(rel)
            plan['delete'].append(rel)
            continue
        x, y = xy
        if x not in ' MAD' or y not in ' MD' or xy in ('DD', 'AA'):
            return None, '%r is a state this verb does not restore (rename, copy, conflict or' \
                         ' type change)' % line
        if x in 'MA':
            in_index.append(rel)
        if x != ' ':
            plan['staged'].append(rel)
        if x != 'D' and y != 'D':
            on_disk.append(rel)
        (plan['delete'] if x == 'A' else plan['checkout']).append(rel)
    blobs, why = hashes(path, on_disk)
    if blobs is None:
        return None, 'a file could not be hashed (%s)' % why
    index, why = staged(path, in_index)
    if index is None:
        return None, 'the staged blobs could not be read (%s)' % why
    wants = sorted(set(blobs.items()) | set(index.items()))
    hits, why = found(ctx, wants)
    if hits is None:
        return None, 'the refs could not be searched (%s)' % why
    for rel, blob in wants:
        if (rel, blob) not in hits:
            what = 'its staged copy' if index.get(rel) == blob and blobs.get(rel) != blob else 'it'
            return None, ('%s: %s is not in git at that path (no ref in %s holds this content'
                          ' there)' % (rel, what, ' or '.join(REF_SPACES)))
        plan['where'].append('%s (%s)' % (rel, hits[(rel, blob)]))
    plan['blobs'], plan['index'] = blobs, index
    return plan, None


def restore(ctx, path, plan, count):
    """Check each file once more, then make the tree clean: reset the index, check tracked files
    out of HEAD, delete untracked ones. True when done; False after saying why (git was not run)."""
    blobs, why = hashes(path, [r for r in plan['blobs'] if os.path.lexists(os.path.join(path, r))])
    index, why2 = staged(path, list(plan['index']))
    if blobs is None or index is None:
        ctx.say('failed    %s: a file could not be read again (%s); nothing was restored and git'
                ' was NOT run' % (path, why or why2))
        return False
    changed = [r for r in plan['blobs'] if blobs.get(r) != plan['blobs'][r]]
    changed += [r for r in plan['index'] if index.get(r) != plan['index'][r]]
    if changed:
        ctx.say('failed    %s: %s changed after it was checked; nothing was restored and git was'
                ' NOT run' % (path, '; '.join(sorted(set(changed)))))
        return False
    steps = []
    if plan['staged']:
        steps.append(('reset', '-q'))
    if plan['checkout']:
        steps.append(('checkout', '-q', 'HEAD', '--') + tuple(plan['checkout']))
    # Literal pathspecs: a `[` in a file name is never a glob that names other files.
    literal = dict(remove.git_env(), GIT_LITERAL_PATHSPECS='1')
    for args in steps:
        rc, out, err = remove.git(path, *args, env=literal)
        if rc != 0:
            ctx.say('failed    %s: %s; git worktree remove was NOT run'
                    % (path, remove.failure(args[:1], rc, err or out)))
            return False
    for rel in plan['delete']:
        if not _delete(ctx, path, rel, plan['blobs']):
            return False
    if count:
        ctx.say('restored  %d file(s) found in git at their paths: %s'
                % (count, holders.listed(plan['where'])))
    return True


def _delete(ctx, path, rel, blobs):
    """Delete the untracked file `rel` after checking it once more, just before: no link on the way
    (each folder and the file) and the content checked earlier. True when done or already gone."""
    parts = rel.split('/')
    full = os.path.join(path, *parts)
    try:
        for i in range(1, len(parts) + 1):
            if holders.linked(os.path.join(path, *parts[:i])):
                ctx.say('failed    %s: %s is a link; git worktree remove was NOT run'
                        % (path, '/'.join(parts[:i])))
                return False
        if not os.path.lexists(full):
            return True
        now, why = hashes(path, [rel])
        if now is None or rel not in blobs or now.get(rel) != blobs[rel]:
            ctx.say('failed    %s: %s changed after it was checked%s; git worktree remove was NOT'
                    ' run' % (path, rel, ' (%s)' % why if why else ''))
            return False
        os.remove(full)
    except OSError as e:
        ctx.say('failed    %s: could not delete %s (%s); git worktree remove was NOT run'
                % (path, rel, e))
        return False
    return True


def unmoved(ctx, path, head, logged):
    """True when the tree's HEAD, HEAD reflog and per-worktree ref tips are what was checked; else
    says why (a commit made while the verb ran would otherwise be removed with the tree)."""
    rc, now, err = remove.git(path, 'rev-parse', '--verify', 'HEAD')
    if rc != 0 or now != head:
        ctx.say('failed    %s: HEAD moved after it was checked (%s, was %s); git worktree remove'
                ' was NOT run' % (path, now[:10] if rc == 0 else remove.failure(
                    ('rev-parse',), rc, err), head[:10]))
        return False
    again, why = reflog(path)
    if again != logged:
        ctx.say('failed    %s: its HEAD reflog or a per-worktree ref changed after it was checked%s;'
                ' git worktree remove was NOT run' % (path, ' (%s)' % why if why else ''))
        return False
    return True


def remove_scratch(top, argument, cwd=None, environ=None):
    """`<path> --scratch`: Outcome(code, lines)."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return refused
    return ctx.outcome(_remove_scratch(ctx, argument, environ))


def _remove_scratch(ctx, argument, environ):
    rts, dropped = roots(environ, ctx.main)
    found_target = target(ctx, argument, rts, dropped)
    if not isinstance(found_target, tuple):
        return found_target
    path, entry = found_target
    head = entry['head']
    logged, why = reflog(path)
    if logged is None:
        ctx.say('refused   %s: %s; %s' % (path, why, NOTHING))
        return REFUSED
    ref = reachable(ctx, path, head, logged)
    if ref is None:
        return REFUSED
    ctx.say('scratch   %s: detached %s, in %s' % (path, head[:10], ref))
    why = holders.blockers(path)
    if why:
        ctx.say('refused   %s is held; git was NOT run' % path)
        ctx.describe(path, why)
        return REFUSED
    why = holders.link_reason(path)
    if why:
        ctx.say('refused   %s: %s; %s' % (path, why, NOTHING))
        return REFUSED
    work, _, ignored, why = remove.dirty(ctx, path)
    if work is None:
        ctx.say('refused   %s: its status could not be read (%s); %s' % (path, why, NOTHING))
        return REFUSED
    if ignored:
        ctx.say('refused   %s has %s. %s.' % (path, remove.ignored_reason(ignored, path),
                                             NOTHING[0].upper() + NOTHING[1:]))
        return REFUSED
    plan, why = preserved(ctx, path, work)
    if plan is None:
        ctx.say('refused   %s: %s; %s' % (path, why, NOTHING))
        return REFUSED
    if not restore(ctx, path, plan, len(work)):
        return REFUSED
    if not remove.clear_disposable(ctx, path):
        return REFUSED
    if not unmoved(ctx, path, head, logged):
        return REFUSED
    if not remove.git_remove(ctx, path, entry['path']):
        return REFUSED
    return OK
