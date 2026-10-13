"""Review-only native controller and permission plans; no installation or launch (from the Codex
branch's controller.py: the role comes from content/roles, its instructions from the rendered agent).

The host prepares native configuration and binds resolved responses. Existing kit tools
remain the operation implementation. Runtime metadata checks are read-only and do not
grant approval, modify trust or start a thread.
"""
import copy
from dataclasses import dataclass
import os
from pathlib import Path
import re

from .kit import catalog, discover, instructions, load_kit
from .launcher import plan as assignment_plan, toml_value
from .runtime import tool_definition


PROFILE_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
ACTIVATION_BLOCKERS = (
    'Trusted native patch/rename hooks and authenticated child-to-hook identity are unverified.',
    'Shared-store access and machine-lock nesting need installed native acceptance.',
    'Task/review/landing/cleanup acceptance is incomplete; landing remains disabled.',
)


def _absolute(value):
    if not isinstance(value, (str, Path)):
        raise ValueError('controller access roots must be literal absolute paths')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('controller access roots must be absolute')
    return str(path.resolve())


def _unique(values):
    result, seen = [], set()
    for value in values:
        path = _absolute(value)
        key = os.path.normcase(path)
        if key not in seen:
            result.append(path)
            seen.add(key)
    return tuple(result)


def _grants(values):
    """Normalize equivalent recursive grants, removing redundant descendant roots."""
    paths = {os.path.normcase(path) for path in _unique(values)}
    result = set(paths)
    for path in paths:
        for other in paths - {path}:
            try:
                if os.path.commonpath((path, other)) == other:
                    result.discard(path)
                    break
            except ValueError:             # roots on different Windows drives
                pass
    return result


def _git_roots(place, cwd):
    """Use Git's resolved administration paths, refusing unknown or outside roots."""
    from kitlib.git import repo
    if (place.kind not in (repo.MAIN, repo.LINKED) or not place.top
            or not repo.same_path(place.top, cwd)):
        raise ValueError('controller cannot resolve the assigned Git checkout')
    common = _absolute(place.common)
    git_dir = _absolute(place.git_dir)
    if not Path(common).is_dir() or not Path(git_dir).is_dir():
        raise ValueError('controller Git administration directories are unavailable')
    if place.kind == repo.MAIN:
        valid = repo.same_path(git_dir, common)
    else:
        valid = not repo.same_path(git_dir, common) and repo.inside(git_dir, common)
    if not valid:
        raise ValueError('controller Git directory is outside its known common root')
    return common, git_dir


@dataclass(frozen=True)
class PermissionPlan:
    profile_id: str
    extends: str
    read_roots: tuple
    write_roots: tuple

    def toml(self):
        """A fragment for owner review, never written into active configuration."""
        lines = ['[permissions.' + self.profile_id + ']', 'extends=' + toml_value(self.extends),
                 '[permissions.' + self.profile_id + '.filesystem]']
        if self.extends == ':workspace':
            # Avoid unreviewed native temp grants. Owner-approved literal temp
            # subdirectories may be listed explicitly among write_roots.
            # Retain inherited reads: denying TEMP reads makes the profile a
            # restricted-read policy that the unelevated Windows backend cannot
            # enforce. Read overrides remove implicit writes without that change.
            lines += ['":tmpdir"="read"', '":slash_tmp"="read"']
        # :read-only/:workspace retain native read defaults. Explicit paths show
        # kit/Git dependencies; this proposal does not claim lock/store access works.
        writes = {os.path.normcase(path) for path in self.write_roots}
        for path in _unique((*self.read_roots, *self.write_roots)):
            lines.append(toml_value(path.replace('\\', '/')) + '=' +
                         toml_value('write' if os.path.normcase(path) in writes else 'read'))
        lines += ['[permissions.' + self.profile_id + '.network]', 'enabled=false']
        return '\n'.join(lines) + '\n'


@dataclass(frozen=True)
class ControllerPlan:
    argv: tuple
    cwd: str
    environment: dict
    role: str
    name: str
    prompt: str
    permission: PermissionPlan
    start_params: dict
    blockers: tuple = ACTIVATION_BLOCKERS


def plan(role, cwd, name, brief, permission_profile, *, model=None, effort=None,
         profile=None, classify=None, codex_exe=None, store_read_roots=(), store_write_roots=()):
    """Prepare a dedicated root, including for reviewers of writable workers.

    Permission roots are controller/owner review inputs, not model tool arguments.
    Store paths must come from the reviewed live kit configuration; no default
    store is guessed and no configuration is installed here.
    """
    if not isinstance(permission_profile, str) or not PROFILE_ID.fullmatch(permission_profile):
        raise ValueError('a reviewed named permission profile id is required')
    profile = profile or discover()
    load_kit(profile)
    assignment = assignment_plan(role, cwd, name, brief, model=model, effort=effort,
            kit=str(profile.kit_root), classify=classify, codex_exe=codex_exe)
    from kitlib.git import repo
    place = (classify or repo.classify)(assignment.cwd)
    if not place.common:
        raise ValueError('controller cannot resolve the Git common directory')
    common, git_dir = _git_roots(place, assignment.cwd)
    definition = catalog()[role]
    writing = definition['mode'] not in {'read', 'lander'}
    if not writing and store_write_roots:
        raise ValueError('read-only and lander plans cannot add writable stores')
    # Owner instructions and runtime configuration stay read-only even beneath
    # a writable assigned checkout. Native :workspace protects .codex itself;
    # name AGENTS explicitly because it is the Codex owner's instruction path.
    # The native workspace backend implicitly protects Git metadata. A broad
    # common-directory grant alone does not override the assigned linked gitdir
    # protection. Preserve this exact literal entry in the emitted profile.
    reads = _unique((assignment.cwd, str(profile.kit_root), common, git_dir,
                     str(Path(assignment.cwd) / 'AGENTS.md'),
                     str(Path(assignment.cwd) / '.codex'), *store_read_roots))
    writes = _unique((assignment.cwd, common, git_dir, *store_write_roots)) if writing else ()
    permission = PermissionPlan(permission_profile, ':workspace' if writing else ':read-only', reads, writes)
    overrides = {'features.shell_tool': False, 'features.unified_exec': False,
                 'features.multi_agent_v2': True, 'features.multi_agent_v2_dynamic_tools': True,
                 'features.hooks': True}
    argv = [assignment.argv[0], '--no-daemon', 'app-server', '--listen', 'stdio://']
    for key, value in overrides.items():
        argv += ['--config', key + '=' + toml_value(value)]
    params = {'cwd': assignment.cwd, 'ephemeral': False, 'permissions': permission_profile,
              'developerInstructions': instructions(role), 'dynamicTools': [tool_definition()],
              'config': {'model_reasoning_effort': effort or definition['effort']}}
    if model is not None:
        params['model'] = model
    return ControllerPlan(tuple(argv), assignment.cwd, dict(assignment.environment), role, name,
                          assignment.prompt, permission, params)


