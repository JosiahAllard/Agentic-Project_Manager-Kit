"""The --pm-repo plan's wording (kitlib.machinesetup.pointer.survey): the pointer file on Linux,
the registry value on Windows. The platform is patched, so each case runs on any host."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.config import pointer as cpointer
from kitlib.machinesetup import pointer as mpointer

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LINUX = 'pointer file /etc/pm-kit/pointer, else <profile>/.config/pm-kit/pointer'
WINDOWS = 'HKLM\\SOFTWARE\\pm-kit pm_repo'


class Host(object):
    def __init__(self, admin):
        self.admin = admin

    def is_admin(self):
        return self.admin


class Registry(object):
    def __init__(self, value=None, why=None):
        self.value, self.why = value, why

    def read(self):
        return self.value, self.why


class SurveyWording(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(tempfile.gettempdir(), 'pm-data')

    def survey(self, name, admin=True, apply=False, value=None, why=None):
        reg = Registry(value, why)
        with mock.patch('kitlib.platform.NAME', name), \
                mock.patch.object(cpointer, 'check', return_value=None):
            return mpointer.survey(mpointer.PointerContext(
                self.path, KIT, host=Host(admin), registry=reg), apply=apply)

    def text(self, plan):
        return [c.text for c in plan.changes]

    def flat(self, plan):
        notes = [n for v in plan.notes.values() for n in v]
        return ' | '.join(plan.refusals + notes + self.text(plan))

    def test_set_text_per_platform(self):
        value = mpointer.target(self.path)
        self.assertEqual(self.text(self.survey('linux')), ['%s: set it to %s' % (LINUX, value)])
        self.assertEqual(self.text(self.survey('windows')), ['%s: set it to %s' % (WINDOWS, value)])

    def test_replace_text_per_platform(self):
        old = os.path.join(tempfile.gettempdir(), 'other')
        for name, where in (('linux', LINUX), ('windows', WINDOWS)):
            got = self.text(self.survey(name, value=old))
            self.assertTrue(got[0].startswith('%s: replace %s with ' % (where, old)), got)

    def test_already_names_per_platform(self):
        value = mpointer.target(self.path)
        for name, where in (('linux', LINUX), ('windows', WINDOWS)):
            plan = self.survey(name, value=value)
            self.assertIn('%s already names %s' % (where, value), self.flat(plan))

    def test_unreadable_value_hint_per_platform(self):
        linux = self.flat(self.survey('linux', why='boom'))
        windows = self.flat(self.survey('windows', why='boom'))
        self.assertNotIn('reg query', linux)
        self.assertIn('reg query "HKLM\\SOFTWARE\\pm-kit" /v pm_repo', windows)

    def test_refusal_when_not_elevated_per_platform(self):
        linux = self.flat(self.survey('linux', admin=False, apply=True))
        windows = self.flat(self.survey('windows', admin=False, apply=True))
        self.assertIn('not elevated: %s is root-owned;' % LINUX, linux)
        self.assertNotIn('HKLM', linux)
        self.assertIn('not elevated: HKLM\\SOFTWARE\\pm-kit is under HKLM; run python ', windows)

    def test_elevation_wording_per_platform(self):
        def all_text(name, apply):
            plan = self.survey(name, admin=False, apply=apply)
            return self.flat(plan) + ' | ' + ' | '.join(d for c in plan.changes for d in c.detail)
        for apply in (True, False):
            linux = all_text('linux', apply)
            self.assertIn('as root or with sudo', linux)
            self.assertNotIn('administrator', linux)
        self.assertIn('run it from an administrator terminal', all_text('windows', False))
        self.assertIn('from an administrator terminal, or by hand there:', all_text('windows', True))
        self.assertIn('by hand, from an administrator terminal: ', all_text('windows', False))
        self.assertNotIn('sudo', all_text('windows', True) + all_text('windows', False))


if __name__ == '__main__':
    unittest.main()
