"""kitlib.pmstart.configcheck: the machine config's session-start lines: the leftover home file,
a missing or unreadable pointer, and the integrity line (a file never committed, dirty, or last
committed by someone other than the owner); no line at all when all is well; and its place right
after the standing rules in the PM checklist. Every case runs on a temp home; this module puts the
real check back over the package's stub (tests/pmstart/__init__.py stub_config_lines)."""
from tests.pmstart import HANDOFF_FAKE
import datetime
import os
import unittest
from unittest import mock

from kitlib import pmstart
from kitlib.config import owner
from kitlib.config import user as kuser
from kitlib.git.cmd import Result
from kitlib.pmstart import configcheck
from tests import gitfixture
from tests import home as khome
from tests import pmstart as tpm

OWNER = 'owner@example.com'
OTHER = 'agent@example.com'
NOW = datetime.datetime(2026, 1, 5, 9, 0)
REL = '.pm-kit/config.json'


def setUpModule():
    """The real check; and the checklist reads none of this machine's pm_data setting, foreground
    lock, pm_watch heartbeat, launcher.repos or hub key (as tests/pmstart/test_pmstart.py)."""
    from kitlib.pmstart import fglock
    for obj, name, value in ((configcheck, 'lines', tpm.REAL_CONFIG_LINES),
                             (pmstart, 'pm_data_settings', lambda: None),
                             (fglock, 'read_live', lambda user32=None: 0),
                             (pmstart, 'read_heartbeat', lambda: None)):
        p = mock.patch.object(obj, name, value)
        p.start()
        unittest.addModuleCleanup(p.stop)
    tpm.keep_off_launcher_repos()
    tpm.keep_off_hub_site()


def git(status='', author=OWNER, tracked=REL, rc=0):
    """A fake kitlib.git.cmd.call: `tracked` for ls-files, `status` for status --porcelain,
    `author` for log -1."""
    calls = []

    def run(args, cwd):
        calls.append((list(args), cwd))
        if rc:
            return Result(rc, b'', 'fatal: not a git repository')
        out = {'ls-files': tracked, 'status': status}.get(args[0], author)
        return Result(0, out.encode('utf-8'), '')
    run.calls = calls
    return run


def commit(pm, email):
    """Commit the PM repository's config.json as `email` (the fixture's git)."""
    for args in (['add', REL], ['-c', 'user.name=someone', '-c', 'user.email=' + email,
                                'commit', '-q', '-m', 'machine config']):
        r = gitfixture.own_git(args, pm)
        if r.returncode:
            raise AssertionError('git %s: %s' % (args, r.stderr))


class ConfigCheckCase(unittest.TestCase):
    def setUp(self):
        self.home = khome.temp_home(self)
        self.folder = khome.machine_config(self.home, {'owner_email': OWNER}, case=self)
        self.pm = os.path.dirname(self.folder)
        self.rules = owner.Rules(owner_email=OWNER)

    def leave(self, name=kuser.DIR_NAME):
        """A machine config file left in the home folder's `name` folder; its path."""
        d = os.path.join(self.home, name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, kuser.FILE_NAME)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{}')
        return path

    def lines(self, run=None, rules=None):
        return configcheck.lines(kuser.locate(self.home), run or git(), rules or self.rules)


