"""boot/hook.py, the one hook entry of every host (plan decision 20; docs/new-host.md): what it does
before the kit boots, with the standard library alone. The run through a host's adapter is the
conformance suite's (tests/host/conformance.py) and tests/guard/test_entry.py's."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('hook_under_test', ROOT / 'boot' / 'hook.py')
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
HOOK = {'tool_event': 'Pre', 'tool_field': 'call.name', 'guarded_tools': ['edit', 'mcp__*'], 'fast_path': 'all'}
ENTRY = {'adapter': 'x.adapter', 'shell': 'hosts/x/shell', 'capabilities': {'hook_events': ['Pre', 'Start']},
         'hook': HOOK}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-hook-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def hosts(self, data):
        path = os.path.join(self.dir, 'hosts.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return path


class BeforeBoot(Scratch):
    def test_the_entry_is_read_from_the_hosts_json_beside_boot(self):
        self.assertEqual(H.HOSTS, str(ROOT / 'hosts.json'))
        path = self.hosts({'x': ENTRY})
        self.assertEqual(H.entry_of('x', path), ENTRY)
        for host, data in (('y', {'x': ENTRY}), ('x', {'x': 'no'}), ('x', []), (None, {'x': ENTRY})):
            with self.subTest(host=host, data=data), self.assertRaises(KeyError):
                H.entry_of(host, self.hosts(data))

    def test_the_tool_is_read_at_the_dotted_field(self):
        self.assertEqual(H.tool_of({'call': {'name': 'edit'}}, 'call.name'), 'edit')
        self.assertEqual(H.tool_of({'tool_name': 'Read'}, 'tool_name'), 'Read')
        for payload in ({}, {'call': 'edit'}, [], None, {'call': {}}):
            self.assertIsNone(H.tool_of(payload, 'call.name'))

    def test_only_an_unguarded_named_tool_skips(self):
        call = lambda name: {'call': {'name': name}}        # noqa: E731
        self.assertTrue(H.skips(HOOK, call('read'), {}))
        for payload in (call('edit'), call('mcp__a__b'), call(3), {}, [], None, {'call': {}}):
            with self.subTest(payload=payload):
                self.assertFalse(H.skips(HOOK, payload, {}))

    def test_hook_data_of_the_wrong_shape_skips_nothing(self):
        read = {'call': {'name': 'read'}}
        for tools in ([], 'edit', ['edit', 3], ['edit', ''], None, {'edit': 1}):
            with self.subTest(tools=tools):
                self.assertFalse(H.skips(dict(HOOK, guarded_tools=tools), read, {}))

    def test_fast_path_unmarked_skips_only_an_unmarked_session(self):
        hook = dict(HOOK, fast_path='unmarked')
        read = {'call': {'name': 'read'}}
        for value, skip in ((None, True), ('', True), ('  ', True), ('worker', False), (' pm ', False)):
            env = {} if value is None else {H.MARK: value}
            self.assertEqual(H.skips(hook, read, env), skip, value)
        self.assertTrue(H.skips(HOOK, read, {H.MARK: 'worker'}), 'fast_path all: any session')
        self.assertFalse(H.skips(dict(HOOK, fast_path='never'), read, {}), 'an unknown value never skips')


class WhichKit(Scratch):
    def test_the_checkout_beside_boot_needs_no_here(self):
        self.assertIsNone(H.kit_here(ENTRY))                # this file's own kit: kitboot's default

    def test_a_plugin_copy_finds_the_checkout_its_shell_is_in(self):
        kit = Path(self.dir) / 'kit'
        shell = kit / 'hosts' / 'x' / 'shell'
        shell.mkdir(parents=True)
        self.assertIsNone(H.kit_here(ENTRY, str(shell)), 'no kit.json above it: PM_KIT_ROOT or nothing')
        (kit / 'kit.json').write_text('{}', encoding='utf-8')
        self.assertEqual(os.path.normpath(H.kit_here(ENTRY, str(shell))), str(kit / 'boot' / 'kitboot.py'))
        self.assertIsNone(H.kit_here(dict(ENTRY, shell='a/b'), str(shell)), 'as many folders as the shell has')


class Failures(Scratch):
    def run_hook(self, argv, stdin='{}'):
        out, err = io.StringIO(), io.StringIO()
        code = H.run(argv, io.StringIO(stdin), out, err, {}, self.hosts({'x': ENTRY}))
        return code, out.getvalue(), err.getvalue()

    def test_usage(self):
        for argv in ([], ['x'], ['x', 'Pre', 'a', 'b'], ['x', '--settings-entry', 'a']):
            with self.subTest(argv=argv):
                code, out, err = self.run_hook(argv)
                self.assertEqual((code, out), (H.BLOCK, ''))
                self.assertIn('usage', err)

    def test_an_unknown_host_blocks(self):
        code, out, err = self.run_hook(['y', 'Pre'])
        self.assertEqual((code, out), (H.BLOCK, ''))
        self.assertIn("no host 'y'", err)

    def test_a_command_line_the_entry_cannot_use_names_the_machine_setup(self):
        for argv in (['x'], ['y', 'Pre']):
            with self.subTest(argv=argv):
                err = self.run_hook(argv)[2]
                self.assertIn('re-run python "%s/tools/machine_setup.py"' % str(ROOT).replace(os.sep, '/'), err)

    def test_unreadable_hook_data_blocks_every_call(self):
        out, err = io.StringIO(), io.StringIO()
        missing = os.path.join(self.dir, 'none.json')
        self.assertEqual(H.run(['x', 'Pre'], io.StringIO('{"call": {"name": "read"}}'), out, err, {}, missing),
                         H.BLOCK)
        bad = self.hosts({'x': dict(ENTRY, hook=dict(HOOK, guarded_tools='edit'))})
        with mock.patch.dict(os.environ, {'PM_KIT_ROOT': 'relative'}):
            self.assertEqual(H.run(['x', 'Pre'], io.StringIO('{"call": {"name": "read"}}'), out, err, {}, bad),
                             H.BLOCK, 'no skip: the kit boots, and here fails')

    def test_a_system_exit_on_the_way_blocks_a_tool_call(self):
        with mock.patch.object(H, 'main', side_effect=SystemExit(0)):
            self.assertEqual(self.run_hook(['x', 'Pre'], '{"call": {"name": "edit"}}')[0], H.BLOCK)
            self.assertEqual(self.run_hook(['x', 'Start'])[0], 0)

    def test_the_home_fence_on_the_way_blocks_a_tool_call(self):
        """kitlib.config.user.HomeFenced is a BaseException: a guard that meets the home fence
        (kitlib.guard.shell.home_folder) passes hookmain's `except Exception`, and run() blocks."""
        from kitlib.config import user as kuser
        with mock.patch.object(H, 'main', side_effect=kuser.HomeFenced(kuser.FENCE_MESSAGE)):
            code, out, err = self.run_hook(['x', 'Pre'], '{"call": {"name": "edit"}}')
            self.assertEqual((code, out), (H.BLOCK, ''))
            self.assertIn('HomeFenced', err)

    def test_a_skipped_call_never_boots(self):
        self.assertEqual(self.run_hook(['x', 'Pre'], '{"call": {"name": "read"}}'), (0, '', ''))

    def test_a_kit_that_will_not_boot_blocks_a_tool_call_and_is_said_at_a_start(self):
        with mock.patch.dict(os.environ, {'PM_KIT_ROOT': 'relative'}):
            code, out, err = self.run_hook(['x', 'Pre'], '{"call": {"name": "edit"}}')
            self.assertEqual((code, out), (H.BLOCK, ''))
            self.assertIn('BootError', err)
            code, out, err = self.run_hook(['x', 'Start', 'part'])
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith('PM kit (part): the kit did not load: BootError:'), out)


if __name__ == '__main__':
    unittest.main()
