"""The gate offers a give back (kitlib.lock.giveback) to its last step only (kitlib.gate.runner)."""
import shutil
import tempfile
import types
import unittest
from unittest import mock

from kitlib.gate import runner


class Recorder:
    """A launcher that runs nothing and records what each start was given."""

    def __init__(self):
        self.started = []

    def start(self, cmd, **kw):
        self.started.append((cmd[0], kw.get('give_back', False)))
        return cmd

    def wait(self, p):
        return 0


def step(name):
    return types.SimpleNamespace(name=name, requires=[], requires_strict=False, check=None,
                                 cmd=[name], cwd='.', cont=False)


class LastStepOnly(unittest.TestCase):
    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix='kit-gate-give-')
        self.addCleanup(shutil.rmtree, self.logs, True)

    def test_only_the_last_step_may_give_back(self):
        launcher = Recorder()
        with mock.patch.object(runner.S, 'judge', return_value=('PASS', [])):
            results = runner.run_steps(launcher, [step('a'), step('b'), step('c')], None, {},
                                       self.logs)
        self.assertEqual([r['status'] for r in results], ['PASS'] * 3)
        self.assertEqual(launcher.started, [('a', False), ('b', False), ('c', True)])


if __name__ == '__main__':
    unittest.main()
