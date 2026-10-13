"""First-use repo prep (docs/multi-host-plan.md decisions 23 and 25; docs/new-worktree.md "First-use
prep"): local settings only, nothing tracked and nothing committed.

  run(path, folders=None, kit=None)    lines saying what was done or could not be; [] when the
                                       repository was already prepared

Two steps, each idempotent, each a no-op when there is nothing to do:

- Exclude. Each agent worktree folder (every host's hosts.json worktrees_directory, and the
  repository's `.kit.json` worktrees_dir and worktrees_by_host when it loads) gets one anchored
  line, `/<folder>/`, in the repository's own `info/exclude`. That file is in the git COMMON dir,
  so a linked worktree writes the main checkout's (git rev-parse --git-path). A line already there
  (with or without its trailing slash) is not written again; the file's other lines and its line
  ending are kept, and `info/` is made when it is missing. The exclude only hides the folders from
  the main checkout's untracked list: a worktree has its own index, so its commits and pushes are
  untouched.
- Hooks. The local `core.hooksPath` is set to this kit's `githooks/` (the folder beside the
  kitlib this module is in) only when `git config --get core.hooksPath` finds no value at any level
  (local, global, system). A value already there is left alone, with one exception: a LOCAL value
  that is an absolute path to a folder named `githooks` that no longer exists - the kit's own
  setting from a kit folder that moved - is repointed to this kit's `githooks/` (a hooks folder
  that is gone runs no hooks at all, so nothing a human chose is lost).

Every failure (no git, not a repository, a read-only `.git`) is a line, never an exception: the
callers (the PM session start, tools/new_worktree.py) go on.
"""
import os

from kitlib import kitroot
from kitlib.git import cmd
from kitlib.git import repo as gitrepo

HOOKS_KEY = 'core.hooksPath'
EXCLUDE = 'info/exclude'


def kit_githooks(kit=None):
    """`<kit>/githooks`; `kit` defaults to the kit this module is in (kitlib.kitroot)."""
    return os.path.join(kit or kitroot.root(), 'githooks')


def anchored(folder):
    """`/<folder>/` for a repo-relative folder, or None for one that is not inside the repository
    (absolute, a drive, a `..` step, empty)."""
    f = (folder or '').strip().replace('\\', '/').strip('/')
    parts = [p for p in f.split('/') if p not in ('', '.')]
    if not parts or '..' in parts or ':' in parts[0] or (folder or '').strip().startswith(('/', '\\')):
        return None
    return '/%s/' % '/'.join(parts)


def default_folders(top):
    """Every registered host's worktrees_directory, then the repository's own worktrees_dir and
    worktrees_by_host folders when its `.kit.json` loads (kitlib.worktree.hostdirs); a config that
    does not load adds nothing (the host folders still are written)."""
    from kitlib.host import registry
    found = list(registry.union('worktrees_directory'))
    try:
        from kitlib.config import load
        from kitlib.worktree import hostdirs
        found.extend(hostdirs.folders(load(top)))
    except Exception:           # a refused .kit.json is reported by the tools that need it
        pass
    return found


def exclude_lines(path, folders):
    """`(lines written, None)` or `(None, why)`: the anchored lines of `folders` missing from the
    repository's info/exclude, appended."""
    target, why = gitrepo.git_path(path, EXCLUDE)
    if target is None:
        return None, why
    wanted = list(dict.fromkeys(a for a in (anchored(f) for f in folders) if a))
    try:
        with open(target, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        data = b''
    except OSError as e:
        return None, 'cannot read %s: %s' % (target, e)
    have = {l.strip().rstrip('/') for l in data.decode('utf-8', 'replace').splitlines()}
    missing = [w for w in wanted if w.rstrip('/') not in have]
    if not missing:
        return [], None
    eol = b'\r\n' if b'\r\n' in data else b'\n'
    lead = eol if data and not data.endswith((b'\n', b'\r')) else b''
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'ab') as f:
            f.write(lead + eol.join(m.encode('utf-8') for m in missing) + eol)
    except OSError as e:
        return None, 'cannot write %s: %s' % (target, e)
    return missing, None


def _get(path, *scope):
    """`(value or None when unset, None)` of core.hooksPath, or `(None, why)` when git failed."""
    r = cmd.call(['config'] + list(scope) + ['--get', HOOKS_KEY], path)
    if r.rc == 0:
        return cmd.text(r.out).strip(), None
    if r.rc == 1:                       # git's "no such key"
        return None, None
    return None, r.err or ('git did not run' if r.rc is None else 'git config exited %d' % r.rc)


def _stale_kit_hooks(value):
    """True for an absolute path to a folder named githooks that is not there: the kit's own
    setting from a kit folder that moved."""
    v = value.replace('\\', '/').rstrip('/')
    return (os.path.isabs(value) and os.path.basename(v).lower() == 'githooks'
            and not os.path.isdir(value))


def hooks_line(path, kit=None):
    """`(line or None, None)` or `(None, why)`: core.hooksPath set (unset everywhere), repointed
    (a stale local kit value), or left alone (None: nothing to say)."""
    hooks = kit_githooks(kit)
    value, why = _get(path)
    if why:
        return None, 'cannot read %s: %s' % (HOOKS_KEY, why)
    if value is None:
        old = None
    else:
        local, why = _get(path, '--local')
        if why:
            return None, 'cannot read the local %s: %s' % (HOOKS_KEY, why)
        if local != value or not _stale_kit_hooks(value):
            return None, None           # a hooks path someone set: never overwritten
        old = value
    r = cmd.call(['config', '--local', HOOKS_KEY, hooks], path)
    if r.rc != 0:
        return None, 'cannot set %s: %s' % (HOOKS_KEY, r.err or 'git config exited %s' % r.rc)
    if old is None:
        return 'set the local %s to %s' % (HOOKS_KEY, hooks), None
    return 'repointed the local %s from %s (not there any more) to %s' % (HOOKS_KEY, old, hooks), None


def run(path, folders=None, kit=None):
    """The prep of the repository `path` is in (any of its work trees): lines saying what was done
    or could not be, each starting `repo prep`; [] when there was nothing to do. Never raises for
    a git or file failure. `folders` defaults to default_folders(); `kit` to this kit."""
    try:
        place = gitrepo.classify(path)
        if place.kind not in (gitrepo.MAIN, gitrepo.LINKED):
            return ['repo prep: %s is not a git checkout git could name (%s); nothing prepared'
                    % (path, place.detail or place.kind)]
        top = place.top
        lines = []
        written, why = exclude_lines(top, default_folders(top) if folders is None else folders)
        if why:
            lines.append('repo prep: %s: could not add the worktree folders to info/exclude: %s'
                         % (top, why))
        elif written:
            lines.append('repo prep: %s: added %s to info/exclude (local, not committed)'
                         % (top, ' '.join(written)))
        said, why = hooks_line(top, kit)
        if why:
            lines.append('repo prep: %s: %s' % (top, why))
        elif said:
            lines.append('repo prep: %s: %s' % (top, said))
        return lines
    except Exception as e:              # never blocks a session start or a new worktree
        return ['repo prep: %s: failed: %s: %s' % (path, type(e).__name__, e)]
