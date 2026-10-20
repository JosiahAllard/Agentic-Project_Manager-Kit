import copy
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from hosts.codex.adapter.runtime import KitToolBroker, RoleBindings, RuntimeAppServer, SandboxCommandHost, tool_definition
from kitlib.git import repo


ROOT = str(Path(__file__).resolve().parents[2])


def snapshot(role=None, child=False, mode='workspaceWrite'):
    thread = {'id': 'child' if child else 'parent', 'sessionId': 'session', 'cwd': ROOT,
              'model': 'gpt-6.1-sol', 'agentRole': role, 'parentThreadId': 'parent' if child else None,
              'source': {'subAgent': {'thread_spawn': {'parent_thread_id': 'parent', 'agent_role': role}}} if child else 'appServer'}
    return {'thread': thread, 'cwd': ROOT, 'runtimeWorkspaceRoots': [ROOT],
            'model': 'gpt-6.1-sol', 'sandbox': {'type': mode, 'networkAccess': False},
            'activePermissionProfile': None, 'approvalPolicy': 'on-request'}


class Bindings(unittest.TestCase):
    def registry(self, request):
        return RoleBindings(request, classify=lambda _: SimpleNamespace(kind=repo.LINKED))

    def test_child_analyst_uses_native_role_and_resolved_read_only_permissions(self):
        child = snapshot('kit-analyst', True, 'readOnly')
        request = mock.Mock(side_effect=[{'thread': child['thread']}, child])
        registry = self.registry(request)
        registry.add_root(snapshot(), 'worker')
        bound = registry.resolve('child')
        self.assertEqual(bound.role, 'analyst')
        self.assertEqual(json.loads(bound.policy_json)['type'], 'readOnly')
        self.assertEqual(request.call_args_list[1].args, ('thread/resume', {'threadId': 'child'}))

    def test_unmarked_owner_root_does_not_unmark_a_native_worker_child(self):
        child = snapshot('kit-worker', True)
        request = mock.Mock(side_effect=[{'thread': child['thread']}, child])
        registry = self.registry(request)
        registry.add_root(snapshot(), None)
        self.assertEqual(registry.resolve('child').role, 'worker')

    def test_forged_parent_role_session_or_checkout_fails_before_resume(self):
        for key, value in (('parentThreadId', 'other'), ('agentRole', 'kit-pm'),
                           ('sessionId', 'other'), ('cwd', str(Path(ROOT).parent))):
            child = snapshot('kit-analyst', True, 'readOnly')
            child['thread'][key] = value
            request = mock.Mock(return_value={'thread': child['thread']})
            registry = self.registry(request)
            registry.add_root(snapshot(), 'worker')
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.resolve('child')
            self.assertEqual(request.call_count, 1)

    def test_real_child_with_an_unreviewed_profile_is_refused(self):
        child = snapshot('default', True, 'readOnly')
        registry = self.registry(mock.Mock(return_value={'thread': child['thread']}))
        registry.add_root(snapshot(), 'worker')
        with self.assertRaises(ValueError):
            registry.resolve('child')

    def test_snapshot_never_accepts_external_or_unrestricted_sandbox(self):
        for mode in ('externalSandbox', 'dangerFullAccess'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.registry(mock.Mock()).add_root(snapshot(mode=mode), 'worker')

    def test_missing_or_expanded_workspace_snapshot_is_refused(self):
        for roots in (None, [], [ROOT, str(Path(ROOT).parent)]):
            value = snapshot()
            value['runtimeWorkspaceRoots'] = roots
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                self.registry(mock.Mock()).add_root(value, 'worker')

    def test_read_role_cannot_use_a_writable_native_profile(self):
        with self.assertRaises(ValueError):
            self.registry(mock.Mock()).add_root(snapshot(), 'analyst')

    @unittest.skipUnless(os.name == 'nt', 'Windows paths')
    def test_native_workspace_path_case_is_normalized(self):
        value = snapshot()
        value['runtimeWorkspaceRoots'] = [ROOT.upper()]
        self.assertEqual(self.registry(mock.Mock()).add_root(value, 'worker').cwd, ROOT)


class Commands(unittest.TestCase):
    def setUp(self):
        self.root = snapshot()
        self.request = mock.Mock(return_value=self.root)
        self.registry = RoleBindings(self.request, classify=lambda _: SimpleNamespace(kind=repo.LINKED))
        self.bound = self.registry.add_root(self.root, 'worker')

    def native(self, result):
        return mock.Mock(side_effect=lambda method, params:
                         {'config': {'windows': {'sandbox': 'unelevated'}}} if method == 'config/read' else result)

    def test_execution_uses_exact_native_policy_and_no_interactive_session(self):
        request = self.native({'exitCode': 0, 'stdout': 'OK', 'stderr': ''})
        host = SandboxCommandHost(request, self.bound)
        self.assertEqual(host.run_argv(('python', 'tool.py', '$(literal)'), ROOT)['exitCode'], 0)
        method, params = request.call_args.args
        self.assertEqual(method, 'command/exec')
        self.assertEqual(params['command'][-1], '$(literal)')
        self.assertEqual(params['sandboxPolicy'], self.root['sandbox'])
        self.assertFalse(params['tty'] or params['streamStdin'] or params['streamStdoutStderr'])
        self.assertNotIn('processId', params)
        self.assertNotIn('disableOutputCap', params)

    def test_named_permission_profile_is_preserved_without_legacy_policy_override(self):
        value = snapshot()
        value['activePermissionProfile'] = {'id': 'reviewed-profile'}
        binding = RoleBindings(mock.Mock(), classify=lambda _: SimpleNamespace(kind=repo.LINKED)).add_root(value, 'worker')
        request = self.native({'exitCode': 0, 'stdout': '', 'stderr': ''})
        SandboxCommandHost(request, binding).run_argv(('python', 'tool.py'), ROOT)
        params = request.call_args.args[1]
        self.assertEqual(params['permissionProfile'], 'reviewed-profile')
        self.assertNotIn('sandboxPolicy', params)

    def test_checkout_and_stdin_overrides_never_reach_native_execution(self):
        request = mock.Mock()
        host = SandboxCommandHost(request, self.bound)
        for cwd, closed in ((str(Path(ROOT).parent), True), (ROOT, False)):
            with self.subTest(cwd=cwd, closed=closed), self.assertRaises(ValueError):
                host.run_argv(('python', 'tool.py'), cwd, stdin_closed=closed)
        request.assert_not_called()

    def test_permission_labels_without_windows_backend_never_execute(self):
        request = mock.Mock(return_value={'config': {}})
        with self.assertRaisesRegex(ValueError, 'Windows sandbox backend'):
            SandboxCommandHost(request, self.bound).run_argv(('python', 'tool.py'), ROOT)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['config/read'])

    def test_native_result_cannot_return_an_interactive_process_handle(self):
        request = self.native({'exitCode': 0, 'stdout': '', 'stderr': '', 'processId': 'forged'})
        with self.assertRaises(RuntimeError):
            SandboxCommandHost(request, self.bound).run_argv(('python', 'tool.py'), ROOT)

    def callback(self, arguments):
        return {'threadId': 'parent', 'turnId': 'turn', 'callId': 'call',
                'tool': 'pmkit_run', 'arguments': arguments}

    def test_model_cannot_supply_role_checkout_thread_or_permissions_in_arguments(self):
        broker = KitToolBroker(self.request, self.registry, guard=mock.Mock(return_value=None))
        for field in ('role', 'cwd', 'threadId', 'sandboxPolicy'):
            args = {'tool': 'preflight', 'args': [], field: 'forged'}
            callback = self.callback(args)
            callback['callId'] = field
            result = broker.handle('item/tool/call', callback)
            self.assertFalse(result['success'])
        self.assertFalse(any(c.args[0] == 'command/exec' for c in self.request.call_args_list))

    def test_broker_calls_shared_guard_then_native_kit_cli(self):
        def request(method, params):
            if method == 'thread/read':
                return {'thread': self.root['thread']}
            if method == 'thread/resume':
                return self.root
            if method == 'config/read':
                return {'config': {'windows': {'sandbox': 'unelevated'}}}
            self.assertEqual(method, 'command/exec')
            self.assertTrue(params['command'][1].endswith('preflight.py'))
            return {'exitCode': 0, 'stdout': 'OK', 'stderr': ''}
        guard = mock.Mock(return_value=None)
        broker = KitToolBroker(request, self.registry, guard=guard)
        with mock.patch('kitlib.proc.spawn.run', side_effect=AssertionError('no fallback')):
            result = broker.handle('item/tool/call', self.callback({'tool': 'preflight', 'args': []}))
        self.assertTrue(result['success'], result)
        self.assertEqual(guard.call_args.kwargs['binding'], self.bound)
        self.assertNotIn('environ', guard.call_args.kwargs)

    def test_native_pending_call_replay_returns_the_recorded_outcome_once(self):
        commands = []
        def request(method, params):
            if method == 'thread/read':
                return {'thread': self.root['thread']}
            if method == 'thread/resume':
                return self.root
            if method == 'config/read':
                return {'config': {'windows': {'sandbox': 'unelevated'}}}
            commands.append(params)
            return {'exitCode': 0, 'stdout': 'once', 'stderr': ''}
        broker = KitToolBroker(request, self.registry, guard=mock.Mock(return_value=None))
        callback = self.callback({'tool': 'preflight', 'args': []})
        first = broker.handle('item/tool/call', callback)
        second = broker.handle('item/tool/call', copy.deepcopy(callback))
        self.assertEqual(first, second)
        self.assertTrue(first['success'])
        self.assertEqual(len(commands), 1)
        changed = copy.deepcopy(callback)
        changed['arguments']['args'] = ['--other']
        self.assertFalse(broker.handle('item/tool/call', changed)['success'])
        self.assertEqual(len(commands), 1)

    def test_guard_refusal_never_executes_a_command(self):
        guard = mock.Mock(return_value={'hookSpecificOutput': {'permissionDecisionReason': 'denied'}})
        broker = KitToolBroker(self.request, self.registry, guard=guard)
        result = broker.handle('item/tool/call', self.callback({'tool': 'preflight', 'args': []}))
        self.assertFalse(result['success'])
        self.assertEqual(self.request.call_count, 2)

    def test_permissions_changed_after_binding_are_refused_before_execution(self):
        updated = copy.deepcopy(self.root)
        updated['sandbox']['type'] = 'readOnly'
        self.request.side_effect = [{'thread': self.root['thread']}, updated]
        broker = KitToolBroker(self.request, self.registry, guard=mock.Mock(return_value=None))
        result = broker.handle('item/tool/call', self.callback({'tool': 'preflight', 'args': []}))
        self.assertFalse(result['success'])
        self.assertIn('permissions or identity changed', result['contentItems'][0]['text'])
        self.assertEqual([call.args[0] for call in self.request.call_args_list], ['thread/read', 'thread/resume'])

    def test_malformed_literal_arguments_never_reach_native_execution(self):
        broker = KitToolBroker(self.request, self.registry, guard=mock.Mock(return_value=None))
        for index, tail in enumerate(([{'role': 'pm'}], ['bad\x00argument'], [False])):
            callback = self.callback({'tool': 'preflight', 'args': tail})
            callback['callId'] = str(index)
            self.assertFalse(broker.handle('item/tool/call', callback)['success'])
        self.assertFalse(any(c.args[0] == 'command/exec' for c in self.request.call_args_list))

    def test_expired_broker_result_cannot_repeat_a_command(self):
        broker = KitToolBroker(self.request, self.registry, guard=mock.Mock(return_value=None))
        call = ('parent', 'turn', 'call')
        broker.calls.add(call)
        result = broker.handle('item/tool/call', self.callback({'tool': 'preflight', 'args': []}))
        self.assertFalse(result['success'])
        self.request.assert_not_called()

    def test_unrecognized_requests_are_never_auto_approved(self):
        broker = KitToolBroker(self.request, self.registry)
        with self.assertRaises(ValueError):
            broker.handle('item/commandExecution/requestApproval', {})
        self.request.assert_not_called()

    def test_tool_schema_exposes_only_existing_kit_operation_and_literal_arguments(self):
        schema = tool_definition()['inputSchema']
        self.assertEqual(set(schema['properties']), {'tool', 'args'})
        self.assertFalse(schema['additionalProperties'])


