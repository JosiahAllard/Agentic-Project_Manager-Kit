"""The Linux box checks (kitlib.machinesetup.linuxbox): the front door tools/kit.py asks, and the
default mode's `box` section (WSL and root refused, one kit user per box, the container warning, git
and tmux, the containment tier with the delegation step offered below S1). The box is a fake and the
platform is patched, so each case runs on any host."""
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib import machinesetup
from kitlib.host import registry
from kitlib.machinesetup import linuxbox
from kitlib.machinesetup.common import Plan

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NATIVE = '6.8.0-45-generic\n'
WSL = '5.15.153.1-microsoft-standard-WSL2\n'
OPENRC = frozenset({'/run/openrc', '/etc/local.d'})
CRON = frozenset({'/etc/cron.d'})


class Box(object):
    """linuxbox.Box's methods, answered from fields."""

    def __init__(self, **kw):
        self.release, self.me, self.marker_text, self.marker_why = NATIVE, 1000, None, None
        self.where, self.has_systemd, self.sudo = None, True, None
        self.tools = {'git': '/usr/bin/git', 'tmux': '/usr/bin/tmux'}
        self.tier_name, self.all_users, self.tier_error = 'S2', [], None
        self.init_name, self.paths, self.cg2, self.accounts = 'init', set(), True, {}
        self.files, self.exes, self.tmux_said = {}, {}, 'tmux 3.4'
        for k, v in kw.items():
            setattr(self, k, v)
        self.probed = 0

    def osrelease(self):
        return self.release

    def container(self):
        return self.where

    def uid(self):
        return self.me

    def name(self, uid):
        return 'kit' if uid == 1000 else 'u%d' % uid

    def users(self):
        return self.all_users

    def marker(self):
        return self.marker_text, self.marker_why

    def which(self, name):
        return self.tools.get(name)

    def systemd(self):
        return self.has_systemd

    def tier(self):
        self.probed += 1
        if self.tier_error:
            raise self.tier_error
        return SimpleNamespace(name=self.tier_name, text='containment: %s (fake)' % self.tier_name)

    def sudo_user(self):
        return self.sudo

    def init(self):
        return self.init_name

    def exists(self, path):
        return path in self.paths

    def cgroup2(self):
        return self.cg2

    def account(self, uid):
        return self.accounts.get(uid, (uid, '/srv/%s' % self.name(uid)))

    def read(self, path):
        return self.files.get(path)

    def exe(self, pid):
        return self.exes.get(pid)

    def tmux_version(self, path):
        return self.tmux_said


def survey(box):
    plan = Plan((linuxbox.SECTION,))
    linuxbox.survey(plan, SimpleNamespace(kit=KIT, box=box))
    return plan


def notes(plan):
    return plan.notes[linuxbox.SECTION]


class FrontDoor(unittest.TestCase):

    def test_windows_and_native_linux_run(self):
        self.assertIsNone(linuxbox.front_door('win32'))
        self.assertIsNone(linuxbox.front_door('linux', NATIVE))

    def test_darwin_and_other_systems_are_refused(self):
        for name in ('darwin', 'freebsd13', 'cygwin'):
            with self.subTest(name):
                self.assertEqual(linuxbox.front_door(name),
                                 'the kit runs on Windows and Linux only (this is %s)' % name)

    def test_wsl_is_refused_by_its_kernel_release(self):
        why = linuxbox.front_door('linux', WSL)
        self.assertIn('does not run in WSL', why)
        self.assertIn(WSL.strip(), why)
        self.assertIn('the kit does not run in WSL', linuxbox.front_door('linux', '4.4.0-19041-Microsoft'))

    def test_linux_reads_the_kernel_release_when_none_is_given(self):
        with mock.patch.object(linuxbox.Box, 'osrelease', return_value=WSL):
            self.assertIn('WSL', linuxbox.front_door('linux'))


class Marker(unittest.TestCase):

    def test_round_trip(self):
        self.assertEqual(linuxbox.read_marker(linuxbox.marker_text(1001, 'ann')), (1001, 'ann'))

    def test_bad_text_is_refused(self):
        for text in ('', 'ann 1001', '1001', '1001 ann extra', None):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    linuxbox.read_marker(text)


