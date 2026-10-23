"""The machine config's session-start lines, for the PM and coordinator session starts
(kitlib.pmstart.checklist, kitlib.coordinatorstart.render, kitlib.hoststart.shared_checks).

  lines(loc, run, rules)   no line when all is well; otherwise what is wrong with the machine config
                           (kitlib.config.user.locate): no pointer, a pointer or file that cannot be
                           read, a copy left in the home folder, or a policy file that is not
                           committed by the owner and unchanged (the integrity line)

The leftover is the home folder's machine config FILE that locate() still finds while the PM repo's
is read (Location.leftover): the kit ignores it, so it is reported, never read. The home folder
itself stays (it holds the auto-task state). No line names an email address: the owner check
compares the last author with the owner rules and says only whether they match.

Every line is a warning: nothing here stops a session. A git call that fails is a "could not
check machine_config" line after the leftover line.
"""
import os

from kitlib.config import user as kuser

PREFIX = 'machine config: '
LEFTOVER = PREFIX + ("a copy is still in the home folder (%s); the kit reads %s and ignores it: "
                     "remove it (Remove-Item -LiteralPath '%s')")
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
    """The integrity line for an OK Location `loc`, or None when its policy file is committed by
    the owner and unchanged. `run(args, cwd)` gives a kitlib.git.cmd
    Result (default kitlib.git.cmd.call); `rules` the owner rules (default the PM repository's
    kitlib.config.load owner_rules). Raises when git cannot answer."""
    if run is None:
        from kitlib.git import cmd
        run = cmd.call
    shown, pm = loc.display(), loc.pm
    rel = os.path.relpath(loc.path, pm).replace('\\', '/')
    if not _git(run, ['ls-files', '--', rel], pm):         # untracked: never committed
        return NEVER % shown
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
    if loc.state != kuser.OK:
        return [UNREADABLE % loc.error]
    out = []
    if loc.leftover:
        out.append(LEFTOVER % (loc.leftover, loc.display(), loc.leftover))
    try:
        line = integrity(loc, run, rules)
    except Exception as e:                      # the leftover line still shows
        line = 'could not check machine_config: %s: %s' % (type(e).__name__, e)
    return out + ([line] if line else [])
