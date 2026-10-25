"""Remove a workstream's worktree only when its work has landed and nothing holds it, or abandon
unlanded work under a tag (docs/worktree-remove.md). tools/worktree_remove.py is the entry point.

`remove(top, argument)`, `abandon(top, argument, why)` and `check(top)` act on the MAIN checkout of
the repository `top` is in (the main checkout or any linked worktree) and return Outcome(code,
lines). Nothing here prints. The checks, in order, are the PM tool's:

- the target: under `"repo".worktrees_dir` or another registered host's worktree folder
  (kitlib.worktree.hostdirs; homes_of: a folder reached through a junction is not one), not
  `"worktree_remove".exempt`, present, registered (a stump is named and never deleted), and on
  one of `branch_template`'s branches when one is set, unless it is detached; or, named
  by its PATH, a registered DETACHED tree outside those folders (`outside_target`: not the main
  checkout nor in it, no link on the way, not locked, holding no other registered tree); the plain
  verb (`remove`) alone also takes such a tree on a branch (the owner's rule: only that deliberate
  by-path call clears one), every later check still running. A slug never names an outside tree:
  one that only matches such a tree is refused with its path-form command (`slug_redirect`);
- landed: the branch (or detached HEAD) is an ancestor of `<remote>/<main_branch>` AS IT STANDS,
  never fetched (`merge_check`); UNLANDED work is the owner's to remove and the refusal prints the
  owner's commands and the `--abandon` verb, running none of them;
- on a tree outside the worktree folders or a DETACHED one anywhere, every commit in its HEAD
  reflog is reached by a ref (`kept_reflog`; a commit left there by a checkout lives only in that
  log, which `git worktree remove` deletes), and HEAD and the log are read again just before git
  (`unmoved`);
- holders (`holders.blockers`), then work (`dirty`: anything uncommitted or untracked, and any
  git-IGNORED file outside `build_output` or a keeper inside it; a `{piece, path}` entry is the
  folder the pieces table puts there, and one that cannot be resolved holds nothing), then links
  or junctions inside;
- only then the `disposable_untracked` files are deleted, the status is read once more
  (`last_look`, inside `git_remove`: every caller gets it), `git worktree remove` runs (never
  forced, never retried) and the branch goes with `git branch -d` (never -D).

`remove_branch` deletes a LANDED local branch that no worktree has checked out and no backlog record
still pulls, with `git branch -d` (never -D).

`abandon` tags the tip `archive/<slug>` (annotated, `abandoned: <why>`, local), every refusal
running before the tag, then removes the tree and deletes the branch with `git update-ref -d <ref>
<tip>` while the tag still holds that tip. Backlog records still pulled by the branch are reported.
On a LANDED tree holding uncommitted work it saves that work under the tag instead, then removes
the tree (`_abandon_landed_dirty`); on a landed clean tree it refuses.

Exit codes: OK 0; REFUSED 1 (refused, a leftover exists, git failed, or `git branch -d` refused
after the removal); CONFIG 2 (the kit configuration was refused).
"""
import collections
import datetime
import fnmatch
import json
import os
import re
import shutil

from kitlib import kitroot
from kitlib import pieces
from kitlib.backlog import store as backlog_store
from kitlib.backlog.errors import Failure as BacklogFailure
from kitlib.config import ConfigError, load
from kitlib.config import user as kuser
from kitlib.git import cmd, refs
from kitlib.host.registry import RegistryError

from . import holders, hostdirs
from .holders import canon

OK, REFUSED, CONFIG = 0, 1, 2
Outcome = collections.namedtuple('Outcome', 'code lines')

TOOL = 'worktree_remove'
ARCHIVE = 'archive/'
# Files that refuse a removal even inside build output (a dev database under bin/, SQLite's
# journals, keys, model weights, lowercase *.local.*). `"worktree_remove".keepers` adds to these.
KEEPERS = ('*.db', '*.db-wal', '*.db-shm', '*.sqlite', '*.key', '*.key.*', '*.pem', '*.pfx',
           '*.npz', '*.pt', '*.ckpt', '*.safetensors', '*.onnx', '*.local.*')
# --no-optional-locks: a status may otherwise rewrite the worktree's index (a write by a read),
# and the sweep's idle check reads that git dir.
STATUS_ARGS = ('--no-optional-locks', 'status', '--porcelain', '--branch', '--untracked-files=all',
               '--ignored=matching')


class GitReadError(RuntimeError):
    """A git read that failed, or printed nothing where git always answers: never an empty answer."""


def git_env():
    """Every git here: the scrubbed environment (cmd.scrubbed_env) with GCM_INTERACTIVE=never, so
    Git Credential Manager never opens a prompt the nightly task could not answer."""
    return dict(cmd.scrubbed_env(), GCM_INTERACTIVE='never')


def git(cwd, *args, timeout=cmd.GIT_TIMEOUT, env=None, stdin=None):
    """`(rc, stdout stripped, stderr)`; rc None when git did not run."""
    r = cmd.call(list(args), cwd, stdin=stdin, timeout=timeout, env=env or git_env())
    return r.rc, cmd.text(r.out or b'').strip(), (r.err or '').strip()


def failure(args, rc, err):
    """How a git read went wrong: the command, its exit code (or that it did not run), its stderr."""
    err = '; '.join(line.strip() for line in (err or '').splitlines() if line.strip())
    if rc is None:
        return 'git %s did not run: %s' % (' '.join(args), err or '?')
    return 'git %s exited %d%s' % (' '.join(args), rc, ': ' + err if err else ' and printed nothing')


def read(cwd, *args):
    """git's stdout for a read whose answer is never empty; GitReadError otherwise."""
    rc, out, err = git(cwd, *args)
    if rc != 0 or not out:
        raise GitReadError(failure(args, rc, err))
    return out


class Ctx(object):
    """One run: the main checkout, its kit Config, the settings below, the caller's directory and
    the output lines."""

    def __init__(self, main, cfg, cwd=None):
        self.main, self.cfg = main, cfg
        self.cwd = cwd or os.getcwd()
        self.s = settings(main, cfg)
        self.out = []

    def say(self, *lines):
        self.out.extend(lines)

    def describe(self, path, why):
        self.say(*holders.describe(path, why, cwd=self.cwd))

    def outcome(self, code):
        return Outcome(code, list(self.out))


def piece_dirs(cfg, entries):
    """`(dirs, None)`: each `{piece, path}` build_output entry as the literal repo-relative folder
    the pieces table puts it at (no trailing `/`), or `([], why)` when any of them cannot be
    resolved - no table, a malformed one (of any shape: the section is opaque, so anything the
    table load raises counts), an unknown piece, a piece or path that is not a string (the schema
    lets a null through), a path that is empty, absolute, `..`-climbing, backslashed or a glob,
    and a piece dir with an empty or `.` part. Never raises: a caller refuses on `why` (fail
    closed)."""
    if not entries:
        return [], None
    try:
        table = pieces.load_repo(None, cfg)
    except Exception as e:      # an opaque section: any shape can fail any way; refuse, never crash
        return [], 'the pieces table cannot be read (%s: %s)' % (type(e).__name__, e)
    dirs = []
    for ent in entries:
        pid, rel = ent.get('piece'), ent.get('path')
        if not isinstance(pid, str) or not isinstance(rel, str):
            return [], ('build_output entry %s: "piece" and "path" must both be strings'
                        % json.dumps(ent))
        parts = rel.rstrip('/').split('/')
        if (not rel.rstrip('/') or rel.startswith('/') or '\\' in rel or ':' in rel
                or any(c in rel for c in '*?[]') or '..' in parts or '' in parts or '.' in parts):
            return [], ('build_output entry %s: "path" must be a folder relative to the piece,'
                        ' `/`-separated, with no glob, `..` or empty part' % json.dumps(ent))
        try:
            folder = table.path(pid, rel).rstrip('/')
        except KeyError as k:
            return [], 'build_output entry %s: %s' % (json.dumps(ent), k.args[0])
        except Exception as e:
            return [], 'build_output entry %s: the pieces table cannot resolve it (%s: %s)' % (
                json.dumps(ent), type(e).__name__, e)
        if any(p in ('', '.', '..') for p in folder.split('/')):
            return [], ('build_output entry %s: piece %r has dir %r, which has an empty or `.`'
                        ' part, so it names no folder git prints' % (json.dumps(ent), pid,
                                                                       table.dir(pid)))
        dirs.append(folder)
    return dirs, None


def host_folders(cfg):
    """Every host's worktree folder (kitlib.worktree.hostdirs). A hosts.json that cannot be read
    leaves only "repo".worktrees_dir: removal then finds fewer worktrees, never more."""
    try:
        return hostdirs.folders(cfg)
    except RegistryError:
        return (os.path.normpath(cfg.repo['worktrees_dir']),)


def templates(value):
    """`branch_template` as a list of {slug} templates (the schema allows one string or a list)."""
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def settings(main, cfg):
    """This repository's values (the schema has already checked their shapes)."""
    wr = cfg.section(TOOL)
    build = list(wr.get('build_output') or [])
    dirs, dirs_error = piece_dirs(cfg, [b for b in build if isinstance(b, dict)])
    return {
        'base': cfg.main_branch, 'remote': cfg.remote,
        'upstream': '%s/%s' % (cfg.remote, cfg.main_branch),
        'worktrees_dir': os.path.normpath(cfg.repo['worktrees_dir']),
        'worktrees_dirs': list(host_folders(cfg)),
        'tool': kitroot.command('worktree_remove', main),     # the kit's own tool (no repository copy)
        'name': cfg.repo.get('name') or os.path.basename(os.path.normpath(main)),
        'exempt': dict(wr.get('exempt') or {}),
        'disposable_untracked': list(wr.get('disposable_untracked') or []),
        'build_output': [b for b in build if isinstance(b, str)],
        'build_dirs': dirs, 'build_dirs_error': dirs_error,
        'keepers': list(KEEPERS) + list(wr.get('keepers') or []),
        'archive_next_free': bool(wr.get('archive_next_free')),
        'branch_template': templates(wr.get('branch_template')),
    }