def inspect_runtime(planned, request):
    """Read effective backend, requirements and selected-profile availability."""
    blockers = list(planned.blockers)
    config = request('config/read', {'cwd': planned.cwd, 'includeLayers': False})
    if not isinstance(config, dict) or not isinstance(config.get('config'), dict):
        raise ValueError('invalid native config/read response: config must be an object')
    windows = config['config'].get('windows')
    if not isinstance(windows, dict) or windows.get('sandbox') not in {'elevated', 'unelevated'}:
        blockers.append('Native Windows sandbox backend is not enabled.')
    seen, found, cursor = set(), None, None
    for _ in range(32):
        params = {'cwd': planned.cwd, 'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        page = request('permissionProfile/list', params)
        if not isinstance(page, dict) or not isinstance(page.get('data'), list):
            raise ValueError('invalid native permission-profile list')
        for entry in page['data']:
            if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or type(entry.get('allowed')) is not bool:
                raise ValueError('invalid native permission-profile entry')
            if entry['id'] == planned.permission.profile_id:
                if found is not None:
                    raise ValueError('duplicate selected native permission profile')
                found = entry['allowed']
        cursor = page.get('nextCursor')
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError('invalid native permission-profile pagination')
        seen.add(cursor)
    else:
        raise ValueError('native permission-profile pagination exceeds controller limit')
    if found is not True:
        blockers.append('Selected native permission profile is missing or forbidden by effective requirements.')
    requirements = request('configRequirements/read', {})
    if not isinstance(requirements, dict):
        raise ValueError('invalid native effective requirements')
    return {'ready': False, 'permission_allowed': found is True,
            'requirements': copy.deepcopy(requirements), 'blockers': blockers}


def bind_started(planned, snapshot, bindings):
    """Check the actual dedicated-root response before the existing registry binds it.

    Used by private acceptance controllers; this does not start/resume a session.
    A selected profile name does not replace the registry's effective-policy check.
    """
    if not isinstance(snapshot, dict):
        raise ValueError('invalid native root response')
    selected = snapshot.get('activePermissionProfile')
    if not isinstance(selected, dict) or selected.get('id') != planned.permission.profile_id:
        raise ValueError('native permission profile differs from the reviewed controller plan')
    if os.path.normcase(_absolute(snapshot.get('cwd', ''))) != os.path.normcase(planned.cwd):
        raise ValueError('native root checkout differs from the controller plan')
    expected_model = planned.start_params.get('model')
    if expected_model is not None and snapshot.get('model') != expected_model:
        raise ValueError('native root model differs from the controller plan')
    policy = snapshot.get('sandbox')
    if not isinstance(policy, dict):
        raise ValueError('resolved native sandbox is required for permission-plan comparison')
    if policy.get('networkAccess', False) is not False:
        raise ValueError('native network grant differs from the reviewed restricted permission plan')
    if planned.permission.extends == ':read-only':
        if policy.get('type') != 'readOnly':
            raise ValueError('read-only kit role has writable native permissions')
        if set(policy) - {'type', 'networkAccess'}:
            raise ValueError('native read-only permission equivalence cannot be established')
    else:
        if policy.get('type') != 'workspaceWrite':
            raise ValueError('native writable permission type differs from the reviewed controller plan')
        if set(policy) - {'type', 'networkAccess', 'writableRoots', 'excludeTmpdirEnvVar', 'excludeSlashTmp'}:
            raise ValueError('native writable permission equivalence cannot be established')
        # Native workspaceWrite also grants cwd and, unless excluded, temporary
        # locations. The thread snapshot does not identify the resolved TMPDIR
        # path. Never guess it using the controller's own environment. Literal
        # temp roots can be reviewed as ordinary write roots; implicit allowances
        # must be explicitly excluded before this comparison can establish equality.
        if policy.get('excludeTmpdirEnvVar') is not True or policy.get('excludeSlashTmp') is not True:
            raise ValueError('native implicit temporary write grants cannot be matched to the reviewed plan; '
                             'explicit temp exclusion and reviewed literal roots are required')
        roots = policy.get('writableRoots', [])
        if not isinstance(roots, list):
            raise ValueError('native writableRoots must be an array of absolute paths')
        if _grants((planned.cwd, *roots)) != _grants(planned.permission.write_roots):
            raise ValueError('native writable grants differ from the reviewed permission plan')
    return bindings.add_root(snapshot, planned.role)


def execute(planned):
    raise ValueError('native controller execution is disabled: ' + '; '.join(planned.blockers))
