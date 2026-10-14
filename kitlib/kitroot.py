"""Where this kit is, for code that names one of its tools in a command it prints or runs.

  root()             the kit checkout (or plugin export) this module is in
  tool(name, kit)    `<kit>/tools/<name>.py`, a kit tool; `kit` defaults to root(). A repository
                     holds no copy of the kit's tools (decision 21 in docs/multi-host-plan.md
                     section 7), so a command printed for a repository names this path
  command(name, checkout, kit)
                     `python "<kit>/tools/<name>.py"`, with ` -C "<checkout>"` when one is given:
                     the command a printed hint names (forward slashes, both quoted)
  lock_tool(kit)     `<kit>/tools/machine_lock.py`, the lock CLI; `kit` defaults to root()

A caller that prints a path quotes it itself: a kit path may hold a space.
"""
import os


def root():
    """The folder that holds this kitlib: the checkout this module is in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tool(name, kit=None):
    """The path of the kit tool `name` (no `.py`) under `kit` (default: root()). The file is not
    looked for."""
    return os.path.join(kit or root(), 'tools', name + '.py')


def command(name, checkout=None, kit=None):
    """`python "<kit>/tools/<name>.py"`, plus ` -C "<checkout>"` when `checkout` is given, in
    forward slashes: a hint printed for a repository names the kit's own tool."""
    out = 'python "%s"' % tool(name, kit).replace('\\', '/')
    return out + (' -C "%s"' % checkout.replace('\\', '/') if checkout else '')


def lock_tool(kit=None):
    """The path of the lock CLI under `kit` (default: root()). The file is not looked for."""
    return tool('machine_lock', kit)
