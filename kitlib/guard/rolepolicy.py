"""Role policy: what a role may do on any host, over the shared rules (docs/guard.md "Role policy";
ARCHITECTURE.md "Roles"). Lifted from the Codex branch's guard_adapter and kit_adapter, with no host
in it.

Off unless the repository opts in: the main checkout's .kit.json `guard.role_policy` is true. With it
off no role is held, whatever mode a role file or the roles table names, so every call is judged
exactly as before. With it on, a role is held to its mode: `roles.<role>.mode` in that .kit.json
(a key under the role's old or new name too, kitlib.content.names_of), else the `mode` of its
role file, `<kit>/content/roles/<role>.md` under the kit root the guard runs from (Ctx.kit;
kitlib.content.role_mode, which reads an old name such as opus-worker as its new one). A role with neither, or whose role files are missing or unreadable, is judged as before.
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
  rules_for(mode)       [(rule, plain text)]: the RULES rows that hold a mode (tools/agent_view.py)
  check(action, ctx)    a Decision, or None to go on to the shared rules:
    role-edit     an edit inside the session's checkout by a read, test or lander role, unless its
                  role file's `writes` names the file; an edit outside it (temp, the scratch
                  folder) is the write jail's, as for any session
    role-workdir  a shell call whose own working directory (Action.workdir, a host whose shell
                  call names one) is outside the checkout the session runs in
    role-shell    an interactive interpreter (shell_gap): a shell, cmd, PowerShell or python whose
                  own options (those before the script, the command or `-`) name no script,
                  command or informational option, or tell it to stay open (-i, -Interactive,
                  -NoExit and any prefix of it, /k). Standard input fed by a pipe, a heredoc, a
                  here-string or `<` is the script: bare, `-` and bash's `-s` then pass
    role-lock     fail-closed on any command whose words, heredoc body, here-string or `<` file
                  name the lock CLI (machine_lock.py in any spelling Windows opens as it - any
                  case, a trailing dot or space, a `::$DATA` stream: rules.is_lock_script - or its
                  module tools.machine_lock: _lock_seq). It passes only when its program never
                  runs them with its output staying (backlog.py, ledger.py and commit.py under
                  tools/ of the kit folder, of a checkout of the kit repository or of the kit's
                  plugin export: kit_checkout, kit_export; a bare reader or printer; a git read or
                  commit (LOCK_PATH_GIT; a commit's message is text) with plain options and no
                  environment assignment on the line:
                  _lock_path_only, _git_plain), or runs the lock CLI itself (its script, python
                  -m: _run_words; the python a launcher starts as its program: _launched) with a
                  verb inside the mode's LOCK_VERBS and no --force in any abbreviation, and no
                  later command reads the last word back (bash's $_, PowerShell's $$: _last_words).
                  Any other program given it may copy, store or run it (cp, Copy-Item, ln, read,
                  xargs, python -, runpy, a python given to a shell as $0 or $1: HANDED_ON; so is
                  the name glued to other text in a launcher's line the guard does not read:
                  _hidden_launch), and a command with no program the guard can name (an
                  assignment alone, `$c=gc ...`) whose words name it is NO_PROGRAM; both are
                  refused in every mode. Not read (_lock_words): an environment assignment's
                  value, a glob, a name built from pieces, an 8.3 short name. A lock MCP
                  tool (lock_mcp_tool) whose verb (MCP_VERBS) is outside the mode's verbs, or that
                  the lock server does not have, is refused too
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
import re

from kitlib import agentname
from kitlib import config as kconfig
from kitlib import content as kcontent
from kitlib.content import oncommit
from kitlib.host.content import MODES
from kitlib.git import repo

from . import commands as cm
from . import jail
from . import paths
from . import rules
from . import shell as sh
from .context import OWNER

NO_EDIT = ('read', 'test', 'lander')
READ_ONLY_CHECKOUT = ('read', 'test')
OWNER_PATHS_READ_ONLY = NO_EDIT
# What each rule holds a mode to, in plain words: (rule, the modes it holds, what it says). check
# and context read the same mode tuples; tests/guard/test_rolepolicy.py pins each row to the
# answers the guard gives, and tools/agent_view.py shows a role the rows of its mode (rules_for).
RULES = (
    ('role-edit', NO_EDIT, 'no file-tool edit inside its checkout, except a file its role file\'s '
                           '`writes` names; temp and scratch folders stay open'),
    ('role-workdir', MODES, 'a shell call runs inside its checkout, not in another folder'),
    ('role-shell', MODES, 'no interactive interpreter: a shell, cmd, PowerShell or python is given '
                          'the script or command to run'),
    ('role-lock', MODES, 'the machine lock only through its mode\'s verbs, and never with --force'),
    ('write-jail', READ_ONLY_CHECKOUT, 'its checkout is read-only to the shell'),
    ('write-jail', OWNER_PATHS_READ_ONLY, 'every host\'s owner paths in its checkout are read-only'),
)
# The lock verbs a mode runs: verb -> None (any argument) or the sub-verbs it may take ('' none).
# Only verbs tools/machine_lock.py has (its parser: run, background, status, stop, top, hold,
# division, game); AGENT_LOCK is docs/guard.md "Lock CLI owner writes" "Still open" among them: an
# agent's rights. `status` also gives the run history (--days).
AGENT_LOCK = {'status': None, 'run': None, 'background': None, 'stop': None,
              'hold': ('agent', 'list', 'end', 'edit'), 'division': ('',), 'game': ('', 'show')}
LOCK_VERBS = {'controller': AGENT_LOCK, 'write': AGENT_LOCK, 'test': AGENT_LOCK,
              'lander': {'status': None, 'run': None},
              'read': {'status': None}}
# The lock MCP's tools (kitlib.lockmcp.server.TOOLS) as the lock CLI's verbs they match, held to
# LOCK_VERBS the same way. A tool under the lock's prefix that is not here is refused.
MCP_VERBS = {'lock_snapshot': ('status',), 'lock_wait_change': ('status',), 'lock_stats': ('status',),
             'lock_run': ('status',), 'lock_hold': ('status',), 'lock_hold_create': ('hold', 'create'),
             'lock_hold_edit': ('hold', 'edit'), 'lock_hold_end': ('hold', 'end'),
             'lock_set_game_mode': ('game', 'on'), 'lock_set_games': ('game', 'set'),
             'lock_set_division': ('division', 'set'), 'lock_end_run': ('stop',)}
REAL_NOT_FORCE = ('--for',)   # a lock CLI option of its own (hold edit, window_args), not --force cut short
STDIN_OPS = ('<', '<<', '<<<')
# What an interpreter's own options say (shell_gap): it runs a script or command and ends, it reads
# its script from standard input, it stays on the terminal, or (PowerShell) it stays open.
FINITE, STDIN, INTERACTIVE, NOEXIT = 'finite', 'stdin', 'interactive', 'noexit'
PY_INFO, PY_INFO_LONG = 'Vh?', ('--version', '--help')         # python -V, --help-env, ...
PY_VALUE, PY_VALUE_LONG = 'WX', ('--check-hash-based-pycs',)   # options that take the next word
SH_INFO_LONG = ('--version', '--help')
SH_VALUE, SH_VALUE_LONG = 'oO', ('--rcfile', '--init-file')
PS_INFO = ('version', 'v', 'help', 'h', '?')
PS_VALUE_NAMES = ('executionpolicy', 'windowstyle', 'outputformat', 'inputformat', 'configurationname',
                  'workingdirectory', 'psconsolefile', 'settingsfile', 'custompipename', 'encodedarguments')
PS_VALUE_SHORT = ('ex', 'ep', 'w', 'of', 'o', 'if', 'wd')
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
    here judges (only edits, shell calls and the lock's MCP tools are), the owner's own session, a session outside any checkout's .kit.json, a .kit.json
    that does not turn the policy on, or a role with no mode in the roles table or its role file."""
    if action is None or action.kind not in ('edit', 'shell', 'mcp'):
        return None, None
    if action.kind == 'mcp' and lock_mcp_tool(action, ctx) is None:
        return None, None                       # an MCP call that is not the lock's: not judged here
    if not getattr(action, 'agent', None) and ctx.session_role(action) == OWNER:
        return None, None                       # the owner's own session: no role, nothing read
    cwd = action.cwd or os.getcwd()
    cfg = ctx.config(cwd)
    if cfg is None or not enabled(cfg):
        return None, None
    table = kconfig.roles(cfg)
    files = kcontent.role_modes(ctx.kit)
    role = role_of(action, ctx, set(table) | set(files) | set(kcontent.ALIASES))
    named = (table.get(n, {}).get('mode') for n in kcontent.names_of(role))   # an old or new roles key
    m = next((x for x in named if x), None) or kcontent.role_mode(role, ctx.kit)
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
    if action.kind == 'mcp':
        name = lock_mcp_tool(action, ctx)
        said = MCP_VERBS.get(name)
        if said is not None and verb_allowed(list(said), LOCK_VERBS[m]):
            return None
        return _deny('role-lock', tag, 'The lock MCP tool %s (%s) is not one the %s role (mode %s) calls: its lock '
                     'verbs are %s. To see the lock state:'
                     % (name, 'machine_lock.py ' + ' '.join(said) if said else 'not a tool of the lock server',
                        role, m, verbs_text(LOCK_VERBS[m])), 'python %s status' % rules.lock_tool(ctx))
    cwd = action.cwd or os.getcwd()
    top = checkout(ctx, cwd)
    if action.kind == 'edit':
        target = edit_path(action, cwd, getattr(ctx, 'home', None))
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
    parsed = cm.read(text, rules.SHELLS.get(action.shell, sh.BASH), where or cwd, home=getattr(ctx, 'home', None))
    gap = shell_gap(parsed)
    if gap:
        name, why, instead = gap
        return _deny('role-shell', tag, '`%s` %s; a hook cannot answer an interpreter that waits for input. '
                     'Give it the script or command to run:' % (name, why), instead)
    found = lock_verb(parsed, m, ctx.kit,
                      lambda root: kit_checkout(ctx, root, top) or kit_export(ctx, root, action, cwd))
    if found:
        verb, allowed = found
        return _deny('role-lock', tag, 'machine_lock.py %s is not one of the lock verbs the %s role (mode %s) '
                     'runs (%s; never --force). To see the lock state:' % (verb, role, m, allowed),
                     'python %s status' % rules.lock_tool(ctx))
    return None


