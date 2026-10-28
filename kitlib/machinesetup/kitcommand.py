"""The kit command on the user PATH (docs/machine-setup.md "Extras"): `kit TOOL [ARGUMENTS...]` in a
terminal runs tools/kit.py (kitlib.kitcmd), which runs TOOL from the plugin export the machine uses
now. Planned in both modes of machine setup, in the extras section; scheduled tasks are untouched.

Two changes, each planned only when it differs:

- the launcher FILE_NAME in `<plugin.export_dir>\\bin` (FOLDER; a plugin prune removes only export
  folders, so it stays): three lines, the last `python "<live main checkout>\\tools\\kit.py" %*`.
  The checkout is the kit's live main checkout (worktreesweep.tool_root, as the tick task takes
  it), written when --apply runs; no tracked file holds it. tools/kit.py reads the machine config
  at every call, so a plugin refresh never makes the launcher stale; a moved checkout does, and the
  next machine setup rewrites it. `python` is the one on PATH, as for the git hooks. A file there
  that lacks MARK (not written by machine setup) is left as is, and nothing is planned.
- that folder appended to the user Path (HKCU\\Environment), the raw value kept as it is (its
  %VAR% entries unexpanded, its REG_EXPAND_SZ type) through the host's winreg reader and writer,
  then WM_SETTINGCHANGE broadcast. A dry run only reads the Path. A process started before
  --apply (an open terminal or session) keeps its old PATH.

On Linux (docs/linux-support-plan.md section 6, step 11) the launcher is LINUX_FILE,
`~/.local/bin/kit` under the home folder: a sh script (MARK_SH its second line, mode 0755) whose
last line is `exec python3 '<live main checkout>/tools/kit.py' "$@"`. No PATH is changed: most
distributions put ~/.local/bin on a login shell's PATH when it exists, and a note says so when
this process's PATH lacks it. plugin.export_dir is not read there.

Nothing is planned, and a note says why, when the kit has no live main checkout, that checkout is
not a main checkout (a branch worktree is an unreviewed copy) or has no tools/kit.py, or
plugin.export_dir cannot be read.

Known limits: Git Bash does not find `kit` by name (it runs `kit.cmd`); cmd.exe reads the command
line again before the launcher runs, so in an argument `^` is dropped, `%VAR%` is expanded, and an
`&`, `|`, `<` or `>` that cmd sees outside double quotes (an argument without a blank, or after
one holding a `"`) ends the kit command and runs the rest as a command of its own (probed from
Windows PowerShell 5.1 and Git Bash). An exit code of 0xC000013A makes cmd ask "Terminate batch
job (Y/N)?".
"""
import os
import shlex

from kitlib import platform, pluginsource
from kitlib.config import ConfigError
from kitlib.git import repo as gitrepo

from . import worktreesweep

FOLDER = 'bin'
FILE_NAME = 'kit.cmd'
TOOL = os.path.join('tools', 'kit.py')
MARK = 'rem pm-kit: the kit command'
REG_SZ, REG_EXPAND_SZ = 1, 2                        # winreg's value types
PATH_KEY = 'HKCU\\Environment Path'
LINUX_FILE = ('.local', 'bin', 'kit')                 # under the home folder
MARK_SH = '# pm-kit: the kit command'
EXECUTABLE = 0o755


def launcher_sh(root):
    """The Linux launcher's text for the kit checkout `root`, LF line ends, the path quoted for sh."""
    tool = os.path.join(root, TOOL).replace('\\', '/')
    return '\n'.join(['#!/bin/sh',
                      '%s (kit TOOL [ARGUMENTS...]); written by tools/machine_setup.py --apply, which '
                      'rewrites it' % MARK_SH,
                      'exec python3 %s "$@"' % shlex.quote(tool), ''])


def launcher_text(root):
    """The launcher's text for the kit checkout `root`, CRLF line ends. A `%` in the path is
    doubled, so cmd does not read it as a variable."""
    tool = os.path.join(os.path.normpath(root), TOOL).replace('%', '%%')
    return '\r\n'.join(['@echo off',
                        '%s (kit TOOL [ARGUMENTS...]); written by tools/machine_setup.py --apply, '
                        'which rewrites it' % MARK,
                        'python "%s" %%*' % tool, ''])


def _key(entry):
    entry = os.path.expandvars(entry.strip().strip('"'))
    return os.path.normcase(os.path.normpath(entry)) if entry else ''


def path_has(raw, folder):
    """Whether the Path value `raw` (unexpanded, `;`-separated) lists `folder`: case, slashes, a
    trailing separator, quotes and %VAR% references do not matter."""
    want = _key(folder)
    return any(_key(e) == want for e in (raw or '').split(';'))


def path_with(raw, folder):
    """`raw` with `folder` appended as its last entry; every entry before it kept as written."""
    raw = (raw or '').rstrip(';')
    return '%s;%s' % (raw, folder) if raw else folder


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('ascii', 'replace')
    except OSError:
        return None


