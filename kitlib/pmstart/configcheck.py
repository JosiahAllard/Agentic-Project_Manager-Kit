"""The machine config's session-start lines, for the PM and coordinator session starts
(kitlib.pmstart.checklist, kitlib.coordinatorstart.render, kitlib.hoststart.shared_checks).

  lines(loc, run, rules)   no line when all is well; otherwise what is wrong with the machine config
                           (kitlib.config.user.locate): no pointer, a pointer or file that cannot be
                           read, a copy left in the home folder, the auto-tasks state left in the
                           home folder, or a policy file that is not committed by the owner and
                           unchanged (the integrity line)

The leftover is the home folder's machine config FILE that locate() still finds beside the PM
repo's (kitlib.config.user LEFTOVER): the machine config moved, so the kit stops reading it until
the copy is gone, and the line names its remove command. The auto-tasks state still in the home
folder (kitlib.autotasks.tick home_problem) stops the auto tasks the same way, and its line names
the move. No line names an email address: the owner check compares the last author with the owner
rules and says only whether they match.

Nothing here stops a session itself. A git call that fails is a "could not check machine_config"
line after the leftover lines.
"""
import os

from kitlib.config import user as kuser

PREFIX = 'machine config: '
LEFTOVER = PREFIX + ("a copy is still in the home folder (%s); the kit reads only %s and stops "
                     "until it is removed: remove it (Remove-Item -LiteralPath '%s')")
UNREADABLE = PREFIX + 'cannot be read: %s'
NO_POINTER = PREFIX + ('no PM repo pointer: set it (tools/machine_setup.py --pm-repo, %s)'
                       % kuser.POINTER_DOC)
DIRTY = PREFIX + '%s has uncommitted changes: check them (git -C %s diff -- %s)'
NOT_OWNER = PREFIX + ('%s was last committed by someone other than the owner: check it '
                      '(git -C %s log -1 -- %s)')
NEVER = PREFIX + "%s was never committed: commit it from the owner's terminal"
NO_OWNER = PREFIX + "cannot check %s's author: %s"


def _git(run, args, cwd):
    """`git <args>`'s output text; RuntimeError naming the command when it fails."""
    r = run(args, cwd)
    if r.rc != 0:
        raise RuntimeError('git %s: %s' % (' '.join(args[:2]), (r.err or 'exit %s' % r.rc)[:200]))
    return r.out.decode('utf-8', 'replace').strip()


def _rules(pm):
    from kitlib import config as kconfig
    return kconfig.load(pm).owner_rules()


def integrity(loc, run=None, rules=None):
    """The integrity line for an OK or LEFTOVER Location `loc`, or None when its policy file is
    committed by the owner and unchanged, or is not there and never was tracked (an empty config). `run(args, cwd)` gives a kitlib.git.cmd
    Result (default kitlib.git.cmd.call); `rules` the owner rules (default the PM repository's
    kitlib.config.load owner_rules). Raises when git cannot answer."""
    if run is None:
        from kitlib.git import cmd
        run = cmd.call
    shown, pm = loc.display(), loc.pm
    rel = os.path.relpath(loc.path, pm).replace('\\', '/')
    if not _git(run, ['ls-files', '--', rel], pm):         # untracked: never committed
        # no file at all is an empty config: nothing to commit (a tracked one deleted is a change)
        return NEVER % shown if os.path.lexists(loc.path) else None
    if _git(run, ['status', '--porcelain', '--', rel], pm):
        return DIRTY % (shown, pm, rel)
    author = _git(run, ['log', '-1', '--format=%ae', '--', rel], pm)
    if not author:
        return NEVER % shown
    rules = rules if rules is not None else _rules(pm)
    problem = rules.identity_problem()
    if problem:
        return NO_OWNER % (shown, problem)
    if not rules.is_owner(author):
        return NOT_OWNER % (shown, pm, rel)
    return None


def lines(loc=None, run=None, rules=None):
    """The machine config's session-start lines for the Location `loc` (default
    kitlib.config.user.locate()): [] when all is well; `run` and `rules` as integrity."""
    loc = loc if loc is not None else kuser.locate()
    if loc.state == kuser.NONE:
        return [NO_POINTER]
    if loc.state not in (kuser.OK, kuser.LEFTOVER):
        return [UNREADABLE % loc.error]
    out = []
    if loc.leftover:
        # PowerShell's single-quoted string: a ' in the path is written ''
        out.append(LEFTOVER % (loc.leftover, loc.display(), loc.leftover.replace("'", "''")))
    from kitlib.autotasks import tick
    state = tick.home_problem(os.path.join(loc.folder, tick.DIR_NAME), loc.home)
    if state:
        out.append(PREFIX + state)
    try:
        line = integrity(loc, run, rules)
    except Exception as e:                      # the leftover line still shows
        line = 'could not check machine_config: %s: %s' % (type(e).__name__, e)
    return out + ([line] if line else [])
