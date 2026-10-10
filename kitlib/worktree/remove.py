"""Remove a workstream's worktree only when its work has landed and nothing holds it, or abandon
unlanded work under a tag (ported from the PM kit's tools/worktree_remove.py;
docs/worktree-remove.md). tools/worktree_remove.py is the entry point.

`remove(top, argument)`, `abandon(top, argument, why)` and `check(top)` act on the MAIN checkout of
the repository `top` is in (the main checkout or any linked worktree) and return Outcome(code,
lines). Nothing here prints. The checks, in order, are the PM tool's:

- the target: under `"repo".worktrees_dir`, not `"worktree_remove".exempt`, present, registered
  (a stump is named and never deleted), and on `branch_template`'s branch when one is set;
- landed: the branch (or detached HEAD) is an ancestor of `<remote>/<main_branch>` AS IT STANDS,
  never fetched (`merge_check`); UNLANDED work is the owner's to remove and the refusal prints his
  commands and the `--abandon` verb, running none of them;
- holders (`holders.blockers`), then work (`dirty`: anything uncommitted or untracked, and any
  git-IGNORED file outside `build_output` or a keeper inside it), then links or junctions inside;
- only then the `disposable_untracked` files are deleted, `git worktree remove` runs (never
  forced, never retried) and the branch goes with `git branch -d` (never -D).

`remove_branch` deletes a LANDED local branch that no worktree has checked out and no backlog record
still pulls, with `git branch -d` (never -D).

`abandon` tags the tip `archive/<slug>` (annotated, `abandoned: <why>`, local), every refusal
running before the tag, then removes the tree and deletes the branch with `git update-ref -d <ref>
<tip>` while the tag still holds that tip. Backlog records still pulled by the branch are reported.

Exit codes: OK 0; REFUSED 1 (refused, a leftover exists, git failed, or `git branch -d` refused
after the removal); CONFIG 2 (the kit configuration was refused).
"""
import collections
import datetime
import fnmatch
import os
import re

from kitlib.backlog import store as backlog_store
from kitlib.backlog.errors import Failure as BacklogFailure
from kitlib.config import ConfigError, load
from kitlib.git import cmd

from . import holders
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


def settings(main, cfg):
    """This repository's values (the schema has already checked their shapes)."""
    wr = cfg.section(TOOL)
    tools = cfg.tools_dir.replace('\\', '/').rstrip('/')
    return {
        'base': cfg.main_branch, 'remote': cfg.remote,
        'upstream': '%s/%s' % (cfg.remote, cfg.main_branch),
        'worktrees_dir': os.path.normpath(cfg.repo['worktrees_dir']),
        'tool': 'python %s/worktree_remove.py' % tools,
        'name': cfg.repo.get('name') or os.path.basename(os.path.normpath(main)),
        'exempt': dict(wr.get('exempt') or {}),
        'disposable_untracked': list(wr.get('disposable_untracked') or []),
        'build_output': list(wr.get('build_output') or []),
        'keepers': list(KEEPERS) + list(wr.get('keepers') or []),
        'archive_next_free': bool(wr.get('archive_next_free')),
        'branch_template': wr.get('branch_template'),
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


def merge_check(ctx, key, registered_path):
    """None when the worktree's work is already in the base, else the refusal lines. The base is
    read as it stands, never fetched; a failure of the check itself refuses with no commands."""
    s, main = ctx.s, ctx.main
    up = s['upstream']
    registered_path = os.path.normpath(registered_path)
    try:
        branch, head = checked_out(main, key)
    except GitReadError as e:
        return ['refused   %s: the worktree list could not be read (%s), so whether its work has'
                ' landed is unknown; nothing was removed' % (registered_path, e)]
    tip = ('refs/heads/' + branch) if branch else head
    name = branch or ('detached HEAD %s' % (head or '?'))
    if not tip:
        return ['refused   %s: git reported no HEAD for it, so whether its work has landed is'
                ' unknown; nothing was removed' % registered_path]
    base, why = base_commit(ctx)
    if base is None:
        return ['refused   %s: cannot read %s (%s), so whether %s has landed is'
                ' unknown; nothing was removed' % (registered_path, up, why, name)]
    rc, _, err = git(main, 'merge-base', '--is-ancestor', tip, base)
    if rc == 0:
        ctx.say('merged    %s is in %s %s' % (name, up, base[:10]))
        return None
    if rc != 1:
        return ['refused   %s: the merge check failed (git merge-base exited %s: %s); nothing was'
                ' removed' % (registered_path, rc, err or '?')]
    rc, count, err = git(main, 'rev-list', '--count', '%s..%s' % (base, tip))
    count = count if rc == 0 and count else 'an unknown number of'
    slug = os.path.basename(registered_path)
    lines = ['refused   %s holds UNLANDED work: %s has %s commit(s) not in %s %s;'
             ' nothing was removed' % (registered_path, name, count, up, base[:10]),
             '  owner    removing unlanded work is the owner\'s standing rule that deleting data'
             ' is his, and this verb has no override. If he chooses to discard it, he runs,',
             '           from %s:' % main,
             '             git worktree remove "%s"' % registered_path]
    if branch:
        lines.append('             git branch -D %s' % branch)
    lines.append('  abandon  to drop it but keep its commits under the tag %s%s, run instead:'
                 % (ARCHIVE, slug))
    lines.append('             %s %s --abandon "<why>"' % (s['tool'], slug))
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
    home = home_of(ctx)
    stumps = []
    for name in sorted(os.listdir(home)) if os.path.isdir(home) else []:
        path = os.path.join(home, name)
        if os.path.isdir(path) and canon(path) not in known and not exemption(ctx.s, path):
            stumps.append(path)
    prefix = canon(home) + os.sep
    orphans = [p for p in sorted(known) if p.startswith(prefix)
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
        if not canon(path).startswith(home + os.sep) or canon(path) in skipped \
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
                   os.path.basename(path), ' and its branch' if branch else ''))
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


