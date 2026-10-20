"""The --pm-repo mode (docs/machine-setup.md "PM repo pointer"): set the PM-repo pointer,
`HKLM\\SOFTWARE\\pm-kit` value `pm_repo` (kitlib.config.pointer), to the PM repository's work tree.

The value is under HKLM, so --apply is refused unless the process is elevated; nothing elevates
itself. A dry run prints the plan and the `reg add` that makes the same change by hand. The path
is checked as the pointer's reader checks it (kitlib.config.pointer.check) before any write, so the
pointer is never set to a value the reader would call BAD.
"""
import os

from kitlib.config import pointer as cpointer
from kitlib.config import user
from kitlib.git import repo

from .common import Plan, kit_file
from . import settings

SECTION = 'pointer'
COMMAND = 'tools/machine_setup.py'


def command(kit, path, apply=True):
    return 'python %s --pm-repo "%s"%s' % (kit_file(kit, COMMAND).replace('\\', '/'), path,
                                         ' --apply' if apply else '')


class Registry(object):
    """The pointer's one registry value: read() as kitlib.config.pointer.read_raw, write(value)
    sets it. Tests pass their own object with these two methods."""

    def read(self):
        return cpointer.read_raw()

    def write(self, value):
        if user.fenced():
            raise RuntimeError('the kit\'s tests never write the registry (%s %s)'
                               % (cpointer.KEY_NAME, cpointer.VALUE))
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, cpointer.KEY_PATH, 0,
                                winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, cpointer.VALUE, 0, winreg.REG_SZ, value)


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
    if why:
        plan.note(SECTION, 'the value there now cannot be read (%s): it is replaced' % why)
    if old is not None and old == value:
        plan.note(SECTION, '%s %s already names %s' % (cpointer.KEY_NAME, cpointer.VALUE, value))
        return plan
    if not ctx.host.is_admin():
        if apply:
            plan.refuse('not elevated: %s is under HKLM; run %s from an administrator terminal, or '
                        'by hand there: %s' % (cpointer.KEY_NAME, command(ctx.kit, value),
                                               cpointer.command(value)))
            return plan
        plan.note(SECTION, 'not elevated: --apply is refused here; run it from an administrator terminal')
    if old is None:
        text = '%s %s: set it to %s' % (cpointer.KEY_NAME, cpointer.VALUE, value)
        undo = cpointer.delete_command()
    else:
        text = '%s %s: replace %s with %s%s' % (
            cpointer.KEY_NAME, cpointer.VALUE, old, value,
            ' (the same folder, spelled differently)' if repo.same_path(old, value) else '')
        undo = cpointer.command(old)
    plan.add(SECTION, text, lambda: ctx.registry.write(value), undo=undo,
             detail=['by hand, from an administrator terminal: %s' % cpointer.command(value)])
    return plan
