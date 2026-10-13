"""Runtime-owned kit execution through sandboxed app-server command/exec (from the Codex branch's
runtime_host.py; hosts/codex/README.md "Runtime"). Codex's CommandHost.

Tool arguments never select a role, checkout, thread, environment or sandbox. Bindings
come from thread start/resume responses and child metadata read on the same RPC connection.
This adapter does not expose process/spawn, shellCommand, stdin writes or permission changes.

  RoleBindings          the controller's bound roots and their verified native children
  SandboxCommandHost    kitlib.host.CommandHost for one Binding: native command/exec under the
                        captured permissions, closed stdin, PM_KIT_ROLE the bound role and
                        PM_KIT_HOST codex; refused without an enabled Windows sandbox backend
  evaluate_bound(event, binding)   the guard on a command the broker runs for a binding: the
                        payload parsed as a hook's (events.parse), held by Codex's screen and
                        kitlib.guard with the BOUND role, and as a subagent when the binding is a
                        native child (its identity is native metadata the broker verified, not a
                        hook payload's); None for an allow, else Codex's deny JSON
  KitOperationExecutor, BoundKitHost, KitToolBroker   run one kit tool for a fresh-checked binding
  RuntimeAppServer      an owned app-server connection
"""
import copy
from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import subprocess

from .dispatch import TOOLS, execute_in_host, plan, _flag
from .kit import HOST, ROLE_ENV, catalog, child_environment, discover, load_kit, native_role


TOOL_NAME = 'pmkit_run'
METADATA_METHODS = frozenset({'thread/read', 'thread/list', 'thread/loaded/list',
                            'account/rateLimits/read', 'model/list', 'hooks/list',
                            'permissionProfile/list', 'configRequirements/read', 'config/read'})
CONTROLLER_METHODS = METADATA_METHODS | {'thread/start', 'thread/resume', 'turn/start',
                                       'turn/interrupt', 'command/exec'}
SESSION_ONLY = frozenset({'pm', 'coordinator', 'deep-worker', 'consultant', 'lander'})
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('invalid native thread/session identifier')
    return value


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('native checkout must be absolute')
    return str(Path(value).resolve())


def _key(value):
    return os.path.normcase(_path(value))


def _role(value):
    try:
        return native_role(value)
    except ValueError:
        raise ValueError('native child does not use a reviewed kit role') from None


def _deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def evaluate_bound(event, binding, ctx=None, profile=None, environ=None):
    """In-process broker guard. Only the freshly resolved native binding supplies the role."""
    try:
        from kitlib import guard
        from kitlib.host import registry
        from . import events, policy
        if binding.role not in catalog():
            raise ValueError('runtime binding has no reviewed kit role')
        if not isinstance(event, dict) or not isinstance(event.get('cwd'), str):
            raise ValueError('runtime guard event has no checkout')
        if os.path.normcase(os.path.realpath(event['cwd'])) != os.path.normcase(os.path.realpath(binding.cwd)):
            raise ValueError('runtime guard checkout differs from the resolved binding')
        parsed = events.parse('PreToolUse', event)
        child = binding.parent is not None
        env = dict(os.environ if environ is None else environ, **{ROLE_ENV: binding.role})
        for action in parsed if isinstance(parsed, tuple) else (parsed,):
            action = replace(action, role=binding.role, agent=binding.thread if child else None,
                             agent_role=binding.role if child else None)
            d = policy.screen(action, env)
            if d is None or d.verdict == 'allow':
                d = guard.decide(action, ctx, environ=env, profile=lambda: registry.host(HOST).profile)
            if d.verdict != 'allow':        # Codex has no ask: an ask is refused with its reason
                return _deny(d.reason)
        return None
    except Exception as error:
        return _deny('The Codex kit broker cannot clear this call (%s: %s). Report the event and error to '
                     'the PM. [kit guard: rule=host-runtime-error role=subagent]' % (type(error).__name__, error))


