"""kitlib.stubs under a pinned run: the kit a stub runs is the live kit checkout
(kitlib.pluginsource.live_root), never the export of pushed main that kitlib.gate.pin runs gate.py
and land.py from. A downstream gate's kit-stubs step runs `{here}/kit_stubs.py check`, and `{here}`
is the export's tools folder (kitlib.gate.steps.KIT_TOOLS); the stubs a repository has committed
name the main checkout and must stay current.

The export and the live checkout are fake kits in the fixture's temp folder; stubs.KIT_ROOT is
patched to the export. No real repository and no ~/.claude* file is touched.
"""
import os
import unittest
from unittest import mock

from kitlib import pluginsource, stubs
from tests.stubs.test_kit_stubs import StubCase

SHA = '0123456789ab' * 3 + '0123'


class Pinned(StubCase):
    """A run from an export (it holds pluginsource.MARKER) with KIT_PINNED and KIT_LIVE_ROOT set,
    as kitlib.gate.pin starts it."""

    def setUp(self):
        super().setUp()
        self.export = os.path.join(self.tmp, 'kit-export')
        self.live = os.path.join(self.tmp, 'kit-live')
        for kit in (self.export, self.live):
            self.put(os.path.join(kit, 'tools', 'cmd_a.py'), "if __name__ == '__main__':\n    pass\n")
        self.put(os.path.join(self.export, pluginsource.MARKER), '{}\n')
        env = mock.patch.dict(os.environ, {'KIT_PINNED': SHA, pluginsource.LIVE_ROOT_ENV: self.live})
        env.start()
        self.addCleanup(env.stop)
        root = mock.patch.object(stubs, 'KIT_ROOT', self.export)
        root.start()
        self.addCleanup(root.stop)
        self.config(stubs={'tools': ['cmd_a']})

    def test_a_stub_running_the_live_root_is_current(self):
        self.put(self.stub('cmd_a'), stubs.stub_text('cmd_a', kit=self.live))
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.OK, out)
        self.assertIn('kit_stubs: 1 stub(s) current', out)
        self.assertIsNone(stubs.problem_line(self.root))

    def test_a_stub_running_the_export_is_stale(self):
        self.put(self.stub('cmd_a'), stubs.stub_text('cmd_a', kit=self.export))
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('stale    %s (runs the kit at %r)' % (self.stub('cmd_a'), self.export), out)

    def test_write_names_the_live_root_never_the_export(self):
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        text = self.read(self.stub('cmd_a'))
        self.assertEqual(stubs.stub_kit(text), repr(os.path.normpath(self.live)))
        self.assertNotIn(self.export, text)
        self.assertEqual(self.run_stubs('check')[0], stubs.OK)

    def test_the_problem_lines_write_command_names_the_live_root(self):
        line = stubs.problem_line(self.root)
        self.assertIn('run python %s write' % os.path.join(self.live, 'tools', 'kit_stubs.py'), line)
        self.assertNotIn(self.export, line)

    def test_an_export_without_the_variable_names_itself(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(pluginsource.LIVE_ROOT_ENV)
            self.assertEqual(self.run_stubs('write')[0], stubs.OK)
        self.assertEqual(stubs.stub_kit(self.read(self.stub('cmd_a'))),
                         repr(os.path.normpath(self.export)))


class Unpinned(StubCase):
    """A checkout (no MARKER) names itself whatever KIT_LIVE_ROOT says: unchanged behaviour."""

    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.tmp, 'kit-other')
        os.makedirs(self.other)
        env = mock.patch.dict(os.environ, {pluginsource.LIVE_ROOT_ENV: self.other})
        env.start()
        self.addCleanup(env.stop)
        self.config(stubs={'tools': ['landed']})

    def test_write_and_check_use_this_checkout(self):
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        self.assertEqual(stubs.stub_kit(self.read(self.stub('landed'))),
                         repr(os.path.normpath(stubs.KIT_ROOT)))
        self.assertEqual(self.run_stubs('check')[0], stubs.OK)
        self.assertIsNone(stubs.problem_line(self.root))

    def test_a_stub_running_the_variables_folder_is_stale(self):
        self.put(self.stub('landed'), stubs.stub_text('landed', kit=self.other))
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('(runs the kit at %r)' % self.other, out)


if __name__ == '__main__':
    unittest.main()
