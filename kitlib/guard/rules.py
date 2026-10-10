"""The guard's rules (ARCHITECTURE.md "Guard rules"; docs/guard.md "Rules").

decide(event, ctx) -> Decision or None (allow). Every rule is a read-only check of real state
and every deny names the exact command to use instead. A deny reason ends with the tag
`[kit guard: rule=<rule> role=<role>]`; claude.guard_selftest reads those tags back out of
subagent transcripts.

Rules, in the order they are checked:
  lock-mcp         the lock MCP's owner-only tools: a subagent or a launcher-marked session is
                   denied; an unmarked main session (the owner's own) is allowed
  owner-lock       a shell command running reserve_ui, or running a lock CLI owner-only write
                   (reserve, release, hold whole|half|slots, division --set, game on|off|set,
                   top, stop/unbook --force), in any session but the owner's own
  ledger           an edit of the machine config's ledger_path, by anyone
  lock-state       an agent session's edit of a file in the machine lock's state folder
  kit-json         an agent session's edit of a .kit.json in a checkout
  main-checkout    an edit in a main checkout by a role whose edit_main_checkout is false
  write-jail       an agent session's write outside its own checkout, the temp folder and the
                   .kit.json data roots (kitlib.guard.jail); git that writes a checkout outside them
  wrapped-gate     gate.py or `machine_lock.py run` piped into another command (P16)
  root-search      a recursive search from a drive or filesystem root
  heavy            a heavy program run with no lock held (MACHINE_LOCK_RUN verified by nest_check)
  git-*            damaging git in a main checkout; branch -D anywhere; a push to main with no
                   gate-OK record when the repo sets repo.push_block; push --no-verify, send-pack,
                   http-push, a remote helper or a git alias for one in a repository with a PM
                   data record, and any push from a copy of PM data without one; a remote
                   repointed (git-remote-rewrite) in a repository with one; all in any session
                   but the owner's own
  git-hooks-path   a change of core.hooksPath (git config, git -c, clone -c, GIT_CONFIG_*), in
                   any session
  owner-identity   the owner's email given to a commit, in any session but the owner's own
  kill-by-name     an agent session ending processes by name (taskkill /IM, Stop-Process -Name)
  heredoc-winpath  python reading a bash heredoc with a single-backslash Windows path in a string
  busy-wait        a foreground sleep loop with no counter, clock or timeout bounding it
A command line the parser cannot read cleanly also goes through kitlib.guard.fallback.
"Agent session" and "the owner's own" are the session mark's (docs/guard.md "Session mark").
"""
import ntpath
import os
import posixpath
import re

from kitlib import config as kconfig
from kitlib import pmdata
from kitlib.git import repo

from . import commands as cm
from . import context
from . import jail
from . import shell as sh

DENY, ASK = 'deny', 'ask'
FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
              'NotebookEdit': 'notebook_path'}
SHELL_TOOLS = {'Bash': sh.BASH, 'PowerShell': sh.PS}

# The lock MCP's tool-name prefix as the kit plugin would register it. INFERRED, not observed:
# the kit has no installed plugin yet. ~/.claude-pm-kit/config.json guard_mcp_prefix replaces it.
MCP_PREFIX = 'mcp__plugin_claude-pm-kit_machine-lock__'
# The live machine_lock.py still requires --lock on `run`; ARCHITECTURE.md retires the flag
# for the kit's own tool. Alternatives carry it until the kit tool is the one installed.
LOCK_TOOL = '%s/.claude/tools/machine_lock/machine_lock.py'
LOCK_RUN_FLAGS = '--lock'
TAG = '[kit guard: rule=%s role=%s]'
NULL_TARGETS = {'/dev/null', '$null', 'nul', 'nul:', '&1', '&2'}


class Decision(object):
    __slots__ = ('kind', 'rule', 'reason')

    def __init__(self, kind, rule, reason):
        self.kind, self.rule, self.reason = kind, rule, reason

    def __repr__(self):
        return 'Decision(%s, %s)' % (self.kind, self.rule)


def tag(rule, role):
    return TAG % (rule, role)


def refuse(rule, role, what, instead, kind=DENY):
    return Decision(kind, rule, '%s Instead: %s %s' % (what.rstrip(), instead.strip(), tag(rule, role)))


def fwd(path):
    return path.replace('\\', '/')


def quote(path):
    return '"%s"' % fwd(path)


def tag_role(event):
    """'main' when the event has no agent_id, else 'subagent' (the tag the self-test reads)."""
    return 'subagent' if (event or {}).get('agent_id') else 'main'


# ---------------------------------------------------------------- entry

def decide(event, ctx=None):
    ctx = ctx or context.Ctx()
    tool = (event or {}).get('tool_name') or ''
    ti = (event or {}).get('tool_input') or {}
    cwd = (event or {}).get('cwd') or os.getcwd()
    role = tag_role(event)
    d = lock_mcp(event, ctx, tool, role)
    if d:
        return d
    if tool in FILE_TOOLS:
        target = ti.get(FILE_TOOLS[tool])
        if not isinstance(target, str) or not target:
            return None
        target = sh.resolve_path(target, cwd) or target
        return edit_target(event, ctx, target, cwd, role, '%s of %s' % (tool, target),
                           rewrite=lambda path: '%s(%s="%s")' % (tool, FILE_TOOLS[tool], fwd(path)))
    if tool in SHELL_TOOLS:
        text = ti.get('command')
        if not isinstance(text, str) or not text.strip():
            return None
        return shell_command(event, ctx, text, SHELL_TOOLS[tool], cwd, role)
    return None


def shell_command(event, ctx, text, shell, cwd, role):
    parsed = cm.read(text, shell, cwd)
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        for rule in (owner_lock, wrapped_gate, root_search, heavy, git_rules, git_hooks_path, owner_identity,
                     kill_by_name, heredoc_winpath, shell_writes):
            d = rule(event, ctx, cmd, inv, cwd, role)
            if d:
                return d
    d = busy_wait(event, ctx, parsed, cwd, role)
    if d:
        return d
    if not parsed.clean:
        from . import fallback
        return fallback.check(event, ctx, text, cwd, role, parsed)
    return None


# ---------------------------------------------------------------- lock MCP

def mcp_prefix(ctx):
    return ctx.user().get('guard_mcp_prefix') or MCP_PREFIX


def mcp_owner_tool(ctx, tool):
    """The owner-only lock MCP tool `tool` names, or None: under the configured prefix, under any
    other server name (`mcp__<server>__<tool>`), or listed in full in guard_mcp_tools."""
    if not tool.startswith('mcp__'):
        return None
    from kitlib.lockmcp import server as lockmcp   # here, not at the top: only mcp__ calls pay its import
    prefix = mcp_prefix(ctx)
    if tool.startswith(prefix) and tool[len(prefix):] in lockmcp.OWNER_ONLY:
        return tool[len(prefix):]
    tail = tool.rsplit('__', 1)[-1]
    if tail in lockmcp.OWNER_ONLY:
        return tail                             # the same tool under another server name
    if tool in (ctx.user().get('guard_mcp_tools') or ()):
        return tool
    return None


def lock_mcp(event, ctx, tool, role):
    name = mcp_owner_tool(ctx, tool)
    if not name:
        return None
    who = ctx.session_role(event)
    if who == context.OWNER:
        return None                             # a session nothing marks is the owner's own
    what = '%s changes the machine lock and is the owner\'s alone (ARCHITECTURE.md "Guard rules").' % name
    status = 'python %s status' % (LOCK_TOOL % fwd(ctx.home))
    return refuse('lock-mcp', role, what + ' This session is %s. Ask the owner (through the PM) to make the change; '
                  'the read tools (lock_snapshot, lock_run, lock_hold) stay open. To see the lock state:' % who, status)


def is_owner_session(event, ctx):
    """True for the owner's own main session: no agent_id and no launcher mark (docs/guard.md
    "Session mark")."""
    return not (event or {}).get('agent_id') and ctx.session_role(event) == context.OWNER


def agent_session(event, ctx):
    """True for a subagent, or a session whose mark is one of guard_agent_marks (default:
    coordinator), case ignored. The rules only agents are held to (kill by name, lock state,
    kit-json, the write jail) use it."""
    if (event or {}).get('agent_id'):
        return True
    marks = ctx.user().get('guard_agent_marks') or AGENT_MARKS
    return str(ctx.session_role(event) or '').lower() in {str(m).lower() for m in marks}


AGENT_MARKS = ('coordinator', 'conductor')  # 'conductor' is the old mark, until the launcher is renamed


# ---------------------------------------------------------------- owner lock tools

# The lock's own owner check reads process ancestry, which a detached process can forge, so agents do
# not run the owner's tools. reserve_ui is denied when it is run, never when it is read or handled by git.
# The window's package, kitlib.lockui, counts too, so python -c cannot import and start it.
RESERVE_UI_RE = re.compile(r'(?i)(?<![A-Za-z0-9_])(?:reserve_ui(?:\.pyw?)?|kitlib(?:[./\\]|\s+import\s+)lockui)'
                           r'(?![A-Za-z0-9_])')
UI_NAME_RE = re.compile(r'(?i)^reserve_ui(?:\.pyw?)?$')
OPENERS = ('invoke-item', 'ii', 'explorer')     # open a file through its association: runs a .pyw
# the interpreters this rule knows: commands.PYTHON_RE plus the pyw launcher
UI_PYTHON_RE = re.compile(r'^(python|pythonw|py|pyw)(\d+(\.\d+)*)?$')
UI_RUN_MODULES = ('runpy',)                      # python -m modules that run a file named after them
LOCK_SCRIPT = 'machine_lock.py'
# options of either lock CLI that take a value, so their value is not read as a subcommand
LOCK_VALUE_OPTS = {'--test-root', '--test-hardware', '--test-kinds', '--test-actor', '--kind', '--repo',
                   '--slug', '--cores', '--hold', '--reservation', '--gpu-share', '--on-release',
                   '--max-wait', '--expect', '--note', '--booking', '--detached-id', '--from', '--until',
                   '--date', '--for', '--cpu', '--gpu', '--set'}
OWNER_LOCK_WHY = ('owner-only lock tools are run by the owner, not agents; the lock\'s own owner check stays '
                  'as a second layer. Ask the owner to make the change. To see the lock state:')


def owner_lock_refusal(ctx, role, what):
    return refuse('owner-lock', role, '%s, the owner\'s: %s' % (what, OWNER_LOCK_WHY),
                  'python %s status' % (LOCK_TOOL % fwd(ctx.home)))


def _ui_word(w):
    return not w.dyn and bool(UI_NAME_RE.match(ntpath.basename(w.text.replace('/', '\\'))))


def _reads_stdin(cmd):
    """True when the command's standard input is a pipe or a `<` redirect."""
    return bool(cmd.piped_in) or any(r.op == '<' for r in cmd.redirects)


def runs_reserve_ui(cmd, inv):
    """True when `inv` runs reserve_ui: as its program (a direct path, &, Start-Process, cmd
    start), as the script of python, pythonw, py or pyw (also through start or Start-Process),
    as python's standard input (`python < reserve_ui.pyw`, `cat reserve_ui.pyw | python`), or
    opened by Invoke-Item or explorer. A program, script or opened file named by a variable
    counts when the command's text names reserve_ui (`$p = "reserve_ui.pyw"; pythonw $p`), and
    so do python -c with reserve_ui or kitlib.lockui in its arguments and python -m reserve_ui
    or -m runpy.
    Reads, git and other -m modules are left alone."""
    if not RESERVE_UI_RE.search(cmd.source or ''):
        return False
    name, prog, args = inv.name, inv.word, list(inv.args)
    if name == 'start' and inv.via != 'start-process':  # Git Bash's start: [/opts] ["title"] prog
        while args and (args[0].text.startswith('/') or not args[0].text):
            args.pop(0)
        if not args:
            return False
        prog, args = args[0], args[1:]
        name = cm.base_name(prog.text)
    if prog is not None and (_ui_word(prog) or prog.dyn):
        return True
    if name in OPENERS:
        return any(_ui_word(a) or a.dyn for a in args)
    if not UI_PYTHON_RE.match(name):
        return False
    k = 0
    while k < len(args):
        t = args[k].text
        if t == '-':
            return _reads_stdin(cmd)
        if t.startswith('--'):
            k += 1
            continue
        if t.startswith('-'):
            # a cluster of short flags (`-um`, `-Sc`): python reads it letter by letter, and -c, -m,
            # -X or -W take the rest of the word, or the next word, as their value
            for j in range(1, len(t)):
                ch = t[j]
                if ch not in 'cmXW':
                    continue
                rest = t[j + 1:]
                nxt = args[k + 1] if k + 1 < len(args) else None
                if ch == 'c':
                    return bool(RESERVE_UI_RE.search(rest)) or any(RESERVE_UI_RE.search(a.text) for a in args[k + 1:])
                if ch == 'm':
                    if rest:
                        return args[k].dyn or bool(UI_NAME_RE.match(rest)) or rest.lower() in UI_RUN_MODULES
                    if nxt is None:
                        return False
                    return nxt.dyn or bool(UI_NAME_RE.match(nxt.text)) or nxt.text.lower() in UI_RUN_MODULES
                k += 0 if rest else 1                  # -X / -W: skip the value word too
                break
            k += 1
            continue
        return _ui_word(args[k]) or args[k].dyn
    return _reads_stdin(cmd)


def lock_owner_write(words):
    """The owner-only write of a lock CLI call whose arguments (after the script) are `words`,
    as text, or None. The live CLI: reserve, release, stop/unbook --force. The kit CLI
    (tools/machine_lock.py): hold whole|half|slots, division --set|--default, game on|off|set,
    top (move a waiting run to the front), stop --force. Reads, runs, agent holds and bookings, and ending one's own run or hold stay
    open; the lock's own check refuses an agent the rest (kitlib.lock.api.authorize)."""
    head = []
    for w in words:
        if w == '--':
            break
        head.append(w)
    pos, k = [], 0
    while k < len(head):
        t = head[k]
        if t.startswith('-'):
            k += 2 if t in LOCK_VALUE_OPTS else 1
            continue
        pos.append(t)
        k += 1
    if not pos:
        return None
    sub, args = pos[0], pos[1:]
    if sub in ('reserve', 'release', 'top'):
        return sub
    if sub == 'hold' and args[:1] and args[0] in ('whole', 'half', 'slots'):
        return 'hold ' + args[0]
    if sub == 'game' and args[:1] and args[0] in ('on', 'off', 'set'):
        return 'game ' + args[0]
    if sub == 'division':
        for t in head:
            if t in ('--set', '--default') or t.startswith('--set='):
                return 'division ' + t.split('=', 1)[0]
    if sub in ('stop', 'unbook') and '--force' in head:
        return sub + ' --force'
    return None


def owner_lock(event, ctx, cmd, inv, cwd, role):
    """A command that runs machine_lock.py (any launcher: python, pythonw, py, start,
    Start-Process, a direct path; nested in a `run` too) with an owner-only write. The owner's
    own session (nothing marks it) is not held to it."""
    if inv is None or is_owner_session(event, ctx):
        return None
    if runs_reserve_ui(cmd, inv):
        return owner_lock_refusal(ctx, role, '`%s` runs reserve_ui, the owner\'s lock reservation UI' % bare(cmd))
    seq = [inv.path] + inv.argv
    for k, t in enumerate(seq):
        if cm.base_name(t) != LOCK_SCRIPT:
            continue
        rest = []
        for w in seq[k + 1:]:
            rest.extend(w.split() if ' ' in w else [w])     # a Start-Process "-ArgumentList" string
        found = lock_owner_write(rest)
        if found:
            return owner_lock_refusal(ctx, role, '`%s` runs machine_lock.py %s' % (bare(cmd), found))
    return None


# ---------------------------------------------------------------- edits: ledger and main checkout

def same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def ledger_rule(ctx, target, role, what):
    user = ctx.user()
    broken = user.get('broken')
    if broken and role == 'subagent':
        return refuse('ledger', role, '%s: the machine config ~/.claude-pm-kit/config.json is broken (%s), so the '
                      'guard cannot tell where the rulings ledger is and refuses every subagent write. The owner '
                      'or the PM session fixes it.' % (what, broken),
                      'python -m json.tool %s' % quote(os.path.join(ctx.home, '.claude-pm-kit', 'config.json')))
    ledger = user.get('ledger_path')
    if not ledger or not target or not same_file(target, ledger):
        return None
    return refuse('ledger', role, '%s writes the rulings ledger, which only ledger.py writes. '
                  "Add the entry with the kit's tools/ledger.py add (docs/ledger.md)." % what,
                  'python tools/ledger.py add --repo <repo> --prov <prov> --q "<question>" --a "<answer>" '
                  '--decides "<what it decides>" --source "<source>"')


def lock_state_dir(ctx):
    """The machine lock's state folder: <home>/.claude/machine-lock (kitlib.lock.store)."""
    return os.path.join(ctx.home, '.claude', 'machine-lock')


def lock_state(event, ctx, target, role, what):
    """An agent session's edit of a file in the machine lock's state folder: the lock tool alone
    writes it, under its own mutex."""
    if not target or not agent_session(event, ctx):
        return None
    top = os.path.normcase(os.path.abspath(lock_state_dir(ctx)))
    here_ = os.path.normcase(os.path.abspath(target))
    if here_ != top and not here_.startswith(top.rstrip('\\/') + os.sep):
        return None
    return refuse('lock-state', role, '%s edits the machine lock\'s state, which only the lock tool writes, under its own '
                  'mutex. Ask the owner (through the PM) when the state looks wrong. To see it:' % what,
                  'python %s status' % (LOCK_TOOL % fwd(ctx.home)))


WT_ADD = 'git -C %s worktree add .claude/worktrees/<slug> -b <branch>'
PM_DATA_WHY = ('Agents do not work in the PM data worktree %s (a worktree made from it would read the kit\'s own '
               '.kit.json, not its rules); the PM changes it, so ask the coordinator to take the change to the PM. '
               'To see its state:')


def new_worktree(ctx, top):
    """(sentence or None, command): the worktree to make of the main checkout `top`; for the PM
    data worktree no worktree is offered (ctx.is_pm_data): a sentence saying so, and a read."""
    if top and ctx.is_pm_data(top):
        return PM_DATA_WHY % quote(top), 'git -C %s status' % quote(top)
    return None, WT_ADD % (quote(top) if top else '"<main checkout>"')


class Judged(object):
    """`ctx` as one judgement of the shared repository sees it from the PM data worktree: config,
    config_error and main_top answer `top`'s .kit.json (`cfg`, or None and `err`), and no place
    is the PM data worktree again."""

    def __init__(self, ctx, top, cfg, err):
        self._ctx, self._top, self._cfg, self._err = ctx, top, cfg, err

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def config(self, path):
        return self._cfg

    def config_error(self, path):
        return self._err

    def main_top(self, path):
        return self._top

    def is_pm_data(self, path):
        return False


class DataBranch(object):
    """The PM data worktree's own Config, with its data branch as the branch a push is judged for."""

    def __init__(self, cfg, branch):
        self._cfg, self.main_branch = cfg, branch

    def __getattr__(self, name):
        return getattr(self._cfg, name)