def edit_path(action, cwd, home=None):
    """The file an edit Action writes, made absolute against `cwd` as the shared rules do, `~` as
    `home` (ctx.home; None: sh.home_folder's)."""
    path = action.path
    if not isinstance(path, str) or not path:
        return None
    return sh.resolve_path(path, cwd, home=home) or path


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
    if name in cm.BASH_SHELLS:
        return 'sh'
    if cm.PYTHON_RE.fullmatch(name):
        return 'py'
    return None


def stdin_fed(cmd):
    """True when the command's standard input is a pipe, a heredoc, a here-string or a `<` file,
    not the terminal."""
    return bool(cmd.piped_in) or any(r.op in STDIN_OPS and not r.dup for r in cmd.redirects)


def _ps_key(word, names, short=(), least=3):
    """True when the PowerShell parameter `word` (lower case, `-` or `/` and any `:value` gone) is
    one of `short` or a prefix of one of `names` at least `least` letters long."""
    return word in short or (len(word) >= least and any(n.startswith(word) for n in names))


def _python_opts(args):
    """What python's own options say, read up to the operand that ends them (the script, `-`, or
    the text after -c or -m): FINITE, STDIN, (INTERACTIVE, word) or None (no operand)."""
    k = 0
    while k < len(args):
        a = args[k]
        if a == '-':
            return STDIN
        if a == '--':
            return FINITE if k + 1 < len(args) else None
        if not a.startswith('-'):
            return FINITE                       # the script; every later word is its own
        if a.startswith('--'):
            name = a.split('=', 1)[0]
            if name in PY_INFO_LONG or name.startswith('--help'):
                return FINITE
            k += 2 if name in PY_VALUE_LONG and '=' not in a else 1
            continue
        for j, ch in enumerate(a[1:], 1):       # a cluster: -Bc "...", -Wignore, -OO
            if ch in 'cm':
                return FINITE                   # the rest of the word or the next is the command
            if ch == 'i':
                return INTERACTIVE, a
            if ch in PY_INFO:
                return FINITE
            if ch in PY_VALUE:
                k += 0 if j + 1 < len(a) else 1  # -Wignore holds its value; -W ignore takes the next
                break
        k += 1
    return None


def _bash_opts(args):
    """bash's (or sh's) own options, read as _python_opts: the script or -c ends them, -s reads
    the script from standard input, -i stays on the terminal."""
    k = 0
    while k < len(args):
        a = args[k]
        if a in ('-', '--'):
            return FINITE if k + 1 < len(args) else STDIN
        if not a.startswith(('-', '+')):
            return FINITE
        if a.startswith('--'):
            if a in SH_INFO_LONG:
                return FINITE
            k += 2 if a in SH_VALUE_LONG else 1
            continue
        flags = a[1:]
        if a[0] == '-' and 'i' in flags:
            return INTERACTIVE, a
        if a[0] == '-' and 'c' in flags:
            return FINITE
        if a[0] == '-' and 's' in flags:
            return STDIN
        k += 2 if any(f in SH_VALUE for f in flags) else 1
    return None


