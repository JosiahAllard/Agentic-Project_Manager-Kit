"""kitlib.guard: the kit's PreToolUse shell guard and git pre-push check (ARCHITECTURE.md
"Layers": Guards; docs/guard.md).

decide(event, ctx=None) -> Decision(kind 'deny' or 'ask', rule, reason) or None (allow);
hook_output(decision) -> the PreToolUse JSON Claude Code reads. claude/hooks/guard.py and
githooks/pre-push are the thin entry points.
"""
from .context import Ctx
from .rules import ASK, DENY, Decision, decide


def hook_output(decision):
    """The hook's stdout object for a decision; None (print nothing) for an allow."""
    if decision is None:
        return None
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                   'permissionDecision': decision.kind,
                                   'permissionDecisionReason': decision.reason}}


__all__ = ['Ctx', 'Decision', 'DENY', 'ASK', 'decide', 'hook_output']
