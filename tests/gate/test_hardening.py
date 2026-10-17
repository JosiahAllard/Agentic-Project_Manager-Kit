"""The gate's second review round, cut to its contracts: the pushed commit is the commit the
merged gate passed, a SystemExit or Ctrl+C never leaves a merge or a lock behind silently, and a
landing where no gate step ran is refused.

Scratch repositories with a bare remote in temp and the scratch lock store only."""
import os
import unittest

from kitlib import gate as G
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, LandCase

# A step that does what the file MODE in the tree it runs in says, then prints ok; it exits 1
# when the mode ends with "red".
MODE_STEP = '''import os, subprocess, sys
m = open('MODE').read().strip() if os.path.exists('MODE') else ''
if m == 'edit':
    with open('README.md', 'a') as f:
        f.write('changed by the gate\\n')
elif m == 'commit':
    with open('c.txt', 'w') as f:
        f.write('c\\n')
    subprocess.check_call(['git', 'add', 'c.txt'])
    subprocess.check_call(['git', '-c', 'user.name=gate', '-c', 'user.email=gate@example.invalid',
                           'commit', '-q', '-m', 'made by the gate'])
elif m.startswith('untracked'):
    with open('left.txt', 'w') as f:
        f.write('x\\n')
print('ok')
sys.exit(1 if m.endswith('red') else 0)
'''

QUITS = (
    'import sys\n'
    'def quits(ctx):\n'
    '    ctx["log"].write("about to exit\\n")\n'
    '    sys.exit(0)\n'
    'GATE_CHECKS = {"quits": quits}\n')

MISSING = 'kit-no-such-command-for-tests'


def joined(out):
    return '\n'.join(out.lines)


class _CommittedTools(LandCase):
    """LandCase with files committed on main (and pushed) before each test."""
    FILES = {}

    def setUp(self):
        LandCase.setUp(self)
        if self.FILES:
            for rel, text in self.FILES.items():
                self.repo.write(rel, text)
            self.main = self.repo.commit('tools', *self.FILES)
            self.repo.git('push', '-q', 'origin', 'main')


# ---------------------------------------------------------------- 1 the pushed commit is the gated one

class GatedIsPushed(_CommittedTools):
    GATE = {'steps': [{'name': 'mode', 'always': True, 'cmd': ['{python}', 'tools/mode_step.py'],
                       'pass': '^ok$'}]}
    FILES = {'tools/mode_step.py': MODE_STEP}

    def land_mode(self, mode):
        _, tip = self.branch('feature', {'MODE': mode + '\n'})
        return self.lander(BRANCH, self.main, tip).run()

    def assert_nothing_pushed(self, out):
        self.assertEqual(self.remote_main(), self.main, out.lines)
        self.assertEqual(self.records(), [], out.lines)

    def assert_merge_named(self, out):
        """A green gate the landing tree did not keep to (Lander.unchanged) names the gated merge
        in its FAILED outcome, as a refused push does: a merge of the base, not on main."""
        self.assertRegex(out.facts.get('merge') or '', '^[0-9a-f]{40}$', out.lines)
        self.assertIn('parent %s' % self.main, self.repo.git('cat-file', '-p', out.merge))
        self.assertNotEqual(out.merge, self.main_now(), out.lines)

    def assert_main_checkout_untouched(self, out):
        """The step ran in the landing tree: the main checkout was never merged into, so it is
        at the base and clean, and the landing lock is released (nothing is left for a person)."""
        self.assertEqual(self.main_now(), self.main, out.lines)
        self.assertEqual(self.repo.git('status', '--porcelain'), '', out.lines)
        self.assertIsNone(self.lock().read(), out.lines)

    def test_a_step_that_commits_pushes_nothing(self):
        out = self.land_mode('commit')
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.undone)
        self.assertIn('HEAD moved', joined(out))
        self.assert_merge_named(out)
        self.assert_nothing_pushed(out)
        self.assert_main_checkout_untouched(out)
        self.assertNotIn('made by the gate', self.repo.git('log', '--format=%s', 'main'))


# ---------------------------------------------------------------- 2 SystemExit and Ctrl+C

class ChecksThatExit(_CommittedTools):
    KIT = dict(LandCase.KIT, hook={'module': 'kit_hooks'})
    GATE = {'steps': [{'name': 'quits', 'always': True, 'check': 'quits'}]}
    FILES = {'tools/kit_hooks.py': QUITS}

    def assert_logged_exit(self, logs):
        found = [os.path.join(d, f) for d, _, fs in os.walk(logs) for f in fs if f == 'quits.log']
        self.assertEqual(len(found), 1, found)
        with open(found[0], encoding='utf-8') as f:
            text = f.read()
        self.assertIn('SystemExit', text)
        self.assertIn('code 0', text)

    def test_sys_exit_0_in_a_landing_check_is_red_and_undone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.verdict.startswith('GATE FAILED: quits'), out.lines)
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.FAILED)
        self.assert_logged_exit(self.logs)


class CtrlC(LandCase):
    def test_ctrl_c_during_the_gate_undoes_the_merge_and_releases(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []

        def interrupted(lander):
            raise KeyboardInterrupt
        lander = self.lander(BRANCH, self.main, tip, say=said.append, merged_gate=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            lander.run()
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertTrue(any('undone' in s for s in said), said)


# ---------------------------------------------------------------- 3 a gate where nothing ran

SKIPPED = {'steps': [{'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$',
                      'requires': [MISSING]}]}


class LandingNothingRan(LandCase):
    GATE = SKIPPED

    def test_a_landing_where_nothing_ran_is_refused_and_undone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.lines[0].startswith('REFUSED: no gate step ran'), out.lines)
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.REFUSED)


if __name__ == '__main__':
    unittest.main()
