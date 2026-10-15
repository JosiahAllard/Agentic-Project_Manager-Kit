"""kitlib.stubs under a pinned run: the kit a stub runs is the live kit checkout
(kitlib.pluginsource.live_root), never the export of pushed main that kitlib.gate.pin runs gate.py
and land.py from. A downstream gate's kit-stubs step runs `{here}/kit_stubs.py check`, and `{here}`
is the export's tools folder (kitlib.gate.steps.KIT_TOOLS); the stubs a repository has committed
name the main checkout and must stay current. Run from the plugin's export with no KIT_LIVE_ROOT
(the session start hook), the kit is the main checkout the export's marker names; an export whose
marker names none is refused, never written into a stub.

The export and the live checkout are fake kits in the fixture's temp folder; stubs.KIT_ROOT is
patched to the export. No real repository and no ~/.claude* file is touched.
"""
import json
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

    def test_an_export_without_the_variable_or_a_marker_checkout_writes_nothing(self):
        """The export's marker ('{}') names no main checkout: a write would name the export, which
        a later prune may remove, so it is refused and no stub is written."""
        with mock.patch.dict(os.environ):
            os.environ.pop(pluginsource.LIVE_ROOT_ENV)
            code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.CONFIG, out)
        self.assertIn('kit_stubs: refused:', out)
        self.assertFalse(os.path.exists(self.stub('cmd_a')))


class FromTheExport(StubCase):
    """A run from the plugin's export with no KIT_LIVE_ROOT, as the session start hook runs: the
    export's marker names the main checkout it was made from (pluginsource.export_checkout)."""

    def setUp(self):
        super().setUp()
        self.export = os.path.join(self.tmp, 'kit-export')
        self.live = os.path.join(self.tmp, 'kit-live')
        for kit in (self.export, self.live):
            self.put(os.path.join(kit, 'tools', 'cmd_a.py'), "if __name__ == '__main__':\n    pass\n")
            self.put(os.path.join(kit, 'kit.json'), '{}\n')
        os.makedirs(os.path.join(self.live, '.git'))
        self.put(os.path.join(self.export, pluginsource.MARKER),
                 json.dumps({'sha': SHA, 'checkout': self.live.replace('\\', '/')}) + '\n')
        root = mock.patch.object(stubs, 'KIT_ROOT', self.export)
        root.start()
        self.addCleanup(root.stop)
        self.config(stubs={'tools': ['cmd_a']})

    def test_kit_root_is_the_main_checkout_the_marker_names(self):
        self.assertEqual(os.path.normpath(stubs.kit_root()), os.path.normpath(self.live))

    def test_a_stub_running_the_main_checkout_is_current(self):
        self.put(self.stub('cmd_a'), stubs.stub_text('cmd_a', kit=self.live))
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.OK, out)
        self.assertIsNone(stubs.problem_line(self.root))

    def test_write_names_the_main_checkout_never_the_export(self):
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        text = self.read(self.stub('cmd_a'))
        self.assertEqual(stubs.stub_kit(text), repr(os.path.normpath(self.live)))
        self.assertNotIn(self.export, text)

    def test_the_problem_lines_write_command_names_the_main_checkout(self):
        line = stubs.problem_line(self.root)
        self.assertIn('run python %s write' % os.path.join(os.path.normpath(self.live), 'tools',
                                                          'kit_stubs.py'), line)
        self.assertNotIn(self.export, line)

    def test_the_variable_wins_over_the_marker(self):
        other = os.path.join(self.tmp, 'kit-other')
        os.makedirs(other)
        with mock.patch.dict(os.environ, {pluginsource.LIVE_ROOT_ENV: other}):
            self.assertEqual(stubs.kit_root(), other)

    def test_an_explicit_export_kit_is_mapped_to_its_checkout(self):
        """machine_setup.py --repo --stubs passes its own folder as the kit: run from the plugin,
        that is the export."""
        code, out = self.run_stubs('write', kit=self.export)
        self.assertEqual(code, stubs.OK, out)
        self.assertEqual(stubs.stub_kit(self.read(self.stub('cmd_a'))),
                         repr(os.path.normpath(self.live)))


class ExportNamingNoCheckout(StubCase):
    """An export whose marker names no main checkout: an older marker with no `checkout`, or one
    naming a folder that is not a main checkout (no .git folder) or is gone. Nothing may name the
    export, so write and check are refused and the session start's line carries no command."""

    MARKERS = ('{}', '{"checkout": "%(plain)s"}', '{"checkout": "%(gone)s"}')

    def setUp(self):
        super().setUp()
        self.export = os.path.join(self.tmp, 'kit-export')
        self.put(os.path.join(self.export, 'tools', 'cmd_a.py'), "if __name__ == '__main__':\n    pass\n")
        self.plain = os.path.join(self.tmp, 'kit-plain')
        os.makedirs(os.path.join(self.plain, 'tools'))
        root = mock.patch.object(stubs, 'KIT_ROOT', self.export)
        root.start()
        self.addCleanup(root.stop)
        self.config(stubs={'tools': ['cmd_a']})

    def markers(self):
        for m in self.MARKERS:
            text = m % {'plain': self.plain.replace('\\', '/'),
                        'gone': os.path.join(self.tmp, 'gone').replace('\\', '/')}
            self.put(os.path.join(self.export, pluginsource.MARKER), text + '\n')
            yield text

    def test_write_is_refused_and_writes_nothing(self):
        for marker in self.markers():
            with self.subTest(marker=marker):
                code, out = self.run_stubs('write')
                self.assertEqual(code, stubs.CONFIG, out)
                self.assertIn('kit_stubs: refused:', out)
                self.assertFalse(os.path.exists(self.stub('cmd_a')))

    def test_check_is_refused(self):
        for marker in self.markers():
            with self.subTest(marker=marker):
                code, out = self.run_stubs('check')
                self.assertEqual(code, stubs.CONFIG, out)

    def test_problem_line_names_no_write_command(self):
        label = os.path.basename(self.root)
        for marker in self.markers():
            with self.subTest(marker=marker):
                line = stubs.problem_line(self.root)
                self.assertTrue(line.startswith('could not check stubs (%s): ' % label), line)
                self.assertNotIn('kit_stubs.py write', line)
                self.assertNotIn('run python', line)


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