def kit_judged(ctx, where):
    """(Judged by the kit's own .kit.json, None) for git run in the PM data worktree `where`, which
    acts on refs and worktrees of the repository it shares with the kit; (None, why) when that
    .kit.json cannot be read, and the caller refuses."""
    top = ctx.repo_main_top(where)
    if not top:
        return None, 'the main checkout of the repository %s belongs to cannot be found' % where
    cfg = ctx.config_at(top)
    err = ctx.config_error_at(top)
    if cfg is None:
        return None, 'its .kit.json in %s does not load (%s)' % (top, err or 'no .kit.json')
    return Judged(ctx, top, cfg, None), None


def git_push_pm_data(event, ctx, gc, role):
    """git push from the PM data worktree: every ref but the data branch is judged by the kit's own
    .kit.json (main checkout's rules), the data branch by the data worktree's own. Both judgements
    run the git-push-pmdata checks too (git_push)."""
    kit, why = kit_judged(ctx, gc.dir)
    if kit is None:
        return refuse('git-push-main', role, '`git push` in the PM data worktree, where the kit\'s rules for the shared '
                      'repository cannot be read: %s. To see the worktrees:' % why, 'git -C %s worktree list' % quote(gc.dir))
    d = git_push(event, kit, gc, role)
    if d:
        return d
    cfg = ctx.config(gc.dir)
    cfg = DataBranch(cfg, ctx.pm_data_branch(gc.dir)) if cfg is not None else None
    return git_push(event, Judged(ctx, ctx.main_top(gc.dir), cfg, ctx.config_error(gc.dir)), gc, role)


def pm_data_unknown(ctx, target, cwd, role, what):
    """A subagent write while the guard cannot tell the PM data worktree (ctx.pm_data_error):
    denied, as for a broken machine config (fail closed)."""
    if role != 'subagent':
        return None
    why = ctx.pm_data_error(target) or ctx.pm_data_error(cwd)
    if not why:
        return None
    return refuse('pm-data', role, '%s: the guard cannot tell which worktree is the PM data worktree (%s), so it '
                  'refuses every subagent write. The owner or the PM session fixes it. To read the machine config:'
                  % (what, why), 'python -m json.tool %s' % quote(os.path.join(ctx.home, '.claude-pm-kit', 'config.json')))


def own_worktree(ctx, cwd, main_top=None):
    """The top of the linked worktree the call runs in (of `main_top`, when given), or None."""
    here_ = ctx.place(cwd) if cwd else None
    if here_ is None or here_.kind != repo.LINKED or not here_.top:
        return None
    if main_top is not None:
        mine = ctx.main_top(cwd)
        if not mine or os.path.normcase(mine) != os.path.normcase(main_top):
            return None
    return here_.top


def edit_target(event, ctx, target, cwd, role, what, unknown_why=None, rewrite=None, run=None):
    """Deny an edit of `target` in a main checkout (or a place git cannot tell) by a role that
    may not edit one, and an agent session's write outside its roots (write_jail). `rewrite(path)`
    gives the same edit aimed at `path` (the alternative); `run` the command to rerun from the
    worktree when it writes into the directory it runs in."""
    d = ledger_rule(ctx, target, role, what) or pm_data_unknown(ctx, target, cwd, role, what) or \
        lock_state(event, ctx, target, role, what) or kit_json(event, ctx, target, role, what)
    if d:
        return d
    return main_checkout(event, ctx, target, cwd, role, what, unknown_why, rewrite, run) or \
        write_jail(event, ctx, target, cwd, role, what, rewrite, run)


def main_checkout(event, ctx, target, cwd, role, what, unknown_why=None, rewrite=None, run=None):
    cfg = ctx.config(target) if target else None
    perms = kconfig.permissions(event, kconfig.roles(cfg))
    if perms.get('edit_main_checkout'):
        return None
    main_top = ctx.main_top(cwd) if cwd else None
    new_wt = new_worktree(ctx, main_top)[1]
    if target is None:
        mine = own_worktree(ctx, cwd)
        instead = rewrite(fwd(mine) + '/<file>') if mine and rewrite is not None else new_wt
        return refuse('main-checkout', role, '%s: the guard cannot tell where it writes (%s), and this '
                      'role may not edit a main checkout. Write to a literal path inside your worktree.' %
                      (what, unknown_why or 'unknown path'), instead)
    place = ctx.place(target)
    if place.kind not in (repo.MAIN, repo.UNKNOWN):
        return None
    if place.kind == repo.UNKNOWN:
        mine = own_worktree(ctx, cwd)
        instead = rewrite(fwd(os.path.join(mine, os.path.basename(target)))) if mine and rewrite is not None \
            else new_wt
        return refuse('main-checkout', role, '%s: git cannot tell whether %s is in a main checkout (%s); '
                      'this role may not edit one. Write inside a path git reports as a linked worktree.' %
                      (what, target, place.detail), instead)
    what2, instead = worktree_alternative(ctx, target, place.top, cwd, rewrite, run)
    return refuse('main-checkout', role, '%s is in the main checkout %s; this role may not edit a main '
                  'checkout. %s' % (what, place.top, what2), instead)


KIT_JSON = '.kit.json'


def kit_json(event, ctx, target, role, what):
    """An agent session's edit of a .kit.json in a checkout (or a place git cannot tell): the
    file names the session's own write roots (guard.data_roots) and is the owner's."""
    if not target or not agent_session(event, ctx):
        return None
    if KIT_JSON not in (os.path.basename(target).lower(), os.path.basename(jail.real(target))):
        return None
    if ctx.place(target).kind == repo.NONE:
        return None                             # a file of that name outside git configures nothing
    return refuse('kit-json', role, '%s: a .kit.json sets the repository\'s rules, the folders agent sessions may '
                  'write among them (guard.data_roots), and only the owner changes it (owner.paths). Ask the '
                  'coordinator to take the change to the owner. To read it:' % what,
                  'python -m json.tool %s' % quote(target))


def jail_roots(ctx, cwd):
    return jail.roots(ctx, cwd, lambda p: is_root(p, None, ctx))


JAIL_ASK = 'Ask the coordinator (a coordinator asks the PM) when the work needs a folder outside them.'


def write_jail(event, ctx, target, cwd, role, what, rewrite=None, run=None):
    """An agent session's write outside its roots (kitlib.guard.jail): its checkout, temp and
    the .kit.json data roots, judged by the resolved path."""
    if not target or not agent_session(event, ctx):
        return None
    rs = jail_roots(ctx, cwd)
    verdict, root = jail.judge(target, rs)
    if verdict is None:
        return None
    return jail_refusal(role, rs, verdict, root, what, target, rewrite, run)


def jail_refusal(role, rs, verdict, root, what, target, rewrite=None, run=None):
    dest = jail.destination(rs)
    if verdict == jail.ADD_ONLY:
        why = ('%s: %s already exists in %s, an add-only data folder: an agent session creates new files there and '
               'never overwrites, moves or deletes one. %s' % (what, fwd(target), fwd(root.path), JAIL_ASK))
        if rewrite is not None:
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            return refuse('write-jail', role, why + ' To write a new file:',
                          rewrite(fwd(folder) + '/<new file name>'))
        return refuse('write-jail', role, why + ' To see the folder\'s files:', 'ls %s' % quote(root.path))
    if verdict == jail.READ_ONLY:
        why = ('%s: %s is in %s, a data folder that is read-only for agent sessions (.kit.json guard.data_roots). %s'
               % (what, fwd(target), fwd(root.path), JAIL_ASK))
    else:
        why = ('%s: %s is outside the folders an agent session may write: %s. %s'
               % (what, fwd(target), jail.describe(rs), JAIL_ASK))
    if dest is None:
        return refuse('write-jail', role, why + ' To see where this session runs:', 'pwd')
    if rewrite is not None:
        name = os.path.basename(target.rstrip('\\/')) or 'out'
        return refuse('write-jail', role, why + ' The same write in %s:' % fwd(dest.path),
                      rewrite(fwd(os.path.join(dest.path, name))))
    return refuse('write-jail', role, why + ' Run it with %s as the working directory:' % quote(dest.path),
                  run or 'cd %s' % quote(dest.path))


def worktree_alternative(ctx, target, main_top, cwd, rewrite=None, run=None):
    """(sentence, command): the same edit in the session's own worktree of this main checkout, or
    a new worktree to make it in."""
    rel = os.path.relpath(target, main_top)
    mine = own_worktree(ctx, cwd, main_top)
    if mine:
        if rewrite is not None:
            return 'Make the change in your worktree:', rewrite(fwd(os.path.join(mine, rel)))
        if run is not None:
            return 'Run it with %s as the working directory:' % quote(os.path.join(mine, rel)), run
    no_wt, instead = new_worktree(ctx, main_top)
    if no_wt:
        return no_wt, instead
    return 'Make the change in a worktree of it (%s there); the lander brings it to main:' % fwd(rel), instead


# ---------------------------------------------------------------- shell writes

# PowerShell common parameters every cmdlet takes: the ones that take a value, the switches
PS_COMMON_VALUES = ('erroraction', 'warningaction', 'informationaction', 'progressaction', 'errorvariable',
                    'warningvariable', 'informationvariable', 'outvariable', 'outbuffer', 'pipelinevariable')
PS_COMMON_SWITCHES = ('verbose', 'debug', 'whatif', 'confirm')
PS_COMMON_ALIASES = {'ea': 'erroraction', 'wa': 'warningaction', 'infa': 'informationaction',
                     'proga': 'progressaction', 'ev': 'errorvariable', 'wv': 'warningvariable',
                     'iv': 'informationvariable', 'ov': 'outvariable', 'ob': 'outbuffer',
                     'pv': 'pipelinevariable', 'vb': 'verbose', 'db': 'debug', 'wi': 'whatif',
                     'cf': 'confirm', 'lp': 'literalpath', 'pspath': 'literalpath'}


class PSCmdlet(object):
    """A cmdlet's parameters as far as the guard needs them: `values` take a value, `switches`
    do not, `pos` names the positional slots in order, `writes` the parameters naming a file it
    writes (`'*'` in pos: every further positional binds to the last slot)."""

    def __init__(self, values, switches=(), pos=(), writes=(), aliases=None):
        self.values = tuple(values) + PS_COMMON_VALUES
        self.switches = tuple(switches) + PS_COMMON_SWITCHES
        self.pos, self.writes = tuple(pos), tuple(writes)
        self.aliases = dict(PS_COMMON_ALIASES, **(aliases or {}))

    def names(self):
        return self.values + self.switches


_PATHS = ('path', 'literalpath')
_FILTERS = ('filter', 'include', 'exclude', 'credential')
PS_CMDLETS = {
    'out-file': PSCmdlet(('filepath', 'literalpath', 'encoding', 'width', 'inputobject'),
                         ('append', 'force', 'noclobber', 'nonewline'), ('filepath', 'encoding'),
                         ('filepath', 'literalpath'), {'path': 'filepath'}),
    'set-content': PSCmdlet(_PATHS + _FILTERS + ('value', 'encoding', 'stream'),
                            ('passthru', 'force', 'nonewline', 'asbytestream'), ('path', 'value'), _PATHS),
    'add-content': PSCmdlet(_PATHS + _FILTERS + ('value', 'encoding', 'stream'),
                            ('passthru', 'force', 'nonewline', 'asbytestream'), ('path', 'value'), _PATHS),
    'clear-content': PSCmdlet(_PATHS + _FILTERS + ('stream',), ('force',), ('path',), _PATHS),
    'tee-object': PSCmdlet(('filepath', 'literalpath', 'variable', 'inputobject', 'encoding'), ('append',),
                           ('filepath',), ('filepath', 'literalpath'), {'path': 'filepath'}),
    'new-item': PSCmdlet(('path', 'name', 'itemtype', 'value', 'credential'), ('force',), ('path',), ('path',),
                         {'type': 'itemtype'}),
    'remove-item': PSCmdlet(_PATHS + _FILTERS + ('stream',), ('recurse', 'force'), ('path', '*'), _PATHS),
    'copy-item': PSCmdlet(_PATHS + _FILTERS + ('destination', 'tosession', 'fromsession'),
                          ('container', 'force', 'recurse', 'passthru'), ('path', 'destination'), ('destination',)),
    'move-item': PSCmdlet(_PATHS + _FILTERS + ('destination',), ('force', 'passthru'), ('path', 'destination'),
                          _PATHS + ('destination',)),
    'rename-item': PSCmdlet(_PATHS + ('newname', 'credential'), ('force', 'passthru'), ('path', 'newname'), _PATHS),
    'expand-archive': PSCmdlet(_PATHS + ('destinationpath',), ('force', 'passthru'), ('path', 'destinationpath'),
                               ('destinationpath',)),
    'compress-archive': PSCmdlet(_PATHS + ('destinationpath', 'compressionlevel'), ('update', 'force', 'passthru'),
                                 ('path', 'destinationpath'), ('destinationpath',)),
    'export-csv': PSCmdlet(_PATHS + ('inputobject', 'delimiter', 'encoding', 'quotefields', 'usequotes'),
                           ('append', 'force', 'noclobber', 'notypeinformation', 'includetypeinformation',
                            'useculture'), ('path', 'delimiter'), _PATHS),
    'export-clixml': PSCmdlet(_PATHS + ('inputobject', 'depth', 'encoding'), ('force', 'noclobber'), ('path',), _PATHS),
    'invoke-webrequest': PSCmdlet(('uri', 'outfile', 'method', 'body', 'headers', 'contenttype', 'useragent',
                                   'credential', 'websession', 'sessionvariable', 'timeoutsec', 'maximumredirection',
                                   'proxy', 'proxycredential', 'infile', 'certificate', 'certificatethumbprint',
                                   'transferencoding', 'maximumretrycount', 'retryintervalsec', 'form',
                                   'custommethod', 'authentication', 'token', 'httpversion'),
                                  ('usebasicparsing', 'passthru', 'usedefaultcredentials', 'disablekeepalive',
                                   'skipcertificatecheck', 'skipheadervalidation', 'resume', 'noproxy'),
                                  ('uri',), ('outfile',)),
    'get-childitem': PSCmdlet(_PATHS + ('filter', 'include', 'exclude', 'depth', 'attributes'),
                              ('recurse', 'force', 'name', 'directory', 'file', 'hidden', 'readonly', 'system',
                               'followsymlink'), ('path', 'filter'), ()),
    'set-itemproperty': PSCmdlet(_PATHS + _FILTERS + ('name', 'value', 'type', 'inputobject'), ('passthru', 'force'),
                                 ('path', 'name', 'value'), _PATHS),
    'start-transcript': PSCmdlet(_PATHS + ('outputdirectory',),
                                 ('append', 'force', 'noclobber', 'includeinvocationheader', 'useminimalheader'),
                                 ('path',), _PATHS + ('outputdirectory',)),
}
PS_CMDLETS['invoke-restmethod'] = PS_CMDLETS['invoke-webrequest']
PS_ALIASES = {'sc': 'set-content', 'ac': 'add-content', 'clc': 'clear-content', 'tee': 'tee-object',
              'ni': 'new-item', 'mkdir': 'new-item', 'md': 'new-item', 'ri': 'remove-item',
              'rm': 'remove-item', 'del': 'remove-item', 'erase': 'remove-item', 'rmdir': 'remove-item',
              'rd': 'remove-item', 'cpi': 'copy-item', 'cp': 'copy-item', 'copy': 'copy-item',
              'mi': 'move-item', 'mv': 'move-item', 'move': 'move-item', 'rni': 'rename-item',
              'ren': 'rename-item', 'iwr': 'invoke-webrequest', 'irm': 'invoke-restmethod',
              'curl': 'invoke-webrequest', 'wget': 'invoke-webrequest', 'epcsv': 'export-csv',
              'gci': 'get-childitem', 'ls': 'get-childitem', 'dir': 'get-childitem', 'sp': 'set-itemproperty'}
BASH_WRITERS = {'tee', 'touch', 'mkdir', 'rm', 'rmdir', 'cp', 'mv', 'truncate', 'install', 'ln',
                'unlink', 'shred'}
CMD_WRITERS = {'copy', 'xcopy', 'robocopy', 'move', 'del', 'erase', 'md', 'mkdir', 'rd', 'rmdir',
               'ren', 'rename', 'type'}
COPIERS = {'robocopy', 'xcopy'}                 # Windows programs every shell runs
EVALS = {'node': ('-e', '--eval', '-p', '--print'), 'ruby': ('-e',)}
IO_FILE_RE = re.compile(r'(?i)\[(?:System\.)?IO\.(?:File|Directory)\]::'
                        r'(?:Write|Append|Create|Copy|Move|Delete|Replace|Open)')
WIN_OPT_RE = re.compile(r'^/[A-Za-z]+(?::\S*)?$')


def ps_params(cmd, words, spec):
    """({canonical param: [value Word]}, [positional Word]) of a cmdlet's arguments, by `spec`.
    A parameter is matched as PowerShell does: exactly, by an alias, or by the one name it is a
    prefix of. An ambiguous prefix binds to every name it could be (the guard fails closed). A
    quoted word is never a parameter."""
    names = spec.names()
    named, pos = {}, []
    k = 0
    while k < len(words):
        w = words[k]
        t = w.text
        if t.startswith('-') and len(t) > 1 and not w.dyn and not cm.quoted(cmd, w) and not re.match(r'^-\d', t):
            body, colon, val = t[1:].partition(':')
            body = body.lower()
            if body in names:
                cands = [body]
            elif body in spec.aliases:
                cands = [spec.aliases[body]]
            else:
                cands = [n for n in names if n.startswith(body)]
            k += 1
            vals = []
            if colon:
                vals = [sh.Word(val, w.dyn, w.start, w.end)]
            elif any(c in spec.values for c in cands) and k < len(words):
                vals = [words[k]]
                k += 1
            for c in cands:
                named.setdefault(c, []).extend(vals)
            continue
        pos.extend(_split_commas(w))
        k += 1
    return named, pos


def ps_bound(spec, named, pos):
    """{param: [Word]}: the named parameters with the positionals bound to the slots left over."""
    out = dict((k, list(v)) for k, v in named.items())
    free = [p for p in spec.pos if p != '*' and p not in named]
    rest = list(pos)
    for k, p in enumerate(free):
        if not rest:
            break
        if k == len(free) - 1 and '*' in spec.pos:
            out.setdefault(p, []).extend(rest)
            rest = []
            break
        out.setdefault(p, []).append(rest.pop(0))
    return out


def ps_args(cmd, words, cmdlet):
    spec = PS_CMDLETS[cmdlet]
    named, pos = ps_params(cmd, words, spec)
    return ps_bound(spec, named, pos)


def _split_commas(w):
    if w.dyn or ',' not in w.text:
        return [w]
    return [sh.Word(p, False, w.start, w.end) for p in w.text.split(',') if p]


def here(w):
    """A Word for the directory a command runs in: the file it writes there is not named."""
    return sh.Word('.', False, w.start if w is not None else 0, w.end if w is not None else 0)


