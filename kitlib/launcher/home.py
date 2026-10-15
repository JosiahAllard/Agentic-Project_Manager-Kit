"""A launched session's home worktree (docs/launcher.md "The home worktree").

With `--home-worktree`, a session whose folder is a repository's MAIN checkout starts instead in
`<main>/<the host's worktrees folder>/home-<session name>`: a linked worktree of the same repository, detached at the
main checkout's HEAD and held with `git worktree lock`. A relative path any process of the session
resolves (a .NET relative write the guard cannot see, a subagent's shell: subagents start in the
session's folder) then lands in that worktree, never in the main checkout. The worktree carries
the tracked CLAUDE.md, AGENTS.md and .claude/ files, and the host keys auto memory by repository,
so a session keeps them.

  folder()             the host's worktrees folder (hosts.json), read when called, never at import
  state_folder()       the host's state folder (hosts.json), the same way
  home_path(main, n)   the home worktree's folder of the session named n: one home per session name
  ensure(main, path)   create it, lock it, or move a clean one to the main checkout's HEAD; the
                       lines it says, or a LaunchError (nothing created)

A home belongs to one session name, and the launcher refuses to start a name that is already
running (kitlib.launcher.start home_folder), so ensure() never moves a home under a running
session. Detached and locked: the nightly sweep never takes one (kitlib.worktree.sweep sweep_skip), and
`git worktree remove` refuses a locked worktree. A dirty home (a stray write landed there) is left
as it is and named, never cleaned.
"""
import os
import re

from kitlib.git import cmd
from kitlib.host import registry
from kitlib.launcher.settings import LaunchError

PREFIX = 'home-'
LOCK_REASON = 'launcher home worktree of %s: start folder of launched sessions'
_BAD = re.compile(r'[^a-z0-9._-]+')
SHOWN = 10          # dirty files named, at most


def _profile():
    """The default host's profile; a hosts.json it cannot read is a LaunchError, not a traceback."""
    try:
        return registry.default_profile()
    except registry.RegistryError as e:
        raise LaunchError('--home-worktree: %s; nothing was started.' % e) from None


def folder():
    """The default host's worktrees_directory (hosts.json), as a relative OS path. Read when a home
    is wanted, so a launch.py verb that makes none (stop, after-reboot) never reads hosts.json."""
    return os.path.normpath(_profile().worktrees_directory)


def state_folder():
    """The default host's state_directory (hosts.json), as a relative OS path, read like folder()."""
    return os.path.normpath(_profile().state_directory)


def home_path(main, name):
    """`<main>/<folder()>/home-<name>`, the session name lowercased and anything but a-z, 0-9, '.',
    '_' and '-' made '-'. The full name: deep-x and deep-x-r2 get two homes, but names that differ
    only in case or a trailing '-'/'.' share one (start.home_folder compares homes, not names)."""
    slug = _BAD.sub('-', name.lower()).strip('-.') or 'session'
    return os.path.join(os.path.normpath(main), folder(), PREFIX + slug)


def _git(args, cwd, git):
    r = git(args, cwd)
    if r.rc != 0:
        return None, (r.err or '').strip() or 'git %s exited %s' % (' '.join(args[:2]), r.rc)
    return cmd.text(r.out).strip(), None


