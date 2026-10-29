"""Where this kit is, for code that names one of its tools in a command it prints or runs.

  root()             the kit checkout (or plugin export) this module is in
  tool(name, kit)    `<kit>/tools/<name>.py`, a kit tool; `kit` defaults to root(). A repository
                     holds no copy of the kit's tools (decision 21 in docs/multi-host-plan.md
                     section 7), so a command printed for a repository names this path
  command(name, checkout, kit)
                     `python "<kit>/tools/<name>.py"`, with ` -C "<checkout>"` when one is given:
                     the command a printed hint names (forward slashes, both quoted)
  lock_tool(kit)     `<kit>/tools/machine_lock.py`, the lock CLI; with no `kit`, root()'s, or the
                     one beside the real kitlib when only a linked kitlib is here

A caller that prints a path quotes it itself: a kit path may hold a space.
"""
import os

from kitlib import platform


def _python():
    """The interpreter name a printed hint uses: stock Linux has only `python3`. (Same rule as
    machinesetup.common.python_name; kept local so kitroot depends on nothing above platform.)"""
    return 'python3' if platform.NAME == 'linux' else 'python'


def root():
    """The folder that holds this kitlib: the checkout this module is in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tool(name, kit=None):
    """The path of the kit tool `name` (no `.py`) under `kit` (default: root()). The file is not
    looked for."""
    return os.path.join(kit or root(), 'tools', name + '.py')


def command(name, checkout=None, kit=None):
    """`python "<kit>/tools/<name>.py"` (`python3` on Linux), plus ` -C "<checkout>"` when
    `checkout` is given, in forward slashes: a hint printed for a repository names the kit's own
    tool."""
    out = '%s "%s"' % (_python(), tool(name, kit).replace('\\', '/'))
    return out + (' -C "%s"' % checkout.replace('\\', '/') if checkout else '')


def lock_tool(kit=None):
    """The path of the lock CLI under `kit`, used as given. With no kit: `<root()>/tools/
    machine_lock.py` when that file is there; otherwise, when this kitlib is reached through a
    link (a junction over `kitlib` alone), the tool beside the folder the link resolves to, if that
    one is there; otherwise the spelled path. root() itself stays as spelled: a checkout reached
    whole through a link, or on a substituted drive, keeps the name it was reached by."""
    if kit:
        return tool('machine_lock', kit)
    spelled = tool('machine_lock')
    if os.path.isfile(spelled):
        return spelled
    real = tool('machine_lock', os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    return real if os.path.isfile(real) else spelled
