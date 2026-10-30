"""The one hook runner (multi-host plan section 2.2), behind the one hook entry of every host,
boot/hook.py (plan decision 20). What every host's hook needs besides its own parse and render
lives here once:

  dispatch(adapter, event_name, kit, text, stdout, entry, args, part, environ, started)
        boot/hook.py's work once the kit has booted and the adapter is imported. A start event
        (adapter.CONTEXT_EVENTS): the context text, adapter.start_text(event_name, kit, payload,
        environ, part) when the adapter has one, else the kit header (kitlib.host.header; no part
        then), written through adapter.context; exit 0. The header part (no part) also records
        which kit folder the header named (kitlib.host.headerfollow.note); at a SubagentStart that
        folder, and the one the context text names, is the current export
        (kitlib.host.headerfollow.start_kit), not the one the session began on. Any other event: run()
        over the payload `text`, the adapter's DEADLINE (None when it has none) for the whole call
        from `started`, with `kit`. `entry` and `args` are the hook's own command line, for the
        reproduce line of a deny.
  run(adapter, event_name, stdin, stdout, environ, entry, deadline, started, args, kit)
        the hook: stdin's JSON -> adapter.parse -> kitlib.guard.decide -> degrade -> adapter.render
        -> stdout; returns the exit code render gives. Fails closed: any exception on the way (a
        kitlib.guard that will not import, a payload parse refuses, a rule that raises) is a deny,
        rule guard-error, naming the error and the command that reproduces it, rendered by the
        adapter like any other. An error writing the answer is raised to the entry, which exits
        with the host's blocking code (2 on Claude Code), since there is nothing left to say it with.
        `deadline` (seconds; None: none) bounds decide, every Action of the call together: a guard
        still deciding then is a deny, rule guard-timeout, so a host that gives up on a slow hook
        never reads silence as an allow. `started` (a time.monotonic() reading; None: now) is when
        that budget began, for an entry whose own work before run (a boot) counts against it; the
        timeout reason names the whole budget. On a host whose hooks carry no subagent identity, a call
        in a marked session is judged as an agent of its mark's role (unidentified()). `args`: the
        entry's arguments, named in the reproduce line after `entry`. With `kit` (the kit the hook
        booted), the header-update line of a session whose header named an older plugin export
        (kitlib.host.headerfollow.line: at once when the folder the header named is gone, else at
        most once an hour) goes with the verdict whatever it is, through
        the adapter's optional tool_context(event_name, text, rendered) (none: no line), so a
        reason keeps its format; the record is rewritten only after the answer is written. A deny,
        whatever made it, is then logged before run returns: one line in
        <pm_data>/audit/refusals.jsonl (kitlib.guard.refusal_log; docs/guard-v2.md "Refusal log"),
        with the role of the Action the guard judged. The line never changes the answer or the
        exit code; the host reads the answer when the hook exits, so the log adds its time (no
        process, at most refusal_log.BOUND seconds of lock wait plus file reads) to the hook's.
  failure(e, role, entry, args)       that guard-error Decision
  ready(guard)
        raises PlatformUnsupported when the running platform is not one kitlib.guard declares
        ready (READY_PLATFORMS): run() checks it once the guard imports, before any Action is
        judged, so every guarded call on such a platform is the guard-error deny.
  command_line(script, executable)
        {"command", "args"} that starts `script` with no console window: the one place a hook
        command line is rendered (plan section 6, rule 4). Today: pythonw.exe beside `executable`
        (default this interpreter) when there is one, else `executable` itself.
  manifest_command(script, platform, *args, blocking=False)
        {"command", "args"} for a hook manifest another machine runs (a plugin's hooks.json, one
        per platform): the interpreter by name for `platform` (INTERPRETERS), found on that
        machine's PATH, then `script` and `args` as given. The same on every machine that renders
        it, so a committed manifest has no drift. Windows: pythonw in exec form. Linux: /bin/sh -c
        runs python3 with the script and args as its own arguments, so a missing or failing
        python3 still answers as boot/hook.py does: a tool event (`blocking`) exits 2, the
        guard-error deny; a start event prints "PM kit: the kit did not load: ..." and exits 0.
        A platform with no interpreter raises ValueError.
  mcp_interpreter(platform)
        the interpreter, by name, that a plugin manifest's MCP servers run on `platform`
        (MCP_INTERPRETERS): python on Windows, python3 on Linux. ValueError for any other.
  settings_entry(event_name, matcher, script, executable, timeout, args, platform)
        {event_name: [{"matcher", "hooks": [{"type": "command", "command", "args"[, "timeout"]
        [, "onFailure"]}]}]}: a settings.json hooks entry in exec form (no shell), `script` then
        `args`; "onFailure" per `platform` (default the running one, ON_FAILURE). boot/hook.py
        <host> --settings-entry prints the adapter's, and kitlib.machinesetup installs what that
        prints.

The adapter is kitlib.host.HostAdapter. Its parse may return one Action or a tuple of them, for a
host whose one tool call acts on several targets (one patch that adds, deletes and renames files):
each is judged, and the first deny decides, else the first ask, else allow (carrying every
warning a warn-mode rule gave, kitlib.guard.modes, for the adapter to render). An empty tuple is
refused (fail closed). run() also reads four optional attributes:
`tool_context(event_name, text, rendered)` (the stdout text that is `rendered`, the adapter's own
answer to the verdict, with `text` added to the agent's context, for the header-update line of any
verdict; no tool_context: that host gets no line),
`capabilities` (HostCapabilities: a verdict the host cannot show is lowered by kitlib.host.degrade),
`agent(payload)` (the subagent a payload names, read even when parse refuses it, so a guard-error
deny still carries the right role tag) and `screen(action, environ)` (the host's own policy, a
Decision or None, judged before kitlib.guard.decide for each Action; a deny or an ask from it
stands without the guard). Only the standard library and kitlib.host are imported at the top: the
guard is imported inside run(), so its failure to import is a deny too.
"""
import dataclasses
import io
import json
import os
import sys
import threading
import time

