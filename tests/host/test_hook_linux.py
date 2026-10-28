"""The Linux hook command line (kitlib.host.hookmain.manifest_command, rendered into each plugin's
hooks/hooks.linux.json), run for real under /bin/sh: the script and its arguments reach python3
intact, a tool event that fails or finds no python3 exits 2 (the guard-error deny, as boot/hook.py
blocks), and a start event that fails says the kit did not load and exits 0."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from hosts.codex.adapter import content as codex_content
from kitlib.host import hookmain
from tests.platforms import posix_only

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The stand-in hook: prints its arguments, then exits with the last one.
SCRIPT = 'import sys\nprint(" ".join(sys.argv[1:]))\nsys.exit(int(sys.argv[-1]))\n'
DID_NOT_LOAD = 'PM kit: the kit did not load: python3 failed or is not on PATH'


@posix_only('the Linux hook line runs under /bin/sh')
class LinuxHookLine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-hook-line-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.script = os.path.join(self.dir, 'hook.py')
        with open(self.script, 'w', encoding='utf-8') as f:
            f.write(SCRIPT)
        self.bin = os.path.join(self.dir, 'bin')         # python3 on PATH: this interpreter
        self.empty = os.path.join(self.dir, 'empty')     # no python3 anywhere on PATH
        os.mkdir(self.bin)
        os.mkdir(self.empty)
        python3 = os.path.join(self.bin, 'python3')
        with open(python3, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
        os.chmod(python3, os.stat(python3).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_line(self, code, blocking, path=None, script=None, args=None, stdin='', env=None):
        line = hookmain.manifest_command(script or self.script, 'linux',
                                         *(args if args is not None else ['a b', str(code)]), blocking=blocking)
        self.assertEqual(line['command'], '/bin/sh')
        full = dict(os.environ, PATH=path or self.bin, **(env or {}))
        return subprocess.run([line['command']] + line['args'], input=stdin, capture_output=True, text=True,
                              env=full, timeout=60)

    def test_a_tool_event_passes_the_scripts_answer_and_turns_any_failure_into_2(self):
        r = self.run_line(0, True)
        self.assertEqual((r.returncode, r.stdout), (0, 'a b 0\n'))
        for code in (1, 2, 3):
            with self.subTest(code=code):
                self.assertEqual(self.run_line(code, True).returncode, 2)

    def test_a_tool_event_with_no_python3_on_path_exits_2(self):
        r = self.run_line(0, True, path=self.empty)
        self.assertEqual((r.returncode, r.stdout), (2, ''))

    def test_a_start_event_passes_the_scripts_output_and_says_a_failure(self):
        r = self.run_line(0, False)
        self.assertEqual((r.returncode, r.stdout), (0, 'a b 0\n'))
        r = self.run_line(1, False)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, 'a b 1\n%s (exit 1)\n' % DID_NOT_LOAD)

    def test_a_start_event_with_no_python3_on_path_says_the_kit_did_not_load(self):
        r = self.run_line(0, False, path=self.empty)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '%s (exit 127)\n' % DID_NOT_LOAD)

    def test_codex_runs_the_same_line_as_one_shell_string(self):
        line = hookmain.manifest_command('${PLUGIN_ROOT}/hook.py', 'linux', 'a b', '0', blocking=True)
        text = codex_content.CodexContent.command_text(line, 'linux')
        env = dict(os.environ, PATH=self.bin, PLUGIN_ROOT=self.dir)
        r = subprocess.run(['/bin/sh', '-c', text], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, 'a b 0\n'))
        r = subprocess.run(['/bin/sh', '-c', text], capture_output=True, text=True,
                           env=dict(env, PATH=self.empty), timeout=60)
        self.assertEqual(r.returncode, 2)

    def test_the_kits_own_hook_entry_gets_its_arguments_and_payload(self):
        """boot/hook.py of this checkout through the line: an unguarded call skips (exit 0) before
        the kit boots; a guarded one whose kit will not boot blocks, and the line keeps the 2."""
        hook = os.path.join(ROOT, 'boot', 'hook.py')
        skip = self.run_line(None, True, script=hook, args=['claude', 'PreToolUse'],
                             stdin=json.dumps({'tool_name': 'Glob'}))
        self.assertEqual((skip.returncode, skip.stdout, skip.stderr), (0, '', ''))
        blocked = self.run_line(None, True, script=hook, args=['claude', 'PreToolUse'],
                                stdin=json.dumps({'tool_name': 'Edit'}), env={'PM_KIT_ROOT': 'relative'})
        self.assertEqual(blocked.returncode, 2)
        self.assertIn('BootError', blocked.stderr)


if __name__ == '__main__':
    unittest.main()
