"""Render on commit (decision 29 in docs/multi-host-plan.md section 7; docs/content.md "Render on
commit"): when a commit records a path under `content/`, or `kit.json` or `hosts.json`, the kit's
rendered files are made current first and go into the same commit.

  is_kit(root)          the work tree `root` has the kit's layout: content/roles/ and RENDER_TOOL
  wants_render(paths)   a repo-relative path among `paths` is a render source (SOURCES)
  render(root)          RENDER_TOOL --write run in `root`, as its own process: (paths, None), the
                        repo-relative paths it wrote or removed, or (None, why)
  stage(root, paths)    each of `paths` staged in `root`'s index (a removed one as its deletion):
                        None, or why it failed

tools/commit.py (kitlib.commit) and the opt-in pre-commit hook (kitlib.githooks.precommit) call
these. The render runs the work tree's own tools/render_content.py with `-C <root>`, so the
renderers are the ones being committed and kitlib names no host. Its output lines are
`wrote <path>`. Nothing here prints or exits.
"""
import os
import subprocess
import sys

from kitlib.git import cmd
from kitlib.proc import spawn

RENDER_TOOL = 'tools/render_content.py'
SOURCE_DIRS = ('content/',)
SOURCE_FILES = ('kit.json', 'hosts.json')
RENDER_TIMEOUT = 300          # seconds
WROTE = 'wrote '


def command(root):
    """The command line a person runs to render `root` by hand (the refusal names it)."""
    q = lambda p: '"%s"' % p if ' ' in p else p                         # noqa: E731
    return 'python %s -C %s --write' % (q(os.path.join(root, *RENDER_TOOL.split('/'))), q(root))


def is_kit(root):
    """True when the work tree `root` is a kit checkout: it has content/roles/ and RENDER_TOOL."""
    return os.path.isdir(os.path.join(root, 'content', 'roles')) and \
        os.path.isfile(os.path.join(root, *RENDER_TOOL.split('/')))


def wants_render(paths):
    """True when a repo-relative, `/`-separated path among `paths` is a render source: under a
    SOURCE_DIRS folder or one of SOURCE_FILES, case folded (a name typed `Content/...` on Windows
    is the same file)."""
    low = [p.lower() for p in paths]
    return any(p in SOURCE_FILES or p.startswith(SOURCE_DIRS) for p in low)


def render(root, python=None):
    """(sorted repo-relative paths the render wrote or removed, None), or (None, why) when it could
    not run or failed. `python` defaults to this interpreter."""
    argv = [python or sys.executable, os.path.join(root, *RENDER_TOOL.split('/')), '-C', root, '--write']
    try:
        r = spawn.run(argv, cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace',
                      timeout=RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, '%s did not finish in %d s' % (RENDER_TOOL, RENDER_TIMEOUT)
    except OSError as e:
        return None, '%s could not start: %s' % (RENDER_TOOL, e)
    out = r.stdout or ''
    if r.returncode != 0:
        said = (out + (r.stderr or '')).strip().splitlines()
        return None, '%s exited %s%s' % (RENDER_TOOL, r.returncode, ': %s' % said[-1] if said else '')
    return sorted(ln[len(WROTE):].strip() for ln in out.splitlines() if ln.startswith(WROTE)), None


def stage(root, paths):
    """Stage each of `paths` (repo-relative) in `root`'s index: one on disk is added, one the render
    removed is staged as its deletion (and is nothing when it was never tracked). None, or why git
    failed."""
    present = [p for p in paths if os.path.lexists(os.path.join(root, *p.split('/')))]
    gone = [p for p in paths if p not in present]
    for args in (['add', '--'] + present if present else None,
                 ['rm', '-q', '--cached', '--ignore-unmatch', '--'] + gone if gone else None):
        if args is None:
            continue
        r = cmd.call(args, root, timeout=None)
        if r.rc != 0:
            return 'git %s failed: %s' % (args[0], r.err.strip() if r.err else 'exit %s' % r.rc)
    return None