from kitlib.host import Decision, degrade

ERROR = 'guard-error'
TIMEOUT = 'guard-timeout'
TAG = '[kit guard: rule=%s role=%s]'


def reproduce(entry, args=()):
    """The command that runs the hook `entry` with `args` on a saved event."""
    return 'python "%s"%s < event.json' % (entry, ''.join(' ' + a for a in args))


def failure(e, role, entry, args=()):
    """The deny for a guard that failed with `e`, in a session of `role` ('main' or 'subagent');
    `entry` is the hook script whose run with `args` reproduces it."""
    instead = reproduce(entry, args)
    return Decision('deny', ERROR, 'The kit guard failed (%s: %s), so it cannot clear this call. Reproduce it '
                    'with the event JSON this call sent and report the error to the PM; do not reword the call. '
                    'Instead: %s %s' % (type(e).__name__, e, instead, TAG % (ERROR, role)), instead)


def ready(guard):
    """Raise PlatformUnsupported unless the running platform (kitlib.platform.NAME, read now) is
    one `guard` declares ready (kitlib.guard.READY_PLATFORMS): run() turns it into the guard-error
    deny, so no guarded call goes ahead on a platform whose path semantics the guard has not been
    proven on (docs/linux-support-plan.md section 3)."""
    from kitlib import platform
    if platform.NAME not in guard.READY_PLATFORMS:
        raise platform.PlatformUnsupported(
            'the kit guard is not declared ready on this platform (%s; ready: %s)'
            % (platform.NAME, ', '.join(guard.READY_PLATFORMS)))


def _role(adapter, payload, action):
    if isinstance(action, tuple):
        action = action[0] if action else None
    agent = action.agent if action is not None else None
    if action is None and hasattr(adapter, 'agent'):
        try:
            agent = adapter.agent(payload)
        except Exception:                       # the payload is what failed: tag it as main
            agent = None
    return 'subagent' if agent else 'main'


def _decide(guard, action, environ, profile, deadline):
    if deadline is None:
        return guard.decide(action, environ=environ, profile=profile)
    box = {}

    def work():
        try:
            box['d'] = guard.decide(action, environ=environ, profile=profile)
        except BaseException as e:              # handed to the caller's fail-closed path
            box['e'] = e
    t = threading.Thread(target=work, name='kit-guard', daemon=True)
    t.start()
    t.join(deadline)
    if 'e' in box:
        raise box['e']
    if 'd' not in box:
        return None
    return box['d']


# The identity a host without subagent identity in its hooks gives a marked session's call
# (unidentified): the guard then holds it as an agent of its mark's role, never as the owner's or
# a lead's own main session, since a child there runs under its parent's mark.
UNIDENTIFIED = 'unidentified'
JUDGED_AS_AGENT = ('edit', 'shell', 'mcp')


def unidentified(adapter, action, environ):
    """`action` as the guard must judge it on `adapter`'s host. A host whose hooks carry no
    subagent identity (HostCapabilities.subagent_identity_in_hooks false) cannot tell a marked
    session from a child running under its mark, so an edit, shell or MCP call in a session the
    launcher marked (kitlib.sessionmark) is judged as an agent of the mark's role: agent
    UNIDENTIFIED, agent_role the mark. The owner's unmarked session, a host with identity, and an
    Action that already names its agent are unchanged."""
    caps = getattr(adapter, 'capabilities', None)
    if caps is None or caps.subagent_identity_in_hooks or action.agent or action.kind not in JUDGED_AS_AGENT:
        return action
    from kitlib import sessionmark
    mark = sessionmark.mark(environ)
    if mark is None:
        return action
    return dataclasses.replace(action, agent=UNIDENTIFIED, agent_role=mark)


