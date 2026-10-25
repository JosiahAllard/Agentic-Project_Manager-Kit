"""Safety and failure classification for an opt-in native diagnostic."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from hosts.codex.adapter import workflow_canary as canary


class Safety(unittest.TestCase):
    def test_environment_drops_credentials_agent_identity_and_inherited_lock(self):
        profile = mock.Mock(kit_root=Path.cwd(), release_identity=None)
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'private', 'CLAUDE_AGENT_MODEL': 'other',
                                         'MACHINE_LOCK_RUN': 'untrusted', 'GIT_AUTHOR_NAME': 'other',
                                         'CODEX_API_KEY': 'private',
                                         'PM_KIT_RELEASE_ID': 'unrelated', 'HTTP_PROXY': 'elsewhere'}, clear=True):
            env = canary._environment(Path('private-home'), Path('private-temp'), profile)
        for key in ('OPENAI_API_KEY', 'CLAUDE_AGENT_MODEL', 'MACHINE_LOCK_RUN', 'GIT_AUTHOR_NAME',
                    'PM_KIT_RELEASE_ID', 'HTTP_PROXY', 'CODEX_API_KEY'):
            self.assertNotIn(key, env)
        self.assertEqual(env['PM_KIT_ROOT'], str(Path.cwd()))
        self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')

    def test_environment_retains_only_the_controller_verified_release_pin(self):
        profile = mock.Mock(kit_root=Path.cwd(), release_identity='trusted-pin')
        with mock.patch.dict(os.environ, {'PM_KIT_RELEASE_ID': 'unrelated'}, clear=True):
            env = canary._environment(Path('home'), Path('temp'), profile)
        self.assertEqual(env['PM_KIT_RELEASE_ID'], 'trusted-pin')

    def test_expected_refusal_is_mandatory_and_unrelated_failure_does_not_pass(self):
        with self.assertRaisesRegex(RuntimeError, 'did not occur'):
            canary._require_refusal(lambda: None, 'expected')
        with self.assertRaisesRegex(RuntimeError, 'unexpected acceptance refusal'):
            canary._require_refusal(mock.Mock(side_effect=ValueError('different')), 'expected')
        self.assertEqual(canary._require_refusal(mock.Mock(side_effect=ValueError('expected')), 'expected'),
                         'expected')

    def test_native_launch_always_uses_an_owned_job_and_reviewed_bind_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            planned = mock.Mock(argv=('native',), start_params={'model': canary.MODEL})
            planned.permission.profile_id = 'reviewed'
            planned.permission.toml.return_value = 'reviewed fragment'
            client = mock.Mock()
            stack = mock.Mock()
            stack.enter_context.return_value = client
            with mock.patch.object(canary, 'plan', return_value=planned), \
                    mock.patch.object(canary, 'RuntimeAppServer') as native, \
                    mock.patch.object(canary, 'inspect_runtime', return_value={'permission_allowed': True}), \
                    mock.patch.object(canary, 'RoleBindings'), mock.patch.object(canary, 'bind_started') as bind, \
                    mock.patch.object(canary, 'KitOperationExecutor'), \
                    mock.patch.object(canary, '_provider', return_value=mock.Mock(server_port=12345)), \
                    mock.patch.object(canary, '_persist'):
                canary._start(stack, 'native', root, 'analyst', str(root), mock.Mock(), {})
            self.assertIs(native.call_args.kwargs['owned_job'], True)
            bind.assert_called_once()
            self.assertNotIn('turn/start', [call.args[0] for call in client.request.call_args_list])

    def test_missing_fixture_permission_refuses_before_native_thread_start(self):
        with tempfile.TemporaryDirectory() as folder:
            planned = mock.Mock(argv=('native',))
            planned.permission.profile_id = 'reviewed'
            planned.permission.toml.return_value = 'fragment'
            client = mock.Mock()
            stack = mock.Mock()
            stack.enter_context.return_value = client
            with mock.patch.object(canary, 'plan', return_value=planned), \
                    mock.patch.object(canary, 'RuntimeAppServer'), \
                    mock.patch.object(canary, 'inspect_runtime', return_value={'permission_allowed': False}), \
                    mock.patch.object(canary, '_provider', return_value=mock.Mock(server_port=12345)):
                with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                    canary._start(stack, 'native', Path(folder), 'analyst', folder, mock.Mock(), {})
            client.request.assert_not_called()

    def test_bounded_native_commands_preserve_argv_permissions_and_original_input(self):
        client = mock.Mock()
        params = {'command': ['python', 'real-tool.py'], 'timeoutMs': 600000,
                  'permissionProfile': 'reviewed', 'cwd': 'assigned'}
        canary._bounded(client, 'command/exec', params)
        actual = client.request.call_args.args[1]
        self.assertEqual(actual, dict(params, timeoutMs=30000))
        self.assertEqual(params['timeoutMs'], 600000)
        canary._bounded(client, 'thread/read', {'threadId': 'actual'})
        self.assertEqual(client.request.call_args.args, ('thread/read', {'threadId': 'actual'}))

    def test_unrelated_prepare_failure_never_creates_fixture_worker_or_claim(self):
        task = mock.Mock()
        with mock.patch.object(canary.workflow, 'prepare',
                               side_effect=canary.workflow.Stopped('new_worktree', 'fetch failed: other')), \
                mock.patch.object(canary, '_git') as git:
            with self.assertRaises(canary.workflow.Stopped):
                canary._prepare_or_isolate(task, mock.Mock(), {})
        git.assert_not_called()

    def test_successful_prepare_is_distinguished_from_owner_scaffolding(self):
        with mock.patch.object(canary.workflow, 'prepare',
                               side_effect=[{'stage': 'claimed'}, canary.workflow.Stopped('prepare', 'exists')]):
            result = canary._prepare_or_isolate(mock.Mock(), mock.Mock(), {})
        self.assertIs(result['native_prepare'], True)
        self.assertIs(result['native_claim'], True)

    def test_native_mapping_refusal_with_partial_checkout_is_never_scaffolded(self):
        with tempfile.TemporaryDirectory() as folder:
            task = mock.Mock(worker=folder)
            failure = canary.workflow.Stopped('new_worktree', 'fetch failed: CreateFileMapping Win32 error 5')
            with mock.patch.object(canary.workflow, 'prepare', side_effect=failure), \
                    mock.patch.object(canary, '_git') as git:
                with self.assertRaisesRegex(RuntimeError, 'unexpected partial fixture state'):
                    canary._prepare_or_isolate(task, mock.Mock(), {})
            git.assert_not_called()

    def test_private_rollout_requires_one_fixed_provider_response(self):
        client = mock.Mock()
        binding = mock.Mock(thread='actual')
        provider = mock.Mock(error=None, calls=0)
        events = [('turn/completed', {'threadId': 'actual'})]
        with self.assertRaisesRegex(RuntimeError, 'provider failed'):
            canary._persist(client, binding, provider, events)
        self.assertEqual(client.request.call_args.args[0], 'turn/start')

    def _results(self, folder):
        root = Path(folder) / 'results'
        mine, other = root / 'fixture-abc', root / 'real-def'
        for item in (mine, other):
            item.mkdir(parents=True)
            (item / 'gate.json').write_text('{}')
        return root, mine, other

    def test_result_folder_cleanup_removes_only_the_fixture_key_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root, mine, other = self._results(folder)
            with mock.patch('kitlib.gate.result.machine_root', return_value=str(root)):
                canary._remove_result_folder(str(mine))
                self.assertFalse(mine.exists())
                self.assertTrue((other / 'gate.json').exists())
                canary._remove_result_folder(str(mine))      # missing: ignored
                canary._remove_result_folder(None)
                canary._remove_result_folder(str(root))      # the root is never removed
                canary._remove_result_folder(str(Path(folder)))
            self.assertTrue((other / 'gate.json').exists())

    def test_result_folder_cleanup_refuses_the_root_and_a_folder_outside_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root, mine, other = self._results(folder)
            with mock.patch('kitlib.gate.result.machine_root', return_value=str(root)):
                canary._remove_result_folder(str(root / '..'))      # the root's parent
                canary._remove_result_folder(str(mine / '..'))      # the root itself
                canary._remove_result_folder(str(mine / 'deeper'))  # not a child of the root
            self.assertTrue((other / 'gate.json').exists())
            self.assertTrue((mine / 'gate.json').exists())

    def test_result_folder_cleanup_unlinks_a_junction_inside_without_emptying_its_target(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root, mine, other = self._results(folder)
            link = mine / 'inner'
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(other)],
                                  capture_output=True, text=True)
            if made.returncode:
                self.skipTest('no junction: ' + made.stderr)
            with mock.patch('kitlib.gate.result.machine_root', return_value=str(root)):
                canary._remove_result_folder(str(mine))
            self.assertFalse(mine.exists())
            self.assertTrue((other / 'gate.json').exists())

    def test_result_folder_cleanup_never_follows_a_junction(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root, mine, other = self._results(folder)
            link = root / 'linked'
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(other)],
                                  capture_output=True, text=True)
            if made.returncode:
                self.skipTest('no junction: ' + made.stderr)
            try:
                with mock.patch('kitlib.gate.result.machine_root', return_value=str(root)):
                    canary._remove_result_folder(str(link))
                self.assertTrue((other / 'gate.json').exists())
                self.assertTrue(link.exists())
            finally:
                os.rmdir(link)

    def test_cli_help_and_missing_fixture_root_are_bounded(self):
        import sys
        from kitlib.proc import spawn
        script = Path(__file__).resolve().parents[2] / 'hosts/codex/shell/scripts/workflow_canary.py'
        for args, expected in ((['--help'], 0), ([], 2)):
            result = spawn.run([sys.executable, str(script), *args], capture_output=True,
                               text=True, shell=False, timeout=10)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
