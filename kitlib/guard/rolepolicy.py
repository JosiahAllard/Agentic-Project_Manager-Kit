"""Role policy: what a role may do on any host, over the shared rules (docs/guard.md "Role policy";
ARCHITECTURE.md "Roles"). Lifted from the Codex branch's guard_adapter and kit_adapter, with no host
in it.

Off unless the repository opts in: the main checkout's .kit.json `guard.role_policy` is true. With it
off no role is held, whatever mode a role file or the roles table names, so every call is judged
exactly as before. With it on, a role is held to its mode: `roles.<role>.mode` in that .kit.json,
else the `mode` of its role file, `<kit>/content/roles/<role>.md` under the kit root the guard runs
from (Ctx.kit; kitlib.content.role_mode, which reads an old name such as opus-worker as its new
one). A role with neither, or whose role files are missing or unreadable, is judged as before.
The role is the subagent's (config.policy.role_name: Action.agent_role when the roles table or a
role file names it), else the main session's launcher mark when one of them names it, else `main`.
The owner's own session (nothing marks it) is never held, and its calls read no config here.

  MODES   controller   a lead (pm, coordinator): shell and edits as before, plus the checks below
          write        a builder: the same
          read         reads and reports: no file edits in its checkout, the checkout read-only to
                       shell, the owner paths read-only
          test         runs tests: as read, with the agent's lock verbs
          lander       lands: no file edits in its checkout, the owner paths read-only (its git
                       landing stays rules.lander_check's)
  A role file's `writes` (kitlib.content.role_writes: repo-relative paths or globs) are the files a
  read, test or lander role may still edit in its checkout with a file tool (the consultant's notes
  file). A shell write to one stays refused: the jail's roots are folders, not globs.

  screen(action, ctx)   (check's Decision, context's Ctx): what rules.decide runs once the shared
                        rules allow the call (a call they refuse never reaches the policy)
  check(action, ctx)    a Decision, or None to go on to the shared rules:
    role-edit     an edit inside the session's checkout by a read, test or lander role, unless its
                  role file's `writes` names the file; an edit outside it (temp, the scratch
                  folder) is the write jail's, as for any session
    role-workdir  a shell call whose own working directory (Action.workdir, a host whose shell
                  call names one) is outside the checkout the session runs in
    role-shell    an interactive interpreter (shell_gap): a shell, cmd, PowerShell or python run with
                  no script, command or informational option, or told to stay open
                  (-i, -, -s, -NoExit and any prefix of it, /k). Standard input fed by a pipe, a
                  heredoc, a here-string or `<` is the script: bare, `-` and `-s` then pass
    role-lock     the lock CLI (machine_lock.py) run with a verb outside the mode's LOCK_VERBS, or
                  with --force in any abbreviation
  context(action, ctx)  the Ctx the shared rules then use: for a read, test or lander role, a view
                  whose .kit.json guard.data_roots also hold, read-only, every host's owner paths
                  (hosts.json profile owner_paths) in the session's checkout, and for a read or test
                  role the checkout itself (not for a file-tool edit of a `writes` file), so the
                  write jail refuses them (rule write-jail). An owner path that holds the host's
                  worktrees folder is left out in a main checkout, so the worktrees under it stay
                  writable. Write and controller roles get no roots: the owner paths stay under the
                  owner-approval rules, as before. The real config is never changed.

Codex's shell-patch transport refusal (apply_patch run as a shell command) is Codex's own and stays
in its adapter.
"""
import copy
import fnmatch
import os

from kitlib import agentname
from kitlib import config as kconfig
from kitlib import content as kcontent
from kitlib.host.content import MODES
from kitlib.git import repo

from . import commands as cm
from . import paths
from . import rules
from . import shell as sh
from .context import OWNER

NO_EDIT = ('read', 'test', 'lander')
READ_ONLY_CHECKOUT = ('read', 'test')
OWNER_PATHS_READ_ONLY = NO_EDIT
# The lock verbs a mode runs: verb -> None (any argument) or the sub-verbs it may take ('' none).
# AGENT_LOCK is docs/guard.md "Lock CLI owner writes" "Still open": an agent's rights.
AGENT_LOCK = {'status': None, 'stats': None, 'run': None, 'background': None, 'stop': None,
              'book': None, 'unbook': None, 'hold': ('agent', 'list', 'end', 'edit'),
              'division': ('',), 'game': ('', 'show')}
LOCK_VERBS = {'controller': AGENT_LOCK, 'write': AGENT_LOCK, 'test': AGENT_LOCK,
              'lander': {'status': None, 'stats': None, 'run': None},
              'read': {'status': None, 'stats': None}}
INTERACTIVE = ('-', '-i', '-s', '-interactive', '-noexit', '/k')
REAL_NOT_FORCE = ('--for',)   # a lock CLI option of its own (hold edit, window_args), not --force cut short
FED = ('-', '-s')               # with standard input fed, the script comes from it
STDIN_OPS = ('<', '<<', '<<<')
FINITE = {'ps': ('-command', '-c', '-file', '-f', '-version', '-help', '-?'),
          'cmd': ('/c', '/?'),
          'other': ('-c', '-m', '-lc', '-cl', '--command', '-v', '--version', '-h', '--help')}
_deny = rules.refuse
fwd = rules.fwd


def role_of(action, ctx, table):
    """The name the Action runs as (module docstring), or 'main': `table` holds the role names
    known (the roles table's and the role files')."""
    if getattr(action, 'agent', None):
        return kconfig.role_name(action, table)
    who = agentname.bare_mark(str(ctx.session_role(action) or ''))
    return who if who in table and who not in ('main', 'subagent') else 'main'


def enabled(cfg):
    """True when the .kit.json `cfg` turns the role policy on (guard.role_policy)."""
    return cfg.section('guard').get('role_policy') is True


