"""The host contract (ARCHITECTURE.md "Host contract"; docs/multi-host-plan.md section 2.1).

What the core and a host adapter exchange, and nothing else: the neutral `Action` a hook payload
is parsed into, the `Decision` rendered back, a host's `HostProfile` and `HostCapabilities`, and
the protocols an adapter implements. Protocols and frozen data only. This package names no host
and imports nothing from a host folder, `hosts/` or `optional/` (tests/host scans every kitlib
import for that): kitlib never imports a host, the host is chosen at the edge.

  Action, Decision          one guarded event, and the verdict on it
  HostProfile               the host data the core reads (hosts.json "profile")
  Attribution               how its commits name it (hosts.json "profile".attribution)
  SessionTrailer            the session line its commits carry (attribution.session_trailer)
  HostCapabilities          what the host's hooks can carry (hosts.json "capabilities")
  degrade(decision, caps)   the one way a verdict the host cannot show is lowered
  HostAdapter, SessionHost, CommandHost   what an adapter supplies
  registry                  hosts.json read as data: names, selection, adapter module names
  hookmain                  the one hook runner: parse, kitlib.guard.decide, degrade, render, fail
                            closed; and the one place a hook command line is written
  header                    the kit header every session and subagent start adds to the context
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol

# `read`: a file tool that reads or searches (only hub-secrets judges it: the hub's token folder).
# `other`: a tool call no guard rule judges; the adapter still parses it, so a hook never has to
# guess a payload's kind before it knows the host.
KINDS = ('edit', 'shell', 'mcp', 'read', 'session_start', 'subagent_start', 'other')
SHELLS = ('bash', 'powershell')
VERDICTS = ('allow', 'deny', 'ask')


@dataclass(frozen=True)
class Action:
    """One event a hook judges, with every host's field names gone. `tool` is the host's own tool
    name (an MCP tool's full name, matched against HostProfile.mcp_tool_format), kept for messages;
    `path` an edit's target or a read's (the file read, or the folder a search starts from, None
    for `cwd`), `command` a shell's text, `cwd` where the session runs (None: the hook's own
    folder). `role` is PM_KIT_ROLE's value when the host hands it over (None: the guard
    reads its own environment). `agent` is an opaque subagent identity, None for the main session
    and for a host whose hooks carry none (HostCapabilities).

    What the guard needs that only a host can say, each optional so a host that lacks it degrades:
      agent_role   the subagent's role name in the kit's naming (the host's own prefixing gone),
                   None when the host does not say; read only when `agent` is set
      shell        a shell call's dialect, one of SHELLS
      background   the shell call runs in the background, so it does not hold the session
      workdir      a shell call's own working directory, for a host whose shell call names one
                   (the shell then runs there; the role policy holds it inside the checkout);
                   None: `cwd`
      retarget     the same edit aimed elsewhere, as the host's call text with `{path}` for the
                   path (an Instead line); '' when the host has no such form
      params       an MCP call's input object as the host passed it (None: not an MCP call, or a
                   host that does not pass it); the hub-browser rule reads its strings. For a
                   'read': None for a one-file read, else a search's terms: {'search': 'contents',
                   'filter': a file-name glob limiting the files it reads, None for all}. It takes
                   no part in equality or hashing
    """
    kind: str
    tool: str = ''
    path: Optional[str] = None
    command: Optional[str] = None
    cwd: Optional[str] = None
    role: Optional[str] = None
    agent: Optional[str] = None
    agent_role: Optional[str] = None
    shell: Optional[str] = None
    background: bool = False
    retarget: str = ''
    workdir: Optional[str] = None
    params: Optional[dict] = field(default=None, compare=False, hash=False)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError('unknown action kind: %r' % (self.kind,))
        if self.shell is not None and self.shell not in SHELLS:
            raise ValueError('unknown shell: %r' % (self.shell,))


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
class SessionTrailer:
    """The trailer naming the host session a commit was made in: `<key>: <url>`, `{id}` in `url`
    replaced by the value of the environment variable `env` (hosts.json attribution.session_trailer)."""
    key: str
    env: str
    url: str


@dataclass(frozen=True)
class Attribution:
    """How a host's agent commits are attributed: the trailer `Co-Authored-By: <name ...> <email>`.
    `model_env` the environment variable a model name falls back to (None: no fallback);
    `model_pattern` None for a free model name, given `<name> ` in front unless it already starts
    with it or is a co-author family, else a regex an explicit model id must match whole, always
    written `<name> <id>`. `session_trailer` a SessionTrailer, or None when the host has none."""
    name: str
    email: str
    model_env: Optional[str]
    model_pattern: Optional[str]
    session_trailer: Optional[SessionTrailer] = None


@dataclass(frozen=True)
class HostProfile:
    """The host data the core reads, each field with a consumer (plan 2.1): `owner_paths` the
    host's own instruction and settings paths, `worktrees_directory` the repo path its agent
    worktrees go in, `state_directory` its per-user folder name, `process_names` its agent process
    images per platform, ((platform, images), ...) (the lock's agent-host test reads the running
    platform's: registry.process_names), `mcp_tool_format` how it names an MCP tool, with
    {plugin}, {server} and {tool} fields, `session_env` the environment variable its session id
    is in (the lock shows it, never trusts it), `skills_directory` the repo path it reads a
    repository's own skills from, `attribution` its commits' Co-Authored-By (Attribution),
    `agent_name_format` how it names an agent a plugin bundles, with {plugin} and {name} fields
    ({name} last), `session_process` the process image a session it launches runs as (whose
    --remote-control value names the session), `env_prefixes` the environment variable prefixes
    its sessions own (a child launched on another host never inherits them;
    kitlib.sessionmark.child_environment), `cli_program` the name its CLI runs as on Linux and
    `cli_package` its npm package's scoped name (@scope/name), which tell a node process that runs
    the CLI from any other (kitlib.pmstart.runs_cli; kitlib.launcher.lookup and stop)."""
    owner_paths: tuple
    worktrees_directory: str
    state_directory: str
    process_names: tuple
    mcp_tool_format: str
    session_env: str
    skills_directory: str
    attribution: Attribution
    agent_name_format: str
    session_process: str
    env_prefixes: tuple
    cli_program: str
    cli_package: str


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
    """One per host, built by the host's own entry points and passed into the core. A supported host
    passes the conformance suite (tests/host/conformance.py; docs/new-host.md).

    capabilities                 its HostCapabilities (hosts.json), which hookmain degrades by
    parse(event_name, payload)   the host's hook payload as an Action, or a non-empty tuple of
                                 Actions when one call acts on several targets (hookmain judges
                                 each; any deny decides)
    screen(action, environ)      optional: the host's own policy over one Action, a Decision or
                                 None, judged before kitlib.guard (a deny or ask stands alone)
    render(event_name, decision) (stdout text, exit code) the host's hook reads back
    context(event_name, text)    (stdout text, exit code) of a session or subagent start hook that
                                 adds `text` to the agent's context (the kit header); ('', 0) for
                                 no text
    start_text(event_name, kit, payload, environ, part)   optional: the text one start hook adds
                                 to the agent's context, for a session started in payload's cwd
                                 with `environ`: the hook registered on that event with `part`
                                 (None: the plain one, the kit header first; a part it does not
                                 have raises). A check that fails is a line in it. boot/hook.py
                                 writes it (hookmain.dispatch); tools/agent_view.py shows it.
                                 Without it a start event gets the kit header and takes no part.
    start_parts(event_name, role)  optional: the parts, None for the plain hook, that a session of
                                 `role` runs at that event, every registration it loads (the
                                 host's manifest, a role's own settings); without it (None,)
    sessions()                   a SessionHost
    command_host()               a CommandHost, or None for a host without one
    content()                    an object whose renderer() is the host's ContentRenderer
                                 (kitlib.host.content: rendered agents, skills, manifests)
    launcher(), plugin_installer()   None where the host has none
    push(settings)               optional: the Action Hub's push-notification delivery over the
                                 launcher's settings (kitlib.hubsite.push: deliver, is_pm); without
                                 it the hub serves with none (docs/action-hub.md "Push notifications")
    """
    name: str
    profile: HostProfile
    capabilities: HostCapabilities

    def parse(self, event_name, payload): ...
    def render(self, event_name, decision) -> tuple: ...
    def context(self, event_name, text) -> tuple: ...
    def sessions(self) -> SessionHost: ...
    def command_host(self) -> Optional[CommandHost]: ...
    def content(self): ...
    def launcher(self): ...
    def plugin_installer(self): ...