def _ps_opts(args):
    """PowerShell's own parameters (lower case), read as _python_opts: -Command, -File and
    -EncodedCommand end them (`-` after -Command or -File: standard input), a word that is no
    parameter is the file; -NoExit (any prefix of it) and -Interactive stay open."""
    k = 0
    while k < len(args):
        a = args[k]
        if a == '-':
            return STDIN
        if a[:1] not in '-/':
            return FINITE                       # the file pwsh runs; later words are the script's
        key = a.lstrip('-/').split(':', 1)[0]
        if key and 'noexit'.startswith(key):
            return NOEXIT, a
        if _ps_key(key, ('interactive',), ('i',)):
            return INTERACTIVE, a
        if _ps_key(key, ('encodedcommand',), ('e', 'ec')):
            return FINITE
        if _ps_key(key, ('command',), ('c',)) or _ps_key(key, ('file',), ('f',), 2):
            return STDIN if k + 1 < len(args) and args[k + 1] == '-' else FINITE
        if key in PS_INFO:
            return FINITE
        k += 2 if _ps_key(key, PS_VALUE_NAMES, PS_VALUE_SHORT) else 1
    return None


def _cmd_opts(args):
    """cmd's own switches (lower case): /c (or /r) runs a command and ends them, /k stays open."""
    for a in args:
        if a.startswith(('/c', '/r')):
            return FINITE
        if a.startswith('/k'):
            return INTERACTIVE, a
        if a == '/?':
            return FINITE
    return None


OPTS = {'py': _python_opts, 'sh': _bash_opts, 'ps': _ps_opts, 'cmd': _cmd_opts}


def shell_gap(parsed):
    """(program, why, instead) for the first interpreter `parsed` starts that would wait for input,
    else None. Only the interpreter's own options count: they end at the script, the command
    (-c, -m, -Command, -File, -EncodedCommand, /c) or `-`, and every word after that belongs to
    the program run (`python -m pytest -s`, `python x.py -i`). A nested shell's own commands are
    in `parsed` too (commands.read), so `bash -c bash` is caught at the inner bash."""
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        if inv is None:
            continue
        kind = _interpreter(inv.name)
        if kind is None:
            continue
        args = inv.argv if kind in ('py', 'sh') else [a.lower() for a in inv.argv]
        instead = {'ps': '%s -NoProfile -Command "<command>"', 'cmd': '%s /c <command>'}.get(
            kind, '%s -c "<command>"') % inv.name
        said = OPTS[kind](args)
        if said == FINITE:
            continue
        if isinstance(said, tuple):
            how, word = said
            if how == NOEXIT:
                return inv.name, 'keeps PowerShell open (%s is NoExit)' % word, instead
            return inv.name, 'starts an interactive interpreter', instead
        if stdin_fed(cmd):
            continue                            # its script is the pipe, heredoc or file on stdin
        if not args or said == STDIN:
            return inv.name, 'starts an interactive interpreter', instead
        return inv.name, 'names no script, command or informational option', instead
    return None


def _python_script(args):
    """The index in python's words `args` of the script it runs, or None when it runs code (-c,
    -m, `-`) or names no operand. Read as _python_opts reads them."""
    k = 0
    while k < len(args):
        a = args[k]
        if a == '--':
            return k + 1 if k + 1 < len(args) else None
        if a == '-':
            return None
        if not a.startswith('-'):
            return k
        if a.startswith('--'):
            k += 2 if a.split('=', 1)[0] in PY_VALUE_LONG and '=' not in a else 1
            continue
        for j, ch in enumerate(a[1:], 1):
            if ch in 'cm':
                return None
            if ch in PY_VALUE:
                k += 0 if j + 1 < len(a) else 1
                break
        k += 1
    return None


# Programs that never run the words they are given, so a path naming the lock CLI there is a path
# (_lock_path_only). The kit's record, ledger and commit tools, as the program or as python's
# script, and only where the path, made absolute against the command's working directory, is
# tools/ of the kit folder, of a checkout of the kit repository or of an export of the kit
# (_kit_tool, kit_checkout, kit_export):
LOCK_PATH_SCRIPTS = frozenset(('backlog.py', 'commit.py', 'ledger.py'))
# readers, named bare (_reader), that take no command or pager (rg only without --pre). PowerShell's
# take a script block for a delay-bind parameter (-Path { ... }), which _ps_caught refuses:
LOCK_PATH_READERS = frozenset((
    'cat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'rg', 'findstr', 'ls', 'dir', 'stat',
    'type', 'get-content', 'gc', 'select-string', 'sls', 'get-childitem', 'gci', 'get-item', 'gi',
    'test-path', 'get-filehash'))
# git subcommands that only read, stage or commit a path given after the subcommand (a commit's
# message is text; its options that open an editor or ask, LOCK_GIT_SUB_DENIED, are refused):
LOCK_PATH_GIT = frozenset(('add', 'blame', 'commit', 'diff', 'log', 'ls-files', 'show', 'status'))
# PowerShell's preference variable that can set -OutVariable on every later command (_output_stays),
# matched as this prefix so a wildcard (Get-Variable PSDefault*) is caught too; and the ways to
# reach a variable by a name the line does not spell out (a *-Variable cmdlet given a wildcard, the
# variable: drive, $ExecutionContext.SessionState.PSVariable):
PS_DEFAULTS = 'psdefault'
PS_VARIABLE_CMDLETS = frozenset(('get-variable', 'gv', 'set-variable', 'sv', 'set', 'new-variable', 'nv',
                                 'clear-variable', 'clv'))
PS_VARIABLE_WORDS = ('variable:', 'psvariable')
# printers, named bare, whose output to the terminal is the lock path as text (printf -v stores it):
LOCK_PATH_PRINTERS = frozenset(('echo', 'printf', 'write-output', 'write', 'write-host'))
# Launchers the guard does not unwrap that run the program they are given (_launched): a lock path
# there passes only as the script of a python they start (`winpty python tools/machine_lock.py
# status`, `bash -c "python tools/machine_lock.py status"`), judged by its verb.
LOCK_LAUNCHERS = frozenset((
    'winpty', 'uv', 'uvx', 'hatch', 'pdm', 'poetry', 'pipenv', 'conda', 'mamba', 'setsid', 'watch', 'start',
    'bash', 'sh', 'zsh', 'dash', 'ksh', 'cmd', 'pwsh', 'powershell'))
# The program launchers among them (_launch): name -> (their options that take the next word, the
# subcommands that must come first: uv run or uv tool run). An option not listed that takes a value
# makes its value read as the program, which is no python: the call is then refused, not passed.
UV_VALUES = ('-p', '--python', '--with', '--with-editable', '--with-requirements', '--package', '--project',
             '--directory', '--extra', '--group', '--only-group', '--no-group', '--env-file', '--index',
             '--index-url', '--default-index', '--extra-index-url', '-f', '--find-links', '--from', '--cache-dir',
             '--config-file', '--color', '-C', '--config-setting')
