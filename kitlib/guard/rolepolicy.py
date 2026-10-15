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
    role-lock     the lock CLI (a command word naming machine_lock.py, but in a program that
                  never runs its words - backlog.py and commit.py under tools/ of the kit folder
                  or of a checkout of the kit repository, a bare reader whose output reaches no
                  program, git diff: _lock_words; a name inside an environment assignment's
                  value, python -c code or a `<` redirect's source is not read) run with a
                  verb outside the mode's LOCK_VERBS, or with --force in any
                  abbreviation; any command with no program the guard can name (an assignment
                  alone, `$c=gc ...`) whose words name machine_lock.py (lock_verb NO_PROGRAM);
                  a lock MCP tool (lock_mcp_tool) whose verb
                  (MCP_VERBS) is outside them, or that the lock server does not have
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
from kitlib.content import oncommit
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
    found = lock_verb(parsed, m, ctx.kit, lambda root: kit_checkout(ctx, root, top))
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
# (_lock_path_only). The kit's record and commit tools, as the program or as python's script, and
# only where the path, made absolute against the command's working directory, is tools/ of the kit
# folder or of a checkout of the kit repository (_kit_tool):
LOCK_PATH_SCRIPTS = frozenset(('backlog.py', 'commit.py'))
# readers, named bare (_reader), that take no command or pager (rg only without --pre). PowerShell's
# take a script block for a delay-bind parameter (-Path { ... }), which _ps_caught refuses:
LOCK_PATH_READERS = frozenset((
    'cat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'rg', 'findstr', 'ls', 'dir', 'stat',
    'type', 'get-content', 'gc', 'select-string', 'sls', 'get-childitem', 'gci', 'get-item', 'gi',
    'test-path', 'get-filehash'))
# git subcommands that only read or stage a path given after the subcommand:
LOCK_PATH_GIT = frozenset(('add', 'blame', 'diff', 'log', 'ls-files', 'show', 'status'))
# PowerShell's preference variable that can set -OutVariable on every later command (_output_stays):
PS_DEFAULTS = 'psdefaultparametervalues'


def _lock_names(t):
    return rules.LOCK_SCRIPT in t.lower()


def _kit_tool(path, name, cmd, kit, kit_checkout=None, program=False):
    """True when `path`, made absolute against `cmd`'s working directory (Command.cwd: a cd earlier
    on the line is followed), is <root>/tools/<name> where <root> is the kit folder `kit` or a
    folder `kit_checkout(root)` calls a checkout of the kit repository (kit_checkout). A relative
    path whose working directory the guard cannot tell, a `program` named with no folder (the
    shell looks it up on PATH, not in the working directory), or a script of that name anywhere
    else is not it."""
    if program and '/' not in path and '\\' not in path:
        return False
    full = sh.resolve_path(path, cmd.cwd, cmd.shell)
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


def _reader(inv):
    """True when `inv` is a reader (LOCK_PATH_READERS) named bare, so found as that command (a
    `./grep` or `scratch/cat` is some other program), and rg without --pre (it runs a program on
    every file)."""
    if inv is None or inv.dyn or inv.name not in LOCK_PATH_READERS or '/' in inv.path or '\\' in inv.path:
        return False
    return inv.name != 'rg' or not any(a == '--pre' or a.startswith('--pre=') for a in inv.argv)


def _ps_caught(cmd):
    """True when PowerShell may hand `cmd`'s output or items on other than down a plain pipe:
    its program is not the first word (`$c = gc x`, `& gc x`), a ( ) or { } is next to it (a
    delay-bind `-Path { ... }` runs the block for each piped item), a word holds { or }, or it
    sets -OutVariable or -PipelineVariable (a later command reads the variable). When unsure, it
    is caught. An assignment written with no spaces (`$c=gc x`) is one word to PowerShell's
    reader, so the command has no invocation at all: lock_verb refuses it as NO_PROGRAM before it
    gets here."""
    if cmd.shell != sh.PS:
        return False
    inv = cm.invocation(cmd)
    if inv is None or inv.index > 0 or cmd.next_op in ('(', '{') or cmd.prev_op in (')', '}'):
        return True
    for t in cmd.argv:
        p = t.split(':', 1)[0].lower()
        if '{' in t or '}' in t or p in ('-ov', '-pv') or len(p) >= 3 and (
                '-outvariable'.startswith(p) or '-pipelinevariable'.startswith(p)):
            return True
    return False


def _output_stays(cmd, commands):
    """True when `cmd`'s output reaches no program that could run it as code: it sits in no ( )
    or { } group, command substitution or nested shell, PowerShell catches it no other way
    (_ps_caught), and across the whole line no command writes a file (> or >>) and every pipe ends
    in a bare reader (_reader) that is itself in no group and not caught, and no PowerShell command
    names $PSDefaultParameterValues (it can give every later command an -OutVariable). The whole
    line, since a loop or an if piped into python pipes every command inside it."""
    if cmd.nested_in is not None or cmd.groups or _ps_caught(cmd):
        return False
    for c in commands:
        if any(r.writes for r in c.redirects):
            return False
        if c.shell == sh.PS and any(PS_DEFAULTS in w.text.lower() for w in c.words):
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


def _lock_path_only(inv, commands, kit=None, kit_checkout=None):
    """True when `inv` is a program that never runs its words (the kit's LOCK_PATH_SCRIPTS,
    LOCK_PATH_READERS, git's LOCK_PATH_GIT subcommands), so a word naming the lock CLI there is a
    path, not a lock call. A reader's or git's output must not reach a program (_output_stays):
    `cat tools/machine_lock.py | python - stop 42` runs the lock. `kit` is the kit folder and
    `kit_checkout` tells a checkout of the kit repository (_kit_tool)."""
    if inv.dyn:
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
    g = cm.git_call(inv)
    if g is None or g.sub_dyn or g.sub not in LOCK_PATH_GIT:
        return False
    first = len(args) - len(g.rest)             # the words after the subcommand
    if any(_lock_names(t) and (i < first or t.startswith('-')) for i, t in enumerate(args)):
        return False                            # in -c or an option's value: config git may run
    return _output_stays(inv.cmd, commands)


def _lock_words(inv, commands=(), kit=None, kit_checkout=None):
    """The words a lock CLI call hands machine_lock.py, or None when `inv` runs none. Fail-closed:
    a command word naming machine_lock.py - the program, python's script, an argument of a
    launcher (winpty, uv run, setsid, watch, Git Bash's start, Start-Process cmd -ArgumentList
    "/c ...") or of a script - is a lock call, and the words after it are its words, except in a
    program that never runs its words (_lock_path_only: the kit's `backlog.py set --scope
    tools/machine_lock.py`, cat, rg, Get-Content, git diff). Not read (a known limit): the value
    of an environment assignment (GIT_PAGER='python tools/machine_lock.py stop 42' git log,
    GIT_EXTERNAL_DIFF=...), a name inside python -c code (run(['python',
    'tools/machine_lock.py', 'stop', '42'])) and a file named only as a `<` redirect's source
    (python - stop 42 < tools/machine_lock.py, bash's "$(<tools/machine_lock.py)") and a glob the
    shell expands to it (tools/machine_lock.p?: only the base name is matched). `commands` is
    the line's commands, for the pipes; `kit` and `kit_checkout` as _lock_path_only."""
    seq = []
    for w in [inv.path] + inv.argv:
        seq.extend(w.split() if any(c.isspace() for c in w) else [w])  # an -ArgumentList string, a title
    k = next((i for i, t in enumerate(seq) if cm.base_name(t) == rules.LOCK_SCRIPT), None)
    if k is None or _lock_path_only(inv, commands, kit, kit_checkout):
        return None
    return seq[k + 1:]


# The verb named for a command that runs no program the guard can name (an assignment alone, a
# PowerShell expression) whose words name machine_lock.py: lock_verb refuses it in every mode.
NO_PROGRAM = '(in a command that names no program)'


def lock_verb(parsed, m, kit=None, kit_checkout=None):
    """(verb, allowed) for the first lock CLI call whose verb (and sub-verb: `hold agent`) mode
    `m` may not run (LOCK_VERBS), or that forces; None when every one is allowed. `allowed` is the
    mode's verbs as text. Only the words before `--` are the lock's (after it, `run`'s command).
    A command with no invocation (an assignment alone, `$c=gc tools/machine_lock.py`, which
    PowerShell reads as one word with no program the guard can name; `$a = 'tools/...','stop'`
    for splatting) whose words name machine_lock.py is refused as NO_PROGRAM, in any mode:
    fail-closed, since its value may reach a program later on the line. `kit` is the kit folder,
    and `kit_checkout(root)` tells whether a folder is a checkout of the kit repository; the
    tools/backlog.py and tools/commit.py under either are exempt (_kit_tool)."""
    table = LOCK_VERBS[m]
    allowed = verbs_text(table)
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        if inv is None:
            if any(_lock_names(w.text) for w in cmd.words):
                return NO_PROGRAM, allowed
            continue
        words = _lock_words(inv, parsed.commands, kit, kit_checkout)
        if words is None:
            continue
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