def _judge(adapter, guard, actions, environ, deadline, started=None):
    """The Decision on every Action of one call: the host's screen, then the guard, for each; the
    first deny decides, else the first ask, else allow. `deadline` (seconds; None: none) is the
    budget for the whole call, every Action's guard together. None when the guard was still
    deciding when it ran out. `started`: run's."""
    screen = getattr(adapter, 'screen', None)
    end = None if deadline is None else (time.monotonic() if started is None else started) + deadline
    found = []
    for action in actions:
        action = unidentified(adapter, action, environ)
        d = screen(action, environ) if screen is not None else None
        if d is None or d.verdict == 'allow':
            left = None if end is None else max(0.0, end - time.monotonic())
            d = _decide(guard, action, environ, lambda: adapter.profile, left)
            if d is None:
                return None
        if d.verdict == 'deny':
            return d
        found.append(d)
    ask = next((d for d in found if d.verdict == 'ask'), None)
    if ask is not None:
        return ask
    warned = [d for d in found if d.reason]     # an allow with a reason: a warning (kitlib.guard.modes)
    if not warned:
        return found[0]
    reasons = []
    for d in warned:
        if d.reason not in reasons:
            reasons.append(d.reason)
    return Decision('allow', warned[0].rule, ' '.join(reasons))


def _agent(adapter, payload):
    """The agent the adapter reads in `payload`, else None (no agent(), or it raised)."""
    try:
        return adapter.agent(payload) if hasattr(adapter, 'agent') else None
    except Exception:
        return None


def _notice(adapter, event_name, kit, payload, environ, rendered):
    """(stdout text, settle): the header-update line (kitlib.host.headerfollow.line) added to the
    rendered answer through the adapter's tool_context, for any verdict, so the deny and ask reasons
    keep their format. `settle()` rewrites the session's record: the caller runs it once the text is
    written, so a line that never reached the agent is offered again. An adapter without
    tool_context gets no line, and its record is not read: it cannot show one. Never raises and
    never changes the verdict: the line is advice, so any failure is no line."""
    try:
        tell = getattr(adapter, 'tool_context', None)
        if tell is None or not kit or not isinstance(payload, dict):
            return rendered, None
        from kitlib.host import headerfollow
        agent = _agent(adapter, payload)
        line = headerfollow.line(kit, payload, agent, environ, write=False)
        if not line:
            return rendered, None
        out = tell(event_name, line, rendered)
        if not out:
            return rendered, None
        return out, lambda: headerfollow.line(kit, payload, agent, environ)
    except Exception:
        return rendered, None


def run(adapter, event_name, stdin, stdout, environ=None, entry=None, deadline=None, started=None, args=(),
        kit=None):
    environ = os.environ if environ is None else environ
    entry = (entry or sys.argv[0] or 'hook').replace(os.sep, '/')
    payload = action = None
    try:
        payload = json.loads(stdin.read() or '{}')
        action = adapter.parse(event_name, payload)
        actions = action if isinstance(action, tuple) else (action,)
        if not actions:
            raise ValueError('the payload parsed into no action')
        from kitlib import guard
        ready(guard)
        decision = _judge(adapter, guard, actions, environ, deadline, started)
        if decision is None:                    # still deciding at the deadline
            role = _role(adapter, payload, action)
            decision = Decision('deny', TIMEOUT, 'The kit guard did not decide within %s s, so it cannot clear '
                                'this call. Report it to the PM with the event JSON this call sent. Instead: '
                                '%s %s' % (deadline, reproduce(entry, args), TAG % (TIMEOUT, role)),
                                reproduce(entry, args))
        if decision.verdict == 'ask':           # only an ask can need lowering: read the host's data then
            caps = getattr(adapter, 'capabilities', None)
            if caps is not None:
                decision = degrade(decision, caps)
    except Exception as e:                      # fail closed, every tool
        decision = failure(e, _role(adapter, payload, action), entry, args)
    text, code = adapter.render(event_name, decision)
    text, settle = _notice(adapter, event_name, kit, payload, environ, text)
    if text:
        stdout.write(text)
    if settle is not None:
        try:
            settle()
        except Exception:
            pass
    if decision.verdict == 'deny':
        _log(adapter, payload, action, decision, environ)
    return code


