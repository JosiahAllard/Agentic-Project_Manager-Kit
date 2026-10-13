"""Role policy: what a role may do on any host, over the shared rules (docs/guard.md "Role policy";
ARCHITECTURE.md "Roles"). Lifted from the Codex branch's guard_adapter and kit_adapter, with no host
in it.

Off unless the repository opts in: a role is held to it only when the main checkout's .kit.json
`roles.<role>.mode` names its mode, so a session whose role has no mode is judged exactly as before.
The role is the subagent's (config.policy.role_name: Action.agent_role when the roles table names
it), else the main session's launcher mark when the table names it, else `main`. The owner's own
session (nothing marks it) is never held, and its calls read no config here.

  MODES   controller   a lead (pm, coordinator): shell and edits as before, plus the checks below
          write        a builder: the same
          read         reads and reports: no file edits, the whole checkout read-only to shell
          test         runs tests: as read, plus the lock's run verbs
          lander       lands: no file edits (its git landing stays rules.lander_check's)

  screen(action, ctx)   (check's Decision, context's Ctx): what rules.decide runs once the shared
                        rules allow the call (a call they refuse never reaches the policy)
  check(action, ctx)    a Decision, or None to go on to the shared rules:
    role-edit     an edit by a read, test or lander role
    role-workdir  a shell call whose own working directory (Action.workdir, a host whose shell
                  call names one) is outside the checkout the session runs in
    role-shell    an interactive interpreter (shell_gap): a shell, cmd, PowerShell or python run with
                  no script, command or informational option, or told to stay open
                  (-i, -, -s, -NoExit and any prefix of it, /k)
    role-lock     the lock CLI (machine_lock.py) run with a verb outside LOCK_VERBS, or with
                  --force in any abbreviation; read and lander roles get READ_LOCK_VERBS alone
  context(action, ctx)  the Ctx the shared rules then use: for a role with a mode, a view whose
                  .kit.json guard.data_roots also hold, read-only, every host's owner paths
                  (hosts.json profile owner_paths) in the session's checkout, and for a read or test
                  role the checkout itself, so the write jail refuses them (rule write-jail). An
                  owner path that holds the host's worktrees folder is left out in a main checkout,
                  so the worktrees under it stay writable. The real config is never changed.

Codex's shell-patch transport refusal (apply_patch run as a shell command) is Codex's own and stays
in its adapter.
"""
import copy
import os

from kitlib import agentname
from kitlib import config as kconfig
from kitlib.git import repo

from . import commands as cm
from . import paths
from . import rules
from . import shell as sh
from .context import OWNER

MODES = ('controller', 'write', 'read', 'test', 'lander')
NO_EDIT = ('read', 'test', 'lander')
READ_ONLY_CHECKOUT = ('read', 'test')
LOCK_VERBS = ('status', 'stats', 'run', 'background', 'stop')
READ_LOCK_VERBS = ('status', 'stats')
INTERACTIVE = ('-', '-i', '-s', '-interactive', '-noexit', '/k')
FINITE = {'ps': ('-command', '-c', '-file', '-f', '-version', '-help', '-?'),
          'cmd': ('/c', '/?'),
          'other': ('-c', '-m', '-lc', '-cl', '--command', '-v', '--version', '-h', '--help')}
_deny = rules.refuse
fwd = rules.fwd


def role_of(action, ctx, table):
    """The roles-table name the Action runs as (module docstring), or 'main'."""
    if getattr(action, 'agent', None):
        return kconfig.role_name(action, table)
    who = agentname.bare_mark(str(ctx.session_role(action) or ''))
    return who if who in table and who not in ('main', 'subagent') else 'main'


def mode(action, ctx):
    """(role, mode) of the Action, mode None when the policy is off for it: an action no rule
    here judges, the owner's own session, a session outside any checkout's .kit.json, or a role
    whose entry names no mode."""
    if action is None or action.kind not in ('edit', 'shell'):
        return None, None
    if not getattr(action, 'agent', None) and ctx.session_role(action) == OWNER:
        return None, None                       # the owner's own session: no role, nothing read
    cwd = action.cwd or os.getcwd()
    cfg = ctx.config(cwd)
    if cfg is None:
        return None, None
    table = kconfig.roles(cfg)
    role = role_of(action, ctx, table)
    m = table.get(role, {}).get('mode')
    return role, (m if m in MODES else None)


def checkout(ctx, cwd):
    """The checkout a session in `cwd` works in: its git top, else `cwd` itself."""
    return ctx.place(cwd).top or cwd


# ---------------------------------------------------------------- check

def check(action, ctx):
    role, m = mode(action, ctx)
    if m is None:
        return None
    tag = rules.tag_role(action)
    cwd = action.cwd or os.getcwd()
    if action.kind == 'edit':
        if m in NO_EDIT:
            return _deny('role-edit', tag, '%s of %s: the %s role (mode %s) does not edit files.'
                         % (action.tool or 'An edit', action.path, role, m),
                         'return the change to the coordinator as text in your report')
        return None
    top = checkout(ctx, cwd)
    where = getattr(action, 'workdir', None)
    if where and not inside(where, top):
        return _deny('role-workdir', tag, 'The shell call runs in %s, outside the checkout this session works in '
                     '(%s). Run it from the checkout:' % (fwd(where), fwd(top)), 'cd "%s"' % fwd(top))
    text = action.command
    if not isinstance(text, str) or not text.strip():
        return None
    parsed = cm.read(text, rules.SHELLS.get(action.shell, sh.BASH), where or cwd)
    gap = shell_gap(parsed)
    if gap:
        name, why, instead = gap
        return _deny('role-shell', tag, '`%s` %s; a hook cannot answer an interpreter that waits for input. '
                     'Give it the script or command to run:' % (name, why), instead)
    found = lock_verb(parsed, m)
    if found:
        verb, allowed = found
        return _deny('role-lock', tag, 'machine_lock.py %s is not one of the lock verbs the %s role (mode %s) '
                     'runs (%s). To see the lock state:' % (verb, role, m, ', '.join(allowed)),
                     'python %s status' % rules.lock_tool(ctx))
    return None