def main_checkout(start):
    """The checkout that owns the worktrees dir, from any directory of the repository, in its one
    real spelling. GitReadError when git cannot say (never a fallback to `start`)."""
    common = read(start, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return os.path.realpath(os.path.abspath(os.path.dirname(common)))


def open_ctx(top, cwd=None):
    """`(Ctx, None)` for the repository `top` is in, or `(None, Outcome)` refusing."""
    try:
        main = main_checkout(top)
    except GitReadError as e:
        return None, Outcome(REFUSED, ['failed    cannot find the main checkout (%s); nothing was'
                                       ' checked or removed' % e])
    try:
        cfg = load(main)
        ctx = Ctx(main, cfg, cwd)
    except ConfigError as e:
        return None, Outcome(CONFIG, ['%s: refused: kit configuration: %s' % (TOOL, e)])
    if not holders.WINDOWS:
        ctx.say('note      the destructive failure this guards is a Windows one; checking anyway')
    return ctx, None


def home_of(ctx):
    return os.path.normpath(os.path.join(ctx.main, ctx.s['worktrees_dir']))


def homes_of(ctx):
    """Primary root, plus non-redirected roots of other supported hosts."""
    primary = home_of(ctx)
    homes = [primary]
    for folder in ctx.s.get('worktrees_dirs', ()):
        candidate = os.path.abspath(os.path.join(ctx.main, folder))
        if canon(candidate) == canon(primary):
            continue
        # Additional host roots cannot escape the checkout through a junction.
        if os.path.normcase(os.path.realpath(candidate)) != os.path.normcase(candidate):
            continue
        if canon(candidate).startswith(canon(ctx.main) + os.sep):
            homes.append(candidate)
    return homes


def is_workstream(ctx, path):
    return any(canon(path).startswith(canon(home) + os.sep) for home in homes_of(ctx))


def worktree_listing(main):
    """`git worktree list --porcelain`'s text; GitReadError when it failed or printed nothing."""
    return read(main, 'worktree', 'list', '--porcelain')


def registrations(main, listing=None):
    """canon(path) -> (path as git registered it, branch or '(detached)')."""
    out, path = {}, None
    for line in (worktree_listing(main) if listing is None else listing).splitlines():
        if line.startswith('worktree '):
            path = line[len('worktree '):]
            out[canon(path)] = (path, '(detached)')
        elif line.startswith('branch ') and path:
            out[canon(path)] = (path, line[len('branch refs/heads/'):])
    return out


def registered(main, listing=None):
    return {key: branch for key, (_, branch) in registrations(main, listing).items()}


def merge_base_ref(s):
    """`refs/remotes/<remote>/<main_branch>`: only the PUSHED base counts."""
    return 'refs/remotes/%s/%s' % (s['remote'], s['base'])


def base_commit(ctx):
    """`(full hash, None)` of the base as it stands, or `(None, why)`."""
    args = ('rev-parse', '--verify', merge_base_ref(ctx.s) + '^{commit}')
    rc, base, err = git(ctx.main, *args)
    if rc != 0 or not base:
        return None, failure(args, rc, err)
    return base, None


def checked_out(main, key):
    """(branch or None, HEAD or None) of the registered worktree whose canon(path) is `key`."""
    branch = head = path = None
    for line in worktree_listing(main).splitlines():
        if line.startswith('worktree '):
            path = canon(line[len('worktree '):])
        elif path == key and line.startswith('HEAD '):
            head = line[len('HEAD '):].strip()
        elif path == key and line.startswith('branch '):
            branch = line[len('branch refs/heads/'):].strip()
    return branch, head


def merge_check(ctx, key, registered_path, seen=None):
    """None when the worktree's work is already in the base, else the refusal lines. The base is
    read as it stands, never fetched; a failure of the check itself refuses with no commands.
    `seen` (a dict), when given, gets the HEAD this check judged under 'head'."""
    s, main = ctx.s, ctx.main
    up = s['upstream']
    registered_path = os.path.normpath(registered_path)
    try:
        branch, head = checked_out(main, key)
    except GitReadError as e:
        return ['refused   %s: the worktree list could not be read (%s), so whether its work has'
                ' landed is unknown; nothing was removed' % (registered_path, e)]
    if seen is not None:
        seen['head'] = head
    tip = ('refs/heads/' + branch) if branch else head
    name = branch or ('detached HEAD %s' % (head or '?'))
    if not tip:
        return ['refused   %s: git reported no HEAD for it, so whether its work has landed is'
                ' unknown; nothing was removed' % registered_path]
    base, why = base_commit(ctx)
    if base is None:
        return ['refused   %s: cannot read %s (%s), so whether %s has landed is'
                ' unknown; nothing was removed' % (registered_path, up, why, name)]
    merged, why = refs.is_ancestor(tip, base, main)
    if merged:
        ctx.say('merged    %s is in %s %s' % (name, up, base[:10]))
        return None
    if merged is None:
        return ['refused   %s: the merge check failed (%s); nothing was removed'
                % (registered_path, why)]
    rc, count, err = git(main, 'rev-list', '--count', '%s..%s' % (base, tip))
    count = count if rc == 0 and count else 'an unknown number of'
    slug = os.path.basename(registered_path)
    named = handle(ctx, registered_path)
    lines = ['refused   %s holds UNLANDED work: %s has %s commit(s) not in %s %s;'
             ' nothing was removed' % (registered_path, name, count, up, base[:10]),
             '  abandon  the supported route, run on the owner\'s or the PM\'s ruling: it keeps the'
             ' commits under the tag %s%s (--tag %s<name> for the tag a ruling names), then'
             ' removes the tree:' % (ARCHIVE, slug, ARCHIVE),
             '             %s %s --abandon "<why>"' % (s['tool'], named),
             '  owner    removing unlanded work outright is the owner\'s standing rule that'
             ' deleting data is the owner\'s, and this verb has no override. To discard it, the'
             ' owner runs, in the owner\'s own terminal only (the Action Hub refuses these as a'
             ' to-do),',
             '           from %s:' % main,
             '             git worktree remove "%s"' % registered_path]
    if branch:
        lines.append('             git branch -D %s' % branch)
    lines.append('  note     %s was read as it stands, not fetched: if the branch has'
                 ' landed since, `git fetch %s` and run this again.' % (up, s['remote']))
    return lines


def exemption(s, path):
    """The ruling exempting the folder at `path` (`"worktree_remove".exempt`), or None."""
    name = os.path.normcase(os.path.basename(os.path.normpath(path)))
    for slug, ruling in s['exempt'].items():
        if os.path.normcase(slug) == name:
            return ruling
    return None


def leftovers(ctx, listing):
    """(stumps, orphans): directories git does not know about, and registered entries under the
    worktrees dir whose directory is gone. Exempt folders are neither."""
    known = registered(ctx.main, listing)
    stumps = []
    for home in homes_of(ctx):
        for name in sorted(os.listdir(home)) if os.path.isdir(home) else []:
            path = os.path.join(home, name)
            if os.path.isdir(path) and canon(path) not in known and not exemption(ctx.s, path):
                stumps.append(path)
    orphans = [p for p in sorted(known) if is_workstream(ctx, p)
               and not os.path.isdir(p) and not exemption(ctx.s, p)]
    return stumps, orphans


def ancestry(ctx, base):
    """`((first-parent chain, ancestors), None)` of `base` from one `git rev-list --parents`, or
    `(None, why)`."""
    from kitlib.git import batch
    return batch.ancestry(base, ctx.main, timeout=cmd.GIT_TIMEOUT, env=git_env())


def merged_worktrees(ctx, listing, skip=()):
    """`([(path, branch or None)], None)`: registered worktrees in the worktrees dir whose own work
    has LANDED: the tip is in the base AND off its first-parent chain (a fresh worktree, tip ==
    a base tip, is never one); or `(None, why)`. Paths in `skip` are left out (preflight leaves
    out the worktree it runs in)."""
    base, why = base_commit(ctx)
    if base is None:
        return None, why
    got, why = ancestry(ctx, base)
    if got is None:
        return None, 'git rev-list --parents %s: %s' % (base, why)
    chain, ancestors = got
    home = canon(home_of(ctx))
    skipped = {canon(p) for p in skip}
    out = []
    for e in worktree_entries(listing):
        path = e['path']
        if not is_workstream(ctx, path) or canon(path) in skipped \
                or exemption(ctx.s, path) or not os.path.isdir(path):
            continue
        if not e['head'] or e['head'] in chain:
            continue
        if e['head'] in ancestors:
            out.append((os.path.normpath(path), e['branch']))
    return out, None


def worktree_entries(listing):
    """Each registered worktree as a dict: path, head, branch (None detached), locked (None, or
    git's reason, '' for none)."""
    out = []
    for line in listing.splitlines():
        if line.startswith('worktree '):
            out.append({'path': line[len('worktree '):], 'head': None, 'branch': None,
                        'locked': None})
        elif not out:
            continue
        elif line.startswith('HEAD '):
            out[-1]['head'] = line[len('HEAD '):].strip()
        elif line.startswith('branch '):
            out[-1]['branch'] = line[len('branch refs/heads/'):].strip()
        elif line == 'locked' or line.startswith('locked '):
            out[-1]['locked'] = line[len('locked'):].strip()
    return out


def check(top, cwd=None):
    """`--check`: leftovers (stumps, orphans) and, for information, landed worktrees. Exit 1 when a
    stump or orphan exists."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    try:
        listing = worktree_listing(ctx.main)
    except GitReadError as e:
        ctx.say('failed    %s; nothing is listed: which directories git knows is unknown' % e)
        return ctx.outcome(REFUSED)
    stumps, orphans = leftovers(ctx, listing)
    for path in stumps:
        why = holders.blockers(path)
        ctx.say('leftover  %s' % path)
        ctx.describe(path, why)
        ctx.say('           the slug stays unusable until whatever holds it lets go.' if why else
                '           the slug is free again as soon as a human deletes this directory.')
    for path in orphans:
        ctx.say('orphan    %s registered with no directory on disk; run `git worktree repair'
                ' <path>` first, and prune only a single entry proven empty and merged' % path)
    merged, why = merged_worktrees(ctx, listing)
    if merged is None:
        ctx.say('merged    not checked: %s' % why)
    for path, branch in merged or []:
        ctx.say('merged    %s [%s] landed: its own commits are in %s; `%s %s` removes it%s'
                % (path, branch or 'detached', ctx.s['upstream'], ctx.s['tool'],
                   os.path.basename(path) if canon(path).startswith(canon(home_of(ctx)) + os.sep) else '"%s"' % path,
                   ' and its branch' if branch else ''))
    if not stumps and not orphans:
        ctx.say('clean     every directory under %s is a registered worktree'
                % ctx.s['worktrees_dir'])
        return ctx.outcome(OK)
    return ctx.outcome(REFUSED)


def delete_branch(ctx, branch, keep, removed=True):
    """After a removal (`removed`) or on its own: `git branch -d`, never -D. Returns the exit
    status."""
    if branch is None:
        ctx.say('branch    none: it was a detached HEAD')
        return OK
    if keep:
        ctx.say('kept      branch %s (--keep-branch)' % branch)
        return OK
    rc, out, err = git(ctx.main, 'branch', '-d', branch)
    if rc == 0:
        ctx.say('deleted   branch %s (%s)' % (branch, out or 'git branch -d'))
        return OK
    ctx.say('failed    git branch -d %s exited %s: %s' % (branch, rc, err or out),
            '  kept     branch %s is still there; %s' % (
                branch, 'the worktree removal above stands and is not undone.' if removed
                else 'nothing was changed.'),
            '           `-d` checks the main checkout\'s HEAD, not %s: if that checkout is'
            % ctx.s['upstream'],
            '           behind, pull it, then `git branch -d %s`. Never -D.' % branch)
    return REFUSED


def target_path(ctx, argument):
    """The absolute path `argument` names: a path (holding a separator) against the caller's
    directory, a slug under the worktrees dir."""
    if os.path.sep in argument or '/' in argument:
        return os.path.abspath(os.path.join(ctx.cwd, argument))
    return os.path.abspath(os.path.join(home_of(ctx), argument))


def handle(ctx, registered_path):
    """How a printed command names a tree: its slug in a worktree folder, else its quoted path
    (a slug always resolves under the worktree folder)."""
    if is_workstream(ctx, registered_path):
        return os.path.basename(os.path.normpath(registered_path))
    return '"%s"' % os.path.normpath(registered_path)


def _under(path, root):
    return canon(path).startswith(canon(root).rstrip('\\/') + os.sep)


def link_on_the_way(path):
    """The first folder of `path`, itself or an ancestor, that is a link, junction or other reparse
    point (holders.linked), or None. Another spelling of the same folders - an 8.3 short name, a
    subst drive - is no link, so it is not refused: canon resolves it to the tree git registered."""
    p = os.path.abspath(path)
    while True:
        if holders.linked(p):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def kept_reflog(ctx, path, head, tagged):
    """The HEAD reflog (kitlib.worktree.scratch.reflog) of a tree outside every worktree folder or
    detached anywhere, when every commit in it is kept: reached by a branch, remote-tracking
    branch or tag, and with `tagged` (--abandon) also by `head`, which the tag is about to hold. Else None after saying why: a commit made in the tree
    and left by a checkout lives only in `logs/HEAD`, which `git worktree remove` deletes, and a
    commit no ref reaches is the owner's call (the --scratch verb's rule). Reads only."""
    from . import scratch           # scratch imports this module
    logged, why = scratch.reflog(path)
    if logged is None:
        ctx.say('refused   %s: %s; nothing was %s' % (path, why, 'tagged or removed' if tagged
                                                         else 'removed'))
        return None
    if not tagged:
        return logged if scratch.reachable(ctx, path, head, logged) else None
    args = ('for-each-ref', '--format=%(objectname) %(objecttype) %(refname)', 'refs/heads',
            'refs/remotes', 'refs/tags')
    rc, out, err = git(ctx.main, *args)
    if rc != 0:
        ctx.say('refused   %s: whether a ref reaches its HEAD reflog is unknown (%s); nothing was'
                ' tagged or removed' % (path, failure(args[:1], rc, err)))
        return None
    # The tag on HEAD keeps HEAD's history: HEAD is the first "ref" asked (merge-base takes a hash).
    refs = [(head, head)] + [(p[0], p[2]) for p in (line.split(' ', 2) for line in out.splitlines())
                             if len(p) == 3 and p[1] in ('commit', 'tag')]
    lost = []
    for oid in logged:
        if oid == head:
            continue
        got, why = scratch._reaches(ctx, refs, oid)
        if got is None and why:
            ctx.say('refused   %s: whether a ref reaches %s is unknown (%s); nothing was tagged or'
                    ' removed' % (path, oid[:10], why))
            return None
        if got is None:
            lost.append(oid[:10])
    if lost:
        ctx.say('refused   %s: its HEAD reflog or a per-worktree ref holds %s, which no ref'
                ' reaches and the tag on HEAD'
                ' %s would not keep (a commit made there and left behind); whether to drop it is'
                ' the owner\'s call; nothing was tagged or removed' % (path, ', '.join(lost),
                                                                       head[:10]))
        return None
    return logged


def unmoved(ctx, path, head, logged):
    """kitlib.worktree.scratch.unmoved: HEAD and its reflog read once more just before git, so a
    commit made while the verb ran is not removed with an outside or detached tree."""
    from . import scratch
    return scratch.unmoved(ctx, path, head, logged)


def unsaved_count(ctx, commits, also_saved=()):
    """How many commits reached from `commits` no branch, remote-tracking branch, tag or
    `also_saved` commit reaches: `(n, None)`, or `(None, why)`."""
    args = ('rev-list', '--count', *commits, '--not', *also_saved, '--branches', '--remotes',
            '--tags')
    rc, count, err = git(ctx.main, *args)
    if rc != 0 or not count.isdigit():
        return None, failure(args[:2], rc, err)
    return int(count), None


# The pseudo-refs a worktree's admin folder may hold besides its reflogs and per-worktree refs.
ADMIN_HEADS = ('HEAD', 'ORIG_HEAD', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD')
HEX = frozenset('0123456789abcdef')


def _oid(word):
    return len(word) in (40, 64) and set(word) <= HEX


def admin_dir(ctx, path):
    """`(the admin folder <git common dir>/worktrees/<id> whose gitdir file names `path`, None)`,
    or `(None, why)`: none or more than one does, or a read failed."""
    args = ('rev-parse', '--path-format=absolute', '--git-common-dir')
    rc, common, err = git(ctx.main, *args)
    if rc != 0 or not common:
        return None, failure(args, rc, err)
    top = os.path.join(common, 'worktrees')
    try:
        names = sorted(os.listdir(top))
    except OSError as e:
        return None, 'the admin folders in %s could not be listed (%s)' % (top, e)
    found, key = [], canon(path)
    for name in names:
        gitdir = os.path.join(top, name, 'gitdir')
        try:
            with open(gitdir, encoding='utf-8', errors='replace') as fh:
                named = fh.read().strip()
        except FileNotFoundError:
            continue
        except OSError as e:
            return None, '%s could not be read (%s)' % (gitdir, e)
        if named and canon(os.path.dirname(named)) == key:
            found.append(os.path.join(top, name))
    if len(found) != 1:
        return None, ('%s admin folder in %s names it%s' % (
            'more than one' if found else 'no', top, ' (%s)' % ', '.join(found) if found else ''))
    return found[0], None


def admin_commits(ctx, admin):
    """`([every commit id the admin folder `admin` alone may keep], None)` or `(None, why)`: its
    HEAD and pseudo-refs (ADMIN_HEADS) when they hold an id, every id in its reflogs (`logs/`)
    and the tips of its per-worktree refs (`refs/`). `git worktree remove` deletes them all. Read
    from the files only for the files ref format; any other format, and a line that is not an id,
    a symbolic ref or a reflog entry, refuses."""
    args = ('rev-parse', '--show-ref-format')
    rc, out, err = git(ctx.main, *args)
    if rc != 0 or not out:
        return None, failure(args, rc, err)
    if out != 'files':
        return None, 'its refs are stored as %s, whose admin folder this verb cannot read' % out
    files, errors = [os.path.join(admin, n) for n in ADMIN_HEADS], []
    for sub in ('refs', 'logs'):
        root = os.path.join(admin, sub)
        if os.path.isdir(root):
            for folder, _, names in os.walk(root, onerror=errors.append):
                files.extend(os.path.join(folder, n) for n in sorted(names))
        elif os.path.lexists(root):
            errors.append('%s is not a folder' % root)
    if errors:
        return None, 'its admin folder could not be read in full (%s)' % errors[0]
    oids = []
    for name in files:
        try:
            with open(name, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except FileNotFoundError:
            continue
        except OSError as e:
            return None, '%s could not be read (%s)' % (name, e)
        log = os.path.relpath(name, admin).split(os.sep)[0] == 'logs'
        for line in text.splitlines():
            if not line.strip():
                continue
            words = line.split('\t', 1)[0].split(' ')[:2] if log else \
                [] if line.startswith('ref: ') else line.split()[:1]
            if (log and len(words) != 2) or not all(_oid(w) for w in words):
                return None, '%s has a line this verb cannot read: %r' % (name, line)
            oids.extend(w for w in words if w.strip('0') and w not in oids)
    return oids, None


def orphan_unsaved(ctx, path):
    """`(how many commits only the admin folder of the missing tree at `path` keeps and no branch,
    remote-tracking branch or tag reaches, None)`, or `(None, why)`. Reads only."""
    admin, why = admin_dir(ctx, path)
    if admin is None:
        return None, why
    oids, why = admin_commits(ctx, admin)
    if oids is None:
        return None, why
    return unsaved_count(ctx, oids) if oids else (0, None)


def orphan_outside(ctx, path, environ=None):
    """Why the stale registration at `path` is reported only, or None when it is the kit's to
    drop: in a worktree folder (is_workstream) or under a scratch root (kitlib.worktree.scratch
    roots, from `environ`). A folder anywhere else may be on a drive that is not mounted now or
    was remapped: gone from this view only."""
    if is_workstream(ctx, path):
        return None
    from . import scratch           # scratch imports this module
    rts, _ = scratch.roots(environ, ctx.main)
    if any(scratch._strictly_under(path, r) for r in rts):
        return None
    return ('it is outside every worktree folder and scratch root (a drive not mounted now, or'
            ' remapped, looks the same): reported only')


def prune_orphan(ctx, path, environ=None):
    """Drop the registration of the worktree at `path` whose folder is already gone (`--classify`
    class O), keeping its branch: `git worktree remove <the registered path>`, never forced, never
    `git worktree prune` (which drops every such registration). Refused, nothing changed: a path
    that exists, a path outside the worktree folders and scratch roots (orphan_outside; `environ`
    names the roots), an exempt folder, a locked tree, the main checkout, and a registration whose
    admin folder keeps a commit no ref reaches (its HEAD reflog, a per-worktree ref or a
    pseudo-ref: the owner's call) or cannot be read. The folder and the admin folder are read once
    more just before git; a folder made inside that window is the window this narrows, not
    closes. A path no longer registered is `nothing` (OK)."""
    path = os.path.normpath(path)
    nothing = 'nothing was removed'
    if os.path.lexists(path):
        ctx.say('refused   %s exists: its registration is not stale; %s' % (path, nothing))
        return REFUSED
    outside = orphan_outside(ctx, path, environ)
    if outside:
        ctx.say('refused   %s: %s; %s' % (path, outside, nothing))
        return REFUSED
    ruling = exemption(ctx.s, path)
    if ruling:
        ctx.say('refused   %s is exempt (%s); %s' % (path, ruling, nothing))
        return REFUSED
    try:
        entries = worktree_entries(worktree_listing(ctx.main))
    except GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s); %s' % (path, e, nothing))
        return REFUSED
    key = canon(path)
    entry = next((e for e in entries if canon(e['path']) == key), None)
    if entry is None:
        ctx.say('nothing   %s is not registered' % path)
        return OK
    if key == canon(ctx.main):
        ctx.say('refused   %s is the main checkout; %s' % (path, nothing))
        return REFUSED
    if entry['locked'] is not None:
        ctx.say('refused   %s is locked (%s); %s' % (path, entry['locked'] or 'no reason', nothing))
        return REFUSED
    admin, why = admin_dir(ctx, path)
    oids, why = admin_commits(ctx, admin) if admin else (None, why)
    lost, why = (None, why) if oids is None else unsaved_count(ctx, oids) if oids else (0, None)
    if lost is None:
        ctx.say('refused   %s: whether its registration keeps a commit no ref reaches is unknown'
                ' (%s); %s' % (path, why, nothing))
        return REFUSED
    if lost:
        ctx.say('refused   %s: its folder is gone, but its registration (%s) keeps %d commit(s)'
                ' no branch, remote-tracking branch or tag reaches (a HEAD reflog, per-worktree'
                ' ref or pseudo-ref); whether to drop them is the owner\'s call; %s'
                % (path, admin, lost, nothing))
        return REFUSED
    again, why = admin_commits(ctx, admin)
    moved = ('its folder appeared after it was checked' if os.path.lexists(path) else
             'its admin folder could not be read again (%s)' % why if again is None else
             'its admin folder changed after it was checked' if again != oids else None)
    if moved:
        ctx.say('refused   %s: %s; %s' % (path, moved, nothing))
        return REFUSED
    rc, out, err = git(ctx.main, 'worktree', 'remove', entry['path'])
    if rc != 0:
        ctx.say('failed    git worktree remove exited %s: %s' % (rc, err or out))
        return REFUSED
    try:
        still = key in registered(ctx.main)
    except GitReadError as e:
        ctx.say('failed    %s: whether its registration is gone is unknown: the worktree list'
                ' could not be read again (%s)' % (path, e))
        return REFUSED
    if still:
        ctx.say('failed    %s is still registered after git worktree remove exited 0' % path)
        return REFUSED
    ctx.say('removed   %s: its stale registration (the folder was already gone)%s'
            % (path, '; branch %s kept' % entry['branch'] if entry['branch'] else ''))
    return OK


def outside_target(ctx, path, branch_ok=False):
    """`(path, registrations)` for a registered DETACHED worktree outside every worktree folder,
    named by its path (an agent's red-run tree in a scratchpad), or the exit status after saying
    why not (a missing folder: OK). The scope is git's own registration list. With `branch_ok`
    (only the plain verb, `remove`, by the owner's rule) a tree on a branch is taken too, and
    `merge_check` then judges that branch; `--abandon`, the landing cleanup and classify (which
    call `_remove` directly) take only a detached one."""
    main, nothing = ctx.main, 'nothing was removed'
    where = ('is the main checkout' if canon(path) == canon(main) else
             'is inside the main checkout' if _under(path, main) else
             'holds the main checkout' if _under(main, path) else None)
    if where:
        ctx.say('refused   %s is not under %s: it %s %s, and this tool never removes the main'
                ' checkout or a tree inside it outside the worktree folders; %s'
                % (path, home_of(ctx), where, main, nothing))
        return REFUSED
    link = link_on_the_way(path)
    if link:
        ctx.say('refused   %s: a link or junction is on the way to it (%s is one; it resolves to'
                ' %s); %s' % (path, link, os.path.realpath(path), nothing))
        return REFUSED
    if exemption(ctx.s, path):
        ctx.say('refused   %s is exempt (%s): this tool never removes it; nothing was touched'
                % (path, exemption(ctx.s, path)))
        return REFUSED
    if not os.path.isdir(path):
        ctx.say('nothing   %s does not exist' % path)
        return OK
    try:
        listing = worktree_listing(main)
    except GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s), so whether it is a'
                ' registered worktree is unknown; %s' % (path, e, nothing))
        return REFUSED
    entries = worktree_entries(listing)
    entry = next((e for e in entries if canon(e['path']) == canon(path)), None)
    if entry is None:
        ctx.say('refused   %s is not a registered worktree of %s; this tool never deletes a'
                ' folder git does not know; %s' % (path, main, nothing))
        return REFUSED
    inner = [os.path.normpath(e['path']) for e in entries if _under(e['path'], path)]
    if inner:
        ctx.say('refused   %s holds the registered worktree %s; %s'
                % (path, '; '.join(inner), nothing))
        return REFUSED
    if entry['locked'] is not None:
        ctx.say('refused   %s is locked (%s); %s' % (path, entry['locked'] or 'no reason', nothing))
        return REFUSED
    if entry['branch'] and not branch_ok:
        ctx.say('refused   %s is on branch %s: outside the worktree folders only a detached tree'
                ' is removed here (a branch\'s worktree is a workstream\'s, under %s; the plain'
                ' verb named by its path removes one whose branch has landed); %s'
                % (path, entry['branch'], home_of(ctx), nothing))
        return REFUSED
    if entry['branch'] and same_name(entry['branch'], ctx.s['base']):
        ctx.say('refused   %s is on the main branch %s: this tool never removes a tree on it nor'
                ' deletes it; %s' % (path, entry['branch'], nothing))
        return REFUSED
    if not entry['head']:
        ctx.say('refused   %s: git reported no HEAD for it; %s' % (path, nothing))
        return REFUSED
    return path, registrations(main, listing)


def outside_matches(ctx, slug):
    """The registered worktrees outside every worktree folder whose folder is named `slug`, as
    git spells them; GitReadError when the list cannot be read."""
    return [os.path.normpath(e['path']) for e in worktree_entries(worktree_listing(ctx.main))
            if os.path.normcase(os.path.basename(os.path.normpath(e['path'])))
            == os.path.normcase(slug) and not is_workstream(ctx, e['path'])
            and canon(e['path']) != canon(ctx.main)]


def slug_redirect(ctx, argument, path):
    """For a slug with no folder under the worktree folder: REFUSED after naming the path-form
    command of each outside tree of that name (never removed by its name), else None."""
    try:
        found = outside_matches(ctx, argument)
    except GitReadError as e:
        # A slug with no folder was `nothing` (exit 0) before outside trees existed; a list that
        # cannot be read removes nothing either, so it stays `nothing`, with the doubt said.
        ctx.say('note      the worktree list could not be read (%s), so whether a worktree outside'
                ' the worktree folders is named %s is unknown' % (e, argument))
        return None
    if not found:
        return None
    ctx.say('refused   %s is not under %s: a worktree named %s is registered outside the worktree'
            ' folders, and a slug never names one; nothing was removed. Name it by its path:'
            ' --scratch for a red-run tree under a temp folder (its HEAD and reflog need only be in'
            ' some ref; uncommitted files git already holds are restored), the plain verb when its'
            ' HEAD is in %s, --abandon to keep unlanded work under a tag:'
            % (argument, home_of(ctx), argument, ctx.s['upstream']))
    for p in found:
        ctx.say('             %s "%s" --scratch' % (ctx.s['tool'], p),
                '             %s "%s"' % (ctx.s['tool'], p),
                '             %s "%s" --abandon "<why>"' % (ctx.s['tool'], p))
    return REFUSED


def registered_target(ctx, argument, branch_ok=False):
    """`(path, registrations)` for the registered workstream worktree `argument` names, or, named
    by its path, a detached one outside the worktree folders (`outside_target`; also one on a
    branch with `branch_ok`); else the exit status after saying why there is none (absent: 0)."""
    s = ctx.s
    path = target_path(ctx, argument)
    by_path = os.path.sep in argument or '/' in argument
    if not is_workstream(ctx, path):
        if by_path:
            return outside_target(ctx, path, branch_ok)
        ctx.say('refused   %s is not under %s; this tool removes workstream worktrees only'
                % (path, home_of(ctx)))
        return REFUSED
    if exemption(s, path):
        ctx.say('refused   %s is exempt (%s): this tool never removes it; nothing was touched'
                % (path, exemption(s, path)))
        return REFUSED
    if not os.path.isdir(path):
        if not by_path:
            redirected = slug_redirect(ctx, argument, path)
            if redirected is not None:
                return redirected
        ctx.say('nothing   %s does not exist' % path)
        return OK
    try:
        known = registrations(ctx.main)
    except GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s), so whether it is a'
                ' registered worktree is unknown; nothing was removed' % (path, e))
        return REFUSED
    if canon(path) not in known:
        ctx.say('leftover  %s is not a registered worktree - the stump of a removal that already'
                ' failed' % path)
        ctx.describe(path, holders.blockers(path))
        ctx.say('           git cannot remove it (`not a working tree`) and neither will this tool.',
                '           Clearing it is a human decision; this tool never deletes a leftover.')
        return REFUSED
    registered_path, branch = known[canon(path)]
    # A detached tree has no branch to hold to a template; the HEAD reflog check
    # (kept_reflog) still guards what it made.
    if s['branch_template'] and branch != '(detached)':
        slug = os.path.basename(os.path.normpath(registered_path))
        wants = [t.replace('{slug}', slug) for t in s['branch_template']]
        if branch not in wants:
            ctx.say('refused   %s is on %s, not %s: this repository removes only worktrees on'
                    ' their own %s branch (.kit.json worktree_remove.branch_template); nothing was'
                    ' removed' % (path, branch, ' or '.join(wants), ' or '.join(s['branch_template'])))
            return REFUSED
    return path, known


def dirty(ctx, path):
    """`(work, disposable, ignored, None)`: status lines that are work, the untracked files
    `disposable_untracked` names, and `!! ` lines of ignored files outside build output (or keepers
    inside it); `(None, None, None, why)` when the status cannot be read (never a clean tree).
    When a `{piece, path}` build_output entry cannot be resolved, no file counts as inside it and
    the first `!! ` line outside the globs says why (fail closed: the tree stays)."""
    s = ctx.s
    globs, build, dirs = s['disposable_untracked'], s['build_output'], s['build_dirs']
    unresolved = s['build_dirs_error']
    # --branch puts a `## ` header first, so the strip cannot eat the first entry's leading space.
    rc, out, err = git(path, *STATUS_ARGS)
    if rc != 0 or not out:
        return None, None, None, failure(STATUS_ARGS, rc, err)
    work, disposable, ignored, budget = [], [], [], [holders.WALK_MAX]
    for line in out.splitlines():
        if line.startswith('## '):
            continue
        if line.startswith('!! '):
            rel = line[3:]
            # A quoted path is never matched: it is named in the refusal instead (fail closed).
            if rel.startswith('"') or not holders.is_build_output(rel, build, dirs):
                if unresolved:
                    line, unresolved = '%s (%s)' % (line, unresolved), None
                ignored.append(line)
            else:
                ignored.extend(holders.keepers_in(path, rel, s['keepers'], budget))
            continue
        rel = line[3:] if line.startswith('?? ') and not line[3:].startswith('"') else None
        if rel and any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel.rsplit('/', 1)[-1], g)
                       for g in globs):
            disposable.append(rel)
        else:
            work.append(line)
    return work, disposable, ignored, None


def ignored_reason(ignored, path):
    """The words for a tree holding ignored files that are not build output."""
    return ('ignored files that are not build output (%d): %s - `git worktree remove` would'
            ' delete them without a word; list them: git -C "%s" status --ignored --porcelain (a'
            " file inside a build-output folder shows there only as its folder); deleting them is"
            " the owner's decision, not an agent's. Move each one worth keeping out of the tree, or"
            ' name them in .kit.json worktree_remove.build_output if they are build output (a'
            ' keeper - KEEPERS or worktree_remove.keepers - refuses even there)'
            % (len(ignored), holders.listed(ignored), os.path.normpath(path)))


def dirty_refusal(ctx, path, what, rerun):
    """None when `path` holds no work and no ignored file outside build output, else says why it
    is refused and returns REFUSED. `what` ends the refusal; `rerun` follows an ignored refusal."""
    work, _, ignored, why = dirty(ctx, path)
    end = what[0].upper() + what[1:]
    if work is None:
        ctx.say('refused   %s: its status could not be read (%s); %s' % (path, why, what))
        return REFUSED
    if work:
        ctx.say('refused   %s has uncommitted or untracked files (%d): %s%s - an agent may still'
                ' be working there%s. %s.'
                % (path, len(work), '; '.join(work[:5]), ' ...' if len(work) > 5 else '',
                   ', and the tag would keep commits only' if 'tagged' in what else '', end))
        return REFUSED
    if ignored:
        ctx.say('refused   %s has %s. %s.' % (path, ignored_reason(ignored, path), end),
                '  rerun    %s' % rerun)
        return REFUSED
    return None


def clear_disposable(ctx, path):
    """Delete the untracked files `disposable_untracked` names, the verbs' last step before
    `git_remove`. True when none is left; False after saying which could not go, or that ignored
    files appeared since."""
    _, disposable, ignored, why = dirty(ctx, path)
    if disposable is None:
        ctx.say('failed    %s: its status could not be read again (%s); git was NOT run'
                % (path, why))
        return False
    if ignored:
        ctx.say('failed    %s now has %s; git was NOT run' % (path, ignored_reason(ignored, path)))
        return False
    for rel in disposable:
        try:
            os.remove(os.path.join(path, rel))
        except OSError as e:
            ctx.say('failed    could not delete %s (%s); git was NOT run' % (rel, e))
            return False
    if disposable:
        ctx.say('cleared   %d untracked file(s) that are not work (.kit.json worktree_remove.'
                'disposable_untracked): %s' % (len(disposable), '; '.join(disposable[:5])))
    return True


def last_look(ctx, path):
    """The status read `git_remove` makes just before git. True when `dirty` lists nothing at all;
    False after saying what appeared since the caller's checks, or that the status cannot be read.
    git's own refusal names modified and untracked files and never an ignored one, so this read is
    all that keeps an ignored file written late. It narrows that window and does not close it: an
    ignored file written after this returns is deleted with the tree."""
    work, disposable, ignored, why = dirty(ctx, path)
    if work is None:
        ctx.say('failed    %s: its status could not be read again (%s); git was NOT run'
                % (path, why))
        return False
    # disposable_untracked counts here: the callers that clear such files have done it already.
    lines = work + ['?? ' + rel for rel in disposable]
    if lines:
        reason = 'uncommitted or untracked files (%d): %s%s' % (
            len(lines), '; '.join(lines[:5]), ' ...' if len(lines) > 5 else '')
    elif ignored:
        reason = ignored_reason(ignored, path)
    else:
        return True
    ctx.say('failed    %s changed after it was checked - %s; git was NOT run' % (path, reason))
    return False


def git_remove(ctx, path, registered_path):
    """`last_look`, then `git worktree remove`. True when it removed the tree; False after saying
    why git was not run or what is left (intact, or the entry destroyed) - never retried, never
    forced."""
    if not last_look(ctx, path):
        return False
    rc, out, err = git(ctx.main, 'worktree', 'remove', registered_path)
    if rc == 0:
        ctx.say('removed   %s' % path)
        return True
    ctx.say('failed    git worktree remove exited %s: %s' % (rc, err or out))
    if '--force' in (err or out):
        ctx.say('  NOTE     that message is git\'s, and its advice is a trap here: `--force` is the',
                '           run that destroys the administrative entry when the tree cannot be',
                '           deleted. Commit or clear the modified files instead, then run this again.')
    try:
        still = canon(path) in registered(ctx.main)
    except GitReadError as e:
        ctx.say('  unknown  whether the administrative entry survived: the worktree list could not'
                ' be',
                '           read again (%s). Run `git worktree list` before anything else.' % e)
        return False
    if still:
        ctx.say('  intact   the worktree is still registered; nothing was destroyed')
    else:
        ctx.say('  DESTROYED the administrative entry is gone and the directory is still there.',
                '           A retry cannot work (`not a working tree`); the slug is burned until a',
                '           human clears it. Something took hold between the check and the removal.')
        ctx.describe(path, holders.blockers(path))
    return False


def remove(top, argument, keep_branch=False, cwd=None):
    """The plain verb: remove a LANDED worktree that nothing holds, then `git branch -d` its branch."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    return ctx.outcome(_remove(ctx, argument, keep_branch, branch_ok=True))


def _remove(ctx, argument, keep_branch, branch_ok=False):
    """The plain verb's checks. `branch_ok` (only `remove`) lets a tree on a branch outside the
    worktree folders through `outside_target`; the landing cleanup and classify leave it False."""
    found = registered_target(ctx, argument, branch_ok)
    if not isinstance(found, tuple):
        return found
    path, known = found
    key = canon(path)
    seen = {}
    refusal = merge_check(ctx, key, known[key][0], seen)
    if refusal:
        ctx.say(*refusal)
        if any('UNLANDED' in line for line in refusal):
            # The owner's printed commands are exactly as destructive on failure, so name any holder too.
            ctx.describe(path, holders.blockers(path))
            ctx.say('  note     release every holder first: the owner\'s `git worktree remove` above fails'
                    ' the same destructive way this tool refuses to risk.')
        return REFUSED
    # An outside tree, and a detached one anywhere: a commit made there and left by a checkout
    # lives only in its HEAD reflog, which `git worktree remove` deletes.
    logs = not is_workstream(ctx, path) or known[key][1] == '(detached)'
    if logs:
        # The HEAD merge_check found in the base: the reflog check and `unmoved` hold the tree to
        # it, so a HEAD moved since (to an unlanded tip) is refused, never judged afresh.
        head = seen.get('head')
        if not head:
            ctx.say('refused   %s: git reported no HEAD for it; nothing was removed' % path)
            return REFUSED
        logged = kept_reflog(ctx, path, head, False)
        if logged is None:
            return REFUSED
    why = holders.blockers(path)
    if why:
        ctx.say('refused   %s is held; git was NOT run' % path)
        ctx.describe(path, why)
        ctx.say('  kept     the worktree is still registered and intact, and still removable later')
        return REFUSED
    rerun = '%s %s%s' % (ctx.s['tool'], handle(ctx, known[key][0]),
                         ' --keep-branch' if keep_branch else '')
    if dirty_refusal(ctx, path, 'nothing was removed', rerun):
        return REFUSED
    why = holders.link_reason(path)
    if why:
        ctx.say('refused   %s: %s; nothing was removed' % (path, why))
        return REFUSED
    if not clear_disposable(ctx, path):
        return REFUSED
    if logs and not unmoved(ctx, path, head, logged):
        return REFUSED
    registered_path, branch = known[key]
    if not git_remove(ctx, path, registered_path):
        return REFUSED
    return delete_branch(ctx, None if branch == '(detached)' else branch, keep_branch)


def remove_branch(top, branch, cwd=None):
    """`<branch> --branch`: delete a LANDED local branch that has no worktree, with `git branch
    -d` (never -D). The remote branch is not touched."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    return ctx.outcome(_remove_branch(ctx, branch))


def _remove_branch(ctx, branch):
    """The plain verb's checks for a branch with no tree: not the main branch; present; checked out
    nowhere (a worktree on it is a session's, removed by its slug); in `<remote>/<main_branch>` as
    it stands (never fetched); no backlog record still pulled by it (an open claim, as the sweep
    reads it). Every refusal changes nothing."""
    main, s = ctx.main, ctx.s
    up = s['upstream']
    if same_name(branch, s['base']):
        ctx.say('refused   %s is the main branch; nothing was deleted' % branch)
        return REFUSED
    tip, bad = local_branch(main, branch)
    if bad:
        ctx.say('refused   %s; nothing was deleted' % bad)
        return REFUSED
    if tip is None:
        ctx.say('nothing   there is no local branch %s' % branch)
        return OK
    try:
        known = registrations(main)
    except GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s), so whether a worktree'
                ' holds it is unknown; nothing was deleted' % (branch, e))
        return REFUSED
    holding = sorted(os.path.normpath(p) for p, b in known.values() if same_name(b, branch))
    if holding:
        ctx.say('refused   branch %s is checked out at %s: remove that worktree instead (`%s'
                ' <slug>`); nothing was deleted' % (branch, ', '.join(holding), s['tool']))
        return REFUSED
    base, err = base_commit(ctx)
    if base is None:
        ctx.say('refused   %s: cannot read %s (%s), so whether it has landed is unknown; nothing'
                ' was deleted' % (branch, up, err))
        return REFUSED
    merged, why = refs.is_ancestor(tip, base, main)
    if merged is False:
        rc, count, _ = git(main, 'rev-list', '--count', '%s..%s' % (base, tip))
        ctx.say('refused   branch %s holds UNLANDED work: %s commit(s) not in %s %s; nothing was'
                ' deleted' % (branch, count if rc == 0 and count else 'an unknown number of',
                              up, base[:10]),
                '  abandon  the supported route, run on the owner\'s or the PM\'s ruling: it keeps'
                ' the commits under the tag %s%s (--tag %s<name> for the tag a ruling names),'
                ' then deletes the branch:' % (ARCHIVE, branch_slug(ctx, branch), ARCHIVE),
                '             %s %s --branch --abandon "<why>"' % (s['tool'], branch),
                '  owner    deleting unlanded work outright is the owner\'s own act, in the owner\'s'
                ' own terminal only: the Action Hub refuses it as a to-do, and this verb has no'
                ' override.',
                '  note     %s was read as it stands, not fetched: if the branch has landed'
                ' since, `git fetch %s` and run this again.' % (up, s['remote']))
        return REFUSED
    if merged is None:
        ctx.say('refused   %s: the merge check failed (%s); nothing was deleted' % (branch, why))
        return REFUSED
    ctx.say('merged    %s is in %s %s' % (branch, up, base[:10]))
    ids, why = pulled_by(ctx, base, branch)
    if ids is None:
        ctx.say('refused   %s: the open-claim check failed (%s); nothing was deleted'
                % (branch, why))
        return REFUSED
    if ids:
        ctx.say('refused   a backlog record is still pulled by %s (an open claim): %s; nothing was'
                ' deleted. Tell the coordinator, so it is closed or released first.'
                % (branch, ', '.join(ids)))
        return REFUSED
    return delete_branch(ctx, branch, False, removed=False)


# --- abandon ------------------------------------------------------------------------------------

def tag_tip(main, tag):
    """`(commit, None)` `refs/tags/<tag>` peels to, `('', None)` when there is no such tag, or
    `(None, why)` when git cannot tell.

    GOTCHA: this asked `rev-parse -q --verify`, whose "no such tag" is exit
    1 with no output, a killed git's shape (refs.KILLED_SHAPE): a kill read as a free tag name.
    refs.resolve(known=True) tells the two apart."""
    return refs.resolve('refs/tags/%s' % tag, main, known=True)


def make_tag(main, tag, tip, message):
    """An annotated, unsigned, local `tag` on `tip`, `message` verbatim. (rc, stderr)."""
    rc, out, err = git(main, '-c', 'tag.gpgSign=false', 'tag', '-a', '--cleanup=verbatim', '-F', '-',
                       tag, tip, stdin=message.encode('utf-8'))
    return rc, err or out


def tag_name_refusal(main, tag):
    """None when `tag` (an --abandon --tag) is an archive/ tag name git accepts, else why not."""
    if not tag.startswith(ARCHIVE) or len(tag) == len(ARCHIVE):
        return '--tag %s does not start with %s and name a tag after it' % (tag, ARCHIVE)
    args = ('check-ref-format', 'refs/tags/' + tag)
    rc, _, err = git(main, *args)
    if rc != 0:
        return '--tag %s is not a tag name git accepts (%s)' % (tag, failure(args, rc, err))
    return None


def pick_tag(ctx, slug, tip, same=None, named=None):
    """`(tag, commit it holds or '', None)`: archive/<slug>; with archive_next_free the first of
    archive/<slug>, -2, -3, ... that is free or already on `tip` (or on a commit `same` accepts).
    `named` (--tag) is taken as it is, never the next free one: the caller refuses it when it
    holds another commit. `(tag, None, why)` when git cannot tell what `tag` holds: never taken
    for a free name."""
    if named:
        held, why = tag_tip(ctx.main, named)
        return named, held, why
    tag = ARCHIVE + slug
    held, why = tag_tip(ctx.main, tag)
    if held is None or not ctx.s['archive_next_free']:
        return tag, held, why
    n = 1
    while held and held != tip and not (same and same(held)):
        n += 1
        tag = '%s%s-%d' % (ARCHIVE, slug, n)
        held, why = tag_tip(ctx.main, tag)
    return tag, held, why


def branch_tip(main, branch):
    """`(commit, None)` of `refs/heads/<branch>`, `('', None)` when there is no such branch, or
    `(None, why)` when git cannot tell (a kill is never "no branch": tag_tip's GOTCHA)."""
    return refs.resolve('refs/heads/' + branch, main, known=True)


def same_name(a, b):
    """Branch names git may treat as one: on a case-insensitive file system (core.ignorecase)
    `refs/heads/Main` opens the file of `refs/heads/main`."""
    return a.casefold() == b.casefold()


def local_branch(main, name):
    """`(tip, None)` for exactly the local branch `name`, `(None, None)` when there is none, or
    `(None, why)` refusing. `name` must come back unchanged from `git check-ref-format --branch`,
    so revision syntax (`main~1`, `main^`, `main@{1}`, `@{-1}`) is never looked up; the lookup is an
    exact match over `git for-each-ref refs/heads/`. A branch whose name differs from `name` only in
    case is refused, never resolved to: the delete would act on the typed name."""
    args = ('check-ref-format', '--branch', name)
    rc, out, err = git(main, *args)
    if rc != 0 or out != name:
        return None, '%s is not a branch name as typed (%s)' % (
            name, failure(args, rc, err) if rc != 0 else 'git reads it as %s' % (out or '?'))
    args = ('for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads/')
    rc, out, err = git(main, *args)
    if rc != 0:
        return None, 'the local branches could not be read (%s)' % failure(args, rc, err)
    tip, near = None, []
    for line in out.splitlines():
        sha, _, ref = line.partition(' ')
        have = ref[len('refs/heads/'):]
        if have == name:
            tip = sha
        elif same_name(have, name):
            near.append(have)
    if near:
        return None, ('git has %s, whose name and %s differ only in case (one ref on this file'
                      ' system); name the branch exactly as git lists it' % (', '.join(near), name))
    return tip, None


def pulled_by(ctx, rev, branch):
    """`(ids, None)`: backlog records `state: pulled` with `pulled_by: <branch>`, or `(None, why)`
    (pulled_index, for one branch)."""
    index, why = pulled_index(ctx, rev)
    if index is None:
        return None, why
    return index.get(branch, []), None


def pulled_index(ctx, rev):
    """`({branch: sorted ids}, None)` over every backlog record `state: pulled`, keyed by its
    `pulled_by`, or `(None, why)`: one store read for every branch. A main or folder store is read
    in the tree of `rev`; a ref or branch store at its local ref as it stands (never fetched; `rev`
    is not used), an absent ref holding no records."""
    try:
        backend = backlog_store.open_backend(ctx.main, ctx.cfg, fetch=False)
    except (BacklogFailure, ConfigError, KeyError) as e:
        return None, 'the backlog store cannot be opened (%s)' % e
    if isinstance(backend, backlog_store.MainBackend):
        records, _, why = backlog_store.records_in_tree(ctx.main, rev, backend.rel)
        where = '%s at %s' % (backend.rel, rev[:10])
    else:
        # refs.ref_commit: a git ended from outside is no answer, never "no store ref".
        tip, why = refs.ref_commit(backend.local_ref, ctx.main)
        if tip is None:
            return None, 'cannot read %s (%s)' % (backend.local_ref, why)
        if not tip:
            return {}, None
        records, _, why = backlog_store.records_in_tree(ctx.main, tip, backlog_store.STORE_DIR)
        where = '%s at %s' % (backend.local_ref, tip[:10])
    if records is None:
        return None, 'the backlog records %s cannot be read (%s)' % (where, why)
    unread = [r.stem for r in records if not r.readable]
    if unread:
        return None, 'unreadable backlog record(s) %s: %s' % (where, ', '.join(unread))
    index = {}
    for r in records:
        if r.get('state') == 'pulled' and r.get('pulled_by'):
            index.setdefault(r.get('pulled_by'), []).append(r.id)
    return {branch: sorted(ids) for branch, ids in index.items()}, None


def _shown(answer, none):
    """A tag_tip or branch_tip answer for a line: the short hash, `none` for no such ref, or that
    git could not tell."""
    sha, why = answer
    if sha is None:
        return '(unknown: git could not tell: %s)' % why
    return sha[:10] if sha else none


def left_after(ctx, path, branch, tag):
    now, held = branch_tip(ctx.main, branch), tag_tip(ctx.main, tag)
    ctx.say('  left     %s; branch %s kept at %s; tag %s holds %s. Nothing was'
            ' forced.' % ('worktree %s is gone' % path if path else 'there was no worktree',
                          branch, _shown(now, '(none)'), tag, _shown(held, '(nothing)')),
            '  next     tell the coordinator; do not run git branch -D')


def drop_branch(ctx, path, branch, tip, tag, keep):
    """Delete `branch` only while it still holds `tip` and `tag` still holds that tip (`update-ref
    -d <ref> <old>` is atomic). Never `branch -D`. Returns the exit status."""
    main = ctx.main
    if branch is None:
        ctx.say('branch    none: it was a detached HEAD')
        return OK
    if keep:
        ctx.say('kept      branch %s (--keep-branch); its commits are also under tag %s'
                % (branch, tag))
        return OK
    held, why = tag_tip(main, tag)
    if held != tip:
        ctx.say('failed    tag %s %s, so branch %s was not deleted'
                % (tag, 'no longer holds %s' % tip[:10] if held is not None else
                   'could not be read (%s)' % why, branch))
        left_after(ctx, path, branch, tag)
        return REFUSED
    rc, _, err = git(main, 'update-ref', '-d', 'refs/heads/' + branch, tip)
    if rc != 0:
        ctx.say('failed    could not delete branch %s (the delete holds it to the tagged %s): %s'
                % (branch, tip[:10], err or '?'))
        left_after(ctx, path, branch, tag)
        return REFUSED
    section = 'branch.' + branch
    if git(main, 'config', '--get-regexp', '^' + re.escape(section) + r'\.')[0] == 0:
        git(main, 'config', '--remove-section', section)
    ctx.say('deleted   branch %s (was %s); its commits are kept by tag %s' % (branch, tip[:10], tag))
    return OK


def abandon(top, argument, why, keep_branch=False, cwd=None, today=None, tag=None):
    """`--abandon <why>`: drop UNLANDED work, keeping it as the annotated tag archive/<slug>, or
    as `tag` (--tag archive/<name>, the tag a ruling names): made when absent, reused when it
    already holds the work, refused on any other commit."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    if tag is not None:
        bad = tag_name_refusal(ctx.main, tag)
        if bad:
            ctx.say('refused   %s; nothing was tagged or removed' % bad)
            return ctx.outcome(REFUSED)
    return ctx.outcome(_abandon(ctx, argument, why, keep_branch, today, tag))


def _abandon(ctx, argument, why, keep_branch, today, named=None, expect=None):
    """The abandon of one registered tree. `expect`, when given, is the commit the caller checked
    (the sweep's stale mark): a tree at any other commit, or one whose work is already in the base,
    is refused, never tagged."""
    found = registered_target(ctx, argument)
    if not isinstance(found, tuple):
        return found
    path, known = found
    main, s = ctx.main, ctx.s
    key = canon(path)
    registered_path = known[key][0]
    # The name git registered, not the one typed: the tag is the one the UNLANDED hint names.
    slug = os.path.basename(os.path.normpath(registered_path))
    up = s['upstream']
    try:
        branch, tip = checked_out(main, key)
    except GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s); nothing was tagged or'
                ' removed' % (path, e))
        return REFUSED
    if not tip:
        ctx.say('refused   %s: git reported no HEAD for it; nothing was tagged or removed' % path)
        return REFUSED
    if expect is not None and tip != expect:
        ctx.say('refused   %s: it is at %s, not the %s it was checked at; nothing was tagged or'
                ' removed' % (path, tip[:10], expect[:10]))
        return REFUSED
    name = branch or ('detached HEAD %s' % tip[:10])
    others = sorted(os.path.normpath(p) for k, (p, b) in known.items()
                    if branch and b == branch and k != key)
    if others:
        ctx.say('refused   branch %s is also checked out at %s; nothing was tagged or removed'
                % (branch, ', '.join(others)))
        return REFUSED
    base, err = base_commit(ctx)
    if base is None:
        ctx.say('refused   %s: cannot read %s (%s); nothing was tagged or removed' % (path, up, err))
        return REFUSED
    merged, why_not = refs.is_ancestor(tip, base, main)
    if merged and expect is not None:
        ctx.say('refused   %s: %s is already in %s %s, so it is no longer stale work; nothing was'
                ' tagged or removed' % (path, name, up, base[:10]))
        return REFUSED
    if merged:
        work = dirty(ctx, path)[0]
        if work:
            return _abandon_landed_dirty(ctx, path, registered_path, slug, branch, tip, name,
                                         len(work), why, keep_branch, today, named)
        ctx.say('refused   %s: %s is already in %s %s - nothing to abandon. Remove it'
                ' with `%s %s`; nothing was tagged or removed'
                % (path, name, up, base[:10], s['tool'], handle(ctx, registered_path)))
        return REFUSED
    if merged is None:
        ctx.say('refused   %s: the merge check failed (%s); nothing was tagged or removed'
                % (path, why_not))
        return REFUSED
    # An outside tree, and a detached one anywhere (the plain verb's rule).
    logs = not is_workstream(ctx, path) or not branch
    logged = kept_reflog(ctx, path, tip, True) if logs else None
    if logs and logged is None:
        return REFUSED
    rerun = '%s %s --abandon "<why>"%s%s' % (s['tool'], handle(ctx, registered_path),
                                             ' --tag %s' % named if named else '',
                                             ' --keep-branch' if keep_branch else '')
    if dirty_refusal(ctx, path, 'nothing was tagged or removed', rerun):
        return REFUSED
    why_link = holders.link_reason(path)
    if why_link:
        ctx.say('refused   %s: %s; nothing was tagged or removed' % (path, why_link))
        return REFUSED
    held = holders.blockers(path)
    if held:
        ctx.say('refused   %s is held; nothing was tagged and git was NOT run' % path)
        ctx.describe(path, held)
        return REFUSED
    tag, existing, why_tag = pick_tag(ctx, slug, tip, named=named)
    if existing is None:
        ctx.say('refused   %s: cannot read tag %s (%s); nothing was tagged or removed'
                % (path, tag, why_tag))
        return REFUSED
    if existing and existing != tip:
        ctx.say('refused   tag %s already exists on %s, not on %s (%s); nothing was tagged or'
                ' removed' % (tag, existing[:10], tip[:10], name))
        return REFUSED
    if existing:
        ctx.say('reused    tag %s already holds %s (%s)'
                % (tag, tip[:10], 'the tag --tag names' if named else 'a previous run made it'))
    else:
        message = 'abandoned: %s\n\n%s at %s\nworktree %s\ndate %s\n' % (
            why, ('branch ' + branch) if branch else 'detached HEAD', tip,
            os.path.normpath(registered_path), (today or datetime.date.today()).isoformat())
        rc, err = make_tag(main, tag, tip, message)
        if rc != 0:
            ctx.say('refused   could not create tag %s: git tag exited %s: %s; nothing was removed'
                    % (tag, rc, err or '?'))
            return REFUSED
        held, why_tag = tag_tip(main, tag)
        if held != tip:
            ctx.say('refused   tag %s %s after it was made; nothing was removed'
                    % (tag, 'does not hold %s' % tip[:10] if held is not None else
                       'could not be read (%s)' % why_tag))
            return REFUSED
        ctx.say('tagged    %s -> %s (%s): abandoned: %s' % (tag, tip[:10], name, why))
    if (not clear_disposable(ctx, path)
            or (logs and not unmoved(ctx, path, tip, logged))
            or not git_remove(ctx, path, registered_path)):
        ctx.say('  kept     tag %s stays on %s%s. Once the cause is cleared, the same command'
                ' reruns and reuses the tag.' % (tag, tip[:10],
                                                 ', and branch %s' % branch if branch else ''))
        return REFUSED
    status = drop_branch(ctx, path, branch, tip, tag, keep_branch)
    if branch:
        report_pulls(ctx, base, tip, branch)
    return status


def snapshot_tree(path):
    """`(tree, None)`: the tree of the worktree at `path` as it stands (tracked changes, staged or
    not, and untracked files that are not ignored), written through a throwaway index so neither
    the files nor the worktree's own index change; `(None, why)` when git fails."""
    folder = kuser.mkdtemp('kit-abandon-')
    env = dict(git_env(), GIT_INDEX_FILE=os.path.join(folder, 'index'))
    try:
        for args in (('read-tree', 'HEAD'), ('add', '-A', '--', '.'), ('write-tree',)):
            rc, out, err = git(path, *args, env=env)
            if rc != 0 or (args == ('write-tree',) and not out):
                return None, failure(args, rc, err)
        return out, None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def saved_work(main, commit, tree, tip):
    """True when `commit` is exactly `tree` on the one parent `tip`: a previous run's saved work."""
    rc, out, _ = git(main, 'log', '-1', '--format=%T %P', commit)
    return rc == 0 and out == '%s %s' % (tree, tip)


def _abandon_landed_dirty(ctx, path, registered_path, slug, branch, tip, name, count, why,
                          keep_branch, today, named=None):
    """`--abandon` on a LANDED tree that holds uncommitted work (the plain verb refuses the work,
    and the commits are in the base: each verb used to name the other). Every check of the plain
    verb runs first; then the work is saved as a commit on `tip` under the annotated tag
    archive/<slug>, the tree is reset to its HEAD (`reset --hard`, `clean -d`, never -x: build
    output goes with the tree as in the plain verb) only while it still matches the saved commit,
    and removed; the branch goes with `git branch -d`. A file written between that last match and
    the reset is lost: the window is narrowed, not closed."""
    main, s = ctx.main, ctx.s
    nothing = 'nothing was tagged or removed'
    logs = not is_workstream(ctx, path) or not branch
    logged = kept_reflog(ctx, path, tip, False) if logs else None
    if logs and logged is None:
        return REFUSED
    _, _, ignored, why_not = dirty(ctx, path)
    if ignored is None:
        ctx.say('refused   %s: its status could not be read (%s); %s' % (path, why_not, nothing))
        return REFUSED
    if ignored:
        ctx.say('refused   %s has %s. %s.' % (path, ignored_reason(ignored, path),
                                             nothing[0].upper() + nothing[1:]),
                '  rerun    %s %s --abandon "<why>"%s%s' % (
                    s['tool'], handle(ctx, registered_path),
                    ' --tag %s' % named if named else '',
                    ' --keep-branch' if keep_branch else ''))
        return REFUSED
    why_link = holders.link_reason(path)
    if why_link:
        ctx.say('refused   %s: %s; %s' % (path, why_link, nothing))
        return REFUSED
    held = holders.blockers(path)
    if held:
        ctx.say('refused   %s is held; nothing was tagged and git was NOT run' % path)
        ctx.describe(path, held)
        return REFUSED
    if not clear_disposable(ctx, path):
        return REFUSED
    tree, why_tree = snapshot_tree(path)
    if tree is None:
        ctx.say('refused   %s: its uncommitted work could not be read (%s); %s'
                % (path, why_tree, nothing))
        return REFUSED

    def same(commit):
        return saved_work(main, commit, tree, tip)

    tag, existing, why_tag = pick_tag(ctx, slug, None, same, named=named)
    if existing is None:
        ctx.say('refused   %s: cannot read tag %s (%s); %s' % (path, tag, why_tag, nothing))
        return REFUSED
    if existing and not same(existing):
        ctx.say('refused   tag %s already exists on %s, not on the uncommitted work of %s; %s'
                % (tag, existing[:10], name, nothing))
        return REFUSED
    if existing:
        commit = existing
        ctx.say('reused    tag %s already holds this work (%s)'
                % (tag, 'the tag --tag names' if named else 'a previous run made it'))
    else:
        message = 'abandoned: %s\n\nuncommitted work on %s at %s\nworktree %s\ndate %s\n' % (
            why, ('branch ' + branch) if branch else 'detached HEAD', tip,
            os.path.normpath(registered_path), (today or datetime.date.today()).isoformat())
        rc, commit, err = git(main, 'commit-tree', tree, '-p', tip, '-F', '-',
                              stdin=message.encode('utf-8'))
        if rc != 0 or not commit:
            ctx.say('refused   %s: could not commit its uncommitted work (%s); %s'
                    % (path, failure(('commit-tree',), rc, err), nothing))
            return REFUSED
        rc, err = make_tag(main, tag, commit, message)
        if rc != 0:
            ctx.say('refused   could not create tag %s: git tag exited %s: %s; nothing was removed'
                    % (tag, rc, err or '?'))
            return REFUSED
        got, why_tag = tag_tip(main, tag)
        if got != commit:
            ctx.say('refused   tag %s %s after it was made; nothing was removed'
                    % (tag, 'does not hold %s' % commit[:10] if got is not None else
                       'could not be read (%s)' % why_tag))
            return REFUSED
    ctx.say('saved     uncommitted work (%d) of %s as %s under tag %s (on %s, already in %s);'
            ' see it: git diff %s %s' % (count, path, commit[:10], tag, tip[:10], s['upstream'],
                                        tip[:10], tag))
    kept = ('  kept     tag %s holds the saved work%s. Nothing was forced.'
            % (tag, ', and branch %s' % branch if branch else ''))
    if logs and not unmoved(ctx, path, tip, logged):
        ctx.say(kept)
        return REFUSED
    now, why_now = snapshot_tree(path)
    head = git(path, 'rev-parse', 'HEAD')[1]
    if now != tree or head != tip:
        ctx.say('failed    %s changed after its work was saved (%s); it was NOT reset or removed'
                % (path, why_now or ('HEAD is %s' % head[:10] if head != tip else
                                     'the files differ')), kept)
        return REFUSED
    for args in (('reset', '-q', '--hard', 'HEAD'), ('clean', '-q', '-f', '-d')):
        rc, _, err = git(path, *args)
        if rc != 0:
            ctx.say('failed    %s: %s; git worktree remove was NOT run'
                    % (path, failure(args, rc, err)), kept)
            return REFUSED
    ctx.say('reset     %s to %s (its uncommitted work is under tag %s)' % (path, tip[:10], tag))
    if not git_remove(ctx, path, registered_path):
        ctx.say(kept)
        return REFUSED
    return delete_branch(ctx, branch, keep_branch)


def branch_slug(ctx, branch):
    """The tag name's tail: `branch` with `branch_template`'s prefix and suffix cut off when it
    fits the template, else the whole branch name."""
    for template in ctx.s['branch_template']:
        pre, post = template.split('{slug}', 1)
        if (branch.startswith(pre) and branch.endswith(post)
                and len(branch) > len(pre) + len(post)):
            return branch[len(pre):len(branch) - len(post)]
    return branch


def abandon_branch(top, branch, why, cwd=None, today=None, tag=None):
    """`<branch> --branch --abandon <why>`: drop an UNLANDED local branch that has no worktree,
    keeping its commits as the annotated tag archive/<slug>, or as `tag` (--tag, as abandon).
    The remote branch is not touched."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    if tag is not None:
        bad = tag_name_refusal(ctx.main, tag)
        if bad:
            ctx.say('refused   %s; nothing was tagged or deleted' % bad)
            return ctx.outcome(REFUSED)
    return ctx.outcome(_abandon_branch(ctx, branch, why, today, tag))


def _abandon_branch(ctx, branch, why, today, named=None):
    main, s = ctx.main, ctx.s
    up = s['upstream']
    if same_name(branch, s['base']):
        ctx.say('refused   %s is the main branch; nothing was tagged or deleted' % branch)
        return REFUSED
    tip, bad = local_branch(main, branch)
    if bad:
        ctx.say('refused   %s; nothing was tagged or deleted' % bad)
        return REFUSED
    if tip is None:
        ctx.say('refused   there is no local branch %s; nothing was tagged or deleted' % branch)
        return REFUSED
    try:
        known = registrations(main)
    except GitReadError as e:
        ctx.say('refused   %s: the worktree list could not be read (%s), so whether a worktree'
                ' holds it is unknown; nothing was tagged or deleted' % (branch, e))
        return REFUSED
    holding = sorted(os.path.normpath(p) for p, b in known.values() if same_name(b, branch))
    if holding:
        ctx.say('refused   branch %s is checked out at %s: abandon that worktree instead (`%s'
                ' <slug> --abandon "<why>"`); nothing was tagged or deleted'
                % (branch, ', '.join(holding), s['tool']))
        return REFUSED
    base, err = base_commit(ctx)
    if base is None:
        ctx.say('refused   %s: cannot read %s (%s); nothing was tagged or deleted'
                % (branch, up, err))
        return REFUSED
    merged, why_not = refs.is_ancestor(tip, base, main)
    if merged:
        ctx.say('refused   %s is already in %s %s - nothing to abandon. Delete it with `%s %s'
                ' --branch` (or the landing cleanup, land.py --sweep-landed); nothing was tagged'
                ' or deleted' % (branch, up, base[:10], s['tool'], branch))
        return REFUSED
    if merged is None:
        ctx.say('refused   %s: the merge check failed (%s); nothing was tagged or deleted'
                % (branch, why_not))
        return REFUSED
    tag, existing, why_tag = pick_tag(ctx, branch_slug(ctx, branch), tip, named=named)
    if existing is None:
        ctx.say('refused   %s: cannot read tag %s (%s); nothing was tagged or deleted'
                % (branch, tag, why_tag))
        return REFUSED
    if existing and existing != tip:
        ctx.say('refused   tag %s already exists on %s, not on %s (%s); nothing was tagged or'
                ' deleted' % (tag, existing[:10], tip[:10], branch))
        return REFUSED
    if existing:
        ctx.say('reused    tag %s already holds %s (%s)'
                % (tag, tip[:10], 'the tag --tag names' if named else 'a previous run made it'))
    else:
        message = 'abandoned: %s\n\nbranch %s at %s\nno worktree\ndate %s\n' % (
            why, branch, tip, (today or datetime.date.today()).isoformat())
        rc, err = make_tag(main, tag, tip, message)
        if rc != 0:
            ctx.say('refused   could not create tag %s: git tag exited %s: %s; nothing was deleted'
                    % (tag, rc, err or '?'))
            return REFUSED
        held, why_tag = tag_tip(main, tag)
        if held != tip:
            ctx.say('refused   tag %s %s after it was made; nothing was deleted'
                    % (tag, 'does not hold %s' % tip[:10] if held is not None else
                       'could not be read (%s)' % why_tag))
            return REFUSED
        ctx.say('tagged    %s -> %s (%s): abandoned: %s' % (tag, tip[:10], branch, why))
    status = drop_branch(ctx, None, branch, tip, tag, False)
    report_pulls(ctx, base, tip, branch)
    return status


def report_pulls(ctx, base, tip, branch):
    """Backlog records the abandoned branch still pulls: on the base (or the store), a dead pull
    the coordinator releases; on the branch's own tip only (a main or folder store), nothing to do.
    Reported, never refused."""
    up = ctx.s['upstream']
    on_base, why = pulled_by(ctx, base, branch)
    if on_base is None:
        ctx.say('note      backlog records pulled by %s were not checked: %s' % (branch, why))
        return
    for record in on_base:
        ctx.say('warning   backlog record %s on %s is pulled by %s: a dead pull now the branch is'
                ' gone. Tell the coordinator, so it is released in a records commit.'
                % (record, up, branch))
    try:
        backend = backlog_store.open_backend(ctx.main, ctx.cfg, fetch=False)
    except (BacklogFailure, ConfigError, KeyError):
        return
    if not isinstance(backend, backlog_store.MainBackend):
        return
    on_tip, _ = pulled_by(ctx, tip, branch)
    for record in on_tip or []:
        if record not in on_base:
            ctx.say('note      backlog record %s was pulled on %s only: the copy on %s was'
                    ' never pulled, so there is nothing to release' % (record, branch, up))