def _log(adapter, payload, action, decision, environ):
    """The refusal log's line for the deny run() answered (kitlib.guard.refusal_log), once the
    answer is written: the call's first Action as the guard judged it (unidentified()), and its
    role, or None and the payload's role when parse refused the payload. Never raises: the line is
    a record, not part of the answer."""
    try:
        from kitlib.guard import refusal_log
        first = action[0] if isinstance(action, tuple) and action else action
        if first is not None and not isinstance(first, tuple):
            first = unidentified(adapter, first, environ)
        else:
            first = None
        refusal_log.record(decision, first, getattr(adapter, 'name', None), _role(adapter, payload, first),
                           environ)
    except Exception:
        pass


def dispatch(adapter, event_name, kit, text, stdout, entry=None, args=(), part=None, environ=None, started=None):
    if event_name in getattr(adapter, 'CONTEXT_EVENTS', ()):
        try:
            payload = json.loads(text or '{}')
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        if part is None and event_name == 'SubagentStart':
            try:
                from kitlib.host import headerfollow
                kit = headerfollow.start_kit(kit, environ)
            except Exception:                   # advice too: the session's own folder stands
                pass
        start = getattr(adapter, 'start_text', None)
        if start is not None:
            body = start(event_name, kit, payload, environ, part)
        elif part is None:
            from kitlib.host import header
            body = header.text(kit)
        else:
            raise ValueError('%s has no start check %r' % (adapter.name, part))
        if part is None:                        # the header part: record the folder it names
            try:
                from kitlib.host import headerfollow
                headerfollow.note(kit, payload, _agent(adapter, payload), event_name)
            except Exception:                   # the record is advice: never fail a start for it
                pass
        stdout.write(adapter.context(event_name, body)[0])
        return 0
    if part is not None:
        raise ValueError('%s takes no part (%r)' % (event_name, part))
    return run(adapter, event_name, io.StringIO(text), stdout, environ, entry, getattr(adapter, 'DEADLINE', None),
               started, args, kit)


def windowless(executable=None):
    """The pythonw.exe beside `executable` (default: this interpreter), else `executable` itself."""
    exe = executable or sys.executable
    w = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    return w if os.name == 'nt' and os.path.isfile(exe) and os.path.isfile(w) else exe


def command_line(script, executable=None):
    return {'command': windowless(executable).replace(os.sep, '/'), 'args': [script.replace(os.sep, '/')]}


# The hook's Python interpreter, by name, per hosts.json platform: windowless pythonw on Windows
# (plan section 6, rule 4), python3 on Linux (run by SHELL, manifest_command).
INTERPRETERS = {'windows': 'pythonw', 'linux': 'python3'}
SHELL = '/bin/sh'
SHELL_NAME = 'kit-hook'
TOOL_BODY = '%s "$@" || exit 2'
START_BODY = '%s "$@" || echo "PM kit: the kit did not load: %s failed or is not on PATH (exit $?)"'


# The MCP servers' Python interpreter, by name, per hosts.json platform (mcp_interpreter): python
# on Windows, python3 on Linux, where stock Ubuntu has no `python`.
MCP_INTERPRETERS = {'windows': 'python', 'linux': 'python3'}


def mcp_interpreter(platform):
    name = MCP_INTERPRETERS.get(platform)
    if not name:
        raise ValueError('no MCP server interpreter for the platform %r' % (platform,))
    return name


def manifest_command(script, platform, *args, blocking=False):
    name = INTERPRETERS.get(platform)
    if not name:
        raise ValueError('no hook interpreter for the platform %r' % (platform,))
    argv = [script.replace(os.sep, '/')] + list(args)
    if platform == 'windows':
        return {'command': name, 'args': argv}
    body = TOOL_BODY % name if blocking else START_BODY % (name, name)
    return {'command': SHELL, 'args': ['-c', body, SHELL_NAME] + argv}


# What Claude Code does with a settings hook that fails (cannot start, times out, exits neither 0 nor
# 2), per running platform: on Linux it blocks the call (code.claude.com/docs/en/hooks, "Block the
# action when a hook fails"; Claude Code 2.1.295 or later), so a gone interpreter is no unguarded
# call. Windows: unset (the host's default, the call goes ahead), its entry unchanged.
ON_FAILURE = {'linux': 'block'}


def settings_entry(event_name, matcher, script, executable=None, timeout=None, args=(), platform=None):
    hook = {'type': 'command'}
    hook.update(command_line(script, executable))
    hook['args'] += list(args)
    if timeout is not None:
        hook['timeout'] = timeout
    if platform is None:
        from kitlib import platform as running
        platform = running.NAME
    if platform in ON_FAILURE:
        hook['onFailure'] = ON_FAILURE[platform]
    return {event_name: [{'matcher': matcher, 'hooks': [hook]}]}