def _cluster_values(args, value_shorts, value_longs, k):
    """Step over one option at args[k]: (next k, [(option, value Word or None)])."""
    t = args[k].text
    if t.startswith('--'):
        name, eq, val = t.partition('=')
        if eq:
            return k + 1, [(name, sh.Word(val, args[k].dyn, args[k].start, args[k].end))]
        if name in value_longs:
            return k + 2, [(name, args[k + 1] if k + 1 < len(args) else None)]
        return k + 1, [(name, None)]
    out = []
    body = t[1:]
    for j, c in enumerate(body):
        if c in value_shorts:
            rest = body[j + 1:]
            if rest:
                return k + 1, out + [('-' + c, sh.Word(rest, args[k].dyn, args[k].start, args[k].end))]
            return k + 2, out + [('-' + c, args[k + 1] if k + 1 < len(args) else None)]
        out.append(('-' + c, None))
    return k + 1, out


def unix_opts(args, value_shorts='', value_longs=()):
    """([(option, value Word or None)], [positional Word]) of a Unix command's arguments."""
    opts, pos = [], []
    k = 0
    while k < len(args):
        t = args[k].text
        if t == '--':
            pos.extend(args[k + 1:])
            break
        if t.startswith('-') and len(t) > 1:
            k, got = _cluster_values(args, value_shorts, value_longs, k)
            opts.extend(got)
            continue
        pos.append(args[k])
        k += 1
    return opts, pos


def _sed(args):
    in_place = False
    for w in args:
        t = w.text
        if t.startswith('--in-place'):
            in_place = True
        elif t.startswith('-') and not t.startswith('--') and 'i' in t:
            body = t[1:]
            stop = min([body.index(c) for c in 'efl' if c in body] or [len(body)])
            in_place = in_place or body.index('i') < stop
    if not in_place:
        return []
    opts, pos = unix_opts(args, 'efl', ('--expression', '--file', '--line-length'))
    script = any(o in ('-e', '-f', '--expression', '--file') for o, _v in opts)
    return pos if script else pos[1:]


def _perl(args, cmd_word=None):
    in_place, script, pos = False, False, []
    k = 0
    while k < len(args):
        t = args[k].text
        if t == '--':
            pos.extend(args[k + 1:])
            break
        if t.startswith('-') and len(t) > 1:
            body = t[1:]
            k += 1
            for j, c in enumerate(body):
                if c == 'i':
                    in_place = True
                    break                       # -i.bak: the rest is the suffix
                if c in 'eEMmI':
                    script = script or c in 'eE'
                    if not body[j + 1:]:
                        k += 1                  # the value is the next word
                    break
            continue
        pos.append(args[k])
        k += 1
    if not in_place:
        return [here(cmd_word)] if script else []     # perl -e: as python -c
    return pos if script else pos[1:]


def _curl(args, cmd_word):
    out = []
    opts, _pos = unix_opts(args, 'oDcHduXAebTFxmrwKEQYyzCtP',
                           ('--output', '--dump-header', '--cookie-jar', '--header', '--data', '--user',
                            '--request', '--user-agent', '--referer', '--cookie', '--upload-file', '--form',
                            '--proxy', '--max-time', '--range', '--write-out', '--config', '--cert',
                            '--quote', '--time-cond', '--url', '--output-dir'))
    folder = None
    for o, v in opts:
        if o == '--output-dir' and v is not None:
            folder = v
    for o, v in opts:
        if o in ('-o', '--output') and v is not None:
            if v.text != '-':
                out.append(_under(folder, v))
        elif o in ('-D', '--dump-header', '-c', '--cookie-jar') and v is not None:
            if v.text != '-':
                out.append(v)
        elif o in ('-O', '--remote-name', '--remote-name-all'):
            out.append(folder if folder is not None else here(cmd_word))
    return out


def _under(folder, w):
    """`w` placed in `folder` (curl --output-dir), unless it is absolute."""
    if folder is None or re.match(r'^(?:[A-Za-z]:)?[\\/]', w.text):
        return w
    return sh.Word(folder.text.rstrip('/\\') + '/' + w.text, folder.dyn or w.dyn, w.start, w.end)


def _wget(args, cmd_word):
    out, prefix = [], None
    opts, pos = unix_opts(args, 'OoaPiBetTwQUlA',
                          ('--output-document', '--output-file', '--append-output', '--directory-prefix',
                           '--input-file', '--base', '--execute', '--tries', '--timeout', '--wait',
                           '--quota', '--user-agent', '--level', '--accept', '--reject', '--user', '--password'))
    if any(o == '--spider' for o, _v in opts):
        return []                               # checks the URL, writes nothing
    document = False
    for o, v in opts:
        if o in ('-O', '--output-document'):
            document = True
            if v is not None and v.text != '-':
                out.append(v)
        elif o in ('-o', '--output-file', '-a', '--append-output') and v is not None:
            out.append(v)
        elif o in ('-P', '--directory-prefix') and v is not None:
            prefix = v
    if not document and pos:
        out.append(prefix if prefix is not None else here(cmd_word))
    return out


def _patch(args, cmd_word):
    opts, _pos = unix_opts(args, 'odipFzBYVDrg',
                           ('--output', '--directory', '--input', '--strip', '--fuzz', '--prefix', '--suffix',
                            '--basename-prefix', '--version-control', '--reject-file', '--get'))
    names = [o for o, _v in opts]
    if '--dry-run' in names:
        return []
    for o, v in opts:
        if o in ('-o', '--output') and v is not None:
            return [v] if v.text != '-' else []
    for o, v in opts:
        if o in ('-d', '--directory') and v is not None:
            return [v]
    return [here(cmd_word)]


def _rsync(args):
    opts, pos = unix_opts(args, 'eTfB', ('--rsh', '--temp-dir', '--filter', '--exclude', '--include',
                                        '--exclude-from', '--include-from', '--files-from', '--block-size',
                                        '--partial-dir', '--backup-dir', '--suffix', '--chmod', '--log-file'))
    if any(o in ('-n', '--dry-run') for o, _v in opts):
        return []                               # a dry run lists, writes nothing
    return pos[-1:] if len(pos) >= 2 else []


def _tar(args, cmd_word):
    first = args[0].text if args else ''
    extract = bool(re.match(r'^[A-Za-z]*x', first)) and not first.startswith('-')       # tar xf a.tar
    folder = None
    for k, w in enumerate(args):
        t = w.text
        if t in ('--extract', '--get') or re.match(r'^-[A-Za-z]*x', t) and not t.startswith('--'):
            extract = True
        if t in ('-C', '--directory') and k + 1 < len(args):
            folder = args[k + 1]
        elif t.startswith('--directory='):
            folder = sh.Word(t.partition('=')[2], w.dyn, w.start, w.end)
    if not extract:
        return []
    return [folder if folder is not None else here(cmd_word)]


def _unzip(args, cmd_word):
    for k, w in enumerate(args):
        t = w.text
        if t == '-d' and k + 1 < len(args):
            return [args[k + 1]]
        if t.startswith('-') and not t.startswith('--') and re.search(r'[ltvZpz]', t[1:]):
            return []                           # lists, tests or prints to standard output
    return [here(cmd_word)]


def _copier(args):
    """robocopy src dst [files] [/opts]; xcopy src [dst] [/opts]: the destination Word."""
    pos = [w for w in args if not WIN_OPT_RE.match(w.text)]
    return pos[1:2]


def _evals(name, args, cmd_word):
    """node -e, ruby -e: code given on the line writes where it likes, as python -c does."""
    for w in args:
        if w.text in EVALS[name] or w.text.partition('=')[0] in EVALS[name]:
            return [here(cmd_word)]
        if not w.text.startswith('-'):
            return []                           # a script file
    return []


def _python(cmd, args, cmd_word):
    k = 0
    while k < len(args):
        t = args[k].text
        if t == '-c' or (t.startswith('-') and not t.startswith('--') and t.endswith('c') and len(t) > 2):
            return [here(cmd_word)]
        if t == '-':
            return [here(cmd_word)]
        if t in ('-X', '-W'):
            k += 2
            continue
        if t.startswith('-'):
            if t == '-m' or t.endswith('m') and not t.startswith('--'):
                return []                       # a module: what it writes is its own business
            k += 1
            continue
        return []                               # a script file
    if cmd.piped_in or any(r.op in ('<<', '<<<', '<') for r in cmd.redirects):
        return [here(cmd_word)]                 # a script read from standard input
    return []


def write_targets(cmd, inv):
    """[Word]: what the command writes, by its redirections and the file commands the guard
    knows. A Word whose text is '.' stands for the directory the command runs in."""
    out = []
    for r in cmd.redirects:
        if not r.writes or r.target is None:
            continue
        if r.target.text.lower() in NULL_TARGETS:
            continue
        out.append(r.target)
    if cmd.shell == sh.PS and IO_FILE_RE.search(cmd.text):
        out.append(here(cmd.words[0] if cmd.words else None))
    if inv is None:
        return out
    out.extend(inv.writes)
    name, args = inv.name, inv.args
    word = inv.word
    if inv.via == 'xargs':
        # xargs appends the names it reads: a stand-in name in the directory it runs in
        args = list(args) + [here(word)]
    if name in COPIERS:
        return out + _copier(args)
    if cmd.shell != sh.PS and name == 'find':
        if any(w.text == '-delete' for w in args):
            return out + (_find_paths(args)[0] or [here(word)])
        return out
    if cmd.shell == sh.PS and not inv.path.lower().endswith('.exe'):
        cmdlet = PS_ALIASES.get(name, name)
        if cmdlet in PS_CMDLETS and cmdlet != 'get-childitem':
            spec = PS_CMDLETS[cmdlet]
            bound = ps_args(cmd, args, cmdlet)
            for p in spec.writes:
                out.extend(bound.get(p, []))
            if cmdlet == 'expand-archive' and not bound.get('destinationpath'):
                out.append(here(word))
            if cmdlet == 'new-item' and bound.get('name'):
                base = (bound.get('path') or [None])[0]
                for n in bound['name']:
                    out.append(sh.Word(ntpath.join(base.text, n.text) if base is not None else n.text,
                                       n.dyn or (base is not None and base.dyn), n.start, n.end))
            if name not in ('curl', 'wget'):
                return out
        elif cmdlet.startswith('export-') and cmdlet not in PS_CMDLETS:
            bound = ps_bound(PS_CMDLETS['export-clixml'], *ps_params(cmd, args, PS_CMDLETS['export-clixml']))
            return out + bound.get('path', []) + bound.get('literalpath', [])
    if cmd.shell == sh.CMD and name in CMD_WRITERS:
        pos = [w for w in args if not w.text.startswith('/')]
        if name == 'type':
            return out
        if name in ('copy', 'xcopy', 'robocopy', 'ren', 'rename'):
            return out + pos[-1:] if name != 'robocopy' else out + pos[1:2]
        return out + pos
    if name == 'curl':
        return out + _curl(args, word)
    if name == 'wget':
        return out + _wget(args, word)
    if cm.PYTHON_RE.match(name):
        return out + _python(cmd, args, word)
    if name in EVALS:
        return out + _evals(name, args, word)
    if cmd.shell == sh.PS:
        return out
    if name == 'sed':
        return out + _sed(args)
    if name == 'perl':
        return out + _perl(args, word)
    if name == 'tar':
        return out + _tar(args, word)
    if name == 'unzip':
        return out + _unzip(args, word)
    if name == 'dd':
        return out + [sh.Word(w.text[3:], w.dyn, w.start, w.end) for w in args if w.text.startswith('of=')]
    if name == 'patch':
        return out + _patch(args, word)
    if name == 'rsync':
        return out + _rsync(args)
    if name in BASH_WRITERS:
        if name in COPY_LONGS:
            return out + copy_writes(name, args, word)
        pos = [w for w in args if not (w.text.startswith('-') and len(w.text) > 1)]
        return out + pos
    return out


GLOB_CHARS = set('*?[]{}+:')                   # ':' a drive-qualified name (D:x is relative to D:'s folder)

# GNU coreutils cp, mv, ln and install: the long options each takes (getopt_long reads a unique
# prefix as the option; a name missing here reads as unknown, and an unknown option makes every
# operand count), those whose value may be the next word, the short options with a value, and the
# options a copy into an existing folder may carry for the guard to judge it by the sources' names
# (with any other option the folder itself is judged).
COPY_LONGS = {
    'cp': ('archive', 'attributes-only', 'backup', 'copy-contents', 'context', 'debug', 'dereference', 'force',
           'help', 'interactive', 'keep-directory-symlink', 'link', 'no-clobber', 'no-dereference',
           'no-preserve', 'no-target-directory', 'one-file-system', 'parents', 'preserve', 'recursive',
           'reflink', 'remove-destination', 'sparse', 'strip-trailing-slashes', 'suffix', 'symbolic-link',
           'target-directory', 'update', 'verbose', 'version'),
    'mv': ('backup', 'context', 'debug', 'exchange', 'force', 'help', 'interactive', 'no-clobber', 'no-copy',
           'no-target-directory', 'strip-trailing-slashes', 'suffix', 'target-directory', 'update', 'verbose',
           'version'),
    'ln': ('backup', 'directory', 'force', 'help', 'interactive', 'logical', 'no-dereference',
           'no-target-directory', 'physical', 'relative', 'suffix', 'symbolic', 'target-directory', 'verbose',
           'version'),
    'install': ('backup', 'compare', 'context', 'debug', 'directory', 'group', 'help', 'mode',
                'no-target-directory', 'owner', 'preserve-context', 'preserve-timestamps', 'strip',
                'strip-program', 'suffix', 'target-directory', 'verbose', 'version'),
}
COPY_VALUE_LONGS = {'suffix', 'target-directory', 'no-preserve', 'sparse', 'group', 'mode', 'owner', 'strip-program'}
COPY_VALUE_SHORTS = {'cp': 'St', 'mv': 'St', 'ln': 'St', 'install': 'Stmog'}
COPY_INTO_SHORTS = {'cp': set('rRapvnt'), 'mv': set('vnt'), 'ln': set('svt'), 'install': set('vmt')}
COPY_INTO_LONGS = {'cp': {'recursive', 'archive', 'verbose', 'no-clobber', 'target-directory'},
                   'mv': {'verbose', 'no-clobber', 'target-directory'},
                   'ln': {'symbolic', 'verbose', 'target-directory'},
                   'install': {'verbose', 'mode', 'target-directory'}}


class CopyArgs(object):
    """cp, mv, ln or install's arguments: `dirs` the -t/--target-directory values, `pos` the
    operands, `shorts` and `longs` the options (a long one by its full name), `unsure` an option
    the text does not name (not literal, or a long prefix that is unknown or ambiguous)."""

    def __init__(self, name, args):
        self.dirs, self.pos, self.shorts, self.longs, self.unsure = [], [], set(), set(), False
        names = COPY_LONGS[name]
        k = 0
        while k < len(args):
            w = args[k]
            t = w.text
            if t == '--':
                self.pos.extend(args[k + 1:])
                break
            if not (t.startswith('-') and len(t) > 1):
                self.pos.append(w)
                k += 1
                continue
            if w.dyn:
                self.unsure = True
                k += 1
                continue
            value = None
            if t.startswith('--'):
                body, eq, val = t[2:].partition('=')
                full = body if body in names else None
                if full is None:
                    cands = [n for n in names if n.startswith(body)]
                    full = cands[0] if len(cands) == 1 else None
                if full is None:
                    self.unsure = True
                    k += 1
                    continue
                self.longs.add(full)
                if eq:
                    value = sh.Word(val, w.dyn, w.start, w.end)
                elif full in COPY_VALUE_LONGS:
                    value = args[k + 1] if k + 1 < len(args) else None
                    k += 1
                if full == 'target-directory' and value is not None:
                    self.dirs.append(value)
                k += 1
                continue
            body = t[1:]
            for j, c in enumerate(body):
                self.shorts.add(c)
                if c in COPY_VALUE_SHORTS[name]:
                    rest = body[j + 1:]
                    if rest:
                        value = sh.Word(rest, w.dyn, w.start, w.end)
                    else:
                        value = args[k + 1] if k + 1 < len(args) else None
                        k += 1
                    if c == 't' and value is not None:
                        self.dirs.append(value)
                    break
            k += 1

    def no_target(self):
        return 'T' in self.shorts or 'no-target-directory' in self.longs

    def makes_dirs(self, name):
        return name == 'install' and ('d' in self.shorts or 'directory' in self.longs)


def copy_writes(name, args, word):
    """[Word]: what cp, mv, ln or install writes. An option the text does not name may shift
    the operands, so then every operand counts."""
    ca = CopyArgs(name, args)
    if ca.unsure or name == 'mv' or ca.makes_dirs(name):
        return ca.dirs + ca.pos                 # mv also removes its sources
    if ca.dirs:
        return ca.dirs
    if name == 'ln' and len(ca.pos) == 1 and not ca.no_target():
        return [here(word)]                     # ln <target>: a link in the directory it runs in
    return ca.pos[-1:]


def copy_into(cmd, inv):
    """(destination Word, [source Word], sources must be files) of a copy, move or link that,
    when the destination is an existing folder, writes <folder>/<each source's name>: cp, mv, ln
    and install with only the options COPY_INTO_SHORTS/LONGS allow, Copy-Item and Move-Item,
    cmd's copy and move (of a file: cmd copies a folder's files, not the folder). None for
    anything else, and then the folder itself is judged."""
    if inv is None or inv.via == 'xargs':
        return None
    name, args = inv.name, inv.args
    if cmd.shell == sh.PS and not inv.path.lower().endswith('.exe'):
        cmdlet = PS_ALIASES.get(name, name)
        if cmdlet not in ('copy-item', 'move-item'):
            return None
        bound = ps_args(cmd, args, cmdlet)
        dest = bound.get('destination') or []
        srcs = bound.get('path', []) + bound.get('literalpath', [])
        return (dest[0], srcs, False) if len(dest) == 1 else None
    if cmd.shell == sh.CMD and name in ('copy', 'move'):
        pos = [w for w in args if not w.text.startswith('/')]
        return (pos[-1], pos[:-1], name == 'copy') if len(pos) > 1 else None
    if cmd.shell == sh.PS or name not in COPY_LONGS:
        return None
    ca = CopyArgs(name, args)
    if ca.unsure or ca.no_target() or ca.makes_dirs(name) or len(ca.dirs) > 1 or \
            ca.shorts - COPY_INTO_SHORTS[name] or ca.longs - COPY_INTO_LONGS[name]:
        return None
    if ca.dirs:
        return ca.dirs[0], ca.pos, False
    if name == 'ln' and len(ca.pos) == 1:
        return here(inv.word), ca.pos, False
    return (ca.pos[-1], ca.pos[:-1], False) if len(ca.pos) > 1 else None


