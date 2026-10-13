"""What a repository with no `.kit.json` setting says about itself: its main branch and its name
(docs/kit-json.md "With no file"). kitlib.config.load fills `repo.main_branch` and `repo.name`
from here when the file does not set them.

Read from the git files, never by starting git, so a guard hook pays no process launch for them:
`<root>/.git` is the git dir (a folder) or names it (`gitdir: X`, a linked worktree, whose
`commondir` names the common dir); refs and config are read from the common dir.

  main_branch(root, remote)  the branch `refs/remotes/<remote>/HEAD` points at (what `git clone`
                             and `git remote set-head <remote> --auto` write); else `main` when
                             `refs/heads/main` or `refs/remotes/<remote>/main` exists; else
                             `master` when only that exists (local or remote-tracking); else
                             `main`. A repository whose refs are in a reftable (no loose refs,
                             no packed-refs) is asked once: `git symbolic-ref`.
  repo_name(root, remote)    the last part of `<remote>`'s URL in the common config, `.git`
                             dropped; else the main checkout's folder name (the folder holding
                             the common `.git`), else `root`'s own.

Anything that cannot be read is "not there": the answer falls to the next rule, never an error.
So is a `<remote>/HEAD` naming a branch that starts with `-` or `/` or holds `..`.

`<remote>/HEAD` is local: any session can repoint it (`git remote set-head`). The push rules
therefore guard `main` as well while `.kit.json` names no main branch (Config.guarded_mains).
"""
import os
import re

from . import cmd

FALLBACK_MAIN = 'main'
_NAME_RE = re.compile(r'[^\s~^:?*\[\\]+\Z')


def _read(path, limit=1 << 20):
    try:
        with open(path, 'rb') as f:
            return f.read(limit).decode('utf-8', 'replace')
    except (OSError, ValueError):
        return None


def common_dir(root):
    """The git common dir of the work tree at `root`, read from files, or None."""
    if not root:
        return None
    dot = os.path.join(root, '.git')
    if os.path.isdir(dot):
        gitdir = dot
    else:
        text = _read(dot, 4096)
        if not text or not text.startswith('gitdir:'):
            return None
        target = text[len('gitdir:'):].strip()
        if not target or '\n' in target:
            return None
        gitdir = os.path.normpath(os.path.join(root, target))
    named = _read(os.path.join(gitdir, 'commondir'), 4096)
    if named is not None:
        named = named.strip()
        if not named or '\n' in named:
            return None
        gitdir = os.path.normpath(os.path.join(gitdir, named))
    return gitdir if os.path.isdir(gitdir) else None


def _packed(common):
    """The ref names packed-refs lists (None when there is no packed-refs)."""
    text = _read(os.path.join(common, 'packed-refs'), 64 << 20)
    if text is None:
        return None
    out = set()
    for line in text.splitlines():
        parts = line.split(' ', 1)
        if len(parts) == 2 and not line.startswith(('#', '^')):
            out.add(parts[1].strip())
    return out


def _name_ok(name):
    """A remote or branch name safe to join to a path and to pass to git: no leading `-` or `/`,
    no `..`, none of the characters git refuses in a ref."""
    return bool(name) and bool(_NAME_RE.match(name)) and '..' not in name and name[0] not in '-/'


def _symref_head(common, remote, root):
    """The branch `refs/remotes/<remote>/HEAD` points at, or None."""
    text = _read(os.path.join(common, 'refs', 'remotes', remote, 'HEAD'), 4096)
    if text is None and os.path.isdir(os.path.join(common, 'reftable')):
        r = cmd.call(['symbolic-ref', '--quiet', 'refs/remotes/%s/HEAD' % remote], root)
        text = 'ref: ' + cmd.text(r.out) if r.rc == 0 else None
    if not text or not text.startswith('ref:'):
        return None
    target = text[4:].strip()
    prefix = 'refs/remotes/%s/' % remote
    if not target.startswith(prefix) or '\n' in target:
        return None
    branch = target[len(prefix):]
    return branch if _name_ok(branch) and branch != 'HEAD' else None


def main_branch_source(root, remote='origin'):
    """`(branch, source)`: main_branch's answer and the rule that gave it: `<remote>/HEAD`,
    `exists` (main or master found) or `fallback` (nothing found)."""
    common = common_dir(root)
    if common is None:
        return FALLBACK_MAIN, 'fallback'
    if _name_ok(remote):
        head = _symref_head(common, remote, root)
        if head:
            return head, '%s/HEAD' % remote
    packed = []                                  # packed-refs, read only when a loose ref is missing
    for name in (FALLBACK_MAIN, 'master'):
        candidates = ['refs/heads/' + name]
        if _name_ok(remote):
            candidates.append('refs/remotes/%s/%s' % (remote, name))
        if any(os.path.isfile(os.path.join(common, *c.split('/'))) for c in candidates):
            return name, 'exists'
        if not packed:
            packed.append(_packed(common))
        if packed[0] is not None and any(c in packed[0] for c in candidates):
            return name, 'exists'
    return FALLBACK_MAIN, 'fallback'


def main_branch(root, remote='origin'):
    """The main branch of the repository at `root` (module docstring)."""
    return main_branch_source(root, remote)[0]


def remote_url(common, remote):
    """`url` of `[remote "<remote>"]` in the common config, or None."""
    text = _read(os.path.join(common, 'config'))
    if text is None:
        return None
    section = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in '#;':
            continue
        if s.startswith('['):
            head, _, s = s[1:].partition(']')
            m = re.match(r'\s*remote\s+"((?:[^"\\]|\\.)*)"\s*\Z', head, re.I)
            section = m.group(1).replace('\\"', '"').replace('\\\\', '\\') if m else ''
            s = s.strip()
            if not s:
                continue
        if section != remote:
            continue
        key, eq, value = s.partition('=')
        if eq and key.strip().lower() == 'url':
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return value or None
    return None


def name_from_url(url):
    """The repository name a remote URL ends in, `.git` dropped, or '' when none."""
    u = (url or '').strip().replace('\\', '/').rstrip('/')
    if u.lower().endswith('.git'):
        u = u[:-4].rstrip('/')
    tail = re.split(r'[/:]', u)[-1] if u else ''
    return tail.strip()


def repo_name(root, remote='origin'):
    """The repository's name (module docstring)."""
    common = common_dir(root)
    if common is not None and _name_ok(remote):
        name = name_from_url(remote_url(common, remote))
        if name:
            return name
    if common is not None and os.path.basename(os.path.normpath(common)).lower() == '.git':
        return os.path.basename(os.path.dirname(os.path.normpath(common)))
    return os.path.basename(os.path.normpath(root)) if root else ''
