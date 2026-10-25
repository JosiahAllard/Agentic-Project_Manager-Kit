"""Disposable actual-native local-task acceptance, without inference or installation.

All repositories and profiles are owned public diagnostic fixtures. The tiny
fixture gate makes an ordinary transient booking in the authoritative machine
lock. This is controller execution over fresh native metadata, not an agent/model
task or a trusted-hook test. No machine lock configuration is changed.

From the Codex branch (pmkit_codex/workflow_canary.py): a native acceptance check run by hand against an installed codex.exe (hosts/codex/shell/scripts/workflow_canary.py); kept because it checks what the runtime broker relies on, on the real binary.
"""
from contextlib import ExitStack
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

from . import workflow
from .command_canary import _remove_fixtures
from .controller import bind_started, inspect_runtime, plan
from .sessions import native_codex
from .permissions_canary import fixture_directory, write_probe
from .kit import discover, host_prefixes, load_kit
from .runtime import KitOperationExecutor, RoleBindings, RuntimeAppServer, SandboxCommandHost


MODEL = 'pmkit-workflow-diagnostic'
# The fixture's own worktree folder, not the host default: the main checkout's .kit.json decides it.
FIXTURE_FOLDER = '.fixture-agents'
SESSION = 'private-workflow-probe'
ITEM = 'PROBE'