class LinesTest(ConfigCheckCase):
    def test_a_clean_setup_prints_nothing(self):
        run = git()
        self.assertEqual(configcheck.lines(kuser.locate(self.home), run, self.rules), [])
        self.assertEqual([c[0] for c in run.calls],
                         [['ls-files', '--', REL], ['status', '--porcelain', '--', REL],
                          ['log', '-1', '--format=%ae', '--', REL]])
        self.assertTrue(all(os.path.normcase(c[1]) == os.path.normcase(self.pm) for c in run.calls))

    def test_a_config_file_left_in_the_home_folder_is_named_with_its_remove_command(self):
        left = self.leave()
        (line,) = self.lines()
        self.assertIn('a copy is still in the home folder (%s)' % left, line)
        self.assertIn("Remove-Item -LiteralPath '%s'" % left, line)

    def test_a_config_file_left_in_the_retired_folder_is_named_too(self):
        from kitlib.config import migration
        left = self.leave(migration.OLD_DIR_NAME)
        (line,) = self.lines()
        self.assertIn("Remove-Item -LiteralPath '%s'" % left, line)

    def test_the_home_folder_without_a_config_file_is_no_leftover(self):
        os.makedirs(os.path.join(self.home, kuser.DIR_NAME, 'auto-tasks'))
        self.assertEqual(self.lines(), [])

    def test_a_leftover_and_a_dirty_file_are_two_lines(self):
        self.leave()
        out = self.lines(git(status=' M ' + REL))
        self.assertEqual(len(out), 2, out)
        self.assertIn('a copy is still in the home folder', out[0])
        self.assertIn('has uncommitted changes', out[1])

    def test_a_dirty_file_is_a_loud_line(self):
        (line,) = self.lines(git(status=' M ' + REL))
        self.assertEqual(line, configcheck.DIRTY % (kuser.locate(self.home).display(), self.pm, REL))

    def test_an_untracked_file_was_never_committed(self):
        run = git(tracked='')
        (line,) = self.lines(run)
        self.assertIn('was never committed', line)
        self.assertEqual(len(run.calls), 1)

    def test_a_tracked_file_with_no_commit_was_never_committed(self):
        (line,) = self.lines(git(author=''))
        self.assertIn('was never committed', line)

    def test_another_author_is_a_loud_line_that_never_names_an_address(self):
        (line,) = self.lines(git(author=OTHER))
        self.assertIn('someone other than the owner', line)
        for out in (self.lines(git(author=OTHER)), self.lines(git(author=OTHER), owner.Rules()),
                    self.lines(git(author=OTHER, status=' M ' + REL))):
            self.assertTrue(out)
            for l in out:
                self.assertNotIn(OTHER, l)
                self.assertNotIn(OWNER, l)
                self.assertNotIn('@', l)

    def test_an_unknown_owner_is_said_not_guessed(self):
        (line,) = self.lines(git(), owner.Rules())
        self.assertIn("cannot check", line)
        self.assertIn("identity is not configured", line)

    def test_no_config_file_in_the_pm_repository_prints_nothing(self):
        loc = kuser.locate(self.home)
        os.remove(loc.path)
        loc = kuser.locate(self.home)
        self.assertEqual(loc.state, kuser.OK)
        self.assertEqual(configcheck.lines(loc, git(tracked='')), [])

    def test_a_tracked_file_that_was_deleted_is_a_change(self):
        loc = kuser.locate(self.home)
        os.remove(loc.path)
        (line,) = configcheck.lines(kuser.locate(self.home), git(status=' D ' + REL), self.rules)
        self.assertIn('has uncommitted changes', line)

    def test_an_apostrophe_in_the_leftover_path_is_doubled_in_the_remove_command(self):
        left = os.path.join(self.home, "it's", kuser.FILE_NAME)
        loc = kuser.locate(self.home)._replace(leftover=left)
        (line,) = configcheck.lines(loc, git(), self.rules)
        self.assertIn("Remove-Item -LiteralPath '%s')" % left.replace("'", "''"), line)
        self.assertIn('home folder (%s)' % left, line)

    def test_git_failing_keeps_the_leftover_line(self):
        self.leave()
        out = self.lines(git(rc=128))
        self.assertIn('a copy is still in the home folder', out[0])
        self.assertTrue(out[1].startswith('could not check machine_config: RuntimeError'), out)


