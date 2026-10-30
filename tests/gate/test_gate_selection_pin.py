"""The kit's own gate, pinned: a change to `.kit.json` "gate" that widens (or narrows) what a
branch, full, daily or landing gate runs fails here until the pin below is updated on purpose.

Pinned three ways, each against the checked-in `.kit.json` read over its defaults
(kitlib.config.load; the "gate" section reads no machine or user config) and through
kitlib.gate.detect.effective, the gate a run uses:
  section    the whole "gate" section (PINNED): a new step, glob, `always`, `smoke_steps`,
             `docs_only`, `setup` or `merged_args` is a difference
  argv       what kitlib.gate.steps.build gives each mode: diff, merged, full, daily
  selection  what kitlib.gate.steps.select picks for typical changes, with its reasons
The Widening cases feed widened copies of PINNED through the argv and selection checks alone, so
those checks are known to catch a widening even where the section pin would too.

A `.kit.json` change is unmapped in tests/selection.json, so it runs the full suite, this test
included.
"""
import copy
import os
import sys
import unittest

import kitlib.config as kconfig
from kitlib.gate import detect
from kitlib.gate import steps as S

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = 'C:/repo'
HERE = 'tests/gate/test_gate_selection_pin.py'
UPDATE = ('the kit\'s gate changed: .kit.json section "gate". If that is deliberate, update '
          'PINNED (and ARGV, SELECTION) in %s' % HERE)

SUITE = ['{python}', 'tools/kit_tests.py']
PINNED = {
    'steps': [{
        'name': 'kit-suite',
        'always': True,
        'globs': ['*'],
        'reuse': True,
        'cmd': SUITE + ['--select', '--smoke', '-C', '{root}'],
        'full_cmd': SUITE + ['--full', '-C', '{root}'],
        'daily_cmd': SUITE + ['--full', '--both-tiers', '-C', '{root}'],
        'pass': ['kit_tests: OK'],
    }],
    'setup': None,
    'worktree_guard': None,
    'warn_untracked': None,
    'fail': None,
    'warn_lines': None,
    'zero_tests': None,
    'errors': None,
    'kind': 'test',
    'smoke_steps': ['kit-suite'],
    'docs_only': {'globs': ['docs/*.md'], 'steps': ['kit-suite']},
}

# mode: (build keywords, {step: argv after {python}})
ARGV = {
    'diff': ({}, {'kit-suite': ['tools/kit_tests.py', '--select', '--smoke', '-C', ROOT]}),
    'merged': ({'merged': True},
               {'kit-suite': ['tools/kit_tests.py', '--select', '--smoke', '-C', ROOT]}),
    'full': ({'full': True}, {'kit-suite': ['tools/kit_tests.py', '--full', '-C', ROOT]}),
    'daily': ({'full': True, 'daily': True},
              {'kit-suite': ['tools/kit_tests.py', '--full', '--both-tiers', '-C', ROOT]}),
}

# (what the run is, changed paths, full): {step: why}, in run order
SELECTION = [
    ('a code change', ['kitlib/x/y.py'], False, {'kit-suite': 'always'}),
    ('a docs-only change', ['docs/a.md'], False, {'kit-suite': 'docs-only'}),
    ('a .kit.json change', ['.kit.json'], False, {'kit-suite': 'always'}),
    ('a docs change outside docs/*.md', ['docs/sub/a.md', 'README.md'], False,
     {'kit-suite': 'always'}),
    ('a full run', [], True, {'kit-suite': 'full'}),
]


def kit_gate():
    """The gate a run in the kit checkout uses: `.kit.json` "gate" over its defaults, the
    configured source (an auto-detected gate would pick its own steps)."""
    cfg = kconfig.load(KIT_ROOT, machine=lambda: {})
    eff = detect.effective(cfg, KIT_ROOT)
    return eff.gate, eff.source


def argv_problems(gate):
    out = []
    for mode, (kw, want) in ARGV.items():
        built = S.build(gate, ROOT, 'base', 'tools', **kw)
        got = {s.name: s.cmd[1:] for s in built if s.cmd}
        if any(s.cmd and s.cmd[0] != sys.executable for s in built):
            out.append('%s: a step does not start with {python}' % mode)
        if got != want:
            out.append('%s argv: %r, pinned %r' % (mode, got, want))
    return out


def selection_problems(gate):
    out = []
    built = S.build(gate, ROOT, 'base', 'tools')
    for what, changed, full, want in SELECTION:
        docs = not full and S.docs_only(gate, changed)
        chosen, why = S.select(gate, built, changed, full=full, read=lambda rel: None, docs=docs)
        got = [(s.name, why[s.name]) for s in chosen]
        if got != list(want.items()):
            out.append('%s %r: runs %r, pinned %r' % (what, changed, got, list(want.items())))
    return out


class Pin(unittest.TestCase):
    def setUp(self):
        self.gate, source = kit_gate()
        self.assertEqual(source, detect.CONFIG, UPDATE)

    def test_the_gate_section_is_the_pinned_one(self):
        self.assertEqual(self.gate, PINNED, UPDATE)

    def test_each_mode_builds_the_pinned_argv(self):
        self.assertEqual(argv_problems(self.gate), [], UPDATE)

    def test_typical_changes_select_the_pinned_steps(self):
        self.assertEqual(selection_problems(self.gate), [], UPDATE)


def widened(change):
    g = copy.deepcopy(PINNED)
    change(g)
    return g


def step(g):
    return next(s for s in g['steps'] if s['name'] == 'kit-suite')


# A `check` step, not a `cmd` one: steps.build gives it cmd None, which argv_problems skips, so only
# the selection check can catch an EXTRA step that gets selected.
EXTRA = {'name': 'extra', 'check': 'extra'}

WIDENINGS = {
    'cmd loses --select': lambda g: step(g)['cmd'].remove('--select'),
    'cmd is the full run': lambda g: step(g).update(cmd=list(step(g)['full_cmd'])),
    'full_cmd gains --both-tiers': lambda g: step(g)['full_cmd'].insert(3, '--both-tiers'),
    'daily_cmd gains an argument': lambda g: step(g)['daily_cmd'].append('--x'),
    'merged_args set': lambda g: step(g).update(merged_args=['--all']),
    'a new always step': lambda g: g['steps'].append(dict(EXTRA, always=True)),
    'a new step selected by a glob': lambda g: g['steps'].append(dict(EXTRA, globs=['kitlib/*'])),
    'a manual step made a smoke step': lambda g: (g['steps'].append(dict(EXTRA, manual=True)),
                                                  g['smoke_steps'].append('extra')),
    'docs_only steps gain a step': lambda g: (g['steps'].append(dict(EXTRA, manual=True)),
                                              g['docs_only']['steps'].append('extra')),
    'docs_only globs widen': lambda g: g['docs_only'].update(globs=['docs/**', '*.md']),
    'a setup argv': lambda g: g.update(setup=['{python}', '-c', 'pass']),
}


class Widening(unittest.TestCase):
    """The argv and selection checks catch each widening on their own (never decorative)."""

    def test_the_pin_itself_has_no_problems(self):
        self.assertEqual(argv_problems(PINNED) + selection_problems(PINNED), [])

    def test_each_widening_is_caught_by_argv_or_selection(self):
        for name, change in WIDENINGS.items():
            with self.subTest(name):
                g = widened(change)
                self.assertNotEqual(g, PINNED)
                self.assertTrue(argv_problems(g) + selection_problems(g), name)


if __name__ == '__main__':
    unittest.main()