def mode(action, ctx):
    """(role, mode) of the Action, mode None when the policy is off for it: an action no rule
    here judges, the owner's own session, a session outside any checkout's .kit.json, a .kit.json
    that does not turn the policy on, or a role with no mode in the roles table or its role file."""
    if action is None or action.kind not in ('edit', 'shell'):
        return None, None
    if not getattr(action, 'agent', None) and ctx.session_role(action) == OWNER:
        return None, None                       # the owner's own session: no role, nothing read
    cwd = action.cwd or os.getcwd()
    cfg = ctx.config(cwd)
    if cfg is None or not enabled(cfg):
        return None, None
    table = kconfig.roles(cfg)
    files = kcontent.role_modes(ctx.kit)
    role = role_of(action, ctx, set(table) | set(files) | set(kcontent.ALIASES))
    m = table.get(role, {}).get('mode') or kcontent.role_mode(role, ctx.kit)
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
    top = checkout(ctx, cwd)
    if action.kind == 'edit':
        target = edit_path(action, cwd)
        if m in NO_EDIT and target and inside(target, top) and not role_may_write(ctx, role, target, top):
            return _deny('role-edit', tag, '%s of %s: the %s role (mode %s) does not edit files in the '
                         'checkout it works in (%s); its scratch and temp folders stay open.'
                         % (action.tool or 'An edit', action.path, role, m, fwd(top)),
                         'return the change to the coordinator as text in your report')
        return None
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
                     'runs (%s; never --force). To see the lock state:' % (verb, role, m, allowed),
                     'python %s status' % rules.lock_tool(ctx))
    return None


def edit_path(action, cwd):
    """The file an edit Action writes, made absolute against `cwd` as the shared rules do."""
    path = action.path
    if not isinstance(path, str) or not path:
        return None
    return sh.resolve_path(path, cwd) or path


def role_may_write(ctx, role, target, top):
    """True when `target`, inside the checkout `top`, matches a `writes` entry of the role's role
    file (repo-relative, posix, glob_match; case-insensitive where the filesystem is)."""
    pats = kcontent.role_writes(role, ctx.kit)
    if not pats:
        return False
    rel = os.path.relpath(paths.norm(target, real=True), paths.norm(top, real=True)).replace(os.sep, '/')
    if rel == '.' or rel.startswith('../'):
        return False
    return any(glob_match(rel, p) for p in pats)


def glob_match(rel, pattern):
    """True when the posix path `rel` matches `pattern` part by part: a `*` never crosses a `/`."""
    have, want = rel.split('/'), pattern.split('/')
    return len(have) == len(want) and all(fnmatch.fnmatch(h, w) for h, w in zip(have, want))


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


def stdin_fed(cmd):
    """True when the command's standard input is a pipe, a heredoc, a here-string or a `<` file,
    not the terminal."""
    return bool(cmd.piped_in) or any(r.op in STDIN_OPS and not r.dup for r in cmd.redirects)


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
        fed = stdin_fed(cmd)
        rest = [a for a in args if not (fed and a in FED)]
        if fed and (not args or len(rest) < len(args)) and not any(a in INTERACTIVE for a in rest):
            continue                            # its script is the pipe, heredoc or file on stdin
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
    """(verb, allowed) for the first lock CLI call whose verb (and sub-verb: `hold agent`) mode
    `m` may not run (LOCK_VERBS), or that forces; None when every one is allowed. `allowed` is the
    mode's verbs as text. Only the words before `--` are the lock's (after it, `run`'s command)."""
    table = LOCK_VERBS[m]
    allowed = verbs_text(table)
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        words = _lock_words(inv) if inv is not None else None
        if words is None:
            continue
        head = words[:words.index('--')] if '--' in words else words
        said, k = [], 0
        while k < len(head):
            t = head[k]
            if t.startswith('-'):
                key = t.split('=', 1)[0]
                if len(key) > 2 and key.startswith('--') and key not in REAL_NOT_FORCE and '--force'.startswith(key):
                    return '%s %s' % (' '.join(said[:1]), t), allowed
                k += 2 if t in rules.LOCK_VALUE_OPTS else 1
                continue
            said.append(t)
            k += 1
        if not said or said[0] not in table:
            return (said[0] if said else '(no verb)'), allowed
        subs, sub = table[said[0]], (said[1] if len(said) > 1 else '')
        if subs is not None and sub not in subs:
            return ' '.join(said[:2]), allowed
    return None


def verbs_text(table):
    """The verbs of a LOCK_VERBS entry as one line: `hold agent|list|end|edit`, `game [show]`."""
    out = []
    for verb, subs in table.items():
        if subs is None:
            out.append(verb)
            continue
        named = [s for s in subs if s]
        text = '|'.join(named)
        out.append('%s (bare)' % verb if not named else
                   ('%s [%s]' % (verb, text) if '' in subs else '%s %s' % (verb, text)))
    return ', '.join(out)


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
    if m not in OWNER_PATHS_READ_ONLY:
        return ctx                              # write, controller: the owner-approval rules alone
    cwd = action.cwd or os.getcwd()
    top = checkout(ctx, cwd)
    extra = [{'path': p, 'mode': 'read-only'} for p in owner_roots(ctx, top)]
    if m in READ_ONLY_CHECKOUT and not writes_file(action, ctx, role, cwd, top):
        extra.append({'path': top, 'mode': 'read-only'})
    return PolicyContext(ctx, extra)


def writes_file(action, ctx, role, cwd, top):
    """True for a file-tool edit of a file the role's `writes` names (role_may_write)."""
    if action.kind != 'edit':
        return False
    target = edit_path(action, cwd)
    return bool(target) and inside(target, top) and role_may_write(ctx, role, target, top)


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
