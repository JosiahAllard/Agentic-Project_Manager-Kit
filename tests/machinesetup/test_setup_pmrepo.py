"""installer/machine-setup.ps1 -PmRepo (docs/install.md step 1): a mode of its own that passes
--pm-repo to tools/machine_setup.py, with -Python and -Apply only, and refuses an empty value
instead of falling through to the default mode. The wrapper runs a fake `python` (a .cmd in a temp
folder that writes the arguments it was given to a file), so neither the real tool nor the PM-repo
pointer is touched."""
import os
import shutil
import subprocess
import tempfile
import unittest

from kitlib import machinesetup as M

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PS = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                  'powershell.exe')
PS1 = os.path.join(ROOT, 'installer', 'machine-setup.ps1')


class PmRepoWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kit-pmrepo-ps1-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.args = os.path.join(self.tmp, 'args.txt')
        self.fake = os.path.join(self.tmp, 'fakepython.cmd')
        with open(self.fake, 'w', encoding='ascii') as f:
            f.write('@echo off\r\necho %*> "%~dp0args.txt"\r\nexit /b 0\r\n')
        self.pm = os.path.join(self.tmp, 'pm-repo')

    def run_ps1(self, *params):
        cmd = [PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', PS1,
               '-Python', self.fake] + list(params)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def passed(self):
        """The arguments the fake python got, or None when the wrapper never ran it."""
        if not os.path.exists(self.args):
            return None
        with open(self.args, encoding='ascii', errors='replace') as f:
            return f.read().strip()

    def assert_pm_repo(self, got):
        """--pm-repo with the folder, bare or quoted (a TEMP folder with a space is quoted)."""
        forms = ('--pm-repo %s' % self.pm, '--pm-repo "%s"' % self.pm)
        self.assertTrue(any(f in got for f in forms), got)

    def test_it_passes_pm_repo_and_nothing_else_on_a_dry_run(self):
        code, out = self.run_ps1('-PmRepo', self.pm)
        self.assertEqual(code, M.EXIT_OK, out)
        got = self.passed()
        self.assertIsNotNone(got, out)
        self.assertIn('machine_setup.py', got)
        self.assert_pm_repo(got)
        self.assertNotIn('--apply', got)
        for flag in ('--old-kit', '--owner-email', '--managed-settings', '--home', '--config-dir'):
            self.assertNotIn(flag, got)

    def test_apply_is_passed_through(self):
        code, out = self.run_ps1('-PmRepo', self.pm, '-Apply')
        self.assertEqual(code, M.EXIT_OK, out)
        got = self.passed() or ''
        self.assert_pm_repo(got)
        self.assertIn('--apply', got)

    def test_every_other_mode_or_option_is_usage_and_runs_nothing(self):
        others = (['-OldKit', self.tmp], ['-Fresh'], ['-ManagedSettings'],
                  ['-OwnerEmail', 'a@example.invalid'], ['-PmSettings', self.tmp],
                  ['-ConfigDir', self.tmp], ['-AgentTemp', self.tmp], ['-HomeDir', self.tmp],
                  ['-Desktop', self.tmp], ['-NoSweepTask'], ['-RegisterSweepTask'], ['-Shortcut'],
                  ['-Procwatch'], ['-ProcwatchKill'])
        for extra in others:
            code, out = self.run_ps1('-PmRepo', self.pm, *extra)
            self.assertEqual(code, M.EXIT_USAGE, '%s: %s' % (extra, out))
            self.assertIn('-PmRepo is a mode of its own', out, extra)
            self.assertIsNone(self.passed(), extra)

    def test_an_empty_value_is_refused_not_taken_for_the_default_mode(self):
        for value in ('', ' '):
            code, out = self.run_ps1('-PmRepo', value)
            self.assertEqual(code, M.EXIT_USAGE, '%r: %s' % (value, out))
            self.assertIn('-PmRepo needs the PM repository folder', out, value)
            self.assertIsNone(self.passed(), value)


if __name__ == '__main__':
    unittest.main()