def _parent(thread):
    source = thread.get('source')
    spawn = source.get('subAgent', {}).get('thread_spawn') if isinstance(source, dict) else None
    if not isinstance(spawn, dict):
        raise ValueError('native child spawn provenance is unavailable')
    parent = _id(thread.get('parentThreadId'))
    if spawn.get('parent_thread_id') != parent or spawn.get('agent_role') != thread.get('agentRole'):
        raise ValueError('native child role/parent provenance is inconsistent')
    return parent


@dataclass(frozen=True)
class Binding:
    thread: str
    session: str
    cwd: str
    role: str | None
    model: str
    policy_json: str
    permission_profile: str | None
    parent: str | None


class RoleBindings:
    """Only the controller binds roots; children are verified against live native metadata."""
    def __init__(self, request, profile=None, classify=None):
        self.request, self.profile = request, profile or discover()
        from kitlib.git import repo
        self.classify = classify or repo.classify
        self.bindings = {}

    def _capture(self, snapshot, role, parent=None):
        if role is not None and role not in catalog():
            raise ValueError('unknown kit role')
        thread, policy = snapshot.get('thread'), snapshot.get('sandbox')
        if not isinstance(thread, dict) or not isinstance(policy, dict):
            raise ValueError('resolved native thread and sandbox are required')
        if policy.get('type') not in {'readOnly', 'workspaceWrite'}:
            raise ValueError('runtime kit commands require a native restricted sandbox')
        cwd = _path(snapshot.get('cwd'))
        if _key(thread.get('cwd')) != _key(cwd):
            raise ValueError('native thread/permission checkout mismatch')
        roots = snapshot.get('runtimeWorkspaceRoots')
        if not isinstance(roots, list) or len(roots) != 1 or _key(roots[0]) != _key(cwd):
            raise ValueError('exactly the assigned native workspace root is required')
        selected = snapshot.get('activePermissionProfile')
        permission = None
        if selected is not None:
            if not isinstance(selected, dict) or not isinstance(selected.get('id'), str) or not selected['id']:
                raise ValueError('invalid native permission profile')
            permission = selected['id']
        if role is not None:
            mode = catalog()[role]['mode']
            if catalog()[role]['sandbox'] == 'read-only' and policy['type'] != 'readOnly':
                raise ValueError('read-only kit role has writable native permissions')
            if mode in {'write', 'test'}:
                from kitlib.git import repo
                if self.classify(cwd).kind != repo.LINKED:
                    raise ValueError('writing/test roles require an assigned linked worktree')
        model = snapshot.get('model')
        if not isinstance(model, str) or not model:
            raise ValueError('native model identity is unavailable')
        return Binding(_id(thread.get('id')), _id(thread.get('sessionId')), cwd, role, model,
                       json.dumps(policy, sort_keys=True), permission, parent)

    def add_root(self, snapshot, role):
        thread = snapshot.get('thread') or {}
        if thread.get('parentThreadId') is not None or thread.get('agentRole') is not None:
            raise ValueError('controller root is a native child')
        binding = self._capture(snapshot, role)
        if binding.thread in self.bindings:
            raise ValueError('native root is already bound; rebind requires controller review')
        self.bindings[binding.thread] = binding
        return binding

    def resolve(self, thread_id):
        thread_id = _id(thread_id)
        value = self.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
        thread = value.get('thread') if isinstance(value, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != thread_id:
            raise ValueError('native thread identity is unavailable')
        have = self.bindings.get(thread_id)
        if have is not None:
            # thread/read omits effective permissions. Re-capture without overrides
            # before each operation; a later turn may have changed the native sandbox.
            snapshot = self.request('thread/resume', {'threadId': thread_id})
            refreshed = self._capture(snapshot, have.role, have.parent)
            if refreshed != have:
                raise ValueError('native permissions or identity changed; controller must review its binding')
            actual = snapshot.get('thread') or {}
            if have.parent is None:
                if actual.get('parentThreadId') is not None or actual.get('agentRole') is not None:
                    raise ValueError('bound controller root became a native child')
            elif _parent(actual) != have.parent or _role(actual.get('agentRole')) != have.role:
                raise ValueError('resolved native child role changed')
        else:
            parent = _parent(thread)
            if parent not in self.bindings:
                raise ValueError('native child has no bound controller parent')
            owner = self.bindings[parent]
            role = _role(thread.get('agentRole'))
            if role in SESSION_ONLY:
                raise ValueError('this kit role requires its own controller session')
            if owner.role not in {None, 'pm', 'coordinator'} and catalog()[role]['mode'] != 'read':
                raise ValueError('worker children may only take read-only kit roles')
            if thread.get('sessionId') != owner.session or _key(thread.get('cwd')) != _key(owner.cwd):
                raise ValueError('native child escaped its bound session/checkout')
            # A metadata name is insufficient to infer permissions. Resume without any
            # override to obtain this loaded child's actual resolved permission snapshot.
            snapshot = self.request('thread/resume', {'threadId': thread_id})
            actual = snapshot.get('thread') or {}
            if _parent(actual) != parent or actual.get('agentRole') != thread.get('agentRole'):
                raise ValueError('native child identity changed while capturing permissions')
            have = self._capture(snapshot, role, parent)
            if have.session != owner.session or _key(have.cwd) != _key(owner.cwd):
                raise ValueError('resolved child escaped its bound session/checkout')
            self.bindings[thread_id] = have
        if (thread.get('sessionId') != have.session or _key(thread.get('cwd')) != _key(have.cwd)
                or thread.get('model') != have.model):
            raise ValueError('native thread changed; controller must review its binding')
        if have.parent is not None:
            if _parent(thread) != have.parent or _role(thread.get('agentRole')) != have.role:
                raise ValueError('native child role changed')
        return have


def command_env(role):
    """The environment changes a sandboxed kit command runs with: the bound role and this host
    marked, the session name dropped, and every other host's model variable cleared, so a
    commit never takes its attribution from another host's session."""
    from kitlib import sessionmark
    from kitlib.host import registry
    env = {ROLE_ENV: role, 'PM_KIT_HOST': HOST, sessionmark.SESSION_NAME_ENV: None}
    for h in registry.load().values():
        if h.name != HOST and h.profile.attribution.model_env:
            env[h.profile.attribution.model_env] = None
    return env


class SandboxCommandHost:
    def __init__(self, request, binding, timeout_ms=600000):
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 3600000:
            raise ValueError('invalid runtime command timeout')
        self.request, self.binding, self.timeout_ms = request, binding, timeout_ms

    def run_argv(self, argv, cwd, stdin_closed=True):
        if stdin_closed is not True or _key(cwd) != _key(self.binding.cwd):
            raise ValueError('runtime commands require the bound checkout and closed stdin')
        config = self.request('config/read', {'cwd': self.binding.cwd, 'includeLayers': False})
        windows = (config.get('config') or {}).get('windows') if isinstance(config, dict) else None
        if not isinstance(windows, dict) or windows.get('sandbox') not in {'elevated', 'unelevated'}:
            raise ValueError('native Windows sandbox backend is not enabled; no command was executed')
        params = {'command': list(argv), 'cwd': self.binding.cwd, 'timeoutMs': self.timeout_ms,
                  'tty': False, 'streamStdin': False, 'streamStdoutStderr': False,
                  'env': command_env(self.binding.role)}
        if self.binding.permission_profile is not None:
            params['permissionProfile'] = self.binding.permission_profile
        else:
            params['sandboxPolicy'] = json.loads(self.binding.policy_json)
        result = self.request('command/exec', params)
        if (not isinstance(result, dict) or set(result) != {'exitCode', 'stdout', 'stderr'}
                or not isinstance(result.get('exitCode'), int)
                or isinstance(result['exitCode'], bool) or not all(isinstance(result.get(k), str) for k in ('stdout', 'stderr'))):
            raise RuntimeError('invalid sandboxed command result')
        return result


def tool_definition():
    return {'type': 'function', 'name': TOOL_NAME,
            'description': 'Run an existing PM kit operation in this assigned checkout under the actual native sandbox. No shell, stdin, role, checkout or permission overrides.',
            'inputSchema': {'type': 'object', 'properties': {
                'tool': {'type': 'string', 'enum': sorted(TOOLS)},
                'args': {'type': 'array', 'items': {'type': 'string'}}},
                'required': ['tool', 'args'], 'additionalProperties': False}}


class KitOperationExecutor:
    """Controller-owned execution shared by native callbacks and local workflows.

    A controller may bind a tool-only host to a real thread without inventing a
    dynamic tool callback. Every execution refreshes provenance and permissions,
    checks an expected binding when supplied, then applies the existing guard.
    """
    def __init__(self, request, bindings, profile=None, guard=evaluate_bound):
        self.request, self.bindings = request, bindings
        self.profile, self.guard = profile or bindings.profile, guard

    def bind(self, thread_id):
        return BoundKitHost(self, self.bindings.resolve(thread_id))

    def execute(self, thread_id, tool, args, *, expected=None):
        binding = self.bindings.resolve(thread_id)
        if expected is not None and binding != expected:
            raise ValueError('native binding changed since controller preparation')
        if binding.role is None:
            raise ValueError('unmarked owner root has no kit role; native kit children are resolved independently')
        if (not isinstance(tool, str) or tool not in TOOLS or not isinstance(args, (list, tuple))
                or not all(isinstance(arg, str) and '\x00' not in arg for arg in args)):
            raise ValueError('unknown kit tool or invalid argv')
        tail = tuple(args)
        if tool in {'commit', 'sync_base'} or (tool in {'ledger', 'pending'} and
                                               any(a in {'--commit', '--push'} for a in tail)):
            model = _flag(tail, '--model')
            if model is not None and model != binding.model:
                raise ValueError('model attribution differs from the bound native model')
            if model is None:
                if tool in {'ledger', 'pending'}:
                    tail = (tail[0], '--model', binding.model, *tail[1:])
                else:
                    tail = ('--model', binding.model, *tail)
        planned = plan(tool, tail, binding.cwd, role=binding.role, profile=self.profile)
        command = '& ' + ' '.join("'" + a.replace("'", "''") + "'" for a in planned.argv)
        decision = self.guard({'hook_event_name': 'PreToolUse', 'cwd': binding.cwd,
                'tool_name': 'PowerShell', 'tool_input': {'command': command, 'shell': 'powershell.exe'}},
                binding=binding, profile=self.profile)
        if decision is not None:
            raise ValueError(decision['hookSpecificOutput']['permissionDecisionReason'])
        return execute_in_host(planned, SandboxCommandHost(self.request, binding))


@dataclass(frozen=True)
class BoundKitHost:
    """A real controller binding with a fresh-checked tool entry, never raw argv."""
    executor: KitOperationExecutor
    binding: Binding

    def run_tool(self, tool, args):
        return self.executor.execute(self.binding.thread, tool, args, expected=self.binding)


class KitToolBroker:
    def __init__(self, request, bindings, profile=None, guard=evaluate_bound):
        self.request, self.bindings = request, bindings
        self.profile, self.guard = profile or bindings.profile, guard
        self.executor = KitOperationExecutor(request, bindings, self.profile, guard)
        self.calls = set()
        self.results = OrderedDict()

    def handle(self, method, params):
        if method != 'item/tool/call' or params.get('tool') != TOOL_NAME or params.get('namespace') not in (None, ''):
            raise ValueError('unsupported runtime server request; no approval granted')
        try:
            call = (_id(params.get('threadId')), _id(params.get('turnId')), _id(params.get('callId')))
            if call in self.calls:
                cached = self.results.get(call)
                if cached is None or cached[0] != params.get('arguments'):
                    raise ValueError('replayed runtime call changed or its outcome expired')
                return copy.deepcopy(cached[1])
            if len(self.calls) >= 4096:
                raise ValueError('runtime call limit reached; controller must create a new connection')
            self.calls.add(call)
            args = params.get('arguments')
            if not isinstance(args, dict) or set(args) != {'tool', 'args'}:
                raise ValueError('tool arguments cannot override runtime identity or permissions')
            if (not isinstance(args['tool'], str) or args['tool'] not in TOOLS
                    or not isinstance(args['args'], list)
                    or not all(isinstance(arg, str) and '\x00' not in arg for arg in args['args'])):
                raise ValueError('unknown kit tool or invalid argv')
            result = self.executor.execute(call[0], args['tool'], args['args'])
            reply = {'success': result['exitCode'] == 0,
                     'contentItems': [{'type': 'inputText', 'text': json.dumps(result)}]}
        except Exception as error:
            reply = {'success': False, 'contentItems': [{'type': 'inputText', 'text': 'PM kit runtime refused: %s' % error}]}
        if 'call' in locals() and call not in self.results:
            self.results[call] = (copy.deepcopy(params.get('arguments')), copy.deepcopy(reply))
            if len(self.results) > 64:
                self.results.popitem(last=False)
        return reply


class RuntimeAppServer:
    """An explicitly selected owned runtime connection; no global daemon or settings changes."""
    def __init__(self, argv, cwd, environ=None, timeout=30, on_event=None, owned_job=False):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise ValueError('runtime metadata timeout must be between 0 and 60 seconds')
        self.argv, self.cwd = tuple(argv), _path(cwd)
        self.environ = child_environment(os.environ if environ is None else environ)
        self.timeout, self.on_event = timeout, on_event
        self.process, self.rpc, self.on_request = None, None, None
        if type(owned_job) is not bool:
            raise ValueError('owned runtime job selection must be explicit')
        self.owned_job, self.job = owned_job, None
        self.cleanup_errors = ()

    def __enter__(self):
        from kitlib.proc import spawn
        from .sessions import _version
        from kitlib.hostrpc import Client
        try:
            if self.owned_job:
                from kitlib.proc.job import Job
                self.job = Job(mask=None, kill_on_close=True)
                start = self.job.start
            else:
                start = spawn.popen
            self.process = start(list(self.argv), cwd=self.cwd, env=self.environ,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', bufsize=1, shell=False)
            self.rpc = Client(self.process.stdout, self.process.stdin, timeout=self.timeout,
                              on_event=self.on_event, on_request=self._server_request)
            self.rpc.request('initialize', {'clientInfo': {'name': 'pmkit_runtime_adapter', 'version': _version()},
                                          'capabilities': {'experimentalApi': True}})
            self.rpc.send({'method': 'initialized'})
            return self
        except BaseException:
            self.close()
            raise

    def _server_request(self, method, params):
        if self.on_request is None:
            raise ValueError('runtime request has no reviewed handler')
        return self.on_request(method, params)

    def request(self, method, params=None):
        if method not in CONTROLLER_METHODS or self.rpc is None:
            raise ValueError('runtime method is unavailable')
        timeout = 3610 if method == 'command/exec' else self.timeout
        return self.rpc.request(method, params, timeout=timeout)

    def pump(self, timeout=1):
        return self.rpc.pump(timeout)

    def close(self):
        if self.process is None and self.job is None:
            return self.cleanup_errors
        process, errors = self.process, []
        if process is not None:
            try:
                process.stdin.close()
            except Exception as error:
                errors.append('stdin close: ' + str(error))
            for operation in (None, process.terminate, process.kill):
                try:
                    if operation is not None:
                        operation()
                    process.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    if operation == process.kill:
                        errors.append('owned runtime did not exit after kill')
                except Exception as error:
                    errors.append('owned runtime cleanup: ' + str(error))
        if self.job is not None:
            try:
                # Native plugin discovery can leave children holding stdout and
                # temporary pack files after its parent exits. This job contains
                # only the selected owned process tree, never existing sessions.
                self.job.end(kill=True)
            except Exception as error:
                errors.append('owned runtime job cleanup: ' + str(error))
            finally:
                self.job = None
        self.process = None
        if self.rpc is not None:
            self.rpc.reader.join(timeout=1)
            if self.rpc.reader.is_alive():
                errors.append('owned runtime reader remains open')
        self.cleanup_errors = tuple(errors)
        return self.cleanup_errors

    def __exit__(self, exc_type, *_):
        errors = self.close()
        if errors and exc_type is None:
            raise RuntimeError('owned runtime cleanup failed: ' + '; '.join(errors))
