"""The Windows scheduled task that runs the nightly worktree sweep, ported from the PM
kit's tools/worktree_sweep_task.py (docs/worktree-remove.md). tools/worktree_sweep_task.py is the
entry point; `run` here does the work and returns Outcome(code, lines). Nothing here prints.

The task, `<repo name>-worktree-sweep`, runs `python "<kit>\\tools\\worktree_remove.py" -C "<main
checkout>" --sweep-merged --apply` daily at 03:00 as the registering user (`-RunLevel Limited`,
`-StartWhenAvailable`). The tool is this kit's own copy, by absolute path, and must sit in the kit's
MAIN checkout (a branch worktree's copy is unreviewed and goes away with the branch). No console
window opens: the task's program is `pythonw.exe` running the carrier beside the tool (`CARRIER`,
tools/no_window.py), which starts the python.exe beside that pythonw with a hidden console
(kitlib.proc.spawn.carry). The pythonw is the one in the base install of the python that runs
this (kitlib.launcher.pythonw.base_pythonw), written into the line as a literal path, never what
`Get-Command pythonw` finds: with an app execution alias first on PATH that is
`...\\WindowsApps\\pythonw.exe`, not the real install. Nothing is printed or registered when that
pythonw is under WindowsApps or is not a file (`run`: refused); the line still throws, registering
nothing, on a WindowsApps path (`NOT_THE_STORE_ALIAS`), so a hand-edited copy is checked too.

Nothing is registered without `--register --owner-confirmed`; a bare `--register` is refused before
anything else is read. Agents never type `--owner-confirmed`.
"""
import os

from kitlib.config import ConfigError, load
from kitlib.git import repo as gitrepo
from kitlib.launcher.pythonw import base_pythonw, refusal
from kitlib.proc.spawn import CARRIER      # tools/no_window.py, beside the tool the task runs

from .remove import OK, REFUSED, Outcome, GitReadError, canon, main_checkout

USAGE = 2
AT = '03:00'
SWEEP = '--sweep-merged --apply'
# PowerShell ends a single-quoted string at any of these, not only at ' (its own
# CodeGeneration.EscapeSingleQuotedStringContent doubles all five).
SINGLE_QUOTES = ("'", '‘', '’', '‚', '‛')
# `$py` is the resolved pythonw.exe, a literal (PYTHONW_IS % its quoted path).
PYTHONW_IS = '$py = %s'
NOT_THE_STORE_ALIAS = (r"if (-not $py -or $py -match '\\WindowsApps\\') { throw ('pythonw is ' + $py +"
                       r" ': no pythonw, or the Microsoft Store alias; nothing was registered') }")


def quoted(text):
    """`text` as a PowerShell single-quoted string: every single-quote character doubled."""
    for q in SINGLE_QUOTES:
        text = text.replace(q, q + q)
    return "'" + text + "'"


def task_name(name):
    return '%s-worktree-sweep' % name


def argument(tool, root):
    """The task's pythonw arguments: the carrier beside the tool, `python` (the carrier's word for
    the python.exe beside its pythonw), the tool and the repository, each path double-quoted (a
    path with a space stays one argument), then the sweep."""
    tool = os.path.normpath(tool)
    return '"%s" python "%s" -C "%s" %s' % (os.path.join(os.path.dirname(tool), CARRIER), tool,
                                            os.path.normpath(root), SWEEP)


def description(name):
    return '%s: nightly sweep of landed, idle, unheld worktrees' % name


def command(root, name, tool, pythonw):
    """The one PowerShell line that registers the task to run `pythonw`, its working directory
    `root`."""
    return ("%s; %s; Register-ScheduledTask -TaskName %s"
            " -Trigger (New-ScheduledTaskTrigger -Daily -At %s)"
            " -Action (New-ScheduledTaskAction -Execute $py -Argument %s -WorkingDirectory %s)"
            " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)"
            " -User $env:USERNAME -RunLevel Limited -Description %s"
            % (PYTHONW_IS % quoted(pythonw), NOT_THE_STORE_ALIAS, quoted(task_name(name)), AT,
               quoted(argument(tool, root)), quoted(root), quoted(description(name))))


def parse(argv):
    """(root or None, register, owner_confirmed), or None for a usage error: each flag at most
    once, `-C` with a value, and `--owner-confirmed` only with `--register`."""
    root, register, confirmed, rest = None, False, False, list(argv)
    while rest:
        word = rest.pop(0)
        if word == '--register' and not register:
            register = True
        elif word == '--owner-confirmed' and not confirmed:
            confirmed = True
        elif word == '-C' and root is None and rest and not rest[0].startswith('-'):
            root = rest.pop(0)
        else:
            return None
    if confirmed and not register:
        return None
    return root, register, confirmed


def resolve(root, tool):
    """(main checkout, name) to register, or a refusal message."""
    root = os.path.normpath(os.path.abspath(root))
    try:
        main = main_checkout(root)
    except GitReadError as e:
        return 'cannot find the main checkout of %s (%s)' % (root, e)
    if canon(main) != canon(root):
        return '%s is not the main checkout (%s): the sweep runs only there' % (root, main)
    try:
        cfg = load(main)
    except ConfigError as e:
        return 'kit configuration: %s' % e
    name = cfg.repo.get('name') or os.path.basename(os.path.normpath(main))
    if not os.path.isfile(tool):
        return 'the sweep tool is not at %s' % tool
    place = gitrepo.classify(os.path.dirname(os.path.dirname(os.path.abspath(tool))))
    if place.kind != gitrepo.MAIN:
        return ('%s is not in a main checkout (%s): the task must run the kit\'s main copy, never'
                ' a branch worktree\'s' % (tool, place.detail or place.kind))
    return main, name


def run(argv, runner, root, tool, pythonw=None):
    """The verb. `runner(args) -> exit code` runs PowerShell (tests pass a fake); `root` is the
    default repository (the caller's directory); `tool` the worktree_remove.py the task runs;
    `pythonw` the task's program (default: kitlib.launcher.pythonw.base_pythonw())."""
    got = parse(argv)
    if got is None:
        return Outcome(USAGE, ['usage:    worktree_sweep_task.py [-C <main checkout>]'
                               ' [--register --owner-confirmed]'])
    named, register, confirmed = got
    if register and not confirmed:
        return Outcome(REFUSED, ['refused   --register is the owner\'s act: he runs it'
                                 ' with --owner-confirmed; nothing was registered'])
    found = resolve(named or root, tool)
    if isinstance(found, str):
        return Outcome(REFUSED, ['refused   %s; nothing was registered' % found])
    main, name = found
    pythonw = pythonw or base_pythonw()
    why = refusal(pythonw)
    if why:
        return Outcome(REFUSED, ['refused   %s; nothing was registered' % why])
    line = command(main, name, tool, pythonw)
    if not register:
        return Outcome(OK, [line, 'printed   nothing was registered; the owner registers it with'
                                  ' --register --owner-confirmed'])
    rc = runner(['powershell', '-NoProfile', '-NonInteractive', '-Command', line])
    return Outcome(OK if rc == 0 else REFUSED,
                   [line, '%s  powershell exited %d' % ('registered' if rc == 0 else 'failed   ', rc)])
