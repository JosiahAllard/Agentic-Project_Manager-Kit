"""The time fence (docs/flaky-tests.md): a wall-clock budget whose expiry is a normal, visible
output (a degrade, such as kitlib.agentstart's `NOT CHECKED - the rest: over the 1.0 s budget`)
must not let the machine's load decide a test's result.

  fenced(environ=None)   True when the mapping (default os.environ) carries FENCE_ENV = '1'
  budget(seconds)        `seconds`, or CEILING in a fenced process

A degrade budget takes `budget=None` and resolves it with `budget(DEFAULT)` at call time; a budget
a caller passes explicitly is never fenced, so a test of the over-budget path passes one. A ceiling
(a timeout that only ends a hang) does not go through here.

Who sets the marker: tests/__init__.py for a direct `python -m unittest` run, and tools/kit_tests.py
`child_env` for its units (a pool worker replaces its environment for each unit). Production never
sets it, so a real run gets `seconds`. The marker is read at each call. Not fenced: a child whose
environment a test built without the marker, and a child that is not the kit's Python code.
CEILING is past kitlib.tooldeps.selfrun.UNIT_TIMEOUT, so a real hang still ends, as a unit TIMEOUT.
"""
import os

FENCE_ENV = 'KIT_TIME_FENCE'
CEILING = 600.0


def fenced(environ=None):
    return (os.environ if environ is None else environ).get(FENCE_ENV) == '1'


def budget(seconds):
    return CEILING if fenced() else seconds
