"""installer/machine-setup.sh, the Linux front end of tools/machine_setup.py: the arguments it checks
and passes on, the interpreter it runs, and its refusal off Linux. It runs under sh (Git Bash on
Windows) with a fake `uname` on PATH and a fake interpreter that prints the arguments it was given,
so each case runs on any host with a POSIX shell."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from kitlib.proc import bash

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, 'installer', 'machine-setup.sh').replace('\\', '/')


def posix_shell():
    """The shell the cases run under. On Windows Git's own usr/bin/bash.exe: the bin/bash.exe
    launcher (the one found when only Git's cmd folder is on PATH, as in a scheduled run) resets
    PATH so Git's real uname comes before the fake one."""
    if os.name != 'nt':
        return shutil.which('sh')
    found = bash.git_bash()
    if found:
        git = os.path.dirname(os.path.dirname(found))
        if os.path.basename(git).lower() == 'usr':
            git = os.path.dirname(git)
        usr = os.path.join(git, 'usr', 'bin', 'bash.exe')
        if os.path.isfile(usr):
            return usr
    return found


SHELL = posix_shell()


def fake(path, text):
    with open(path, 'w', newline='\n') as f:
        f.write(text)
    os.chmod(path, 0o755)


@unittest.skipUnless(SHELL, 'no POSIX shell (Git Bash on Windows) to run the installer')
class InstallerSh(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, 'bin')
        os.makedirs(self.bin)
        self.python = os.path.join(self.dir, 'py').replace('\\', '/')
        fake(self.python, '#!/bin/sh\nfor a; do printf "[%s]" "$a"; done; echo\n')
        self.system('Linux')

    def system(self, name):
        fake(os.path.join(self.bin, 'uname'), '#!/bin/sh\necho %s\n' % name)

    def run_sh(self, *args, python=True):
        # The shell's own folder after the fake: its dirname, tr and uname are the ones a run whose
        # PATH lacks Git's usr/bin would otherwise not find.
        path = os.pathsep.join([self.bin, os.path.dirname(SHELL), os.environ.get('PATH', '')])
        env = dict(os.environ, PATH=path)
        argv = [SHELL, SCRIPT] + (['--python', self.python] if python else []) + list(args)
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout.strip()

    def passed(self, *args):
        code, out = self.run_sh(*args)
        self.assertEqual(code, 0, out)
        # The kit as the shell spells it (Git Bash: /z/...): the folder above the script's.
        fields = re.findall(r'\[([^\]]*)\]', out)
        tool, flag, kit = fields[:3]
        self.assertEqual((tool, flag), (kit + '/tools/machine_setup.py', '--kit'), out)
        self.assertTrue(kit.endswith('/' + os.path.basename(ROOT)), out)
        return ''.join('[%s]' % f for f in fields[3:])

    def test_default_mode_passes_its_options_through(self):
        self.assertEqual(self.passed('--agent-temp', '/srv/agent temp', '--config-dir', '/a',
                                     '--config-dir', '/b', '--shortcut', '--apply'),
                         '[--agent-temp][/srv/agent temp][--config-dir][/a][--config-dir][/b][--shortcut]'
                         '[--apply]')

    def test_any_value_is_taken_as_given(self):
        # No marker word: a value that looks like one still passes through.
        self.assertEqual(self.passed('--agent-temp', '--end-of-arguments--', '--home', ''),
                         '[--agent-temp][--end-of-arguments--][--home][]')

    def test_root_modes(self):
        self.assertEqual(self.passed('--pm-repo', '/srv/pm', '--apply'), '[--pm-repo][/srv/pm][--apply]')
        self.assertEqual(self.passed('--apply', '--delegation'), '[--delegation][--apply]')
        self.assertEqual(self.passed('--delegation'), '[--delegation]')

    def usage(self, *args, python=True):
        code, out = self.run_sh(*args, python=python)
        self.assertEqual(code, 2, out)
        self.assertNotIn('[--kit]', out)
        return out

    def test_usage_errors_exit_2_and_run_nothing(self):
        self.assertIn('--delegation is a mode of its own: it takes only --python and --apply (not --shortcut)',
                      self.usage('--delegation', '--shortcut'))
        self.assertIn('--pm-repo is a mode of its own', self.usage('--agent-temp', '/t', '--pm-repo', '/p'))
        self.assertIn('give --pm-repo or --delegation, not both', self.usage('--pm-repo', '/p', '--delegation'))
        self.assertIn('not an empty value', self.usage('--pm-repo', ' '))
        self.assertIn('--agent-temp needs a value', self.usage('--agent-temp'))
        self.assertIn('unknown option --old-kit', self.usage('--old-kit', '/k'))
        self.assertIn('--python /no/such is not a file', self.usage('--python', '/no/such', python=False))

    def test_refused_off_linux(self):
        self.system('Darwin')
        out = self.usage('--delegation')
        self.assertIn('REFUSED - this is the Linux installer (this is Darwin)', out)


if __name__ == '__main__':
    unittest.main()
