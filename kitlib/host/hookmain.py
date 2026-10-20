"""The one hook runner (multi-host plan section 2.2), behind the one hook entry of every host,
boot/hook.py (plan decision 20). What every host's hook needs besides its own parse and render
lives here once:

  dispatch(adapter, event_name, kit, text, stdout, entry, args, part, environ, started)
        boot/hook.py's work once the kit has booted and the adapter is imported. A start event
        (adapter.CONTEXT_EVENTS): the context text, adapter.start_text(event_name, kit, payload,
        environ, part) when the adapter has one, else the kit header (kitlib.host.header; no part
        then), written through adapter.context; exit 0. Any other event: run() over the payload
        `text`, the adapter's DEADLINE (None when it has none) for the whole call from `started`.
        `entry` and `args` are the hook's own command line, for the reproduce line of a deny.
  run(adapter, event_name, stdin, stdout, environ, entry, deadline, started, args)
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
        entry's arguments, named in the reproduce line after `entry`.
  failure(e, role, entry, args)       that guard-error Decision
  command_line(script, executable)
        {"command", "args"} that starts `script` with no console window: the one place a hook
        command line is rendered (plan section 6, rule 4). Today: pythonw.exe beside `executable`
        (default this interpreter) when there is one, else `executable` itself.
  manifest_command(script, platforms, *args)
        {"command", "args"} for a hook manifest another machine runs (a plugin's hooks.json): the
        windowless interpreter by name for the host's hosts.json `platforms` (INTERPRETERS), found
        on that machine's PATH, then `script` and `args` as given. The same on every machine that
        renders it, so a committed manifest has no drift.
  settings_entry(event_name, matcher, script, executable, timeout, args)
        {event_name: [{"matcher", "hooks": [{"type": "command", "command", "args"[, "timeout"]}]}]}:
        a settings.json hooks entry in exec form (no shell), `script` then `args`. boot/hook.py
        <host> --settings-entry prints the adapter's, and kitlib.machinesetup installs what that
        prints.

The adapter is kitlib.host.HostAdapter. Its parse may return one Action or a tuple of them, for a
host whose one tool call acts on several targets (one patch that adds, deletes and renames files):
each is judged, and the first deny decides, else the first ask, else allow (carrying every
warning a warn-mode rule gave, kitlib.guard.modes, for the adapter to render). An empty tuple is
refused (fail closed). run() also reads three optional attributes:
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


def run(adapter, event_name, stdin, stdout, environ=None, entry=None, deadline=None, started=None, args=()):
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
    if text:
        stdout.write(text)
    return code


def dispatch(adapter, event_name, kit, text, stdout, entry=None, args=(), part=None, environ=None, started=None):
    if event_name in getattr(adapter, 'CONTEXT_EVENTS', ()):
        try:
            payload = json.loads(text or '{}')
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        start = getattr(adapter, 'start_text', None)
        if start is not None:
            body = start(event_name, kit, payload, environ, part)
        elif part is None:
            from kitlib.host import header
            body = header.text(kit)
        else:
            raise ValueError('%s has no start check %r' % (adapter.name, part))
        stdout.write(adapter.context(event_name, body)[0])
        return 0
    if part is not None:
        raise ValueError('%s takes no part (%r)' % (event_name, part))
    return run(adapter, event_name, io.StringIO(text), stdout, environ, entry, getattr(adapter, 'DEADLINE', None),
               started, args)


def windowless(executable=None):
    """The pythonw.exe beside `executable` (default: this interpreter), else `executable` itself."""
    exe = executable or sys.executable
    w = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    return w if os.name == 'nt' and os.path.isfile(exe) and os.path.isfile(w) else exe


def command_line(script, executable=None):
    return {'command': windowless(executable).replace(os.sep, '/'), 'args': [script.replace(os.sep, '/')]}


# The windowless Python interpreter, by name, per hosts.json platform (plan section 6, rule 4).
INTERPRETERS = {'windows': 'pythonw'}


def manifest_command(script, platforms, *args):
    names = sorted(set(INTERPRETERS.get(p, '') for p in platforms))
    if len(names) != 1 or not names[0]:
        raise ValueError('no one windowless interpreter for the platforms %s' % ', '.join(platforms))
    return {'command': names[0], 'args': [script.replace(os.sep, '/')] + list(args)}


def settings_entry(event_name, matcher, script, executable=None, timeout=None, args=()):
    hook = {'type': 'command'}
    hook.update(command_line(script, executable))
    hook['args'] += list(args)
    if timeout is not None:
        hook['timeout'] = timeout
    return {event_name: [{'matcher': matcher, 'hooks': [hook]}]}