def into_folder(target, w, into, cwd):
    """[path]: what a write of `target` (the Word `w`) writes. When `into` (copy_into) names `w`
    as the destination and `target` is an existing folder, <target>/<each source's name>; a
    source the guard cannot name (not literal, a wildcard, a drive-qualified name, no name, or a
    folder where only a file is read so) keeps the folder itself."""
    if into is None or not os.path.isdir(target):
        return [target]
    dest, srcs, files_only = into
    if (dest.start, dest.end, dest.text) != (w.start, w.end, w.text) or not srcs:
        return [target]
    out = []
    for s in srcs:
        base = re.split(r'[\\/]', s.text.rstrip('\\/'))[-1] if not s.dyn else ''
        if not base or base in ('.', '..') or GLOB_CHARS & set(base):
            return [target]
        if files_only:
            src = sh.resolve_path(s.text, cwd)
            if not src or not os.path.isfile(src):
                return [target]
        out.append(os.path.join(target, base))
    return out


BASH_DEVICE_RE = re.compile(r'^/dev/')         # /dev/stderr, /dev/tty, /dev/fd/3: no file
BASH_TMP_RE = re.compile(r'^/tmp(?:/|$)')        # Git Bash mounts /tmp on a temp folder: jail.bash_tmp


def shell_writes(event, ctx, cmd, inv, cwd, role):
    d = git_jail(event, ctx, cmd, inv, cwd, role)
    if d:
        return d
    into = copy_into(cmd, inv)
    for w in write_targets(cmd, inv):
        if cmd.shell == sh.BASH and not w.dyn and BASH_DEVICE_RE.match(w.text):
            continue
        if w.dyn:
            target, why = None, 'a target that is not literal: %s' % w.text
        elif cmd.shell == sh.BASH and BASH_TMP_RE.match(w.text):
            target, why = os.path.normpath(os.path.join(jail.bash_tmp(ctx), w.text[5:])), None
        else:
            target = sh.resolve_path(w.text, cmd.cwd)
            why = None if target else 'the directory it runs in is unknown' if cmd.cwd is None \
                else 'a path the text cannot place: %s' % w.text
        if target is None and cmd.cwd is not None and \
                ctx.place(cmd.cwd).kind not in (repo.MAIN, repo.UNKNOWN):
            continue                            # a target it cannot place, from a worktree or elsewhere
        what = '`%s` writes %s' % (cmd.text, w.text if w.text != '.' else 'files in the directory it runs in')
        stand_in = w.text == '.' and cmd.source[w.start:w.end] != '.'     # here(): no word to aim
        part = any((x.start, x.end) == (w.start, w.end) and x.text != w.text for x in cmd.words)
        rewrite = None if stand_in or part else (lambda path, w=w: retarget(cmd, w, path))   # part: -tDIR
        for t in into_folder(target, w, into, cmd.cwd) if target else [target]:
            d = edit_target(event, ctx, t, cwd, role, what, why, rewrite=rewrite, run=bare(cmd))
            if d:
                return d
    return None


DUPS_RE = re.compile(r'(?:[ \t]*[0-9]*[<>]&[0-9-]+)+')


def retarget(cmd, w, path):
    """The command `cmd` (from its program word, redirections kept) with the word `w` replaced
    by `path`, quoted."""
    start = cmd.words[0].start if cmd.words else w.start
    start = min(start, w.start)
    end = max(cmd.end, w.end)
    m = DUPS_RE.match(cmd.source, end)          # a dup (2>&1) has no word, so no span
    end = m.end() if m else end
    return (cmd.source[start:w.start] + quote(path) + cmd.source[w.end:end]).strip()


# ---------------------------------------------------------------- wrapped gates (P16)

def gate_log(ctx, cmd):
    place = ctx.place(cmd.cwd) if cmd.cwd else None
    slug = os.path.basename(place.top) if place is not None and place.kind == repo.LINKED else 'gate'
    return fwd(os.path.join(ctx.tempdir, '%s-gate.log' % slug))


def bare(cmd):
    if not cmd.words:
        return cmd.text
    return cmd.source[cmd.words[0].start:cmd.words[-1].end]


def wrapped_gate(event, ctx, cmd, inv, cwd, role):
    kind = cm.wrapper(inv)
    if not kind or not cmd.piped_out:
        return None
    instead = '%s > %s 2>&1' % (bare(cmd), quote(gate_log(ctx, cmd)))
    if cmd.cwd and not same_dir(cmd.cwd, cwd):
        how = 'Run it as its own call with %s as the working directory (no cd chain), then read the log:' % \
            quote(cmd.cwd)
    elif cmd.cwd is None:
        how = 'Run it as its own call from the worktree root, then read the log:'
    else:
        how = 'Run it as a command of its own, then read the log:'
    return refuse('wrapped-gate', role, '%s is piped into another command; a wrapped gate reads to the '
                  'classifier as something else and is refused (refusal pattern P16). %s' % (bare(cmd), how), instead)


def same_dir(a, b):
    return bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# ---------------------------------------------------------------- root searches

# A whole-profile or whole-drive spelling that names a root whatever its value (it may be dynamic)
_ROOT_NAMES = r'(?:HOME|USERPROFILE|HOMEDRIVE|SystemDrive|HOMEPATH)'
_ROOT_VAR = (r'(?:~|\$%s|\$\{%s\}|\$env:%s|\$\{env:%s\}|%%%s%%)' %
             (_ROOT_NAMES, _ROOT_NAMES, _ROOT_NAMES, _ROOT_NAMES, _ROOT_NAMES))
ROOT_VAR_RE = re.compile(r'(?i)^%s{1,2}[\\/]?$' % _ROOT_VAR)       # $env:HOMEDRIVE$env:HOMEPATH too
WILD = re.compile(r'[*?\[]')


def is_root(text, cwd, ctx=None):
    """True when `text` names a drive or filesystem root, the user's home, or a variable that is
    one of those."""
    t = text.strip()
    if not t:
        return False
    if ROOT_VAR_RE.match(t):
        return True
    p = sh.resolve_path(t, cwd)
    if not p:
        return False
    if ctx is not None and getattr(ctx, 'home', None) and same_dir(p, ctx.home):
        return True
    drive, rest = os.path.splitdrive(p)
    return bool(drive) and rest in ('', '\\', '/')


def split_wild(text):
    """(parent, leaf) when the last part of `text` is a wildcard (<drive>:\\*.cs), else (text, None)."""
    m = re.match(r'^(.*[\\/])([^\\/]*)$', text)
    if m and WILD.search(m.group(2)):
        return m.group(1), m.group(2)
    if not m and WILD.search(text) and not re.match(r'^[A-Za-z]:', text):
        return '.', text
    return text, None


def _find_paths(args):
    k, out = 0, []
    while k < len(args) and args[k].text in ('-H', '-L', '-P') or (k < len(args) and args[k].text.startswith('-O')):
        k += 1
    while k < len(args):
        t = args[k].text
        if t.startswith('-') or t in ('(', '!', '\\(', ')'):
            break
        out.append(args[k])
        k += 1
    return out, True


def search_paths(cmd, inv):
    """(path Words, default-to-cwd) a recursive search walks, or None when it is no recursive
    search."""
    if inv is None:
        return None
    name, args = inv.name, inv.args
    if cmd.shell == sh.PS and PS_ALIASES.get(name, name) == 'get-childitem' and \
            not inv.path.lower().endswith('.exe'):
        bound = ps_args(cmd, args, 'get-childitem')
        if 'recurse' not in bound and 'depth' not in bound:
            return None
        return (bound.get('path') or bound.get('literalpath') or []), True
    if cmd.shell != sh.PS and name == 'find':
        return _find_paths(args)
    if cmd.shell == sh.CMD and name == 'dir' or (cmd.shell != sh.PS and name == 'dir' and
                                                 any(w.text.lower() == '/s' for w in args)):
        if not any(w.text.lower().startswith('/s') for w in args):
            return None
        return [w for w in args if not w.text.startswith('/')], True
    if cmd.shell == sh.BASH and name in ('ls', 'dir'):
        if not any(re.match(r'^-[A-Za-z]*R', w.text) or w.text == '--recursive' for w in args):
            return None
        return [w for w in args if not w.text.startswith('-')], True
    if name == 'where' and (cmd.shell != sh.PS or inv.path.lower().endswith('.exe')):
        for k, w in enumerate(args[:-1]):
            if w.text.lower() == '/r':
                return [args[k + 1]], False
        return None
    if name == 'tree' and (cmd.shell != sh.PS or not PS_ALIASES.get(name)):
        opt = (lambda t: t.startswith('-')) if cmd.shell == sh.BASH else (lambda t: bool(WIN_OPT_RE.match(t)))
        return [w for w in args if not opt(w.text)], True
    if name == 'findstr':
        if not any(w.text.lower() == '/s' for w in args):
            return None
        return [w for w in args if not w.text.startswith('/')][1:], True
    if name == 'du' and cmd.shell != sh.PS:
        return [w for w in args if not w.text.startswith('-')], True
    if name in ('grep', 'egrep', 'fgrep', 'rg'):
        if name != 'rg' and not any(re.match(r'^-[A-Za-z]*[rR]', w.text) or
                                    w.text in ('--recursive', '--dereference-recursive') for w in args):
            return None
        pos = [w for w in args if not w.text.startswith('-')]
        if name == 'rg' and any(w.text == '--files' for w in args):
            return pos, True                    # rg --files: every positional is a path
        has_pattern_opt = any(w.text in ('-e', '-f', '--regexp', '--file') for w in args)
        return (pos if has_pattern_opt else pos[1:]), True
    return None


def root_search(event, ctx, cmd, inv, cwd, role):
    found = search_paths(cmd, inv)
    if not found:
        return None
    paths, default_cwd = found
    where = cmd.cwd
    for w in paths:
        parent, leaf = split_wild(w.text)
        if ROOT_VAR_RE.match(parent.strip()) or (not w.dyn and is_root(parent, where, ctx)):
            what = '`%s` searches the whole drive or profile from %s.' % (cmd.text, w.text)
            if cwd and not is_root(cwd, None, ctx):
                new = quote(cwd + '/' + leaf) if leaf else quote(cwd)
                fixed = cmd.source[cmd.start:w.start] + new + cmd.source[w.end:cmd.end]
                return refuse('root-search', role, '%s Search the project folder instead (the Glob or Grep tool '
                              'with path %s does the same):' % (what, quote(cwd)), fixed)
            return refuse('root-search', role, what + ' ' + GLOB_HINT, GLOB_ANY)
    if not paths and default_cwd and where and is_root(where, None, ctx):
        return refuse('root-search', role, '`%s` runs from the root %s and searches all of it. %s' %
                      (cmd.text, where, GLOB_HINT), GLOB_ANY)
    return None


GLOB_HINT = 'Search inside a project folder, with the Glob or Grep tool:'
GLOB_ANY = 'Glob(pattern="**/*", path="<a project folder>")'


# ---------------------------------------------------------------- heavy programs

def plain_run(cmd, inv):
    """The command line that runs `inv`, written so it reads plainly: from the program word on (no
    env prefixes, no keyword, no & call operator, no start or Start-Process wrapper), with any
    word the shell rewrites (a brace expansion, an ANSI-C escape, a backslash bash drops) written
    as the shell passes it."""
    if inv.via == 'start-process':
        return ' '.join([inv.path] + [w.text for w in inv.args])
    words = cmd.words[inv.index:]
    if not words:
        return inv.path
    if any(w.synth for w in words) or inv.path != words[0].text:
        return ' '.join([inv.path] + [w.text for w in words[1:]])
    return cmd.source[words[0].start:words[-1].end]


def lock_alternative(ctx, cmd, entry, cwd, run=None):
    """The machine_lock run line for a heavy command; `run` the command text (plain_run's by
    default). A command whose directory differs from the call's says where to run it."""
    where = cmd.cwd or cwd
    cfg = ctx.config(where) if where else None
    top = ctx.main_top(where) if where else None
    name = (cfg.repo.get('name') if cfg is not None else '') or (os.path.basename(top) if top else '<repo>')
    place = ctx.place(where) if where else None
    slug = os.path.basename(place.top) if place is not None and place.kind == repo.LINKED else 'main'
    tool = LOCK_TOOL % fwd(ctx.home)
    if run is None:
        inv = cm.invocation(cmd)
        run = plain_run(cmd, inv) if inv is not None else bare(cmd)
    return 'python %s run %s --kind %s --repo %s --slug %s -- %s' % (
        tool, LOCK_RUN_FLAGS, entry['kind'], name, slug, run)


def run_from(cmd, cwd):
    """The sentence that says where to run a command whose directory differs from the call's."""
    if cmd.cwd and cwd and not same_dir(cmd.cwd, cwd):
        return ' Run it with %s as the working directory:' % quote(cmd.cwd)
    return ''


def heavy(event, ctx, cmd, inv, cwd, role):
    if inv is None or inv.dyn or cm.wrapper(inv):
        return None
    where = cmd.cwd or cwd
    cfg = ctx.config(where) if where else None
    entry = kconfig.heavy_match(inv.path, inv.argv, kconfig.heavy_programs(cfg))
    if not entry:
        return None
    held, why = ctx.lock_held()
    if held:
        return None
    return refuse('heavy', role, '`%s` runs %s, a heavy program (kind %s: %s), and this session holds no '
                  'machine lock (%s).%s' % (bare(cmd), inv.name, entry['kind'], entry.get('why', ''), why,
                                            run_from(cmd, cwd)),
                  lock_alternative(ctx, cmd, entry, cwd, plain_run(cmd, inv)))


# ---------------------------------------------------------------- git

# Long options per subcommand, so an abbreviation git accepts (--ha for --hard) is read as git
# reads it; the ones that take a value; the short options that take one.
GIT_LONGS = {
    'reset': ('hard', 'soft', 'mixed', 'keep', 'merge', 'quiet', 'patch', 'pathspec-from-file',
              'pathspec-file-nul', 'recurse-submodules', 'no-recurse-submodules', 'intent-to-add',
              'refresh', 'no-refresh'),
    'commit': ('all', 'allow-empty', 'allow-empty-message', 'amend', 'message', 'file', 'author', 'date',
               'template', 'reuse-message', 'reedit-message', 'fixup', 'squash', 'patch', 'include', 'only',
               'interactive', 'quiet', 'verbose', 'dry-run', 'no-verify', 'verify', 'signoff', 'no-signoff',
               'no-edit', 'edit', 'cleanup', 'status', 'no-status', 'gpg-sign', 'no-gpg-sign',
               'pathspec-from-file', 'pathspec-file-nul', 'reset-author', 'short', 'branch', 'porcelain',
               'long', 'null', 'untracked-files', 'trailer'),
    'push': ('force', 'force-with-lease', 'no-force-with-lease', 'force-if-includes', 'no-force-if-includes',
             'no-verify', 'verify', 'delete', 'all', 'mirror', 'branches', 'tags', 'follow-tags',
             'no-follow-tags', 'dry-run', 'porcelain', 'prune', 'set-upstream', 'quiet', 'verbose', 'progress',
             'no-progress', 'atomic', 'no-atomic', 'signed', 'no-signed', 'push-option', 'repo', 'receive-pack',
             'exec', 'thin', 'no-thin', 'recurse-submodules', 'no-recurse-submodules', 'ipv4', 'ipv6'),
    'restore': ('staged', 'worktree', 'source', 'patch', 'quiet', 'progress', 'no-progress', 'ours', 'theirs',
                'merge', 'conflict', 'ignore-unmerged', 'ignore-skip-worktree-bits', 'recurse-submodules',
                'no-recurse-submodules', 'overlay', 'no-overlay', 'pathspec-from-file', 'pathspec-file-nul'),
    'clean': ('dry-run', 'force', 'interactive', 'quiet', 'exclude'),
    'branch': ('delete', 'force', 'move', 'copy', 'create-reflog', 'set-upstream-to', 'unset-upstream', 'track',
               'no-track', 'list', 'all', 'remotes', 'verbose', 'quiet', 'abbrev', 'no-abbrev', 'column',
               'no-column', 'sort', 'merged', 'no-merged', 'contains', 'no-contains', 'points-at', 'format',
               'color', 'no-color', 'ignore-case', 'show-current', 'edit-description', 'recurse-submodules'),
    'add': ('all', 'update', 'no-all', 'ignore-removal', 'no-ignore-removal', 'dry-run', 'verbose', 'force',
            'interactive', 'patch', 'edit', 'intent-to-add', 'refresh', 'ignore-errors', 'ignore-missing',
            'renormalize', 'chmod', 'pathspec-from-file', 'pathspec-file-nul', 'sparse'),
    'apply': ('check', 'stat', 'numstat', 'summary', 'index', 'cached', 'intent-to-add', '3way', 'reverse',
              'reject', 'unidiff-zero', 'apply', 'no-add', 'exclude', 'include', 'directory', 'verbose',
              'quiet', 'whitespace', 'recount', 'allow-empty', 'inaccurate-eof', 'build-fake-ancestor',
              'unsafe-paths', 'ignore-space-change', 'ignore-whitespace'),
    'stash': ('message', 'patch', 'staged', 'keep-index', 'no-keep-index', 'include-untracked', 'all', 'quiet',
              'pathspec-from-file', 'pathspec-file-nul', 'index'),
}
GIT_LONGS['stage'] = GIT_LONGS['add']
GIT_LONG_VALUES = {'message', 'file', 'author', 'date', 'template', 'reuse-message', 'reedit-message', 'fixup',
                   'squash', 'cleanup', 'trailer', 'source', 'conflict', 'exclude', 'push-option', 'repo',
                   'receive-pack', 'exec', 'set-upstream-to', 'sort', 'format', 'contains', 'no-contains',
                   'points-at', 'pathspec-from-file', 'directory', 'whitespace', 'include', 'build-fake-ancestor'}
GIT_SHORT_VALUES = {'commit': 'mFCct', 'restore': 's', 'clean': 'e', 'push': 'o', 'stash': 'm', 'branch': 'u',
                    'apply': 'pC'}


def _canon_long(sub, body):
    """The long options `--body` selects for `sub`: itself when exact or unknown, the one it
    abbreviates, or every one it could abbreviate (an ambiguous prefix fails closed)."""
    table = GIT_LONGS.get(sub, ())
    if body in table or len(body) < 2:
        return [body]
    hits = [n for n in table if n.startswith(body)]
    return hits or [body]


def _opts(sub, args):
    """(short option letters, long option names, positional Words) of a git subcommand's
    arguments, option values left out."""
    shorts, longs, pos = set(), set(), []
    values = GIT_SHORT_VALUES.get(sub, '')
    k = 0
    while k < len(args):
        t = args[k].text
        if t == '--':
            pos.extend(args[k + 1:])
            break
        if t.startswith('--'):
            body, eq, _v = t[2:].partition('=')
            names = _canon_long(sub, body)
            longs.update(names)
            k += 1
            if not eq and len(names) == 1 and names[0] in GIT_LONG_VALUES:
                k += 1
            continue
        if t.startswith('-') and len(t) > 1:
            body = t[1:]
            k += 1
            for j, c in enumerate(body):
                shorts.add(c)
                if c in values:
                    if not body[j + 1:]:
                        k += 1
                    break
            continue
        pos.append(args[k])
        k += 1
    return shorts, longs, pos


