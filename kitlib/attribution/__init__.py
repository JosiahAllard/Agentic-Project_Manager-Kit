"""Explicit agent-provider attribution, shared by commits, sync and ledger writes.

Claude remains the default. Provider identity is never inferred from a model name.
Codex requires an explicit model id; Claude's existing family-name handling is retained.
"""
import re


PROVIDERS = ('claude', 'codex')
CODEX_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]*\Z')
AGENT_TRAILER = re.compile(
    r'^(?:\S.*\s<noreply@anthropic\.com>|'
    r'Codex [A-Za-z0-9][A-Za-z0-9._:/-]* <noreply@openai\.com>)$')


def model_environment(provider='claude'):
    """Only legacy Claude callers have a model environment fallback."""
    if provider not in PROVIDERS:
        raise ValueError('unsupported attribution provider: %s' % provider)
    return 'CLAUDE_AGENT_MODEL' if provider == 'claude' else None


def coauthor(model, provider='claude', families=()):
    model_environment(provider)
    if provider == 'codex':
        if not isinstance(model, str) or not CODEX_MODEL.fullmatch(model):
            raise ValueError('Codex requires an explicit active --model id')
        return 'Co-Authored-By: Codex %s <noreply@openai.com>' % model
    name = ' '.join((model or '').split())
    if not name:
        raise ValueError('--model is empty')
    if not any(name == f or (name.startswith(f) and name[len(f)] in ' .-') for f in families):
        if name != 'Claude' and not name.startswith('Claude '):
            name = 'Claude ' + name
    return 'Co-Authored-By: %s <noreply@anthropic.com>' % name