def registered_target(ctx, argument):
    """`(path, registrations)` for the registered workstream worktree `argument` names, or the exit
    status after saying why there is none (absent: 0)."""
    s = ctx.s
    path = target_path(ctx, argument)
    home = home_of(ctx)
    if not canon(path).startswith(canon(home) + os.sep):
        ctx.say('refused   %s is not under %s; this tool removes workstream worktrees only'
                % (path, home))
        return REFUSED
    if exemption(s, path):
        ctx.say('refused   %s is exempt (%s): this tool never removes it; nothing was touched'
                % (path, exemption(s, path)))
        return REFUSED
    if not os.path.isdir(path):
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
    template = s['branch_template']
    if template:
        registered_path, branch = known[canon(path)]
        want = template.replace('{slug}', os.path.basename(os.path.normpath(registered_path)))
        if branch != want:
            ctx.say('refused   %s is on %s, not %s: this repository removes only worktrees on'
                    ' their own %s branch (.kit.json worktree_remove.branch_template); nothing was'
                    ' removed' % (path, branch, want, template))
            return REFUSED
    return path, known


def dirty(ctx, path):
    """`(work, disposable, ignored, None)`: status lines that are work, the untracked files
    `disposable_untracked` names, and `!! ` lines of ignored files outside build output (or keepers
    inside it); `(None, None, None, why)` when the status cannot be read (never a clean tree)."""
    s = ctx.s
    globs, build = s['disposable_untracked'], s['build_output']
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
            if rel.startswith('"') or not holders.is_build_output(rel, build):
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
    """Delete the untracked files `disposable_untracked` names, the last step before git. True when
    none is left; False after saying which could not go, or that ignored files appeared since."""
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


def git_remove(ctx, path, registered_path):
    """`git worktree remove`, every check made. True when it removed the tree; False after saying
    what is left (intact, or the entry destroyed) - never retried, never forced."""
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
    return ctx.outcome(_remove(ctx, argument, keep_branch))


def _remove(ctx, argument, keep_branch):
    found = registered_target(ctx, argument)
    if not isinstance(found, tuple):
        return found
    path, known = found
    key = canon(path)
    refusal = merge_check(ctx, key, known[key][0])
    if refusal:
        ctx.say(*refusal)
        if any('UNLANDED' in line for line in refusal):
            # His printed commands are exactly as destructive on failure, so name any holder too.
            ctx.describe(path, holders.blockers(path))
            ctx.say('  note     release every holder first: his `git worktree remove` above fails'
                    ' the same destructive way this tool refuses to risk.')
        return REFUSED
    why = holders.blockers(path)
    if why:
        ctx.say('refused   %s is held; git was NOT run' % path)
        ctx.describe(path, why)
        ctx.say('  kept     the worktree is still registered and intact, and still removable later')
        return REFUSED
    rerun = '%s %s%s' % (ctx.s['tool'], os.path.basename(os.path.normpath(known[key][0])),
                         ' --keep-branch' if keep_branch else '')
    if dirty_refusal(ctx, path, 'nothing was removed', rerun):
        return REFUSED
    why = holders.link_reason(path)
    if why:
        ctx.say('refused   %s: %s; nothing was removed' % (path, why))
        return REFUSED
    if not clear_disposable(ctx, path):
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
    rc, _, err = git(main, 'merge-base', '--is-ancestor', tip, base)
    if rc == 1:
        rc, count, _ = git(main, 'rev-list', '--count', '%s..%s' % (base, tip))
        ctx.say('refused   branch %s holds UNLANDED work: %s commit(s) not in %s %s; nothing was'
                ' deleted' % (branch, count if rc == 0 and count else 'an unknown number of',
                              up, base[:10]),
                '  abandon  to drop it but keep its commits under a tag, run instead:',
                '             %s %s --branch --abandon "<why>"' % (s['tool'], branch),
                '  note     %s was read as it stands, not fetched: if the branch has landed'
                ' since, `git fetch %s` and run this again.' % (up, s['remote']))
        return REFUSED
    if rc != 0:
        ctx.say('refused   %s: the merge check failed (git merge-base exited %s: %s); nothing was'
                ' deleted' % (branch, rc, err or '?'))
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
    """The commit `refs/tags/<tag>` peels to, or None."""
    rc, out, _ = git(main, 'rev-parse', '-q', '--verify', 'refs/tags/%s^{commit}' % tag)
    return out if rc == 0 and out else None