def _short_flags(args):
    out = set()
    for w in args:
        t = w.text
        if t.startswith('-') and not t.startswith('--') and len(t) > 1:
            out.update(t[1:])
    return out


def _pos(args):
    return [w for w in args if not w.text.startswith('-')]


def git_alias(cmd, gc, role):
    name, val = gc.alias
    rest = ' '.join(w.text for w in gc.rest)
    if val.startswith('!'):
        how, instead = 'Run the command itself:', ('%s %s' % (val[1:], rest)).strip()
    else:
        how, instead = 'Run the git command itself:', ('git %s %s' % (val if gc.sub == name else gc.sub, rest)).strip()
    return refuse('git-alias', role, '`%s` defines a git alias on the command line (alias.%s=%s), which hides '
                  'the command git runs. %s' % (bare(cmd), name, val, how), instead)


def git_rules(event, ctx, cmd, inv, cwd, role):
    d = git_send_pack_pmdata(event, ctx, inv, role)
    if d:
        return d
    gc = cm.git_call(inv)
    if gc is None:
        return None
    if gc.alias:
        return git_alias(cmd, gc, role)
    if gc.sub_dyn or (gc.sub == '' and gc.inv is not None and gc.inv.via == 'xargs'):
        d = git_push_pmdata(event, ctx, gc, role)       # a computed subcommand may be push
        if d:
            return d
    d = git_alias_config(event, ctx, gc, role) or git_remote_rewrite(event, ctx, gc, role)
    if d:
        return d
    sub, args = gc.sub, gc.rest
    text = bare(cmd)
    if sub == 'worktree':
        return git_worktree_remove(ctx, gc, role, text)     # from any place: it removes another tree
    if sub == 'push':
        return git_push(event, ctx, gc, role)
    if sub == 'branch':
        d = git_branch_delete(ctx, gc, role)
        if d:
            return d
    place = ctx.place(gc.dir) if gc.dir and not gc.unknown else \
        repo.Place(repo.UNKNOWN, None, 'the directory git runs in is unknown', None, None)
    if place.kind not in (repo.MAIN, repo.UNKNOWN):
        return None
    where = 'the main checkout %s' % place.top if place.kind == repo.MAIN else \
        'a checkout git cannot place (%s)' % place.detail
    top = place.top or '<main checkout>'
    short, longs, pos = _opts(sub, args)
    if writes_main(sub, short, longs):
        cfg = ctx.config(place.top or gc.dir)
        if not kconfig.permissions(event, kconfig.roles(cfg)).get('edit_main_checkout'):
            how, instead = git_in_worktree(ctx, cmd, gc, top, cwd)
            return refuse('git-main-history', role, '`%s` writes history or files in %s; this role may not change a '
                          'main checkout. %s' % (text, where, how), instead)
    no_wt, wt = new_worktree(ctx, place.top)
    if sub == 'restore':
        staged = 'S' in short or 'staged' in longs
        worktree = 'W' in short or 'worktree' in longs
        if not staged or worktree:
            return refuse('git-restore', role, '`%s` overwrites working files in %s, where landers work. To undo a '
                          'change, make it in a worktree (%s). To unstage only:' % (text, where, WT_ADD % quote(top)),
                          'git -C %s restore --staged -- <path>' % quote(top)) if not no_wt else \
                refuse('git-restore', role, '`%s` overwrites working files in %s. %s' % (text, where, no_wt), wt)
    if sub in ('checkout', 'switch'):
        return refuse('git-checkout', role, '`%s` changes files in %s, where landers work. Read a file with git -C %s '
                      'show <rev>:<path>, or check the branch out in a worktree:' % (text, where, quote(top)),
                      'git -C %s worktree add .claude/worktrees/<slug> <branch>' % quote(top)) if not no_wt else \
            refuse('git-checkout', role, '`%s` changes files in %s. %s' % (text, where, no_wt), wt)
    if sub == 'reset' and 'hard' in longs:
        rev = ' '.join(w.text for w in pos) or 'HEAD'
        return refuse('git-reset-hard', role, '`%s` discards work in %s. Reset --keep refuses to drop uncommitted '
                      'changes:' % (text, where), 'git -C %s reset --keep %s' % (quote(top), rev))
    if sub == 'clean':
        if 'n' in short or 'dry-run' in longs:
            return None
        return refuse('git-clean', role, '`%s` deletes untracked files in %s. Remove what must go one named path at a '
                      'time; to list them:' % (text, where), 'git -C %s clean -n' % quote(top))
    if sub == 'stash':
        verb = pos[:1]
        if verb and verb[0].text in ('list', 'show'):
            return None
        return refuse('git-stash', role, '`%s` moves work in %s into a stash every worktree shares. %s' % (
            text, where, no_wt or 'Commit the change by explicit path on a branch in a worktree:'), wt)
    if sub in ('add', 'stage'):
        bulk = {'A', 'u'} & short or {'all', 'update'} & longs or \
            any(w.text in ('.', ':/', '*') for w in pos)
        if bulk:
            return refuse('git-bulk-add', role, '`%s` stages everything in %s, other sessions\' work included. Stage '
                          'by explicit path:' % (text, where), 'git -C %s add -- <each path you changed>' % quote(top))
    if sub == 'commit':
        if 'a' in short or 'all' in longs:
            return refuse('git-commit-all', role, '`%s` commits every tracked change in %s, other sessions\' work '
                          'included. Stage by explicit path, then commit with git -C %s commit -m "<message>":' %
                          (text, where, quote(top)), 'git -C %s add -- <each path you changed>' % quote(top))
    return None


HISTORY_SUBS = ('commit', 'merge', 'rebase', 'cherry-pick', 'revert', 'am', 'pull', 'apply', 'rm', 'mv',
                'checkout-index', 'update-ref')
APPLY_READ_ONLY = {'check', 'stat', 'numstat', 'summary'}


def writes_main(sub, short, longs):
    """True when git `sub` with these options writes history, refs or tracked files (a reset
    --hard has a rule of its own)."""
    if sub == 'apply':
        return not (APPLY_READ_ONLY & longs)
    if sub in HISTORY_SUBS:
        return True
    if sub == 'reset':
        return 'hard' not in longs
    if sub == 'read-tree':
        return 'u' in short
    if sub == 'branch':
        return 'f' in short or 'force' in longs
    return False


# git subcommands that change nothing in the checkout they run in, whatever their arguments
GIT_READS = {'status', 'log', 'diff', 'show', 'rev-parse', 'ls-files', 'ls-tree', 'ls-remote', 'cat-file', 'blame',
             'annotate', 'grep', 'describe', 'shortlog', 'rev-list', 'merge-base', 'for-each-ref', 'show-ref',
             'name-rev', 'help', 'version', 'check-ignore', 'check-attr', 'check-ref-format', 'var', 'count-objects',
             'diff-tree', 'diff-files', 'diff-index', 'cherry', 'range-diff', 'whatchanged', 'verify-commit',
             'verify-tag', 'show-branch', 'fsck'}
# subcommands that only read when their first word is one of these (None: when they have no word)
GIT_READ_VERBS = {'stash': ('list', 'show'), 'worktree': ('list',), 'remote': (None, 'show', 'get-url'),
                  'reflog': (None, 'show', 'exists'), 'notes': (None, 'list', 'show'),
                  'submodule': (None, 'status', 'summary')}
GIT_LIST_FLAGS = {'-l', '--list', '--show-current', '--merged', '--no-merged', '--contains', '--no-contains',
                  '--points-at'}
GIT_REF_WRITE_FLAGS = {'-d', '-D', '-m', '-M', '-c', '-C', '-f', '-u', '-a', '-s', '-e', '--delete', '--move',
                       '--copy', '--force', '--set-upstream-to', '--unset-upstream', '--edit-description',
                       '--annotate', '--sign'}
# strict reads (git_reads): the only options branch and tag may carry, spelt in full, and the
# short-flag clusters they may combine (branch -av, tag -n5); config's are CONFIG_STRICT_READ_OPTS
GIT_LISTING_OPTS = GIT_LIST_FLAGS | {'-i', '--ignore-case', '--sort', '--format', '--color', '--no-color', '--column',
                                     '--no-column', '--omit-empty'}
GIT_STRICT_READ_OPTS = {'branch': (GIT_LISTING_OPTS | {'--all', '--remotes', '-vv', '--verbose', '--abbrev',
                                                       '--no-abbrev'}, re.compile(r'-[ailrv]+$')),
                        'tag': (GIT_LISTING_OPTS, re.compile(r'-(?=.)[il]*(?:n\d*)?$'))}


def opens_pager(t):
    """True for grep's -O[<cmd>] (alone or in a cluster: -nO) and --open-files-in-pager, spelt in
    full or abbreviated (--open): it runs a program of the caller's choosing."""
    if t.startswith('--'):
        name = t.split('=', 1)[0]
        return len(name) >= 4 and '--open-files-in-pager'.startswith(name)
    return t.startswith('-') and 'O' in t[1:]


def writes_output_file(t):
    """True for log/show/diff's --output[=<file>], spelt in full or abbreviated (--outp=x)."""
    name = t.split('=', 1)[0]
    return len(name) >= 4 and '--output'.startswith(name)


def strict_read_opts(sub, words):
    """True when every option of branch, tag or config is one GIT_STRICT_READ_OPTS or
    CONFIG_STRICT_READ_OPTS names in full: git expands an abbreviation (branch --unset-up), so an
    option the guard does not know may be a write."""
    if sub == 'config':
        known, cluster = CONFIG_STRICT_READ_OPTS, None
    else:
        known, cluster = GIT_STRICT_READ_OPTS[sub]
    return all(t.split('=', 1)[0] in known or cluster is not None and cluster.fullmatch(t)
               for t in words if t.startswith('-'))


def git_reads(gc, strict=False):
    """True when git `gc` changes nothing in the checkout it runs in. `strict` (a checkout the text
    cannot place) counts only what is plainly a read: no computed word, no --output file, no grep
    -O (opens_pager), and branch, tag and config only with options it names in full
    (strict_read_opts)."""
    sub = gc.sub
    words = [w.text for w in gc.rest]
    if strict and (any(w.dyn for w in gc.rest) or any(writes_output_file(t) for t in words)):
        return False
    if strict and sub == 'grep' and any(opens_pager(t) for t in words):
        return False
    if sub in GIT_READS:
        return True
    pos = [t for t in words if not t.startswith('-')]
    if sub in GIT_READ_VERBS:
        return (pos[0] if pos else None) in GIT_READ_VERBS[sub]
    if strict and sub in ('branch', 'tag', 'config') and not strict_read_opts(sub, words):
        return False
    if sub in ('branch', 'tag'):
        flags = {t.split('=', 1)[0] for t in words if t.startswith('-')}
        writes = flags & GIT_REF_WRITE_FLAGS
        if sub == 'branch':
            writes -= {'-a'}                    # branch -a lists every branch
        return not writes and (not pos or bool(flags & GIT_LIST_FLAGS))
    if sub == 'config':
        return any(t in CONFIG_READ_OPTS or t in ('-l', '--list') for t in words)
    return False


def git_targets(gc):
    """[Word]: the folders git `gc` creates besides the checkout it runs in (worktree add, clone)."""
    args = gc.rest
    if gc.sub == 'worktree' and args and args[0].text == 'add':
        rest, skip = args[1:], {'-b', '-B', '--reason', '--orphan'}
    elif gc.sub == 'clone':
        rest, skip = args, {'-b', '--branch', '-o', '--origin', '--depth', '--reference', '-c', '--config',
                            '--template', '-u', '--upload-pack', '--separate-git-dir', '--filter', '-j', '--jobs',
                            '--shallow-since', '--shallow-exclude', '--reference-if-able', '--server-option'}
    else:
        return []
    pos, k = [], 0
    while k < len(rest):
        t = rest[k].text
        if t in skip:
            k += 2
            continue
        if not t.startswith('-'):
            pos.append(rest[k])
        k += 1
    if gc.sub == 'worktree':
        return pos[:1]
    return pos[1:2]


LANDER = 'lander'


def lander_main_git(event, ctx, cmd, inv, gc):
    """True only for the lander role's `git fetch <remote> <main>` or `git merge --ff-only
    <remote>/<main>` (remote and main from the main checkout's .kit.json repo section) in a main
    checkout whose .kit.json lets that role edit one (roles.lander.edit_main_checkout): the one
    main-checkout write the jail leaves a lander that runs from a worktree. git may be called as
    git, git.exe or PowerShell's `& git`, with at most `-C <main checkout>` before the verb. Any
    other role, verb, option, ref, folder, git option (-c, --git-dir, ...), environment
    assignment or wrapper: False."""
    if gc.unknown or gc.sub not in ('fetch', 'merge') or inv.via is not None or inv.dyn:
        return False
    if [w.text for w in cmd.words[:inv.index]] not in ([], ['&']):
        return False                            # an assignment or a wrapper before git
    pre = inv.args[:len(inv.args) - len(gc.rest) - 1]
    if [w.text for w in pre[:1]] not in ([], ['-C']) or len(pre) not in (0, 2) or any(w.dyn for w in pre):
        return False
    if any(w.dyn for w in gc.rest) or ctx.place(gc.dir).kind != repo.MAIN:
        return False
    cfg = ctx.config(gc.dir)
    if cfg is None:
        return False
    table = kconfig.roles(cfg)
    if kconfig.role_name(event, table) != LANDER or not table[LANDER].get('edit_main_checkout'):
        return False
    remote, main = cfg.remote, cfg.main_branch
    want = [remote, main] if gc.sub == 'fetch' else ['--ff-only', '%s/%s' % (remote, main)]
    return [w.text for w in gc.rest] == want


def git_jail_unknown(ctx, cmd, gc, cwd, role):
    """An agent session's git that writes a checkout the text cannot place (GIT_DIR and the rest
    of commands.GIT_ENV_NAMES set, an environment change the guard cannot read
    (commands.git_env_unsafe), --git-dir, --work-tree, -c core.worktree, --config-env, a
    computed -C; a -C after any of them does not place it), by any role: it may be a main
    checkout, and the main-checkout git rules judge only history writes there, so a symbolic-ref
    or branch -m that re-points main's HEAD would pass them. Only a plain read passes
    (git_reads strict). The hint names what hides the place (git_unplaced): drop it, or name a
    computed -C literally."""
    if git_reads(gc, strict=True):
        return None
    if gc.blind:
        how, instead = git_unplaced(ctx, cmd, gc, cwd)
        return refuse('write-jail', role, '`%s` writes a checkout the guard cannot place: %s can point git at a '
                      'git directory, work tree, index or config the text does not show, and a -C does not undo '
                      'it. %s' % (bare(cmd), gc.blind, how), instead)
    return refuse('write-jail', role, '`%s` writes a checkout the guard cannot place (a computed -C, or a '
                  'folder it cannot tell). Name the checkout literally:' % bare(cmd),
                  ('git -C "<checkout>" %s %s' % (gc.sub, ' '.join(w.text for w in gc.rest))).rstrip())


def git_jail(event, ctx, cmd, inv, cwd, role):
    """An agent session's git that writes a checkout (or makes a folder) outside its roots
    (kitlib.guard.jail): git -C <elsewhere> commit, a cd chain into another checkout, worktree
    add or clone to a folder outside them."""
    gc = cm.git_call(inv)
    if gc is None or not agent_session(event, ctx):
        return None
    if not gc.dir:                              # commands.git_call: no place the text can tell
        return git_jail_unknown(ctx, cmd, gc, cwd, role)
    if gc.unknown:
        return None                             # a computed subcommand: the unreadable rule's
    rs = None
    for w in git_targets(gc):
        if w.dyn:
            continue
        target = sh.resolve_path(w.text, gc.dir)
        if not target:
            continue
        rs = rs or jail_roots(ctx, cwd)
        verdict, root = jail.judge(target, rs)
        if verdict is not None:
            what = '`%s` makes %s' % (bare(cmd), w.text)
            return jail_refusal(role, rs, verdict, root, what, target,
                                rewrite=lambda path, w=w: retarget(cmd, w, path))
    if git_reads(gc) or lander_main_git(event, ctx, cmd, inv, gc):
        return None
    d = pm_data_unknown(ctx, gc.dir, cwd, role, '`%s`' % bare(cmd))
    if d:
        return d
    rs = rs or jail_roots(ctx, cwd)
    verdict, root = jail.judge(gc.dir, rs)
    if verdict is None:
        return None
    what = '`%s` changes the checkout %s' % (bare(cmd), fwd(gc.dir))
    dest = jail.destination(rs)
    if verdict != jail.OUTSIDE or dest is None:
        return jail_refusal(role, rs, verdict, root, what, gc.dir)
    rest = cmd.source[gc.rest[0].start:gc.rest[-1].end] if gc.rest else ''
    return refuse('write-jail', role, '%s: it is outside the folders an agent session may write: %s. %s The same command '
                  'in %s:' % (what, jail.describe(rs), JAIL_ASK, fwd(dest.path)),
                  ('git -C %s %s %s' % (quote(dest.path), gc.sub, rest)).rstrip())


def git_in_worktree(ctx, cmd, gc, main_top, cwd):
    """(sentence, command): the same git command in the session's own worktree of this main
    checkout, when it has one, else the worktree to make; git_unplaced when what hides the place
    is in the text."""
    if gc.blind:
        return git_unplaced(ctx, cmd, gc, cwd)
    rest = cmd.source[gc.rest[0].start:gc.rest[-1].end] if gc.rest else ''
    mine = own_worktree(ctx, cwd, main_top)
    if mine:
        return 'Run it in your worktree:', ('git -C %s %s %s' % (quote(mine), gc.sub, rest)).rstrip()
    no_wt, instead = new_worktree(ctx, main_top)
    if no_wt:
        return no_wt, instead
    return 'Work in a worktree and run git %s there; the lander brings it to main:' % gc.sub, instead


def git_unplaced(ctx, cmd, gc, cwd):
    """(sentence, command) for git whose place `gc.blind` hides (GIT_DIR and the like, --git-dir,
    --work-tree, core.worktree, --config-env, a computed -c): drop it and run the same git
    command in the session's own worktree, or where it runs when it is in none."""
    rest = cmd.source[gc.rest[0].start:gc.rest[-1].end] if gc.rest else ''
    mine = own_worktree(ctx, cwd)
    git = 'git -C %s' % quote(mine) if mine else 'git'
    return 'Drop %s and run it in your worktree:' % gc.blind, ('%s %s %s' % (git, gc.sub, rest)).rstrip()


def git_branch_delete(ctx, gc, role):
    short, longs, pos = _opts('branch', gc.rest)
    force = 'D' in short or (('delete' in longs or 'd' in short) and ('force' in longs or 'f' in short))
    if not force:
        return None
    names = ' '.join(w.text for w in pos) or '<branch>'
    return refuse('git-branch-force-delete', role, '`git branch %s` deletes a branch whether or not it is merged. '
                  'Delete it with -d, which refuses an unmerged branch; unmerged work goes to the PM, never '
                  'deleted:' % ' '.join(w.text for w in gc.rest), 'git branch -d %s' % names)


