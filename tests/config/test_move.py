"""The move of the machine config into the PM repository (kitlib.config.move, tools/config_move.py;
docs/kit-json.md "Moving the machine config into the PM repo"). Every case runs under a temp home
and the pointer's test fence (kitlib.config.pointer.FENCE_ENV) naming a temp repository: the real
home folder and the real PM repository are never read or written."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kitlib.config import move, pointer, user
from tests import gitfixture, home as thome
from tests.realgit import real_git
from tools import config_move

SECRET = b'\x00secret-bytes\r\n\xff'
EMAIL = 'someone.private@example.org'
PATTERN = 'Zebulon'


@real_git('git status and check-ignore on the PM repo: the fake lacks check-ignore')
class Move(unittest.TestCase):

    def setUp(self):
        self.home = thome.temp_home(self)
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.pm = self.repo.root
        self.folder = os.path.join(self.pm, user.DIR_NAME)
        patch = mock.patch.dict(os.environ, {pointer.FENCE_ENV: self.pm})
        patch.start()
        self.addCleanup(patch.stop)
        loc = user.home_location(self.home)
        self.new_dir, self.old_dir = os.path.dirname(loc.new), os.path.dirname(loc.old)

    def config(self, body, folder=None):
        folder = folder or self.new_dir
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, user.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(json.dumps(body))

    def put(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)

    def run_move(self, apply_it=False):
        code, lines = move.run(apply_it, home=self.home)
        return code, '\n'.join(lines)

    def target(self, rel):
        return os.path.join(self.folder, *rel.split('/'))

    def read(self, rel):
        with open(self.target(rel), 'rb') as f:
            return f.read()

    def plain(self):
        self.config({'owner_email': EMAIL, 'machine_lock': {'cpu_slots': 4},
                     'ledger_path': os.path.join(self.pm, 'docs', 'ledger.md'),
                     '_note': 'kept local'})

    # --- the key table

    def test_every_config_key_has_exactly_one_class(self):
        classes = move.POLICY + move.LOCAL + move.DROPPED
        self.assertEqual(len(classes), len(set(classes)))
        self.assertEqual(set(classes), set(user.SCHEMA) | set(user.RETIRED))
        self.assertTrue(set(move.RELATIVE_KEYS) <= set(move.POLICY))

    # --- dry run and apply

    def test_a_dry_run_writes_nothing(self):
        self.plain()
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('dry run', out)
        self.assertFalse(os.path.exists(self.folder))

    def test_apply_splits_the_keys_and_copies_byte_for_byte(self):
        self.plain()
        self.put(os.path.join(self.new_dir, 'hub', 'token.json'), SECRET)
        self.put(os.path.join(self.new_dir, 'hub', 'sub', 'key.pem'), SECRET + b'2')
        self.put(os.path.join(self.new_dir, move.DELEGATIONS_FILE), b'{"delegations": []}\n')
        self.put(os.path.join(self.new_dir, move.MANAGED_FILE), b'{"x": 1}')
        self.put(os.path.join(self.new_dir, 'config.json.bak1'), b'old')
        self.put(os.path.join(self.new_dir, 'hub', 'hub_backlog_state_x.json'), b'{}')
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        policy = json.loads(self.read('config.json'))
        local = json.loads(self.read('local.json'))
        self.assertEqual(set(policy), {'machine_lock', 'ledger_path'})
        self.assertEqual(policy['ledger_path'], 'docs/ledger.md')
        self.assertEqual(set(local), {'owner_email', '_note'})
        self.assertEqual(self.read('hub/token.json'), SECRET)
        self.assertEqual(self.read('hub/sub/key.pem'), SECRET + b'2')
        self.assertEqual(self.read(move.DELEGATIONS_FILE), b'{"delegations": []}\n')
        self.assertEqual(self.read(move.MANAGED_FILE), b'{"x": 1}')
        self.assertEqual(self.read('.gitignore').decode(), move.GITIGNORE)
        self.assertFalse(os.path.exists(self.target('config.json.bak1')))
        self.assertFalse(os.path.exists(self.target('hub/hub_backlog_state_x.json')))
        self.assertIn('delete  ~/%s/config.json.bak1' % user.DIR_NAME, out)
        self.assertNotIn('delete  ' + self.home, out)
        self.assertIn('hub_backlog_state_x.json (never copied', out)
        self.assertIn('verify  git ignores', out)
        self.assertIn('commit -m "%s"' % move.COMMIT_MESSAGE, out)
        self.assertIn('Remove-Item -Recurse -Force "%s"' % self.new_dir, out)
        self.assertNotIn(EMAIL, out)
        # the home folder is left as it was
        self.assertTrue(os.path.exists(os.path.join(self.new_dir, 'hub', 'token.json')))
        # git sees only the tracked files
        status = self.repo.git('status', '--porcelain', '--untracked-files=all')
        self.assertIn('.pm-kit/config.json', status)
        self.assertIn('.pm-kit/.gitignore', status)
        self.assertNotIn('local.json', status)
        self.assertNotIn('hub/', status)
        self.assertNotIn(move.MANAGED_FILE, status)

    def test_a_rerun_after_apply_changes_nothing(self):
        self.plain()
        self.put(os.path.join(self.new_dir, 'hub', 'token.json'), SECRET)
        self.assertEqual(self.run_move(apply_it=True)[0], move.OK)
        before = {rel: self.read(rel) for rel in ('config.json', 'local.json', 'hub/token.json')}
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertNotIn('wrote', out)
        self.assertIn('same    config.json', out)
        self.assertEqual(before, {rel: self.read(rel) for rel in before})

    def test_the_old_folder_is_read_when_the_new_has_none(self):
        self.config({'pm_start': {}}, folder=self.old_dir)
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('policy  config.json: pm_start', out)
        self.assertIn('Remove-Item', '\n'.join(move.commands(move.survey(self.home))))

    def test_an_unknown_home_file_is_a_note_and_not_copied(self):
        self.plain()
        self.put(os.path.join(self.new_dir, 'stray.txt'), b'x')
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertIn('NOTE    ~/%s/stray.txt' % user.DIR_NAME, out)
        self.assertFalse(os.path.exists(self.target('stray.txt')))

    def test_a_path_outside_the_pm_repo_stays_absolute(self):
        outside = os.path.join(os.path.dirname(self.pm), 'elsewhere', 'pending.md')
        self.config({'pending_path': outside})
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertEqual(json.loads(self.read('config.json'))['pending_path'], outside)
        self.assertNotIn('relative pending_path', out)

    # --- dropped keys

    def test_conductor_start_is_carried_over_when_coordinator_start_is_absent(self):
        self.config({'conductor_start': {'max_items': 3}})
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertEqual(json.loads(self.read('config.json')), {'coordinator_start': {'max_items': 3}})
        self.assertIn('drop    conductor_start (carried over as coordinator_start)', out)

    def test_conductor_start_is_dropped_when_coordinator_start_is_set(self):
        self.config({'conductor_start': {'max_items': 3}, 'coordinator_start': {'max_items': 5}})
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertEqual(json.loads(self.read('config.json')), {'coordinator_start': {'max_items': 5}})
        self.assertIn('drop    conductor_start (coordinator_start is set, so this key was never read)', out)

    def test_ledger_refs_is_dropped(self):
        self.config({'ledger_refs': ['x']})
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('drop    ledger_refs (retired)', out)

    def test_standing_delegation_is_dropped(self):
        self.config({'standing_delegation': 'R-' + '9168'})
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('drop    standing_delegation (retired)', out)

    def test_daily_red_ruling_is_dropped(self):
        self.config({'daily_red_ruling': 'R-' + '9168'})
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('drop    daily_red_ruling (retired)', out)

    def pm_data(self, **change):
        value = {'path': self.pm, 'branch': 'main', 'remote': 'origin'}
        value.update(change)
        self.repo.git('remote', 'add', 'origin', '../pm-origin.git')
        self.config({'pm_data': value})
        return self.run_move()

    def test_pm_data_matching_the_pointer_is_dropped(self):
        code, out = self.pm_data()
        self.assertEqual(code, move.OK, out)
        self.assertIn('drop    pm_data (matches the pointer)', out)

    def test_pm_data_on_another_branch_refuses(self):
        code, out = self.pm_data(branch='elsewhere')
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('its branch is not the branch the pointer is on', out)
        self.assertNotIn('elsewhere', out)

    def test_pm_data_naming_another_remote_refuses(self):
        code, out = self.pm_data(remote='upstreamx')
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('its remote is not a remote of the PM repo', out)
        self.assertNotIn('upstreamx', out)

    def test_an_invalid_pm_data_value_refuses(self):
        code, out = self.pm_data(path=7)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('pm_data does not match the pointer:', out)
        self.assertNotIn('drop    pm_data', out)

    def test_pm_data_against_a_record_checks_its_branch_and_remote(self):
        from kitlib import pmdata
        pmdata.write(self.repo.common_dir(), 'main', 'origin', 'a' * 40)
        code, out = self.pm_data(remote='upstreamx')
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('its remote is not the recorded remote', out)

    def test_pm_data_naming_another_folder_refuses(self):
        other = os.path.join(os.path.dirname(self.pm), 'other-pm')
        self.config({'pm_data': {'path': other, 'branch': 'main', 'remote': 'origin'}})
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('REFUSE  pm_data does not match the pointer: its path is another folder', out)

    # --- refusals

    def test_no_pointer_refuses(self):
        self.plain()
        with mock.patch.dict(os.environ, {pointer.FENCE_ENV: ''}):
            code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED)
        self.assertIn('REFUSE  no PM repo pointer', out)
        self.assertFalse(os.path.exists(self.folder))

    def test_a_bad_pointer_refuses(self):
        self.plain()
        with mock.patch.dict(os.environ, {pointer.FENCE_ENV: os.path.join(self.pm, 'nope')}):
            code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED)
        self.assertIn('REFUSE  no PM repo pointer', out)
        self.assertFalse(os.path.exists(self.folder))

    def test_a_config_the_schema_refuses_names_keys_not_values(self):
        self.config({'guard_rules': {'x': 'Zebulon-value'}, 'pm_start': {}})
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('REFUSE  the machine config fails its schema in key(s) guard_rules', out)
        self.assertNotIn('Zebulon', out)

    def test_no_machine_config_refuses(self):
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED)
        self.assertIn('nothing to move', out)

    def test_a_target_that_differs_refuses_and_nothing_is_written(self):
        self.plain()
        self.put(os.path.join(self.new_dir, 'hub', 'token.json'), SECRET)
        self.put(self.target('hub/token.json'), b'other')
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('exists and differs', out)
        self.assertFalse(os.path.exists(self.target('config.json')))
        self.assertEqual(self.read('hub/token.json'), b'other')

    def test_a_dirty_pm_kit_folder_refuses(self):
        self.plain()
        self.put(self.target('notes.txt'), b'x')
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('has uncommitted changes (notes.txt)', out)
        self.assertFalse(os.path.exists(self.target('config.json')))

    def test_a_dirty_file_equal_to_the_plan_counts_as_clean(self):
        self.plain()
        self.put(self.target('.gitignore'), move.GITIGNORE.encode())
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertIn('same    .gitignore', out)

    def test_a_dirty_tracked_file_that_differs_refuses(self):
        self.plain()
        self.repo.write('.pm-kit/.gitignore', move.GITIGNORE)
        self.repo.commit('ignore', '.pm-kit/.gitignore')
        self.put(self.target('.gitignore'), b'local.json\n')
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('exists and differs', out)
        self.assertIn('has uncommitted changes (.gitignore)', out)

    def test_a_target_not_ignored_by_git_stops_before_any_secret(self):
        self.plain()
        self.put(os.path.join(self.new_dir, 'hub', 'token.json'), SECRET)
        self.repo.write('.pm-kit/.gitignore', move.GITIGNORE)
        self.repo.commit('ignore', '.pm-kit/.gitignore')
        self.repo.write('.gitignore', '!.pm-kit/hub/\n!.pm-kit/hub/**\n')
        with mock.patch.object(move, 'ignored', return_value=(['hub/token.json'], None)):
            code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('nothing secret was written', out)
        self.assertFalse(os.path.exists(self.target('hub/token.json')))
        self.assertFalse(os.path.exists(self.target('local.json')))

    def test_check_ignore_on_real_git_stops_before_any_secret(self):
        # A tracked hub file equal to the source counts as `same`, so the dirty check passes;
        # git check-ignore does not report a tracked file, so the move refuses before it writes
        # local.json. Stubbing ignored() to answer [] makes this test fail.
        self.plain()
        self.put(os.path.join(self.new_dir, 'hub', 'token.json'), SECRET)
        self.put(self.target('hub/token.json'), SECRET)
        self.repo.git('add', '-f', '.pm-kit/hub/token.json')
        self.repo.git('commit', '-q', '-m', 'tracked secret')
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('git does not ignore hub/token.json', out)
        self.assertIn('nothing secret was written', out)
        self.assertFalse(os.path.exists(self.target('local.json')))
        self.assertFalse(os.path.exists(self.target(move.MANAGED_FILE)))

    def test_a_write_that_fails_part_way_leaves_no_temp_file_and_says_what_was_written(self):
        self.plain()
        self.put(os.path.join(self.new_dir, 'hub', 'token.json'), SECRET)
        real = os.rename

        def rename(src, dst):
            if dst.endswith('token.json'):
                raise PermissionError(13, 'Access is denied', dst)
            return real(src, dst)
        with mock.patch.object(move.os, 'rename', side_effect=rename):
            code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('wrote   config.json', out)
        self.assertIn('wrote   local.json', out)
        self.assertIn('REFUSE  cannot write %s: Access is denied' % self.target('hub/token.json'), out)
        self.assertIn('a rerun resumes', out)
        self.assertNotIn(EMAIL, out)
        self.assertNotIn('Traceback', out)
        left = [n for _r, _d, files in os.walk(self.folder) for n in files
                if n.endswith(move.TMP_SUFFIX)]
        self.assertEqual(left, [])
        # the rerun resumes: what was written shows as same, the rest is written
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.OK, out)
        self.assertIn('same    config.json', out)
        self.assertIn('wrote   hub/token.json', out)
        self.assertEqual(self.read('hub/token.json'), SECRET)

    def test_a_write_error_is_removed_with_its_temp_file(self):
        target = self.target('x.json')

        class Boom(Exception):
            pass
        with mock.patch.object(move.os, 'rename', side_effect=Boom):
            with self.assertRaises(Boom):
                move._write_new(target, b'secret')
        self.assertEqual(os.listdir(self.folder), [])

    def test_git_ignores_the_moves_temp_files(self):
        self.put(self.target('.gitignore'), move.GITIGNORE.encode())
        missed, why = move.ignored(self.pm, ['.local.json.abc' + move.TMP_SUFFIX])
        self.assertEqual((missed, why), ([], None))

    # --- personal details

    def test_an_email_in_a_policy_key_refuses_without_the_value(self):
        self.config({'owner_email': EMAIL, 'pm_start': {'who': EMAIL}})
        code, out = self.run_move(apply_it=True)
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('config.json key pm_start holds an email address', out)
        self.assertNotIn(EMAIL, out)
        self.assertNotIn('someone', out)
        self.assertFalse(os.path.exists(self.folder))

    def test_any_address_pattern_refuses(self):
        self.config({'pm_start': {'x': 'mail other.person@example.com'}})
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('key pm_start holds an email address', out)
        self.assertNotIn('other.person', out)

    def test_a_profile_path_in_a_policy_key_refuses_without_the_value(self):
        value = os.path.join(self.home, 'work', 'ledger.md')
        self.config({'ledger_path': value})
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('config.json key ledger_path holds a profile path', out)
        self.assertNotIn(os.path.join(self.home, 'work'), out)

    def test_the_windows_profile_folder_refuses_too(self):
        profile = os.path.join('Q:' + os.sep, 'Users', 'Someone')
        with mock.patch.object(user, 'real_profile', return_value=profile):
            self.config({'temp_dir': profile.lower().replace(os.sep, '/') + '/temp'})
            code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('key temp_dir holds a profile path', out)
        self.assertNotIn('someone', out.lower())

    def test_an_email_in_the_delegations_file_refuses(self):
        self.config({'pm_start': {}})
        self.put(os.path.join(self.new_dir, move.DELEGATIONS_FILE),
                 json.dumps({'by': EMAIL}).encode())
        code, out = self.run_move()
        self.assertEqual(code, move.REFUSED, out)
        self.assertIn('%s holds an email address' % move.DELEGATIONS_FILE, out)
        self.assertNotIn(EMAIL, out)

    def test_a_scrub_pattern_match_warns_without_value_or_pattern(self):
        self.config({'scrub_patterns': [PATTERN], 'pm_start': {'name': 'zebulon-box'}})
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('warn  config.json key pm_start matches a scrub pattern: check it holds no '
                      'personal detail before you commit', out.splitlines())
        self.assertNotIn(PATTERN.lower(), out.lower())

    def test_a_scrub_pattern_in_the_delegations_file_warns(self):
        self.config({'scrub_patterns': [PATTERN], 'pm_start': {}})
        self.put(os.path.join(self.new_dir, move.DELEGATIONS_FILE),
                 json.dumps({'note': 'zebulon said so'}).encode())
        code, out = self.run_move()
        self.assertEqual(code, move.OK, out)
        self.assertIn('warn  %s matches a scrub pattern: check it holds no personal detail before '
                      'you commit' % move.DELEGATIONS_FILE, out.splitlines())
        self.assertNotIn(PATTERN.lower(), out.lower())

    # --- the tool

    def test_the_tool_exit_codes(self):
        self.plain()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(config_move.main([]), 0)
        self.assertIn('dry run', buf.getvalue())
        self.assertFalse(os.path.exists(self.folder))
        with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {pointer.FENCE_ENV: ''}):
            self.assertEqual(config_move.main(['--apply']), 1)
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                config_move.main(['--bogus'])
        self.assertEqual(e.exception.code, 2)


class Fence(unittest.TestCase):

    def test_without_a_temp_home_the_fence_refuses(self):
        thome.fence_decoy(self)
        with self.assertRaises(user.HomeFenced):
            move.survey()


if __name__ == '__main__':
    unittest.main()