def _canon(path):
    """`path` as one spelling: junctions and 8.3 short names resolved, case folded (git prints
    the long, forward-slashed form)."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _entry(main, path, git):
    """(the `git worktree list` entry for `path` as a dict, or None), or a LaunchError."""
    out, why = _git(['worktree', 'list', '--porcelain'], main, git)
    if out is None:
        raise LaunchError('git worktree list in %s failed: %s' % (main, why))
    want = _canon(path)
    cur = found = None
    for line in out.splitlines():
        if line.startswith('worktree '):
            cur = {'path': line[len('worktree '):], 'head': None, 'branch': None, 'locked': None}
            if _canon(cur['path']) == want:
                found = cur
            continue
        if cur is None:
            continue
        if line.startswith('HEAD '):
            cur['head'] = line[5:].strip()
        elif line.startswith('branch '):
            cur['branch'] = line[len('branch '):].strip()
        elif line == 'locked' or line.startswith('locked '):
            cur['locked'] = line[len('locked'):].strip()
    return found


def ensure(main, path, whatif=False, git=cmd.call):
    """Make `path` the session's home: missing, a detached worktree at the main checkout's HEAD,
    then locked; present, locked if it is not, and moved to that HEAD when it has nothing
    uncommitted or untracked. Refused (LaunchError, nothing changed) when the folder is not a
    worktree of this repository, is registered with no folder, or is on a branch. `whatif`: the
    lines say what would happen and no git writes."""
    head, why = _git(['rev-parse', '--verify', 'HEAD^{commit}'], main, git)
    if head is None:
        raise LaunchError('cannot read the HEAD of %s: %s' % (main, why))
    entry = _entry(main, path, git)
    reason = LOCK_REASON % os.path.basename(path)
    out = []
    if not os.path.exists(path):
        if entry is not None:
            raise LaunchError('%s is registered as a worktree but its folder is missing; run `git -C "%s" '
                              'worktree unlock "%s"` and `git -C "%s" worktree prune`, then launch again.'
                              % (path, main, path, main))
        if whatif:
            return ['Would create the home worktree %s, detached at %s, and lock it.' % (path, head[:10])]
        _, why = _git(['worktree', 'add', '--detach', path, head], main, git)
        if why is not None:
            raise LaunchError('git worktree add %s failed: %s. If another launch of this session is making it '
                              'at this moment, wait for that one; nothing was started.' % (path, why))
        out.append('Created the home worktree %s, detached at %s.' % (path, head[:10]))
        _, why = _git(['worktree', 'lock', '--reason', reason, path], main, git)
        if why is not None:
            raise LaunchError('git worktree lock %s failed: %s; it is detached, so the sweep still leaves it, '
                              'but lock it before launching again.' % (path, why), out)
        return out
    if entry is None:
        raise LaunchError('%s exists but is not a worktree of %s; move it away, then launch again.' % (path, main))
    if entry['branch']:
        raise LaunchError('%s is on %s, not detached: it is a work branch, not a home worktree; '
                          'nothing was started.' % (path, entry['branch']))
    if entry['locked'] is None:
        if whatif:
            out.append('Would lock the home worktree %s.' % path)
        else:
            _, why = _git(['worktree', 'lock', '--reason', reason, path], main, git)
            if why is not None:
                raise LaunchError('git worktree lock %s failed: %s' % (path, why))
            out.append('Locked the home worktree %s.' % path)
    status, why = _git(['--no-optional-locks', 'status', '--porcelain', '--untracked-files=all'], path, git)
    if status is None:
        raise LaunchError('git status in %s failed: %s' % (path, why), out)
    dirty = [l for l in status.splitlines() if l.strip()]
    if dirty:
        shown = ', '.join(l[3:] for l in dirty[:SHOWN]) + (' and %d more' % (len(dirty) - SHOWN)
                                                          if len(dirty) > SHOWN else '')
        out.append('Warning: the home worktree %s has %d uncommitted or untracked file(s), left as they are and '
                   'not moved to %s: %s. A write meant for the main checkout may have landed there.'
                   % (path, len(dirty), head[:10], shown))
        return out
    if entry['head'] == head:
        out.append('Home worktree %s is at %s.' % (path, head[:10]))
        return out
    if whatif:
        out.append('Would move the home worktree %s to %s.' % (path, head[:10]))
        return out
    _, why = _git(['checkout', '-q', '--detach', head], path, git)
    if why is not None:
        out.append('Warning: moving the home worktree %s to %s failed (%s); it stays at %s.'
                   % (path, head[:10], why, (entry['head'] or '?')[:10]))
        return out
    out.append('Moved the home worktree %s to %s.' % (path, head[:10]))
    return out