def inside(path, top):
    """True when `path` is `top` or below it, both as the filesystem reaches them; different
    drives are simply outside."""
    here, root = paths.norm(path, real=True), paths.norm(top, real=True)
    try:
        return os.path.commonpath((here, root)) == root
    except ValueError:
        return False


def _interpreter(name):
    if name in cm.PS_SHELLS:
        return 'ps'
    if name == 'cmd':
        return 'cmd'
    if name in cm.BASH_SHELLS or cm.PYTHON_RE.fullmatch(name):
        return 'other'
    return None


def shell_gap(parsed):
    """(program, why, instead) for the first interpreter `parsed` starts that would wait for input,
    else None. A nested shell's own commands are in `parsed` too (commands.read), so `bash -c bash`
    is caught at the inner bash."""
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        if inv is None:
            continue
        kind = _interpreter(inv.name)
        if kind is None:
            continue
        args = [a.lower() for a in inv.argv]
        instead = {'ps': '%s -NoProfile -Command "<command>"', 'cmd': '%s /c <command>'}.get(
            kind, '%s -c "<command>"') % inv.name
        if not args or any(a in INTERACTIVE for a in args):
            return inv.name, 'starts an interactive interpreter', instead
        if kind == 'ps':
            for a in args:
                key = a.lstrip('-/').split(':', 1)[0]
                if a[:1] in '-/' and key and 'noexit'.startswith(key):
                    return inv.name, 'keeps PowerShell open (%s is NoExit)' % a, instead
            finite = any(a in FINITE['ps'] for a in args)
        elif kind == 'cmd':
            finite = any(a in FINITE['cmd'] for a in args)
        else:
            finite = any(a in FINITE['other'] for a in args) or any(not a.startswith('-') for a in args)
        if not finite:
            return inv.name, 'names no script, command or informational option', instead
    return None


def _lock_words(inv):
    seq = [inv.path] + inv.argv
    for k, t in enumerate(seq):
        if cm.base_name(t) == rules.LOCK_SCRIPT:
            rest = []
            for w in seq[k + 1:]:
                rest.extend(w.split() if ' ' in w else [w])     # a Start-Process "-ArgumentList" string
            return rest
    return None


def lock_verb(parsed, m):
    """(verb, allowed) for the first lock CLI call whose verb mode `m` may not run, or that forces;
    None when every one is allowed. Only the words before `--` are the lock's (after it, `run`'s
    command)."""
    allowed = READ_LOCK_VERBS if m in ('read', 'lander') else LOCK_VERBS
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        words = _lock_words(inv) if inv is not None else None
        if words is None:
            continue
        head = words[:words.index('--')] if '--' in words else words
        verb, k = None, 0
        while k < len(head):
            t = head[k]
            if t.startswith('-'):
                if len(t) > 2 and t.startswith('--') and '--force'.startswith(t.split('=', 1)[0]):
                    return '%s %s' % (verb or '', t), allowed
                k += 2 if t in rules.LOCK_VALUE_OPTS else 1
                continue
            if verb is None:
                verb = t
            k += 1
        if verb is None or verb not in allowed:
            return verb or '(no verb)', allowed
    return None


# ---------------------------------------------------------------- context

def owner_roots(ctx, top):
    """[path] read-only for a role with a mode: every host's owner paths in the checkout `top`; in a
    main checkout, not one that holds that host's worktrees folder."""
    from kitlib.host import registry
    main = ctx.place(top).kind == repo.MAIN
    out = []
    for h in registry.load().values():
        wt = h.profile.worktrees_directory.strip('/')
        for p in h.profile.owner_paths:
            rel = p.strip('/')
            if main and (wt == rel or wt.startswith(rel + '/')):
                continue
            path = os.path.join(top, *rel.split('/'))
            if path not in out:
                out.append(path)
    return out


def screen(action, ctx):
    role, m = mode(action, ctx)
    if m is None:
        return None, ctx                        # off: the shared rules alone, as before
    return check(action, ctx), context(action, ctx)


def context(action, ctx):
    role, m = mode(action, ctx)
    if m is None:
        return ctx
    top = checkout(ctx, action.cwd or os.getcwd())
    extra = [{'path': p, 'mode': 'read-only'} for p in owner_roots(ctx, top)]
    if m in READ_ONLY_CHECKOUT:
        extra.append({'path': top, 'mode': 'read-only'})
    return PolicyContext(ctx, extra)


class ConfigView(object):
    """A Config whose guard.data_roots also hold `extra`; the rest is the Config's own."""

    def __init__(self, config, extra):
        self._config, self._extra = config, extra

    def __getattr__(self, name):
        return getattr(self._config, name)

    def section(self, name):
        value = self._config.section(name)
        if name != 'guard':
            return value
        value = copy.deepcopy(value)
        value['data_roots'] = list(value.get('data_roots') or []) + copy.deepcopy(self._extra)
        return value


class PolicyContext(object):
    """The Ctx the shared rules read, with every .kit.json it hands out seen through ConfigView."""

    def __init__(self, base, extra):
        self._base, self._extra = base, extra

    def __getattr__(self, name):
        return getattr(self._base, name)

    def config(self, path):
        value = self._base.config(path)
        return None if value is None else ConfigView(value, self._extra)

    def config_at(self, top):
        value = self._base.config_at(top)
        return None if value is None else ConfigView(value, self._extra)