LAUNCH_PROGRAMS = {
    'winpty': ((), ((),)), 'setsid': ((), ((),)), 'watch': (('-n', '--interval', '-q', '--equexit'), ((),)),
    'uvx': (UV_VALUES, ((),)), 'uv': (UV_VALUES, (('run',), ('tool', 'run'))),
    'hatch': (('-e', '--env', '-p', '--project', '--data-dir', '--cache-dir', '--config'), (('run',),)),
    'pdm': (('-p', '--project', '--venv', '-L', '--lockfile'), (('run',),)),
    'poetry': (('-C', '--directory', '-P', '--project'), (('run',),)),
    'pipenv': (('--python',), (('run',),)),
    'conda': (('-n', '--name', '-p', '--prefix', '--cwd'), (('run',),)),
    'mamba': (('-n', '--name', '-p', '--prefix', '--cwd'), (('run',),)),
}
# the module python -m runs as the lock CLI, matched as the last dotted part (`-m tools.machine_lock`)
LOCK_MODULE = 'machine_lock'
# a word that is a module name of the lock CLI (`tools.machine_lock`, `machine_lock`), and the
# module's name anywhere in code a python runs (`runpy.run_module('tools.machine_lock')`), not
# inside a longer name (`tests.lock.test_machine_lock`):
LOCK_MODULE_WORD_RE = re.compile(r'(?:\w+\.)*%s' % LOCK_MODULE, re.I)
LOCK_MODULE_CODE_RE = re.compile(r'(?<!\w)%s(?!\w)' % LOCK_MODULE, re.I)


def _lock_names(t):
    return rules.LOCK_SCRIPT in t.lower()


def _lock_code(t):
    """True when the text `t`, code or data, names the lock CLI: machine_lock.py anywhere, or
    the module's name standing alone (LOCK_MODULE_CODE_RE)."""
    return _lock_names(t) or bool(LOCK_MODULE_CODE_RE.search(t))


def _kit_tool(path, name, cmd, kit, kit_checkout=None, program=False):
    """True when `path`, made absolute against `cmd`'s working directory (Command.cwd: a cd earlier
    on the line is followed), is <root>/tools/<name> where <root> is the kit folder `kit` or a
    folder `kit_checkout(root)` calls a checkout of the kit repository (kit_checkout). A relative
    path whose working directory the guard cannot tell, a `program` named with no folder (the
    shell looks it up on PATH, not in the working directory), or a script of that name anywhere
    else is not it."""
    if program and '/' not in path and '\\' not in path:
        return False
    full = sh.resolve_path(path, cmd.cwd, cmd.shell, cmd.home)
    if not full:
        return False
    tools, base = os.path.split(full)
    root, folder = os.path.split(tools)
    if base.lower() != name or folder.lower() != 'tools' or not root:
        return False
    if kit and paths.norm(root) == paths.norm(kit):
        return True
    return kit_checkout is not None and bool(kit_checkout(root))


def kit_checkout(ctx, root, top):
    """True when the folder `root` is the work-tree root of a checkout (the main one or a linked
    worktree) of the repository the session's checkout `top` is in, and that repository is the
    kit's (kitlib.content.oncommit.is_kit: content/roles/ and tools/render_content.py at `root`)."""
    p, s = ctx.place(root), ctx.place(top)
    if not (p.top and p.common and s.common) or paths.norm(p.top) != paths.norm(root):
        return False
    return paths.norm(p.common) == paths.norm(s.common) and oncommit.is_kit(root)


# An export of the kit (kitlib.pluginsource): <export root>/<12 hex digits>, holding the marker
# file the export writes and the kit's version file.
EXPORT_NAME_RE = re.compile(r'[0-9a-f]{12}')
EXPORT_FILES = ('.pm-kit-export', 'kit.json')


def kit_export(ctx, root, action, cwd):
    """True when the folder `root` is an export of the kit the machine runs from: a folder named
    by 12 hex digits (EXPORT_NAME_RE) right under the machine's plugin export root (the machine
    config's plugin.export_dir, else %LOCALAPPDATA%\\pm-kit\\plugin: kitlib.pluginsource.settings),
    holding EXPORT_FILES, and neither inside nor holding a folder the session may write (its jail
    roots that are not read-only: rules.jail_roots), so a session cannot make one of its own. A
    machine config the guard cannot read, an export root it cannot name, a pin folder or any
    other place is not one."""
    from kitlib import pluginsource
    user = ctx.user()
    if not isinstance(user, dict) or user.get('error'):
        return False
    try:
        export = pluginsource.settings(user.get(pluginsource.CONFIG_KEY), ctx.environ).export_dir
    except (pluginsource.RefreshError, OSError, ValueError, TypeError):
        return False
    parent, name = os.path.split(paths.norm(root, fold=False))
    if not EXPORT_NAME_RE.fullmatch(name) or paths.norm(parent) != paths.norm(export):
        return False
    if not all(os.path.isfile(os.path.join(root, f)) for f in EXPORT_FILES):
        return False
    here = jail.real(root)
    for r in rules.jail_roots(action, ctx, cwd):
        if r.mode != jail.READ_ONLY and (jail.inside(here, r.real) or jail.inside(r.real, here)):
            return False
    return True


def _reader(inv, names=LOCK_PATH_READERS):
    """True when `inv` is a reader (LOCK_PATH_READERS, or the program set `names`) named bare, so
    found as that command (a `./grep` or `scratch/cat` is some other program), and rg without
    --pre (it runs a program on every file)."""
    if inv is None or inv.dyn or inv.name not in names or '/' in inv.path or '\\' in inv.path:
        return False
    return inv.name != 'rg' or not any(a == '--pre' or a.startswith('--pre=') for a in inv.argv)


# PowerShell's common parameters that store a command's output or stream records in a variable a
# later command reads (_ps_caught), with their aliases:
PS_VARIABLE_PARAMS = ('-outvariable', '-pipelinevariable', '-errorvariable', '-warningvariable',
                      '-informationvariable')
PS_VARIABLE_ALIASES = ('-ov', '-pv', '-ev', '-wv', '-iv')


def _ps_caught(cmd):
    """True when PowerShell may hand `cmd`'s output or items on other than down a plain pipe:
    its program is not the first word (`$c = gc x`, `& gc x`), a ( ) or { } is next to it (a
    delay-bind `-Path { ... }` runs the block for each piped item), a word holds { or }, or it
    sets a variable a later command reads (PS_VARIABLE_PARAMS: -OutVariable, -PipelineVariable,
    and -ErrorVariable, -WarningVariable and -InformationVariable, whose records hold the text the
    command was given: `Write-Host -InformationVariable c tools/machine_lock.py`), in any
    abbreviation of three letters or more. When unsure, it is caught. An assignment written with
    no spaces (`$c=gc x`) is one word to PowerShell's reader, so the command has no invocation at
    all: lock_verb refuses it as NO_PROGRAM before it gets here."""
    if cmd.shell != sh.PS:
        return False
    inv = cm.invocation(cmd)
    if inv is None or inv.index > 0 or cmd.next_op in ('(', '{') or cmd.prev_op in (')', '}'):
        return True
    if any(w.dyn and '(' in w.text for w in cmd.words):
        return True                             # a ( ) argument (shell._Scan.ps_paren_arg), -Param:(...) too, is caught as the group was
    for t in cmd.argv:
        p = t.split(':', 1)[0].lower()
        if '{' in t or '}' in t or p in PS_VARIABLE_ALIASES or len(p) >= 3 and any(
                v.startswith(p) for v in PS_VARIABLE_PARAMS):
            return True
    return False