class Survey(unittest.TestCase):

    def test_a_native_box_below_s1_offers_the_delegation_step(self):
        box = Box()
        plan = survey(box)
        self.assertEqual(plan.refusals, [])
        self.assertEqual(plan.changes, [])
        self.assertEqual(notes(plan)[0], 'containment: S2 (fake)')
        self.assertIn('sudo python3 %s --delegation --apply'
                      % os.path.join(KIT, 'tools', 'machine_setup.py'), notes(plan)[1])
        self.assertIn('may be declined', notes(plan)[1])
        self.assertEqual(box.probed, 1)

    def test_s1_offers_nothing_more(self):
        self.assertEqual(notes(survey(Box(tier_name='S1'))), ['containment: S1 (fake)'])

    def test_wsl_is_refused_before_anything_else_is_read(self):
        box = Box(release=WSL, me=0)
        plan = survey(box)
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn('this is WSL', plan.refusals[0])
        self.assertEqual(box.probed, 0)

    def test_root_is_refused(self):
        plan = survey(Box(me=0))
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn('runs as the kit user, not as root', plan.refusals[0])

    def test_a_marker_naming_another_user_is_refused(self):
        box = Box(me=1001, marker_text='1000 kit\n')
        plan = survey(box)
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn("this box's kit user is kit (uid 1000", plan.refusals[0])
        self.assertIn('refuses a second', plan.refusals[0])
        self.assertEqual(box.probed, 0)

    def test_the_marker_naming_this_user_passes(self):
        self.assertEqual(survey(Box(marker_text='1000 kit\n')).refusals, [])

    def test_an_unreadable_or_bad_marker_is_refused(self):
        self.assertIn('cannot read /etc/pm-kit/kit-user (denied)',
                      survey(Box(marker_why='denied')).refusals[0])
        self.assertIn('is not one line', survey(Box(marker_text='kit\n')).refusals[0])

    def test_without_the_marker_another_users_kit_state_is_a_warning(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        other, clean, system = (os.path.join(root, n) for n in ('other', 'clean', 'system'))
        lock = registry.state_path(other, 'machine-lock')
        os.makedirs(lock)
        os.makedirs(os.path.join(system, '.config', 'pm-kit'))
        open(os.path.join(system, '.config', 'pm-kit', 'pointer'), 'w').close()
        os.makedirs(clean)
        box = Box(all_users=[('kit', 1000, os.path.join(root, 'me')), ('other', 1001, other),
                             ('clean', 1002, clean), ('daemon', 999, system)])
        plan = survey(box)
        self.assertEqual(plan.refusals, [])
        warned = [n for n in notes(plan) if n.startswith('WARNING: user')]
        self.assertEqual(len(warned), 1, notes(plan))
        self.assertIn('user other (uid 1001) has kit state at %s' % lock, warned[0])
        self.assertIn('one kit user per box', warned[0])

    def test_a_container_is_a_warning_and_offers_no_root_step(self):
        plan = survey(Box(where='/.dockerenv', tier_name='W1'))
        self.assertEqual(plan.refusals, [])
        got = notes(plan)
        self.assertTrue(got[0].startswith('WARNING: this runs in a container (/.dockerenv)'), got)
        self.assertIn('never claims strong containment', got[0])
        self.assertEqual(got[1:], ['containment: W1 (fake)'])

    def test_no_systemd_and_no_boot_hook_route_offers_no_root_step(self):
        got = notes(survey(Box(has_systemd=False, tier_name='W2')))
        self.assertEqual(got[0], 'containment: W2 (fake)')
        self.assertIn('no OpenRC local.d and no /etc/cron.d', got[1])
        self.assertIn('stays at the lower tier', got[1])
        self.assertNotIn('--delegation', ' '.join(got))
        for box, want in ((Box(has_systemd=False, cg2=False), 'no cgroup v2'),
                          (Box(has_systemd=False, init_name='runit', paths=CRON), 'runit')):
            with self.subTest(want):
                got = notes(survey(box))
                self.assertIn(want, got[1])
                self.assertNotIn('--delegation', ' '.join(got))

    def test_no_systemd_offers_the_boot_hook_and_names_the_kit_s_tmux_server(self):
        for paths, route in ((OPENRC, 'OpenRC local.d'), (CRON, 'cron @reboot')):
            with self.subTest(route):
                got = notes(survey(Box(has_systemd=False, tier_name='W1', paths=paths)))
                self.assertEqual(got[0], 'containment: W1 (fake)')
                self.assertIn('sudo python3 %s --delegation --apply'
                              % os.path.join(KIT, 'tools', 'machine_setup.py'), got[1])
                self.assertIn(route, got[1])
                self.assertIn('may be declined', got[1])
                self.assertIn('tmux -L pm-kit', got[1])
                self.assertNotIn('sign out', got[1])

    def test_a_boot_hook_already_set_up_says_where_to_start_work(self):
        got = notes(survey(Box(has_systemd=False, tier_name='W1',
                               paths=OPENRC | {'/etc/pm-kit/cgroup-boot'})))
        self.assertIn('the boot hook is set up', got[1])
        self.assertIn('tmux -L pm-kit', got[1])
        self.assertNotIn('--delegation', ' '.join(got))

    def test_missing_git_and_tmux_are_named(self):
        got = notes(survey(Box(tools={})))
        self.assertTrue(got[0].startswith('no git on PATH: '), got)
        self.assertTrue(got[1].startswith('no tmux on PATH: '), got)

    def test_a_probe_that_fails_is_a_note(self):
        got = notes(survey(Box(tier_error=OSError('no cgroup2'))))
        self.assertEqual(got, ['containment could not be probed (no cgroup2)'])


class DefaultMode(unittest.TestCase):
    """machinesetup.survey: the box section on Linux only, its refusals stopping the run."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def survey(self, name, box, kit=KIT):
        with mock.patch('kitlib.platform.NAME', name):
            return machinesetup.survey(machinesetup.Context(self.home, kit, config_dirs=[self.home], box=box))

    def test_linux_refusal_stops_the_run(self):
        box = Box(release=WSL)
        plan = self.survey('linux', box)
        self.assertEqual(plan.SECTIONS, ('box',) + machinesetup.Plan.FRESH_SECTIONS)
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn('this is WSL', plan.refusals[0])
        self.assertEqual(plan.changes, [])

    def test_windows_has_no_box_section(self):
        # Not a kit checkout: the survey stops at the kit check, before any Windows step.
        plan = self.survey('windows', Box(release=WSL), kit=self.home)
        self.assertEqual(plan.SECTIONS, machinesetup.Plan.FRESH_SECTIONS)
        self.assertNotIn('box', '\n'.join(plan.lines()))


if __name__ == '__main__':
    unittest.main()
