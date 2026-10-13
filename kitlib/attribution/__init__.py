"""Explicit agent-provider attribution, shared by commits, sync and ledger writes.

A provider is a host name, and its attribution is that host's hosts.json profile "attribution"
(kitlib.host.Attribution): the trailer name and email, the model environment fallback and whether
the model must be an explicit id. Provider None is the registry's default host, the old default.
Provider identity is never inferred from a model name.

  providers(path)                  {name: Attribution}: every registered host's, then UNREGISTERED
  model_environment(provider)      the model fallback variable, or None
  requires_model_id(provider)      whether --model must be an explicit id (no fallback)
  coauthor(model, provider, families)   the Co-Authored-By line; ValueError when it cannot be one
  agent_trailer(path)              the regex a Co-Authored-By value of any provider matches
  AGENT_TRAILER                    agent_trailer() of the kit's hosts.json, read at import
"""
import re

from kitlib.host import Attribution, registry

# Codex's attribution, which no hosts.json entry declares until step 9 of docs/multi-host-plan.md
# registers that host (its profile's attribution then carries it, and this goes). Kept so a Codex
# trailer stays writable and recognised meanwhile.
UNREGISTERED = {'codex': Attribution('Codex', 'noreply@openai.com', None,
                                     r'[A-Za-z0-9][A-Za-z0-9._:/-]*')}


def providers(path=None):
    """{name: Attribution}: each registered host's, in file order, then UNREGISTERED's others."""
    out = {name: h.profile.attribution for name, h in registry.load(path).items()}
    for name, a in UNREGISTERED.items():
        out.setdefault(name, a)
    return out


def _of(provider, path=None):
    provider = registry.default(path) if provider is None else provider
    found = providers(path).get(provider)
    if found is None:
        raise ValueError('unsupported attribution provider: %s' % provider)
    return found


def model_environment(provider=None, path=None):
    """The variable a model name falls back to for `provider`, or None when it has none."""
    return _of(provider, path).model_env


def requires_model_id(provider=None, path=None):
    """Whether `provider` takes only an explicit model id that matches its pattern."""
    return _of(provider, path).model_pattern is not None


def coauthor(model, provider=None, families=(), path=None):
    a = _of(provider, path)
    if a.model_pattern is not None:
        if not isinstance(model, str) or not re.fullmatch(a.model_pattern, model):
            raise ValueError('%s requires an explicit active --model id' % a.name)
        return 'Co-Authored-By: %s %s <%s>' % (a.name, model, a.email)
    name = ' '.join((model or '').split())
    if not name:
        raise ValueError('--model is empty')
    if not any(name == f or (name.startswith(f) and name[len(f)] in ' .-') for f in families):
        if name != a.name and not name.startswith(a.name + ' '):
            name = a.name + ' ' + name
    return 'Co-Authored-By: %s <%s>' % (name, a.email)


def agent_trailer(path=None):
    """The compiled regex a whole Co-Authored-By value of any provider matches: a free-name
    provider's any name before its email, an id provider's `<name> <id> <email>`."""
    alts = []
    for a in providers(path).values():
        email = re.escape('<%s>' % a.email)
        if a.model_pattern is None:
            alts.append(r'\S.*\s' + email)
        else:
            alts.append('%s (?:%s) %s' % (re.escape(a.name), a.model_pattern, email))
    return re.compile('^(?:%s)$' % '|'.join(alts))


AGENT_TRAILER = agent_trailer()
