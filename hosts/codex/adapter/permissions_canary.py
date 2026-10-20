"""Disposable native acceptance of reviewed controller permission fragments.

No model turn runs: commands use the native sandbox directly. Configuration and
Git writes are confined to public fake repositories and a private CODEX_HOME.
This does not verify trusted hooks or activate any installation.

From the Codex branch (pmkit_codex/permissions_canary.py): a native acceptance check run by hand against an installed codex.exe (hosts/codex/shell/scripts/permissions_canary.py); kept because it checks what the runtime broker relies on, on the real binary.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from .command_canary import _remove_fixtures
from .controller import bind_started, inspect_runtime, plan
from .sessions import native_codex
from .kit import discover, host_prefixes, load_kit
from .runtime import RoleBindings, RuntimeAppServer, SandboxCommandHost


DENIED_MARKER = 'PMKIT_PERMISSION_DENIED'
WRITTEN_MARKER = 'PMKIT_PERMISSION_WRITTEN'
READ_MARKER = 'PMKIT_CONTENTS_VERIFIED'
DENIED_EXIT = 73
WRITE_SCRIPT = '''from pathlib import Path
import sys
try:
    Path(sys.argv[1]).write_text("probe")
except PermissionError:
    print("PMKIT_PERMISSION_DENIED")
    sys.exit(73)
print("PMKIT_PERMISSION_WRITTEN")
'''
READ_SCRIPT = '''from pathlib import Path
import json, sys
for filename, expected in json.loads(sys.argv[1]):
    path = Path(filename)
    if path.name not in [entry.name for entry in path.parent.iterdir()]:
        raise ValueError("fixture enumeration failed")
    if path.read_text(encoding="utf-8") != expected:
        raise ValueError("fixture contents differ")
print("PMKIT_CONTENTS_VERIFIED")
'''


def fixture_directory(value):
    if value is None or not Path(value).is_absolute():
        raise ValueError('a reviewed absolute non-temp fixture directory is required')
    destination = Path(value).resolve()
    if destination.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('native TEMP access would invalidate permission denial probes')
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _command(host, script, *arguments):
    return host.run_argv((sys.executable, '-c', script, *map(str, arguments)), host.binding.cwd)


def write_probe(host, target, allowed, existing=False):
    """A denied RPC is proof only when it names a sandbox PermissionError."""
    target = Path(target)
    if target.exists() and not existing:
        raise ValueError('write probe target must not exist')
    before = target.read_bytes() if existing else None
    try:
        result = _command(host, WRITE_SCRIPT, target)
    except RuntimeError as error:
        if allowed or 'sandbox denied exec error' not in str(error) or 'PermissionError' not in str(error):
            raise
        result = {'exitCode': DENIED_EXIT, 'stdout': DENIED_MARKER, 'stderr': ''}
    changed = target.read_bytes() != before if existing else target.exists()
    expected_code, expected_marker = (0, WRITTEN_MARKER) if allowed else (DENIED_EXIT, DENIED_MARKER)
    if (changed != allowed or result.get('exitCode') != expected_code
            or result.get('stdout', '').strip() != expected_marker or result.get('stderr') != ''):
        raise RuntimeError('native permission write probe differs from reviewed access')
    return 'allowed' if allowed else 'denied'


def read_probe(host, files):
    """Prove enumeration and exact file contents, rather than metadata visibility."""
    expected = [(str(path), contents) for path, contents in files]
    result = _command(host, READ_SCRIPT, json.dumps(expected))
    if (result.get('exitCode') != 0 or result.get('stdout', '').strip() != READ_MARKER
            or result.get('stderr') != ''):
        raise RuntimeError('native assigned checkout/shared-kit content read failed')
    return 'enumerated and contents verified'


def _scenario(exe, directory, home, role, profile):
    from kitlib.git import cmd
    main = directory / role
    main.mkdir()
    env = dict(os.environ)
    for key in tuple(env):
        if key.upper().startswith(('OPENAI_', 'MACHINE_LOCK_', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
                                  + host_prefixes()):
            env.pop(key)
    env.update(CODEX_HOME=str(home), USERPROFILE=str(home), HOME=str(home),
               PM_KIT_ROOT=str(profile.kit_root), PYTHONDONTWRITEBYTECODE='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    for args in (['init', '-q', '--initial-branch=main'],
                 ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.invalid',
                  'commit', '-q', '--allow-empty', '-m', 'private permission fixture']):
        if cmd.call(args, str(main), env=env).rc != 0:
            raise RuntimeError('private repository fixture failed')
    cwd = main / '.codex/worktrees/probe'
    if cmd.call(['worktree', 'add', '-q', '-b', 'codex/probe', str(cwd)], str(main), env=env).rc != 0:
        raise RuntimeError('private linked worktree fixture failed')
    brief = cwd / 'brief.txt'
    brief.write_text('Private permission acceptance; no inference.', encoding='utf-8')
    (cwd / 'AGENTS.md').write_text('Private disposable permission fixture.', encoding='utf-8')
    (cwd / '.codex').mkdir()
    shared, temporary, outside = (directory / (role + '-' + name) for name in ('shared', 'temp', 'outside'))
    for path in (shared, temporary, outside):
        path.mkdir()
    read_files = []
    for label, path in (('checkout', cwd), ('common_git', main / '.git'),
                        ('shared_store', shared), ('literal_temp', temporary)):
        fixture = path / 'read-fixture.txt'
        contents = 'Private known contents: ' + role + '/' + label
        fixture.write_text(contents, encoding='utf-8')
        read_files.append((fixture, contents))
    kit_tool = profile.kit_root / 'tools/worktree_paths.py'
    read_files.append((kit_tool, kit_tool.read_text(encoding='utf-8')))
    planned = plan(role, cwd, 'permission-canary', brief, 'pmkit_permission_probe',
                   model='pmkit-permission-probe', profile=profile, codex_exe=exe,
                   store_read_roots=(shared,),
                   store_write_roots=(shared, temporary) if role == 'worker' else ())
    (home / 'config.toml').write_text('default_permissions="pmkit_permission_probe"\n' +
                                    planned.permission.toml(), encoding='utf-8', newline='\n')
    argv = [exe, '--no-daemon', 'app-server', '--listen', 'stdio://']
    for key, value in {'windows.sandbox': 'unelevated', 'features.hooks': False,
                       'features.apps': False, 'features.plugins': False,
                       'features.shell_tool': False, 'web_search': 'disabled'}.items():
        argv += ['--config', key + '=' + json.dumps(value)]
    with RuntimeAppServer(argv, str(cwd), environ=env) as client:
        inspected = inspect_runtime(planned, client.request)
        if not inspected['permission_allowed']:
            raise RuntimeError('native runtime rejected the controller permission profile')
        snapshot = client.request('thread/start', dict(planned.start_params, ephemeral=True))
        # Preserve native evidence even when its legacy projection cannot establish
        # equality. Fail-closed binding remains required for actual kit execution.
        try:
            binding = bind_started(planned, snapshot, RoleBindings(client.request, profile))
        except ValueError as error:
            return {'role': role, 'profile_accepted': True, 'binding_verified': False,
                    'reason': str(error), 'native_projection': snapshot.get('sandbox')}
        host = SandboxCommandHost(client.request, binding, timeout_ms=20000)
        reads = read_probe(host, read_files)
        kit_read = host.run_argv((sys.executable, str(profile.kit_root / 'tools/worktree_paths.py'),
                                  str(cwd)), binding.cwd)
        if kit_read['exitCode'] != 0:
            raise RuntimeError('existing kit worktree_paths command failed under reviewed native permissions')
        stdin = _command(host, 'import sys; print(repr(sys.stdin.read()))')
        if stdin['exitCode'] != 0 or stdin['stdout'].strip() != "''":
            raise RuntimeError('native stdin was not closed')
        probes = {}
        for label, target, allowed in (
                ('checkout', cwd / 'write.txt', role == 'worker'),
                ('common_git', main / '.git/probe.txt', role == 'worker'),
                ('shared_store', shared / 'probe.txt', role == 'worker'),
                ('literal_temp', temporary / 'probe.txt', role == 'worker'),
                ('outside', outside / 'probe.txt', False),
                ('protected_codex', cwd / '.codex/probe.txt', False)):
            probes[label] = write_probe(host, target, allowed)
        probes['protected_agents'] = write_probe(host, cwd / 'AGENTS.md', False, existing=True)
        return {'role': role, 'profile_accepted': True, 'binding_verified': True,
                'native_projection': snapshot['sandbox']['type'], 'reads': reads,
                'kit_command': 'worktree_paths', 'kit_exit_code': kit_read['exitCode'],
                'stdin': 'closed', 'writes': probes}


def probe(codex_exe=None, profile=None, fixture_root=None):
    profile = profile or discover()
    load_kit(profile)
    destination = fixture_directory(fixture_root)
    exe = str(native_codex(codex_exe))
    from kitlib.proc import spawn
    version = spawn.run([exe, '--version'], capture_output=True, text=True,
                        timeout=10, shell=False)
    if version.returncode != 0 or not version.stdout.strip().startswith('codex-cli '):
        raise RuntimeError('selected native executable version is unverified')
    directory = destination / ('pmkit-native-fixtures-' + uuid.uuid4().hex)
    directory.mkdir()  # inherit ACLs; private 0700 TEMP fixtures cannot measure the sandbox
    try:
        results = []
        for role in ('worker', 'analyst'):
            with tempfile.TemporaryDirectory(prefix='pmkit-permission-home-') as folder:
                results.append(_scenario(exe, directory, Path(folder), role, profile))
        return {'scenarios': results, 'inference': False, 'active_settings_changed': False,
                'hook_trust_changed': False, 'hooks_verified': False,
                'native_version': version.stdout.strip(),
                'complete': all(result['binding_verified'] for result in results)}
    finally:
        _remove_fixtures(directory, destination)