def _ps_variable_reach(cmd):
    """True when the PowerShell command `cmd` may set a variable the line does not name: a word
    holding PS_DEFAULTS ($PSDefaultParameterValues, PSDefault*), the variable: drive or
    PSVariable, or a *-Variable cmdlet given a wildcard (Get-Variable P*s). The literal words of a
    bare reader (_reader) in no group and not caught (_ps_caught) are text it searches for or a
    file it reads, not a variable it sets: `sls PSVariable tools/machine_lock.py` passes."""
    inv = cm.invocation(cmd)
    plain = set()
    if _reader(inv) and not cmd.groups and cmd.nested_in is None and not _ps_caught(cmd):
        plain = {id(w) for w in inv.args if not w.dyn}
    texts = [w.text.lower() for w in cmd.words if id(w) not in plain]
    if any(PS_DEFAULTS in t or any(v in t for v in PS_VARIABLE_WORDS) for t in texts):
        return True
    return inv is not None and inv.name in PS_VARIABLE_CMDLETS and any(
        ch in t for t in inv.argv for ch in '*?[')


def _output_stays(cmd, commands):
    """True when `cmd`'s output reaches no program that could run it as code: it sits in no ( )
    or { } group, command substitution or nested shell, PowerShell catches it no other way
    (_ps_caught), and across the whole line no command writes a file (> or >>) and every pipe ends
    in a bare reader (_reader) that is itself in no group and not caught, and no PowerShell command
    may set a variable it does not name (_ps_variable_reach: $PSDefaultParameterValues can give
    every later command an -OutVariable, and Get-Variable PSDefault* reaches it too). The whole
    line, since a loop or an if piped into python pipes every command inside it."""
    if cmd.nested_in is not None or cmd.groups or _ps_caught(cmd):
        return False
    for c in commands:
        if any(r.writes for r in c.redirects):
            return False
        if c.shell == sh.PS and _ps_variable_reach(c):
            return False
        into = [d for d in commands if d.pipe_from is c]
        if c.piped_out and not into:
            return False                        # into a group or a block: no pipe link to read
        if c.piped_in or c.prev_op == '|':
            into.append(c)
        for d in into:
            if d.groups or d.nested_in is not None or _ps_caught(d) or not _reader(cm.invocation(d)):
                return False
    return True


# What hands a later command the words of an earlier one, or the line's own text (_last_words):
# bash's $_ (the last word of the command before; ${_}, a nameref or ${!n} to `_`) and
# $BASH_COMMAND; PowerShell's $$ and $^ (the last and first token of the line before), $Error and
# $MyInvocation (their records hold the line). With `items`, PowerShell's pipeline and match
# variables too ($_, $PSItem, $input, $Matches). A *-Variable cmdlet, the variable: drive or
# PSVariable may reach them by a name the text does not spell: _ps_variable_reach and
# _ps_names_last.
BASH_LAST_RE = re.compile(r'\$\{?!|\$\{?_(?!\w)|(?<![\w$])_(?!\w)|BASH_COMMAND')
PS_LAST_RE = re.compile(r'(?i)\$(?:\$|\^|\{[$^]\}|\{?(?:\w+:)?(?:error|myinvocation)(?!\w))')
PS_ITEM_RE = re.compile(r'(?i)\$\{?(?:_|psitem|input|matches)(?!\w)')
# a variable name a *-Variable cmdlet is given that is plainly none of those (`Get-Variable c`):
PS_PLAIN_NAME_RE = re.compile(r'[A-Za-z][\w-]*')
PS_LAST_NAMES = ('error', 'myinvocation', 'psitem', 'input', 'matches')
# git's options that run a program or write a file (an external diff, a textconv filter, --output),
# matched as rules.hub_opt_denied does (any prefix: --ext, --out), and the short -o:
LOCK_GIT_DENIED = rules.HUB_GIT_DENIED + ('--output',)
LOCK_GIT_SUB_DENIED = {'commit': rules.HUB_GIT_SUB_DENIED['commit']}


def _last_words(commands, items=False):
    """True when a command of the line may read an earlier command's words or the line's text
    (BASH_LAST_RE, PS_LAST_RE, and PS_ITEM_RE with `items`), read from each command's source as
    its shell writes it: `echo tools/machine_lock.py; python "$_" hold create x` runs the lock.
    Fail-closed: any such text anywhere on the line counts, quoted or not."""
    for c in commands:
        src = c.source or ''
        if c.shell == sh.BASH and BASH_LAST_RE.search(src):
            return True
        if c.shell == sh.PS and (PS_LAST_RE.search(src) or items and PS_ITEM_RE.search(src)
                                 or _ps_variable_reach(c) or _ps_names_last(c)):
            return True
    return False


def _ps_names_last(cmd):
    """True when the PowerShell command `cmd` is a *-Variable cmdlet (PS_VARIABLE_CMDLETS) given a
    name that may be $$, $^, $_ or the like (_last_words): one the shell fills in, or any but a
    plain name (PS_PLAIN_NAME_RE) that is not one of PS_LAST_NAMES. `Get-Variable c` is not;
    `gv '$'`, `gv ^` and `Get-Variable Error` are."""
    inv = cm.invocation(cmd)
    if inv is None or inv.name not in PS_VARIABLE_CMDLETS:
        return False
    return any(w.dyn or not w.text.startswith('-') and (
        not PS_PLAIN_NAME_RE.fullmatch(w.text) or w.text.lower() in PS_LAST_NAMES) for w in inv.args)


def _git_plain(args, g, commands):
    """True when git's words `args` (GitCall `g`) only read: global options only -C <path>,
    --no-pager and -P (no -c, --config-env, --exec-path, --git-dir: rules.hub_git_family's
    rule), none of LOCK_GIT_DENIED, -o or the subcommand's LOCK_GIT_SUB_DENIED (git commit's
    editor and prompts) before a `--`, no alias or place git cannot be told
    (g.blind), and the line sets no environment variable (commands.line_env_names: GIT_PAGER,
    GIT_EXTERNAL_DIFF, PAGER, PATH; and what git_env or home_env marks): a pager, an external
    diff or a textconv filter git starts may run the path it is given."""
    if g.alias or g.blind or getattr(g.inv.cmd, 'git_env', False) or getattr(g.inv.cmd, 'home_env', False):
        return False
    if cm.line_env_names(commands):
        return False
    first = len(args) - len(g.rest)             # the words after the subcommand
    k = 0
    while k < first - 1:
        if args[k] == '-C' and k + 1 < first - 1 and not args[k + 1].startswith('-'):
            k += 2
        elif args[k] in ('--no-pager', '-P'):
            k += 1
        else:
            return False
    head = g.argv[:g.argv.index('--')] if '--' in g.argv else g.argv
    longs, shorts = LOCK_GIT_SUB_DENIED.get(g.sub, ((), ''))
    return not any(t == '-o' or rules.hub_opt_denied(t, LOCK_GIT_DENIED)
                   or t.startswith('-') and rules.hub_opt_denied(t, longs, shorts) for t in head)


