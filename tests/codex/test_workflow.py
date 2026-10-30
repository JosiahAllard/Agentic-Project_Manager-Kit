"""Protocol checks and disposable shared-kit workflow acceptance, not native proof."""
from pathlib import Path
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from hosts.codex.adapter import workflow as W
from hosts.codex.adapter.kit import discover
from hosts.codex.adapter.runtime import BoundKitHost, KitOperationExecutor, RoleBindings
from tests.gate import fixtures
from tests.realgit import real_git


def task():
    return W.Task(str(Path.cwd()), str(Path.cwd()), 'codex/probe', 'PROBE',
                  'probe-session', 'gpt-6.1-sol', 'a' * 40, ('value.txt',), discover())


def linked(path):
    """The tests run from the main checkout or a linked worktree alike: the place check is faked."""
    from kitlib.git import repo
    return SimpleNamespace(kind=repo.LINKED, top=path)


def host(role='worker', cwd=None, model='gpt-6.1-sol'):
    result = mock.Mock(spec=BoundKitHost)
    result.binding = SimpleNamespace(role=role, cwd=cwd or str(Path.cwd()), model=model)
    result.run_tool.return_value = {'exitCode': 0, 'stdout': 'ok', 'stderr': ''}
    return result


class Protocol(unittest.TestCase):
    def fresh_host(self, *, guard=None):
        from tests.codex.test_runtime import snapshot
        value = snapshot()
        live = copy.deepcopy(value)

        def request(method, params):
            if method == 'thread/read':
                return {'thread': live['thread']}
            if method == 'thread/resume':
                return live
            if method == 'config/read':
                return {'config': {'windows': {'sandbox': 'unelevated'}}}
            return {'exitCode': 0, 'stdout': 'ok', 'stderr': ''}
        rpc = mock.Mock(side_effect=request)
        registry = RoleBindings(rpc, classify=linked)
        binding = registry.add_root(value, 'worker')
        return BoundKitHost(KitOperationExecutor(rpc, registry, guard=guard or mock.Mock(return_value=None)),
                            binding), live, rpc

    def test_stale_model_permissions_or_session_refuse_before_guard_and_command(self):
        for field in ('model', 'permissions', 'session'):
            guard = mock.Mock(return_value=None)
            h, live, rpc = self.fresh_host(guard=guard)
            if field == 'model':
                live['model'] = live['thread']['model'] = 'new-model'
            elif field == 'permissions':
                live['sandbox']['type'] = 'readOnly'
            else:
                live['thread']['sessionId'] = 'another-session'
            with self.subTest(field=field), self.assertRaisesRegex(W.Stopped, 'changed'):
                W._run(task(), 'preflight', (), 'worker', h.binding.cwd, h)
            guard.assert_not_called()
            self.assertNotIn('command/exec', [c.args[0] for c in rpc.call_args_list])

    def test_local_executor_applies_shared_guard_without_fabricating_callback_identity(self):
        guard = mock.Mock(return_value={'hookSpecificOutput': {'permissionDecisionReason': 'guard denied'}})
        h, _, rpc = self.fresh_host(guard=guard)
        with self.assertRaisesRegex(W.Stopped, 'guard denied'):
            W._run(task(), 'preflight', (), 'worker', h.binding.cwd, h)
        guard.assert_called_once()
        self.assertEqual([c.args[0] for c in rpc.call_args_list], ['thread/read', 'thread/resume'])

    def test_unreadable_kit_configuration_is_a_preview_refusal(self):
        from kitlib.backlog.errors import Failure
        with mock.patch('kitlib.backlog.opener.open_repo', side_effect=Failure('bad configuration')), \
                mock.patch('kitlib.git.repo.classify', side_effect=linked):
            with self.assertRaisesRegex(ValueError, 'bad configuration'):
                W.plan(str(Path.cwd()), 'codex/probe', 'PROBE', 'probe-session',
                       'gpt-6.1-sol', ('value.txt',))

    def test_captured_raw_command_host_cannot_bypass_fresh_resolution(self):
        from hosts.codex.adapter.runtime import SandboxCommandHost
        from tests.codex.test_runtime import snapshot
        request = mock.Mock()
        binding = RoleBindings(request, classify=linked).add_root(snapshot(), 'worker')
        raw = SandboxCommandHost(request, binding)
        with self.assertRaisesRegex(W.Stopped, 'fresh-bound kit executor'):
            W._run(task(), 'preflight', (), 'worker', binding.cwd, raw)
        request.assert_not_called()

    def test_wrong_bound_role_refuses_before_execution(self):
        h = host('analyst')
        with self.assertRaisesRegex(W.Stopped, 'bound to the requested role'):
            W._run(task(), 'commit', (), 'worker', str(Path.cwd()), h)
        h.run_tool.assert_not_called()

    def test_wrong_cwd_refuses_before_execution(self):
        h = host(cwd=str(Path.cwd().parent))
        with self.assertRaises(W.Stopped):
            W._run(task(), 'preflight', (), 'worker', str(Path.cwd()), h)
        h.run_tool.assert_not_called()

    def test_wrong_native_model_cannot_be_attributed(self):
        h = host(model='other-model')
        with self.assertRaisesRegex(W.Stopped, 'native worker model'):
            W._run(task(), 'commit', (), 'worker', str(Path.cwd()), h)
        h.run_tool.assert_not_called()

    def test_failed_tool_is_not_automatically_retried(self):
        h = host()
        h.run_tool.return_value = {'exitCode': 1, 'stdout': '', 'stderr': 'refused'}
        with mock.patch.object(W.kit_adapter, 'plan') as p:
            p.return_value = SimpleNamespace(argv=('python', 'preflight.py'),
                                            cwd=str(Path.cwd()), role='worker')
            with self.assertRaisesRegex(W.Stopped, 'inspect authoritative state'):
                W._run(task(), 'preflight', (), 'worker', str(Path.cwd()), h)
        self.assertEqual(h.run_tool.call_count, 1)

    def test_boolean_exit_and_unexpected_handles_are_rejected(self):
        for result in ({'exitCode': False, 'stdout': '', 'stderr': ''},
                       {'exitCode': 0, 'stdout': '', 'stderr': '', 'processId': 'x'}):
            h = host()
            h.run_tool.return_value = result
            with self.assertRaisesRegex(W.Stopped, 'invalid CommandHost result'):
                W._run(task(), 'preflight', (), 'worker', str(Path.cwd()), h)

    def test_missing_full_hash_does_not_prepare_review(self):
        h = host('analyst')
        with self.assertRaisesRegex(ValueError, 'full expected'):
            W.handoff(task(), 'a' * 8, h)
        h.run_tool.assert_not_called()

    def test_already_committed_work_is_not_replayed(self):
        with mock.patch.object(W, '_checkout'), mock.patch.object(W, '_claimed'), \
                mock.patch.object(W, '_git', return_value=''), mock.patch.object(W, '_run') as run:
            with self.assertRaisesRegex(W.Stopped, 'do not replay'):
                W.complete(task(), 'probe', host(), host('test-runner'), host('analyst'))
            run.assert_not_called()

    def test_dirty_or_changed_tip_has_no_review_handoff(self):
        with mock.patch.object(W, '_checkout'), mock.patch.object(W, '_claimed'), \
                mock.patch.object(W, '_head', return_value='b' * 40), \
                mock.patch.object(W, '_run') as run:
            with self.assertRaisesRegex(W.Stopped, 'checkout changed'):
                W.handoff(task(), 'a' * 40, host('analyst'))
            run.assert_not_called()

    def test_ungated_tip_is_not_review_ready(self):
        with mock.patch.object(W, '_checkout'), mock.patch.object(W, '_claimed'), \
                mock.patch.object(W, '_head', return_value='a' * 40), \
                mock.patch.object(W, '_git', return_value=''), \
                mock.patch('kitlib.records.gate_ok.verify', return_value=(None, 'no record')), \
                mock.patch.object(W, '_run') as run:
            with self.assertRaisesRegex(W.Stopped, 'gate proof is missing'):
                W.handoff(task(), 'a' * 40, host('analyst'))
            run.assert_not_called()

    def test_tip_advancing_during_review_preparation_refuses_handoff(self):
        with mock.patch.object(W, '_checkout'), mock.patch.object(W, '_claimed'), \
                mock.patch.object(W, '_head', side_effect=['a' * 40, 'b' * 40]), \
                mock.patch.object(W, '_git', return_value=''), \
                mock.patch('kitlib.records.gate_ok.verify', return_value=({'verdict': 'OK', 'base': 'a' * 40}, None)), \
                mock.patch('kitlib.git.refs.merge_base', return_value=('a' * 40, None)), \
                mock.patch.object(W, '_run', return_value={'stdout': 'pack'}):
            with self.assertRaisesRegex(W.Stopped, 'during review'):
                W.handoff(task(), 'a' * 40, host('analyst'))

    def test_recovery_only_plans_read_tools(self):
        operations = W.recovery_plan(task())
        self.assertEqual([Path(p.argv[1]).stem for p in operations], ['backlog', 'worktree_paths'])
        self.assertIn('show', operations[0].argv)

    def test_preview_is_explicit_about_provider_independence_and_missing_review(self):
        preview = W.describe(task())
        self.assertEqual(preview['bridge'], 'not_required')
        self.assertEqual(preview['webpage'], 'not_required')
        self.assertEqual(preview['independent_review'], 'required')

    def test_prepared_base_outside_tip_ancestry_refuses_before_review(self):
        with mock.patch.object(W, '_checkout'), mock.patch.object(W, '_claimed'), \
                mock.patch.object(W, '_head', return_value='a' * 40), \
                mock.patch.object(W, '_git', return_value=''), \
                mock.patch('kitlib.records.gate_ok.verify', return_value=({'base': 'b' * 40}, None)), \
                mock.patch('kitlib.git.refs.merge_base', return_value=('b' * 40, None)), \
                mock.patch.object(W, '_run') as run:
            with self.assertRaisesRegex(W.Stopped, 'not an ancestor'):
                W.handoff(task(), 'a' * 40, host('analyst'))
            run.assert_not_called()