def write_launcher(path, text, mode=None):
    """Write `text` to `path` (ASCII) through a temp file and os.replace, its folder made first;
    `mode`, when given, set on the file before it is moved into place."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(text.encode('ascii'))
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def survey(plan, ctx):
    root = worktreesweep.tool_root(ctx.kit)
    if root is None or worktreesweep.is_export(root):
        plan.note('extras', 'kit command: the kit at %s is a plugin export with no live main checkout; '
                            'not planned (pass --kit <kit main checkout>)' % (root or ctx.kit))
        return
    if not os.path.isfile(os.path.join(root, TOOL)):
        plan.note('extras', 'kit command: no %s; not planned' % os.path.join(root, TOOL))
        return
    place = gitrepo.classify(root)
    if place.kind != gitrepo.MAIN:
        plan.note('extras', 'kit command: the kit at %s is not a main checkout (%s), so `kit` would run an '
                            'unreviewed copy; not planned' % (root, place.detail or place.kind))
        return
    if platform.NAME == 'linux':
        linux_steps(plan, root, os.path.join(ctx.home, *LINUX_FILE))
        return
    try:
        export = pluginsource.load(ctx.home, kit=ctx.kit).export_dir
    except (ConfigError, pluginsource.RefreshError, OSError, ValueError) as e:
        plan.note('extras', 'kit command: plugin.export_dir could not be read (%s); not planned' % e)
        return
    plan_steps(plan, ctx.host, root, os.path.join(export, FOLDER))


def linux_steps(plan, root, path, environ=None):
    """Plan the Linux launcher at `path` for the checkout `root`; a note when its folder is not on
    PATH (`environ`'s, default this process's)."""
    try:
        text = launcher_sh(root)
        text.encode('ascii')
    except UnicodeEncodeError:
        plan.note('extras', 'kit command: the path %s is not plain ASCII; not planned' % root)
        return
    have = _read(path)
    if have is not None and MARK_SH not in have:
        plan.note('extras', 'kit command: %s was not written by machine setup; left as is' % path)
        return
    if have != text:
        plan.add('extras', '%s the kit command %s (mode 755): exec python3 %s "$@"'
                 % ('write' if have is None else 'rewrite', path, os.path.join(root, TOOL)),
                 lambda: write_launcher(path, text, EXECUTABLE),
                 undo=('delete "%s"' % path) if have is None else 'its old text was: %r' % have)
    folder = os.path.dirname(path)
    entries = (os.environ if environ is None else environ).get('PATH', '').split(os.pathsep)
    if not any(e.rstrip('/') == folder.rstrip('/') for e in entries if e):
        plan.note('extras', 'kit command: %s is not on this PATH; most login shells add it when it exists, '
                            'so open a new terminal, or add it to your shell profile' % folder)


def plan_steps(plan, host, root, folder):
    """Plan the launcher in `folder` for the checkout `root`, then `folder` on the user Path."""
    try:
        text = launcher_text(root)
        text.encode('ascii')
    except UnicodeEncodeError:
        plan.note('extras', 'kit command: the path %s is not plain ASCII, which cmd.exe may misread; '
                            'not planned' % root)
        return
    path = os.path.join(folder, FILE_NAME)
    have = _read(path)
    if have is not None and MARK not in have:
        plan.note('extras', 'kit command: %s was not written by machine setup; left as is, and its folder '
                            'is not added to the PATH' % path)
        return
    if have != text:
        plan.add('extras', '%s the kit command %s: python "%s" %%*'
                 % ('write' if have is None else 'rewrite', path, os.path.join(root, TOOL)),
                 lambda: write_launcher(path, text),
                 undo=('delete "%s"' % path) if have is None else 'its old text was: %r' % have)
    try:
        raw, kind = host.user_path()
    except (OSError, ValueError) as e:  # a denied OpenKey; a Path that is not a string
        plan.note('extras', 'kit command: the user PATH (%s) could not be read (%s); its step is skipped, '
                            'so add %s to it by hand' % (PATH_KEY, e, folder))
        return
    if path_has(raw, folder):
        return
    new = path_with(raw, folder)
    kind = kind if kind in (REG_SZ, REG_EXPAND_SZ) else REG_EXPAND_SZ
    plan.add('extras', 'add %s to the user PATH (%s, appended, the other entries and the value type '
                       'kept), so `kit` runs in a cmd or PowerShell terminal started after this run'
             % (folder, PATH_KEY),
             lambda: host.set_user_path(new, kind),
             undo=('set %s back to: %s' % (PATH_KEY, raw)) if raw is not None
             else 'delete the value %s' % PATH_KEY)
    plan.note('extras', 'kit command: a terminal or session started before --apply keeps its old PATH; '
                        'open a new one. Git Bash runs it as kit.cmd')
