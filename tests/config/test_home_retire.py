"""The home folder's machine config is retired (docs/kit-json.md "Moving
the machine config folder"): with a good PM-repo pointer, a config.json still in the home folder
beside the PM repository's is LEFTOVER (kitlib.config.user.locate), an error naming its remove
command for every reader, never a warning beside a config that is read. The preflight `config`
line (kitlib.preflight.configline) says BROKEN, and the session-start lines
(kitlib.pmstart.configcheck) name the remove command and, for the auto-tasks state still in the
home folder (kitlib.autotasks.tick), its move command. Every case runs on a temp home and a fenced
temp PM repository."""
import os
import unittest

from kitlib.config import ConfigError, migration
from kitlib.config import user as kuser
from kitlib.git.cmd import Result
from kitlib.preflight import configline
from kitlib.pmstart import configcheck
from tests import home as khome
from tests import pmstart as tpm


def fake_git(status=''):
    """A kitlib.git.cmd.call fake for the integrity check: tracked, `status` for status
    --porcelain (default clean), committed by OWNER."""
    def run(args, cwd):
        out = {'ls-files': '.pm-kit/config.json', 'status': status}.get(args[0], OWNER)
        return Result(0, out.encode('utf-8'), '')
    return run


clean_git = fake_git()


OWNER = 'owner@example.com'