def make_tag(main, tag, tip, message):
    """An annotated, unsigned, local `tag` on `tip`, `message` verbatim. (rc, stderr)."""
    rc, out, err = git(main, '-c', 'tag.gpgSign=false', 'tag', '-a', '--cleanup=verbatim', '-F', '-',
                       tag, tip, stdin=message.encode('utf-8'))
    return rc, err or out


def pick_tag(ctx, slug, tip):
    """`(tag, commit it holds or None)`: archive/<slug>; with archive_next_free the first of
    archive/<slug>, -2, -3, ... that is free or already on `tip`."""
    tag = ARCHIVE + slug
    held = tag_tip(ctx.main, tag)
    if not ctx.s['archive_next_free']:
        return tag, held
    n = 1
    while held is not None and held != tip:
        n += 1
        tag = '%s%s-%d' % (ARCHIVE, slug, n)
        held = tag_tip(ctx.main, tag)
    return tag, held


def branch_tip(main, branch):
    rc, out, _ = git(main, 'rev-parse', '-q', '--verify', 'refs/heads/' + branch)
    return out if rc == 0 and out else None


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
    """`(ids, None)`: backlog records `state: pulled` with `pulled_by: <branch>`, or `(None, why)`.
    A main or folder store is read in the tree of `rev`; a ref or branch store at its local ref as
    it stands (never fetched; `rev` is not used), an absent ref holding no records."""
    try:
        backend = backlog_store.open_backend(ctx.main, ctx.cfg, fetch=False)
    except (BacklogFailure, ConfigError, KeyError) as e:
        return None, 'the backlog store cannot be opened (%s)' % e
    if isinstance(backend, backlog_store.MainBackend):
        records, _, why = backlog_store.records_in_tree(ctx.main, rev, backend.rel)
        where = '%s at %s' % (backend.rel, rev[:10])
    else:
        rc, tip, err = git(ctx.main, 'rev-parse', '--verify', '--quiet',
                           backend.local_ref + '^{commit}')
        if rc == 1 and not tip and not err:
            return [], None
        if rc != 0 or not tip:
            return None, 'cannot read %s (%s)' % (backend.local_ref, failure(
                ('rev-parse', backend.local_ref), rc, err))
        records, _, why = backlog_store.records_in_tree(ctx.main, tip, backlog_store.STORE_DIR)
        where = '%s at %s' % (backend.local_ref, tip[:10])
    if records is None:
        return None, 'the backlog records %s cannot be read (%s)' % (where, why)
    unread = [r.stem for r in records if not r.readable]
    if unread:
        return None, 'unreadable backlog record(s) %s: %s' % (where, ', '.join(unread))
    return sorted(r.id for r in records
                  if r.get('state') == 'pulled' and r.get('pulled_by') == branch), None


