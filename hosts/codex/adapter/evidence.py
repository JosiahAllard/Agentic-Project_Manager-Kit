"""Read-only structural checks of captured hooks against actual runtime bindings (from the Codex
branch's hook_evidence.py): what a live capture must show before hosts.json may declare
subagent_identity_in_hooks for Codex.

This is acceptance support, not an authentication transport or permission grant. Never
use payloads, inherited marks, transcripts or these reports to activate a hook guard.
"""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class HookEvidence:
    event: str
    role: str | None
    child: bool
    identity_matches: bool
    blockers: tuple


def inspect(event, binding):
    """Report identity gaps without recording arguments, commands or transcript content."""
    if not isinstance(event, dict):
        raise ValueError('captured native hook must be an object')
    name = event.get('hook_event_name')
    if name not in {'SessionStart', 'SubagentStart', 'PreToolUse', 'PostToolUse', 'SubagentStop'}:
        raise ValueError('unsupported native hook evidence event')
    blockers = []
    if event.get('session_id') != binding.session:
        blockers.append('Native hook session does not match the bound session tree.')
    cwd = event.get('cwd')
    if not isinstance(cwd, str) or os.path.normcase(os.path.realpath(cwd)) != os.path.normcase(os.path.realpath(binding.cwd)):
        blockers.append('Native hook checkout does not match the bound checkout.')
    if event.get('model') != binding.model:
        blockers.append('Native hook model is missing or differs from the bound model.')
    if binding.parent is not None:
        if event.get('agent_id') != binding.thread:
            blockers.append('Native hook lacks the actual calling child identifier; parent session identity is ambiguous.')
    else:
        # A session-only PreToolUse payload can also be emitted by a child. Root
        # session equality cannot prove root origin without an authenticated route.
        if name in {'PreToolUse', 'PostToolUse'} and event.get('agent_id') != binding.thread:
            blockers.append('Native hook lacks the exact bound calling thread identifier; session identity also covers children.')
        elif event.get('agent_id') not in (None, binding.thread):
            blockers.append('Native hook calling identifier differs from the bound root.')
    identity_matches = not blockers
    blockers.append('An authenticated runtime-to-hook invocation transport and trusted native denial canary are still required.')
    return HookEvidence(name, binding.role, binding.parent is not None, identity_matches, tuple(blockers))
