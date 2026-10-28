"""tools/kit_stubs.py memory, and kitlib.stubs.kit_root: the kit its owner command names.

Every case runs against a throwaway repository (tests/gitfixture.py); the fake kits are folders in
the fixture's temp. Run from an export of the kit (it holds kitlib.pluginsource.MARKER), the kit
named is KIT_LIVE_ROOT, else the main checkout the export's marker names, never the export a later
prune may remove.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import pluginsource, stubs
from tests import gitfixture

TOOL = os.path.join(stubs.KIT_ROOT, 'tools', 'kit_stubs.py')
SHA = '0123456789ab' * 3 + '0123'


class Case(unittest.TestCase):

    def setUp(self):
        # Not the run's own pin: a landing gate runs pinned (kitlib/gate/pin.py sets both), and
        # these tests choose the live root themselves.
        fixture = {k: v for k, v in gitfixture.fixture_env().items()
                   if k not in ('KIT_LIVE_ROOT', 'KIT_PINNED')}
        env = mock.patch.dict(os.environ, fixture, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.tmp = os.path.realpath(self.fx.base)
        self.root = os.path.realpath(self.fx.root)

    def read(self, path):
        with open(path, encoding='utf-8', newline='') as fh:
            return fh.read()

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def call(self, args, cwd):
        done = subprocess.run([sys.executable] + list(args), cwd=cwd, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env({'USERPROFILE': self.tmp, 'HOME': self.tmp}),
                              timeout=120)
        return done.returncode, (done.stdout or '') + (done.stderr or '')


class KitRoot(Case):
    """kit_root() from a checkout and from an export; stubs.KIT_ROOT is patched to the fake kit."""

    def setUp(self):
        super().setUp()
        self.export = os.path.join(self.tmp, 'kit-export')
        self.live = os.path.join(self.tmp, 'kit-live')
        for kit in (self.export, self.live):
            self.put(os.path.join(kit, 'tools', 'cmd_a.py'), "if __name__ == '__main__':\n    pass\n")
            self.put(os.path.join(kit, 'kit.json'), '{}\n')
        os.makedirs(os.path.join(self.live, '.git'))
        root = mock.patch.object(stubs, 'KIT_ROOT', self.export)
        root.start()
        self.addCleanup(root.stop)

    def marker(self, obj):
        self.put(os.path.join(self.export, pluginsource.MARKER), json.dumps(obj) + '\n')

    def test_a_checkout_names_itself_whatever_the_variable_says(self):
        other = os.path.join(self.tmp, 'kit-other')
        os.makedirs(other)
        with mock.patch.dict(os.environ, {pluginsource.LIVE_ROOT_ENV: other}):
            self.assertEqual(stubs.kit_root(), self.export)

    def test_an_export_names_the_main_checkout_its_marker_names(self):
        self.marker({'sha': SHA, 'checkout': self.live.replace('\\', '/')})
        self.assertEqual(os.path.normpath(stubs.kit_root()), os.path.normpath(self.live))

    def test_the_variable_wins_over_the_marker(self):
        self.marker({'sha': SHA, 'checkout': self.live.replace('\\', '/')})
        other = os.path.join(self.tmp, 'kit-other')
        os.makedirs(other)
        with mock.patch.dict(os.environ, {pluginsource.LIVE_ROOT_ENV: other}):
            self.assertEqual(stubs.kit_root(), other)

    def test_live_kit_refuses_an_export_whose_marker_names_no_kit_checkout(self):
        """No checkout, a folder that is gone, or a git checkout that is not the kit (no kit.json,
        or no tools folder): export_checkout accepts the last two, live_kit's own test does not."""
        nokitjson = os.path.join(self.tmp, 'repo-nokitjson')
        os.makedirs(os.path.join(nokitjson, '.git'))
        os.makedirs(os.path.join(nokitjson, 'tools'))
        notools = os.path.join(self.tmp, 'repo-notools')
        os.makedirs(os.path.join(notools, '.git'))
        self.put(os.path.join(notools, 'kit.json'), '{}\n')
        why = '%s is an export of the kit' % os.path.normpath(self.export)
        for obj in ({}, {'checkout': os.path.join(self.tmp, 'gone').replace('\\', '/')},
                    {'checkout': nokitjson.replace('\\', '/')},
                    {'checkout': notools.replace('\\', '/')}):
            self.marker(obj)
            with self.subTest(marker=obj):
                kit, refusal = stubs.live_kit(self.export)
                self.assertIsNone(kit)
                self.assertTrue(refusal.startswith(why), refusal)


class TheMemoryVerb(Case):
    """kit_stubs.py memory: the repository's autoMemoryDirectory (kitlib.stubs.memory). No case here
    writes the key with --apply into a file that lacks it: that value would name the temp folder's
    real drive (tests/stubs/test_memory.py writes against a faked main checkout instead)."""

    def settings(self):
        return os.path.join(self.root, '.claude', 'settings.json')

    def test_the_dry_run_names_the_value_and_the_command_and_writes_nothing(self):
        from kitlib.stubs import memory
        code, out = self.call([TOOL, 'memory', '-C', self.root], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('would add autoMemoryDirectory = %s' % memory.wanted(self.root), out)
        self.assertIn('memory -C "%s" --apply' % self.root, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))

    def test_a_current_value_is_ok_with_apply_and_unchanged(self):
        from kitlib.stubs import memory
        raw = json.dumps({'autoMemoryDirectory': memory.wanted(self.root)})
        self.put(self.settings(), raw)
        code, out = self.call([TOOL, 'memory', '--apply'], self.root)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(self.settings()), raw)

    def test_a_file_it_cannot_edit_exits_1(self):
        # The local file: --apply writes only there (kitlib.stubs.memory). A shared settings.json
        # that is not an object is skipped, and on real git the key then lands in the local file.
        local = os.path.join(self.root, '.claude', 'settings.local.json')
        self.put(local, '[1]')
        code, out = self.call([TOOL, 'memory', '--apply'], self.root)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.read(local), '[1]')

    def test_usage(self):
        """memory is the one verb."""
        for args in ([], ['write'], ['check'], ['memory', '--tools', 'landed'],
                     ['memory', '--dry-run'], ['memory', '-C', self.tmp]):
            code, out = self.call([TOOL] + args, self.root)
            self.assertEqual(code, 2, (args, out))
            self.assertIn('USAGE:', out)

    def test_a_broken_hosts_json_is_an_error_line_not_a_traceback(self):
        import contextlib
        import importlib.util
        import io
        from kitlib.host import registry
        spec = importlib.util.spec_from_file_location('kit_stubs_cli', TOOL)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        out = io.StringIO()
        with mock.patch.object(registry, 'load', side_effect=registry.RegistryError('hosts.json broken')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(['memory', '-C', self.root])
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn('kit_stubs: ERROR: hosts.json broken', out.getvalue())
        self.assertNotIn('Traceback', out.getvalue())


if __name__ == '__main__':
    unittest.main()
