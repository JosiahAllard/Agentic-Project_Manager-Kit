"""Where this kit is, for code that names one of its tools in a command it prints or runs.

  root()             the kit checkout (or plugin export) this module is in
  lock_tool(kit)     `<kit>/tools/machine_lock.py`, the lock CLI; `kit` defaults to root()

A caller that prints a path quotes it itself: a kit path may hold a space.
"""
import os


def root():
    """The folder that holds this kitlib: the checkout this module is in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lock_tool(kit=None):
    """The path of the lock CLI under `kit` (default: root()). The file is not looked for."""
    return os.path.join(kit or root(), 'tools', 'machine_lock.py')
