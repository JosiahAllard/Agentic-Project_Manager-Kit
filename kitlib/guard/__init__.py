"""kitlib.guard: the kit's tool-call guard and git pre-push check (ARCHITECTURE.md "Layers":
Guards; docs/guard.md).

decide(action, ctx=None) -> kitlib.host.Decision: 'allow', or 'deny'/'ask' with the rule, the
reason and the command to use instead. `action` is the neutral kitlib.host.Action; a host's
adapter parses its own hook payload into one and renders the Decision back in its own format
(hosts/claude/adapter for Claude Code), and kitlib.host.hookmain runs the two around decide. No module
here reads a host's payload or writes a host's output. githooks/pre-push is the git entry point
(kitlib.guard.prepush).
"""
from kitlib.host import Decision

from .context import Ctx
from .rules import ALLOW, ASK, DENY, decide

__all__ = ['Ctx', 'Decision', 'ALLOW', 'DENY', 'ASK', 'decide']
