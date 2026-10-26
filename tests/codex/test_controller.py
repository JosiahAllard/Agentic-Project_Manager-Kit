import copy
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tomllib
import unittest
from unittest import mock

from hosts.codex.adapter.controller import bind_started, execute, inspect_runtime, plan
from hosts.codex.adapter.runtime import RoleBindings
from kitlib.git import repo
from tests.guard.world import World
from tests import gitfixture
from tests.realgit import real_git
from tests.platforms import windows_only


class Controller(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        old = Path(self.world.wt)
        assigned = Path(self.world.main) / '.codex/worktrees/assigned'
        assigned.parent.mkdir(parents=True)
        old.rename(assigned)
        self.world.wt = str(assigned)
        self.brief = assigned / 'brief.txt'
        self.brief.write_text('Exact assigned brief', encoding='utf-8')
        self.exe = assigned / 'codex.exe'
        self.exe.touch()
        # This injected classifier models an existing linked administration
        # directory; its resolved metadata path is an explicit fixture input.
        Path(self.world.classify(self.world.wt).git_dir).mkdir(parents=True)

    def build(self, role='worker', **kwargs):
        return plan(role, self.world.wt, 'native-test', self.brief, 'pmkit_reviewed',
                    classify=self.world.classify, codex_exe=self.exe, **kwargs)

    def native_root(self, planned):
        reading = planned.permission.extends == ':read-only'
        policy = {'type': 'readOnly' if reading else 'workspaceWrite', 'networkAccess': False}
        if not reading:
            policy.update(writableRoots=list(planned.permission.write_roots),
                          excludeTmpdirEnvVar=True, excludeSlashTmp=True)
        return {'thread': {'id': 'root', 'sessionId': 'session', 'cwd': planned.cwd},
                'cwd': planned.cwd, 'model': 'gpt-6.1-sol', 'runtimeWorkspaceRoots': [planned.cwd],
                'activePermissionProfile': {'id': planned.permission.profile_id}, 'sandbox': policy}

    def test_plan_uses_named_native_permissions_and_existing_dynamic_kit_tool(self):
        before = set(Path(self.world.wt).iterdir())
        planned = self.build()
        self.assertIn('app-server', planned.argv)
        self.assertNotIn('exec', planned.argv)
        self.assertNotIn('--sandbox', planned.argv)
        self.assertEqual(planned.start_params['permissions'], 'pmkit_reviewed')
        self.assertNotIn('sandbox', planned.start_params)
        self.assertFalse(planned.start_params['ephemeral'])
        self.assertEqual(planned.start_params['dynamicTools'][0]['name'], 'pmkit_run')
        self.assertEqual(planned.permission.extends, ':workspace')
        self.assertIn(str(Path(self.world.main) / '.git'), planned.permission.write_roots)
        self.assertEqual(before, set(Path(self.world.wt).iterdir()))
        self.assertNotIn('Exact assigned brief', ' '.join(planned.argv))

    def test_dedicated_analyst_plan_has_no_writable_roots(self):
        planned = self.build('analyst')
        self.assertEqual(planned.permission.extends, ':read-only')
        self.assertEqual(planned.permission.write_roots, ())
        parsed = tomllib.loads(planned.permission.toml())
        self.assertTrue(all(value == 'read' for value in parsed['permissions']['pmkit_reviewed']['filesystem'].values()))
        with self.assertRaisesRegex(ValueError, 'writable stores'):
            self.build('analyst', store_write_roots=(self.world.data,))

    def test_exact_linked_gitdir_entry_survives_recursive_common_grant(self):
        git_dir = str(Path(self.world.classify(self.world.wt).git_dir).resolve()).replace('\\', '/')
        for role, access in (('worker', 'write'), ('analyst', 'read')):
            planned = self.build(role)
            entries = tomllib.loads(planned.permission.toml())['permissions']['pmkit_reviewed']['filesystem']
            self.assertEqual(entries[git_dir], access)
            self.assertIn(str(Path(git_dir)), planned.permission.read_roots)

    def test_unresolved_relative_or_outside_gitdir_is_refused(self):
        original = self.world.classify(self.world.wt)
        outside = Path(self.world.data)
        for git_dir in (None, 'relative', str(outside), original.common,
                        str(Path(original.common) / 'missing')):
            def classify(path, git_dir=git_dir):
                return self.world.classify(path)._replace(git_dir=git_dir)
            with self.subTest(git_dir=git_dir), self.assertRaises(ValueError):
                plan('worker', self.world.wt, 'native-test', self.brief, 'pmkit_reviewed',
                     classify=classify, codex_exe=self.exe)

    def test_main_lander_keeps_git_admin_read_only(self):
        """A read or lander role gets no write grant on Codex, whatever its role file's sandbox
        (content/roles/lander.md says workspace-write for the default host), as the source had it."""
        planned = plan('lander', self.world.main, 'landing-review', self.brief, 'pmkit_reviewed',
                       classify=self.world.classify, codex_exe=self.exe)
        entries = tomllib.loads(planned.permission.toml())['permissions']['pmkit_reviewed']['filesystem']
        self.assertEqual(entries[str(Path(self.world.main) / '.git').replace('\\', '/')], 'read')
        self.assertEqual(planned.permission.write_roots, ())
        self.assertEqual(planned.permission.extends, ':read-only')
        with self.assertRaises(ValueError):
            execute(planned)

    def test_store_access_roots_are_explicit_absolute_review_inputs(self):
        planned = self.build(store_read_roots=(self.world.data,), store_write_roots=(self.world.data,))
        parsed = tomllib.loads(planned.permission.toml())
        root = str(Path(self.world.data).resolve()).replace('\\', '/')
        self.assertEqual(parsed['permissions']['pmkit_reviewed']['filesystem'][root], 'write')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.build(store_read_roots=('relative-store',))

    def test_permission_proposal_excludes_unreviewed_temp_and_network_grants(self):
        values = tomllib.loads(self.build().permission.toml())['permissions']['pmkit_reviewed']
        self.assertEqual(values['filesystem'][':tmpdir'], 'read')
        self.assertEqual(values['filesystem'][':slash_tmp'], 'read')
        self.assertFalse(values['network']['enabled'])

    def test_owner_instructions_and_runtime_config_override_parent_write_grant(self):
        planned = self.build()
        values = tomllib.loads(planned.permission.toml())['permissions']['pmkit_reviewed']['filesystem']
        for child in ('AGENTS.md', '.codex'):
            self.assertEqual(values[str(Path(planned.cwd) / child).replace('\\', '/')], 'read')
        self.assertEqual(values[planned.cwd.replace('\\', '/')], 'write')

    def test_native_runtime_inspection_never_starts_or_grants_permission(self):
        responses = {'config/read': {'config': {'windows': {'sandbox': 'unelevated'}}},
                     'permissionProfile/list': {'data': [{'id': 'pmkit_reviewed', 'allowed': True}]},
                     'configRequirements/read': {'requirements': None}}
        request = mock.Mock(side_effect=lambda method, params: copy.deepcopy(responses[method]))
        result = inspect_runtime(self.build(), request)
        self.assertTrue(result['permission_allowed'])
        self.assertFalse(result['ready'])
        self.assertEqual([call.args[0] for call in request.call_args_list], list(responses))
        self.assertTrue(any('hook' in blocker for blocker in result['blockers']))

    def test_forbidden_profile_and_disabled_backend_are_activation_blockers(self):
        request = mock.Mock(side_effect=[{'config': {}},
                {'data': [{'id': 'pmkit_reviewed', 'allowed': False}]}, {'requirements': None}])
        result = inspect_runtime(self.build(), request)
        self.assertFalse(result['permission_allowed'])
        self.assertTrue(any('backend' in value for value in result['blockers']))
        self.assertTrue(any('forbidden' in value for value in result['blockers']))

    def test_malformed_config_is_an_explicit_refusal_before_other_native_requests(self):
        for malformed in ({'config': [1]}, {'config': None}, {'config': False}, {}, []):
            request = mock.Mock(return_value=malformed)
            with self.subTest(config=malformed), self.assertRaisesRegex(ValueError, 'config must be an object'):
                inspect_runtime(self.build(), request)
            self.assertEqual(request.call_count, 1)

    def test_profile_pagination_is_bounded_and_repeated_cursors_fail(self):
        request = mock.Mock(side_effect=[{'config': {'windows': {'sandbox': 'unelevated'}}},
                                         {'data': [], 'nextCursor': 'same'}, {'data': [], 'nextCursor': 'same'}])
        with self.assertRaisesRegex(ValueError, 'pagination'):
            inspect_runtime(self.build(), request)
        self.assertEqual(request.call_count, 3)

    def test_profile_on_later_page_is_found_with_exact_project_checkout(self):
        request = mock.Mock(side_effect=[{'config': {'windows': {'sandbox': 'unelevated'}}},
                {'data': [], 'nextCursor': 'next'},
                {'data': [{'id': 'pmkit_reviewed', 'allowed': True}], 'nextCursor': None}, {'requirements': None}])
        self.assertTrue(inspect_runtime(self.build(), request)['permission_allowed'])
        self.assertEqual(request.call_args_list[2].args[1]['cwd'], self.world.wt)
        self.assertEqual(request.call_args_list[2].args[1]['cursor'], 'next')

    def test_dedicated_root_cannot_gain_writer_permissions_from_profile_name(self):
        planned = self.build('analyst')
        value = {'thread': {'id': 'root', 'sessionId': 'session', 'cwd': planned.cwd},
                 'cwd': planned.cwd, 'model': 'actual-model', 'runtimeWorkspaceRoots': [planned.cwd],
                 'activePermissionProfile': {'id': planned.permission.profile_id},
                 'sandbox': {'type': 'workspaceWrite'}}
        bindings = RoleBindings(mock.Mock(), classify=lambda _: SimpleNamespace(kind=repo.LINKED))
        with self.assertRaisesRegex(ValueError, 'writable native'):
            bind_started(planned, value, bindings)
        self.assertEqual(bindings.bindings, {})

    def test_mismatched_native_profile_never_reaches_the_binding_registry(self):
        bindings = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'differs'):
            bind_started(self.build(), {'activePermissionProfile': {'id': 'other'}}, bindings)
        bindings.add_root.assert_not_called()

    def test_same_profile_identifier_cannot_hide_unreviewed_writable_root(self):
        planned = self.build()
        native = self.native_root(planned)
        native['sandbox']['writableRoots'].append(self.world.data)
        bindings = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'writable grants differ'):
            bind_started(planned, native, bindings)
        bindings.add_root.assert_not_called()

    def test_narrower_native_git_grant_requires_controller_review(self):
        planned = self.build()
        native = self.native_root(planned)
        native['sandbox']['writableRoots'] = [str(Path(self.world.main) / '.git' / 'objects')]
        bindings = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'writable grants differ'):
            bind_started(planned, native, bindings)
        bindings.add_root.assert_not_called()

    def test_known_effective_roots_are_normalized_and_implicit_checkout_is_accounted_for(self):
        planned = self.build()
        native = self.native_root(planned)
        common = str(Path(self.world.main) / '.git')
        native['sandbox']['writableRoots'] = [common, common, str(Path(planned.cwd) / 'subdirectory')]
        bindings = RoleBindings(mock.Mock(), classify=lambda _: SimpleNamespace(kind=repo.LINKED))
        self.assertEqual(bind_started(planned, native, bindings).role, 'worker')

    def test_implicit_native_temp_allowances_fail_when_paths_cannot_be_proven(self):
        planned = self.build()
        for key in ('excludeTmpdirEnvVar', 'excludeSlashTmp'):
            for value in (False, None, 'true'):
                native = self.native_root(planned)
                native['sandbox'][key] = value
                bindings = mock.Mock()
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'temporary write grants'):
                    bind_started(planned, native, bindings)
                bindings.add_root.assert_not_called()
        native = self.native_root(planned)
        del native['sandbox']['excludeTmpdirEnvVar']
        with self.assertRaisesRegex(ValueError, 'temporary write grants'):
            bind_started(planned, native, mock.Mock())

    def test_literal_temp_subdirectory_requires_explicit_reviewed_grant(self):
        temp_root = str(Path(self.world.data) / 'reviewed-temp')
        planned = self.build()
        native = self.native_root(planned)
        native['sandbox']['writableRoots'].append(temp_root)
        with self.assertRaisesRegex(ValueError, 'writable grants differ'):
            bind_started(planned, native, mock.Mock())
        reviewed = self.build(store_write_roots=(temp_root,))
        bindings = RoleBindings(mock.Mock(), classify=lambda _: SimpleNamespace(kind=repo.LINKED))
        self.assertEqual(bind_started(reviewed, self.native_root(reviewed), bindings).role, 'worker')

    def test_added_network_grant_and_unknown_policy_fields_fail_before_binding(self):
        planned = self.build()
        for key, value in (('networkAccess', True), ('futureUnresolvedGrant', {'path': self.world.data})):
            native = self.native_root(planned)
            native['sandbox'][key] = value
            bindings = mock.Mock()
            with self.subTest(field=key), self.assertRaises(ValueError):
                bind_started(planned, native, bindings)
            bindings.add_root.assert_not_called()

    def test_execution_refuses_before_any_runtime_connection(self):
        with self.assertRaisesRegex(ValueError, 'execution is disabled'):
            execute(self.build())

    def test_controller_cli_validates_required_arguments_without_starting_a_runtime(self):
        script = Path(__file__).resolve().parents[2] / 'hosts/codex/shell/scripts/controller.py'
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--permission-profile', result.stderr)

    @windows_only('Windows path comparison')
    def test_matching_native_checkout_case_preserves_the_controller_assignment(self):
        planned = self.build('analyst', model='gpt-6.1-sol')
        value = {'thread': {'id': 'root', 'sessionId': 'session', 'cwd': planned.cwd.upper()},
                 'cwd': planned.cwd.upper(), 'model': 'gpt-6.1-sol', 'runtimeWorkspaceRoots': [planned.cwd],
                 'activePermissionProfile': {'id': planned.permission.profile_id},
                 'sandbox': {'type': 'readOnly'}}
        bindings = RoleBindings(mock.Mock(), classify=lambda _: SimpleNamespace(kind=repo.LINKED))
        self.assertEqual(bind_started(planned, value, bindings).role, 'analyst')


