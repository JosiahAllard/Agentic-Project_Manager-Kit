"""The --delegation mode (kitlib.machinesetup.delegation, machinesetup.run_delegation): the systemd
drop-in, the daemon reload and the kit-user marker, written under a test folder standing for /; and
its refusals (off Linux, WSL, no systemd, root's own login, not root with --apply, a marker naming
another user). The box is a fake and the platform is patched, so each case runs on any host."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup
from kitlib.machinesetup import delegation, linuxbox

from tests.machinesetup.test_linuxbox import WSL, Box

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Host(delegation.Host):
    """The live writes, under the test folder; the reload recorded."""

    def __init__(self, fail_reload=False):
        self.reloads, self.fail_reload = 0, fail_reload

    def reload(self):
        if self.fail_reload:
            raise RuntimeError('systemctl daemon-reload exit 1: boom')
        self.reloads += 1


class Delegation(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.host = Host()

    def ctx(self, **kw):
        kw.setdefault('me', 0)
        kw.setdefault('sudo', (1000, 'kit'))
        return delegation.DelegationContext(KIT, box=Box(**kw), host=self.host, root=self.root)

    def run_mode(self, ctx, apply, name='linux'):
        lines = []
        with mock.patch('kitlib.platform.NAME', name):
            code, _ = machinesetup.run_delegation(ctx, apply=apply, out=lines.append)
        return code, '\n'.join(lines)

    def read(self, path):
        p = os.path.join(self.root, *path.lstrip('/').split('/'))
        if not os.path.exists(p):
            return None
        with open(p, newline='') as f:
            return f.read()

    def test_apply_writes_the_dropin_reloads_and_names_the_kit_user(self):
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertEqual(self.read(delegation.DROPIN), delegation.DROPIN_TEXT)
        self.assertIn('Delegate=cpuset cpu io memory pids\n', self.read(delegation.DROPIN))
        self.assertEqual(self.read(linuxbox.MARKER), '1000 kit\n')
        self.assertEqual(self.host.reloads, 1)
        self.assertIn('sign out and in again', out)

    def test_the_folders_it_makes_are_0755_whatever_the_umask(self):
        real = os.chmod
        with mock.patch.object(delegation.os, 'chmod', side_effect=real) as chmod:
            self.run_mode(self.ctx(), apply=True)
        folders = {c[0][0] for c in chmod.call_args_list if c[0][1] == 0o755}
        here = lambda *p: os.path.join(self.root, *p)                     # noqa: E731
        self.assertEqual(folders, {here('etc'), here('etc', 'pm-kit'), here('etc', 'systemd'),
                                   here('etc', 'systemd', 'system'),
                                   here('etc', 'systemd', 'system', 'user@.service.d')})

    def test_a_second_run_has_nothing_to_do(self):
        self.run_mode(self.ctx(), apply=True)
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('delegation: nothing to do', out)
        self.assertEqual(self.host.reloads, 1)

    def test_a_dry_run_changes_nothing(self):
        code, out = self.run_mode(self.ctx(), apply=False)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('write %s' % delegation.DROPIN, out)
        self.assertIn('write %s: kit (uid 1000)' % linuxbox.MARKER, out)
        self.assertIsNone(self.read(delegation.DROPIN))
        self.assertIsNone(self.read(linuxbox.MARKER))
        self.assertEqual(self.host.reloads, 0)

    def test_a_dry_run_as_the_kit_user_names_the_sudo_command(self):
        code, out = self.run_mode(self.ctx(me=1000, sudo=None), apply=False)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('not root: --apply is refused here; run: sudo python3 ', out)
        self.assertIn('write %s: kit (uid 1000)' % linuxbox.MARKER, out)

    def refused(self, ctx, apply=True, name='linux'):
        code, out = self.run_mode(ctx, apply=apply, name=name)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIsNone(self.read(delegation.DROPIN))
        self.assertIsNone(self.read(linuxbox.MARKER))
        self.assertEqual(self.host.reloads, 0)
        return out

    def test_refusals(self):
        self.assertIn('--delegation is a Linux step (this is windows)',
                      self.refused(self.ctx(), name='windows'))
        self.assertIn('this is WSL', self.refused(self.ctx(release=WSL)))
        self.assertIn('no systemd on this box', self.refused(self.ctx(has_systemd=False)))
        self.assertIn('run it through sudo from the kit user', self.refused(self.ctx(sudo=None)))
        self.assertIn('run it through sudo from the kit user', self.refused(self.ctx(sudo=(0, 'root'))))
        self.assertIn('not root: %s and %s are root-owned' % (delegation.DROPIN, linuxbox.MARKER),
                      self.refused(self.ctx(me=1000, sudo=None)))

    def test_a_marker_naming_another_user_is_refused(self):
        p = os.path.join(self.root, 'etc', 'pm-kit')
        os.makedirs(p)
        with open(os.path.join(p, 'kit-user'), 'w') as f:
            f.write('1001 ann\n')
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIn("this box's kit user is ann (uid 1001", out)
        self.assertIsNone(self.read(delegation.DROPIN))

    def test_a_failed_reload_stops_before_the_marker(self):
        self.host = Host(fail_reload=True)
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIn('STOPPED at: systemctl daemon-reload', out)
        self.assertIn('boom', out)
        self.assertIn('already done by this run (1): write %s' % delegation.DROPIN, out)
        self.assertIsNone(self.read(linuxbox.MARKER))


if __name__ == '__main__':
    unittest.main()