class RuntimeLifetime(unittest.TestCase):
    def test_owned_job_ends_descendants_before_joining_pipe_reader(self):
        client = RuntimeAppServer(('unused.exe',), ROOT, owned_job=True)
        client.process, client.job, client.rpc = mock.Mock(), mock.Mock(), mock.Mock()
        job = client.job
        client.rpc.reader.is_alive.return_value = False
        client.rpc.reader.join.side_effect = lambda **_: job.end.assert_called_once_with(kill=True)
        self.assertEqual(client.close(), ())
        self.assertIsNone(client.job)

    def test_failed_job_start_never_falls_back_to_unowned_process(self):
        client = RuntimeAppServer(('unused.exe',), ROOT, owned_job=True)
        with mock.patch('kitlib.proc.job.Job') as factory, \
                mock.patch('kitlib.proc.spawn.popen') as unsafe:
            factory.return_value.start.side_effect = OSError('owned job refused')
            with self.assertRaisesRegex(OSError, 'owned job refused'):
                client.__enter__()
            unsafe.assert_not_called()
            factory.return_value.end.assert_called_once_with(kill=True)
            self.assertIsNone(client.job)

    def test_owned_job_is_the_only_process_starter(self):
        client = RuntimeAppServer(('unused.exe',), ROOT, owned_job=True)
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO('{"id":1,"result":{}}\n')
        with mock.patch('kitlib.proc.job.Job') as factory, \
                mock.patch('kitlib.proc.spawn.popen') as unsafe:
            factory.return_value.start.return_value = process
            with client:
                self.assertIs(client.process, process)
            factory.assert_called_once_with(mask=None, kill_on_close=True)
            factory.return_value.start.assert_called_once()
            factory.return_value.end.assert_called_once_with(kill=True)
            unsafe.assert_not_called()

    def test_unsandboxed_native_methods_never_reach_the_transport(self):
        client = RuntimeAppServer(('unused.exe',), ROOT)
        client.rpc = mock.Mock()
        for method in ('process/spawn', 'thread/shellCommand', 'command/exec/write', 'hooks/trust'):
            with self.subTest(method=method), self.assertRaises(ValueError):
                client.request(method)
        client.rpc.request.assert_not_called()

    def test_cleanup_waits_then_terminates_only_its_owned_process(self):
        client = RuntimeAppServer(('unused.exe',), ROOT)
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('owned', 3), 0]
        client.process = process
        self.assertEqual(client.close(), ())
        process.stdin.close.assert_called_once()
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertIsNone(client.process)

    def test_cleanup_failure_is_reported_without_overwriting_a_primary_error(self):
        for primary in (None, ValueError):
            client = RuntimeAppServer(('unused.exe',), ROOT)
            process = mock.Mock()
            process.stdin.close.side_effect = OSError('closed pipe')
            client.process = process
            if primary is None:
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    client.__exit__(primary)
            else:
                client.__exit__(primary)
            process.wait.assert_called_once()
            self.assertTrue(client.cleanup_errors)