@real_git('native permission regression uses actual Git linked-worktree administration paths')
class ActualGitPermissions(unittest.TestCase):
    def test_actual_assigned_gitdir_is_explicit_for_worker_and_reader(self):
        fixture = gitfixture.TempRepo()
        self.addCleanup(fixture.cleanup)
        assigned = Path(fixture.root) / '.codex/worktrees/assigned'
        fixture.git('worktree', 'add', '-q', str(assigned), '-b', 'codex/different-task-name')
        brief = assigned / 'brief.txt'
        brief.write_text('Native permission fixture', encoding='utf-8', newline='\n')
        executable = assigned / 'codex.exe'
        executable.touch()
        actual = repo.classify(str(assigned))
        self.assertEqual(actual.kind, repo.LINKED)
        git_dir = str(Path(actual.git_dir).resolve()).replace('\\', '/')
        common = str(Path(actual.common).resolve()).replace('\\', '/')
        for role, access in (('worker', 'write'), ('analyst', 'read')):
            planned = plan(role, assigned, 'actual-paths', brief, 'pmkit_reviewed', codex_exe=executable)
            entries = tomllib.loads(planned.permission.toml())['permissions']['pmkit_reviewed']['filesystem']
            self.assertEqual(entries[git_dir], access)
            self.assertEqual(entries[common], access)
            for child in ('AGENTS.md', '.codex'):
                self.assertEqual(entries[str(assigned / child).replace('\\', '/')], 'read')
            if role == 'analyst':
                self.assertEqual(planned.permission.write_roots, ())