def left_after(ctx, path, branch, tag):
    now, held = branch_tip(ctx.main, branch), tag_tip(ctx.main, tag)
    ctx.say('  left     %s; branch %s kept at %s; tag %s holds %s. Nothing was'
            ' forced.' % ('worktree %s is gone' % path if path else 'there was no worktree',
                          branch, now[:10] if now else '(none)', tag,
                          held[:10] if held else '(nothing)'),
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
    if tag_tip(main, tag) != tip:
        ctx.say('failed    tag %s no longer holds %s, so branch %s was not deleted'
                % (tag, tip[:10], branch))
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


def abandon(top, argument, why, keep_branch=False, cwd=None, today=None):
    """`--abandon <why>`: drop UNLANDED work, keeping it as the annotated tag archive/<slug>."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    return ctx.outcome(_abandon(ctx, argument, why, keep_branch, today))


def _abandon(ctx, argument, why, keep_branch, today):
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
    rc, _, err = git(main, 'merge-base', '--is-ancestor', tip, base)
    if rc == 0:
        ctx.say('refused   %s: %s is already in %s %s - nothing to abandon. Remove it'
                ' with `%s %s`; nothing was tagged or removed'
                % (path, name, up, base[:10], s['tool'], slug))
        return REFUSED
    if rc != 1:
        ctx.say('refused   %s: the merge check failed (git merge-base exited %s: %s); nothing was'
                ' tagged or removed' % (path, rc, err or '?'))
        return REFUSED
    rerun = '%s %s --abandon "<why>"%s' % (s['tool'], slug, ' --keep-branch' if keep_branch else '')
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
    tag, existing = pick_tag(ctx, slug, tip)
    if existing and existing != tip:
        ctx.say('refused   tag %s already exists on %s, not on %s (%s); nothing was tagged or'
                ' removed' % (tag, existing[:10], tip[:10], name))
        return REFUSED
    if existing:
        ctx.say('reused    tag %s already holds %s (a previous run made it)' % (tag, tip[:10]))
    else:
        message = 'abandoned: %s\n\n%s at %s\nworktree %s\ndate %s\n' % (
            why, ('branch ' + branch) if branch else 'detached HEAD', tip,
            os.path.normpath(registered_path), (today or datetime.date.today()).isoformat())
        rc, err = make_tag(main, tag, tip, message)
        if rc != 0:
            ctx.say('refused   could not create tag %s: git tag exited %s: %s; nothing was removed'
                    % (tag, rc, err or '?'))
            return REFUSED
        if tag_tip(main, tag) != tip:
            ctx.say('refused   tag %s does not hold %s after it was made; nothing was removed'
                    % (tag, tip[:10]))
            return REFUSED
        ctx.say('tagged    %s -> %s (%s): abandoned: %s' % (tag, tip[:10], name, why))
    if not clear_disposable(ctx, path) or not git_remove(ctx, path, registered_path):
        ctx.say('  kept     tag %s stays on %s%s. Once the cause is cleared, the same command'
                ' reruns and reuses the tag.' % (tag, tip[:10],
                                                 ', and branch %s' % branch if branch else ''))
        return REFUSED
    status = drop_branch(ctx, path, branch, tip, tag, keep_branch)
    if branch:
        report_pulls(ctx, base, tip, branch)
    return status


def branch_slug(ctx, branch):
    """The tag name's tail: `branch` with `branch_template`'s prefix and suffix cut off when it
    fits the template, else the whole branch name."""
    template = ctx.s['branch_template']
    if template and '{slug}' in template:
        pre, post = template.split('{slug}', 1)
        if (branch.startswith(pre) and branch.endswith(post)
                and len(branch) > len(pre) + len(post)):
            return branch[len(pre):len(branch) - len(post)]
    return branch


def abandon_branch(top, branch, why, cwd=None, today=None):
    """`<branch> --branch --abandon <why>`: drop an UNLANDED local branch that has no worktree,
    keeping its commits as the annotated tag archive/<slug>. The remote branch is not touched."""
    ctx, refused = open_ctx(top, cwd)
    if refused:
        return refused
    return ctx.outcome(_abandon_branch(ctx, branch, why, today))


def _abandon_branch(ctx, branch, why, today):
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
    rc, _, err = git(main, 'merge-base', '--is-ancestor', tip, base)
    if rc == 0:
        ctx.say('refused   %s is already in %s %s - nothing to abandon. Delete it with `%s %s'
                ' --branch` (or the landing cleanup, land.py --sweep-landed); nothing was tagged'
                ' or deleted' % (branch, up, base[:10], s['tool'], branch))
        return REFUSED
    if rc != 1:
        ctx.say('refused   %s: the merge check failed (git merge-base exited %s: %s); nothing was'
                ' tagged or deleted' % (branch, rc, err or '?'))
        return REFUSED
    tag, existing = pick_tag(ctx, branch_slug(ctx, branch), tip)
    if existing and existing != tip:
        ctx.say('refused   tag %s already exists on %s, not on %s (%s); nothing was tagged or'
                ' deleted' % (tag, existing[:10], tip[:10], branch))
        return REFUSED
    if existing:
        ctx.say('reused    tag %s already holds %s (a previous run made it)' % (tag, tip[:10]))
    else:
        message = 'abandoned: %s\n\nbranch %s at %s\nno worktree\ndate %s\n' % (
            why, branch, tip, (today or datetime.date.today()).isoformat())
        rc, err = make_tag(main, tag, tip, message)
        if rc != 0:
            ctx.say('refused   could not create tag %s: git tag exited %s: %s; nothing was deleted'
                    % (tag, rc, err or '?'))
            return REFUSED
        if tag_tip(main, tag) != tip:
            ctx.say('refused   tag %s does not hold %s after it was made; nothing was deleted'
                    % (tag, tip[:10]))
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
