"""A consultant's notes file in agent temp (kitlib/launcher/start.py consultant_notes_path,
docs/launcher.md "Start"): the default path, the copy of an old docs/ops/consultant-notes.local.md
on first use (the old file is never changed or deleted), and the agent-temp sweep keeping the
folder (optional/agenttemp/agent_temp_sweep.py BUILT_IN_KEEP)."""
import os
import sys
import unittest
from unittest import mock

from kitlib.launcher import start
from kitlib.launcher import settings as S
from tests.launcher.fakes import FakeHost, session
from tests.launcher.test_start import Base

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'optional', 'agenttemp'))
import agent_temp_sweep                                             # noqa: E402

OLD_TEXT = b'# consultant notes\r\ncall 1: use the gate\r\n'


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def read(path):
    with open(path, 'rb') as f:
        return f.read()


class Notes(Base):
    def setUp(self):
        Base.setUp(self)
        self.repo = os.path.join(self.h.root, 'repos', 'MyRepo')
        put(os.path.join(self.repo, 'docs', 'conventions-digest.md'), b'digest')
        self.temp = os.path.join(self.h.root, 'agent-temp')
        self.host = FakeHost(self.h.root, env={'AGENT_TEMP': self.temp}, ignored=True)
        self.notes = os.path.join(self.temp, 'myrepo-consultant', 'notes.md')
        self.old = os.path.join(self.repo, 'docs', 'ops', 'consultant-notes.local.md')

    def cons(self, plan=True, **kw):
        """What it says, then the window script it opened, if any (a live start prints no prompt)."""
        args = dict(repo_path=self.repo, account='1', plan=plan)
        args.update(kw)
        n = len(self.host.opened)
        out = list(start.consultant(self.h.settings, self.host, **args))
        return '\n'.join(out + self.host.opened[n:])

    def test_the_default_notes_file_is_in_agent_temp_and_nothing_is_written(self):
        for plan in (True, False):
            out = self.cons(plan=plan)
            self.assertIn('then your notes file %s (create it' % self.notes, out)
            self.assertNotIn('Warning', out)
            self.assertNotIn('old notes', out)
        self.assertEqual(start.consultant_notes_path(self.host, 'myrepo'), self.notes)
        self.assertFalse(os.path.exists(self.temp))
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'docs', 'ops')))

    def test_temp_then_the_home_folder_when_agent_temp_is_not_set(self):
        tmp = os.path.join(self.h.root, 'tmp')
        self.host = FakeHost(self.h.root, env={'TEMP': tmp}, ignored=True)
        self.assertIn('your notes file %s ' % os.path.join(tmp, 'myrepo-consultant', 'notes.md'), self.cons())
        self.host = FakeHost(self.h.root, ignored=True)
        self.assertIn('your notes file %s ' % os.path.join(self.h.root, 'myrepo-consultant', 'notes.md'),
                      self.cons())

    def test_the_repo_slug_names_the_folder(self):
        self.assertIn('your notes file %s ' % os.path.join(self.temp, 'other-consultant', 'notes.md'),
                      self.cons(repo='other'))

    def test_an_old_notes_file_is_copied_on_first_use_and_left_as_it_is(self):
        put(self.old, OLD_TEXT)
        before = os.stat(self.old).st_mtime_ns
        out = self.cons(plan=False)
        self.assertEqual(read(self.notes), OLD_TEXT)
        self.assertEqual(read(self.old), OLD_TEXT)
        self.assertEqual(os.stat(self.old).st_mtime_ns, before)
        self.assertIn('Copied the old notes file %s to %s; the old file is left as it is and is no longer read.'
                      % (self.old, self.notes), out)
        self.assertLess(out.index('Copied the old notes file'), out.index('Started consultant-myrepo'))
        self.assertEqual(len(self.host.opened), 1)

    def test_plan_says_it_would_copy_and_copies_nothing(self):
        put(self.old, OLD_TEXT)
        out = self.cons()
        self.assertIn('Would copy the old notes file %s to %s; the old file would be left as it is.'
                      % (self.old, self.notes), out)
        self.assertFalse(os.path.exists(self.temp))
        self.assertEqual(read(self.old), OLD_TEXT)

    def test_with_both_files_neither_is_written_and_the_old_one_is_named(self):
        put(self.old, OLD_TEXT)
        put(self.notes, b'new')
        for plan in (True, False):
            out = self.cons(plan=plan)
            self.assertIn('Warning: %s is no longer read; the notes file is %s. The old file is left as it is.'
                          % (self.old, self.notes), out)
            self.assertNotIn('copy', out.lower())
        self.assertEqual(read(self.notes), b'new')
        self.assertEqual(read(self.old), OLD_TEXT)

    def test_a_failed_launch_removes_the_copy_and_never_the_old_file(self):
        put(self.old, OLD_TEXT)
        with mock.patch.object(FakeHost, 'open_window', side_effect=OSError('no window')):
            with self.assertRaises(S.LaunchError) as c:
                self.cons(plan=False)
        self.assertIn('no window', str(c.exception))
        self.assertIn('Launch failed; removed the copy %s (the old file %s is unchanged).' % (self.notes, self.old),
                      c.exception.lines)
        self.assertFalse(os.path.exists(self.notes))
        self.assertEqual(read(self.old), OLD_TEXT)

    def test_a_refused_launch_copies_nothing(self):
        put(self.old, OLD_TEXT)
        self.full('1', 97)
        with self.assertRaisesRegex(S.LaunchError, 'Account 1 is at 97%'):
            self.cons(plan=False)
        self.assertFalse(os.path.exists(self.temp))

    def test_a_given_notes_path_is_used_as_given_and_nothing_is_copied(self):
        put(self.old, OLD_TEXT)
        out = self.cons(plan=False, notes_path='notes/c.local.md')
        self.assertIn('your notes file %s ' % os.path.join(self.repo, 'notes', 'c.local.md'), out)
        self.assertNotIn('old notes', out)
        self.assertFalse(os.path.exists(self.temp))
        out = self.cons(plan=False, notes_path=self.old)            # the old file, named: used where it is
        self.assertIn('your notes file %s ' % self.old, out)
        self.assertNotIn('old notes', out)

    def test_replace_says_the_running_consultant_still_writes_the_old_file(self):
        put(self.old, OLD_TEXT)
        self.host.procs = [session(4242, 'consultant-myrepo', agent='consultant')]
        out = self.cons(plan=False, replace=True)
        self.assertIn('Warning: the running consultant-myrepo still writes %s; what it writes after this copy '
                      'stays there.' % self.old, out)
        self.assertEqual(read(self.notes), OLD_TEXT)

    def test_the_agent_temp_sweep_keeps_the_notes_folder(self):
        self.assertTrue(agent_temp_sweep.is_kept(start.CONSULTANT_FOLDER % 'myrepo', agent_temp_sweep.BUILT_IN_KEEP))
        self.assertTrue(agent_temp_sweep.is_kept(start.COORDINATOR_FOLDER % 'kit', agent_temp_sweep.BUILT_IN_KEEP))
        self.assertEqual(os.path.basename(os.path.dirname(self.notes)), start.CONSULTANT_FOLDER % 'myrepo')


if __name__ == '__main__':
    unittest.main()