class RelocatedEntry(unittest.TestCase):
    def test_cached_subtree_help_and_safe_preview_refusal(self):
        from kitlib.proc import spawn
        from tests.gitfixture import fixture_env
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='local-workflow-cache-') as folder:
            base = Path(folder)
            plugin = base / 'published-cache'
            shutil.copytree(root / 'hosts' / 'codex' / 'shell', plugin, ignore=shutil.ignore_patterns('__pycache__'))
            home = base / 'home'
            home.mkdir()
            env = fixture_env({'PM_KIT_ROOT': str(root), 'HOME': str(home),
                               'USERPROFILE': str(home), 'GIT_CEILING_DIRECTORIES': str(base),
                               'PYTHONDONTWRITEBYTECODE': '1'})
            entry = plugin / 'scripts' / 'workflow.py'
            result = spawn.run([sys.executable, str(entry), '--help'], cwd=base, env=env,
                               shell=False, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--branch', result.stdout)
            result = spawn.run([sys.executable, str(entry), '--cwd', str(base),
                               '--branch', 'codex/probe', '--item', 'PROBE',
                               '--session', 'probe-session', '--model', 'gpt-6.1-sol',
                               '--path', 'value.txt'], cwd=base, env=env, shell=False,
                               capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('linked coordinator checkout root', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(list(home.iterdir()), [])


class LocalHost(BoundKitHost):
    """Test injection: bounded local CLIs; gate uses the shared scratch-store API.

    This exercises real kit/Git behavior. It provides no native sandbox evidence.
    """
    def __init__(self, case, role, cwd):
        calls = []
        thread = {'id': 'fixture-' + role, 'sessionId': 'fixture-session', 'cwd': cwd,
                  'model': 'gpt-6.1-sol', 'agentRole': None, 'parentThreadId': None,
                  'source': 'appServer'}
        value = {'thread': thread, 'cwd': cwd, 'runtimeWorkspaceRoots': [cwd],
                 'model': 'gpt-6.1-sol', 'sandbox': {'type': 'readOnly' if role == 'analyst'
                  else 'workspaceWrite', 'networkAccess': False}, 'activePermissionProfile': None}

        def request(method, params):
            if method == 'thread/read':
                return {'thread': thread}
            if method == 'thread/resume':
                return value
            if method == 'config/read':
                return {'config': {'windows': {'sandbox': 'unelevated'}}}
            case.assertEqual(method, 'command/exec')
            case.assertEqual(params['cwd'], cwd)
            argv = params['command']
            calls.append(Path(argv[1]).stem)
            if calls[-1] == 'gate':
                from kitlib.gate import runner
                outcome = runner.branch_gate(cwd, base=case.main,
                        book_kw=case.book_kw, log_dir=case.logs)
                return {'exitCode': outcome.code, 'stdout': '\n'.join(outcome.lines), 'stderr': ''}
            from kitlib.proc import spawn
            env = dict(case.env)
            for key, item in params['env'].items():
                if item is None:
                    env.pop(key, None)
                else:
                    env[key] = item
            result = spawn.run(list(argv), cwd=cwd, env=env, shell=False,
                               stdin=-3, capture_output=True, text=True, timeout=60)
            return {'exitCode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

        registry = RoleBindings(request)
        binding = registry.add_root(value, role)
        super().__init__(KitOperationExecutor(request, registry), binding)
        object.__setattr__(self, 'calls', calls)


@real_git('shared entry-point subprocesses and gate locking need genuine Git')
class LocalAcceptance(fixtures.GateRepoCase):
    KIT = {'repo': {'name': 'disposable-local-acceptance'},
           'backlog': {'store': 'ref'}, 'doc_index': {'enabled': False},
           'review_pack': {'coauthor': True}}

    def setUp(self):
        super().setUp()
        self.env['KIT_FAKE_GIT'] = '0'
        self.env['PM_KIT_ROOT'] = str(Path(__file__).resolve().parents[2])
        self.env['PM_KIT_ROLE'] = 'coordinator'
        self.envpatch = mock.patch.dict(os.environ, self.env, clear=True)
        self.envpatch.start()
        self.addCleanup(self.envpatch.stop)
        self.repo.write('.gitignore', '.claude/\n.codex/\n')
        self.repo.write('value.txt', 'before\n')
        self.main = self.repo.commit('acceptance fixture', '.gitignore', 'value.txt')
        self.repo.git('push', '-q', 'origin', 'main')
        self.coordinator = self.repo.worktree('coordinator', 'codex/coordinator')
        from kitlib.backlog.opener import open_store
        from tests.backlog.helpers import priced
        ctx = open_store(self.coordinator, session='probe-session')
        ctx.store.backend.publish(ctx.store.load(), priced('PROBE', scope='value.txt',
                                  session='probe-session'), 'synthetic acceptance task')
        self.task = W.plan(self.coordinator, 'codex/probe', 'PROBE', 'probe-session',
                           'gpt-6.1-sol', ('value.txt',))

    def test_real_kit_local_workflow_and_exact_tip_handoff_without_providers(self):
        coordinator = LocalHost(self, 'coordinator', self.coordinator)
        prepared = W.prepare(self.task, coordinator)
        self.assertEqual(prepared['bridge'], 'not_required')
        self.assertEqual(coordinator.calls, ['new_worktree', 'backlog'])
        from kitlib.git import checkout
        checkout.write_text('value.txt', 'after\n', root=self.task.worker)
        worker = LocalHost(self, 'worker', self.task.worker)
        tester = LocalHost(self, 'test-runner', self.task.worker)
        analyst = LocalHost(self, 'analyst', self.task.worker)
        result = W.complete(self.task, 'controlled acceptance fixture change', worker, tester, analyst)
        self.assertEqual(result['stage'], 'review_requested')
        self.assertEqual(result['independent_review'], 'required')
        self.assertEqual(result['landing'], 'PM_only_disabled')
        self.assertEqual(worker.calls, ['commit'])
        self.assertEqual(tester.calls, ['gate'])
        self.assertEqual(analyst.calls, ['review_pack'])
        self.assertIn('Co-Authored-By: Codex gpt-6.1-sol <noreply@openai.com>',
                      self.repo.git('show', '-s', '--format=%B', result['tip']))
        self.assertIn('Closes: PROBE', self.repo.git('show', '-s', '--format=%B', result['tip']))
        self.assertEqual(self.repo.head(), self.main)
        self.assertTrue(any(d.get('kind') == 'test' for d in self.store.history_records(1)))
        self.assertEqual(W.handoff(self.task, result['tip'], analyst)['tip'], result['tip'])
        checkout.write_text('value.txt', 'later uncommitted edit\n', root=self.task.worker)
        with self.assertRaisesRegex(W.Stopped, 'dirty'):
            W.handoff(self.task, result['tip'], analyst)

    def test_prepare_cannot_replay_an_existing_checkout_or_changed_base(self):
        coordinator = LocalHost(self, 'coordinator', self.coordinator)
        W.prepare(self.task, coordinator)
        with self.assertRaisesRegex(W.Stopped, 'exists'):
            W.prepare(self.task, coordinator)
        self.assertEqual(coordinator.calls, ['new_worktree', 'backlog'])

    def test_real_gate_of_same_tip_with_narrower_base_cannot_cover_task_review(self):
        coordinator = LocalHost(self, 'coordinator', self.coordinator)
        W.prepare(self.task, coordinator)
        from kitlib.git import checkout
        worker = LocalHost(self, 'worker', self.task.worker)
        checkout.write_text('value.txt', 'intermediate task edit\n', root=self.task.worker)
        W._run(self.task, 'commit', ('--model', self.task.model, '-m', 'intermediate fixture work',
                                    'value.txt'), 'worker', self.task.worker, worker)
        narrower_base = self.repo.head(self.task.worker)
        checkout.write_text('value.txt', 'final task edit\n', root=self.task.worker)
        tester = LocalHost(self, 'test-runner', self.task.worker)
        analyst = LocalHost(self, 'analyst', self.task.worker)
        result = W.complete(self.task, 'final controlled fixture work', worker, tester, analyst)
        self.assertEqual(analyst.calls, ['review_pack'])
        from kitlib.gate import runner
        from kitlib.records import gate_ok
        from kitlib.config import load
        # Advance only the disposable remote's main, as a partial concurrent
        # landing would. The kit correctly writes no record for a nondefault
        # custom base; this makes the narrower base the real new default.
        self.repo.git('push', '-q', 'origin', narrower_base + ':refs/heads/main')
        gated = runner.branch_gate(self.task.worker, base=narrower_base,
                                   book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(gated.code, 0, gated.lines)
        record, why = gate_ok.verify(self.task.worker, result['tip'],
                                    gate_config=runner.gate_config(load(self.task.worker)))
        self.assertIsNone(why)
        self.assertEqual(record['base'], narrower_base)
        self.assertNotEqual(record['base'], self.task.base)
        with self.assertRaisesRegex(W.Stopped, 'does not cover'):
            W.handoff(self.task, result['tip'], analyst)
        self.assertEqual(analyst.calls, ['review_pack'])

    def test_main_and_escaping_file_plans_are_refused(self):
        for cwd, paths in ((self.repo.root, ('value.txt',)),
                           (self.coordinator, ('../value.txt',)),
                           (self.coordinator, ('C:/outside.txt',))):
            with self.subTest(cwd=cwd, paths=paths), self.assertRaises(ValueError):
                W.plan(cwd, 'codex/new-probe', 'PROBE', 'probe-session', 'gpt-6.1-sol', paths)

    def test_destination_and_base_follow_main_even_when_coordinator_settings_are_stale(self):
        config = json.loads(Path(self.coordinator, '.kit.json').read_text(encoding='utf-8'))
        config['repo']['worktrees_by_host'] = {'codex': 'stale-destination'}
        config['repo']['main_branch'] = 'does-not-exist'
        self.repo.write('.kit.json', json.dumps(config) + '\n', root=self.coordinator)
        current = W.plan(self.coordinator, 'codex/probe', 'PROBE', 'probe-session',
                         'gpt-6.1-sol', ('value.txt',))
        self.assertEqual(current.worker, self.task.worker)
        self.assertEqual(current.base, self.main)

    def test_changed_main_creation_configuration_refuses_before_partial_creation(self):
        config = json.loads(Path(self.repo.root, '.kit.json').read_text(encoding='utf-8'))
        config['repo']['worktrees_by_host'] = {'codex': 'new-destination'}
        self.repo.write('.kit.json', json.dumps(config) + '\n')
        coordinator = LocalHost(self, 'coordinator', self.coordinator)
        with self.assertRaisesRegex(W.Stopped, 'configuration changed'):
            W.prepare(self.task, coordinator)
        self.assertEqual(coordinator.calls, [])
        self.assertFalse(Path(self.task.worker).exists())
        from kitlib.git import refs
        self.assertEqual(refs.resolve(self.task.branch, self.coordinator, known=True), ('', None))