def _lock_path_only(inv, commands, kit=None, kit_checkout=None):
    """True when `inv` is a program that never runs its words (the kit's LOCK_PATH_SCRIPTS,
    LOCK_PATH_READERS, LOCK_PATH_PRINTERS but printf -v, git's LOCK_PATH_GIT subcommands with
    only plain options: _git_plain), so a word naming the lock CLI there is a path, not a lock
    call. A reader's, printer's or git's output must not reach a program (_output_stays): `cat
    tools/machine_lock.py | python - stop 42` runs the lock; nor may a later command read its
    words (_last_words: `ls tools/machine_lock.py; python "$_" stop 42`). `kit` is the kit
    folder and `kit_checkout` tells a checkout of the kit repository (_kit_tool)."""
    if inv.dyn or _last_words(commands, items=True):
        return False
    name, args = inv.name, inv.argv
    if name in LOCK_PATH_SCRIPTS:
        return _kit_tool(inv.path, name, inv.cmd, kit, kit_checkout, program=True)
    if rules.HUB_CODE_PYTHON_RE.match(name):
        k = _python_script(args)
        return k is not None and cm.base_name(args[k]) in LOCK_PATH_SCRIPTS and _kit_tool(
            args[k], cm.base_name(args[k]), inv.cmd, kit, kit_checkout)
    if name in LOCK_PATH_READERS:
        return _reader(inv) and _output_stays(inv.cmd, commands)
    if name in LOCK_PATH_PRINTERS:
        if name == 'printf' and any(a.startswith('-v') for a in args):
            return False                        # printf -v c ...: stored for a later command
        return _reader(inv, LOCK_PATH_PRINTERS) and _output_stays(inv.cmd, commands)
    g = cm.git_call(inv)
    if g is None or g.sub_dyn or g.sub not in LOCK_PATH_GIT or not _git_plain(args, g, commands):
        return False
    first = len(args) - len(g.rest)             # the words after the subcommand
    if any(_lock_names(t) and (i < first or t.startswith('-')) for i, t in enumerate(args)):
        return False                            # in an option's value: what git may run
    return _output_stays(inv.cmd, commands)


def _python_module(args):
    """(index, module) for the module python's words `args` run with -m (`-m tools.machine_lock`,
    or attached: `-mtools.machine_lock`, whose index is the option's own), or None when it runs a
    script, -c code or `-`. Read as _python_script reads them."""
    k = 0
    while k < len(args):
        a = args[k]
        if a in ('--', '-') or not a.startswith('-'):
            return None
        if a.startswith('--'):
            k += 2 if a.split('=', 1)[0] in PY_VALUE_LONG and '=' not in a else 1
            continue
        for j, ch in enumerate(a[1:], 1):
            if ch == 'c':
                return None
            if ch == 'm':
                if j + 1 < len(a):
                    return k, a[j + 1:]
                return (k + 1, args[k + 1]) if k + 1 < len(args) else None
            if ch in PY_VALUE:
                k += 0 if j + 1 < len(a) else 1
                break
        k += 1
    return None


def _lock_module(args):
    """The index in python's words `args` of the -m module that is the lock CLI (_python_module:
    its last dotted part is LOCK_MODULE), else None."""
    m = _python_module(args)
    return m[0] if m is not None and m[1].rsplit('.', 1)[-1].lower() == LOCK_MODULE else None


def _python_word(t):
    return bool(rules.HUB_CODE_PYTHON_RE.match(cm.base_name(t)))


def _fed(cmd):
    """[text] `cmd` reads on standard input from its redirects (STDIN_OPS): a heredoc's body (the
    parser keeps it as the redirect's target), a here-string's text, a `<` file's name."""
    return [r.target.text for r in cmd.redirects if r.op in STDIN_OPS and not r.dup and r.target is not None]


def _tokens(w):
    """The word `w` as _lock_seq reads it: split into its words when it holds white space."""
    return w.split() if any(c.isspace() for c in w) else [w]


def _lock_seq(inv):
    """(seq, named): the words `inv` is given that the guard reads for the lock CLI, and for each
    whether it names it. The program and its words, a word holding white space split into its
    words (_tokens: an -ArgumentList string, a title, `bash -c` text), named when it is a path
    Windows opens as machine_lock.py (rules.is_lock_script: any case, a trailing dot or space, a
    `::$DATA` stream) or it is a module name of it (LOCK_MODULE_WORD_RE: `tools.machine_lock`,
    for runpy or a later python -m), so `-m "machine_lock.py: fix"` is not; then the text of what
    it reads on standard input (_fed: a heredoc's body, a here-string, a `<` file) split the same
    way, named when it names the lock anywhere (_lock_code), since a body is code or data (`read a
    <<EOF`, `runpy.run_path('tools/machine_lock.py')`); every word after a python word is read as
    code, named when the module's name stands alone in it (LOCK_MODULE_CODE_RE: `python -c
    "...run_module('tools.machine_lock')"`, `python -m runpy machine_lock`, but not
    tests/lock/test_machine_lock.py); and the module after a python's -m whose last dotted part is LOCK_MODULE
    (_lock_module: `python -m tools.machine_lock`). A file written (`> x`) is not read: it is the
    write rules'."""
    seq, named = [], []
    for w in [inv.path] + inv.argv:
        for t in _tokens(w):
            seq.append(t)
            named.append(rules.is_lock_script(t) or bool(LOCK_MODULE_WORD_RE.fullmatch(t)))
    for text in _fed(inv.cmd):
        for t in text.split():
            seq.append(t)
            named.append(_lock_code(t))
    python = False
    for j, t in enumerate(seq):
        if python:                              # not test_machine_lock.py, as _lock_code would
            named[j] = named[j] or bool(LOCK_MODULE_CODE_RE.search(t))
        if _python_word(t):
            python = True
            k = _lock_module(seq[j + 1:])
            if k is not None:
                named[j + 1 + k] = True
    return seq, named


def _lock_words(inv, commands=(), kit=None, kit_checkout=None):
    """The words after the first one naming the lock CLI that `inv` is given (_lock_seq), or None
    when none names it or `inv` never runs its words (_lock_path_only: the kit's `backlog.py set
    --scope tools/machine_lock.py`, cat, rg, Get-Content, echo, git diff, whose output stays).
    lock_verb reads them as the lock's verb only when a launcher runs the lock (_launched, where
    that one word is the script); a call that runs it itself is read from its script or module
    (_run_words), not from an earlier word naming it; any other program given them is refused
    (HANDED_ON). Not read (a known limit): the value of
    an environment assignment (only a git line holding one is refused: _git_plain), code that
    builds the name (python -c "...'machine_' + 'lock'..."), a glob the shell expands to it
    (tools/machine_lock.p?: only the base name is matched), a Windows 8.3 short name
    (tools/MACHIN~1.PY), a name the shell or PowerShell builds from pieces (`python
    "tools/machine_$('lock').py"`, `$p = 'tools/machine_' + 'lock.py'`, bash's `${p}lock.py`), and
    outside a launcher's unread line (_hidden_launch) the name glued to other text in one word
    (`--file=machine_lock.py`). `commands` is the line's commands, for the pipes; `kit` and
    `kit_checkout` as _lock_path_only."""
    seq, named = _lock_seq(inv)
    k = next((i for i, n in enumerate(named) if n), None)
    if k is None or _lock_path_only(inv, commands, kit, kit_checkout):
        return None
    return seq[k + 1:]


