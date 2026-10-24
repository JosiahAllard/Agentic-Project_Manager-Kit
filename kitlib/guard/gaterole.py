"""Who may lift a branch's gate-run budget (docs/guard.md "Gate allow").

  gate-allow   a run of tools/gate.py allow (kitlib/gate/budget.py `allow`: it raises the gate
               runs a branch may make; docs/gate.md "The branch gate budget") by any session but
               the owner's own or a main session marked coordinator (or its old mark, MARKS) or
               pm. Every subagent is refused, whatever role its payload names, and so is any
               other marked main session (deep-worker, consultant, plain).

A gate.py run is read as rules.tool_run reads a kit tool: the script in any path form, python
-m tools.gate, python -m runpy tools.gate, a Start-Process -ArgumentList string. Its subcommand
is the first word after the script, as tools/gate.py `main` reads it, so `-C <dir> allow` (a
usage error at the tool) and text that only names the command pass. A computed first word is not
read (the guard catches honest mistakes, not evasion). It needs no switch in .kit.json.

A host with no subagent identity (kitlib.host.hookmain.UNIDENTIFIED, Codex) gives a marked
session's every call, its children's too, agent UNIDENTIFIED and agent_role the mark: such a call
is judged by that mark, so a coordinator's session there may allow, and so may a child under it.
"""
from kitlib import agentname

from . import rules

RULE = 'gate-allow'
UNIDENTIFIED = 'unidentified'                   # kitlib.host.hookmain.UNIDENTIFIED
SCRIPTS = ('gate.py', 'gate')                   # commands.wrapper's names for the gate
VERB = 'allow'
# the main sessions that may allow more runs: the coordinator (and its old mark) and the PM
MARKS = ('coordinator', 'conductor', 'pm')
WHY = ('`gate.py allow` raises the gate runs a branch may make past the branch gate budget, and only '
       'its coordinator (or the PM) runs it (docs/gate.md "The branch gate budget").')
INSTEAD = ('stop and send your coordinator what each gate run found and why another is needed; '
           'it runs gate.py allow')


def gate_words(inv):
    """The Words after tools/gate.py when `inv` runs it (rules.tool_run; runpy's module read as
    the script; the kit command's `kit gate`: rules.script_run), else None."""
    run = rules.script_run(inv)
    if run is None or run[0] not in SCRIPTS:
        return None
    return run[1]


def allows(inv):
    """True when `inv` runs `gate.py allow`: its first word after the script is literally
    `allow` (a Start-Process -ArgumentList string split as rules.tool_words splits it)."""
    words = gate_words(inv)
    if not words or words[0].dyn:
        return False
    texts = rules.tool_words(words[:1])
    return bool(texts) and texts[0] == VERB


def session_mark(event, ctx):
    """The launcher mark the call is judged by, without the kit plugin's prefix, or None for a
    subagent the host told apart: an unidentified call's agent_role, else the main session's mark."""
    if getattr(event, 'agent', None) == UNIDENTIFIED:
        return agentname.bare_mark(str(getattr(event, 'agent_role', None) or ''))
    if rules.subagent(event):
        return None
    return agentname.bare_mark(str(ctx.session_role(event) or ''))


def may_allow(event, ctx):
    """True for the owner's own session and a session marked with one of MARKS (case
    ignored); a subagent the host told apart never."""
    if rules.is_owner_session(event, ctx):
        return True
    mark = session_mark(event, ctx)
    return mark is not None and mark.lower() in MARKS


def who(event, ctx):
    """The session as the refusal names it."""
    mark = session_mark(event, ctx)
    if mark is None:
        name = agentname.bare(getattr(event, 'agent_role', None) or '') or 'unnamed'
        return 'a subagent (%s)' % name
    return 'a session marked %s' % mark


def shell(event, ctx, cmd, inv, cwd, role):
    """The rules.shell_command rule: gate-allow."""
    if inv is None or not allows(inv) or may_allow(event, ctx):
        return None
    return rules.refuse(RULE, role, '`%s`: %s This session is %s.' % (rules.bare(cmd), WHY, who(event, ctx)),
                        INSTEAD)