def git_worktree_remove(ctx, gc, role, text):
    args = gc.rest
    if not args or args[0].text != 'remove':
        return None
    if ctx.is_pm_data(gc.dir):
        ctx, why = kit_judged(ctx, gc.dir)
        if ctx is None:
            return refuse('git-worktree-remove', role, '`%s` from the PM data worktree, where the kit\'s rules for the '
                          'shared repository cannot be read: %s. To see the worktrees:' % (text, why),
                          'git -C %s worktree list' % quote(gc.dir))
    pos = _pos(args[1:])
    if not pos or pos[0].dyn or gc.dir is None:
        target = None
    else:
        target = sh.resolve_path(pos[0].text, gc.dir)
    main_top = (ctx.main_top(gc.dir) if gc.dir else None) or gc.dir
    if target is None:
        return refuse('git-worktree-remove', role, '`%s`: the guard cannot tell which worktree it removes. Name it '
                      'by a literal path:' % text,
                      'git -C %s worktree remove "<worktree path>"' % (quote(main_top) if main_top else '"<main checkout>"'))
    cfg = ctx.config(gc.dir)
    main = cfg.main_branch if cfg is not None else 'main'
    clean, why = ctx.git.is_clean(target)
    if clean is not True:
        return refuse('git-worktree-remove', role, '`%s` removes a worktree that is not clean (%s). Commit or drop '
                      'what status lists, then remove it with git -C %s worktree remove %s. To see it:' %
                      (text, why or 'uncommitted or untracked files', quote(main_top), quote(target)),
                      'git -C %s status' % quote(target))
    head, why = ctx.git.resolve('HEAD', target)
    merged, why2 = ctx.git.is_ancestor(head, main, gc.dir) if head else (None, why)
    if merged is not True:
        tool = os.path.join(main_top, cfg.tools_dir, 'worktree_remove.py') if cfg is not None else None
        if tool and os.path.isfile(tool):
            how = ('Merge it first; unmerged work goes to the PM, who decides (%s %s --abandon "<why>" only on the '
                   'PM\'s word).' % (fwd(tool), os.path.basename(target)))
        else:
            how = 'Merge it first; unmerged work goes to the PM, who decides; never removed by hand.'
        return refuse('git-worktree-remove', role, '`%s` removes a worktree whose commit is not merged into %s (%s). '
                      '%s Its unmerged commits:' % (text, main, why2 or 'not an ancestor', how),
                      'git -C %s log --oneline %s..HEAD' % (quote(target), main))
    return None


def _branch_of(ref):
    return ref[len('refs/heads/'):] if ref.startswith('refs/heads/') else ref


def pmdata_recorded(ctx, path):
    """True when the repository `path` is in has a PM data record (kit-pm-data.json in its git
    common dir, kitlib.pmdata), and when git cannot place `path` (fail closed); False outside git.
    The file's existence only: it is not parsed, and no git runs beyond the cached place."""
    if not path:
        return True
    place = ctx.place(path)
    if place.kind == repo.NONE:
        return False
    if place.kind not in (repo.MAIN, repo.LINKED) or not place.common:
        return True
    return os.path.exists(os.path.join(place.common, pmdata.FILE_NAME))


def _no_verify_word(word):
    t = word.text
    return t.startswith('--') and 'no-verify' in _canon_long('push', t[2:].partition('=')[0])


def git_push_pmdata(event, ctx, gc, role):
    """`git push --no-verify` (any abbreviation git accepts), and a `git push` with a word computed
    at run time or arguments xargs appends, or git with a computed subcommand or one xargs
    supplies (any of them may be --no-verify or push), in a
    repository with a PM data record, in every session but the owner's own: the pre-push hook is
    what keeps PM data off public remotes (docs/pm-data-branch.md "Pre-push guard")."""
    if is_owner_session(event, ctx):
        return None
    xargs = gc.inv is not None and gc.inv.via == 'xargs'
    computed = gc.sub_dyn or xargs or any(w.dyn for w in gc.rest)
    _short, longs, _pos = _opts('push', gc.rest)
    if not computed and 'no-verify' not in longs:
        return None
    bare_xargs = xargs and gc.sub == ''
    if bare_xargs and agent_session(event, ctx):
        return None                         # the write jail denies it for them (git_jail_unknown)
    # git with no subcommand run by xargs: the input names the repository (-C, --git-dir) and the
    # command, so no record can be looked up for it: denied whatever the cwd holds (fail closed)
    where = None if bare_xargs else gc.dir
    if not pmdata_recorded(ctx, where):     # None: the text cannot place it (recorded, fail closed)
        return None
    if bare_xargs:
        return refuse('git-push-pmdata', role, '`xargs git` takes its subcommand and repository from its input: the '
                      'guard cannot tell which repository or command xargs will run git with, and a push with '
                      '--no-verify would skip the pre-push hook that keeps PM data off public remotes. Run git '
                      'with its subcommand written out:', 'git -C "<checkout>" <subcommand> <arguments>')
    if computed:
        what = ('a git subcommand' if gc.sub_dyn else
                'arguments xargs appends to `git push`' if xargs else 'a `git push` word')
        return refuse('git-push-pmdata', role, '`git %s %s` has %s computed at run time, and this repository has %s: '
                      'the guard cannot judge a computed push word with PM data present (it may be --no-verify, '
                      'which skips the pre-push hook). Write the push literally:'
                      % (gc.sub, ' '.join(w.text for w in gc.rest), what, pmdata.FILE_NAME),
                      'git -C %s push <remote> <branch>' % (quote(where) if where else '"<checkout>"'))
    words, cut = [], False
    for w in gc.rest:
        if not cut and w.text == '--':
            cut = True
        if cut or not _no_verify_word(w):
            words.append(w.text)
    return refuse('git-push-pmdata', role, '`git push %s` skips the pre-push hook, which keeps PM data off public '
                  'remotes in this repository (it has %s). Push with the hook:'
                  % (' '.join(w.text for w in gc.rest), pmdata.FILE_NAME),
                  ('git -C %s push %s' % (quote(gc.dir), ' '.join(words))).rstrip())


def home_config(event, ctx, gc, role):
    """git push on a line that sets HOME, XDG_CONFIG_HOME, USERPROFILE, HOMEDRIVE or HOMEPATH
    (commands.HOME_ENV_NAMES, Command.home_env), in a repository with a PM data record, in every
    session but the owner's own (rule git-push-pmdata): git would read another user config, whose
    url.<base>.pushInsteadOf can send the push to a public remote while the pre-push hook, reading
    the same config, takes it for the private one (KIT-GUARD-HOME-GITCONFIG)."""
    name = gc.inv.cmd.home_env if gc.inv is not None else False
    if not name or is_owner_session(event, ctx) or not pmdata_recorded(ctx, gc.dir):
        return None
    return refuse('git-push-pmdata', role, '`git push %s` runs on a line that sets %s, and this repository has %s: git '
                  'would read the user config of another folder, whose url.<base>.pushInsteadOf can send the push '
                  'to a public remote while the pre-push hook, reading the same config, takes it for the private '
                  'one. Push without setting %s:' % (' '.join(w.text for w in gc.rest), name, pmdata.FILE_NAME, name),
                  ('git -C %s push %s' % (quote(gc.dir), ' '.join(w.text for w in gc.rest))).rstrip())


def hookless_sub(sub):
    """True for a git subcommand that talks to a remote without running the pre-push hook:
    send-pack, http-push, and a remote helper run by hand (remote-https, remote-ext, remote-fd,
    any remote-<transport>)."""
    return sub in ('send-pack', 'http-push') or (sub.startswith('remote-') and len(sub) > len('remote-'))


def git_send_pack_pmdata(event, ctx, inv, role):
    """`git send-pack`, `git http-push` or a `git remote-<helper>`, or their dashed programs
    (git-send-pack, git-http-push, git-remote-https, ...), in a repository with a PM data record
    (or where the text cannot place it), in every session but the owner's own: each talks to a
    remote without running the pre-push hook (rule git-push-pmdata). In a repository without a
    record, data_copy judges it."""
    if inv is None or inv.dyn:
        return None
    if inv.name.startswith('git-') and hookless_sub(inv.name[len('git-'):]):
        sub, where, args = inv.name[len('git-'):], inv.cmd.cwd, [w.text for w in inv.args]
    else:
        gc = cm.git_call(inv)
        if gc is None or not hookless_sub(gc.sub):
            return None
        sub, where, args = gc.sub, (None if gc.unknown else gc.dir), gc.argv
    return hookless_refusal(event, ctx, sub, where, args, role)


def hookless_refusal(event, ctx, sub, where, args, role):
    """git_send_pack_pmdata's judgement of `git <sub> <args>` run in `where`."""
    if is_owner_session(event, ctx):
        return None
    text = ('git %s %s' % (sub, ' '.join(args))).rstrip()
    if not pmdata_recorded(ctx, where):
        return data_copy(event, ctx, where, role, '`%s`' % text)
    return refuse('git-push-pmdata', role, '`%s` talks to a remote without running the pre-push hook, which '
                  'keeps PM data off public remotes in this repository (it has %s, or the guard cannot tell). '
                  'Push with git push, which runs the hook:' % (text, pmdata.FILE_NAME),
                  'git -C %s push <remote> <branch>' % (quote(where) if where else '"<checkout>"'))


def data_copy(event, ctx, where, role, what):
    """A push or send-pack (`what`) from a repository without a PM data record of its own that
    holds the root commit the machine's record names (a clone or a fetch of the data branch), or
    is shallow (it may hold data commits without their root), when the machine config names
    pm_data, in every session but the owner's own: no pre-push leak stage judges it, as the
    record is not there and core.hooksPath is set per repository (docs/pm-data-branch.md
    "Failure modes"). A check git cannot answer denies too (fail closed)."""
    if is_owner_session(event, ctx) or not where:
        return None
    if ctx.place(where).kind not in (repo.MAIN, repo.LINKED):
        return None
    root, why = ctx.pm_root()
    if root is None and why is None:
        return None                             # the machine config names no pm_data
    found = None
    if why is None:
        held, why = ctx.gitread.has_commit(where, root)
        if why is None and held:
            found = 'holds the PM data root commit %s' % root[:12]
    if why is None and found is None:
        shallow, why = ctx.gitread.is_shallow(where)
        if why is None and shallow:
            found = 'is a shallow clone, which may hold PM data commits without their root'
    if why is not None:
        found = 'may hold PM data, and the guard cannot check it (%s)' % why
    if found is None:
        return None
    return refuse('git-push-pmdata', role, '%s: the repository %s has no %s of its own but %s, so no pre-push leak '
                  'stage would judge what it sends. PM data is pushed only from the repository that holds the '
                  'record, to its private remote. To see where this repository fetches from:'
                  % (what, fwd(where), pmdata.FILE_NAME, found), 'git -C %s remote -v' % quote(where))


# ---------------------------------------------------------------- git aliases from config

# git's builtin commands (git --list-cmds=builtins, git 2.55): git ignores an alias that would hide one
GIT_BUILTINS = frozenset((
    'add', 'am', 'annotate', 'apply', 'archive', 'backfill', 'bisect', 'blame', 'branch', 'bugreport', 'bundle',
    'cat-file', 'check-attr', 'check-ignore', 'check-mailmap', 'check-ref-format', 'checkout', 'checkout--worker',
    'checkout-index', 'cherry', 'cherry-pick', 'clean', 'clone', 'column', 'commit', 'commit-graph', 'commit-tree',
    'config', 'count-objects', 'credential', 'credential-cache', 'credential-cache--daemon', 'credential-store',
    'describe', 'diagnose', 'diff', 'diff-files', 'diff-index', 'diff-pairs', 'diff-tree', 'difftool',
    'fast-export', 'fast-import', 'fetch', 'fetch-pack', 'fmt-merge-msg', 'for-each-ref', 'for-each-repo',
    'format-patch', 'format-rev', 'fsck', 'fsck-objects', 'fsmonitor--daemon', 'gc', 'get-tar-commit-id', 'grep',
    'hash-object', 'help', 'history', 'hook', 'index-pack', 'init', 'init-db', 'interpret-trailers',
    'last-modified', 'log', 'ls-files', 'ls-remote', 'ls-tree', 'mailinfo', 'mailsplit', 'maintenance', 'merge',
    'merge-base', 'merge-file', 'merge-index', 'merge-ours', 'merge-recursive', 'merge-recursive-ours',
    'merge-recursive-theirs', 'merge-subtree', 'merge-tree', 'mktag', 'mktree', 'multi-pack-index', 'mv',
    'name-rev', 'notes', 'pack-objects', 'pack-redundant', 'pack-refs', 'patch-id', 'pickaxe', 'prune',
    'prune-packed', 'pull', 'push', 'range-diff', 'read-tree', 'rebase', 'receive-pack', 'reflog', 'refs',
    'remote', 'remote-ext', 'remote-fd', 'repack', 'replace', 'replay', 'repo', 'rerere', 'reset', 'restore',
    'rev-list', 'rev-parse', 'revert', 'rm', 'send-pack', 'shortlog', 'show', 'show-branch', 'show-index',
    'show-ref', 'sparse-checkout', 'stage', 'stash', 'status', 'stripspace', 'submodule--helper', 'survey',
    'switch', 'symbolic-ref', 'tag', 'unpack-file', 'unpack-objects', 'update-index', 'update-ref',
    'update-server-info', 'upload-archive', 'upload-archive--writer', 'upload-pack', 'url-parse', 'var',
    'verify-commit', 'verify-pack', 'verify-tag', 'version', 'whatchanged', 'worktree', 'write-tree'))
ALIAS_DEPTH = 10


def split_alias(text):
    """The words of an alias value as git splits them (split_cmdline): blanks separate, '...' and
    "..." group, a backslash outside '...' takes the next character as it is. None for an
    unclosed quote or a trailing backslash."""
    words, cur, have, q, i = [], [], False, None, 0
    while i < len(text):
        c = text[i]
        if q is None and c in ' \t\n':
            if have:
                words.append(''.join(cur))
                cur, have = [], False
        elif q is None and c in '\'"':
            q, have = c, True
        elif c == q:
            q = None
        elif c == '\\' and q != "'":
            i += 1
            if i >= len(text):
                return None
            cur.append(text[i])
            have = True
        else:
            cur.append(c)
            have = True
        i += 1
    if q is not None:
        return None
    if have:
        words.append(''.join(cur))
    return words


def git_alias_config(event, ctx, gc, role):
    """A git subcommand that is not a git builtin, in a repository with a PM data record (or one
    the text cannot place), in every session but the owner's own: git runs the alias its config
    gives the name (alias.<name>), so the expansion is judged as the command it runs (a push, a
    hook-skipping transport, a remote rewrite, another alias). A shell alias (!...), one that
    starts with an option or cannot be split, a chain deeper than ALIAS_DEPTH, and a config git
    cannot read are denied (git-push-pmdata): the guard cannot judge them. A name with no alias
    is left to git (its own error, or a git-<name> program)."""
    if gc.sub_dyn or not gc.sub or gc.sub in GIT_BUILTINS or hookless_sub(gc.sub) or is_owner_session(event, ctx):
        return None
    where = None if gc.unknown else gc.dir
    if not pmdata_recorded(ctx, where):
        return None
    return _alias_judged(event, ctx, gc, role, where, [])


def _alias_refusal(gc, role, where, chain, why):
    shown = ' -> '.join(chain) or 'alias.%s' % gc.sub
    return refuse('git-push-pmdata', role, '`git %s` in a repository with a PM data record runs a git alias (%s) the '
                  'guard cannot judge: %s. Write the git command itself:' % (gc.sub, shown, why),
                  'git -C %s <subcommand> <arguments>' % (quote(where) if where else '"<checkout>"'))


def _alias_judged(event, ctx, gc, role, where, chain):
    if where is None:
        return _alias_refusal(gc, role, where, chain, 'the guard cannot tell the repository whose config holds it')
    val, why = ctx.gitread.alias(where, gc.sub)
    if why:
        return _alias_refusal(gc, role, where, chain, 'its config cannot be read (%s)' % why)
    if val is None:
        return None                             # no alias: git's own error, or a git-<name> program
    chain = chain + ['alias.%s=%s' % (gc.sub, val)]
    if len(chain) > ALIAS_DEPTH:
        return _alias_refusal(gc, role, where, chain[:2], 'the aliases nest deeper than %d' % ALIAS_DEPTH)
    if val.startswith('!'):
        return _alias_refusal(gc, role, where, chain, 'it runs a shell command')
    words = split_alias(val)
    if not words or words[0].startswith('-'):
        return _alias_refusal(gc, role, where, chain, 'it starts with an option or cannot be split' if words else
                              'it cannot be split')
    rest = [sh.Word(t, False, None, None) for t in words[1:]] + list(gc.rest)
    sub = cm.GitCall(gc.dir, words[0], rest, gc.inv, gc.unknown, None, False, gc.blind)
    if hookless_sub(sub.sub):
        d = hookless_refusal(event, ctx, sub.sub, where, sub.argv, role)
    elif sub.sub == 'push':
        d = git_push(event, ctx, sub, role)
    elif sub.sub in ('remote', 'config'):
        d = git_remote_rewrite(event, ctx, sub, role)
    elif sub.sub not in GIT_BUILTINS:
        d = _alias_judged(event, ctx, sub, role, where, chain)
    else:
        d = None
    if d is None or len(chain) > 1:
        return d
    return Decision(d.kind, d.rule, '`git %s` runs the git alias %s. %s' % (gc.sub, chain[0], d.reason))


# ---------------------------------------------------------------- remotes repointed

REMOTE_REWRITE_VERBS = ('set-url', 'add', 'rename', 'remove', 'rm')
CONFIG_SECTION_OPTS = ('--remove-section', '--rename-section')
CONFIG_EDIT_OPTS = ('--edit', '-e')


def remote_rewrite_key(name):
    """True for a config key or section that can point a remote somewhere else: remote.* (url,
    pushurl, ...), url.* (insteadOf, pushInsteadOf), include.* and includeIf.* (they read
    another file), case ignored as git ignores it in section and key names."""
    n = name.lower()
    return n in ('remote', 'url', 'include', 'includeif') or n.startswith(('remote.', 'url.', 'include.', 'includeif.'))