class RealGitTest(ConfigCheckCase):
    """The check's own defaults: the PM repository's git (the fixture's) and its owner rules."""

    def test_never_committed_then_clean_then_dirty_then_another_author(self):
        loc = kuser.locate(self.home)
        (line,) = configcheck.lines(loc)
        self.assertEqual(line, configcheck.NEVER % loc.display())
        commit(self.pm, OWNER)
        self.assertEqual(configcheck.lines(loc), [])
        with open(loc.path, 'a', encoding='utf-8') as fh:
            fh.write(' ')
        (line,) = configcheck.lines(loc)
        self.assertIn('has uncommitted changes', line)
        commit(self.pm, OTHER)
        (line,) = configcheck.lines(loc)
        self.assertIn('someone other than the owner', line)
        self.assertNotIn('@', line)


class PointerTest(unittest.TestCase):
    def setUp(self):
        self.home = khome.temp_home(self)

    def test_no_pointer_and_no_home_file_names_machine_setup(self):
        loc = kuser.locate(self.home)
        self.assertEqual(loc.state, kuser.NONE)
        self.assertEqual(configcheck.lines(loc, git()), [configcheck.NO_POINTER])
        self.assertIn('--pm-repo', configcheck.NO_POINTER)

    def test_an_unmoved_config_says_why_and_runs_no_git(self):
        d = os.path.join(self.home, kuser.DIR_NAME)
        os.makedirs(d)
        with open(os.path.join(d, kuser.FILE_NAME), 'w', encoding='utf-8') as fh:
            fh.write('{}')
        loc = kuser.locate(self.home)
        self.assertEqual(loc.state, kuser.UNMOVED)
        run = git()
        (line,) = configcheck.lines(loc, run)
        self.assertEqual(line, configcheck.UNREADABLE % loc.error)
        self.assertEqual(run.calls, [])

    def test_a_bad_pointer_says_why(self):
        with mock.patch.dict(os.environ, {khome.PM_REPO_FENCE_ENV: os.path.join(self.home, 'nope')}):
            kuser.forget()
            self.addCleanup(kuser.forget)
            loc = kuser.locate(self.home)
            self.assertEqual(loc.state, kuser.BAD)
            (line,) = configcheck.lines(loc, git())
        self.assertTrue(line.startswith('machine config: cannot be read: '), line)
        self.assertIn('pointer', line)


class ChecklistTest(unittest.TestCase):
    FAKES = dict(handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
                 liveness={"read": lambda p: "{}"})

    def setUp(self):
        self.home = khome.temp_home(self)

    def test_the_real_check_on_a_temp_home_follows_the_standing_lines(self):
        txt = pmstart.checklist({"standing": ["x"]}, NOW, procs=[], **self.FAKES)
        self.assertEqual(txt.splitlines()[1:3], ['- standing rule: x', '- ' + configcheck.NO_POINTER])

    def test_a_clean_setup_adds_no_line(self):
        folder = khome.machine_config(self.home, {'owner_email': OWNER}, case=self)
        commit(os.path.dirname(folder), OWNER)
        txt = pmstart.checklist({"standing": ["x"]}, NOW, procs=[], **self.FAKES)
        self.assertNotIn('machine config', txt)
        self.assertNotIn('machine_config', txt)
        self.assertEqual(txt.splitlines()[1], '- standing rule: x')

    def test_the_fake_location_is_passed_through(self):
        d = os.path.join(self.home, kuser.DIR_NAME)
        os.makedirs(d)
        with open(os.path.join(d, kuser.FILE_NAME), 'w', encoding='utf-8') as fh:
            fh.write('{}')
        loc = kuser.locate(self.home)
        txt = pmstart.checklist({}, NOW, procs=[], machine_config={"loc": loc}, **self.FAKES)
        self.assertEqual(txt.splitlines()[1], '- ' + configcheck.UNREADABLE % loc.error)

    def test_a_failing_check_is_isolated(self):
        def boom(**kw):
            raise OSError('disk gone')
        with mock.patch.object(configcheck, 'lines', boom):
            txt = pmstart.checklist({}, NOW, procs=[], **self.FAKES)
        self.assertEqual(txt.splitlines()[1], '- could not check machine_config: OSError: disk gone')
        self.assertGreater(len(txt.splitlines()), 3)


if __name__ == '__main__':
    unittest.main()