class BoundGuard(unittest.TestCase):
    """evaluate_bound: the broker's guard holds the BOUND role (never the inherited PM_KIT_ROLE) as
    an agent of that role, root or child, with the role policy on for it whatever .kit.json says:
    the World's .kit.json here leaves guard.role_policy unset."""

    def setUp(self):
        from tests.guard.world import World
        from tests.guard.test_rolepolicy import kit
        self.w = World(kit=kit({}, on=None))
        self.addCleanup(self.w.cleanup)

    def run_bound(self, role, event, child=False, cwd=None):
        from hosts.codex.adapter.runtime import evaluate_bound
        from kitlib import kitroot
        binding = SimpleNamespace(role=role, cwd=cwd or self.w.wt, parent='parent' if child else None, thread='t')
        event = dict(event, hook_event_name='PreToolUse', cwd=binding.cwd)
        with mock.patch.dict(os.environ, {'PM_KIT_ROLE': 'worker'}):
            out = evaluate_bound(event, binding, ctx=self.w.ctx(kit=kitroot.root()))
        return out and out['hookSpecificOutput']['permissionDecisionReason']

    def judge(self, role, path, child=False, cwd=None):
        return self.run_bound(role, {'tool_name': 'apply_patch', 'tool_input': {
            'command': '*** Begin Patch\n*** Add File: %s\n+x\n*** End Patch' % path}}, child, cwd)

    def shell(self, role, command, cwd=None, **data):
        return self.run_bound(role, {'tool_name': 'PowerShell', 'tool_input': dict(data, command=command)}, cwd=cwd)

    def test_bound_guard_ignores_the_inherited_role(self):
        self.assertIsNone(self.judge('worker', 'a.py'))
        self.assertIn('rule=host-role-edit', self.judge('analyst', 'a.py'))

    def test_reader_lander_and_test_roles_cannot_patch_files(self):
        for role in ('analyst', 'lander', 'test-runner'):
            with self.subTest(role=role):
                self.assertIn('rule=host-role-edit', self.judge(role, 'worker.py'))

    def test_test_and_lander_shell_writes_are_denied(self):
        self.assertIsNotNone(self.shell('test-runner', 'Set-Content -LiteralPath a.txt -Value changed'))
        self.assertIsNotNone(self.shell('lander', 'Set-Content -LiteralPath a.txt -Value changed', cwd=self.w.main))
        self.assertIsNone(self.shell('worker', 'Set-Content -LiteralPath a.txt -Value changed'))

    def test_bound_guard_holds_root_and_child_off_main(self):
        target = os.path.join(self.w.main, 'a.py')
        for child in (False, True):
            with self.subTest(child=child):
                self.assertIn('rule=main-checkout', self.judge('worker', target, child=child))

    def backlog(self, role, args, child=False):
        return self.run_bound(role, {'tool_name': 'PowerShell', 'tool_input': {
            'command': 'python tools/backlog.py ' + args}}, child)

    def test_a_bound_root_meets_the_backlog_tools_own_check(self):
        """A root binding is a marked session, judged as an agent of its mark (hookmain.UNIDENTIFIED):
        the backlog's role check is the tool's own, run under the bound mark, so a coordinator root
        may claim its item. A child bound to a session-only role is unlisted, as on Claude."""
        pull = '--session s --as kit/x pull X-1'
        self.assertIsNone(self.backlog('coordinator', pull))
        self.assertIn('rule=backlog-role', self.backlog('coordinator', pull, child=True) or '')
        self.assertIn('rule=backlog-role', self.backlog('worker', 'close X-1', child=True) or '')

    def test_bound_guard_role_policy_checks_run_with_the_policy_off(self):
        self.assertIn('rule=role-shell', self.shell('worker', 'powershell.exe'))
        self.assertIn('rule=role-workdir', self.shell('worker', 'Get-Location', workdir=self.w.plain))

    # .kit.json shapes that fail to load: bad JSON, empty, a BOM alone, null, an array, a section
    # file that is missing or bad, and a directory.
    BROKEN = ('{ broken', '', '\ufeff', 'null', '[]', '{"guard": "missing.json"}', '{"guard": "g.json"}', 'dir')

    def break_kit_json(self, shape):
        from tests.guard.world import World
        from tests.guard.test_rolepolicy import kit
        self.w = World(kit=kit({}, on=None))
        self.addCleanup(self.w.cleanup)
        target = os.path.join(self.w.main, '.kit.json')
        if os.path.exists(target):
            os.remove(target)
        if shape == 'dir':
            os.mkdir(target)
            return
        with open(os.path.join(self.w.main, 'g.json'), 'w', encoding='utf-8') as f:
            f.write('{bad')
        with open(target, 'w', encoding='utf-8') as f:
            f.write(shape)

    def test_a_kit_json_that_does_not_load_refuses_every_call(self):
        """PolicyOn uses a default config only where no .kit.json exists. One that fails to load
        (any shape) would leave the bound role's policy off, so the broker refuses every call until
        it is fixed, not only the calls the shared rules refuse on a config error."""
        calls = {'role-shell': lambda role: self.shell(role, 'powershell.exe'),
                 'role-workdir': lambda role: self.shell(role, 'Get-Location', workdir=self.w.plain),
                 'commit': lambda role: self.shell(role, 'git commit -am x'),
                 'push': lambda role: self.shell(role, 'git push --force origin HEAD:main'),
                 'status': lambda role: self.shell(role, 'git status')}
        for shape in self.BROKEN:
            self.break_kit_json(shape)
            for role in ('analyst', 'test-runner', 'worker', 'coordinator'):
                for name, call in calls.items():
                    with self.subTest(shape=shape, role=role, call=name):
                        reason = call(role) or ''
                        self.assertIn('rule=host-runtime-error', reason)
                        self.assertIn('.kit.json does not load', reason)

    def test_no_kit_json_gets_the_policy_default(self):
        os.remove(os.path.join(self.w.main, '.kit.json'))
        self.assertIn('rule=role-shell', self.shell('worker', 'powershell.exe'))
        self.assertIn('rule=role-workdir', self.shell('worker', 'Get-Location', workdir=self.w.plain))
        self.assertIsNone(self.shell('worker', 'git status'))

    def test_bound_guard_refuses_a_spoofed_checkout(self):
        from hosts.codex.adapter.runtime import evaluate_bound
        binding = SimpleNamespace(role='worker', cwd=self.w.wt, parent=None, thread='t')
        out = evaluate_bound({'hook_event_name': 'PreToolUse', 'cwd': self.w.main, 'tool_name': 'Read',
                              'tool_input': {}}, binding)
        self.assertIn('checkout differs', out['hookSpecificOutput']['permissionDecisionReason'])

    def test_a_sandboxed_command_carries_the_bound_role_and_host(self):
        from hosts.codex.adapter.runtime import command_env
        env = command_env('analyst')
        self.assertEqual((env['PM_KIT_ROLE'], env['PM_KIT_HOST'], env['PM_KIT_SESSION_NAME']), ('analyst', 'codex', None))
        self.assertIn('CLAUDE_AGENT_MODEL', env, 'another host\'s model variable is cleared')
        self.assertIsNone(env['CLAUDE_AGENT_MODEL'])