def config_rewrite(gc):
    """(what, files, scope_all) when a `git config` call writes a key or section
    remote_rewrite_key names (a computed one counts) or opens a config in an editor, else None.
    `files`: the Words of its -f/--file paths; `scope_all`: --global or --system."""
    opts, pos, files = [], [], []
    args = gc.rest
    k = 0
    while k < len(args):
        w = args[k]
        t = w.text
        if t == '--':
            pos.extend(args[k + 1:])
            break
        if t.startswith('-') and len(t) > 1 and not w.dyn:
            name, eq, val = t.partition('=')
            opts.append(name)
            if name in CONFIG_FILE_OPTS:
                if eq:
                    files.append(sh.Word(val, w.dyn, w.start, w.end))
                elif k + 1 < len(args):
                    files.append(args[k + 1])
            k += 2 if (name in CONFIG_VALUE_OPTS and not eq) else 1
            continue
        pos.append(w)
        k += 1
    scope_all = any(o in ('--global', '--system') for o in opts)
    if any(o in CONFIG_EDIT_OPTS for o in opts) or (pos and not pos[0].dyn and pos[0].text == 'edit'):
        return 'opens a config file in an editor, where any remote can be changed', files, scope_all
    if any(o in CONFIG_READ_OPTS for o in opts) or not pos:
        return None
    verb = pos[0].text
    if verb in ('get', 'list'):
        return None
    if verb in ('set', 'unset'):
        keys = pos[1:2]
    elif verb in ('rename-section', 'remove-section'):
        keys = pos[1:3]
    elif any(o in CONFIG_SECTION_OPTS for o in opts):
        keys = pos[:2]
    elif any(o in CONFIG_WRITE_OPTS for o in opts) or len(pos) > 1:
        keys = pos[:1]
    else:
        return None
    for w in keys:
        if w.dyn or remote_rewrite_key(w.text):
            return 'writes %s' % w.text, files, scope_all
    return None


def _config_file_recorded(ctx, path, where):
    """True when a config file git reads belongs to a repository with a PM data record (its
    folder, or the git common dir two folders up from a worktree's config, holds the record), or
    is a user or system config (scope_all, judged by machine_pm_data); a path that cannot be
    resolved counts (fail closed)."""
    p = sh.resolve_path(path, where) if path else None
    if not p:
        return True
    parts = [x.lower() for x in fwd(posixpath.normpath(fwd(p))).split('/') if x]
    if parts and (parts[-1] in ('.gitconfig', 'gitconfig') or parts[-3:-1] == ['.config', 'git']):
        return machine_pm_data(ctx)
    d = os.path.dirname(p)
    for _ in range(3):
        if os.path.exists(os.path.join(d, pmdata.FILE_NAME)):
            return True
        d = os.path.dirname(d)
    return False


def machine_pm_data(ctx):
    """True when the machine config names pm_data, or cannot be read (Ctx.pm_data_named)."""
    return ctx.pm_data_named()


def _remote_verb(gc):
    """The git remote verb that repoints a remote (REMOTE_REWRITE_VERBS), a computed one too,
    or None."""
    for w in gc.rest:
        if w.dyn:
            return w.text
        if w.text.startswith('-'):
            continue
        return w.text if w.text in REMOTE_REWRITE_VERBS else None
    return None


def git_remote_rewrite(event, ctx, gc, role):
    """git-remote-rewrite: `git remote set-url|add|rename|remove`, a `git config` write to
    remote.*, url.*, include.* or includeIf.* (any spelling: --add, --unset, set, unset, the
    section verbs) and `git config --edit`, in a repository with a PM data record (or one the text
    cannot place), in every session but the owner's own. A --global or --system write, or one to
    a user or system config file, counts when the machine config names pm_data; a --file git
    does not read is left alone. The pre-push leak stage tells the private remote by its
    configured URLs, so a remote pointed elsewhere would make a public one look private
    (docs/pm-data-branch.md "Pre-push guard")."""
    if gc.sub not in ('remote', 'config') or is_owner_session(event, ctx):
        return None
    where = None if gc.unknown else gc.dir
    if gc.sub == 'remote':
        verb = _remote_verb(gc)
        if verb is None:
            return None
        what = 'repoints a remote (git remote %s)' % verb
        recorded = pmdata_recorded(ctx, where)
    else:
        call = config_rewrite(gc)
        if call is None:
            return None
        what, files, scope_all = call
        if any(not f.dyn and not repo_config_file(f.text) for f in files):
            return None                         # a file git does not read
        if files:
            recorded = any(f.dyn or _config_file_recorded(ctx, f.text, where) for f in files)
        else:
            recorded = pmdata_recorded(ctx, where) or (scope_all and machine_pm_data(ctx))
    if not recorded:
        return None
    return refuse('git-remote-rewrite', role, '`git %s %s` %s, and this repository has %s (or the guard cannot tell): '
                  'the pre-push hook tells the private remote by its configured URLs, so a remote pointed elsewhere '
                  'could carry PM data to a public one. Ask the owner (through the PM) when a remote must change. '
                  'To see them:' % (gc.sub, ' '.join(w.text for w in gc.rest), what, pmdata.FILE_NAME),
                  'git -C %s remote -v' % (quote(where) if where else '"<checkout>"'))


def git_push(event, ctx, gc, role):
    if ctx.is_pm_data(gc.dir):
        return git_push_pm_data(event, ctx, gc, role)
    rest = ' '.join(w.text for w in gc.rest)
    if gc.unknown or gc.dir is None:
        return refuse('git-push-main', role, '`git push` runs in a directory the guard cannot tell, so it cannot '
                      'check for a gate-OK record. Name the checkout literally:',
                      ('git -C "<checkout>" push %s' % rest).rstrip())
    d = git_push_pmdata(event, ctx, gc, role) or home_config(event, ctx, gc, role)
    if d:
        return d
    if not pmdata_recorded(ctx, gc.dir):
        d = data_copy(event, ctx, gc.dir, role, '`%s`' % ('git push %s' % rest).rstrip())
        if d:
            return d
    cfg = ctx.config(gc.dir)
    if cfg is None:
        err = ctx.config_error(gc.dir)
        if not err:
            return None                         # no .kit.json: push_block is off
        top = ctx.main_top(gc.dir) or gc.dir
        return refuse('git-push-main', role, '`git push` in a repository whose .kit.json does not load (%s). Fix it '
                      'so it loads (the message names the key; docs/kit-json.md lists them), then push. To see '
                      'the error:' % err, 'python -m json.tool %s' % quote(os.path.join(top, '.kit.json')))
    if not cfg.push_block:
        return None
    main = cfg.main_branch
    short, longs, pos = _opts('push', gc.rest)
    if any(w.dyn for w in pos):
        remote = pos[0].text if pos and not pos[0].dyn else cfg.remote
        return refuse('git-push-main', role, '`git push %s` has a refspec that is not literal, so the guard cannot '
                      'tell whether it pushes %s. Name the refs literally:' % (rest, main),
                      'git -C %s push %s <branch>' % (quote(gc.dir), remote))
    delete = 'd' in short or 'delete' in longs
    force = 'f' in short or bool({'force', 'force-with-lease', 'force-if-includes'} & longs)
    no_verify = 'no-verify' in longs
    every = bool({'all', 'mirror', 'branches'} & longs)
    remote = pos[0].text if pos else cfg.remote
    specs = [w.text for w in pos[1:]]
    branch, _why = ctx.git.current_branch(gc.dir)
    targets = []
    if every:
        targets.append(('refs/heads/' + main, main, False))
    if not specs and not every:
        if branch:
            targets.append((branch, branch, False))
    for s in specs:
        plus = s.startswith('+')
        s = s.lstrip('+')
        src, colon, dst = s.partition(':')
        if not colon:
            dst = branch if src == 'HEAD' else src
        if delete:
            src, dst = '', dst or src
        targets.append((src, _branch_of(dst or ''), plus))
    for src, dst, plus in targets:
        if dst != main:
            continue
        what = '`git push %s` to %s/%s' % (' '.join(w.text for w in gc.rest), remote, main)
        land = landing_command(ctx, gc, cfg, remote, branch)
        if not src:
            return refuse('git-push-main', role, '%s deletes %s; main is never deleted. To see where it is:' %
                          (what, main), 'git -C %s ls-remote %s %s' % (quote(gc.dir), remote, main))
        if no_verify:
            return refuse('git-push-main', role, '%s skips the pre-push hook. %s' % (what, LAND_HOW), land)
        if force or plus:
            return refuse('git-push-main', role, '%s rewrites %s. %s' % (what, main, LAND_HOW), land)
        sha, why = ctx.git.resolve(src, gc.dir)
        if not sha:
            return refuse('git-push-main', role, '%s: cannot resolve %s (%s). %s' % (what, src, why, LAND_HOW), land)
        rec, why = ctx.gate_ok(gc.dir, sha)
        if rec is None:
            return refuse('git-push-main', role, '%s pushes %s, which has no gate-OK record (%s). %s' %
                          (what, sha[:12], why, LAND_HOW), land)
    return None


LAND_HOW = 'Land it with the gate, which records the pass and pushes:'


def landing_command(ctx, gc, cfg, remote, branch):
    main_sha, _ = ctx.git.remote_tip(remote, cfg.main_branch, gc.dir)
    tip, _ = ctx.git.resolve(branch, gc.dir) if branch else (None, None)
    return 'python %s/land.py %s --expect-main %s --expect-tip %s' % (
        cfg.tools_dir, branch if branch and branch != cfg.main_branch else '<branch>',
        main_sha or '<%s/%s sha>' % (remote, cfg.main_branch),
        tip if branch and branch != cfg.main_branch and tip else '<branch tip sha>')


# ---------------------------------------------------------------- busy-wait loops

SLEEPERS = ('sleep', 'start-sleep')
THREAD_SLEEP_RE = re.compile(r'(?i)Thread\]::Sleep')
# A loop that counts or watches a clock is bounded: a numeric comparison, bash arithmetic, the
# $SECONDS variable (case-sensitive: -Seconds is a Start-Sleep parameter), a stopwatch's .Elapsed.
# -eq and -ne are left out: they are status checks too.
BOUND_RE = re.compile(r'(?i:(?:^|[\s(])-(?:lt|le|gt|ge)(?=[\s)]|$))|\(\(|\$\{?SECONDS\b|(?i:\.Elapsed\b)')
BASH_LEAD_SKIP = ('do', 'then', 'else', 'elif', '{', '!')
BUSY_MINUTES = 10                                # the time limit an alternative gives
BUSY_WHY = ('waits in a sleep loop with no time limit, in the foreground: if what it waits for never '
            'comes, the call never returns, and ending a stuck process by hand is refused as interfering '
            'with workloads. Wait in the background instead (run_in_background: true, so the completion '
            'notice wakes you, with a command that exits when the condition holds; or the Monitor tool), '
            'or give the loop a time limit:')


def _first(cmd):
    """The command's first word as lower-case text ('' when quoted or none)."""
    w = cmd.words[0] if cmd.words else None
    return '' if w is None or cm.quoted(cmd, w) else w.text.lower()


def _lead(cmd):
    """The command's first word that is not a bash keyword prefix (do, then, else, elif, {, !), as
    lower-case text ('' when quoted or none)."""
    for w in cmd.words:
        if cm.quoted(cmd, w):
            return ''
        if w.text not in BASH_LEAD_SKIP:
            return w.text.lower()
    return ''


def _sleeps(cmd):
    """True when the command pauses: sleep, Start-Sleep, timeout.exe /t, [Threading.Thread]::Sleep."""
    inv = cm.invocation(cmd)
    if inv is not None and not inv.dyn:
        if inv.name in SLEEPERS:
            return True
        if inv.name == 'timeout' and any(a.text.lower() in ('/t', '//t') for a in inv.args):
            return True
    return bool(THREAD_SLEEP_RE.search(cmd.text or ''))


def _under_timeout(cmd):
    """True when a command this one is nested in runs it under `timeout`."""
    outer = cmd.nested_in
    while outer is not None:
        inv = cm.invocation(outer)
        stop = inv.index if inv is not None else len(outer.words)
        if any(not w.dyn and cm.base_name(w.text) == 'timeout' for w in outer.words[:stop]):
            return True
        outer = outer.nested_in
    return False


def _nested_under(c, ids):
    outer = c.nested_in
    while outer is not None:
        if id(outer) in ids:
            return True
        outer = outer.nested_in
    return False


def _segments(parsed):
    """The parse's commands grouped by the text they were read from (the call's line, or the text
    a nested shell runs), each list in order."""
    out, order = {}, []
    for c in parsed.commands:
        key = id(c.nested_in) if c.nested_in is not None else None
        if key not in out:
            out[key] = []
            order.append(key)
        out[key].append(c)
    return [out[k] for k in order]


def _bash_loops(cmds):
    """[(first, last)] index pairs of the while and until loops in a bash segment."""
    loops, stack = [], []
    for i, c in enumerate(cmds):
        lead = _lead(c)
        if lead in ('while', 'until', 'for', 'select'):
            stack.append((lead, i))
        elif _first(c) == 'done' and stack:
            kind, start = stack.pop()
            if kind in ('while', 'until'):
                loops.append((start, i))
    return loops


def _extends(groups, base):
    return len(groups) > len(base) and tuple(groups[:len(base)]) == tuple(base)


def _ps_loops(cmds):
    """[(first, last)] index pairs of the while and do loops in a PowerShell segment."""
    loops, tails = [], set()
    for i, c in enumerate(cmds):
        kind = _first(c)
        if i in tails or kind not in ('while', 'do') or len(c.words) != 1:
            continue
        base, j = c.groups, i + 1
        while j < len(cmds) and _extends(cmds[j].groups, base):
            j += 1
        if kind == 'do' and j < len(cmds) and _first(cmds[j]) in ('until', 'while') and \
                tuple(cmds[j].groups) == tuple(base) and cmds[j].prev_op == '}':
            tails.add(j)
            j += 1
            while j < len(cmds) and _extends(cmds[j].groups, base):
                j += 1
        if j - 1 > i:
            loops.append((i, j - 1))
    return loops


def _match_bracket(text, k):
    """The index after the bracket group that opens at text[k], skipping quoted text, or None."""
    pairs = {'(': ')', '{': '}', '[': ']'}
    stack, quote_ch = [], None
    i = k
    while i < len(text):
        ch = text[i]
        if quote_ch:
            if ch == '`' and quote_ch == '"':
                i += 2
                continue
            if ch == quote_ch:
                quote_ch = None
        elif ch in ('"', "'"):
            quote_ch = ch
        elif ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return None


def _ps_loop_span(src, start, kind):
    """(end, body_open) of the PowerShell loop whose keyword starts at src[start], or None: `end` is
    the index after the loop, `body_open` the index of its body's '{'."""
    k = start + len(kind)
    parts = ('(', '{') if kind == 'while' else ('{', None, '(')
    body_open = None
    for want in parts:
        while k < len(src) and src[k] in ' \t\r\n':
            k += 1
        if want is None:                         # do { } until|while ( )
            m = re.match(r'(?i)(?:until|while)\b', src[k:])
            if not m:
                return None
            k += m.end()
            continue
        if k >= len(src) or src[k] != want:
            return None
        if want == '{':
            body_open = k
        k = _match_bracket(src, k)
        if k is None:
            return None
    return k, body_open


def _busy_loop(parsed, cmds, first, last):
    """(loop text, alternative) when cmds[first..last] is a sleep loop nothing bounds, else None."""
    inner = cmds[first:last + 1]
    ids = {id(c) for c in inner}
    nested = [c for c in parsed.commands if _nested_under(c, ids)]
    if not any(_sleeps(c) for c in inner + nested) or _under_timeout(cmds[first]):
        return None
    head = cmds[first]
    src = head.source
    if head.shell == sh.PS:
        span = _ps_loop_span(src, head.start, _first(head))
        if span is None:
            return None                          # a loop the guard cannot read is left alone
        end, body_open = span
        loop = src[head.start:end]
        if BOUND_RE.search(loop):
            return None
        cut = body_open + 1 - head.start
        return loop, '$deadline = (Get-Date).AddMinutes(%d); %s if ((Get-Date) -gt $deadline) { break }; %s' % (
            BUSY_MINUTES, loop[:cut], loop[cut:].lstrip())
    words = [w for w in head.words if w.text not in BASH_LEAD_SKIP]
    start = words[0].start if words else head.start
    done = cmds[last]
    done_end = done.words[0].end if done.words else done.end
    loop = src[start:done_end]
    if BOUND_RE.search(loop):
        return None
    tested = [w for w in words[1:] if not cm.ASSIGN_RE.match(w.text)]       # while IFS= read: the assignment is no program
    if tested and cm.base_name(tested[0].text) == 'read':
        return None                              # while read: it reads lines, it waits on nothing
    rest = src[done_end:done.end] + DUP_RE.match(src, done.end).group(0)   # the loop's redirects
    return loop, "timeout %d bash -c '%s'%s" % (BUSY_MINUTES * 60, loop.replace("'", "'\\''"), rest.rstrip())


DUP_RE = re.compile(r'(?:\s*\d*[<>]&\d+)*')       # fd duplications (2>&1) a command's span leaves out


def _backgrounded(cmd):
    """True when the command, or a command it is nested in, ends with & (runs in the background)."""
    c = cmd
    while c is not None:
        if c.next_op == '&':
            return True
        c = c.nested_in
    return False


def busy_wait(event, ctx, parsed, cwd, role):
    """A foreground while/until (bash) or while/do (PowerShell) loop that sleeps, with no counter,
    clock or `timeout` bounding it. A call with run_in_background, or a loop sent to the background
    with &, is left alone: neither holds the call."""
    ti = (event or {}).get('tool_input') or {}
    if ti.get('run_in_background') is True:
        return None
    for cmds in _segments(parsed):
        shell = cmds[0].shell
        loops = _ps_loops(cmds) if shell == sh.PS else _bash_loops(cmds) if shell == sh.BASH else []
        for first, last in loops:
            if _backgrounded(cmds[last]):
                continue                         # `done &`: it does not hold the call
            found = _busy_loop(parsed, cmds, first, last)
            if found:
                return refuse('busy-wait', role, '`%s` %s' % (found[0], BUSY_WHY), found[1])
    return None


# ---------------------------------------------------------------- Windows paths in heredocs

# A drive path with one backslash (<drive>:\Users): in a python string that is not raw, \U, \N and \x
# stop the script and \n, \t, \a and \b change the path silently. The part after the backslash must
# look like a folder or file name (two characters or more), and a letter after % is a format
# (%s:\n), so a label ending in an escape ("x:\t") is not a path.
WINPATH_RE = re.compile(r'(?<![\w\\%])[A-Za-z]:\\(?!\\)[\w.$~-]{2,}')
RAW_PREFIX_RE = re.compile(r'(?i)^(?:r|rb|br|rf|fr)$')
# What bash does to an unquoted heredoc's backslashes before python reads it: \\ \$ \` lose the
# backslash, and a backslash before a newline joins the lines.
HEREDOC_UNQUOTE_RE = re.compile(r'\\([\\$`\n])')


def _heredoc_as_read(word):
    """The heredoc body as the program reads it: an unquoted one (<<EOF) after bash's backslash
    handling, so C:\\\\Users reaches python as C:\\Users."""
    text = word.text
    if not word.dyn:
        return text
    return HEREDOC_UNQUOTE_RE.sub(lambda m: '' if m.group(1) == '\n' else m.group(1), text)


def _python_reads_stdin(inv):
    """True when python, by its arguments, reads its script from standard input: no script, no
    -c, no -m, or `-`."""
    args = inv.args
    k = 0
    while k < len(args):
        t = args[k].text
        if t == '-':
            return True
        if t in ('-X', '-W'):
            k += 2
            continue
        if t.startswith('--'):
            k += 1
            continue
        if t.startswith('-'):
            if 'c' in t[1:] or 'm' in t[1:]:
                return False
            k += 1
            continue
        return False                             # a script file
    return True