def _handler():
    """Persist a real native rollout with one fixed response, no model inference."""
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                self.connection.settimeout(5)
                size = int(self.headers.get('Content-Length', '0'))
                if (self.path != '/v1/responses' or self.headers.get('Authorization')
                        or self.headers.get('Content-Encoding') or not 0 < size <= 8 * 1024 * 1024):
                    raise ValueError('unexpected private provider request')
                request = json.loads(self.rfile.read(size))
                if request.get('model') != MODEL or self.server.calls != 0:
                    raise ValueError('unexpected diagnostic model or extra turn')
                self.server.calls += 1
                item = {'type': 'message', 'id': 'workflow_done', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Owned fixed diagnostic response.'}]}
                events = [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'workflow_response',
                           'status': 'completed', 'output': [item],
                           'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
                body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                self.server.error = '%s: %s' % (type(error).__name__, error)
                self.send_error(400, 'private diagnostic stopped')
    return Provider


def _close_provider(provider, thread):
    provider.shutdown()
    provider.server_close()
    thread.join(timeout=5)
    if thread.is_alive():
        raise RuntimeError('private diagnostic provider did not stop')


def _provider(stack):
    provider = ThreadingHTTPServer(('127.0.0.1', 0), _handler())
    provider.calls, provider.error = 0, None
    serving = threading.Thread(target=lambda: provider.serve_forever(poll_interval=.1), daemon=True)
    serving.start()
    stack.callback(_close_provider, provider, serving)
    return provider


def _persist(client, binding, provider, events):
    client.request('turn/start', {'threadId': binding.thread, 'input': [
                   {'type': 'text', 'text': 'Owned fixed diagnostic response; no inference.'}]})
    deadline = time.monotonic() + 35
    while not any(method == 'turn/completed' and data.get('threadId') == binding.thread
                  for method, data in events):
        if time.monotonic() >= deadline:
            raise RuntimeError('private diagnostic rollout did not complete')
        try:
            client.pump(timeout=1)
        except TimeoutError:
            pass
    if provider.error or provider.calls != 1:
        raise RuntimeError('private diagnostic provider failed: ' + str(provider.error))


def _bounded(client, method, params=None):
    # Cap fixture CLI execution without changing argv, authority or permissions.
    if method == 'command/exec':
        params = dict(params, timeoutMs=min(params['timeoutMs'], 30000))
    return client.request(method, params)


def _environment(home, temporary, profile):
    env = dict(os.environ)
    for key in tuple(env):
        upper = key.upper()
        if upper.startswith(('OPENAI_', 'MACHINE_LOCK_', 'GIT_', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                             'PM_KIT_') + host_prefixes(own=True)):
            env.pop(key)
    env.update(CODEX_HOME=str(home), USERPROFILE=str(home), HOME=str(home),
               TEMP=str(temporary), TMP=str(temporary), PM_KIT_ROOT=str(profile.kit_root),
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               PYTHONDONTWRITEBYTECODE='1', KIT_FAKE_GIT='0',
               NO_PROXY='127.0.0.1,localhost',
               PSModuleAnalysisCachePath=str(temporary / 'ps-cache'))
    if profile.release_identity is not None:
        env['PM_KIT_RELEASE_ID'] = profile.release_identity
    return env


def _git(cwd, args, env):
    from kitlib.git import cmd
    result = cmd.call(list(args), str(cwd), env=env)
    if result.rc != 0:
        raise RuntimeError('private Git fixture operation failed: ' + str(result.err))
    return cmd.text(result.out or b'').strip()


def _fixtures(directory, profile, env):
    main, origin = directory / 'repo', directory / 'origin.git'
    main.mkdir()
    _git(directory, ('init', '-q', '--bare', str(origin)), env)
    _git(main, ('init', '-q', '--initial-branch=main'), env)
    for key, value in (('user.name', 'Workflow Probe'), ('user.email', 'probe@example.invalid'),
                       ('commit.gpgsign', 'false'), ('core.hooksPath', str(directory / 'no-hooks')),
                       ('core.autocrlf', 'false'), ('gc.auto', '0')):
        _git(main, ('config', key, value), env)
    cfg = {'repo': {'name': 'native-workflow-fixture-' + directory.name[-12:],
                    'worktrees_by_host': {'codex': FIXTURE_FOLDER + '/worktrees'}},
           'landing': {'log_dir': FIXTURE_FOLDER + '/gate-logs'},
           'backlog': {'store': 'ref'}, 'doc_index': {'enabled': False},
           'review_pack': {'coauthor': True},
           'gate': {'steps': [{'name': 'diagnostic-fixture', 'always': True,
                              'cmd': ['{python}', '-I', '-B', '-c',
                                      'from pathlib import Path; assert Path("value.txt").read_text()=="after\\n"; print("fixture-ok")'],
                              'pass': '^fixture-ok$'}]}}
    for name, text in (('.kit.json', json.dumps(cfg)), ('.gitignore', FIXTURE_FOLDER + '/\n.codex/\n'),
                       ('AGENTS.md', 'Owned diagnostic instructions.\n'), ('value.txt', 'before\n')):
        (main / name).write_text(text, encoding='utf-8', newline='\n')
    _git(main, ('add', '--', '.kit.json', '.gitignore', 'AGENTS.md', 'value.txt'), env)
    _git(main, ('commit', '-q', '-m', 'private workflow fixture'), env)
    _git(main, ('remote', 'add', 'origin', str(origin)), env)
    _git(main, ('push', '-q', '-u', 'origin', 'main'), env)
    coordinator = main / FIXTURE_FOLDER / 'worktrees' / 'coordinator'
    _git(main, ('worktree', 'add', '-q', '-b', 'codex/coordinator', str(coordinator)), env)
    from kitlib.backlog.opener import open_store
    ctx = open_store(str(coordinator), session=SESSION)
    record = '\n'.join(('id: ' + ITEM, 'area: diagnostic', 'priority: 2', 'priority_by: agent',
                        'effort: -', 'state: open', 'blocked_on: -', 'scope: value.txt',
                        'summary: Owned no-inference acceptance fixture', 'session: ' + SESSION, ''))
    ctx.store.backend.publish(ctx.store.load(), {ITEM: record}, 'private acceptance fixture')
    return workflow.plan(str(coordinator), 'codex/probe', ITEM, SESSION, MODEL,
                         ('value.txt',), profile=profile), main, origin


def _start(stack, exe, directory, role, cwd, profile, env, extra_writes=(), extra_reads=()):
    home = directory / ('home-' + role)
    home.mkdir()
    brief = directory / 'brief.txt'
    local_env = dict(env, CODEX_HOME=str(home), USERPROFILE=str(home), HOME=str(home))
    planned = plan(role, cwd, 'diagnostic-' + role, str(brief), 'workflow_' + role.replace('-', '_'),
                   model=MODEL, profile=profile, codex_exe=exe,
                   store_write_roots=extra_writes, store_read_roots=extra_reads)
    config = 'default_permissions=' + json.dumps(planned.permission.profile_id) + '\n' + planned.permission.toml()
    (home / 'config.toml').write_text(config, encoding='utf-8', newline='\n')
    provider = _provider(stack)
    argv = list(planned.argv)
    for key, value in {'features.hooks': False, 'features.apps': False, 'features.plugins': False,
                       'windows.sandbox': 'unelevated', 'web_search': 'disabled',
                       'model_provider': 'pmkit_private',
                       'model_providers.pmkit_private.name': 'Owned no-inference workflow diagnostic',
                       'model_providers.pmkit_private.base_url': 'http://127.0.0.1:%d/v1' % provider.server_port,
                       'model_providers.pmkit_private.wire_api': 'responses',
                       'model_providers.pmkit_private.requires_openai_auth': False,
                       'model_providers.pmkit_private.supports_websockets': False,
                       'model_providers.pmkit_private.request_max_retries': 0,
                       'model_providers.pmkit_private.stream_max_retries': 0,
                       'features.enable_request_compression': False}.items():
        argv += ['--config', key + '=' + json.dumps(value)]
    events = []
    client = stack.enter_context(RuntimeAppServer(argv, cwd, environ=local_env, owned_job=True,
                         on_event=lambda method, data: events.append((method, data))))
    inspection = inspect_runtime(planned, client.request)
    if not inspection['permission_allowed']:
        raise RuntimeError('native role permission profile is unavailable')
    request = lambda method, params=None: _bounded(client, method, params)
    bindings = RoleBindings(request, profile)
    snapshot = client.request('thread/start', dict(planned.start_params, ephemeral=False,
                                                 modelProvider='pmkit_private'))
    binding = bind_started(planned, snapshot, bindings)
    _persist(client, binding, provider, events)
    host = KitOperationExecutor(request, bindings, profile).bind(binding.thread)
    return client, bindings, host


def _require_refusal(call, expected):
    try:
        call()
    except (ValueError, workflow.Stopped) as error:
        if expected not in str(error):
            raise RuntimeError('unexpected acceptance refusal: ' + str(error)) from error
        return expected
    raise RuntimeError('required acceptance refusal did not occur')


def _prepare_or_isolate(task, coordinator, env):
    """Preserve a native transport refusal; owner scaffolding cannot turn it green."""
    try:
        prepared = workflow.prepare(task, coordinator)
    except workflow.Stopped as error:
        if error.stage != 'new_worktree' or not all(text in str(error) for text in
                         ('fetch failed', 'CreateFileMapping', 'Win32 error 5')):
            raise
        from kitlib.git import refs
        if Path(task.worker).exists() or refs.resolve(task.branch, task.coordinator, known=True)[0] != '':
            raise RuntimeError('native worktree refusal left unexpected partial fixture state') from error
        # This is controlled setup for subsequent independent native operations.
        # The new-worktree/claim workflow remains failed, visibly in the report.
        _git(task.coordinator, ('worktree', 'add', '-q', '-b', task.branch, task.worker), env)
        from kitlib.backlog.opener import open_store
        from kitlib.backlog.plan import plan_pull
        ctx = open_store(task.coordinator, session=task.session, branch=task.branch)
        ctx.store.write(lambda snapshot: plan_pull(snapshot, ctx, task.item))
        workflow._checkout(task)
        workflow._claimed(task)
        return {'stage': 'owner_scaffolded_for_remaining_operations', 'native_prepare': False,
                'reason': 'Git for Windows local-transport sh.exe shared mapping denied (Win32 error 5)',
                'native_claim': 'not_reached'}
    _require_refusal(lambda: workflow.prepare(task, coordinator), 'exists')
    return dict(prepared, native_prepare=True, native_claim=True)


def _remove_result_folder(folder):
    """Best effort: remove the fixture's own per-repository result folder (result.result_dir),
    never the machine-wide folder above it. None, a missing folder or an error is ignored."""
    if not folder:
        return
    from kitlib.gate import result
    target = Path(folder)
    if target.parent.name != result.DIR_NAME or not target.name:
        return
    shutil.rmtree(target, ignore_errors=True)


def probe(codex_exe=None, profile=None, fixture_root=None):
    """Use real kit CLIs/native bindings; this gate checks only the tiny fixture."""
    profile = profile or discover()
    load_kit(profile)
    destination = fixture_directory(fixture_root)
    exe = str(native_codex(codex_exe))
    from kitlib.proc import spawn
    version = spawn.run([exe, '--version'], capture_output=True, text=True, timeout=10, shell=False)
    if version.returncode or not version.stdout.strip().startswith('codex-cli '):
        raise RuntimeError('native executable version could not be verified')
    directory = destination / ('pmkit-native-fixtures-' + uuid.uuid4().hex)
    directory.mkdir()
    result_folder = None
    try:
        temporary = directory / 'temp'
        temporary.mkdir()
        home = directory / 'controller-home'
        home.mkdir()
        env = _environment(home, temporary, profile)
        (directory / 'brief.txt').write_text('Owned native workflow diagnostic. No inference.', encoding='utf-8')
        task, main, origin = _fixtures(directory, profile, env)
        from kitlib.lock.store import Store
        lock_store = Store()
        lock_root = lock_store.root
        with ExitStack() as stack:
            _, _, coordinator = _start(stack, exe, directory, 'coordinator', task.coordinator,
                                        profile, env, (str(main / FIXTURE_FOLDER / 'worktrees'), str(origin), str(temporary)))
            prepared = _prepare_or_isolate(task, coordinator, env)
            _, _, worker = _start(stack, exe, directory, 'worker', task.worker, profile, env,
                                  (str(temporary),))
            _, _, tester = _start(stack, exe, directory, 'test-runner', task.worker, profile, env,
                                  (str(temporary), lock_root))
            _, _, reviewer = _start(stack, exe, directory, 'analyst', task.worker, profile, env,
                                    extra_reads=(lock_root,))
            if reviewer.binding.parent is not None:
                raise RuntimeError('reviewer must be a dedicated native root')
            raw_worker = SandboxCommandHost(worker.executor.request, worker.binding, timeout_ms=20000)
            raw_reviewer = SandboxCommandHost(reviewer.executor.request, reviewer.binding, timeout_ms=20000)
            (Path(task.worker) / '.codex').mkdir(exist_ok=True)
            protected = write_probe(raw_worker, Path(task.worker) / 'AGENTS.md', False, existing=True)
            protected_config = write_probe(raw_worker, Path(task.worker) / '.codex/write.txt', False)
            denied_review = write_probe(raw_reviewer, Path(task.worker) / 'review-write.txt', False)
            _require_refusal(lambda: reviewer.run_tool('commit', ('--model', MODEL, '-m', 'forbidden', 'value.txt')),
                             'read-only role')
            wrong = replace(task, model='invented-production-model')
            _require_refusal(lambda: workflow._run(wrong, 'commit', (), 'worker', task.worker, worker),
                             'native worker model')
            edit = raw_worker.run_argv((sys.executable, '-I', '-B', '-c',
                        'from pathlib import Path; Path("value.txt").write_text("after\\n")'), task.worker)
            if edit['exitCode'] != 0:
                raise RuntimeError('native assigned-worktree edit failed: ' + json.dumps(edit))
            workflow._run(task, 'commit', ('--model', MODEL, '-m', 'controlled native fixture change',
                          '-b', 'Closes: ' + ITEM, *task.paths), 'worker', task.worker, worker)
            tip = _git(task.worker, ('rev-parse', 'HEAD'), env)
            body = _git(task.worker, ('show', '-s', '--format=%B', tip), env)
            from .attribution import trailer
            if trailer(MODEL) not in body or 'Closes: ' + ITEM not in body:
                raise RuntimeError('actual diagnostic attribution/closing trailer was lost')
            _require_refusal(lambda: workflow.handoff(task, tip, reviewer), 'gate proof is missing')
            gated = workflow._run(task, 'gate', ('--base', task.base), 'test-runner', task.worker, tester)
            handoff = workflow.handoff(task, tip, reviewer)
            if _git(main, ('rev-parse', 'HEAD'), env) != task.base:
                raise RuntimeError('fixture main changed')
            from kitlib.records import gate_ok
            from kitlib.config import load
            from kitlib.gate.runner import gate_config
            record, why = gate_ok.verify(task.worker, tip, gate_config=gate_config(load(task.worker)))
            if not record or why or record.get('base') != task.base:
                raise RuntimeError('real fixture gate proof does not cover the exact tip/base')
            # The result folder is under the real machine dev_drive, which the canary shares with
            # its gate child (the PM-repo pointer is the registry's, not the fixture home's); the
            # fixture repo's key keeps it apart from every real repository's.
            from kitlib.gate import result
            result_folder = result.result_dir(task.worker)
            gate_result, error = result.read(task.worker, task.branch)
            if error or not gate_result or gate_result.get('verdict') != 'pass':
                raise RuntimeError('real gate result record is missing')
            step_log = Path(gate_result['log']) / 'diagnostic-fixture.log'
            step_output = step_log.read_text(encoding='utf-8')
            runs = [run for run in lock_store.history_records(1) if run.get('repo') == record['repo']]
            if len(runs) != 1 or Path(lock_store.path('active', runs[0]['id'] + '.json')).exists():
                raise RuntimeError('own native fixture gate booking did not end cleanly')
            return {'complete': prepared['native_prepare'], 'stage': handoff['stage'], 'base': task.base, 'tip': tip,
                    'native_model': worker.binding.model, 'inference': False, 'hooks_verified': False,
                    'native_version': version.stdout.strip(), 'protected_instructions': protected,
                    'protected_codex': protected_config, 'reviewer_write': denied_review,
                    'independent_review': 'not_performed', 'landing': 'disabled',
                    'gate_scope': 'tiny owned fixture; ordinary authoritative machine-lock booking',
                    'gate_output': gated['stdout'], 'gate_step_output': step_output,
                    'gate_record': {'commit': record['commit'], 'base': record['base'], 'steps': record['steps']},
                    'lock_run': {'id': runs[0]['id'], 'kind': runs[0]['kind'], 'ended': runs[0]['ended']},
                    'bridge': 'not_required', 'webpage': 'not_required',
                    'active_settings_changed': False, 'hook_trust_changed': False,
                    'preparation': prepared,
                    'native_remaining_operations': ['assigned edit', 'commit', 'gate', 'review_pack'],
                    'remaining_operations_verified': True}
    finally:
        try:
            _remove_result_folder(result_folder)
        finally:
            _remove_fixtures(directory, destination)
