"""Credential-free native proof of kit commands and child-role metadata.

A private provider emits fixed calls, never inference. A throwaway repository and
CODEX_HOME contain all fixtures; active configuration, hook trust and daemons are untouched.
Hooks stay disabled: this proves command-broker binding, not installed patch-hook behavior.

From the Codex branch (pmkit_codex/command_canary.py): a native acceptance check run by hand against an installed codex.exe (hosts/codex/shell/scripts/command_canary.py); kept because it checks what the runtime broker relies on, on the real binary.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import shutil
import stat
import threading
import time
import uuid

from .sessions import native_codex
from .kit import discover, host_prefixes, load_kit
from .runtime import KitToolBroker, RoleBindings, RuntimeAppServer, SandboxCommandHost, tool_definition


def _handler():
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                self.connection.settimeout(5)
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 8 * 1024 * 1024 or self.headers.get('Authorization') or self.headers.get('Content-Encoding'):
                    raise ValueError('unexpected private-provider request')
                if self.path != '/v1/responses':
                    raise ValueError('unexpected endpoint')
                request = json.loads(self.rfile.read(size))
                model = request.get('model')
                routes = {}
                declarations = {}
                def inspect(specs, namespace=None):
                    for spec in specs:
                        if not isinstance(spec, dict):
                            continue
                        if spec.get('type') == 'namespace':
                            inspect(spec.get('tools', []), spec.get('name'))
                        elif isinstance(spec.get('name'), str):
                            routes[spec['name']] = namespace
                            declarations[spec['name']] = spec
                inspect(request.get('tools', []))
                def names(specs):
                    return [name for spec in specs if isinstance(spec, dict) for name in
                            (names(spec.get('tools', [])) if spec.get('type') == 'namespace' else [spec.get('name', spec.get('type'))])]
                with self.server.lock:
                    count = self.server.counts.get(model, 0)
                    self.server.counts[model] = count + 1
                    self.server.inventories[model] = names(request.get('tools', []))
                    if model == 'pmkit-root-probe':
                        self.server.spawn_specs = [s for s in request.get('tools', []) if s.get('name') == 'spawn_agent']
                    for item in request.get('input', []):
                        if isinstance(item, dict) and item.get('type') in {'function_call_output', 'custom_tool_call_output'}:
                            self.server.tool_outputs.append(item.get('output'))
                if model not in {'pmkit-root-probe', 'pmkit-child-probe'} or count > 3:
                    raise ValueError('unexpected model or extra diagnostic turn')
                if count == 0:
                    if model == 'pmkit-root-probe' and self.server.child_role is not None:
                        name, args = 'spawn_agent', {'agent_type': self.server.child_role,
                                                    'message': 'Run the private kit read once, then finish.'}
                        parameters = declarations.get(name, {}).get('parameters', {})
                        if 'task_name' in parameters.get('properties', {}):
                            args['task_name'] = 'private_probe'
                    else:
                        name, args = 'pmkit_run', {'tool': 'worktree_paths', 'args': ['.']}
                    output = [{'type': 'function_call', 'id': 'pmkit_' + model,
                               'call_id': 'pmkit_' + model, 'name': name, 'arguments': json.dumps(args)}]
                    if routes.get(name) is not None:
                        output[0]['namespace'] = routes[name]
                else:
                    output = [{'type': 'message', 'id': 'pmkit_done', 'role': 'assistant',
                               'content': [{'type': 'output_text', 'text': 'Private canary finished.'}]}]
                events = [{'type': 'response.output_item.done', 'output_index': index, 'item': item}
                          for index, item in enumerate(output)]
                events.append({'type': 'response.completed', 'response': {'id': 'pmkit_canary',
                              'status': 'completed', 'output': output,
                              'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})
                body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                self.server.error = '%s: %s' % (type(error).__name__, error)
                self.send_error(400, 'private canary stopped')
    return Provider


def _write_probe(host, cwd, marker, writing):
    try:
        result = host.run_argv((sys.executable, '-c',
            'from pathlib import Path; Path("private-write-probe.txt").write_text("probe")'), str(cwd))
    except RuntimeError as error:
        # This native Windows backend reports a policy denial as an RPC error,
        # rather than a command result. Only the expected negative probe accepts it.
        text = str(error)
        if writing or 'sandbox denied exec error' not in text or 'PermissionError' not in text:
            raise
        result = {'exitCode': 1, 'stdout': '', 'stderr': 'native sandbox denied the private write probe'}
    if writing != marker.exists() or (result['exitCode'] == 0) != writing:
        raise RuntimeError('native sandbox write behavior differs from its captured permissions')
    return 'allowed' if writing else 'denied'


def _scenario(exe, scratch, repositories, parent_role, child_role, profile, permissions_id=':read-only', expected_refusal=False):
    load_kit(profile)
    from kitlib.git import cmd
    base = scratch / (('direct-' + str(parent_role)) if child_role is None else child_role + permissions_id.replace(':', '-'))
    base.mkdir()
    main, home = repositories / base.name, base / 'home'
    main.mkdir()
    home.mkdir()
    env = dict(os.environ)
    for key in tuple(env):
        if key.upper().startswith(('OPENAI_', 'MACHINE_LOCK_', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
                                  + host_prefixes()):
            env.pop(key)
    env.update(CODEX_HOME=str(home), USERPROFILE=str(home), HOME=str(home),
               PM_KIT_ROOT=str(profile.kit_root), PYTHONDONTWRITEBYTECODE='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, NO_PROXY='127.0.0.1,localhost')
    for args in (['init', '-q', '--initial-branch=main'],
                 ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.invalid',
                  'commit', '-q', '--allow-empty', '-m', 'private fixture']):
        result = cmd.call(args, str(main), env=env)
        if result.rc != 0:
            raise RuntimeError('private Git fixture could not be created')
    worktree = main / '.codex' / 'worktrees' / 'probe'
    result = cmd.call(['worktree', 'add', '-q', '-b', 'codex/probe', str(worktree)], str(main), env=env)
    if result.rc != 0:
        raise RuntimeError('private linked worktree could not be created')
    agents = home / 'agents'
    agents.mkdir()
    if child_role is not None:
        (agents / (child_role + '.toml')).write_text(
            'name=' + json.dumps(child_role) + '\ndescription="Private canary role"\n'
            'model="pmkit-child-probe"\n'
            'default_permissions=' + json.dumps(':read-only' if child_role == 'kit-reviewer' else ':workspace') + '\n'
            'developer_instructions="Invoke pmkit_run with tool worktree_paths and args containing one dot once, then finish."\n',
            encoding='utf-8', newline='\n')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), _handler())
    provider.child_role, provider.counts, provider.lock, provider.error = child_role, {}, threading.Lock(), None
    provider.inventories = {}
    provider.spawn_specs, provider.tool_outputs = [], []
    serving = threading.Thread(target=lambda: provider.serve_forever(poll_interval=.1), daemon=True)
    serving.start()
    overrides = {'model_provider': 'pmkit_private', 'model': 'pmkit-root-probe',
                 'model_providers.pmkit_private.name': 'Private credential-free command canary',
                 'model_providers.pmkit_private.base_url': 'http://127.0.0.1:%d/v1' % provider.server_port,
                 'model_providers.pmkit_private.wire_api': 'responses',
                 'model_providers.pmkit_private.requires_openai_auth': False,
                 'model_providers.pmkit_private.supports_websockets': False,
                 'model_providers.pmkit_private.request_max_retries': 0,
                 'model_providers.pmkit_private.stream_max_retries': 0,
                 'features.enable_request_compression': False, 'features.shell_tool': False,
                 'features.multi_agent_v2': True, 'features.multi_agent_v2_dynamic_tools': True,
                 'features.hooks': False, 'features.apps': False, 'features.plugins': False,
                 'windows.sandbox': 'unelevated',
                 'web_search': 'disabled'}
    profile_lines = ['default_permissions="pmkit_read_probe"', '']
    for name, parent, access in (('pmkit_read_probe', ':read-only', 'read'),
                                 ('pmkit_write_probe', ':workspace', 'write')):
        profile_lines += ['[permissions.' + name + ']', 'extends=' + json.dumps(parent),
                          '[permissions.' + name + '.filesystem]', '":root"="read"',
                          json.dumps(str(worktree).replace('\\', '/')) + '=' + json.dumps(access),
                          json.dumps(str(main / '.git').replace('\\', '/')) + '=' + json.dumps(access), '']
    (home / 'config.toml').write_text('\n'.join(profile_lines), encoding='utf-8', newline='\n')
    selected_permissions = 'pmkit_read_probe' if permissions_id == ':read-only' else 'pmkit_write_probe'
    argv = [exe, '--no-daemon', 'app-server', '--listen', 'stdio://']
    for key, value in overrides.items():
        argv += ['--config', key + '=' + json.dumps(value)]
    observed, turns = [], []
    try:
        with RuntimeAppServer(argv, str(worktree), environ=env, on_event=lambda method, data: turns.append((method, data))) as client:
            bindings = RoleBindings(client.request, profile)
            broker = KitToolBroker(client.request, bindings, profile)
            def handle(method, params):
                result = broker.handle(method, params)
                observed.append({'result': result, 'thread': params.get('threadId')})
                return result
            client.on_request = handle
            client.request('permissionProfile/list', {'cwd': str(worktree)})
            start = client.request('thread/start', {'cwd': str(worktree), 'ephemeral': False,
                    'permissions': selected_permissions, 'model': 'pmkit-root-probe', 'modelProvider': 'pmkit_private',
                    'dynamicTools': [tool_definition()]})
            root = bindings.add_root(start, parent_role)
            filesystem_probe = SandboxCommandHost(client.request, root, timeout_ms=20000).run_argv(
                (sys.executable, '-c', 'import os,json; print(json.dumps({"cwd":os.getcwd(),"is_dir":os.path.isdir("."),"entries":os.listdir(".")}))'), str(worktree))
            if filesystem_probe['exitCode'] != 0:
                raise RuntimeError('native workspace filesystem is inaccessible: ' + json.dumps(filesystem_probe))
            client.request('turn/start', {'threadId': root.thread, 'input': [
                {'type': 'text', 'text': 'Private deterministic command/role canary. No inference.'}]})
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                completed = {data.get('threadId') for method, data in turns if method == 'turn/completed'}
                if observed and observed[-1]['thread'] in completed:
                    break
                if root.thread in completed and child_role is not None and not provider.counts.get('pmkit-child-probe') and not any(
                        method == 'thread/started' and (data.get('thread') or {}).get('parentThreadId') == root.thread
                        for method, data in turns):
                    break
                try:
                    client.pump(timeout=1)
                except TimeoutError:
                    pass
            if provider.error:
                raise RuntimeError(provider.error)
            if expected_refusal:
                if not observed or any(call['result']['success'] for call in observed):
                    raise RuntimeError('writable native reviewer was not refused')
                reason = observed[0]['result']['contentItems'][0]['text']
                if 'read-only kit role has writable native permissions' not in reason:
                    raise RuntimeError('native reviewer refused for an unexpected reason: ' + reason)
                return {'parent_role': parent_role, 'native_child': child_role,
                        'expected_refusal': True, 'reason': 'native reviewer inherited writable permissions',
                        'inference': False, 'hooks_verified': False}
            if not observed or any(not call['result']['success'] for call in observed):
                completed_items = [data for method, data in turns if method in {'item/completed', 'turn/completed'}]
                raise RuntimeError('native callback failed: ' + json.dumps({'calls': observed,
                    'models': provider.counts, 'tools': provider.inventories, 'events': completed_items,
                    'spawn_spec': provider.spawn_specs, 'tool_outputs': provider.tool_outputs,
                    'filesystem_probe': filesystem_probe, 'root_policy': json.loads(root.policy_json)}))
            binding = bindings.bindings[observed[0]['thread']]
            expected = parent_role if child_role is None else ('reviewer' if child_role == 'kit-reviewer' else 'worker')
            if binding.role != expected:
                raise RuntimeError('actual native role differs from the expected canary role')
            if binding.permission_profile != selected_permissions and child_role is None:
                raise RuntimeError('native permission profile differs from the requested profile')
            marker = worktree / 'private-write-probe.txt'
            writing = json.loads(binding.policy_json)['type'] == 'workspaceWrite'
            write_probe = _write_probe(SandboxCommandHost(client.request, binding, timeout_ms=20000),
                                       worktree, marker, writing)
            return {'parent_role': parent_role, 'native_child': child_role,
                    'bound_role': binding.role, 'sandbox': json.loads(binding.policy_json)['type'],
                    'kit_command': 'worktree_paths', 'exit_code': 0, 'inference': False,
                    'hooks_verified': False, 'write_probe': write_probe}
    finally:
        provider.shutdown()
        provider.server_close()
        serving.join(timeout=1)


def _remove_fixtures(repositories, destination):
    resolved = repositories.resolve()
    if resolved.parent != destination or not resolved.name.startswith('pmkit-native-fixtures-'):
        raise RuntimeError('private fixture cleanup target changed')

    def readonly(function, path, error):
        target = Path(path)
        if not target.resolve().is_relative_to(resolved):
            raise RuntimeError('fixture cleanup path escaped its reviewed directory')
        # Git object files carry the Windows read-only attribute. Only remove that
        # attribute in this throwaway fixture; do not modify any filesystem ACL.
        if not isinstance(error[1], PermissionError) or target.stat().st_mode & stat.S_IWRITE:
            raise error[1]
        target.chmod(stat.S_IWRITE)
        function(path)

    shutil.rmtree(resolved, onerror=readonly)


def probe(codex_exe=None, profile=None, fixture_root=None):
    profile = profile or discover()
    exe = native_codex(codex_exe)
    if fixture_root is None or not Path(fixture_root).is_absolute():
        raise ValueError('a reviewed absolute non-temp fixture directory is required')
    destination = Path(fixture_root).resolve()
    if destination.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('native TEMP write access would invalidate the read-only denial canary')
    destination.mkdir(parents=True, exist_ok=True)
    # On Windows Python's TemporaryDirectory applies an owner-only 0700 DACL.
    # A restricted token cannot traverse that parent, which would measure the fixture
    # ACL instead of the native permission profile. These contain public fake repos only.
    repositories = destination / ('pmkit-native-fixtures-' + uuid.uuid4().hex)
    repositories.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix='pmkit-command-role-canary-') as folder:
            scratch = Path(folder).resolve()
            if not repositories.is_relative_to(destination):
                raise ValueError('private repository fixtures redirected outside their reviewed directory')
            cases = (('worker', None, ':workspace', False),
                     ('worker', 'kit-reviewer', ':read-only', False),
                     (None, 'kit-worker', ':workspace', False),
                     ('worker', 'kit-reviewer', ':workspace', True),
                     ('reviewer', None, ':read-only', False))
            results = []
            for role, child, permissions, refusal in cases:
                try:
                    results.append(_scenario(exe, scratch, repositories, role, child, profile, permissions, refusal))
                except Exception as error:
                    raise RuntimeError('scenario %s -> %s (%s) failed after %d passed: %s' %
                                       (role, child, permissions, len(results), error)) from error
            return {'scenarios': results,
                    'active_settings_changed': False, 'hook_trust_changed': False, 'inference': False}
    finally:
        _remove_fixtures(repositories, destination)