def _in_plain_string(line, col):
    """True when line[col] sits in a python string literal that is not raw, judged by the nearest
    quote before it. A comment, or no quote before it: False."""
    head = line[:col]
    quotes = [i for i in (head.find('"'), head.find("'")) if i >= 0]
    if not quotes or '#' in head[:min(quotes)]:
        return False
    q = max(head.rfind('"'), head.rfind("'"))
    k = q
    while k > 0 and head[k - 1] == head[q]:
        k -= 1                                   # a triple quote: its first quote
    m = re.search(r'([A-Za-z]{1,2})$', head[:k])
    return not (m and RAW_PREFIX_RE.match(m.group(1)))


def heredoc_winpath(event, ctx, cmd, inv, cwd, role):
    """python reading its script from a bash heredoc that holds a Windows path with single
    backslashes in a string that is not raw."""
    if inv is None or inv.dyn or cmd.shell != sh.BASH or not cm.PYTHON_RE.match(inv.name):
        return None
    if not _python_reads_stdin(inv):
        return None
    for r in cmd.redirects:
        if r.op not in ('<<', '<<-') or not r.body or r.target is None:
            continue
        for line in _heredoc_as_read(r.target).splitlines():
            for m in WINPATH_RE.finditer(line):
                if not _in_plain_string(line, m.start()):
                    continue
                path = re.split(r'[\'"\s]', line[m.start():])[0]
                return refuse('heredoc-winpath', role, '`%s` reads its script from a heredoc whose python string '
                              '%s is a Windows path in a string that is not raw: python reads its backslashes as '
                              'escapes (\\U stops the script, \\n and \\t change the path). Write the script to a '
                              'file with the Write tool, with forward slashes or raw strings (r"..."), then run '
                              'it:' % (bare(cmd), path), '%s "%s"' % (inv.path, fwd(os.path.join(ctx.tempdir, 'script.py'))))
    return None


# ---------------------------------------------------------------- environment assignments

# kept in commands, which marks git whose place an assignment hides (commands.mark_git_env)
env_assignments = cm.env_assignments


# ---------------------------------------------------------------- core.hooksPath

HOOKS_KEY = 'core.hookspath'
CONFIG_VALUE_OPTS = ('--file', '-f', '--blob', '--type', '--default', '--value', '--comment')
CONFIG_FILE_OPTS = ('--file', '-f')
CONFIG_WRITE_OPTS = ('--unset', '--unset-all', '--replace-all', '--add')
CONFIG_READ_OPTS = ('--get', '--get-all', '--get-regexp', '--get-urlmatch', '--get-color', '--get-colorbool',
                    '-l', '--list')
# the only options config may carry where a strict read is wanted (strict_read_opts)
CONFIG_STRICT_READ_OPTS = set(CONFIG_READ_OPTS) | {'--show-origin', '--show-scope', '--name-only', '-z', '--null',
                                                   '--global', '--system', '--local', '--worktree', '--file', '-f',
                                                   '--blob', '--type', '--no-type', '--default', '--includes',
                                                   '--no-includes', '--bool', '--int', '--bool-or-int', '--path',
                                                   '--expiry-date', '--fixed-value'}


def kit_githooks():
    """The kit's own githooks folder: the value the install sets (docs/guard.md "Go-live")."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'githooks')


def repo_config_file(text):
    """True when a config file path names a config git reads: one inside a .git folder
    (.git/config, .git/worktrees/<name>/config, .git/modules/<name>/config, config.worktree
    beside any), or the user's or system's (.gitconfig, gitconfig, .config/git/config). The path
    is normalised first, so .git/./config and .git/info/../config count."""
    norm = posixpath.normpath((text or '').replace('\\', '/'))
    parts = [p.lower() for p in norm.split('/') if p]
    if not parts:
        return False
    if parts[-1] in ('.gitconfig', 'gitconfig'):
        return True
    if len(parts) < 2 or parts[-1] not in ('config', 'config.worktree'):
        return False
    return '.git' in parts[:-1] or (len(parts) >= 3 and parts[-3:-1] == ['.config', 'git'])


def git_config_call(gc):
    """(key, value Word or None, writes) of a `git config` call on a config git reads, or None (no
    key, --blob, or --file names a file git does not read). A --file the text does not name (a
    variable, a substitution) counts as one git reads: fail closed."""
    opts, pos = [], []
    args = gc.rest
    k = 0
    while k < len(args):
        t = args[k].text
        if t.startswith('-'):
            name = t.split('=', 1)[0]
            opts.append(name)
            if name in CONFIG_FILE_OPTS:
                w = args[k] if '=' in t else (args[k + 1] if k + 1 < len(args) else None)
                path = t.split('=', 1)[1] if '=' in t else (w.text if w is not None else '')
                if not (w is not None and w.dyn) and not repo_config_file(path):
                    return None                  # a file git does not read
            k += 2 if t in CONFIG_VALUE_OPTS else 1
            continue
        pos.append(args[k])
        k += 1
    if not pos or '--blob' in opts:
        return None
    verb = pos[0].text
    if verb in ('get', 'list'):
        return (pos[1].text.lower() if len(pos) > 1 else ''), None, False
    if verb in ('set', 'unset'):
        if len(pos) < 2:
            return None
        return pos[1].text.lower(), (pos[2] if len(pos) > 2 else None), True
    if any(o in CONFIG_READ_OPTS for o in opts):
        return verb.lower(), None, False
    writes = any(o in CONFIG_WRITE_OPTS for o in opts) or len(pos) > 1
    return verb.lower(), (pos[1] if len(pos) > 1 else None), writes


def git_prefix_configs(gc):
    """[(key, value text, first Word, last Word)] of the -c and --config-env settings before the
    git subcommand (key lower-case)."""
    a = list(gc.inv.args)
    a = a[:len(a) - len(gc.rest) - 1]
    out, k = [], 0
    while k < len(a):
        t = a[k].text
        if t in ('-c', '--config-env') and k + 1 < len(a):
            key, _eq, val = a[k + 1].text.partition('=')
            out.append((key.lower(), val, a[k], a[k + 1]))
            k += 2
            continue
        if (t.startswith('-c') and not t.startswith('--')) or t.startswith('--config-env='):
            body = t[2:] if t.startswith('-c') else t.split('=', 1)[1]
            key, _eq, val = body.partition('=')
            out.append((key.lower(), val, a[k], a[k]))
        k += 1
    return out


def _is_kit_hooks(text, cwd):
    p = sh.resolve_path(text, cwd) if text else None
    return bool(p) and same_dir(p, kit_githooks())


def _without(cmd, first, last):
    """The command, from its first word, with the words first..last cut out."""
    return (cmd.source[cmd.words[0].start:first.start].rstrip() + ' ' +
            cmd.source[last.end:cmd.words[-1].end].lstrip()).strip()


def _under_temp(ctx, d):
    if not d:
        return False
    top = os.path.normcase(os.path.abspath(ctx.tempdir)).rstrip('\\/')
    here_ = os.path.normcase(os.path.abspath(d))
    return here_ == top or here_.startswith(top + os.sep)


def clone_configs(gc):
    """[(key, value text, first Word, last Word)] of a `git clone`'s -c and --config settings,
    which it writes into the new repository's config (key lower-case)."""
    a = gc.rest
    out, k = [], 0
    while k < len(a):
        t = a[k].text
        if t in ('-c', '--config') and k + 1 < len(a):
            key, _eq, val = a[k + 1].text.partition('=')
            out.append((key.lower(), val, a[k], a[k + 1]))
            k += 2
            continue
        if (t.startswith('-c') and not t.startswith('--')) or t.startswith('--config='):
            body = t[2:] if t.startswith('-c') else t.split('=', 1)[1]
            key, _eq, val = body.partition('=')
            out.append((key.lower(), val, a[k], a[k]))
        k += 1
    return out


def _config_files(gc):
    """The paths a `git config` call names with -f/--file."""
    a, out = gc.rest, []
    for k, w in enumerate(a):
        if w.text in CONFIG_FILE_OPTS and k + 1 < len(a):
            out.append(a[k + 1].text)
        elif w.text.startswith('--file='):
            out.append(w.text.split('=', 1)[1])
    return out


# The variables git reads config settings from: GIT_CONFIG_PARAMETERS ('key'='value' ...) and
# GIT_CONFIG_KEY_<n> (with GIT_CONFIG_VALUE_<n>).
HOOKS_ENV = r'GIT_CONFIG_PARAMETERS|GIT_CONFIG_KEY_\d+'


def git_hooks_path(event, ctx, cmd, inv, cwd, role):
    """A change of core.hooksPath (git config, including -f on a repository's own .git/config;
    git -c, git --config-env, git clone -c; GIT_CONFIG_PARAMETERS or GIT_CONFIG_KEY_<n>), in any
    session: it switches the repository's hooks off or elsewhere. Setting it to the kit's own
    githooks (the install step), and repositories under the temp folder (not --global or
    --system), are left alone."""
    why = ('core.hooksPath picks the folder git runs hooks from, the kit\'s pre-push checks among them; the owner '
           'sets it at install.')
    for name, val in env_assignments(cmd, HOOKS_ENV):
        if HOOKS_KEY in val.lower():
            where = quote(cmd.cwd or cwd) if (cmd.cwd or cwd) else '"<checkout>"'
            return refuse('git-hooks-path', role, '`%s` sets %s to change core.hooksPath for the git commands that '
                          'read it. %s Ask the owner (through the PM) when it needs to change. To see it:' %
                          (bare(cmd), name, why), 'git -C %s config --get core.hooksPath' % where)
    gc = cm.git_call(inv)
    if gc is None:
        return None
    scope_all = any(w.text in ('--global', '--system') for w in gc.rest)
    if _under_temp(ctx, gc.dir) and not scope_all:
        return None
    configs = git_prefix_configs(gc) + (clone_configs(gc) if gc.sub == 'clone' else [])
    for key, val, first, last in configs:
        if key == HOOKS_KEY and not _is_kit_hooks(val, gc.dir):
            return refuse('git-hooks-path', role, '`%s` runs git with core.hooksPath=%s, which replaces the '
                          'repository\'s hooks. %s Run it without the setting:' %
                          (bare(cmd), val, why), _without(cmd, first, last))
    if gc.sub != 'config':
        return None
    if any(_under_temp(ctx, os.path.dirname(sh.resolve_path(f, gc.dir) or '')) for f in _config_files(gc)):
        return None                              # -f on a scratch repository's config under temp
    call = git_config_call(gc)
    if call is None:
        return None
    key, value, writes = call
    if key != HOOKS_KEY or not writes:
        return None
    if value is not None and not value.dyn and _is_kit_hooks(value.text, gc.dir):
        return None
    where = quote(gc.dir) if gc.dir else '"<checkout>"'
    return refuse('git-hooks-path', role, '`%s` changes core.hooksPath. %s Ask the owner (through the PM) when it '
                  'needs to change. To see it:' % (bare(cmd), why), 'git -C %s config --get core.hooksPath' % where)


# ---------------------------------------------------------------- kill by process name

PS_STOP = ('stop-process', 'spps')
PS_PROC_VALUES = ('-id', '-name', '-inputobject', '-module', '-computername') + \
    tuple('-' + p for p in PS_COMMON_VALUES)
PS_PROC_SWITCHES = ('-passthru', '-force', '-includeusername', '-fileversioninfo') + \
    tuple('-' + p for p in PS_COMMON_SWITCHES)
PS_PROC_ALIASES = {'-processname': '-name', '-pid': '-id', '-processid': '-id'}


def ps_proc_params(cmd, args):
    """({canonical parameter: value text}, [positional text]) of Stop-Process or Get-Process
    arguments. A word that is no known parameter counts as a switch."""
    names = PS_PROC_VALUES + PS_PROC_SWITCHES
    named, pos = {}, []
    k = 0
    while k < len(args):
        w = args[k]
        t = w.text
        if t.startswith('-') and len(t) > 1 and not w.dyn and not cm.quoted(cmd, w):
            p = cm.canon_param(t, names, PS_PROC_ALIASES) or t.lower()
            k += 1
            if ':' in t:
                named[p] = t.split(':', 1)[1]
            elif p in PS_PROC_VALUES and k < len(args):
                named[p] = args[k].text
                k += 1
            else:
                named[p] = ''
            continue
        pos.append(t)
        k += 1
    return named, pos


# The options of pkill and killall that take a value (-P 1234, -u name, --signal TERM): the value
# is no process name.
KILL_VALUE_SHORT = {'pkill': 'dgGPstuUF', 'killall': 'suoynZ'}
KILL_VALUE_LONG = {
    'pkill': ('--delimiter', '--pgroup', '--group', '--parent', '--session', '--terminal', '--euid', '--uid',
              '--pidfile', '--ns', '--nslist', '--signal', '--env', '--cgroup'),
    'killall': ('--signal', '--user', '--older-than', '--younger-than', '--ns', '--context'),
}
SIGNAL_OPT_RE = re.compile(r'^-(?:\d+|(?:SIG)?[A-Z][A-Z0-9+-]+)$')    # -9, -HUP, -SIGTERM, -RTMIN+1


def _kill_patterns(name, args):
    """The process-name patterns of a pkill or killall call: its words that are no option and no
    option's value."""
    short, long_ = KILL_VALUE_SHORT[name], KILL_VALUE_LONG[name]
    out, k = [], 0
    while k < len(args):
        t = args[k].text
        k += 1
        if t == '--':
            out += [w.text for w in args[k:]]
            break
        if not t.startswith('-') or t == '-':
            out.append(t)
            continue
        if t.startswith('--'):
            if '=' not in t and t in long_:
                k += 1
            continue
        if SIGNAL_OPT_RE.match(t):
            continue
        body = t[1:]
        for i, ch in enumerate(body):
            if ch in short:                      # the first value letter takes the rest of the word,
                if i == len(body) - 1:           # or the next word when it is last (-P 1234, -fu name;
                    k += 1                       # -ubuilds holds its own value)
                break
    return out


def killed_name(cmd, inv):
    """The process name a command ends processes by, or None (by id, or no kill)."""
    name, args = inv.name, inv.args
    if name == 'taskkill':
        for k, w in enumerate(args[:-1]):
            t = w.text.lower()
            if t in ('/im', '//im', '-im'):
                return args[k + 1].text
            if t in ('/fi', '//fi', '-fi') and 'imagename' in args[k + 1].text.lower():
                return args[k + 1].text.split()[-1]
        return None
    if name in ('pkill', 'killall'):
        pos = _kill_patterns(name, args)
        return pos[0] if pos else None           # no pattern: by parent, group, session, user or terminal
    if cmd.shell != sh.PS or name not in PS_STOP + ('kill',):
        return None
    named, pos = ps_proc_params(cmd, args)
    if '-name' in named:
        return named['-name'] or '<name>'
    if '-id' in named or '-inputobject' in named or pos:
        return None                              # a positional binds to -Id
    src = cmd.pipe_from
    ginv = cm.invocation(src) if src is not None else None
    if ginv is None or ginv.name not in ('get-process', 'gps', 'ps'):
        return None
    gnamed, gpos = ps_proc_params(src, ginv.args)
    if '-id' in gnamed or '-inputobject' in gnamed:
        return None
    return gnamed.get('-name') or (gpos[0] if gpos else '') or '<name>'


def kill_by_name(event, ctx, cmd, inv, cwd, role):
    """An agent session (a subagent, or a session marked as one of guard_agent_marks) ending
    processes by name: that ends every match, other sessions' builds and gates among them."""
    if inv is None or inv.dyn or not agent_session(event, ctx):
        return None
    target = killed_name(cmd, inv)
    if target is None:
        return None
    target = target.strip('"\'')
    if cmd.shell == sh.PS:
        look = 'tasklist /FI "IMAGENAME eq %s"' % target if inv.name == 'taskkill' else \
            'Get-Process -Name %s | Select-Object Id, StartTime, Path' % target
    elif os.name == 'nt':
        look = 'tasklist //FI "IMAGENAME eq %s"' % target
    else:
        look = 'pgrep -a %s' % target
    return refuse('kill-by-name', role, '`%s` ends every process named %s, other sessions\' builds and gates '
                  'among them. Find the one you started, check its start time is the one you saw, and stop that '
                  'one by its id. To list them:' % (bare(cmd), target), look)


# ---------------------------------------------------------------- the owner's identity

IDENTITY_KEYS = ('user.email', 'author.email', 'committer.email')
IDENTITY_ENV = r'(?:GIT_(?:AUTHOR|COMMITTER)_)?EMAIL'     # the variables git reads an identity's email from


def _names_email(text, email):
    """True when `text` holds `email` as a whole address (not inside a longer one)."""
    return re.search(r'(?i)(?<![\w.+-])%s(?![\w.-])' % re.escape(email), text or '') is not None


def _owner_identity_use(cmd, gc, email):
    """What the command does with the owner's email as a commit identity, or None."""
    for name, val in env_assignments(cmd, IDENTITY_ENV):
        if _names_email(val, email):
            return 'sets %s, which git reads a commit\'s identity from, to the owner\'s email' % name
    if gc is None:
        return None
    for key, val, _f, _l in git_prefix_configs(gc):
        if key in IDENTITY_KEYS and _names_email(val, email):
            return 'runs git with %s set to the owner\'s email' % key
    if gc.sub == 'commit':
        args = gc.rest
        for k, w in enumerate(args):
            body, eq, val = w.text.partition('=')
            if body.startswith('--') and 'author' in _canon_long('commit', body[2:]):
                v = val if eq else (args[k + 1].text if k + 1 < len(args) else '')
                if _names_email(v, email):
                    return 'names the owner as the commit\'s author'
    if gc.sub == 'config':
        call = git_config_call(gc)
        if call and call[0] in IDENTITY_KEYS and call[2] and call[1] is not None and _names_email(call[1].text, email):
            return 'sets %s to the owner\'s email' % call[0]
    return None


def owner_identity(event, ctx, cmd, inv, cwd, role):
    """A session other than the owner's own giving a commit the owner's email: git commit
    --author, git config or git -c user/author/committer.email, or GIT_AUTHOR_EMAIL,
    GIT_COMMITTER_EMAIL or EMAIL. Owner-only paths are judged by the author email
    (kitlib.guard.ownercommits), so these would pass an agent's change as the owner's."""
    if is_owner_session(event, ctx):
        return None
    where = cmd.cwd or cwd
    cfg = ctx.config(where) if where else None
    email = cfg.owner_rules().owner_email if cfg is not None else ''
    if not email:
        return None
    gc = cm.git_call(inv)
    found = _owner_identity_use(cmd, gc, email)
    if found is None:
        return None
    d = (gc.dir if gc is not None else None) or where
    return refuse('owner-identity', role, '`%s` %s. Owner-only paths are judged by the author email, so that '
                  'passes an agent\'s change as the owner\'s. Commit as yourself, with the identity git config '
                  'holds; to see it:' % (bare(cmd), found), 'git -C %s config --get user.email' % quote(d))