class Case(unittest.TestCase):
    def setUp(self):
        self.home = khome.temp_home(self)
        self.folder = khome.machine_config(self.home, {'owner_email': OWNER, 'ledger_repos': ['a']},
                                           case=self)
        self.pm = os.path.dirname(self.folder)
        self.pm_file = os.path.join(self.folder, kuser.FILE_NAME)

    def leave(self, name=kuser.DIR_NAME, path=None):
        """A machine config file left in the home folder's `name` folder (or at `path`); its path."""
        path = path or os.path.join(self.home, name, kuser.FILE_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{"ledger_repos": ["home"]}')
        return path


class LocateTest(Case):
    def test_a_home_file_beside_the_pm_repositorys_is_leftover_an_error_naming_its_removal(self):
        for name in (kuser.DIR_NAME, migration.OLD_DIR_NAME):
            with self.subTest(name):
                left = self.leave(name)
                loc = kuser.locate(self.home)
                self.assertEqual((loc.state, loc.path, loc.folder, loc.leftover),
                                 (kuser.LEFTOVER, self.pm_file, self.folder, left))
                self.assertEqual(loc.error, kuser.LEFTOVER_WHY % (self.pm_file, left, left, kuser.MOVE_DOC))
                self.assertIn('moved to the PM repository', loc.error)
                self.assertIn("Remove-Item -LiteralPath '%s'" % left, loc.error)
                os.remove(left)

    def test_every_reader_fails_with_the_leftover_error_and_none_reads_the_pm_file(self):
        left = self.leave()
        why = kuser.locate(self.home).error
        for name, call in (('user_config', lambda: kuser.user_config(self.home)),
                           ('readable_file', lambda: kuser.readable_file(self.home)),
                           ('writable_file', lambda: kuser.writable_file(self.home, 'owner_email')),
                           ('ledger_repos', lambda: kuser.ledger_repos(self.home)),
                           ('owner_email', lambda: kuser.owner_email(self.home))):
            with self.subTest(name):
                with self.assertRaises(ConfigError) as cm:
                    call()
                self.assertEqual(str(cm.exception), why)
                self.assertIn(left, str(cm.exception))

    def test_the_hub_credential_folder_is_refused_too(self):
        from kitlib.hubsite import auth
        self.leave()
        with self.assertRaises(ConfigError):
            auth.folder(self.home)

    def test_an_apostrophe_in_the_leftover_path_is_doubled_in_the_remove_command(self):
        home = os.path.join(self.home, "it's")
        left = self.leave(path=os.path.join(home, kuser.DIR_NAME, kuser.FILE_NAME))
        loc = kuser.locate(home)
        self.assertEqual(loc.state, kuser.LEFTOVER)
        self.assertIn("Remove-Item -LiteralPath '%s'" % left.replace("'", "''"), loc.error)

    def test_a_home_folder_with_other_files_but_no_config_json_is_ok(self):
        for sub in ('auto-tasks', 'hub'):
            os.makedirs(os.path.join(self.home, kuser.DIR_NAME, sub))
        with open(os.path.join(self.home, kuser.DIR_NAME, 'config.json.bak'), 'w') as fh:
            fh.write('{}')
        loc = kuser.locate(self.home)
        self.assertEqual((loc.state, loc.error, loc.leftover), (kuser.OK, None, None))
        self.assertEqual(kuser.ledger_repos(self.home), ['a'])

    def test_config_dir_names_the_pm_folder_never_the_home_one(self):
        self.leave()
        self.assertEqual(kuser.config_dir(self.home), self.folder)


class ConfigLineTest(Case):
    def test_a_leftover_is_broken_with_the_error(self):
        self.leave()
        out = configline.line(self.home)
        self.assertEqual(out, configline.PREFIX + 'BROKEN ' + kuser.locate(self.home).error)
        self.assertNotIn('WARN', out)

    def test_without_a_leftover_the_pm_folder_is_named(self):
        self.assertEqual(configline.line(self.home),
                         configline.PREFIX + '%s (config.json, local.json)' % self.folder.replace('\\', '/'))


class SessionStartTest(Case):
    def setUp(self):
        super().setUp()
        from kitlib.config import owner
        self.rules = owner.Rules(owner_email=OWNER)
        self.home_state = os.path.join(self.home, kuser.DIR_NAME, 'auto-tasks')
        self.pm_state = os.path.join(self.folder, 'auto-tasks')

    def lines(self, run=clean_git):
        return tpm.REAL_CONFIG_LINES(kuser.locate(self.home), run, self.rules)

    def test_a_leftover_names_its_remove_command(self):
        left = self.leave()
        out = self.lines()
        self.assertEqual(out, [configcheck.LEFTOVER % (left, self.pm_file, left)])
        self.assertIn('stops until it is removed', out[0])

    def test_with_a_leftover_the_integrity_check_still_runs(self):
        left = self.leave()
        out = self.lines(fake_git(status=' M .pm-kit/config.json'))
        self.assertEqual(out, [configcheck.LEFTOVER % (left, self.pm_file, left),
                               configcheck.DIRTY % (self.pm_file, self.pm, '.pm-kit/config.json')])

    def test_a_state_folder_git_does_not_ignore_is_named_since_the_tick_says_it_to_no_one(self):
        from unittest import mock
        seen = []

        def unignored(pm):
            seen.append(pm)
            return 'not ignored, add the line'
        with mock.patch.object(configcheck, '_unignored', unignored):
            out = self.lines()
        self.assertEqual(out, [configcheck.PREFIX + 'not ignored, add the line'])
        self.assertEqual(seen, [self.pm])

    def test_the_ignore_line_comes_before_the_home_states_move(self):
        from unittest import mock
        os.makedirs(self.home_state)
        with mock.patch.object(configcheck, '_unignored', lambda pm: 'not ignored'):
            out = self.lines()
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], configcheck.PREFIX + 'not ignored')
        self.assertIn('Copy-Item', out[1])

    def test_the_ignore_check_is_the_ticks(self):
        from unittest import mock
        from kitlib.autotasks import tick
        with mock.patch.object(tick, 'unignored', lambda pm: 'tick says %s' % pm):
            self.assertEqual(tpm.REAL_UNIGNORED(self.pm), 'tick says %s' % self.pm)

    def test_the_auto_tasks_state_in_the_home_folder_names_its_move(self):
        os.makedirs(self.home_state)
        (line,) = self.lines()
        self.assertTrue(line.startswith(configcheck.PREFIX), line)
        self.assertIn("Copy-Item -LiteralPath '%s' -Destination '%s' -Recurse" % (self.home_state, self.pm_state),
                      line)
        self.assertIn("Remove-Item -LiteralPath '%s' -Recurse" % self.home_state, line)

    def test_the_auto_tasks_state_in_both_places_names_the_home_ones_removal(self):
        os.makedirs(self.home_state)
        os.makedirs(self.pm_state)
        (line,) = self.lines()
        self.assertNotIn('Copy-Item', line)
        self.assertIn("Remove-Item -LiteralPath '%s' -Recurse" % self.home_state, line)

    def test_a_clean_setup_prints_nothing(self):
        self.assertEqual(self.lines(), [])


if __name__ == '__main__':
    unittest.main()
