"""The host contract (ARCHITECTURE.md "Host contract"; docs/multi-host-plan.md section 2.1).

What the core and a host adapter exchange, and nothing else: the neutral `Action` a hook payload
is parsed into, the `Decision` rendered back, a host's `HostProfile` and `HostCapabilities`, and
the protocols an adapter implements. Protocols and frozen data only. This package names no host
and imports nothing from a host folder, `hosts/` or `optional/` (tests/host scans every kitlib
import for that): kitlib never imports a host, the host is chosen at the edge.

  Action, Decision          one guarded event, and the verdict on it
  HostProfile               the host data the core reads (hosts.json "profile")
  HostCapabilities          what the host's hooks can carry (hosts.json "capabilities")
  degrade(decision, caps)   the one way a verdict the host cannot show is lowered
  HostAdapter, SessionHost, CommandHost   what an adapter supplies
  registry                  hosts.json read as data: names, selection, adapter module names
"""
from dataclasses import dataclass
from typing import Optional, Protocol

KINDS = ('edit', 'shell', 'mcp', 'session_start', 'subagent_start')
VERDICTS = ('allow', 'deny', 'ask')


@dataclass(frozen=True)
class Action:
    """One event a hook judges, with every host's field names gone. `tool` is the host's own tool
    name, kept for messages only; `path` an edit's target, `command` a shell's text, `cwd` where
    it runs. `role` is PM_KIT_ROLE's value (None: unmarked). `agent` is an opaque subagent identity,
    None for the main session and for a host whose hooks carry none (HostCapabilities)."""
    kind: str
    tool: str = ''
    path: Optional[str] = None
    command: Optional[str] = None
    cwd: Optional[str] = None
    role: Optional[str] = None
    agent: Optional[str] = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError('unknown action kind: %r' % (self.kind,))


@dataclass(frozen=True)
class Decision:
    """The verdict on an Action: `rule` names the guard rule that decided, `reason` says why to the
    agent, `instead` is the exact command to use (empty: none)."""
    verdict: str
    rule: str = ''
    reason: str = ''
    instead: str = ''

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise ValueError('unknown verdict: %r' % (self.verdict,))


@dataclass(frozen=True)
class HostProfile:
    """The host data the core reads, each field with a consumer (plan 2.1): `owner_paths` the
    host's own instruction and settings paths, `worktrees_directory` the repo path its agent
    worktrees go in, `state_directory` its per-user folder name, `process_names` its agent process
    images (the lock's agent-host test), `mcp_tool_format` how it names an MCP tool, with
    {plugin}, {server} and {tool} fields."""
    owner_paths: tuple
    worktrees_directory: str
    state_directory: str
    process_names: tuple
    mcp_tool_format: str


@dataclass(frozen=True)
class HostCapabilities:
    """What a host's hooks can carry, declared as data so the core degrades a missing one the same
    way for every host instead of each adapter improvising. `hook_events` the host's own event
    names the kit hooks; `agent_format` the file format of its agent definitions."""
    hook_events: tuple
    supports_ask: bool
    subagent_identity_in_hooks: bool
    mcp: bool
    skills: bool
    agent_format: str


def degrade(decision, capabilities):
    """`decision` as a host with `capabilities` can show it: `ask` on a host with no ask becomes
    deny, its reason kept. One rule for every host, so a refusal never turns into an allow."""
    if decision.verdict == 'ask' and not capabilities.supports_ask:
        return Decision('deny', decision.rule, decision.reason, decision.instead)
    return decision


class SessionHost(Protocol):
    """A host's session metadata, read-only. Delivery and process liveness are the runtime's."""
    def sessions(self, cwd=None, max_pages=10): ...
    def loaded(self): ...
    def usage(self): ...
    def models(self): ...


class CommandHost(Protocol):
    """Runs a command inside the host session's own sandbox and approvals: literal argv, stdin
    closed, no interactive handle. It refuses when the session's permission context is
    unavailable; there is no unsandboxed fallback."""
    def run_argv(self, argv, cwd, stdin_closed=True): ...


class HostAdapter(Protocol):
    """One per host, built by the host's own entry points and passed into the core.

    parse(event_name, payload)   the host's hook payload as an Action
    render(event_name, decision) (stdout text, exit code) the host's hook reads back
    sessions()                   a SessionHost
    command_host()               a CommandHost, or None for a host without one
    content()                    the host's content renderer (rendered agents, skills, instructions)
    launcher(), plugin_installer()   None where the host has none
    """
    name: str
    profile: HostProfile

    def parse(self, event_name, payload) -> Action: ...
    def render(self, event_name, decision) -> tuple: ...
    def sessions(self) -> SessionHost: ...
    def command_host(self) -> Optional[CommandHost]: ...
    def content(self): ...
    def launcher(self): ...
    def plugin_installer(self): ...