def _word_starts(inv):
    """[q]: the index in _lock_seq's words of the first token (_tokens) of `inv`'s word q, its
    program being word 0; the last entry is the count of those tokens."""
    starts = [0]
    for w in [inv.path] + inv.argv:
        starts.append(starts[-1] + len(_tokens(w)))
    return starts


def _run_words(inv):
    """The words (_lock_seq) given to the lock CLI that `inv` runs itself, or None when it runs
    none: machine_lock.py (in any spelling Windows opens as it: rules.is_lock_script) is its
    program, or python's script (_python_script), or python runs it as a module (_lock_module).
    They are the words after that script or module, never after an earlier word naming the lock:
    python's own options and their values come first, so `python -X machine_lock -X status
    tools/machine_lock.py stop 42` runs stop. None too (fail-closed: lock_verb refuses it as
    HANDED_ON) when no token of that word names the lock. A launcher left unwrapped (winpty, uv
    run) is not; it is _launched's."""
    if rules.is_lock_script(inv.path):
        q = 0
    elif rules.HUB_CODE_PYTHON_RE.match(inv.name):
        k = _python_script(inv.argv)
        if k is None or not rules.is_lock_script(inv.argv[k]):
            k = _lock_module(inv.argv)
        if k is None:
            return None
        q = k + 1
    else:
        return None
    seq, named = _lock_seq(inv)
    starts = _word_starts(inv)
    j = next((j for j in range(starts[q], starts[q + 1]) if named[j]), None)
    return None if j is None else seq[j + 1:]


LINE, STDIN_BODY, PROGRAM = 'line', 'stdin', 'program'


def _skip_opts(words, k, values):
    """The index of the first word from words[k] that is not an option (`--` skipped); an option
    of `values` takes the next word (written --opt=value, it holds it)."""
    while k < len(words):
        a = words[k]
        if a == '--':
            return k + 1
        if not a.startswith('-') or a == '-':
            return k
        k += 2 if a in values else 1
    return k


def _launch(words, i, titled=False):
    """How the launcher words[i] (LOCK_LAUNCHERS) runs what it is given, read from its own options
    as the shell, cmd or PowerShell reads them: (LINE, lo, hi) when words[lo:hi] are the command
    line it runs (bash -c's text, and nothing after it: those words are $0, $1, ...; everything
    after cmd's /c or PowerShell's -Command), (STDIN_BODY,) when it runs what it reads on standard
    input, (PROGRAM, q) when words[q] is the program it runs with the words after it (winpty, uv
    run, poetry run and the like, after their own options: LAUNCH_PROGRAMS); None when it runs a
    script file (bash x.sh, pwsh x.ps1, -File, -EncodedCommand) or the guard cannot tell (an
    option it does not know that takes a value). `titled`: a bash `start`'s first word is quoted,
    so it is the window title."""
    name = cm.base_name(words[i])
    if name in cm.BASH_SHELLS:
        k, run_c, run_s = i + 1, False, False
        while k < len(words):
            a = words[k]
            if a in ('-', '--'):
                k += 1
                break
            if len(a) < 2 or a[0] not in '-+':
                break
            if a.startswith('--'):
                k += 2 if a in SH_VALUE_LONG else 1
                continue
            if a[0] == '-':
                run_c, run_s = run_c or 'c' in a[1:], run_s or 's' in a[1:]
            k += 2 if any(f in SH_VALUE for f in a[1:]) else 1
        if run_c:
            return (LINE, k, k + 1) if k < len(words) else None
        return (STDIN_BODY,) if run_s or k >= len(words) else None
    if name == 'cmd':
        for k in range(i + 1, len(words)):
            a = words[k].lower()
            if a in ('/c', '/r', '//c', '//r'):
                return LINE, k + 1, len(words)
            if not a.startswith('/'):
                return None
        return None
    if name in cm.PS_SHELLS:
        k = i + 1
        while k < len(words):
            a = words[k]
            if a == '-':
                return (STDIN_BODY,)
            if a[:1] not in ('-', '/'):
                return (LINE, k, len(words)) if name == 'powershell' else None   # pwsh: a file
            key = a.lower().lstrip('-/').split(':', 1)[0]
            if _ps_key(key, ('command',), ('c',)):
                return (STDIN_BODY,) if words[k + 1:k + 2] == ['-'] else (LINE, k + 1, len(words))
            if _ps_key(key, ('encodedcommand',), ('e', 'ec')) or _ps_key(key, ('file',), ('f',), 2):
                return None
            k += 2 if _ps_key(key, PS_VALUE_NAMES, PS_VALUE_SHORT) else 1
        return (STDIN_BODY,)
    if name == 'start':
        k = i + 1 + (1 if titled else 0)
        while k < len(words) and (words[k] == '' or words[k].startswith('/')):
            k += 2 if words[k].lower() == '/d' else 1
        return (PROGRAM, k) if k < len(words) else None
    if name not in LAUNCH_PROGRAMS:
        return None
    values, subs = LAUNCH_PROGRAMS[name]
    for sub in subs:
        k = _skip_opts(words, i + 1, values)
        for s in sub:
            if k >= len(words) or words[k] != s:
                break
            k = _skip_opts(words, k + 1, values)
        else:
            return (PROGRAM, k) if k < len(words) else None
    return None


