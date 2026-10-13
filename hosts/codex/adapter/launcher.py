"""Launch plans for a kit role as its own Codex session (ADAPTER.launcher(); from the Codex branch's
launcher.py and worktree_policy.py). A plan is data: execute() refuses, since a role's mark does not
reach its hooks through Codex's native agent definitions yet (README.md, "Not verified").

  plan(role, cwd, name, brief, model, effort, kit, classify, codex_exe) -> LaunchPlan
      `codex exec --json` in `cwd` with the role's sandbox (content/roles/<role>.md `sandbox`), its
      model (an explicit `model`, else its tier's when hosts.json names one; the account's
      otherwise), its reasoning effort, and its instructions (the rendered agent file's
      developer_instructions) as per-run --config overrides. Hooks on; the native shell tool off
      (the command broker in runtime.py runs kit tools). The lock MCP server is the plugin's
      read-only one. The session's environment carries PM_KIT_ROOT, PM_KIT_ROLE=<role> and
      PM_KIT_HOST=codex. The brief goes on stdin (Windows limits a command line), labelled.
      A write or test role needs an assigned linked worktree in the host's worktree folder
      (kitlib.worktree.hostdirs), a lander the main checkout. No persistent setting is written.
  execute(plan)               refuses
  launch_environment(plan, environ)   the session's environment: kitlib.sessionmark.child_environment
                              with the plan's variables
  require_host_worktree(cwd, place)   the assigned worktree, refused outside the host's folder
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys

from kitlib import sessionmark

from hosts.codex.adapter import content as codex_content
from hosts.codex.adapter.sessions import native_codex

HOST = 'codex'
NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
MAX_BRIEF = 50000


@dataclass(frozen=True)
class LaunchPlan:
    argv: tuple
    cwd: str
    environment: dict
    name: str
    role: str
    prompt: str


def kit_root():
    from kitlib import kitroot
    return kitroot.root()


def role_definition(role, kit=None):
    """(Role, its developer instructions on Codex) from content/roles under the kit."""
    from kitlib import content
    found = content.load(kit or kit_root())
    if role not in found.role_names():
        raise ValueError('unknown kit role: %s' % role)
    r = found.role(role)
    body = codex_content.substitute_body(r, found.role_names())
    return r, body


def require_host_worktree(cwd, place):
    from kitlib.config import load
    from kitlib.worktree import hostdirs
    from kitlib.worktree.new import main_checkout_of
    if not place.common:
        raise ValueError('cannot verify the main checkout for the worktree')
    main = Path(main_checkout_of(place.common)).resolve()
    expected = main / hostdirs.folder(load(str(main)), HOST)
    if expected.resolve() != expected:
        raise ValueError('the worktree folder must not redirect into another location')
    assigned = Path(cwd).resolve()
    if assigned == expected or expected not in assigned.parents:
        raise ValueError('a Codex role worktree must be under the host worktree folder: '
                         + str(expected).replace('\\', '/'))
    return assigned


def toml_value(value):
    """`value` as a TOML value for a --config override."""
    if isinstance(value, str):
        return codex_content.toml_string(value)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(v) for v in value) + ']'
    raise ValueError('unsupported TOML value')


def plan(role, cwd, name, brief, model=None, effort=None, kit=None, classify=None, codex_exe=None):
    from kitlib.git import repo
    kit = kit or kit_root()
    r, instructions = role_definition(role, kit)
    if not NAME.match(name or ''):
        raise ValueError('invalid session name')
    cwd, brief = Path(cwd).resolve(), Path(brief).resolve()
    if not cwd.is_dir() or not brief.is_file():
        raise ValueError('working directory or brief does not exist')
    place = (classify or repo.classify)(str(cwd))
    if place.kind not in (repo.MAIN, repo.LINKED):
        raise ValueError('a role session needs a known Git checkout')
    if place.kind == repo.LINKED:
        require_host_worktree(cwd, place)
    if r.mode in ('write', 'test') and place.kind != repo.LINKED:
        raise ValueError('implementation and test roles need an assigned linked worktree')
    if r.mode == 'lander' and place.kind != repo.MAIN:
        raise ValueError('a lander runs from the main checkout')
    body = brief.read_text(encoding='utf-8')
    if not body.strip() or len(body) > MAX_BRIEF:
        raise ValueError('a brief is non-empty and at most %d characters' % MAX_BRIEF)
    argv = [native_codex(codex_exe), '--no-daemon', 'exec', '--json', '--cd', str(cwd), '--sandbox', r.sandbox]
    chosen = model or r.model_for(HOST, codex_content.TIER_MODEL)
    if chosen != codex_content.ACCOUNT_DEFAULT:
        if not isinstance(chosen, str) or not chosen.strip() or any(c in chosen for c in '\r\n\x00'):
            raise ValueError('invalid model')
        argv += ['--model', chosen]
    effort = effort or r.effort or codex_content.TIER_EFFORT[r.tier]
    if effort not in EFFORTS:
        raise ValueError('invalid reasoning effort')
    shell = os.path.join(kit, *codex_content.SHELL_REL.split('/'))
    overrides = {'developer_instructions': instructions,
                 'model_reasoning_effort': effort,
                 'features.hooks': True,
                 'features.unified_exec': False,
                 # The tested build turns unified_exec back on; removing the shell tool is what
                 # takes exec_command and write_stdin away.
                 'features.shell_tool': False,
                 'shell_environment_policy.set.PM_KIT_ROOT': str(kit),
                 'shell_environment_policy.set.%s' % sessionmark.ENV: role,
                 'shell_environment_policy.set.PM_KIT_HOST': HOST,
                 'mcp_servers.pm_kit_lock.command': sys.executable,
                 'mcp_servers.pm_kit_lock.args': [os.path.join(shell, 'mcp', 'lock_server.py')],
                 'mcp_servers.pm_kit_lock.env_vars': ['PM_KIT_ROOT']}
    for key, value in overrides.items():
        argv += ['--config', key + '=' + toml_value(value)]
    argv += ['--', '-']
    prompt = 'Session label: %s. Role: %s. Assigned brief follows verbatim:\n%s' % (name, role, body)
    env = {'PM_KIT_ROOT': str(kit), sessionmark.ENV: role, 'PM_KIT_HOST': HOST}
    return LaunchPlan(tuple(argv), str(cwd), env, name, role, prompt)


def execute(planned, runner=None):
    raise ValueError('role execution is disabled: a Codex role\'s mark does not reach its hooks yet '
                     '(hosts/codex/README.md, "Not verified")')


def launch_environment(planned, environ):
    return sessionmark.child_environment(environ, HOST, planned.environment)
