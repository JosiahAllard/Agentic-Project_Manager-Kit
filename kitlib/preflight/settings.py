"""`.kit.json` "preflight" as preflight and board use it, and the base branch pick.

kitlib.config's schema checks each key's type; settings() adds what a type cannot say: a hook is
`module:function` with both halves Python identifiers (the module is a file in the repo's tools
dir, so no dots), traps and engine prefixes are not blank, udp_ports is [low, high] with
low <= high, and the defaults are filled in.
"""
from kitlib.config import ConfigError
from kitlib.git import cmd

SECTION = 'preflight'
DEFAULT_TRAPS = 'docs/ops/gotchas.md (read once per task; add yours)'


def is_hook(text):
    """`module:function`, both Python identifiers."""
    parts = text.split(':') if isinstance(text, str) else []
    return len(parts) == 2 and all(p.isidentifier() for p in parts)


def settings(cfg):
    """{traps, report_hooks, engines (lower case), udp_ports or None} from a loaded Config;
    ConfigError on a value the schema lets through and preflight cannot use."""
    data = cfg.section(SECTION)
    where = '%s: "%s"' % (cfg.where, SECTION)
    hooks = data['report_hooks'] or []
    bad = [h for h in hooks if not is_hook(h)]
    if bad:
        raise ConfigError('%s.report_hooks must be "module:function" strings, both Python '
                          'identifiers: %s' % (where, ', '.join(bad)))
    if data['traps'] is not None and not data['traps'].strip():
        raise ConfigError('%s.traps must be a non-empty string' % where)
    if not all(e.strip() for e in data['engines'] or []):
        raise ConfigError('%s.engines must be non-empty process-name prefixes' % where)
    ports = data['udp_ports']
    if ports is not None and ports[0] > ports[1]:
        raise ConfigError('%s.udp_ports must be [low, high] with low <= high' % where)
    traps = data['traps'] if data['traps'] is not None else DEFAULT_TRAPS
    return {'traps': traps.strip(), 'report_hooks': list(hooks),
            'engines': [e.strip().lower() for e in data['engines'] or []],
            'udp_ports': list(ports) if ports is not None else None}


def base_names(cfg, explicit=None):
    """The base candidates: [explicit] when given, else ["repo".main_branch] when `.kit.json`
    sets it, else ['main', 'master'] (the first that exists wins, pick_base)."""
    if explicit:
        return [explicit]
    if 'main_branch' in cfg.raw('repo'):
        return [cfg.main_branch]
    return ['main', 'master']


def pick_base(names, local_refs):
    """The base among `names`: the only one, or main when refs/heads/main exists, else master."""
    if len(names) == 1:
        return names[0]
    return 'main' if 'refs/heads/main' in local_refs else 'master'


def base_refs(top, names, remote):
    """{refname: short hash} for `refs/heads/<name>` and `refs/remotes/<remote>/<name>` of each
    name that exists, in ONE `git for-each-ref` (fewer git spawns). A missing
    ref is simply absent; {} when git failed."""
    wanted = [r for n in names for r in ('refs/heads/%s' % n, 'refs/remotes/%s/%s' % (remote, n))]
    out, _why = cmd.run_text(['for-each-ref', '--format=%(refname) %(objectname:short)'] + wanted,
                             top)
    if out is None:
        return {}
    pairs = (line.split(' ', 1) for line in out.splitlines() if ' ' in line)
    return {ref: short for ref, short in pairs if ref in wanted}   # a pattern is also a prefix


def base_pair(top, cfg, explicit=None):
    """(base name, local short hash or '', <remote>/<base> short hash or '') - the one base pick
    preflight and the board share. An explicit base with no local branch may be any revision."""
    names = base_names(cfg, explicit)
    refs = base_refs(top, names, cfg.remote)
    base = pick_base(names, refs)
    b = refs.get('refs/heads/' + base, '')
    ob = refs.get('refs/remotes/%s/%s' % (cfg.remote, base), '')
    if explicit and not b:
        b = cmd.run_text(['rev-parse', '--short', '--verify', '--quiet', explicit], top)[0] or ''
    return base, b, ob


def measure_rev(cfg, base, b, ob, explicit=None):
    """The rev the behind/ahead and contains-base rows measure against: `base` itself when the
    caller named it or when it equals its `<remote>/<base>` (or that is not there), else
    `refs/remotes/<remote>/<base>`: the shared default-base rule (kitlib.git.refs.default_base,
    docs/gate.md "Default base"), so a stale local main does not count the remote's newer commits
    as a branch's work."""
    if explicit or not ob or b == ob:
        return base
    return 'refs/remotes/%s/%s' % (cfg.remote, base)
