"""kitlib.kitcmd and tools/kit.py: which export the `kit` command runs, the refusals that name the
fix, the tool names it takes, and how it runs one (the caller's directory, the arguments as given,
the tool's exit code, Ctrl+C).

Every export here is a fake one in a temporary folder: a <sha12> folder holding the export marker
and a tools folder, named by a marketplace.json kitlib.pluginsource.write_marketplace writes. The
machine config is never read: resolve takes the Settings from its `load` seam.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import kitcmd, pluginsource, stubs
from kitlib.config import ConfigError
from kitlib.config.user import HomeFenced
from tests.platforms import windows_only

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHA = 'a1b2c3d4e5f6'
OTHER = '0f9e8d7c6b5a'

ECHO = '''import json, os, sys
sys.stdout.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:]}))
sys.exit(int(os.environ.get('ECHO_EXIT', '0')))

if __name__ == '__main__':
    pass
'''
CRASH = '''import ctypes
ctypes.windll.kernel32.ExitProcess(0xC000013A)

if __name__ == '__main__':
    pass
'''


def cli():
    spec = importlib.util.spec_from_file_location('kit_cli', os.path.join(ROOT, 'tools', 'kit.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Exports(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kitcmd-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, 'plugin')
        self.here = os.path.join(self.tmp, 'checkout')       # no marker: a kit checkout
        os.makedirs(os.path.join(self.here, 'tools'))

    def export(self, name=SHA, root=None, marker=True, tools=None):
        folder = os.path.join(root or self.root, name)
        os.makedirs(os.path.join(folder, 'tools'), exist_ok=True)
        if marker:
            with open(os.path.join(folder, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
                fh.write('{}\n')
        for tool, text in (tools or {}).items():
            with open(os.path.join(folder, 'tools', tool), 'w', encoding='utf-8') as fh:
                fh.write(text)
        return folder

    def name(self, folder, root=None):
        pluginsource.write_marketplace(root or self.root, folder)

    def load(self):
        return pluginsource.Settings(self.root, False, 'origin')

    def resolve(self, here=None, load=None):
        return kitcmd.resolve(here or self.here, load or self.load)


class Resolve(Exports):
    def test_the_folder_the_marketplace_names_is_the_kit(self):
        folder = self.export()
        self.name(SHA)
        self.assertEqual(self.resolve(), (folder, None))

    def test_a_refresh_is_used_at_the_next_call(self):
        self.export()
        self.name(SHA)
        newer = self.export(OTHER)
        self.name(OTHER)
        self.assertEqual(self.resolve(), (newer, None))

    def test_no_marketplace_is_refused_with_the_refresh(self):
        self.export()
        folder, why = self.resolve()
        self.assertIsNone(folder)
        self.assertIn('marketplace.json is missing', why)
        self.assertIn('plugin_refresh.py', why)

    def test_a_named_folder_that_is_gone_is_refused(self):
        self.name(SHA)
        folder, why = self.resolve()
        self.assertIsNone(folder)
        self.assertIn('is missing or is not an export', why)
        self.assertIn('plugin_refresh.py', why)

    def test_a_named_folder_with_no_marker_is_refused(self):
        self.export(marker=False)
        self.name(SHA)
        self.assertIsNone(self.resolve()[0])

    def test_a_name_outside_the_export_root_is_refused(self):
        self.export(root=self.tmp, name='elsewhere')
        self.name('../elsewhere')
        folder, why = self.resolve()
        self.assertIsNone(folder)
        self.assertIn('is not a folder of', why)

    def test_a_machine_config_that_cannot_be_read_is_refused_with_the_fix(self):
        self.export()
        self.name(SHA)
        for error in (ConfigError('pointer is BAD'), pluginsource.RefreshError('LOCALAPPDATA'),
                      HomeFenced('fenced'), OSError('denied')):
            with self.subTest(error=type(error).__name__):
                def load():
                    raise error
                folder, why = self.resolve(load=load)
                self.assertIsNone(folder)
                self.assertIn('the machine config cannot be read', why)
                self.assertIn('machine_setup.py --pm-repo', why)

    def test_an_export_outside_the_configured_root_is_refused(self):
        self.export()
        self.name(SHA)
        stray = self.export(root=os.path.join(self.tmp, 'old-root'))
        folder, why = self.resolve(here=stray)
        self.assertIsNone(folder)
        self.assertIn('not under the plugin export folder', why)
        self.assertIn(os.path.join(self.root, 'bin'), why)

    def test_an_export_under_the_configured_root_may_run_the_current_one(self):
        old = self.export()
        newer = self.export(OTHER)
        self.name(OTHER)
        self.assertEqual(self.resolve(here=old), (newer, None))
        pin = self.export(root=os.path.join(self.root, 'pins'))
        self.assertEqual(self.resolve(here=pin), (newer, None))


class Names(Exports):
    def test_commands_are_the_tools_with_a_main_block_but_kit_and_helpers(self):
        main = "\n\nif __name__ == '__main__':\n    pass\n"
        folder = self.export(tools={'gate.py': main, 'kit.py': main, '_host.py': main,
                                    'lib.py': 'X = 1\n', 'notes.txt': main})
        os.makedirs(os.path.join(folder, 'tools', 'pkg.py'))
        self.assertEqual(kitcmd.commands(folder), ['gate'])

    def test_the_kit_s_own_commands(self):
        have = kitcmd.commands(ROOT)
        for name in ('gate', 'land', 'backlog', 'preflight', 'kit_stubs'):
            self.assertIn(name, have)
        self.assertNotIn('kit', have)
        self.assertNotIn('_host', have)

    def test_check_name(self):
        have = ['gate', 'land']
        self.assertIsNone(kitcmd.check_name('gate', have))
        self.assertIn('does not run itself', kitcmd.check_name('kit', have))
        self.assertIn('without .py: kit gate', kitcmd.check_name('gate.py', have))
        self.assertIn("no kit tool named 'nope'", kitcmd.check_name('nope', have))
        self.assertIn('no kit tool', kitcmd.check_name('..\\gate', have))

    def test_stubs_reads_the_partition_from_kitcmd(self):
        self.assertIs(stubs.SHARED, kitcmd.SHARED)
        self.assertIs(stubs.KIT_ONLY, kitcmd.KIT_ONLY)
        self.assertIn('kit', kitcmd.KIT_ONLY)


@windows_only('the kit runs on Windows only')
class Run(Exports):
    """A real child: a python running kitcmd.run in a directory of its own, on a fake export."""

    def call(self, tool, args, env=None):
        caller = os.path.join(self.tmp, 'caller dir')
        os.makedirs(caller, exist_ok=True)
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from kitlib import kitcmd; '
                'sys.exit(kitcmd.run(sys.argv[2], sys.argv[3], sys.argv[4:]))')
        folder = self.export(tools={'echo.py': ECHO, 'crash.py': CRASH})
        environ = dict(os.environ, **(env or {}))
        p = subprocess.run([sys.executable, '-c', code, ROOT, folder, tool] + args, cwd=caller,
                           env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=60, creationflags=0x08000000)
        return p, caller

    def test_the_tool_runs_in_the_caller_s_directory_with_the_arguments_as_given(self):
        args = ['-C', 'Z:/some checkout', '--message=a "quoted" word', '', '%PATH%', 'x y']
        p, caller = self.call('echo', args, {'ECHO_EXIT': '7'})
        self.assertEqual(p.returncode, 7, p.stderr)
        seen = json.loads(p.stdout.decode('utf-8'))
        self.assertEqual(os.path.normcase(seen['cwd']), os.path.normcase(caller))
        self.assertEqual(seen['argv'], args)

    def test_a_crash_code_passes_through_with_all_32_bits(self):
        p, _caller = self.call('crash', [])
        self.assertEqual(p.returncode, 0xC000013A, p.stderr)

    def test_the_child_s_command_line_names_the_export_folder(self):
        folder = self.export()
        line = kitcmd.argv(folder, 'gate', ['--full'])
        self.assertEqual(line[0], sys.executable)
        self.assertEqual(line[1], os.path.join(self.root, SHA, 'tools', 'gate.py'))
        self.assertEqual(line[2:], ['--full'])
        self.assertIn(SHA, subprocess.list2cmdline(line).replace('/', '\\').split('\\'))


class Wait(unittest.TestCase):
    class Proc:
        def __init__(self, *outcomes):
            self.outcomes = list(outcomes)
            self.killed = 0

        def wait(self):
            out = self.outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return out

        def kill(self):
            self.killed += 1

    def test_the_tool_s_code(self):
        self.assertEqual(kitcmd.wait(self.Proc(3)), 3)
        self.assertEqual(kitcmd.wait(self.Proc(0xC0000005)), 0xC0000005 - 0x100000000)

    def test_a_first_ctrl_c_waits_for_the_tool_to_end(self):
        proc = self.Proc(KeyboardInterrupt(), 0)
        self.assertEqual(kitcmd.wait(proc), kitcmd.INTERRUPTED)
        self.assertEqual(proc.killed, 0)

    def test_a_second_ctrl_c_ends_the_tool(self):
        proc = self.Proc(KeyboardInterrupt(), KeyboardInterrupt(), 1)
        self.assertEqual(kitcmd.wait(proc), kitcmd.INTERRUPTED)
        self.assertEqual(proc.killed, 1)

    def test_run_starts_the_command_line_argv_gives(self):
        seen = []

        def popen(line):
            seen.append(line)
            return self.Proc(5)
        self.assertEqual(kitcmd.run('K:\\kit', 'gate', ['-C', 'x'], popen=popen), 5)
        self.assertEqual(seen, [kitcmd.argv('K:\\kit', 'gate', ['-C', 'x'])])


class Cli(unittest.TestCase):
    def setUp(self):
        self.kit = cli()

    def main(self, argv, platform='win32', resolve=('K:\\kit', None), have=('gate', 'land'),
             code=0):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(kitcmd, 'resolve', return_value=resolve) as res, \
                mock.patch.object(kitcmd, 'commands', return_value=list(have)), \
                mock.patch.object(kitcmd, 'run', return_value=code) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            got = self.kit.main(argv, platform=platform)
        return got, out.getvalue(), err.getvalue(), res, run

    def test_the_tool_runs_with_its_arguments_and_its_code_is_the_exit(self):
        got, _out, _err, _res, run = self.main(['gate', '-C', 'Z:/w', '--full'], code=9)
        self.assertEqual(got, 9)
        run.assert_called_once_with('K:\\kit', 'gate', ['-C', 'Z:/w', '--full'])

    def test_no_arguments_and_help_list_the_tools(self):
        for argv in ([], ['-h'], ['--help']):
            with self.subTest(argv=argv):
                got, out, _err, _res, run = self.main(argv)
                self.assertEqual(got, 0)
                self.assertIn('tools: gate land', out)
                self.assertIn('kit folder: K:\\kit', out)
                run.assert_not_called()

    def test_refusals_exit_2_with_one_kit_line(self):
        cases = {
            'not windows': dict(argv=['gate'], platform='linux'),
            'an option first': dict(argv=['-C', 'x', 'gate']),
            'unknown tool': dict(argv=['nope']),
            'kit itself': dict(argv=['kit']),
            'a .py name': dict(argv=['gate.py']),
            'no export': dict(argv=['gate'], resolve=(None, 'no plugin export is named')),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                got, out, err, _res, run = self.main(**kw)
                self.assertEqual(got, kitcmd.REFUSED)
                self.assertEqual(out, '')
                self.assertTrue(err.startswith('kit: ') and err.count('\n') == 1, err)
                run.assert_not_called()

    def test_not_windows_reads_no_config(self):
        _got, _out, _err, res, _run = self.main(['gate'], platform='linux')
        res.assert_not_called()

    def test_a_tool_that_cannot_be_started_is_refused(self):
        with mock.patch.object(kitcmd, 'resolve', return_value=('K:\\kit', None)), \
                mock.patch.object(kitcmd, 'commands', return_value=['gate']), \
                mock.patch.object(kitcmd, 'run', side_effect=OSError('no python')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.kit.main(['gate'], platform='win32'), kitcmd.REFUSED)
        self.assertIn('could not start', err.getvalue())


if __name__ == '__main__':
    unittest.main()