def _launched(inv, commands):
    """True when `inv` is a launcher (LOCK_LAUNCHERS, named by a literal word) whose words name
    the lock CLI once (_lock_seq), as the script or -m module of the python it runs (_launch):
    `winpty python tools/machine_lock.py status`, `uv run python ...`, `bash -c "python
    tools/machine_lock.py status"`, a `bash <<'EOF'` body; and whose output stays (_output_stays).
    Then the words after it are the lock's, judged by their verb. A python word counts only when
    it is the program launched: the words after a bash -c text are its $0, $1, ..., so `bash -c
    'python "$1" hold create x' python tools/machine_lock.py status` hands the path on. In a
    command line the guard reads as nested commands (bash -c, cmd /c, pwsh -Command: the commands
    nested in `inv`'s, each judged on its own) the python may stand anywhere before it (`cd tools
    && python machine_lock.py status`); in one the guard does not read (a shell a program
    launcher starts: `uv run sh -c '...'`) it must open the line. A second word naming it, or
    one in any other place, is a path handed on."""
    if inv.dyn or inv.name not in LOCK_LAUNCHERS:
        return False
    seq, named = _lock_seq(inv)
    ks = [i for i, n in enumerate(named) if n]
    if len(ks) != 1:
        return False
    k = ks[0]
    words = [inv.path] + inv.argv
    starts = _word_starts(inv)                  # starts[q]: words[q]'s first token in seq
    i, read = 0, any(c.nested_in is inv.cmd for c in commands)   # $( ) in its words too: see LINE
    while True:
        how = _launch(words, i, i == 0 and bool(inv.args) and cm.quoted(inv.cmd, inv.args[0]))
        if how is None:
            return False
        if how[0] == LINE:                     # a word the shell fills in is not read (only flagged)
            read = read and not any(w.dyn for w in inv.args[how[1] - 1:how[2] - 1])
            lo, hi, strict = starts[how[1]], starts[how[2]], not read
        elif how[0] == STDIN_BODY:
            lo, hi, strict = starts[-1], len(seq), not read
        else:
            q = how[1]
            lo, hi, strict = starts[q], starts[-1], True
            if starts[q + 1] - lo != 1:
                return False                    # the program word holds white space
            if not _python_word(seq[lo]) and cm.base_name(words[q]) in LOCK_LAUNCHERS:
                i, read = q, False              # uv run sh -c ...: that launcher's words, unread
                continue
        break
    if not lo <= k < hi:
        return False
    if not any(_python_word(seq[j]) and k - j - 1 in (_python_script(seq[j + 1:hi]), _lock_module(seq[j + 1:hi]))
               for j in ([lo] if strict else range(lo, k))):
        return False
    return _output_stays(inv.cmd, commands)


def _hidden_launch(inv, commands):
    """True when `inv` is a launcher (LOCK_LAUNCHERS, named by a literal word) whose command line
    the guard does not read as nested commands (a shell a program launcher starts, `uv run sh -c
    '...'`, a script file, an option it cannot place) and one of whose words (_lock_seq) holds the
    lock CLI's name glued to other text, which _lock_seq does not count as naming it: `uv run sh -c
    'f=tools/machine_lock.py; python $f stop 42'`, `setsid sh -c "cat tools/machine_lock.py|python
    - stop 42"`. The name is matched standing alone (LOCK_MODULE_CODE_RE), so
    tests/lock/test_machine_lock.py is not it. Fail-closed: lock_verb refuses it as HANDED_ON."""
    if inv.dyn or inv.name not in LOCK_LAUNCHERS or any(c.nested_in is inv.cmd for c in commands):
        return False
    return any(LOCK_MODULE_CODE_RE.search(t) for t in _lock_seq(inv)[0])


# The verb named for a command that runs no program the guard can name (an assignment alone, a
# PowerShell expression) whose words name machine_lock.py: lock_verb refuses it in every mode.
NO_PROGRAM = '(in a command that names no program)'
# The verb named for a lock path given to a program that neither runs it (_run_words, _launched)
# nor only reads or prints it (_lock_path_only), so may copy, store or pass it on: lock_verb
# refuses it in every mode.
HANDED_ON = '(its path handed on to another program)'


def lock_verb(parsed, m, kit=None, kit_checkout=None):
    """(verb, allowed) for the first lock CLI call whose verb (and sub-verb: `hold agent`) mode
    `m` may not run (LOCK_VERBS), or that forces; None when every one is allowed. `allowed` is the
    mode's verbs as text. Only the words before `--` are the lock's (after it, `run`'s command).
    A command with no invocation (an assignment alone, `$c=gc tools/machine_lock.py`, which
    PowerShell reads as one word with no program the guard can name; `$a = 'tools/...','stop'`
    for splatting) whose words or redirects name machine_lock.py is refused as NO_PROGRAM, in any
    mode: fail-closed, since its value may reach a program later on the line. Fail-closed too, a
    command whose words, heredoc body, here-string or redirect name it (_lock_seq) passes only
    when its program only reads or prints it with its output staying (_lock_path_only), or runs
    it (_run_words, _launched: then judged by its verb); any other program - cp, Copy-Item, ln,
    read, Set-Variable, `$c = echo ...`, xargs, python - - is refused as HANDED_ON. `kit` is the
    kit folder, and `kit_checkout(root)` tells whether a folder is a checkout of the kit
    repository; the tools/backlog.py and tools/commit.py under either are exempt (_kit_tool)."""
    table = LOCK_VERBS[m]
    allowed = verbs_text(table)
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        if inv is None:
            if any(_lock_code(t) for t in [w.text for w in cmd.words] + _fed(cmd)):
                return NO_PROGRAM, allowed
            continue
        words = _lock_words(inv, parsed.commands, kit, kit_checkout)
        if words is None:
            if _hidden_launch(inv, parsed.commands):
                return HANDED_ON, allowed
            continue
        run = _run_words(inv)
        if run is None and not _launched(inv, parsed.commands) or _last_words(parsed.commands):
            return HANDED_ON, allowed           # $_ after `... tools/machine_lock.py`: its path again
        words = words if run is None else run   # a python option's value naming the lock is no verb
        head = words[:words.index('--')] if '--' in words else words
        said, k = [], 0
        while k < len(head):
            t = head[k]
            if t.startswith('-'):
                key = t.split('=', 1)[0]
                if len(key) > 2 and key.startswith('--') and key not in REAL_NOT_FORCE and '--force'.startswith(key):
                    return '%s %s' % (said[0] if said else '(no verb)', t), allowed
                k += 2 if t in rules.LOCK_VALUE_OPTS else 1
                continue
            said.append(t)
            k += 1
        if not verb_allowed(said, table):
            return (said[0] if said and said[0] not in table else ' '.join(said[:2]) or '(no verb)'), allowed
    return None


def verb_allowed(said, table):
    """True when the lock words `said` ([verb, sub-verb, ...]) are a verb of the LOCK_VERBS entry
    `table`, with a sub-verb it allows."""
    if not said or said[0] not in table:
        return False
    subs, sub = table[said[0]], (said[1] if len(said) > 1 else '')
    return subs is None or sub in subs


def lock_mcp_tool(action, ctx):
    """The lock MCP tool an MCP Action calls, or None: any tool under the lock's prefix
    (rules.mcp_prefix: the machine config's, else the host profile's mcp_tool_format), or a tool
    of the lock server (MCP_VERBS) under another server name (rules.host_mcp_separator)."""
    tool = action.tool or ''
    if action.kind != 'mcp' or not tool:
        return None
    prefix = rules.mcp_prefix(ctx)
    if prefix and tool.startswith(prefix) and len(tool) > len(prefix):
        return tool[len(prefix):]
    sep = rules.host_mcp_separator(ctx.host_profile())
    tail = tool.rsplit(sep, 1)[-1] if sep else tool
    return tail if tail in MCP_VERBS else None


def rules_for(m):
    """[(rule, text)] of the RULES rows that hold mode `m`, in order."""
    return [(rule, text) for rule, modes, text in RULES if m in modes]


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
    if m is None or action.kind == 'mcp':
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
    target = edit_path(action, cwd, getattr(ctx, 'home', None))
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
