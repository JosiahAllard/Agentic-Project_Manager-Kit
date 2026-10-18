"""Explicit agent-provider attribution, shared by commits, sync and ledger writes.

A provider is a host name, and its attribution is that host's hosts.json profile "attribution"
(kitlib.host.Attribution): the trailer name and email, the model environment fallback and whether
the model must be an explicit id. Provider None is the registry's default host, the old default.
Provider identity is never inferred from a model name.

  providers(path)                  {name: Attribution}: every registered host's
  model_environment(provider)      the model fallback variable, or None
  requires_model_id(provider)      whether --model must be an explicit id (no fallback)
  display_name(model, provider)    a lower-case model id as its display name, else as given
  coauthor(model, provider, families)   the Co-Authored-By line; ValueError when it cannot be one
  session_trailer(env, provider)   the host's session line (`Claude-Session: <url>`), or None
  session_keys(path)               every host's session trailer key
  agent_trailer(path)              the regex a Co-Authored-By value of any provider matches
  AGENT_TRAILER                    agent_trailer() of the kit's hosts.json, read at import
"""
import re

from kitlib.host import registry


def providers(path=None):
    """{name: Attribution}: each registered host's, in file order."""
    return {name: h.profile.attribution for name, h in registry.load(path).items()}


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


def display_name(model, provider=None, path=None):
    """`<name>-opus-5-5` -> `Opus 5.5` (<name>: the provider's attribution name, lower case, from
    hosts.json); any other value as given. Only a lower-case id of that exact shape is mapped, so
    a display name or a longer id (a date suffix) is never reworded."""
    if not isinstance(model, str):
        return model
    found = re.match(r'%s-(opus|sonnet|haiku)-(\d{1,2}(?:-\d{1,2})?)\Z'
                     % re.escape(_of(provider, path).name.lower()), model.strip())
    if found is None:
        return model
    return '%s %s' % (found.group(1).capitalize(), found.group(2).replace('-', '.'))


def coauthor(model, provider=None, families=(), path=None):
    a = _of(provider, path)
    model = display_name(model, provider, path)
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


SESSION_ID = re.compile(r'[A-Za-z0-9_-]+\Z')


def session_trailer(env, provider=None, path=None):
    """`<key>: <url>` for the session `env` (a mapping) names under `provider`'s
    attribution.session_trailer, or None: the host has none, or its variable is unset, blank or
    not a plain id (letters, digits, `_`, `-`), so nothing odd is ever written into a trailer."""
    st = _of(provider, path).session_trailer
    if st is None:
        return None
    value = (env.get(st.env) or '').strip()
    if not SESSION_ID.match(value):
        return None
    return '%s: %s' % (st.key, st.url.replace('{id}', value))


def session_keys(path=None):
    """The session trailer keys of every registered host, in file order, each once."""
    keys = []
    for a in providers(path).values():
        if a.session_trailer is not None and a.session_trailer.key not in keys:
            keys.append(a.session_trailer.key)
    return tuple(keys)


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
