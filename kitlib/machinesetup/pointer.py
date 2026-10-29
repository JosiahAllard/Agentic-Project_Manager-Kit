"""The --pm-repo mode (docs/machine-setup.md "PM repo pointer"): set the PM-repo pointer,
`HKLM\\SOFTWARE\\pm-kit` value `pm_repo` (kitlib.config.pointer), to the PM repository's work tree.

The value is under HKLM, so --apply is refused unless the process is elevated; nothing elevates
itself. On Linux it is the root-owned file /etc/pm-kit/pointer (kitlib.platform.linux.paths
MACHINE_POINTER), written as root (sudo) through a temp file, mode 0644; declining sudo, a note
gives the line that writes the user's own pointer file. A dry run prints the plan and the `reg add`
(on Linux the sudo write) that makes the same change by hand. The path
is checked as the pointer's reader checks it (kitlib.config.pointer.check) before any write, so the
pointer is never set to a value the reader would call BAD.
"""
import os
import shlex

import kitlib.platform
from kitlib.config import pointer as cpointer
from kitlib.config import user
from kitlib.git import repo
from kitlib.platform.linux import paths as linux_paths

from .common import Plan, kit_file, make_folder, python_name
from . import settings

SECTION = 'pointer'
COMMAND = 'tools/machine_setup.py'


def command(kit, path, apply=True):
    return '%s %s --pm-repo %s%s' % (python_name(), kit_file(kit, COMMAND).replace('\\', '/'),
                                     cpointer.quoted(path), ' --apply' if apply else '')


class Registry(object):
    """The pointer's one registry value: read() as kitlib.config.pointer.read_raw, write(value)
    sets it. Tests pass their own object with these two methods."""

    def read(self):
        return cpointer.read_raw()

    def write(self, value):
        if user.fenced():
            raise RuntimeError('the kit\'s tests never write the registry (%s)'
                               % cpointer.where())
        if kitlib.platform.NAME == 'linux':
            return write_file(linux_paths.MACHINE_POINTER, value)
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, cpointer.KEY_PATH, 0,
                                winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, cpointer.VALUE, 0, winreg.REG_SZ, value)


def user_command(path):
    """Linux: the shell line that writes the user's pointer file (USER_POINTER) to `path`."""
    rel = '~/' + '/'.join(linux_paths.USER_POINTER)
    return "mkdir -p %s && printf '%%s\\n' %s > %s" % (
        '~/' + '/'.join(linux_paths.USER_POINTER[:-1]), shlex.quote(path), rel)


def write_file(path, value):
    """Linux: `value` as the one line of the pointer file `path` (mode 0644), through a temp file
    and os.replace, its folder made (mode 0755, whatever root's umask)."""
    make_folder(os.path.dirname(path))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(value + '\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class PointerContext(object):
    """What the --pm-repo mode reads: `pm_repo` (the path given), the kit (its guard file proves it
    is a kit checkout), `host` (extras.Host: elevation) and `registry` (Registry)."""

    def __init__(self, pm_repo, kit, host=None, registry=None):
        from . import extras
        self.pm_repo = pm_repo
        self.kit = os.path.abspath(kit)
        self.host = host or extras.Host()
        self.registry = registry or Registry()


def target(path):
    """The value the pointer is set to for `path`: normalised, backslashes; a relative path is
    kept as given (check() refuses it)."""
    return os.path.normpath(path) if os.path.isabs(path) else path


def survey(ctx, apply=False):
    """The --pm-repo plan: one registry write, or nothing."""
    plan = Plan((SECTION,))
    if not os.path.isfile(kit_file(ctx.kit, settings.GUARD)):
        plan.refuse('%s is not a kit checkout (no %s)' % (ctx.kit, settings.GUARD))
        return plan
    value = target(ctx.pm_repo)
    bad = cpointer.check(value)
    if bad:
        plan.refuse('--pm-repo: %s' % bad)
        return plan
    old, why = ctx.registry.read()
    linux = kitlib.platform.NAME == 'linux'
    where = 'as root or with sudo' if linux else 'from an administrator terminal'
    if why:
        plan.note(SECTION, 'the value there now cannot be read (%s): it is replaced, and this tool '
                           'cannot restore it; note it first%s'
                           % (why, '' if linux else ', e.g. with `reg query "%s" /v %s`'
                              % (cpointer.KEY_NAME, cpointer.VALUE)))
    if old is not None and old == value:
        plan.note(SECTION, '%s already names %s' % (cpointer.where(), value))
        return plan
    if not ctx.host.is_admin():
        if linux:
            plan.note(SECTION, 'declining sudo: with no %s the user\'s own pointer file is read; write it '
                               'by hand: %s' % (linux_paths.MACHINE_POINTER, user_command(value)))
        if apply:
            plan.refuse('not elevated: %s is %s; run %s %s, or '
                        'by hand there: %s' % (
                            cpointer.where() if linux else cpointer.KEY_NAME,
                            'root-owned' if linux else 'under HKLM',
                            command(ctx.kit, value), where, cpointer.command(value)))
            return plan
        plan.note(SECTION, 'not elevated: --apply is refused here; run it %s' % where)
    if old is None:
        text = '%s: set it to %s' % (cpointer.where(), value)
        # A value that exists but cannot be read is not absent: `reg delete` is no restore.
        undo = None if why else cpointer.delete_command()
    else:
        text = '%s: replace %s with %s%s' % (
            cpointer.where(), old, value,
            ' (the same folder, spelled differently)' if repo.same_path(old, value) else '')
        undo = cpointer.command(old)
    plan.add(SECTION, text, lambda: ctx.registry.write(value), undo=undo,
             detail=['by hand, %s: %s' % (where, cpointer.command(value))])
    return plan
