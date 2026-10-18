"""The guard's rules (ARCHITECTURE.md "Guard rules"; docs/guard.md "Rules").

decide(action, ctx) -> kitlib.host.Decision ('allow' when no rule refuses). `action` is the neutral
kitlib.host.Action a host adapter parsed its hook payload into (hosts/claude/adapter/events.py for
Claude Code); no rule here reads a host's payload. Every rule is a read-only check of real state and
every deny names the exact command to use instead (also in Decision.instead). Each rule below
takes the Action as its first argument, named `event` from before the split; a rule answers None
for an allow, and decide() turns that into Decision('allow'). A deny reason ends with the tag
`[kit guard: rule=<rule> role=<role>]`; hosts.claude.adapter.guard_selftest reads those tags back out of
subagent transcripts.

Rules, in the order they are checked:
  lock-mcp         the lock MCP's owner-only tools: a subagent or a launcher-marked session is
                   denied; an unmarked main session (the owner's own) is allowed
  hub-mcp          the hub MCP server's owner tools (hub_answer, hub_todo_done, hub_story_submit,
                   hub_to_pm_send, a hub tool named for decide or approve), in any session but the
                   owner's own
  hub-browser      a browser or computer-use MCP call whose input names the hub's own origin (a
                   loopback host on a hub port), in any session but the owner's own
  owner-lock      a shell command running reserve_ui, or running a lock CLI owner-only write
                   (reserve, release, hold whole|half|slots, division --set, game on|off|set,
                   top, stop/unbook --force), in any session but the owner's own
  owner-setup      a shell command running machine setup to act (tools/machine_setup.py --apply, its
                   PowerShell front ends with -Apply, python code importing kitlib.machinesetup), in
                   any session but the owner's own: it writes the owner's email
  hub-secrets      a shell command naming the hub's token folder or a token file (owner.token,
                   agent.token, approval.key), reaching the token folder or the hub store (the
                   store through tools/hub.py's own --store excepted), or python or node code that
                   names them or the hub site; an edit of a file in either folder; a read tool's
                   read of the token folder or a file in it, or a search that can read or list
                   one (the store stays readable); in any session but the owner's own
                   (kitlib.guard.hub)
  hub-owner-verb   fail-closed: a shell line whose text names the hub tool as a module or
                   a script (tools/hub.py, tools.hub, an import of hub, python -m hub, a standalone
                   hub on a line that runs python and holds import) anywhere outside the one shape
                   the guard reads
                   in full (HUB_SHAPE: python, plain options, tools/hub.py or -m tools.hub, an agent
                   verb, every word literal with no unquoted wildcard) and outside the path family
                   (hub_path_family: one simple command of cat, Get-Content, type, grep, rg,
                   Select-String, git add|commit|diff|log|show|blame|status|restore --staged,
                   commit.py, backlog.py or ledger.py naming hub.py only as a path, or anyhow in a
                   commit, ledger or backlog message; git -c, --exec-path, --ext-diff, --textconv,
                   commit's editor and prompts, and rg --pre, --pre-glob, --hostname-bin denied;
                   hub_family: such commands on a plain line of ;, &&, || or newlines whose other
                   commands are read as before), and that shape running an owner verb (answer, open, pair, unpair, stop,
                   restart, todos done, stories add|withdraw, to-pm add|send, inbox read, approvals
                   approve|deny, todos add --owner), in any session but the owner's own. Known
                   limit: a name built at run time (string concatenation, an encoding, a variable,
                   a script the agent wrote) is not read; the rule guards against accidents and is
                   not an adversary boundary
  ledger           an edit of the machine config's ledger_path, by anyone
  lock-state       an agent session's edit of a file in the machine lock's state folder
  kit-json         an agent session's edit of a .kit.json in a checkout
  generated-file   an edit of a file tools/render_content.py writes in a kit checkout (a host's
                   agents, skills copies, plugin manifests: kitlib.guard.generated), in any session
  main-checkout    an edit in a main checkout by a role whose edit_main_checkout is false
  write-jail       an agent session's write outside its own checkout, its repository's other
                   worktrees, the temp folder and the .kit.json data roots (kitlib.guard.jail);
                   git that writes a checkout outside them
  wrapped-gate     gate.py or `machine_lock.py run` piped into another command (P16)
  root-search      a recursive search from a drive or filesystem root
  heavy            a heavy program run with no lock held (MACHINE_LOCK_RUN verified by nest_check)
  git-*            damaging git in a main checkout; branch -D anywhere; a push that deletes or
                   force-pushes main, in every repository (with no .kit.json too); a push to main
                   with no gate-OK record when the repo sets repo.push_block; push --no-verify, send-pack,
                   http-push, a remote helper or a git alias for one in a repository with a PM
                   data record, and any push from a copy of PM data without one; a remote
                   repointed (git-remote-rewrite) in a repository with one; all in any session
                   but the owner's own
  git-shared-ref   git update-ref by an agent session (a subagent or a jailed mark but pm) from any
                   checkout, of a ref other than refs/tmp/<name>, a per-worktree ref, or in a linked
                   worktree its HEAD and the branch checked out there (never a guarded main); a
                   ref or a repository the text does not name is refused too
  git-hooks-path   a change of core.hooksPath (git config, git -c, clone -c, GIT_CONFIG_*), in
                   any session
  owner-identity   the owner's email given to a commit, in any session but the owner's own
  kill-by-name     an agent session ending processes by name (taskkill /IM, Stop-Process -Name)
  heredoc-winpath  python reading a bash heredoc with a single-backslash Windows path in a string
  busy-wait        a foreground sleep loop with no counter, clock or timeout bounding it
A command line the parser cannot read cleanly also goes through kitlib.guard.fallback.
"Agent session" and "the owner's own" are the session mark's (docs/guard.md "Session mark").
"""
import ast
import builtins
import fnmatch
import json
import ntpath
import os
import posixpath
import re

from kitlib import agentname
from kitlib import config as kconfig
from kitlib import kitroot
from kitlib import pmdata
from kitlib.git import repo
from kitlib import lockmcp as lockmcp_names
from kitlib.host import Decision

from . import commands as cm
from . import context
from . import generated
from . import hub as hubg
from . import jail
from . import paths
from . import shell as sh

DENY, ASK, ALLOW = 'deny', 'ask', 'allow'
# kitlib.host.Action.shell -> the parser's dialect
SHELLS = {'bash': sh.BASH, 'powershell': sh.PS}
TAG = '[kit guard: rule=%s role=%s]'
NULL_TARGETS = {'/dev/null', '$null', 'nul', 'nul:', '&1', '&2'}


def tag(rule, role):
    return TAG % (rule, role)


def refuse(rule, role, what, instead, kind=DENY):
    return Decision(kind, rule, '%s Instead: %s %s' % (what.rstrip(), instead.strip(), tag(rule, role)),
                    instead.strip())


def reworded(d, reason):
    """`d` with its reason replaced (a rule that adds what led to it)."""
    return Decision(d.verdict, d.rule, reason, d.instead)


def fwd(path):
    return path.replace('\\', '/')


def quote(path):
    return '"%s"' % fwd(path)


def literal_word(path):
    """`path` as one word that bash and PowerShell both read literally: single-quoted when it holds
    a `$` or a backtick (which expand inside double quotes) and no single quote; otherwise
    double-quoted, as quote() does. A path with both a single quote and one of those has no such
    word and stays double-quoted (docs/guard.md "Rules", the `heavy` row)."""
    p = fwd(path)
    if ('$' in p or '`' in p) and "'" not in p:
        return "'%s'" % p
    return quote(p)


def lock_tool(ctx):
    """The lock CLI an alternative names: tools/machine_lock.py of the kit the guard runs from
    (ctx.kit, kitlib.kitroot.lock_tool), as one literal word (literal_word) since a kit path may
    hold a space or a `$`. Its `run` takes no --lock flag: it always locks."""
    return literal_word(kitroot.lock_tool(ctx.kit))


def kit_tool(ctx, name):
    """A kit tool an alternative names: tools/<name>.py of the kit the guard runs from (ctx.kit),
    quoted. A repository holds no copy of the kit's tools (decision 21 in docs/multi-host-plan.md
    section 7), so an alternative never names a repository's own tools folder."""
    return quote(kitroot.tool(name, ctx.kit))


def subagent(event):
    """True when the Action names a subagent (Action.agent): a host's hook told it apart."""
    return bool(getattr(event, 'agent', None))


def tag_role(event):
    """'main' for the main session, else 'subagent' (the tag the self-test reads)."""
    return 'subagent' if subagent(event) else 'main'


# ---------------------------------------------------------------- entry

def decide(event, ctx=None, environ=None, profile=None):
    """The Decision on the kitlib.host.Action `event`: 'allow', or the first rule's deny or ask.
    With no `ctx`, a context.Ctx of `environ` (default os.environ) and the host `profile` (a
    HostProfile or a callable returning one) is built here, so its reads fail inside decide.
    The shared rules judge first, so a call they refuse is refused as it always was; a call they
    allow then meets the role policy (kitlib.guard.rolepolicy), which is off for a role whose
    .kit.json entry names no mode, and, when it adds write-jail roots, the shared rules again."""
    from . import rolepolicy
    ctx = ctx or context.Ctx(environ=environ, profile=profile)
    d = judge(event, ctx)
    if d:
        return d
    d, held = rolepolicy.screen(event, ctx)
    if d is None and held is not ctx:
        d = judge(event, held)
    return d or Decision(ALLOW)


def judge(event, ctx):
    """decide() with None for an allow: the shared rules, before any role policy."""
    tool = event.tool or ''
    cwd = (event.workdir if event.kind == 'shell' else None) or event.cwd or os.getcwd()
    role = tag_role(event)
    d = lock_mcp(event, ctx, tool, role)
    if d:
        return d
    if event.kind == 'edit':
        target = event.path
        if not isinstance(target, str) or not target:
            return None
        target = sh.resolve_path(target, cwd, home=getattr(ctx, 'home', None)) or target
        form = event.retarget
        return edit_target(event, ctx, target, cwd, role, '%s of %s' % (tool, target),
                           rewrite=(lambda path: form.replace('{path}', fwd(path))) if form else None)
    if event.kind == 'shell':
        text = event.command
        if not isinstance(text, str) or not text.strip() or event.shell not in SHELLS:
            return None
        return shell_command(event, ctx, text, SHELLS[event.shell], cwd, role)
    if event.kind == 'mcp':
        return hub_mcp(event, ctx, tool, role)
    if event.kind == 'read':
        return hub_secret_read(event, ctx, cwd, role)
    return None


def shell_command(event, ctx, text, shell, cwd, role):
    parsed = cm.read(text, shell, cwd, home=ctx.home)
    hub_rules = HubShell(event, ctx, parsed)
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        for rule in (owner_lock, owner_setup, hub_rules.secrets, hub_rules.owner_verb, wrapped_gate, root_search,
                     heavy, git_rules, git_hooks_path, owner_identity, kill_by_name, heredoc_winpath, shell_writes):
            d = rule(event, ctx, cmd, inv, cwd, role)
            if d:
                return d
    d = hub_rules.owner_verb(event, ctx, None, None, cwd, role)   # a line with no command read
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

def host_mcp_prefix(profile):
    """The lock MCP's tool-name prefix on the host `profile` (kitlib.host.HostProfile) describes:
    its mcp_tool_format with the kit's plugin and the lock server's name filled in and the tool
    left off. INFERRED for Claude Code, not observed: the kit has no installed plugin yet."""
    return profile.mcp_tool_format.format(plugin=lockmcp_names.PLUGIN_NAME,
                                          server=lockmcp_names.SERVER_NAME, tool='')


def host_mcp_separator(profile):
    """What the host's MCP tool names put between the server and the tool (`__` in
    mcp__<server>__<tool>): the text between {server} and {tool} in mcp_tool_format."""
    fmt = profile.mcp_tool_format
    i, j = fmt.find('{server}'), fmt.find('{tool}')
    return fmt[i + len('{server}'):j] if 0 <= i < j else ''


def mcp_prefix(ctx):
    """The machine config's guard_mcp_prefix, else the session's host's (host_mcp_prefix)."""
    return ctx.user().get('guard_mcp_prefix') or host_mcp_prefix(ctx.host_profile())


def mcp_owner_tool(ctx, tool, kind='mcp'):
    """The owner-only lock MCP tool `tool` names, or None: under the configured prefix, under any
    other server name (the tail after the host's separator, `mcp__<server>__<tool>`), or listed in
    full in guard_mcp_tools. Only an MCP call (Action kind 'mcp') names one."""
    if kind != 'mcp' or not tool:
        return None
    from kitlib.lockmcp import server as lockmcp   # here, not at the top: only MCP calls pay its import
    prefix = mcp_prefix(ctx)
    if tool.startswith(prefix) and tool[len(prefix):] in lockmcp.OWNER_ONLY:
        return tool[len(prefix):]
    sep = host_mcp_separator(ctx.host_profile())
    tail = tool.rsplit(sep, 1)[-1] if sep else tool
    if tail in lockmcp.OWNER_ONLY:
        return tail                             # the same tool under another server name
    if tool in (ctx.user().get('guard_mcp_tools') or ()):
        return tool
    return None


def lock_mcp(event, ctx, tool, role):
    name = mcp_owner_tool(ctx, tool, event.kind)
    if not name:
        return None
    who = ctx.session_role(event)
    if who == context.OWNER:
        return None                             # a session nothing marks is the owner's own
    what = '%s changes the machine lock and is the owner\'s alone (ARCHITECTURE.md "Guard rules").' % name
    status = 'python %s status' % lock_tool(ctx)
    return refuse('lock-mcp', role, what + ' This session is %s. Ask the owner (through the PM) to make the change; '
                  'the read tools (lock_snapshot, lock_run, lock_hold) stay open. To see the lock state:' % who, status)


def is_owner_session(event, ctx):
    """True for the owner's own main session: no subagent and no launcher mark (docs/guard.md
    "Session mark")."""
    return not subagent(event) and ctx.session_role(event) == context.OWNER


def agent_session(event, ctx):
    """True for a subagent, or a session whose mark is one of guard_agent_marks (default
    AGENT_MARKS: coordinator, its old name conductor, deep-worker, consultant and pm), case
    ignored; a guard_agent_marks value replaces the default list. The rules only agents are held to (kill by name, lock state,
    kit-json, the write jail) use it. The mark is compared without the kit plugin's prefix
    (kitlib.agentname.bare_mark), so a role_resolver that does not strip it fails closed too."""
    if subagent(event):
        return True
    marks = ctx.user().get('guard_agent_marks') or AGENT_MARKS
    who = agentname.bare_mark(str(ctx.session_role(event) or ''))
    return who.lower() in {str(m).lower() for m in marks}


# every session kind the kit launcher marks but `plain` and other agents' names; 'conductor' is the
# coordinator's old mark, until the launcher is renamed
AGENT_MARKS = ('coordinator', 'conductor', 'deep-worker', 'consultant', 'pm')


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
                  'python %s status' % lock_tool(ctx))


def is_lock_script(text):
    """True when the path `text` opens the lock CLI on Windows: its base name
    (commands.base_name, in any case) is LOCK_SCRIPT once a named NTFS stream (`::$DATA`, `:x`)
    and then the trailing dots and spaces Win32 drops are taken off: `tools/machine_lock.py.`,
    `"tools/machine_lock.py "`, `tools/machine_lock.py::$DATA` (observed: each opens the file). A
    colon with no stream name after it (`machine_lock.py:`, as in a commit message
    `machine_lock.py: fix`) is a name Windows refuses (EINVAL), so not it."""
    name, colon, stream = cm.base_name(text).partition(':')
    if colon and not stream.strip(':. '):
        return False
    return name.rstrip('. ') == LOCK_SCRIPT


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
    Start-Process, a direct path; nested in a `run` too; named in any spelling Windows opens as it,
    is_lock_script) with an owner-only write. The owner's own session (nothing marks it) is not
    held to it."""
    if inv is None or is_owner_session(event, ctx):
        return None
    if runs_reserve_ui(cmd, inv):
        return owner_lock_refusal(ctx, role, '`%s` runs reserve_ui, the owner\'s lock reservation UI' % bare(cmd))
    seq = [inv.path] + inv.argv
    for k, t in enumerate(seq):
        if not is_lock_script(t):
            continue
        rest = []
        for w in seq[k + 1:]:
            rest.extend(w.split() if ' ' in w else [w])     # a Start-Process "-ArgumentList" string
        found = lock_owner_write(rest)
        if found:
            return owner_lock_refusal(ctx, role, '`%s` runs machine_lock.py %s' % (bare(cmd), found))
    return None


# ---------------------------------------------------------------- owner machine setup

# Machine setup's --apply writes the machine config, the owner's email among them (plan decision 27),
# and the owner's email is what proves a commit the owner's (owner-only paths, owner-identity). An agent
# that ran it could make its own address the owner's, so only the owner runs it to act. Its dry run stays
# open. The entry points: tools/machine_setup.py and its PowerShell front ends; python -c or a script
# on standard input that imports kitlib.machinesetup counts too.
SETUP_SCRIPTS = {'machine_setup.py': 'py', 'machine-setup.ps1': 'ps', 'new-repo.ps1': 'ps'}
SETUP_RE = re.compile(r'(?i)(?<![A-Za-z0-9_])(?:machine[_-]setup|new-repo|machinesetup)(?![A-Za-z0-9_])')
SETUP_IMPORT_RE = re.compile(r'(?i)kitlib(?:[./\\]|\s+import\s+)machinesetup')
# argparse takes a unique prefix of --apply (--ap..); PowerShell a prefix of -Apply, -Apply:$true too
SETUP_APPLY = {'py': re.compile(r'^--ap(?:p(?:l(?:y)?)?)?(?:=.*)?$'),
               'ps': re.compile(r'(?i)^-a(?:p(?:p(?:l(?:y)?)?)?)?(?::.*)?$')}
OWNER_SETUP_WHY = ('machine setup writes the machine config, and the owner\'s email there decides which commits '
                   'count as the owner\'s, so only the owner runs it to act. Ask the owner to run it. To see '
                   'what it would do (the dry run):')


def setup_run(cmd, inv):
    """What `inv` runs of machine setup with its writes on, as text, or None: a setup entry point
    (SETUP_SCRIPTS: its program, or its script after python, powershell -File, Start-Process) with
    --apply / -Apply or a word the text does not show; python -c, or python reading a script on
    standard input, that imports kitlib.machinesetup. Its dry run is None."""
    source = cmd.source or ''
    if not SETUP_RE.search(source):
        return None
    words = ([inv.word] if inv.word is not None else []) + list(inv.args)
    py = UI_PYTHON_RE.match(inv.name or '') is not None
    stdin = bool(cmd.piped_in) or any(r.op.startswith('<') for r in cmd.redirects)   # <, a heredoc, <<<
    if py and (any(w.text == '-c' or re.match(r'^-[A-Za-z]*c', w.text) for w in inv.args) or stdin) \
            and SETUP_IMPORT_RE.search(source):
        return 'python code that imports kitlib.machinesetup'
    for k, w in enumerate(words):
        name = cm.base_name(w.text)
        style = SETUP_SCRIPTS.get(name)
        if style is None and k > 0 and words[k - 1].text == '-m' and w.text.lower().endswith('machine_setup'):
            style = 'py'
        if style is None:
            continue
        rest = []
        for a in words[k + 1:]:
            if a.dyn:
                return '%s with an argument the text does not show' % name
            if style == 'ps' and '"' in a.text:
                # Windows PowerShell 5.1 does not escape it when it passes the value on to python
                return '%s with a double quote inside an argument, which can split it into more' % name
            rest.extend(a.text.split() if ' ' in a.text else [a.text])     # a Start-Process "-ArgumentList" string
        for t in rest:
            if t == '--':
                break
            if SETUP_APPLY[style].match(t):
                return '%s %s' % (name, t)
        return None
    script = cm.python_script(inv) if py else None
    if inv.dyn or (script is not None and inv.args[script[0]].dyn):
        return 'a program or script the text does not show, on a line that names machine setup'
    return None


def owner_setup(event, ctx, cmd, inv, cwd, role):
    """A command that runs machine setup to act (setup_run), in any session but the owner's own."""
    if inv is None or is_owner_session(event, ctx):
        return None
    found = setup_run(cmd, inv)
    if not found:
        return None
    return refuse('owner-setup', role, '`%s` runs %s: %s' % (bare(cmd), found, OWNER_SETUP_WHY),
                  'python %s' % kit_tool(ctx, 'machine_setup'))


# ---------------------------------------------------------------- the Action Hub

# docs/guard.md "Hub secrets". The hub's token folder (owner.token answers as the owner,
# agent.token is every agent's, approval.key will sign the owner's approvals) and its store are
# reached by agents only through tools/hub.py and the hub MCP server, which read agent.token in
# their own process; the hub server's git sync writes the store. So an agent's own edit or shell
# command that names them is refused (hub-secrets), as are tools/hub.py's owner verbs
# (hub-owner-verb), the hub MCP server's owner tools (hub-mcp) and a browser or computer-use call
# aimed at the hub's own page (hub-browser). Every session but the owner's own is held, as
# owner-lock holds it: each is the owner's power, not a place an agent works.
HUB_SCRIPT = 'hub.py'
HUB_MODULE = 'tools.hub'
# hub-owner-verb is fail-closed: in a held session any command text that names the hub
# tool as a module or a script is refused, unless the command is the one shape hub_shape reads in
# full. HUB_NAME_RES are the namings, each searched with the case ignored, in the line's own text
# and in every command's words as the shell passes them (hub_named):
#   hub.py as a word or a path's last part (tools/hub.py, ./hub.py; test_rules_hub.py is not it),
#   and the compiled hub python runs as well (hub.pyc, __pycache__/hub.cpython-312.pyc) or hub.pyw;
#   a dotted name ending in hub (tools.hub, 'tools.hub'; tests.hub.test_x and tools.hubsite are not);
#   an import of hub: import hub, import os, tools.hub as h, from tools import (x, hub),
#   from .hub import main, from tools.hub import main;
#   python's -m (or a cluster ending in m, --module, `runpy`) naming hub itself: cd tools && python -m hub
#   (hub_module_names).
HUB_NAME_RES = (
    re.compile(r'(?i)(?<![\w-])hub(?:\.py[cw]?|(?:\.[\w-]+)+\.pyc)\b'),
    re.compile(r'(?i)(?<=\w)\.hub(?![\w.-])'),
    re.compile(r'(?i)import\s+(?:[\w. \t]+,\s*)*(?:\w+\s*\.\s*)*hub(?![\w-])'),
    re.compile(r'(?i)\bfrom\s+(?:\w+\s*\.\s*)*\.*\s*hub\s+import\b'),
    re.compile(r'(?i)\bfrom\s+[\w.]+\s+import\s*\(?\s*(?:[\w \t]+,\s*)*hub(?![\w-])'),
)
# hub_module_names: a python word (python, py, pythonw, python3.12.exe), then on the same command
# (`mid`, no ; | & or newline) a -m naming hub
HUB_PY_WORD_RE = re.compile(r'(?i)(?<![\w.-])py(?:thon)?w?(?:\d+(?:\.\d+)*)?(?:\.exe)?(?![\w.-])')
HUB_PY_MODULE_RE = re.compile(r'''(?i)(?P<mid>[^;|&\n]*?)(?:(?<![\w-])-[A-Za-z]*m[ \t]*|'''
                              r'''(?<![\w-])--module(?:[ \t]+|=)|\brunpy[ \t]+)['"]?hub(?![\w.-])''')
# the kit's record tools, whose own -m is a message: `python <kit>/tools/commit.py -m "Hub store: x"`
# runs commit.py, and python reads no option after its script word
HUB_PY_MESSAGE_SCRIPTS = frozenset(('commit.py', 'backlog.py', 'ledger.py'))
HUB_PY_PLAIN_WORD_RE = re.compile(r'''^(['"]?)([\w./\\:+,=-]+)\1$''')
# a word before that script word (an option or its value) the shell passes as written: no quote,
# escape, backtick, variable, wildcard or brace, which could join or split words; ASCII only, so
# no Unicode space or control is taken for part of a plain word
HUB_PY_OPT_WORD_RE = re.compile(r'^[\w./:+,=-]+$', re.ASCII)
# the text glued to a python word up to bash's word break (space, tab) or a command's end; a ;
# is taken in, as cmd ends only a command's name there (hub_module_names)
HUB_PY_GLUED_RE = re.compile(r'[^ \t\n|&]*')
# hub_module_names on a line the parser could not read whole: `mid` crosses ; | & and newline too,
# so a separator the shell may keep inside one of python's words cannot hide its -m (fail-closed)
HUB_PY_MODULE_ANY_RE = re.compile(HUB_PY_MODULE_RE.pattern.replace('[^;|&\\n]*?', '[\\s\\S]*?', 1))
# hub_word_module_names: one of python's words, as the shell passes it, that is a -m (a cluster
# ending in m) naming hub glued to it (-mhub, -Bmhub), or that takes the module as the next word
# (-m, -Bm, --module, runpy); --module=hub; and the module word itself
HUB_PY_WORD_MODULE_GLUED_RE = re.compile(r'''(?i)^(?:-[A-Za-z]*m[ \t]*|--module=)['"]?hub(?![\w.-])''')
HUB_PY_WORD_MODULE_NEXT_RE = re.compile(r'(?i)^(?:-[A-Za-z]*m|--module)$|(?:\b|^-[A-Za-z]*m|^--module=)runpy$')
HUB_PY_WORD_HUB_RE = re.compile(r'''(?i)^[ \t]*['"]?hub(?![\w.-])''')
# hub quoted as a name ('hub', ".hub") counts in a command whose text also loads a module by name:
# __import__('tools', fromlist=['hub']), importlib.import_module('.hub', 'tools'), run_module('hub')
HUB_QUOTED_RE = re.compile(r'''(?i)['"]\.*hub['"]''')
HUB_LOADER_RE = re.compile(r'(?i)import|run_module|runpy|run_path|fromlist|find_spec|loader')
# the guard's own module, kitlib.guard.hub, in every spelling, read as another name first:
# kitlib/guard/hub.py, kitlib.guard.hub, from kitlib.guard import rules, hub as h
HUB_EXEMPT_RES = (
    re.compile(r'(?i)kitlib\s*[./\\]\s*guard\s*[./\\]\s*hub(?:\.py)?(?![\w-])'),
    re.compile(r'(?i)\bfrom\s+kitlib\s*\.\s*guard\s+import\s+(?:\(\s*[\w\s,]*\)|\w+(?:[ \t]+as[ \t]+\w+)?'
               r'(?:[ \t]*,[ \t]*\w+(?:[ \t]+as[ \t]+\w+)?)*)'),
)
# python's own options the allowed shape takes: flags that take no value (no -c, -m, -W, -X, -i,
# -x), as a cluster, and the py launcher's version (-3, -3.12, -3.12-64, -V:3.12)
HUB_SHAPE_OPT_RE = re.compile(r'^-(?:[bBdEIOPqRsSuv]+|[23](?:\.\d+)?(?:-(?:32|64))?|V:[\w.\-/]+)$')
HUB_SHAPE = 'python, its plain options, then tools/hub.py or -m tools.hub, then an agent verb, every word literal'
# a line that runs python and holds `import` anywhere names the hub tool by any standalone `hub`
# on it: Python takes a line join (a backslash before the newline) or a comment inside a bracketed
# import list between `import` and `hub`, and takes code from many places (-c, -m code, a pipe of
# any length), so neither the import nor where the code comes from is modelled (hub_code_names).
# x.hub is HUB_NAME_RES's (tests.hub.test_x is not hub). A folder named hub, hub/ (a path:
# <dir>/hub/todos), is not the tool; hub\ still counts, as a backslash may be Python's line join.
HUB_BARE_RE = re.compile(r'(?i)(?<![\w-])(?<!\w\.)hub(?![\w/-])')
HUB_IMPORT_RE = re.compile(r'(?i)\bimport\b')
# a line that can make `cat` run another program, or send what cat writes to one (exec's or
# coproc's descriptors, code run by eval, source or .), so a heredoc cat takes is read
# (hub_written_bodies). Read anywhere on the line, heredoc bodies included: fail-closed
HUB_CAT_REDEFINED_RE = re.compile(r'(?<![\w-])cat\s*\(\s*\)|\bfunction\s+cat\b|\bhash\b|\balias\b|\benable\b'
                                  r'|\bexec\b|\beval\b|\bsource\b|\bcoproc\b|(?<![^\s;&|(){}])\.(?![^\s;&|()])')
# the interpreters whose word makes a line one that runs python for hub_code_names (a program's
# base name, cm.base_name: case and .exe folded, any path before it): UI_PYTHON_RE's, plus ipython
# and pypy, with a version (pypy3, pypy3.10, ipython3). Kept apart from UI_PYTHON_RE, whose other
# readers (the allowed shape, the path family, inline_code) take only CPython
HUB_CODE_PYTHON_RE = re.compile(r'^(python|pythonw|py|pyw|ipython|pypy)(\d+(\.\d+)*)?$')
# The path family, the one other form the guard reads in full (hub_path_family, hub_family;
# docs/guard.md "Hub secrets"): a simple command of a program that only reads or records a file,
# naming the hub tool only as a path argument (a word of its own whose last part is hub.py), or
# naming it anyhow in a commit, ledger or backlog message. Each program's options that can run
# another program are refused by name, a long option's unique prefix included (git abbreviates
# long options). The command is the line's one command, or one of a plain line's (hub_family).
HUB_READERS = frozenset(('cat', 'get-content', 'type', 'grep', 'rg', 'select-string'))
HUB_READER_DENIED = {'rg': ('--pre', '--pre-glob', '--hostname-bin')}
HUB_GIT_SUBS = frozenset(('add', 'commit', 'diff', 'log', 'show', 'blame', 'status', 'restore'))
HUB_GIT_DENIED = ('--ext-diff', '--textconv')
# git add's, commit's and restore's own options that open an editor or ask (and restore's that
# write the work tree); commit's -c is --reedit-message (-C, which reuses one, opens no editor)
HUB_GIT_SUB_DENIED = {'add': (('--edit', '--interactive', '--patch'), 'eip'),
                      'commit': (('--edit', '--interactive', '--patch', '--reedit-message'), 'epc'),
                      'restore': (('--patch', '--worktree'), 'pW')}
# git commit's message: -m v, -mV, --message v, --message=v, and a cluster of flags that take no
# value ending in m (-am v, -amV); its value is text, not a path or an option
HUB_GIT_MESSAGE_RE = re.compile(r'^-[aqvsn]*m(.*)$', re.S)
HUB_COMMIT_VALUES = ('-C', '-m', '-b', '--body-file', '--model', '--host', '--provider', '--owner-approved')
HUB_COMMIT_FLAGS = ('--owner-authored', '--lander-main-checkout', '-h', '--help')
HUB_COMMIT_MESSAGES = ('-m', '-b')
HUB_BACKLOG_MESSAGES = ('--summary', '--reason', '--why', '--why-not-folded', '--owner-ask')
# tools/ledger.py's options whose value is text: an entry's fields, a retirement's why, the commit title
HUB_LEDGER_MESSAGES = ('--q', '--a', '--decides', '--source', '--title', '--why')
# the text the parser leaves after a command's span that the path family takes: stream dups (2>&1)
HUB_DUPS_RE = re.compile(r'^(?:\s*[\d*]?>&[\d-])*\s*$')
# A plain line (hub_family): commands joined only by these operators, so no command's output or
# state reaches another but through a file, a variable or the shell's own memory, each refused
# below, or through what git keeps (a commit's message, the index), which a later command may read
# and run: that is a script the agent wrote, a documented limit (hub_named), not modelled
HUB_LINE_OPS = frozenset((None, ';', '&&', '||', '\n'))
# words that define or change what a later name runs (an alias, a function, a variable, a module,
# an item on a provider drive, a drive of its own: a New-PSDrive on the Function provider makes a
# Get-Content item on it a function), read the shell's memory (history, the call stack), run text as a
# command, or open a compound command whose body the line's own order does not show. A write to
# the env drive in any spelling is an environment variable set (commands.line_env_names); the
# alias, function and variable drives are HUB_LINE_DRIVES
HUB_LINE_DENIED = frozenset((
    'alias', 'unalias', 'function', 'filter', 'hash', 'enable', 'source', '.', 'shopt', 'eval', 'exec',
    'builtin', 'declare', 'typeset', 'local', 'readonly', 'export', 'set', 'unset', 'read', 'mapfile',
    'readarray', 'trap', 'history', 'fc',
    'for', 'while', 'until', 'do', 'done', 'if', 'then', 'else', 'elif', 'fi', 'case', 'esac', 'select',
    'coproc', 'foreach', 'switch',
    'set-alias', 'sal', 'new-alias', 'nal', 'import-alias', 'ipal', 'import-module', 'ipmo',
    'invoke-expression', 'iex', 'get-variable', 'set-variable', 'new-variable', 'clear-variable',
    'remove-variable', 'gv', 'sv', 'nv', 'clv', 'rv', 'get-history', 'ghy', 'h', 'r', 'invoke-history',
    'ihy', 'get-pscallstack', 'set-item', 'si', 'new-item', 'ni', 'new-psdrive', 'ndr', 'mount'))
# text naming a provider drive or preference that changes what a name runs or where output goes
HUB_LINE_DRIVES = ('alias:', 'function:', 'variable:', 'psvariable', 'psdefault', 'pscallstack')
# PowerShell's common parameters that store a command's output or messages in a variable, matched
# as a prefix of two letters or more (-ou, -OutV, -outvariable:c; -p alone, the one a single
# letter can name), and their aliases; PowerShell takes an en dash, em dash or bar for the hyphen
HUB_LINE_PS_VARS = ('-outvariable', '-pipelinevariable', '-errorvariable', '-warningvariable',
                    '-informationvariable')
HUB_LINE_PS_ALIASES = ('-ov', '-pv', '-ev', '-wv', '-iv')
HUB_LINE_PS_DASHES = sh.PS_DASHES
# what one command's text, value or history reaches another by: bash's $_ and ${_} (the last
# word), $! and ${!...}, BASH_ variables, history expansion (!!, !$, !*, !^); PowerShell's $$, $^,
# ${$}, ${^}, $_, and the automatic variables holding a command's text or errors
HUB_LINE_TEXT_RE = re.compile(r'(?i)\$\{?_(?!\w)|\$\{?!|BASH_\w+|\$\$|\$\^|\$\{[$^]\}'
                              r'|\$\{?(?:\w+:)?(?:error|myinvocation|psitem|executioncontext)\b|![!$*^]')
NODE_EVAL = ('-e', '--eval', '-p', '--print')
HUB_SECRETS_WHY = ('the Action Hub\'s token folder holds the owner\'s key to the hub (owner.token answers as the '
                   'owner) and its store is the hub server\'s to write; an agent reaches the hub only through its '
                   'tool and its MCP server, which read agent.token themselves (docs/guard.md "Hub secrets").')
HUB_OWNER_WHY = ('answering, closing, submitting, sending and approving on the Action Hub are the owner\'s alone '
                 '(docs/guard.md "Hub secrets").')


def hub_config(ctx):
    """The machine config's `hub` object: the strict read's, else (a config the strict read
    refuses) the file's own JSON, else {}. Its store and ports are read even from a config with
    a fault elsewhere."""
    user = ctx.user()
    obj = user.get('hub')
    if isinstance(obj, dict):
        return obj
    if not user.get('error'):
        return {}
    try:
        with open(ctx.config_location().path, encoding='utf-8-sig') as f:
            data = json.loads(f.read())
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    obj = data.get('hub') if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else {}


def hub_real(path):
    """jail.real(path), or None when the text is no path the filesystem can take."""
    try:
        return jail.real(path)
    except (OSError, ValueError):
        return None


class HubPlaces(object):
    """The hub's folders for one guard call: `secret` the token folder and `stores` the store
    folders (kitlib.guard.hub.stores), each as written and as jail.real gives it."""

    def __init__(self, ctx):
        self.secret_path = hubg.secret_folder(ctx.config_location().new)
        self.secret = hub_real(self.secret_path)
        self.store_paths = hubg.stores(hub_config(ctx), ctx.pm_data_path())
        self.stores = [s for s in (hub_real(p) for p in self.store_paths) if s]

    def where(self, path):
        """'secret', 'store' or None for the absolute `path` (real()ed here)."""
        here_ = hub_real(path)
        if here_ is None:
            return None
        if self.secret and jail.inside(here_, self.secret):
            return 'secret'
        if any(jail.inside(here_, s) for s in self.stores):
            return 'store'
        return None

    def glob_where(self, pattern):
        """where() for an absolute wildcard path: the folder it can name."""
        if any(f and hubg.glob_reaches(pattern, f) for f in (self.secret_path, self.secret)):
            return 'secret'
        if any(hubg.glob_reaches(pattern, f) for f in self.store_paths + self.stores):
            return 'store'
        return None

    def label(self, where):
        return 'the hub token folder %s' % quote(self.secret_path) if where == 'secret' else 'the hub store'


def hub_session(event, ctx):
    return 'a subagent' if subagent(event) else 'marked %s' % ctx.session_role(event)


def hub_read_alternative(ctx):
    return 'python %s questions' % kit_tool(ctx, 'hub')


def hub_ask_alternative(ctx):
    return 'python %s questions add --title "<what the owner should decide>"' % kit_tool(ctx, 'hub')


def hub_secrets_refusal(event, ctx, role, what):
    return refuse('hub-secrets', role, '%s: %s This session is %s. To read the hub through its tool:' %
                  (what, HUB_SECRETS_WHY, hub_session(event, ctx)), hub_read_alternative(ctx))


def hub_secret_edit(event, ctx, target, role, what):
    """hub-secrets for an edit: a file in the hub's token folder or store (HubPlaces), in any
    session but the owner's own."""
    if not target or is_owner_session(event, ctx):
        return None
    places = HubPlaces(ctx)
    where = places.where(target)
    if where is None:
        return None
    return hub_secrets_refusal(event, ctx, role, '%s is in %s' % (what, places.label(where)))


def hub_secret_read(event, ctx, cwd, role):
    """hub-secrets for a read tool (Action kind 'read'), in any session but the owner's own: a
    read of the hub's token folder or a file in it, and a search that can read or list one. The
    store stays readable. A search starting in the folder is refused; any other is refused unless
    its terms cannot reach it: a names search whose wildcard can list neither the folder nor a
    file in it (hub.list_reaches), a contents search from a parent with a file filter that can
    take in none of its files (hub.filter_reaches). A contents search with no filter is refused at
    every parent: any of its output modes, counts and file lists too, would answer for a token's
    text (docs/guard.md "Hub secrets")."""
    if is_owner_session(event, ctx):
        return None
    secret_path = hubg.secret_folder(ctx.config_location().new)
    secret = hub_real(secret_path)
    if not secret:
        return None
    params = event.params if isinstance(event.params, dict) else None
    target = event.path if isinstance(event.path, str) else None
    tool = event.tool or 'a read'
    label = 'the hub token folder %s' % quote(secret_path)
    if params is None:
        if not target:
            return None
        full = hub_real(sh.resolve_path(target, cwd, home=ctx.home) or target)
        if full and jail.inside(full, secret):
            return hub_secrets_refusal(event, ctx, role, '%s of %s is in %s' % (tool, fwd(target), label))
        return None
    start = sh.resolve_path(target or cwd, cwd, home=ctx.home) or target or cwd
    root = hub_real(start)
    if root is None:
        return None
    what = '%s from %s' % (tool, fwd(start))
    if jail.inside(root, secret):
        return hub_secrets_refusal(event, ctx, role, '%s is in %s' % (what, label))
    if params.get('search') == 'names':
        reach = hub_list_reach(params.get('wildcard'), (root, start), (secret, secret_path),
                               hub_secret_names(secret))
        if reach:
            return hub_secrets_refusal(event, ctx, role, '%s can list %s (%s)' % (what, label, reach))
        return None
    if not jail.inside(secret, root):
        return None
    flt = params.get('filter')
    rel = [p for p in ntpath.relpath(secret, root).split('\\') if p]
    names = hub_secret_names(secret) if flt is not None else None
    if flt is None or names is None or hubg.filter_reaches(flt, rel, names):
        how = 'with no file filter' if flt is None else 'with a file filter (%s) that can take in a file in it' % flt
        return hub_secrets_refusal(event, ctx, role, '%s, a parent of %s, %s' % (what, label, how))
    return None


def hub_list_reach(wildcard, roots, folders, names):
    """The wildcard path, one of `roots` joined to `wildcard` (or `wildcard` itself when absolute),
    that can list one of `folders` or a file in it (hub.list_reaches over `names`), each {a,b}
    form apart; or None. No wildcard lists everything; braces expand_braces cannot write out, and
    names None (too many to read), count as reaching."""
    text = wildcard if isinstance(wildcard, str) and wildcard.strip() else '**'
    forms = hubg.expand_braces(text)
    if forms is None or names is None:
        return text
    for form in forms:
        for root in roots:
            full = form if ntpath.isabs(form) or ntpath.splitdrive(form)[0] else ntpath.join(root, form)
            if any(f and hubg.list_reaches(full, f, names) for f in folders):
                return fwd(full)
    return None


HUB_NAMES_LIMIT = 2000


def hub_secret_names(secret):
    """Every name a search could meet in the token folder, its subfolders' too, with
    hub.SECRET_FILES; None past HUB_NAMES_LIMIT (the caller then counts the folder as reached)."""
    found = list(hubg.SECRET_FILES)
    for _top, dirs, files in os.walk(secret):
        found.extend(dirs)
        found.extend(files)
        if len(found) > HUB_NAMES_LIMIT:
            return None
    return found


def hub_script_run(inv):
    """True when `inv` runs tools/hub.py: as python's script, or as the program."""
    if inv is None:
        return False
    if inv.name == HUB_SCRIPT and not inv.dyn:
        return True
    found = cm.python_script(inv) if UI_PYTHON_RE.match(inv.name or '') else None
    return bool(found) and found[1] == HUB_SCRIPT and not inv.args[found[0]].dyn


def hub_unexempt(text):
    """`text` with the guard's own module, kitlib.guard.hub, read as another name (HUB_EXEMPT_RES)."""
    for rx in HUB_EXEMPT_RES:
        text = rx.sub('kgh', text)
    return text


def hub_text_names(text, loader=False, whole=True, bare=()):
    """The text of `text` that names the hub tool as a module or a script (HUB_NAME_RES; with
    `loader`, also hub quoted as a name in text that loads a module by name), else None.
    `whole` and `bare` are hub_module_names' own; `bare` is dropped when hub_unexempt changes
    the text, since its spans would no longer fit (fail-closed)."""
    unexempt = hub_unexempt(text)
    bare = bare if unexempt == text else ()
    text = unexempt
    for rx in HUB_NAME_RES:
        m = rx.search(text)
        if m:
            return m.group(0).strip()
    found = hub_module_names(text, whole, bare)
    if found:
        return found
    if loader and HUB_LOADER_RE.search(text):
        m = HUB_QUOTED_RE.search(text)
        if m:
            return m.group(0)
    return None


def hub_module_names(text, whole=True, bare=()):
    """The text of `text` from a python word to a -m (a cluster ending in m, --module, runpy)
    naming hub on the same command (HUB_PY_WORD_RE, HUB_PY_MODULE_RE), else None. With `whole`
    False (a line the parser could not read whole) the -m may be past a ; | & or newline
    (HUB_PY_MODULE_ANY_RE). Every python
    word is tried, so one inside another's arguments (`$(python -m hub)`) is read too. The -m is
    python's own unless the python word is bare (hub_py_bare) and python's words before the -m
    reach a script word first (hub_py_script_first): `python <kit>/tools/commit.py -m "Hub
    store: x"` gives commit.py its message. A python word with a path, a quote or any other text
    before or glued to it (`<dir>/python.exe`, `"<dir>/python.exe"`, `"python"`,
    `.../Python312/python.exe`, `python/x`, `python;x`) keeps the -m python's, whatever follows
    (owner rulings Q-1010, Q-1012: agents use bare python; fail-closed). Glued text is cmd's to
    read: cmd ends a command's name at an unquoted /, ; , or = and passes the whole word as
    argv[0] (`cmd /c python/commit.py -m hub` and `cmd /c python;x -m hub` run python's -m), and
    a ; there is read as cmd reads it, so in bash too (fail-closed). This reads text, not words:
    a ; | & or newline the shell keeps inside a word (`-X 'a;b'`) ends `mid` here, so every
    command is also read by its parsed words (hub_word_module_names). A python word whose
    (start, end) is in `bare` counts as bare too: one that starts the text a nested shell runs
    and is bare in that text (hub_nested_bare, owner ruling Q-1009)."""
    module_re = HUB_PY_MODULE_RE if whole else HUB_PY_MODULE_ANY_RE
    for w in HUB_PY_WORD_RE.finditer(text):
        glued = HUB_PY_GLUED_RE.match(text, w.end()).end()
        m = module_re.match(text, w.end())
        if not m and ';' in text[w.end():glued]:
            m = module_re.match(text, text.rindex(';', w.end(), glued) + 1)
        is_bare = w.span() in bare or hub_py_bare(text, w.start(), w.end())
        if m and not (is_bare and hub_py_script_first(text[w.end():m.end('mid')])):
            return text[w.start():m.end()].strip()
    return None


def hub_py_bare(text, start, end):
    """True when the python word at `start`..`end` of `text` is a word by itself: before it the
    text's start, a space, a tab, a newline or ; & |, or a ( opening a group or a substitution
    (one or more, after one of those or a $: `$(python`, `(python`; not `<dir>(python`), so no
    path, quote, backtick or other text; and nothing glued after it up to a space, a tab or the
    command's end (HUB_PY_GLUED_RE)."""
    i = start
    while i > 0 and text[i - 1] == '(':
        i -= 1
    before = ' \t\n;&|$' if i < start else ' \t\n;&|'
    return (i == 0 or text[i - 1] in before) and HUB_PY_GLUED_RE.match(text, end).end() == end


def hub_word_module_names(cmd):
    """The words of `cmd`, as the shell passes them (Word.text, and Word.alt where bash's own
    differs), from a word holding a python word (HUB_PY_WORD_RE) to a later word that is python's
    -m naming hub (HUB_PY_WORD_MODULE_GLUED_RE; or HUB_PY_WORD_MODULE_NEXT_RE and the next word
    HUB_PY_WORD_HUB_RE), joined, else None. A ; | & or newline inside a word is part of that
    word here, never a command's end (`python -X 'a;b' -m hub`). The -m is python's own unless
    the word is a python word and nothing else (no path: `<dir>/python.exe` keeps it python's,
    owner ruling Q-1010) and python's words before it reach a record tool's script word first
    (hub_py_script_words). A
    PowerShell word holding a double quote may reach a native program as other words: Windows
    PowerShell 5.1 passes the quote on unescaped, wrapping the word in quotes or not, and the
    program's C runtime splits it (hub_crt_words). Both splits are read as well as the word
    itself (`python -X 'a;" -m hub "b'` and `python '-B"m"' hub` run python's -m)."""
    variants = [[[t] for t in (w.text for w in cmd.words)]]
    alts = [[w.alt if w.alt is not None else w.text] for w in cmd.words]
    if alts != variants[0]:
        variants.append(alts)
    if cmd.shell == sh.PS and any('"' in w.text for w in cmd.words):
        for wrap in (False, True):
            variants.append([hub_crt_words('"%s"' % w.text if wrap else w.text) if '"' in w.text
                             else [w.text] for w in cmd.words])
    for texts in variants:
        toks = [(k, p) for k, parts in enumerate(texts) for p in parts]
        dyn = {i for i, (k, _) in enumerate(toks) if cmd.words[k].dyn}
        for i, (k, t) in enumerate(toks):
            if not HUB_PY_WORD_RE.search(t):
                continue
            bare = HUB_PY_WORD_RE.fullmatch(t) is not None and i not in dyn
            args = [p for _, p in toks[i + 1:]]
            bad = {j - i - 1 for j in dyn if j > i}
            for j, a in enumerate(args):
                hit = HUB_PY_WORD_MODULE_GLUED_RE.match(a) or (
                    HUB_PY_WORD_MODULE_NEXT_RE.search(a) and j + 1 < len(args) and
                    HUB_PY_WORD_HUB_RE.match(args[j + 1]))
                if hit and not (bare and hub_py_script_words(args[:j], bad)):
                    return ' '.join([t] + args[:j + 2])
    return None


def hub_crt_words(s):
    """The words a Windows program's C runtime (the UCRT, which python uses) reads from the
    command-line text `s`: space and tab split words outside quotes; a double quote opens or
    closes quoting, and two inside quoting give one literal quote; 2n backslashes before a quote
    give n and the quote acts, 2n+1 give n and a literal quote; other backslashes are kept."""
    out, cur, have, quoted, i = [], [], False, False, 0
    while i < len(s):
        c = s[i]
        if c == '\\':
            n = len(s) - i - len(s[i:].lstrip('\\'))
            i += n
            if i < len(s) and s[i] == '"':
                cur.append('\\' * (n // 2))
                if n % 2:
                    cur.append('"')
                    i += 1
            else:
                cur.append('\\' * n)
            have = True
        elif c == '"':
            if quoted and s[i + 1:i + 2] == '"':
                cur.append('"')
                i += 2
            else:
                quoted = not quoted
                i += 1
            have = True
        elif c in ' \t' and not quoted:
            if have:
                out.append(''.join(cur))
            cur, have = [], False
            i += 1
        else:
            cur.append(c)
            have = True
            i += 1
    if have:
        out.append(''.join(cur))
    return out


def hub_py_script_first(mid):
    """True when python's words `mid` (between the python word and a -m naming hub) reach a
    record tool's script word (HUB_PY_MESSAGE_SCRIPTS, a plain word: no variable, no wildcard)
    before any option that takes code or a module (a cluster holding c or m) or `--`. A -W or -X
    at a cluster's end takes the next word; --check-hash-based-pycs takes one. Every word before
    the script word, option values included, must be plain (HUB_PY_OPT_WORD_RE): the split here is
    on space and tab, as bash splits (str.split would also split on a no-break space or a control
    bash keeps inside a word), and an escaped space, a quote, a backtick, a variable or an
    expansion lets the shell hand python other words (`-X a\\ commit.py -m hub` gives -X the value
    `a commit.py`). Anything else is python's -m: fail-closed."""
    return hub_py_script_words([t for t in re.split(r'[ \t]+', mid) if t])


def hub_py_script_words(toks, bad=()):
    """hub_py_script_first on python's words `toks` as already split; a word whose index is in
    `bad` (one the shell may change or split) is never plain, so reaching it first is python's
    -m (fail-closed)."""
    toks = [None if i in bad else t for i, t in enumerate(toks)]
    i = 0
    while i < len(toks):
        if toks[i] is None:
            return False
        t = toks[i]
        if t == '--':
            return False
        if t.startswith('--'):
            i += 2 if t == '--check-hash-based-pycs' else 1
            continue
        if t.startswith('-') and len(t) > 1:
            skip = 1
            for k, ch in enumerate(t[1:], 1):
                if ch in 'cmCM':
                    return False
                if ch in 'WX':
                    if k == len(t) - 1:
                        skip = 2
                    break
            i += skip
            continue
        m = HUB_PY_PLAIN_WORD_RE.match(t)
        return (bool(m) and ntpath.basename(m.group(2)).lower() in HUB_PY_MESSAGE_SCRIPTS and
                all(o is not None and HUB_PY_OPT_WORD_RE.match(o) for o in toks[:i]))
    return False


def hub_glob_names(cmd):
    """A bash wildcard word of a python command (python among its words) that can expand to
    hub.py (`python tools/h*.py`, `tools/hu?.py`), else None. A quoted word is no wildcard."""
    if cmd.shell != sh.BASH or not any(UI_PYTHON_RE.match(cm.base_name(w.text)) for w in cmd.words if not w.dyn):
        return None
    for w in cmd.words:
        if cm.quoted(cmd, w) or not hubg.has_glob(w.text):
            continue
        base = re.split(r'[\\/]', w.text)[-1].lower()
        if any(c in base for c in 'hub') and fnmatch.fnmatchcase(HUB_SCRIPT, base):
            return w.text
    return None


def hub_word_globs(cmd, w):
    """True when the word `w` holds a wildcard (*, ? or [) its shell may expand to a file's name:
    one outside quotes and not escaped (bash's backslash, PowerShell's backtick; bash's $'...'
    takes backslash escapes too). A word with no span in the command's source is read by its text."""
    src = cmd.source
    if not (isinstance(w.start, int) and isinstance(w.end, int) and 0 <= w.start <= w.end <= len(src)):
        return hubg.has_glob(w.text)
    s, esc = src[w.start:w.end], ('`' if cmd.shell == sh.PS else '\\')
    i, quote, ansi, dollar = 0, None, False, -1
    while i < len(s):
        c = s[i]
        if quote == "'":
            if ansi and c == '\\':
                i += 2
                continue
            if c == "'":
                quote = None
        elif quote == '"':
            if c == esc:
                i += 2
                continue
            if c == '"':
                quote = None
        elif c == esc:
            i += 2
            continue
        elif c in '\'"':
            quote, ansi = c, c == "'" and cmd.shell == sh.BASH and dollar == i - 1
        elif c in hubg.GLOB_CHARS:
            return True
        elif c == '$':
            dollar = i
        i += 1
    return False


def hub_literal(cmd, inv):
    """True when every word of `cmd` from its program on is literal (no variable, substitution,
    decoded escape, brace expansion, text bash passes otherwise, PowerShell double quote inside a
    word, or wildcard the shell may expand: hub_word_globs), the program is the command's first
    word (PowerShell: or after `&`), and its redirects only write or append to literal files that
    do not name the hub tool."""
    ps = cmd.shell == sh.PS
    if not (inv.index == 0 or (ps and inv.index == 1 and cmd.words[0].text == '&')):
        return False
    for w in cmd.words[inv.index:]:
        if w.dyn or w.synth or (w.alt is not None and w.alt != w.text) or (ps and '"' in w.text):
            return False
        if hub_word_globs(cmd, w):
            return False
    for r in cmd.redirects:
        if r.body or r.op not in ('>', '>>'):
            return False
        t = r.target
        if t is not None and (t.dyn or t.synth or hub_word_globs(cmd, t) or hub_text_names(t.text)):
            return False
    return True


def hub_shape(parsed, cmd, inv):
    """True when `cmd` is the one form of a hub tool run the guard reads in full (HUB_SHAPE): a
    clean line, a command of the line itself (no substitution, nested shell or pipe into it),
    python run directly with every word literal (hub_literal: no wildcard either), only
    HUB_SHAPE_OPT_RE options, then tools/hub.py (its base name) or `-m tools.hub`, then arguments
    that run an agent verb (kitlib.guard.hub.owner_verb None). Its redirects write or append to
    literal files."""
    if not parsed.clean or inv is None or cmd.depth or cmd.source != parsed.text or cmd.subs:
        return False
    if cmd.nested_in is not None or cmd.pipe_from is not None or cmd.piped_in:
        return False
    if inv.via is not None or inv.dyn or not UI_PYTHON_RE.match(inv.name or ''):
        return False
    if not hub_literal(cmd, inv):
        return False
    args = [w.text for w in inv.args]
    k = 0
    while k < len(args) and HUB_SHAPE_OPT_RE.match(args[k]):
        k += 1
    if k + 1 < len(args) and args[k] == '-m' and args[k + 1] == HUB_MODULE:
        k += 2
    elif k < len(args) and not args[k].startswith('-') and cm.base_name(args[k]) == HUB_SCRIPT:
        k += 1
    else:
        return False
    return hubg.owner_verb([hubg.Word(t) for t in args[k:]]) is None


def hub_path_word(text):
    """True when the word `text` is a path to hub.py: no option, and hub.py its last part
    (tools/hub.py, tools\\hub.py, HEAD:tools/hub.py)."""
    return not text.startswith('-') and ntpath.basename(text.replace('/', '\\')).lower() == HUB_SCRIPT


def hub_opt_denied(text, longs, shorts=''):
    """True when the option word `text` is one of `longs` (as written, with =value, or as a unique
    prefix of one: --ext for --ext-diff), or a cluster of short options holding one of `shorts`."""
    if text.startswith('--'):
        name = text.split('=', 1)[0]
        return len(name) > 2 and any(d.startswith(name) for d in longs)
    return bool(shorts) and text.startswith('-') and len(text) > 1 and any(c in shorts for c in text[1:])


def hub_git_words(sub, rest):
    """git's words `rest` after the subcommand `sub` with a commit's message values left out
    (HUB_GIT_MESSAGE_RE, --message, --message=; none after `--`): they are text, not paths or
    options. An -m with no word after it is kept, to be read as an option."""
    if sub != 'commit':
        return list(rest)
    out, k = [], 0
    while k < len(rest):
        t = rest[k]
        if t == '--':
            return out + list(rest[k:])
        m = HUB_GIT_MESSAGE_RE.match(t)
        if t == '--message' or (m and not m.group(1)):
            if k + 1 >= len(rest):
                return out + [t]
            if m:
                out.append(t[:-1])
            k += 2
            continue
        if t.startswith('--message='):
            k += 1
            continue
        if m:
            out.append(t[:t.index('m')])
            k += 1
            continue
        out.append(t)
        k += 1
    return out


def hub_git_family(args, outlet=False):
    """True when git's arguments `args` (the words after git) read, stage or commit files the way
    the path family takes them: global options only -C <path>, --no-pager and -P; a HUB_GIT_SUBS
    subcommand (restore only with --staged or -S); none of HUB_GIT_DENIED or the subcommand's own
    HUB_GIT_SUB_DENIED; the hub tool named only by path words or in a commit's message
    (hub_git_words). With `outlet`, no --output or -o either (a file a later command could run)."""
    k = 0
    while k < len(args) and args[k].startswith('-'):
        if args[k] == '-C' and k + 1 < len(args) and not args[k + 1].startswith('-'):
            k += 2
        elif args[k] in ('--no-pager', '-P'):
            k += 1
        else:
            return False
    if k >= len(args) or args[k] not in HUB_GIT_SUBS:
        return False
    sub, rest = args[k], hub_git_words(args[k], args[k + 1:])
    if sub == 'restore' and not any(t in ('--staged', '-S') for t in rest):
        return False
    longs, shorts = HUB_GIT_SUB_DENIED.get(sub, ((), ''))
    for t in rest:
        if t.startswith('-') and len(t) > 1 and (hub_opt_denied(t, HUB_GIT_DENIED) or hub_opt_denied(t, longs, shorts)
                                                 or outlet and hub_opt_denied(t, ('--output',), 'o')):
            return False
        if hub_text_names(t) and not hub_path_word(t):
            return False
    return True


def hub_commit_family(args):
    """True when tools/commit.py's arguments `args` (argparse, no abbreviations) name the hub tool
    only in its message (-m, -b) or as a path to commit; every option is one commit.py takes."""
    k = 0
    while k < len(args):
        t = args[k]
        if t == '--':
            return all(hub_path_word(p) or not hub_text_names(p) for p in args[k + 1:])
        if t.startswith('-') and len(t) > 1:
            name, value = (t.split('=', 1) + [None])[:2] if t.startswith('--') else (t[:2], t[2:] or None)
            if name in HUB_COMMIT_FLAGS and value is None:
                k += 1
                continue
            if name not in HUB_COMMIT_VALUES:
                return False
            if value is None:
                if k + 1 >= len(args) or args[k + 1].startswith('-'):
                    return False
                value, k = args[k + 1], k + 1
            if name not in HUB_COMMIT_MESSAGES and hub_text_names(value):
                return False
        elif hub_text_names(t) and not hub_path_word(t):
            return False
        k += 1
    return True


def hub_backlog_family(args, messages=HUB_BACKLOG_MESSAGES):
    """True when tools/backlog.py's arguments `args` name the hub tool only in the value of a
    message option (`messages`, written in full: argparse's own abbreviations are not read as
    one) or as a path word (hub_path_word: a word of its own, or an option's =value, as --scope,
    --body-file and show take them; backlog.py records and reads paths, it runs none). The same
    reading takes tools/ledger.py with HUB_LEDGER_MESSAGES: it records text and runs nothing given."""
    k = 0
    while k < len(args):
        t = args[k]
        name, value = (t.split('=', 1) + [None])[:2] if t.startswith('--') else (t, None)
        if name in messages:
            if value is None and k + 1 < len(args) and not args[k + 1].startswith('-'):
                k += 1
            k += 1
            continue
        if hub_text_names(t) and not hub_path_word(t if value is None else value):
            return False
        k += 1
    return True


def hub_family_cmd(parsed, cmd, outlet=False):
    """True when `cmd` of the line `parsed` is a path family command (HUB_READERS, git, commit.py,
    backlog.py or ledger.py; docs/guard.md "Hub secrets"): a simple command of the line itself
    (no pipe, substitution or nested shell), its program its first word, every word literal with
    no wildcard (hub_literal), and the hub tool named only where that program's reading allows
    (hub_path_word, a message, no option that runs a program; with `outlet`, git writes no
    --output file: hub_git_family)."""
    if cmd.depth or cmd.source != parsed.text or cmd.subs or cmd.nested_in is not None:
        return False
    if cmd.pipe_from is not None or cmd.piped_in or cmd.piped_out or cmd.start is None:
        return False
    inv = cm.invocation(cmd)
    if inv is None or inv.via is not None or inv.dyn or not hub_literal(cmd, inv):
        return False
    args = [w.text for w in inv.args]
    if inv.name in HUB_READERS:
        denied = HUB_READER_DENIED.get(inv.name, ())
        return not any(t.startswith('-') and hub_opt_denied(t, denied) for t in args) and \
            all(hub_path_word(t) or not hub_text_names(t) for t in args)
    if inv.name == 'git':
        return hub_git_family(args, outlet)
    if not UI_PYTHON_RE.match(inv.name or '') or not hub_family_py_bare(parsed, cmd, inv):
        return False
    k = 0
    while k < len(args) and HUB_SHAPE_OPT_RE.match(args[k]):
        k += 1
    script = cm.base_name(args[k]) if k < len(args) and not args[k].startswith('-') else None
    if script is not None and (hub_text_names(args[k]) or HUB_BARE_RE.search(args[k])):
        return False
    if script == 'commit.py':
        return hub_commit_family(args[k + 1:])
    if script == 'backlog.py':
        return hub_backlog_family(args[k + 1:])
    if script == 'ledger.py':
        return hub_backlog_family(args[k + 1:], HUB_LEDGER_MESSAGES)
    return False


def hub_family_py_bare(parsed, cmd, inv):
    """True when the program word of `cmd` (inv.index; after PowerShell's `&`) is a bare python
    word as the line spells it and as the shell passes it (HUB_PY_WORD_RE, whole): no path, quote,
    escape or other text, so `<dir>/python.exe`, `"<dir>/python.exe"` and `"python"` before
    commit.py are no family command (owner rulings Q-1010, Q-1012). A word with no place in the
    line's text is not bare (fail-closed)."""
    if inv.index is None or not 0 <= inv.index < len(cmd.words):
        return False
    w = cmd.words[inv.index]
    o, e = w.start, w.end
    if not (isinstance(o, int) and isinstance(e, int) and 0 <= o < e <= len(parsed.text)):
        return False
    return (HUB_PY_WORD_RE.fullmatch(parsed.text[o:e]) is not None and
            HUB_PY_WORD_RE.fullmatch(w.text) is not None)


def hub_path_family(parsed):
    """True when the line `parsed` is the path family's one command (hub_family_cmd): a clean line
    of a single simple command with no text beside it but stream dups (HUB_DUPS_RE: no chaining,
    heredoc or comment)."""
    if not parsed.clean or len(parsed.commands) != 1:
        return False
    cmd = parsed.commands[0]
    if cmd.start is None or parsed.text[:cmd.start].strip() or not HUB_DUPS_RE.match(parsed.text[cmd.end:]):
        return False
    return hub_family_cmd(parsed, cmd)


def hub_line_plain(parsed):
    """True when the line `parsed` is plain (hub_family): clean; its commands joined only by
    HUB_LINE_OPS, with no group, substitution, nested shell or pipe; no redirect but a stream dup;
    no environment variable set (commands.line_env_names); every word literal (on PowerShell, no
    `$` or splat `@` in one either); every command a program run; none of HUB_LINE_DENIED as a
    first word or a program, HUB_LINE_DRIVES in a word, PowerShell's variable-storing common
    parameters (HUB_LINE_PS_VARS, HUB_LINE_PS_ALIASES), or HUB_LINE_TEXT_RE in the text."""
    if not parsed.clean or len(parsed.commands) < 2 or HUB_LINE_TEXT_RE.search(parsed.text):
        return False
    if cm.line_env_names(parsed.commands):
        return False
    ps = parsed.shell == sh.PS
    for c in parsed.commands:
        if c.prev_op not in HUB_LINE_OPS or c.next_op not in HUB_LINE_OPS or c.groups or c.subs:
            return False
        if c.depth or c.nested_in is not None or c.pipe_from is not None or c.piped_in or c.piped_out:
            return False
        if c.start is None or c.source != parsed.text or not c.words:
            return False
        if any(not r.dup for r in c.redirects):
            return False
        inv = cm.invocation(c)
        if inv is None or (inv.name or '').lower() in HUB_LINE_DENIED or c.words[0].text.lower() in HUB_LINE_DENIED:
            return False
        for w in c.words:
            t = w.text.lower()
            if w.dyn or w.synth or (ps and (t.startswith('@') or '$' in t)):
                return False
            if any(d in t for d in HUB_LINE_DRIVES):
                return False
            if ps and t[:1] in HUB_LINE_PS_DASHES:
                name = '-' + t[1:].split(':', 1)[0]
                if name in HUB_LINE_PS_ALIASES or ((len(name) > 2 or name == '-p')
                                                   and any(v.startswith(name) for v in HUB_LINE_PS_VARS)):
                    return False
    return True


def hub_family(parsed):
    """The commands of a plain line (hub_line_plain) that are path family commands
    (hub_family_cmd, git with no --output file), else []: on such a line no command's output,
    name or state reaches another through the shell, so a family command there names the hub tool
    no more than it does alone, and the line's other commands are read as before (hub_named).
    State git keeps (a commit's message, the index) may still reach a later command that reads
    and runs it; that is a script the agent wrote (hub_named's limit), not modelled."""
    if not hub_line_plain(parsed):
        return []
    return [c for c in parsed.commands if hub_family_cmd(parsed, c, outlet=True)]


def hub_code_names(parsed, text, shaped):
    """`hub` standing alone (HUB_BARE_RE) anywhere on a line that runs python (a word
    HUB_CODE_PYTHON_RE names: python, py, ipython, pypy) and holds `import` (HUB_IMPORT_RE), else
    None. Line-wide, so no reading of where python takes its code from
    (-c, -m code, -m timeit -s, a pipe of any length, a heredoc, a here-string) is needed: the
    texts are the line's own `text` (allowed-shape commands blanked) and, for every other command
    at any depth, its source, its words as the shell passes them (bash's $'...' and PowerShell's
    backtick escapes decoded, Word.alt too) and its redirects' targets and bodies. `shaped` are
    the allowed-shape and path family commands, which run no python code given on the line. The guard's own
    kitlib.guard.hub is read as another name first. A heredoc that cat writes to a file
    (hub_written_bodies) is data, as a file the Write tool writes is: its body is not read here
    (hub_text_names still reads it for an import of hub or a path to hub.py)."""
    rest = [c for c in parsed.commands if not any(c is s for s in shaped)]
    if not any(not w.dyn and HUB_CODE_PYTHON_RE.match(cm.base_name(w.text)) for c in rest for w in c.words):
        return None
    written = hub_written_bodies(parsed)
    for r in written:
        t = r.target
        text = text[:t.start] + ' ' * (t.end - t.start) + text[t.end:]
    texts = [text]
    for c in rest:
        texts.append(c.text or '')
        texts.extend(w.text for w in c.words)
        texts.extend(w.alt for w in c.words if w.alt is not None)
        texts.extend(r.target.text for r in c.redirects
                     if r.target is not None and not any(r is b for b in written))
    texts = [hub_unexempt(t) for t in texts]
    if not any(HUB_IMPORT_RE.search(t) for t in texts):
        return None
    for t in texts:
        m = HUB_BARE_RE.search(t)
        if m:
            return m.group(0)
    return None


def hub_written_bodies(parsed):
    """The heredoc redirects of the line `parsed` whose body cat only writes to a file: a bash
    command of the line's own text (no substitution, group or nested shell) whose program is cat
    itself, every word literal, in no pipe, its output sent with > or >> to a literal file that is
    no device by any spelling (hub_device_target: /./dev/fd/3, /proc/..., CON), and the body
    quoted (<<'EOF') or holding no $( or backtick bash would run. Anything else may reach python:
    its body stays read, as on a line that can make cat another program or hand its file to one
    (HUB_CAT_REDEFINED_RE: a function named cat, hash, alias, exec, eval, source, ., coproc; a
    PATH set)."""
    out = []
    if HUB_CAT_REDEFINED_RE.search(parsed.text):
        return out
    for c in parsed.commands:
        bodies = [r for r in c.redirects if r.body and r.target is not None]
        if not bodies or c.shell != sh.BASH or c.depth != 0 or c.source != parsed.text:
            continue
        if (c.groups or c.subs or c.nested_in is not None or c.pipe_from is not None or c.piped_in
                or c.piped_out or c.path_env):
            continue
        inv = cm.invocation(c)
        if inv is None or inv.via is not None or (inv.name or '').lower() != 'cat' or c.words[0].text != 'cat':
            continue
        if any(w.dyn or w.synth for w in c.words):
            continue
        others = [r for r in c.redirects if not r.body]
        if not any(r.writes and r.fd in ('1', '&') for r in others):
            continue
        if not all(r.writes and r.target is not None and not r.target.dyn and not r.target.synth
                   and not hub_device_target(r.target.text) for r in others):
            continue
        if any(r.target.dyn and ('$(' in r.target.text or '`' in r.target.text) for r in bodies):
            continue
        out.extend(bodies)
    return out


def hub_device_target(text):
    """True when a redirect target `text` is a device or a process's file, not a file on disk, by
    any spelling: any folder of the path named dev or proc (/./dev/fd/3, //dev/stdout, dev/stdout,
    ../dev/stdout) counts, so a folder of that name on disk does too (fail-closed)."""
    t = text.replace('\\', '/').lower()
    base = t.rstrip('/').rsplit('/', 1)[-1].split('.', 1)[0].rstrip(':')
    parts = posixpath.normpath(t).split('/')[:-1]
    return (t.startswith(('/dev/', '/proc/', '//./', '//?/')) or 'dev' in parts or 'proc' in parts or
            base in ('con', 'prn', 'aux', 'nul', 'conin$', 'conout$') or re.fullmatch(r'(com|lpt)\d', base) is not None)


def hub_command_texts(cmd):
    """The texts of `cmd` as the shell passes them: its words (and bash's own text where it
    differs: Word.alt) and its redirects' targets, heredoc and here-string bodies included."""
    words = [w.text for w in cmd.words]
    alts = [w.alt if w.alt is not None else w.text for w in cmd.words]
    targets = [r.target.text for r in cmd.redirects if r.target is not None]
    out = [' '.join(words + targets)]
    if alts != words:
        out.append(' '.join(alts + targets))
    return out


def hub_nested_bare(parsed):
    """The (start, end) spans, in the line's text, of each python word that starts the text a
    nested bash/sh -c '...' or cmd /c "..." runs and is bare in that text, judged by that text's
    own spelling (hub_py_bare on Command.source; owner ruling Q-1009): in the line's text the
    quote that holds the nested text sits before it, and that quote is the outer shell's, not
    part of the word. Only a nested text that one quoted word of a command on the line itself
    holds verbatim (no escape inside) is placed; any other has no span here, so its python is
    judged by the line's text (fail-closed)."""
    out = set()
    for c in parsed.commands:
        outer = c.nested_in
        if outer is None or c.source == parsed.text or outer.source != parsed.text or not c.words:
            continue
        inv = cm.invocation(outer)
        if inv is None or (inv.name not in cm.BASH_SHELLS and inv.name != 'cmd'):
            continue
        hits = [x for x in outer.words if not x.dyn and x.text == c.source]
        if len(hits) != 1:
            continue
        h, src, w = hits[0], c.source, c.words[0]
        raw = parsed.text[h.start:h.end]
        if len(raw) != len(src) + 2 or raw[0] not in '\'"' or raw[-1] != raw[0] or raw[1:-1] != src:
            continue
        if not 0 <= w.start < w.end <= len(src) or src[:w.start].strip('( \t\n'):
            continue
        if (HUB_PY_WORD_RE.fullmatch(src[w.start:w.end]) is not None and
                HUB_PY_WORD_RE.fullmatch(w.text) is not None and hub_py_bare(src, w.start, w.end)):
            out.add((h.start + 1 + w.start, h.start + 1 + w.end))
    return out


def hub_named(parsed):
    """What the line `parsed` names of the hub tool outside the allowed shape, as text, or None
    (docs/guard.md "Hub secrets"). The line's own text is read with every command of the
    allowed shape (hub_shape) blanked out (a python word starting a nested shell's text judged by
    that text: hub_nested_bare), and every other command, at any depth (a substitution,
    a nested shell, a heredoc's body), by its words as the shell passes them (hub_command_texts:
    quotes and escapes inside a name are gone), and any standalone hub on a line that runs python
    and holds `import` (hub_code_names). A line of the path family (hub_path_family) names
    nothing, and on a plain line the path family's commands (hub_family) are read like the
    allowed shape's: blanked from the line's text and not read by their words. A name
    the code builds at run time (string concatenation, an encoding, a variable's value, a script
    the agent wrote) is not read: the rule guards against accidents, not an adversary."""
    if hub_path_family(parsed):
        return None
    text = parsed.text
    shaped = []
    family = hub_family(parsed)
    for cmd in parsed.commands:
        fam = any(cmd is f for f in family)
        if fam or hub_shape(parsed, cmd, cm.invocation(cmd)):
            if not fam:
                shaped.append(cmd)
            if cmd.start is not None:
                text = text[:cmd.start] + ' ' * (cmd.end - cmd.start) + text[cmd.end:]
    found = hub_text_names(text, whole=parsed.clean, bare=hub_nested_bare(parsed))
    if found:
        return found
    for cmd in parsed.commands:
        if any(cmd is s for s in shaped + family):
            continue
        for t in hub_command_texts(cmd):
            found = hub_text_names(t, loader=True)
            if found:
                return found
        found = hub_word_module_names(cmd)
        if found:
            return found
        found = hub_glob_names(cmd)
        if found:
            return found
    return hub_code_names(parsed, text, shaped + family)


def hub_name(text):
    """The hub secret a word names by itself, or None: the token folder (`.pm-kit/hub`) or a
    token file's name as its last part."""
    if hubg.FOLDER_RE.search(text):
        return '.pm-kit/hub'
    base = re.split(r'[\\/]', text)[-1].strip().lower()
    return base if base in hubg.SECRET_FILES else None


def hub_path_texts(text):
    """The texts of one word that may be paths: the word, and the value of `--opt=value` or
    PowerShell's `-Opt:value`; an option alone is none."""
    if not text.startswith('-'):
        return [text]
    for sep in ('=', ':'):
        if sep in text:
            return [text.split(sep, 1)[1]]
    return []


# hub_not_paths: the options of a search whose parse the guard knows. Only options that take no
# value are listed: one wrongly listed here would move the pattern onto a file the search opens.
# Any other option, a word the shell may change (a variable, a wildcard) or an option that names
# the pattern itself (-e, -f, /c:, /g:) and the search's words are all read as paths.
HUB_GREP_FLAGS = frozenset('abcEFGHhiIlLnoPqrRsTUuvwxyzZ0123456789')
HUB_GREP_LONG = frozenset((
    'ignore-case', 'no-ignore-case', 'line-number', 'recursive', 'dereference-recursive', 'count',
    'files-with-matches', 'files-without-match', 'only-matching', 'quiet', 'silent', 'no-messages',
    'invert-match', 'word-regexp', 'line-regexp', 'with-filename', 'no-filename', 'extended-regexp',
    'fixed-strings', 'basic-regexp', 'perl-regexp', 'byte-offset', 'text', 'null', 'null-data',
    'initial-tab', 'color', 'colour'))
HUB_RG_FLAGS = frozenset('abcFHiIlLnNopPqsSuUvwxz0')
HUB_RG_LONG = frozenset((
    'ignore-case', 'smart-case', 'case-sensitive', 'fixed-strings', 'word-regexp', 'line-regexp',
    'invert-match', 'count', 'count-matches', 'files-with-matches', 'files-without-match',
    'line-number', 'no-line-number', 'hidden', 'no-ignore', 'follow', 'only-matching', 'multiline',
    'pcre2', 'vimgrep', 'heading', 'no-heading', 'with-filename', 'no-filename', 'text', 'null',
    'quiet', 'column', 'no-messages', 'byte-offset', 'search-zip', 'unrestricted', 'trim', 'stats'))
# Select-String's parameters (PowerShell 5.1 and 7): the switches, and the ones that take a value
HUB_SLS_SWITCHES = ('-simplematch', '-casesensitive', '-quiet', '-list', '-notmatch', '-allmatches',
                    '-raw', '-noemphasis')
HUB_SLS_VALUES = ('-pattern', '-path', '-literalpath', '-inputobject', '-include', '-exclude',
                  '-encoding', '-context', '-culture')
# a bare name: no separator, drive, home, variable or wildcard; not `.` or `..`, not an option
HUB_BARE_WORD_RE = re.compile(r'^(?!\.\.?$)[\w.][\w.-]*$')
# the long options of a python script whose bare value names, never opens: `--session` gives a
# session name (tools/launch.py, whose sessions include one named hub). A script's other options
# may name a folder it opens, so only an option with a stated reason is listed here.
HUB_PY_NAME_OPTS = frozenset(('--session',))


def _hub_grep_pattern(args, short, long_):
    """The pattern Word of grep or rg (`short`, `long_` the options that take no value), or None:
    the first operand, when every option on the line is one of those (or a `--name=value` that
    names no pattern) and no word is a variable."""
    pattern = None
    for k, w in enumerate(args):
        t = w.text
        if w.dyn:
            return None
        if t == '--':
            if pattern is None and k + 1 < len(args) and not args[k + 1].dyn:
                pattern = args[k + 1]
            break
        if t.startswith('--'):
            name, eq, _ = t[2:].partition('=')
            if not name or any(n.startswith(name) for n in ('regexp', 'file')):
                return None
            if eq or name in long_:
                continue
            return None
        if t.startswith('-') and t != '-':
            if all(c in short for c in t[1:]):
                continue
            return None
        if pattern is None:
            pattern = w
    return pattern


def _hub_findstr_pattern(args):
    """findstr's pattern Word: its first operand, unless /c: or /g: gives the pattern."""
    pattern = None
    for w in args:
        t = w.text.lower()
        if w.dyn or t.startswith('-') or t.startswith(('/c:', '/g:')):
            return None
        if t.startswith('/'):
            continue
        if pattern is None:
            pattern = w
    return pattern


def _hub_sls_patterns(args):
    """Select-String's pattern Words: -Pattern's value (`-Pattern x`, `-Pattern:x`), else its
    first positional word when every parameter before it is known."""
    names = HUB_SLS_SWITCHES + HUB_SLS_VALUES
    named, positional, k = [], None, 0
    while k < len(args):
        w = args[k]
        t = sh.ps_dashed(w.text)
        if w.dyn:
            return []
        if t.startswith('-') and len(t) > 1:
            p = cm.canon_param(t, names)
            if p is None:
                return []
            if p in HUB_SLS_VALUES and ':' not in t:
                if p == '-pattern' and k + 1 < len(args):
                    named.append(args[k + 1])
                k += 2
                continue
            if p == '-pattern':
                named.append(w)
            k += 1
            continue
        if positional is None:
            positional = w
        k += 1
    if named:
        return named
    return [positional] if positional is not None else []


def _hub_python_values(inv):
    """The bare values of a python script's HUB_PY_NAME_OPTS: `--session value` and
    `--session=value`. None when a flag before the script may run a module or code (`-Bm`, `-Ic`:
    commands.python_script reads such a cluster as a plain flag)."""
    found = cm.python_script(inv) if UI_PYTHON_RE.match(inv.name or '') else None
    if not found or inv.args[found[0]].dyn or inv.args[found[0]].text == '-':
        return []
    args, out = inv.args, []
    for w in args[:found[0]]:
        if w.dyn or (w.text.startswith('-') and re.search('[mc]', w.text.lstrip('-'))):
            return []
    for j in range(found[0] + 1, len(args)):
        w = args[j]
        t = w.text
        if t == '--':
            break
        if w.dyn:
            continue
        if t.startswith('--'):
            name, eq, value = t.partition('=')
            if eq and name in HUB_PY_NAME_OPTS and HUB_BARE_WORD_RE.match(value):
                out.append(w)
            continue
        prev = args[j - 1]
        if (j - 1 > found[0] and not prev.dyn and prev.text in HUB_PY_NAME_OPTS
                and HUB_BARE_WORD_RE.match(t)):
            out.append(w)
    return out


def hub_not_paths(inv):
    """The ids of the Words of `inv` that hub-secrets does not read as paths into the store (the
    token folder still counts): a search's pattern (grep, egrep, fgrep, rg, findstr,
    Select-String) when the guard knows every option on the line, and the bare value of a python
    script's `--session` (HUB_PY_NAME_OPTS). A pattern with a wildcard the shell may expand stays
    a path. Nothing for a program the guard cannot name, or one run through Start-Process or
    xargs."""
    if inv is None or inv.dyn or inv.via:
        return set()
    name, args = inv.name, inv.args
    if name in ('grep', 'egrep', 'fgrep'):
        words = [_hub_grep_pattern(args, HUB_GREP_FLAGS, HUB_GREP_LONG)]
    elif name == 'rg':
        words = [_hub_grep_pattern(args, HUB_RG_FLAGS, HUB_RG_LONG)]
    elif name == 'findstr':
        words = [_hub_findstr_pattern(args)]
    elif name in ('select-string', 'sls'):
        words = _hub_sls_patterns(args)
    else:
        return {id(w) for w in _hub_python_values(inv)}
    return {id(w) for w in words if w is not None and not hubg.has_glob(w.text)}


def inline_code(cmd, inv, tool_run=None):
    """The code python or node runs from its own command line (-c, -e) or from standard input
    (a heredoc, a here-string, the command piped in), as texts; [] for a script run or another
    program. The command line's texts are every argument (a path the code is given counts).
    With code on the command line, the command piped in is left out when `tool_run(it)` is true:
    the hub tool run in the one shape the guard reads in full (hub_shape), any verb it allows.
    What the code does with that input is not read: code that execs it passes as a script file
    would. Its heredocs and here-strings still count, and standard input run as code (`-` or no
    script) always counts the command piped in."""
    if inv is None:
        return []
    py, node = UI_PYTHON_RE.match(inv.name or '') is not None, inv.name == 'node'
    if not (py or node):
        return []
    args = [w.text for w in inv.args]
    code, stdin, k = False, True, 0
    while k < len(args):
        t = args[k]
        if py and t in cm.PY_VALUE_OPTS:
            k += 2
            continue
        if (py and re.match(r'^-[A-Za-z]*c', t)) or (node and t.split('=', 1)[0] in NODE_EVAL):
            code = True
            break
        if py and t == '-m':
            stdin = False
            break
        if t.startswith('-') and t != '-':
            k += 1
            continue
        stdin = t == '-'                        # a script file, or `-`: standard input
        break
    texts = list(args) if code else []
    if stdin or code:
        for c in (cmd, cmd.pipe_from):
            if c is None:
                continue
            if c is not cmd and not (code and tool_run is not None and tool_run(c)):
                texts.append(c.text)
            texts.extend(r.target.text for r in c.redirects
                         if r.target is not None and (r.body or r.op == '<<<'))
    return texts


def hub_secret_shell(ctx, cmd, inv, places, parsed=None):
    """What the command `cmd` names of the hub's secrets, as text, or None: a word naming the
    token folder or a token file (hub_name; a search's or git's literal words are its patterns
    and messages, so only their words that are not literal count), a literal path in the token
    folder, in the store (unless tools/hub.py runs: its own --store), or wildcards that can name
    either, and python or node code that names them or the hub site (kitlib.guard.hub.INLINE_RE).
    A path may start with the home folder as `~` or a home variable ($HOME, $env:USERPROFILE,
    %USERPROFILE%, $env:HOMEDRIVE$env:HOMEPATH: shell.home_text, read as ctx.home), and a
    relative one is read from the directory a cd to the home folder, or a bare cd, moved to
    (the command's cwd_home).
    `places()` gives the HubPlaces; `parsed`, the whole line, lets python -c parse what the hub
    tool piped into it prints (inline_code, hub_shape)."""
    name_only = inv is not None and inv.name in hubg.NAME_ONLY_PROGRAMS
    targets = [r.target for r in cmd.redirects if r.target is not None and not r.body and r.op != '<<<']
    for w in cmd.words:
        if w.dyn or not name_only:
            hit = hub_name(w.text)
            if hit:
                return 'names %s' % hit
    for w in targets:
        hit = hub_name(w.text)
        if hit:
            return 'names %s' % hit
    runs_hub = hub_script_run(inv)
    not_paths = hub_not_paths(inv)
    for w in list(cmd.words) + targets:
        for text in hub_path_texts(w.text):
            text = sh.home_text(text, ctx.home, w.dyn)
            if text is None:
                continue
            try:
                p = sh.resolve_path(text, cmd.cwd_home, cmd.shell, ctx.home)
            except (OSError, ValueError):
                p = None
            if not p:
                continue
            p_ = places()
            where = p_.glob_where(p) if hubg.has_glob(text) else p_.where(p)
            if where == 'store' and id(w) in not_paths:
                continue                        # exempt from the store, never the token folder
            if where == 'secret' or (where == 'store' and not runs_hub):
                return 'reaches %s (%s)' % (p_.label(where), w.text)
    tool_run = None if parsed is None else (lambda c: hub_shape(parsed, c, cm.invocation(c)))
    for text in inline_code(cmd, inv, tool_run):
        m = hubg.INLINE_RE.search(text)
        if m:
            return 'runs code that names %s' % m.group(0)
    return None


def hub_line(text):
    """The line `text` for a refusal: stripped, and cut to its first 200 characters."""
    text = text.strip()
    return text if len(text) <= 200 else text[:200] + '...'


class HubShell(object):
    """The hub rules of one shell call, as rules with the shell rules' arguments; the owner's
    own session is read once and the hub's folders only when a word needs them."""

    def __init__(self, event, ctx, parsed):
        self.owner = is_owner_session(event, ctx)
        self.ctx = ctx
        self.parsed = parsed
        self._places = None
        self._named = False

    def places(self):
        if self._places is None:
            self._places = HubPlaces(self.ctx)
        return self._places

    def owner_verb(self, event, ctx, cmd, inv, cwd, role):
        """hub-owner-verb, read once for the whole line (hub_named): the hub tool named outside
        the one shape the guard reads in full, or that shape running an owner verb."""
        if self.owner or self._named is None:
            return None
        found, self._named = hub_named(self.parsed), None
        if not found:
            return None
        return refuse('hub-owner-verb', role, '`%s` names the hub tool (%s) outside the one form the guard reads in '
                      'full (%s), or runs an owner verb in it: %s This session is %s. Ask the owner through a '
                      'question:' % (hub_line(self.parsed.text), found, HUB_SHAPE, HUB_OWNER_WHY,
                                     hub_session(event, ctx)), hub_ask_alternative(ctx))

    def secrets(self, event, ctx, cmd, inv, cwd, role):
        """hub-secrets: a shell command that names the hub's secrets (hub_secret_shell)."""
        if self.owner:
            return None
        found = hub_secret_shell(ctx, cmd, inv, self.places, self.parsed)
        if not found:
            return None
        return hub_secrets_refusal(event, ctx, role, '`%s` %s' % (bare(cmd), found))


def hub_mcp(event, ctx, tool, role):
    """hub-mcp: a hub MCP owner tool (kitlib.guard.hub.owner_mcp); hub-browser: a browser or
    computer-use MCP call whose input names the hub's own origin (a loopback host on a hub
    port). In any session but the owner's own."""
    if event.kind != 'mcp' or not tool or is_owner_session(event, ctx):
        return None
    server, tail = hubg.split_mcp(tool, host_mcp_separator(ctx.host_profile()) or '__')
    who = hub_session(event, ctx)
    if hubg.owner_mcp(tail, server):
        return refuse('hub-mcp', role, '%s acts as the owner on the Action Hub: %s This session is %s; the agent '
                      'tools (asking, listing) stay open. Ask the owner through a question:' %
                      (tail, HUB_OWNER_WHY, who), hub_ask_alternative(ctx))
    if not hubg.browser_server(server):
        return None
    found = hubg.hub_origins(hubg.strings(getattr(event, 'params', None)), hubg.ports(hub_config(ctx)))
    if not found:
        return None
    return refuse('hub-browser', role, '%s reaches the Action Hub\'s own page (%s), where the owner answers as the '
                  'owner: %s This session is %s. Ask the owner through a question:' %
                  (tool, found[0], HUB_OWNER_WHY, who), hub_ask_alternative(ctx))


# ---------------------------------------------------------------- edits: ledger and main checkout

def same_file(a, b):
    return paths.norm(a, absolute=True) == paths.norm(b, absolute=True)


def ledger_rule(ctx, target, role, what):
    user = ctx.user()
    broken = user.get('broken')
    if broken and role == 'subagent':
        return refuse('ledger', role, '%s: the machine config %s is broken (%s), so the '
                      'guard cannot tell where the rulings ledger is and refuses every subagent write. The owner '
                      'or the PM session fixes it.' % (what, ctx.config_location().display(), broken),
                      'python -m json.tool %s' % quote(ctx.config_location().path))
    ledger = user.get('ledger_path')
    if not ledger or not target or not same_file(target, ledger):
        return None
    return refuse('ledger', role, '%s writes the rulings ledger, which only ledger.py writes. '
                  "Add the entry with the kit's tools/ledger.py add (docs/ledger.md)." % what,
                  'python %s add --repo <repo> --prov <prov> --q "<question>" --a "<answer>" '
                  '--decides "<what it decides>" --source "<source>"' % kit_tool(ctx, 'ledger'))


def lock_state_dir(ctx):
    """The machine lock's state folder: <home>/.claude/machine-lock (kitlib.lock.store)."""
    return os.path.join(ctx.home, '.claude', 'machine-lock')


def lock_state(event, ctx, target, role, what):
    """An agent session's edit of a file in the machine lock's state folder: the lock tool alone
    writes it, under its own mutex."""
    if not target or not agent_session(event, ctx):
        return None
    # resolved paths (jail.real: links, junctions and 8.3 names followed), so a write that reaches the
    # state folder through a junction is still this rule's, whatever root the jail would open
    top, here_ = jail.real(lock_state_dir(ctx)), jail.real(target)
    if not jail.inside(here_, top):
        return None
    return refuse('lock-state', role, '%s edits the machine lock\'s state, which only the lock tool writes, under its own '
                  'mutex. Ask the owner (through the PM) when the state looks wrong. To see it:' % what,
                  'python %s status' % lock_tool(ctx))


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
                  % (what, why), 'python -m json.tool %s' % quote(ctx.config_location().path))


def own_worktree(ctx, cwd, main_top=None):
    """The top of the linked worktree the call runs in (of `main_top`, when given), or None."""
    here_ = ctx.place(cwd) if cwd else None
    if here_ is None or here_.kind != repo.LINKED or not here_.top:
        return None
    if main_top is not None:
        mine = ctx.main_top(cwd)
        if not mine or paths.norm(mine) != paths.norm(main_top):
            return None
    return here_.top


def edit_target(event, ctx, target, cwd, role, what, unknown_why=None, rewrite=None, run=None):
    """Deny an edit of `target` in a main checkout (or a place git cannot tell) by a role that
    may not edit one, and an agent session's write outside its roots (write_jail). `rewrite(path)`
    gives the same edit aimed at `path` (the alternative); `run` the command to rerun from the
    worktree when it writes into the directory it runs in."""
    d = ledger_rule(ctx, target, role, what) or pm_data_unknown(ctx, target, cwd, role, what) or \
        lock_state(event, ctx, target, role, what) or hub_secret_edit(event, ctx, target, role, what) or \
        kit_json(event, ctx, target, role, what) or \
        generated_file(ctx, target, role, what)
    if d:
        return d
    return main_checkout(event, ctx, target, cwd, role, what, unknown_why, rewrite, run) or \
        write_jail(event, ctx, target, cwd, role, what, rewrite, run)


def main_checkout(event, ctx, target, cwd, role, what, unknown_why=None, rewrite=None, run=None):
    # a target it cannot place may be in any checkout, so both the kit defaults and the roles of
    # the repository the call runs in must let the role edit one: the defaults alone would let a
    # role that repository keeps out of its main checkout write anywhere, and the repository's
    # alone a role it lets into its own main checkout (a lander) write into any other
    may = kconfig.permissions(event, kconfig.roles(ctx.config(target) if target else None)).get('edit_main_checkout')
    if may and target is None and cwd:
        may = kconfig.permissions(event, kconfig.roles(ctx.config(cwd))).get('edit_main_checkout')
    if may:
        return None
    main_top = ctx.main_top(cwd) if cwd else None
    new_wt = new_worktree(ctx, main_top)[1]
    if target is None:
        mine = own_worktree(ctx, cwd)
        instead = rewrite(fwd(mine) + '/<file>') if mine and rewrite is not None else new_wt
        return refuse('main-checkout', role, '%s: the guard cannot tell where it writes (%s). Name a literal path '
                      'inside your worktree (the Edit or Write tool for a file).' %
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


def generated_file(ctx, target, role, what):
    """An edit of a file the kit's renderer writes, in a kit checkout (kitlib.guard.generated):
    the next render overwrites it and the drift check fails it, so the source is edited and the
    file rendered. The renderer runs as a process, which no edit rule judges."""
    if not target:
        return None
    top = ctx.place(target).top
    if not top:
        return None
    try:
        rel = os.path.relpath(target, top)
    except ValueError:                          # another drive
        return None
    src = generated.source(top, rel)
    if src is None:
        return None
    return refuse('generated-file', role, '%s: %s is generated from %s by tools/render_content.py; a hand edit '
                  'is overwritten by the next render and fails the drift check. Edit %s, then render:'
                  % (what, rel.replace('\\', '/'), src, src),
                  'python %s --write' % quote(os.path.join(top, 'tools', 'render_content.py')))


def session_mark(event, ctx):
    """The launcher mark of a main session without the kit plugin's prefix (guard_mark_roots looks
    it up), or None for a subagent and the owner's unmarked session."""
    if subagent(event):
        return None
    who = agentname.bare_mark(ctx.session_role(event))
    return None if who in (context.OWNER, context.SUBAGENT) else who


def jail_roots(event, ctx, cwd):
    return jail.roots(ctx, cwd, lambda p: is_root(p, None, ctx), session_mark(event, ctx))


JAIL_ASK = 'Ask the coordinator (a coordinator asks the PM) when the work needs a folder outside them.'


def write_jail(event, ctx, target, cwd, role, what, rewrite=None, run=None):
    """An agent session's write outside its roots (kitlib.guard.jail): its checkout, its
    repository's other worktrees (jail.sibling), temp and the .kit.json data roots, judged by the
    resolved path."""
    if not target or not agent_session(event, ctx):
        return None
    rs = jail_roots(event, ctx, cwd)
    verdict, root = jail.judge(target, rs)
    if verdict is None or (verdict == jail.OUTSIDE and jail.sibling(ctx, target, cwd)):
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
    if rewrite is None and run is not None and cwd and paths.norm(target) == paths.norm(cwd):
        py_c = PY_C_HINT if PY_C_RUN_RE.search(run) else ''
        return (py_c + MAIN_CWD_HINT, instead)   # it writes through the main checkout it runs in
    return 'Make the change in a worktree of it (%s there); the lander brings it to main:' % fwd(rel), instead


MAIN_CWD_HINT = ('It writes through its working directory, which is the main checkout: cd into your worktree first, '
                 'in the same call (no tool keeps a cd between calls), or run a script or module by absolute path '
                 '(python <worktree>/tools/x.py -C <worktree>; python -m unittest discover -s <worktree>/tests/<pkg> '
                 '-t <worktree>). No worktree yet:')
# a run that is a python -c (python, python3, py, python.exe; -c alone or in a cluster such as -Ic):
# its refusal names the isolated form that may run in a main checkout when the code only reads
PY_C_RUN_RE = re.compile(r'(?i)(?:^|[\s&("\'\\/])py(?:thon[\d.]*)?(?:\.exe)?["\']?(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*c'
                         r'(?=[\s"\']|$)')
PY_C_HINT = ('A python -c whose code only reads may run in a main checkout as python -I -c (isolated; docs/guard.md '
             '"Rules" lists what the code may use). ')


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
              'epal': 'export-alias', 'epsn': 'export-pssession',
              'gci': 'get-childitem', 'ls': 'get-childitem', 'dir': 'get-childitem', 'sp': 'set-itemproperty'}
BASH_WRITERS = {'tee', 'touch', 'mkdir', 'rm', 'rmdir', 'cp', 'mv', 'truncate', 'install', 'ln',
                'unlink', 'shred'}
CMD_WRITERS = {'copy', 'xcopy', 'robocopy', 'move', 'del', 'erase', 'md', 'mkdir', 'rd', 'rmdir',
               'ren', 'rename', 'type'}
COPIERS = {'robocopy', 'xcopy'}                 # Windows programs every shell runs
EVALS = {'node': ('-e', '--eval', '-p', '--print'), 'ruby': ('-e',)}
# a .NET file call that writes (OpenRead and OpenText only read)
IO_FILE_RE = re.compile(r'(?i)\[(?:System\.)?IO\.(?:File|Directory)\]::'
                        r'(?:Write|Append|Create|Copy|Move|Delete|Replace|Open(?!Read\b|Text\b))')
# a .NET type whose constructor opens a file to write: [IO.StreamWriter]::new(...), New-Object IO.FileStream ...
NET_TYPE_RE = re.compile(r'(?i)(?<![\w.])(?:System\.)?IO\.(?:StreamWriter|FileStream)(?![\w.])')
NET_ABS_RE = re.compile(r'^[A-Za-z]:[\\/]')     # a path .NET reads the same from any directory
# \\?\<drive>:\x and \\.\<drive>:\x are <drive>:\x (a device path); any other \\ or // path is a share the guard cannot place
NET_DEVICE_RE = re.compile(r'^[\\/]{2}[?.][\\/](?=[A-Za-z]:[\\/])')
WIN_OPT_RE = re.compile(r'^/[A-Za-z]+(?::\S*)?$')


PS_NAMED_DYN_RE = re.compile(r'^-[A-Za-z_]\w*:')    # -Name: and a computed value, in one word


def ps_param_word(cmd, w):
    """The text of `w` with its dash as '-' when PowerShell reads it as a parameter name (-Path,
    –Path, -Path:$p), else None. `-Destination:$p`: a literal name and a colon bind the
    computed rest to that parameter. A quoted word or a negative number is never a parameter."""
    t = sh.ps_dashed(w.text)
    if t.startswith('-') and len(t) > 1 and (not w.dyn or PS_NAMED_DYN_RE.match(t)) and \
            not cm.quoted(cmd, w) and not re.match(r'^-\d', t):
        return t
    return None


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
        t = ps_param_word(cmd, w)
        if t is not None:
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
            if colon and val == '' and k < len(words):
                vals = [words[k]]               # `-Path: x`: a colon then a space binds the next word
                k += 1
            elif colon:
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


# an Export-* cmdlet's parameters that never name the file it writes (ps_export_targets)
PS_EXPORT_NO_PATH = ('inputobject', 'depth', 'encoding') + PS_COMMON_VALUES


def ps_export_targets(cmd, words):
    """[Word]: every argument of an Export-* cmdlet with no spec in PS_CMDLETS that could name the
    file it writes. Its parameters are unknown, so a positional, the next word after any parameter
    and a colon value are each a target; only the value of a parameter named exactly in
    PS_EXPORT_NO_PATH is not (`Export-Alias -As Script $p` judges Script and $p)."""
    out, k = [], 0
    while k < len(words):
        w = words[k]
        t = ps_param_word(cmd, w)
        k += 1
        if t is None:
            out.extend(_split_commas(w))
            continue
        body, colon, val = t[1:].partition(':')
        body = body.lower()
        no_path = PS_COMMON_ALIASES.get(body, body) in PS_EXPORT_NO_PATH
        if colon and val:
            if not no_path:
                out.append(sh.Word(val, w.dyn, w.start, w.end))
        elif no_path and k < len(words) and ps_param_word(cmd, words[k]) is None:
            k += 1                              # its value
    return out


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


class NetWrite(object):
    """One path a .NET file call writes (net_writes): `word` its argument, dyn when the line
    computes it (a variable, an expression, a string with $ or a backtick), with its span in the
    source; `end` the offset just past the call's closing parenthesis."""
    __slots__ = ('word', 'end')

    def __init__(self, word, end):
        self.word, self.end = word, end


NET_QUOTES = '‘’‚‛“”„'     # PowerShell reads these as quotes too
NET_DASHES = ''.join(sh.PS_DASHES)  # and these as the dash an operator starts with
NET_UNARY = frozenset(('not', 'bnot', 'join', 'split', 'csplit', 'isplit'))   # -op operand


def _net_string_end(src, k):
    """The offset just past the PowerShell string that starts at src[k] (' or ")."""
    q, n = src[k], len(src)
    k += 1
    while k < n:
        c = src[k]
        if q == '"' and c == '`':
            k += 2
            continue
        if c == q:
            if src[k + 1:k + 2] == q:
                k += 2
                continue
            return k + 1
        k += 1
    return n


def _net_literal(raw):
    """The text of the argument `raw` when it is a plain string ('...', or "..." with no $ or
    backtick), else None."""
    if any(c in NET_QUOTES for c in raw):
        return None
    m = re.match(r"^'((?:[^']|'')*)'$", raw, re.S)
    if m:
        return m.group(1).replace("''", "'")
    m = re.match(r'^"((?:[^"$`]|"")*)"$', raw, re.S)
    return m.group(1).replace('""', '"') if m else None


def _net_args(src, k):
    """([(text or None, start, end)], end): the arguments of the call whose '(' is just before
    offset `k` of `src` (text None: not a plain string, _net_literal), and the offset just past
    its closing parenthesis (len(src) when it has none)."""
    args, n = [], len(src)
    while True:
        while k < n and src[k] in ' \t\r\n':
            k += 1
        start, depth = k, 0
        while k < n:
            c = src[k]
            if c in '\'"':
                k = _net_string_end(src, k)
                continue
            if c == '`':
                k += 2
                continue
            if c in '([{':
                depth += 1
            elif c in ')]}':
                if depth == 0:
                    break
                depth -= 1
            elif c == ',' and depth == 0:
                break
            k += 1
        raw = src[start:min(k, n)].rstrip()
        if raw:
            args.append((_net_literal(raw), start, start + len(raw)))
        if k >= n:
            return args, n
        if src[k] != ',':
            return args, k + 1
        k += 1


def _net_written(method, count):
    """(indexes, all found): the indexes of the arguments a .NET File or Directory method writes -
    Copy its destination, Move both, Replace all three, any other its first - among the `count`
    arguments read, and False when one of them is past the last (the guard did not read the call
    as .NET will)."""
    if method.startswith('copy'):
        idx = (1,)
    elif method.startswith('move'):
        idx = (0, 1)
    elif method.startswith('replace'):
        idx = (0, 1, 2)
    else:
        idx = (0,)
    return [i for i in idx if i < count], all(i < count for i in idx)


def _net_lost(span):
    """True when _net_args may have lost its place in the argument text `span`, so the literal it
    read as an argument may be part of another: a smart quote (PowerShell closes one with a plain
    quote), a here-string (@" or @'), a comment (#), a braced variable (${: PowerShell reads its
    name up to the }, quotes included), or a subexpression ($() where a double quote also appears
    (a " inside it does not end the outer string)."""
    if any(c in NET_QUOTES for c in span) or '@"' in span or "@'" in span or '#' in span or '${' in span:
        return True
    return '"' in span and '$(' in span


def net_mentioned(cmd):
    """True when the PowerShell command names a .NET file call or type that writes (IO_FILE_RE,
    NET_TYPE_RE): net_writes reads it."""
    return cmd.shell == sh.PS and bool(IO_FILE_RE.search(cmd.text) or NET_TYPE_RE.search(cmd.text))


def _net_word(raw):
    """The text of a command-mode argument `raw`: a plain string (_net_literal) or a bare word,
    else None."""
    text = _net_literal(raw)
    if text is None and re.match(r'^[^\s\'"`$(){}\[\]@#;,|&<>%s]+$' % NET_QUOTES, raw):
        return raw
    return text


def _net_list(src, k):
    """([(text or None, start, end)], end): the command-mode argument at offset `k` of `src`, a
    word or a comma list of them ('a', 'b'), read as _net_args reads a call's; a bare word ends at
    a '(' (PowerShell starts a new argument there)."""
    args, n = [], len(src)
    while True:
        start, depth = k, 0
        while k < n:
            c = src[k]
            if c in '\'"':
                k = _net_string_end(src, k)
                continue
            if c == '`':
                k += 2
                continue
            if c in '([{':
                if c == '(' and depth == 0 and k > start and src[k - 1] != '$':
                    break
                depth += 1
            elif c in ')]}':
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and c in ', \t\r\n;|&':
                break
            k += 1
        k = min(k, n)
        if k > start:
            args.append((_net_word(src[start:k]), start, k))
        j = k
        while j < n and src[j] in ' \t':
            j += 1
        if j >= n or src[j] != ',':
            return args, k
        k = j + 1
        while k < n and src[k] in ' \t\r\n':
            k += 1


NEW_OBJECT_PARAMS = ('typename', 'argumentlist', 'property', 'comobject', 'strict')


def _net_new_object(cmd, inv):
    """(type (text or None, start, end), [argument], end, lost) of the New-Object call `inv`, or
    None when the guard does not read it: its -TypeName and -ArgumentList, named or positional,
    each a word, a comma list, (...) or @(...)."""
    src, n = cmd.source, len(cmd.source)
    k, vals, slots, lost = inv.word.end, {}, ['typename', 'argumentlist'], ''
    while True:
        while k < n and src[k] in ' \t':
            k += 1
        if k >= n or src[k] in ';|&\r\n)}':
            break
        m = re.compile(r'-([A-Za-z]+)(:?)').match(src, k)
        if m and src[k + 1:k + 2].isalpha():
            body = m.group(1).lower()
            names = ['argumentlist'] if body == 'args' else [p for p in NEW_OBJECT_PARAMS if p.startswith(body)]
            if len(names) != 1:                 # a common parameter (-ErrorAction Stop, -Verbose)
                body = PS_COMMON_ALIASES.get(body, body)
                if not any(p.startswith(body) for p in PS_COMMON_VALUES + PS_COMMON_SWITCHES):
                    return None
                names = ['skip' if any(p.startswith(body) for p in PS_COMMON_VALUES) else 'strict']
            k = m.end()
            if names[0] == 'strict':
                continue
            while not m.group(2) and k < n and src[k] in ' \t':
                k += 1
            param = names[0]
            if param in slots:
                slots.remove(param)
        elif slots:
            param = slots.pop(0)
        else:
            return None
        stops = ' \t;|&\r\n)}'
        if src.startswith('@(', k) or src.startswith('(', k):
            open_ = k + (2 if src[k] == '@' else 1)
            args, k = _net_args(src, open_)
            lost += src[open_:k]
        else:
            args, k = _net_list(src, k)
            lost += ''.join(src[s:e] for _t, s, e in args)
            stops += '('                        # IO.StreamWriter('x'): the list starts a new argument
        if k < n and src[k] not in stops:
            return None                         # ('a') + 'b': an expression the guard does not read
        vals[param] = args
    kind = vals.get('typename') or []
    if len(kind) != 1:
        return None
    return kind[0], vals.get('argumentlist', []), k, _net_lost(lost)


FILE_MODE_RE = re.compile(r'(?i)^(?:\[(?:System\.)?IO\.FileMode\]::)?(\w+)$')
FILE_ACCESS_RE = re.compile(r'(?i)^(?:\[(?:System\.)?IO\.FileAccess\]::)?(\w+)$')


def _net_arg_name(src, arg, pattern):
    """The enum value name the argument names ('Open', [IO.FileMode]::Open), lower case, or ''."""
    text, s, e = arg
    m = pattern.match(text if text is not None else src[s:e].strip())
    return m.group(1).lower() if m else ''


def _net_ctor(src, kind, args, end, out):
    """Add the path a StreamWriter or FileStream constructor with `args` writes to `out`; True
    when the guard does not read it: no arguments, or a StreamWriter's first argument that is not a
    plain string (a Stream it writes through, judged where it is opened, or a path the line
    computes). A FileStream opened to read only (FileMode Open, FileAccess Read) writes nothing,
    unless it names FileOptions (a sixth argument: DeleteOnClose deletes the file)."""
    if not args:
        return True
    text, s, e = args[0]
    if kind == 'filestream' and 3 <= len(args) < 6 and _net_arg_name(src, args[1], FILE_MODE_RE) == 'open' and \
            _net_arg_name(src, args[2], FILE_ACCESS_RE) == 'read':
        return False
    if text is None and (kind == 'streamwriter' or src[s:e].lower() == '$null'):
        return kind == 'streamwriter'
    out.append(NetWrite(sh.Word(src[s:e] if text is None else text, text is None, s, e), end))
    return False


def _net_operand_end(src, k):
    """The offset just past the operand that starts at src[k]: its strings and brackets whole, up to
    a space, a comma, a statement end or an unopened closing bracket."""
    n, depth = len(src), 0
    while k < n:
        c = src[k]
        if c in '\'"':
            k = _net_string_end(src, k)
            continue
        if c == '`':
            k += 2
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in ' \t\r\n,;|&':
            break
        k += 1
    return min(k, n)


def _net_cast_operand(src, k):
    """The offset of what follows the ']' at src[k - 1], past the spaces, <# #> comments and backtick
    line continuations PowerShell reads as space there."""
    n = len(src)
    while k < n:
        if src[k] in ' \t':
            k += 1
        elif src.startswith('<#', k):
            close = src.find('#>', k + 2)
            k = n if close < 0 else close + 2
        elif src[k] == '`' and src[k + 1:k + 2] == '\n':
            k += 2
        elif src[k] == '`' and src[k + 1:k + 3] == '\r\n':
            k += 3
        else:
            break
    return k


def _net_cast(src, k):
    """The NetWrite of the StreamWriter cast whose type name ends at src[k] (its ']'), or None when
    no operand follows (the type passed as a value: [IO.StreamWriter], [IO.StreamWriter].Name,
    $x -is [IO.StreamWriter] -and $y, @([IO.StreamWriter], 1), List[IO.StreamWriter]). What follows
    the ']' is an operand unless it ends the statement or the expression (a line end, a line comment,
    . ) ] } ; | , ..) or is
    a binary operator (-and, -eq, -f); a unary one (-join, -split, -not, -bnot, -1, +, !) opens an
    operand, as a comment or a line continuation before it does not end one (_net_cast_operand).
    PowerShell converts the operand by the constructor that takes a string, so a plain string
    ('x.txt', or ('x.txt')) is the path; any other operand ($p, ('a' + $b), 'x'.Replace(...): a member
    access or an index binds before the cast) is a path the line computes."""
    n = len(src)
    if src[k:k + 1] != ']':
        return None
    k = _net_cast_operand(src, k + 1)
    if k >= n or src[k] in '\r\n#)]};|,':
        return None                             # ']': a type argument, List[IO.StreamWriter]
    if src[k] == '.' and not (src[k - 1] != ']' and src[k + 1:k + 2].isdigit()):
        return None                             # .Name, ..5; but [IO.StreamWriter] .5 is a cast of 0.5
    if src[k] in NET_DASHES:
        op = re.compile(r'[A-Za-z]*').match(src, k + 1).group(0).lower()
        if op and op not in NET_UNARY:
            return None                         # a binary operator, or one PowerShell rejects
    end = max(_net_operand_end(src, k), k + 1)
    lit = None
    if src[k] in '\'"':
        close = _net_string_end(src, k)
        lit = (_net_literal(src[k:close]), k, close) if close == end else None
    elif src[k] == '(':
        args, close = _net_args(src, k + 1)
        if close == end and src[close - 1:close] == ')' and len(args) == 1 and not _net_lost(src[k + 1:close]):
            lit = args[0]
    if lit is not None and lit[0] is not None:
        return NetWrite(sh.Word(lit[0], False, lit[1], lit[2]), end)
    return NetWrite(sh.Word(src[k:end], True, k, end), end)


def net_writes(cmd):
    """([NetWrite], unread): each path the PowerShell command's .NET file calls (IO_FILE_RE) and
    StreamWriter and FileStream constructors (NET_TYPE_RE: ::new(...), New-Object, or a StreamWriter
    cast, [IO.StreamWriter]'x.txt', whose operand is a path the line computes unless it is a plain
    string, _net_cast) write, and True
    when a match is a write the guard does not read the path of, so write_targets judges the
    directory the command runs in, as it did for every such call, and dotnet_process_dir the one
    its process started in: one inside a quoted word, a method named with no '(' after it (called
    later, through .Invoke or a variable), a call with fewer arguments than the ones it writes
    (_net_written), one whose arguments the parse may have lost its place in (_net_lost), a type
    named any other way (a FileStream cast, [type] as a value) and a New-Object call _net_new_object does
    not read. A $null argument (Replace's backup) writes nothing."""
    if cmd.shell != sh.PS:
        return [], False
    out, unread, src = [], False, cmd.source
    read = []                                   # the span of the type a New-Object call names
    inv = cm.invocation(cmd) if NET_TYPE_RE.search(cmd.text) else None
    if inv is not None and inv.name == 'new-object':
        got = _net_new_object(cmd, inv)
        kind = got and got[0][0] and re.match(r'(?i)^(?:System\.)?IO\.(StreamWriter|FileStream)$', got[0][0])
        if kind:
            read.append(got[0][1:])
            unread = _net_ctor(src, kind.group(1).lower(), got[1], got[2], out) or got[3]
    for m in NET_TYPE_RE.finditer(cmd.text):
        pos, after = cmd.start + m.start(), cmd.start + m.end()
        if any(s <= pos < e for s, e in read):
            continue
        w = next((x for x in cmd.words if x.start <= pos < x.end), None)
        call = re.compile(r'\]::(\w+)').match(src, after)
        if w is None or src[w.start:w.start + 1] in '\'"' + NET_QUOTES or src[pos - 1:pos] != '[':
            unread = True                       # in a string, a type name passed as a value
            continue
        if not call:
            # [IO.StreamWriter]'x.txt', [IO.StreamWriter]$p: a cast opens its operand (_net_cast)
            cast = _net_cast(src, after) if m.group(0).lower().endswith('streamwriter') else None
            if cast is None:
                unread = True                   # [type] passed as a value, a FileStream cast
            else:
                out.append(cast)
            continue
        if call.group(1).lower() != 'new':
            continue                            # a static member: [IO.StreamWriter]::Null
        k = call.end()
        while k < len(src) and src[k] in ' \t':
            k += 1
        if src[k:k + 1] != '(':
            unread = True
            continue
        args, end = _net_args(src, k + 1)
        kind = re.search(r'(?i)(StreamWriter|FileStream)$', m.group(0)).group(1).lower()
        unread = _net_ctor(src, kind, args, end, out) or _net_lost(src[k + 1:end]) or unread
    for m in IO_FILE_RE.finditer(cmd.text):
        pos = cmd.start + m.start()
        w = next((x for x in cmd.words if x.start <= pos < x.end), None)
        if w is None or src[w.start:w.start + 1] in '\'"' + NET_QUOTES:
            unread = True
            continue
        rest = re.compile(r'\w*').match(src, cmd.start + m.end())
        method = (m.group(0).rpartition('::')[2] + rest.group(0)).lower()
        k = rest.end()
        while k < len(src) and src[k] in ' \t':
            k += 1
        if src[k:k + 1] != '(':
            unread = True
            continue
        args, end = _net_args(src, k + 1)
        written, found = _net_written(method, len(args))
        unread = unread or not found or _net_lost(src[k + 1:end])
        for i in written:
            text, s, e = args[i]
            if text is None and src[s:e].lower() == '$null':
                continue
            out.append(NetWrite(sh.Word(src[s:e] if text is None else text, text is None, s, e), end))
    for x in out:
        m = None if x.word.dyn else NET_DEVICE_RE.match(x.word.text)
        if m:                                   # \\?\<drive>:\x is <drive>:\x; the span stays the argument's
            x.word = sh.Word(x.word.text[m.end():], False, x.word.start, x.word.end)
    return out, unread


def net_placed(w):
    """True when the .NET argument Word `w` is a literal absolute path on a drive (NET_ABS_RE;
    net_writes reads \\\\?\\<drive>:\\x as <drive>:\\x). A share (\\\\host\\<drive>$\\x) is not: the guard cannot tell
    which folder it is, so dotnet_unplaced judges it as a path it cannot place."""
    return not w.dyn and bool(NET_ABS_RE.match(w.text))


def net_retarget(cmd, x, path):
    """The command `cmd` through the end of its .NET call, with the NetWrite `x`'s argument
    replaced by `path`."""
    start = cmd.words[0].start if cmd.words else cmd.start
    return (cmd.source[start:x.word.start] + literal_word(path) + cmd.source[x.word.end:x.end]).strip()


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


# tar's short options that take no value in GNU tar and in bsdtar (Windows' tar.exe) alike, and
# those that take one besides -C; a letter in neither may take one. Long options known to take none.
TAR_FLAGS = set('xtcruvzjJZapkmOohPUwSBln')
TAR_VALUES = set('bfFgHIKLNTVX')
TAR_LONG_FLAGS = {'extract', 'get', 'list', 'create', 'append', 'update', 'verbose', 'gzip', 'gunzip', 'ungzip',
                  'bzip2', 'xz', 'lzma', 'zstd', 'lzip', 'compress', 'uncompress', 'auto-compress',
                  'keep-old-files', 'keep-newer-files', 'overwrite', 'preserve-permissions', 'same-permissions',
                  'no-same-owner', 'same-owner', 'no-same-permissions', 'unlink-first', 'absolute-names',
                  'dereference', 'totals', 'to-stdout', 'interactive', 'sparse', 'numeric-owner',
                  'no-recursion', 'touch', 'ignore-zeros', 'check-links', 'verify'}


class _TarDirs:
    """The folders a tar line may extract into: `bases` the folders in force (None is the working
    directory), `seen` every folder a -C named, `unsure` once a -C was read that may not be one."""

    def __init__(self):
        self.bases, self.seen, self.unsure = [None], [], False

    def move(self, folder, sure):
        """A -C to `folder`, relative to the one in force as tar reads it (-C /a -C b is /a/b);
        not `sure`: it may not be a -C, so the folders in force stay possible too."""
        new = []
        for b in self.bases:
            rooted = folder.text.startswith(('/', '\\', '~')) or ntpath.splitdrive(folder.text)[0]
            new.append(folder if b is None or rooted else
                       sh.Word(ntpath.join(b.text, folder.text), folder.dyn or b.dyn, folder.start, folder.end))
        self.bases = new if sure else self.bases + new
        self.seen += new
        self.unsure = self.unsure or not sure


def _tar_old(args, dirs):
    """(index of the first word after the values, the state after them) for the old-style first
    word (tar xCf <dir> a.tar): each letter that takes a value takes the next word in turn. A letter
    the guard does not know may take one too, so a C after one names any of the words it could."""
    k, unknown = 1, 0
    for c in args[0].text:
        if c == 'C':
            for j in range(k, min(k + unknown + 1, len(args))):
                dirs.move(args[j], not unknown)
            k += 1
        elif c in TAR_VALUES:
            k += 1
        elif c not in TAR_FLAGS:
            unknown += 1
    return k, ('maybe' if unknown else None)


def _tar_word(args, k, state, dirs):
    """The state after tar's word args[k] (None, 'value': the next word is a value, 'maybe': it may
    be one, 'folder': it is -C's), moving `dirs` for a -C. A -C is sure only in a word that is no
    value of the word before it and, in a cluster, after letters that take none (-xvC <dir>);
    otherwise (-xfC <dir>: a file C; -NC<dir>: a date; --exclude -C<dir>) both readings stay."""
    w, t = args[k], args[k].text
    if state == 'folder':
        dirs.move(w, True)
        return None
    doubt = state is not None
    nxt = args[k + 1] if k + 1 < len(args) else None
    if t.startswith('--'):
        name, eq, value = t.partition('=')
        # --di is also --diff; bsdtar (Windows' tar.exe) reads --cd as -C
        if name == '--cd' or len(name) >= 5 and '--directory'.startswith(name):
            if eq:
                dirs.move(tail_word(w, value), not doubt)
                return None
            if not doubt:
                return 'folder'
            if nxt is not None:
                dirs.move(nxt, False)
            return 'maybe'
        return None if eq or name[2:] in TAR_LONG_FLAGS or state == 'value' else 'maybe'
    if not t.startswith('-') or len(t) < 2:
        return None
    clean = not doubt
    for j, c in enumerate(t[1:], 1):
        if c == 'C':
            rest = t[j + 1:]
            if clean:
                if rest:
                    dirs.move(tail_word(w, rest), True)
                    return None
                return 'folder'
            folder = tail_word(w, rest) if rest else nxt
            if folder is not None:
                dirs.move(folder, False)
            return 'maybe'
        if c not in TAR_FLAGS:
            clean = False
    if state == 'value':
        return None
    i = next((j for j, c in enumerate(t[1:], 1) if c not in TAR_FLAGS), None)
    if i is None:
        return None
    if t[i] not in TAR_VALUES:
        return 'maybe'
    if i + 1 < len(t):                          # -fa.tar: the value is attached
        return None
    return 'maybe' if doubt else 'value'


def _tar(args, cmd_word):
    """[Word]: the folders tar -x writes: each folder a -C/--directory names, relative to the one
    before it (_TarDirs), with none the working directory, and the working directory too once a -C
    was read that may not be one (_tar_word, _tar_old: fail closed)."""
    first = args[0].text if args else ''
    extract = bool(re.match(r'^[A-Za-z]*x', first)) and not first.startswith('-')       # tar xf a.tar
    dirs, k, state = _TarDirs(), 0, None
    if extract:
        k, state = _tar_old(args, dirs)
    while k < len(args):
        t = args[k].text
        if t == '--' and state is None:
            break
        if t in ('--extract', '--get') or re.match(r'^-[A-Za-z]*x', t) and not t.startswith('--'):
            extract = True
        state = _tar_word(args, k, state, dirs)
        k += 1
    if not extract:
        return []
    if not dirs.seen:
        return [here(cmd_word)]
    return dirs.seen + ([here(cmd_word)] if dirs.unsure else [])


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


# python's short options that take a value: the rest of the word, or the next word (-c CODE,
# -cCODE, -Ic CODE); its long option that takes one
PY_SHORT_VALUE = 'cmWX'
PY_LONG_VALUE = {'--check-hash-based-pycs'}
# the py launcher's version choice (-3, -3.12, -3.12-64), taken before python's own options
PY_LAUNCHER_VER_RE = re.compile(r'^-\d+(\.\d+)*(-(32|64))?$')


def py_program(name, args):
    """(kind, value, flags): how python reads its program from `args`. kind is 'code' (value the
    -c Word), 'module' (-m), 'stdin' (`-`), 'script' or 'none' (no program named: python reads
    standard input). flags is the set of option letters before it that take no value; a -W or -X
    value or a long option adds '?'."""
    flags = set()
    k = 0
    if name == 'py' and args and PY_LAUNCHER_VER_RE.match(args[0].text):
        k = 1
    while k < len(args):
        w = args[k]
        t = w.text
        if t == '-':
            return 'stdin', None, flags
        if t == '--':
            return ('script' if k + 1 < len(args) else 'none'), None, flags
        if t.startswith('--'):
            flags.add('?')
            k += 2 if t in PY_LONG_VALUE else 1
            continue
        if not t.startswith('-') or len(t) < 2:
            return 'script', None, flags
        for j in range(1, len(t)):
            ch = t[j]
            if ch not in PY_SHORT_VALUE:
                flags.add(ch)
                continue
            if t[j + 1:]:
                value, k = sh.Word(t[j + 1:], w.dyn, w.start, w.end), k + 1
            else:
                value, k = (args[k + 1] if k + 1 < len(args) else None), k + 2
            if ch == 'c':
                return 'code', value, flags
            if ch == 'm':
                return 'module', value, flags
            flags.add('?')
            break
        else:
            k += 1
    return 'none', None, flags


# A python -c the guard can prove reads only (py_reads_only): isolated (-I: no working directory
# or PYTHON* variable decides what an import loads) with these other plain flags, importing only
# these modules and naming only these of their attributes (os.path only as `from os.path import`)
PY_READ_FLAGS = set('IESsBbdqOuvP')
PY_READ_MODULES = {
    'json': frozenset(('load', 'loads', 'dump', 'dumps', 'JSONDecodeError')),
    'sys': frozenset(('argv', 'stdin', 'stdout', 'stderr', 'exit', 'version', 'version_info',
                      'platform', 'maxsize', 'executable')),
    're': frozenset(('match', 'fullmatch', 'search', 'findall', 'finditer', 'sub', 'subn',
                     'split', 'escape', 'error', 'A', 'ASCII', 'I', 'IGNORECASE', 'M', 'MULTILINE',
                     'S', 'DOTALL', 'X', 'VERBOSE')),
    'collections': frozenset(('Counter', 'defaultdict', 'OrderedDict', 'deque')),
    'itertools': frozenset(('chain', 'islice', 'groupby', 'count', 'product', 'permutations',
                            'combinations', 'accumulate', 'zip_longest', 'takewhile', 'dropwhile',
                            'starmap', 'repeat', 'compress', 'filterfalse')),
    'math': frozenset(('ceil', 'floor', 'sqrt', 'log', 'log2', 'log10', 'exp', 'fabs', 'fsum',
                       'prod', 'gcd', 'isclose', 'isnan', 'isinf', 'trunc', 'pi', 'e', 'inf', 'nan')),
    'os.path': frozenset(('basename', 'dirname', 'exists', 'isfile', 'isdir', 'isabs', 'join',
                          'getsize', 'getmtime', 'abspath', 'normpath', 'normcase', 'realpath',
                          'relpath', 'split', 'splitext', 'commonpath')),
}
# The only builtins the code may name (an allowlist: type, object and the rest stay out). open
# only as a call that opens for reading (_py_open_reads)
PY_BUILTINS = frozenset(('print', 'len', 'range', 'sorted', 'reversed', 'enumerate', 'zip', 'min', 'max',
                         'sum', 'abs', 'round', 'any', 'all', 'str', 'int', 'float', 'bool', 'list',
                         'dict', 'set', 'tuple', 'repr', 'isinstance', 'map', 'filter', 'open'))
# The only attributes the code may take of a value that is not an imported module: string, list,
# dict, match and read-file methods. A file object's buffer, raw or name, and every class or
# type attribute, stay out
PY_VALUE_ATTRS = frozenset((
    'read', 'readline', 'readlines', 'write', 'flush',
    'strip', 'lstrip', 'rstrip', 'split', 'rsplit', 'splitlines', 'join', 'replace', 'startswith',
    'endswith', 'lower', 'upper', 'find', 'count', 'format', 'encode', 'decode', 'partition',
    'rpartition', 'isdigit',
    'items', 'keys', 'values', 'get', 'append', 'extend', 'sort', 'index', 'most_common',
    'group', 'groups', 'groupdict', 'start', 'end', 'span'))
# names that run text as code, open a file, reach any attribute or module by name, give a value's
# type, or start a debugger or pager: refused as any identifier (a name, attribute, import alias,
# keyword); open is the one builtin call let through, as a Name only
PY_DENIED_NAMES = {'exec', 'eval', 'compile', 'open', 'getattr', 'setattr', 'delattr', 'globals',
                   'locals', 'vars', 'breakpoint', 'help', 'type', 'object', 'super', 'input'}
# identifiers that reach a frame, its globals or code: the way back to every module
PY_FRAME_ATTR_RE = re.compile(r'^(gi|cr|ag|f|tb|co|func|im)_')
PY_OPEN_KEYWORDS = {'encoding', 'errors', 'newline', 'buffering'}
# The only syntax the code may use: statements and expressions that bind, call, compute and loop.
# No def, lambda or class (new names the walk would have to follow), no match (its patterns bind
# and read attributes with no Name or Attribute node), no try, while, del, global, walrus, await,
# yield or template string; any node type a later python adds is refused too
PY_NODES = tuple(t for t in (getattr(ast, n, None) for n in (
    'Module', 'Expr', 'Assign', 'AugAssign', 'For', 'If', 'With', 'withitem', 'Pass', 'Break',
    'Continue', 'Import', 'ImportFrom', 'alias',
    'Call', 'keyword', 'Name', 'Attribute', 'Subscript', 'Slice', 'Starred', 'Constant',
    'JoinedStr', 'FormattedValue', 'BinOp', 'UnaryOp', 'BoolOp', 'Compare', 'IfExp', 'Tuple', 'List',
    'Dict', 'Set', 'ListComp', 'SetComp', 'DictComp', 'GeneratorExp', 'comprehension',
    'Load', 'Store', 'operator', 'unaryop', 'boolop', 'cmpop')) if t is not None)
# Every public builtin name up to python 3.14, written out so the guard does not depend on the
# python it runs on: the python a line starts may be newer than the guard's
PY_BUILTIN_NAMES = frozenset((
    'abs', 'aiter', 'all', 'anext', 'any', 'ascii', 'bin', 'bool', 'breakpoint', 'bytearray', 'bytes',
    'callable', 'chr', 'classmethod', 'compile', 'complex', 'copyright', 'credits', 'delattr', 'dict',
    'dir', 'divmod', 'enumerate', 'eval', 'exec', 'exit', 'filter', 'float', 'format', 'frozenset',
    'getattr', 'globals', 'hasattr', 'hash', 'help', 'hex', 'id', 'input', 'int', 'isinstance',
    'issubclass', 'iter', 'len', 'license', 'list', 'locals', 'map', 'max', 'memoryview', 'min', 'next',
    'object', 'oct', 'open', 'ord', 'pow', 'print', 'property', 'quit', 'range', 'repr', 'reversed',
    'round', 'set', 'setattr', 'slice', 'sorted', 'staticmethod', 'str', 'sum', 'super', 'tuple', 'type',
    'vars', 'zip', 'Ellipsis', 'NotImplemented', 'True', 'False', 'None',
    'ArithmeticError', 'AssertionError', 'AttributeError', 'BaseException', 'BaseExceptionGroup',
    'BlockingIOError', 'BrokenPipeError', 'BufferError', 'BytesWarning', 'ChildProcessError',
    'ConnectionAbortedError', 'ConnectionError', 'ConnectionRefusedError', 'ConnectionResetError',
    'DeprecationWarning', 'EOFError', 'EncodingWarning', 'EnvironmentError', 'Exception',
    'ExceptionGroup', 'FileExistsError', 'FileNotFoundError', 'FloatingPointError', 'FutureWarning',
    'GeneratorExit', 'IOError', 'ImportError', 'ImportWarning', 'IndentationError', 'IndexError',
    'InterruptedError', 'IsADirectoryError', 'KeyError', 'KeyboardInterrupt', 'LookupError',
    'MemoryError', 'ModuleNotFoundError', 'NameError', 'NotADirectoryError', 'NotImplementedError',
    'OSError', 'OverflowError', 'PendingDeprecationWarning', 'PermissionError', 'ProcessLookupError',
    'PythonFinalizationError', 'RecursionError', 'ReferenceError', 'ResourceWarning', 'RuntimeError',
    'RuntimeWarning', 'StopAsyncIteration', 'StopIteration', 'SyntaxError', 'SyntaxWarning',
    'SystemError', 'SystemExit', 'TabError', 'TimeoutError', 'TypeError', 'UnboundLocalError',
    'UnicodeDecodeError', 'UnicodeEncodeError', 'UnicodeError', 'UnicodeTranslateError',
    'UnicodeWarning', 'UserWarning', 'ValueError', 'Warning', 'WindowsError', 'ZeroDivisionError'))
# names a stored name or import alias may not take: it would hide the builtin only when the
# binding runs, so a name read as the binding might reach the builtin. The fixed list joined with
# the running python's own, so a python newer or older than the guard's is covered either way
PY_SHADOWED = PY_BUILTIN_NAMES | frozenset(dir(builtins)) | frozenset(PY_DENIED_NAMES)


def _py_open_reads(call):
    """True when the open() call opens for reading only: a path, then at most a literal mode
    without w, a, x or +, and only plain keywords."""
    if not 1 <= len(call.args) <= 2 or any(isinstance(a, ast.Starred) for a in call.args):
        return False
    mode = call.args[1] if len(call.args) == 2 else None
    for kw in call.keywords:
        if kw.arg == 'mode' and mode is None:
            mode = kw.value
        elif kw.arg not in PY_OPEN_KEYWORDS:
            return False                        # **kwargs, opener=, a second mode
    if mode is None:
        return True
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) <= set('rbtU')


def _py_ident_ok(name):
    """False for an identifier the code may not hold anywhere: one starting with _, a frame
    identifier, or one of PY_DENIED_NAMES."""
    return not (name.startswith('_') or PY_FRAME_ATTR_RE.match(name) or name in PY_DENIED_NAMES)


def py_reads_only(code, flags, ps=False):
    """True when the guard can prove the python -c Word `code`, run with the option letters
    `flags`, writes no file: literal text, isolated mode, only PY_NODES syntax, imports and module
    attributes only from PY_READ_MODULES, a module name only as `module.attr`, other attributes
    only from PY_VALUE_ATTRS, names only from PY_BUILTINS, the imports and the names the code
    stores, no identifier _py_ident_ok refuses, and open() only as a call that opens for reading.
    `ps`: the line reaches python through PowerShell, whose native-argument quoting can hand python
    other text than the Word when it holds a double quote or a backslash (PowerShell 5.1 also
    wraps an argument with a space in quotes without doubling a trailing backslash), so such code
    is not proved; nor is empty or blank code, which PowerShell 5.1 drops so that python takes the
    next word as its code. Each import name binds once. Any text it cannot parse, or any other
    shape, is not proved (False)."""
    if code is None or code.dyn or 'I' not in flags or not flags <= PY_READ_FLAGS:
        return False
    if not code.text.strip():
        return False
    if ps and (ps_native_unsure(code) or '\\' in code.text):
        return False
    try:
        tree = ast.parse(code.text, mode='exec')
    except Exception:                           # SyntaxError, ValueError, RecursionError: unread
        return False
    mods, imported, stored, aliases, parents = {}, set(), set(), set(), {}
    for node in ast.walk(tree):
        if not isinstance(node, PY_NODES):
            return False
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in PY_READ_MODULES or '.' in a.name:
                    return False
                mods[a.asname or a.name] = PY_READ_MODULES[a.name]
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in PY_READ_MODULES:
                return False
            if any(a.name not in PY_READ_MODULES[node.module] for a in node.names):
                return False                    # `*` too
            imported.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.alias):
            bound = node.asname or node.name
            if not all(_py_ident_ok(n) for n in (node.name, node.asname) if n) \
                    or bound in PY_SHADOWED or bound in aliases:
                return False                    # a name two imports bind: which one is unknown
            aliases.add(bound)
        elif isinstance(node, ast.keyword):
            if node.arg is not None and not _py_ident_ok(node.arg):
                return False
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stored.add(node.id)
    if stored & (set(mods) | imported | PY_SHADOWED):
        return False                            # a module, an import or a builtin rebound
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if not _py_ident_ok(node.attr) or not isinstance(node.ctx, ast.Load):
                return False
            v = node.value
            allowed = mods[v.id] if isinstance(v, ast.Name) and v.id in mods else PY_VALUE_ATTRS
            if node.attr not in allowed:
                return False
        elif isinstance(node, ast.Name):
            n, parent = node.id, parents.get(node)
            if n == 'open':
                if not (isinstance(parent, ast.Call) and parent.func is node and _py_open_reads(parent)):
                    return False
                continue
            if not _py_ident_ok(n):
                return False
            if n in mods:
                if not (isinstance(parent, ast.Attribute) and parent.value is node
                        and isinstance(node.ctx, ast.Load)):
                    return False                # a module passed on, rebound or aliased
            elif n not in PY_BUILTINS and n not in imported and n not in stored:
                return False                    # any other builtin, or a name never bound
    return True


def _python(cmd, name, args, cmd_word, via=None):
    # Start-Process hands python one command line the guard re-split with its quotes taken out
    # (commands._split_list), so what python reads is not the words below
    if via == 'start-process':
        return [here(cmd_word)]
    shells, c = set(), cmd
    while c is not None:                        # every shell on the way to python
        shells.add(c.shell)
        # a word PowerShell may hand on as other than one argument (dropped when empty, split at a
        # double quote): python's own words, or the text of a bash -c around it, are not the
        # ones read here
        if c.shell == sh.PS and any(ps_native_unsure(w) for w in c.words):
            return [here(cmd_word)]
        c = c.nested_in
    kind, code, flags = py_program(name, args)
    if kind == 'code':
        # through cmd.exe the Word is not what python gets: cmd ends a quoted part at a \" python
        # reads as a quote, and keeps print(...) one word where the read splits it at the (
        if sh.CMD in shells:
            return [here(cmd_word)]
        return [] if py_reads_only(code, flags, sh.PS in shells) else [here(cmd_word)]
    if kind == 'stdin':
        return [here(cmd_word)]
    if kind in ('module', 'script'):
        return []                               # its own file decides what it writes
    if cmd.piped_in or any(r.op in ('<<', '<<<', '<') for r in cmd.redirects):
        return [here(cmd_word)]                 # a script read from standard input
    return []


# The coarse rule for a PowerShell writer (ps_unread). Parameters whose value is no path: their one
# value is passed over when it holds no command (_ps_value_end).
PS_VALUE_PARAMS = frozenset(('value', 'encoding', 'filter', 'include', 'exclude', 'newname', 'inputobject',
                             'delimiter', 'itemtype', 'uri', 'body', 'headers'))
_PS_SQ = r"'(?:[^'‘’‚‛]|'')*'"
_PS_DQ_PLAIN = r'"[^"“”„`$]*"'
_PS_BARE = r'[^\s$(){}\[\]@`\'"“”„‘’‚‛,;|&<>#]+'
_PS_VAR = r'\$(?:\{[A-Za-z_]\w*(?::[A-Za-z_]\w*)?\}|[A-Za-z_]\w*(?::[A-Za-z_]\w*)?)'
_PS_DQ = r'"(?:[^"“”„`$]|`[^\r\n]|%s)*"' % _PS_VAR
_PS_END = r'(?=[\s;|)}]|$)'
# a native program's argument: plain words and strings with no variable
PS_PLAIN_ARG_RE = re.compile(r'(?:%s|%s|%s)+%s' % (_PS_BARE, _PS_SQ, _PS_DQ_PLAIN, _PS_END))
# a cmdlet's argument: a variable, or a string with one, is one argument too
PS_CMDLET_ARG_RE = re.compile(r'(?:%s|%s|%s|%s(?:\.[A-Za-z_]\w*)*)+%s' % (_PS_BARE, _PS_SQ, _PS_DQ, _PS_VAR, _PS_END))
_PS_CALL_ARG = r"\s*(?:%s|%s|-?\d+)\s*" % (_PS_SQ, _PS_VAR)
_PS_CHAIN = re.compile(r'(?:\.[A-Za-z_]\w*(?:\((?:%s(?:,%s)*)?\))?)*' % (_PS_CALL_ARG, _PS_CALL_ARG))
_PS_VALUE_ATOM = re.compile(r'%s|%s|%s|%s' % (_PS_VAR, _PS_SQ, _PS_DQ, _PS_BARE))
_PS_VALUE_END = re.compile(_PS_END)
# a here-string body line PowerShell may end it on: any quote of its kind then @, indented or not
_PS_HERE_CLOSE = {"'": re.compile("(?m)^\\s*['‘’‚‛]@"), '"': re.compile('(?m)^\\s*["“”„]@')}


def _ps_group_end(src, k):
    """The offset just past the ( ) that opens at src[k], or None when it holds what this does not
    follow (a comment, a here-string, a typographic quote) or does not close."""
    depth, n = 0, len(src)
    while k < n:
        c = src[k]
        if c == '`':
            k += 2
            continue
        if c in "'\"":
            if src[k - 1:k] == '@':
                return None
            k = _net_string_end(src, k)
            continue
        if c in '#“”„‘’‚‛':
            return None
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return None


def _ps_value_end(cmd, p):
    """The offset just past the one value that starts at source offset p, when it holds no command
    and PowerShell passes it as one argument: a plain word or string, a variable, a ( ) the
    scanner's word spans too, each with member calls on plain arguments after it ($s.Trim(),
    (Get-Date).AddDays(1)), then a space or the end; else None."""
    src = cmd.source
    if src.startswith('(', p):
        e = _ps_group_end(src, p)
        if e is None or not any(w.start <= p and w.end == e for w in cmd.words if w.start is not None):
            return None
    elif src.startswith(("@'", '@"'), p):
        q = src[p + 1]
        w = next((w for w in cmd.words if w.start == p and w.end is not None and
                  src[w.end - 2:w.end] == q + '@' and src[p + 2:p + 4].lstrip('\r')[:1] == '\n'), None)
        if w is None:
            return None
        body = src[p + 2:w.end - 2]
        # a $( ) runs a command; a line PowerShell would close on (a typographic quote) is not
        # the end the scanner read
        if (q == '"' and '$(' in body) or _PS_HERE_CLOSE[q].search(body):
            return None
        e = w.end
    else:
        m = _PS_VALUE_ATOM.match(src, p)
        if not m:
            return None
        e = m.end()
    e = _PS_CHAIN.match(src, e).end()
    return e if _PS_VALUE_END.match(src, e) else None


def ps_unread(cmd, args, spec=None):
    """[Word]: for a PowerShell writer (a write cmdlet, an Export-*, robocopy or xcopy, curl, wget
    or tar), a target that is not literal and has no span when any argument is one the guard does
    not read as the scanner did, else []. Coarse by design, it does not depend on how the scanner
    read a ( or a { : the arguments of a cmdlet (`spec`) other than one value of a parameter in
    PS_VALUE_PARAMS must each be a plain word, string or variable (PS_CMDLET_ARG_RE), a native
    program's a plain word or string (PS_PLAIN_ARG_RE: PowerShell spreads a splat or an array into
    its arguments); none may follow a comma (an array binds to the parameter before it); and the
    command may be neither split nor followed by a ( ) or { } that no value accounts for."""
    stand_in = [sh.Word('(an argument the guard does not read)', True, cmd.end, cmd.end)]
    if cmd.split:
        return stand_in
    src = cmd.source
    spans = []                          # [start, end) of the values passed over
    if spec is not None:
        for k, w in enumerate(args):
            t = sh.ps_dashed(w.text)
            if w.start is None or cm.quoted(cmd, w) or not t.startswith('-') or len(t) < 2:
                continue
            body, colon, val = t[1:].partition(':')
            body = body.lower()
            names = spec.names()
            cands = [body] if body in names else [spec.aliases[body]] if body in spec.aliases \
                else [x for x in names if x.startswith(body)]
            if not cands or not all(c in PS_VALUE_PARAMS for c in cands) or not any(c in spec.values for c in cands):
                continue
            if colon and val:
                p = src.find(':', w.start) + 1
            elif k + 1 < len(args) and args[k + 1].start is not None:
                p = args[k + 1].start
            else:
                continue
            e = _ps_value_end(cmd, p)
            if e is not None:
                spans.append((p, e))
    arg_re = PS_CMDLET_ARG_RE if spec is not None else PS_PLAIN_ARG_RE
    first = next((w.start for w in args if w.start is not None), None)
    prev = None                         # the word before the first argument: the program's name
    if first is not None:
        prev = max((x for x in cmd.words if x.end is not None and x.end <= first),
                   key=lambda x: x.end, default=None)
    for w in args:
        if w.start is None or w.start == w.end:
            if w.dyn:
                return stand_in
            continue
        if prev is not None and prev.end is not None and ',' in src[prev.end:w.start]:
            return stand_in
        prev = w
        if any(s <= w.start and w.end <= e for s, e in spans):
            continue
        if sh.ps_dashed(w.text).startswith('-') and not cm.quoted(cmd, w) and spec is not None and \
                any(s == src.find(':', w.start) + 1 and w.start < s and w.end <= e for s, e in spans):
            continue                    # -Value:$v: the value passed over
        m = arg_re.match(src, w.start)
        if not m or m.end() != w.end:
            return stand_in
    if cmd.next_op in ('(', '{') and not any(s < cmd.end < e for s, e in spans):
        return stand_in
    return []


def write_targets(cmd, inv):
    """[Word]: what the command writes, by its redirections and the file commands the guard
    knows. A Word whose text is '.' stands for the directory the command runs in."""
    out = []
    for r in cmd.redirects:
        if not r.writes:
            continue
        t = r.target
        if t is None:
            if cmd.shell == sh.PS:      # `> <#c#>(...)`: a target the scanner did not read
                out.append(sh.Word('(a redirection target the guard does not read)', True, cmd.end, cmd.end))
            continue
        if t.text.lower() in NULL_TARGETS:
            continue
        if cmd.shell == sh.PS and not t.dyn and t.start is not None and t.start != t.end:
            # the coarse rule (ps_unread) for a redirection: a target that is not one plain word,
            # string or variable (`> `<newline>(...)`) is one the line computes
            m = PS_CMDLET_ARG_RE.match(cmd.source, t.start)
            if not m or m.end() != t.end:
                t = sh.Word(t.text, True, t.start, t.end)
        out.append(t)
    if net_mentioned(cmd):
        net, unread = net_writes(cmd)
        out.extend(x.word for x in net if net_placed(x.word))
        # The rest are dotnet_unplaced's, and the directory the command runs in is judged too,
        # as it was for every .NET call: the repository's roles there answer for any session.
        if unread or any(not net_placed(x.word) for x in net):
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
        return out + _copier(args) + (ps_unread(cmd, args) if cmd.shell == sh.PS else [])
    if cmd.shell != sh.PS and name == 'find':
        if any(w.text == '-delete' for w in args):
            return out + (_find_paths(args)[0] or [here(word)])
        return out
    if cmd.shell == sh.PS and not inv.path.lower().endswith('.exe'):
        cmdlet = PS_ALIASES.get(name, name)
        if cmdlet in PS_CMDLETS and cmdlet != 'get-childitem':
            spec = PS_CMDLETS[cmdlet]
            bound = ps_args(cmd, args, cmdlet)
            # a provider-aware cmdlet's item on a drive that holds no files (env:FOO) is no file;
            # every other writer (a native program, a script function, Out-File) gets a file
            native = name not in PS_PROVIDER_CMDLETS or inv.via is not None
            for p in spec.writes:
                out.extend(x for x in bound.get(p, []) if native or not ps_provider_item(cmd, x))
            if spec.writes:
                out.extend(ps_unread(cmd, args, spec))
            if cmdlet == 'expand-archive' and not bound.get('destinationpath'):
                out.append(here(word))
            if cmdlet == 'new-item' and bound.get('name'):
                base = (bound.get('path') or [None])[0]
                for n in bound['name']:
                    x = sh.Word(ntpath.join(base.text, n.text) if base is not None else n.text,
                                n.dyn or (base is not None and base.dyn), n.start, n.end)
                    if native or not ps_provider_item(cmd, x):
                        out.append(x)
            if name not in ('curl', 'wget'):
                return out
        elif cmdlet.startswith('export-') and cmdlet not in PS_CMDLETS:
            return out + ps_export_targets(cmd, args) + ps_unread(cmd, args, PS_CMDLETS['export-clixml'])
    if cmd.shell == sh.CMD and name in CMD_WRITERS:
        pos = [w for w in args if not w.text.startswith('/')]
        if name == 'type':
            return out
        if name in ('copy', 'xcopy', 'robocopy', 'ren', 'rename'):
            return out + pos[-1:] if name != 'robocopy' else out + pos[1:2]
        return out + pos
    # a native writer PowerShell runs (curl and wget are also Invoke-WebRequest's aliases there)
    grouped = ps_unread(cmd, args) if cmd.shell == sh.PS and name in ('curl', 'wget', 'tar') else []
    if grouped and any(x.start == x.end == cmd.end and x.dyn for x in out):
        grouped = []                            # the cmdlet branch's stand-in already
    if name == 'curl':
        return out + _curl(args, word) + grouped
    if name == 'wget':
        return out + _wget(args, word) + grouped
    if cm.PYTHON_RE.match(name):
        return out + _python(cmd, name, args, word, inv.via)
    if name in EVALS:
        return out + _evals(name, args, word)
    if name == 'tar':                           # Windows' own tar.exe runs from PowerShell too
        return out + _tar(args, word) + grouped
    if cmd.shell == sh.PS:
        return out
    if name == 'sed':
        return out + _sed(args)
    if name == 'perl':
        return out + _perl(args, word)
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


def into_folder(target, w, into, cwd, home=None):
    """[path]: what a write of `target` (the Word `w`) writes. When `into` (copy_into) names `w`
    as the destination and `target` is an existing folder, <target>/<each source's name>; a
    source the guard cannot name (not literal, a wildcard, a drive-qualified name, no name, or a
    folder where only a file is read so) keeps the folder itself. `~` is `home` (ctx.home)."""
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
            src = sh.resolve_path(s.text, cwd, home=home)
            if not src or not os.path.isfile(src):
                return [target]
        out.append(os.path.join(target, base))
    return out


# A PowerShell item on a built-in drive that holds no files: env:FOO, Env:\FOO, variable:x,
# function:f, alias:a, HKCU:\x, HKLM:/x, Cert:\x, WSMan:\x, or the provider spelled out
# (Environment::FOO, Microsoft.PowerShell.Core\Registry::HKEY_CURRENT_USER\x). Temp: and
# FileSystem:: hold files and are not here.
PS_PROVIDER_ITEM_RE = re.compile(
    r'(?i)^(?:(?:env|variable|function|alias|hkcu|hklm|cert|wsman):'
    r'|(?:microsoft\.(?:powershell\.(?:core|security)|wsman\.management)\\)?'
    r'(?:environment|variable|function|alias|registry|certificate|wsman)::)')
DOTDOT_RE = re.compile(r'(?:^|[\\/:])\.\.(?:[\\/]|$)')
# A line that may remap a drive name to a folder (New-PSDrive env FileSystem <folder>) or run code
# the text does not show: there env:x may be a file, so it is judged as one. Matched against the
# raw line and against each word as the parser decodes it (n`dr reads ndr, 'New-PSDr'+'ive' reads
# New-PSDr+ive).
PS_DRIVE_REMAP_RE = re.compile(r'(?im)drive|psdr|\b(?:ndr|rdr|mount|iex|ipmo)\b|invoke-expression|import-module'
                               r'|\.ps1\b|\.psm1\b|scriptblock|sessionstate|(?:^|[;&|({])\s*\.\s')
# Session objects, .NET types and static calls a line may reach a drive through
PS_DOTNET_RE = re.compile(r'(?im)\$executioncontext|\$host\b|new-object|add-type|^\s*using\b|[\])]\s*::')
# The provider-aware item and content cmdlets, as the line names them (the canonical name or one
# of these aliases): their item on a drive that holds no files is no file. Script functions
# (Compress-Archive, mkdir) and the file-only writers (Out-File, Tee-Object, Export-*,
# Start-Transcript, Invoke-WebRequest -OutFile) are not here and are judged as files.
PS_PROVIDER_CMDLETS = frozenset(('new-item', 'set-content', 'add-content', 'clear-content', 'remove-item',
                                 'rename-item', 'move-item', 'copy-item', 'set-itemproperty',
                                 'ni', 'sc', 'ac', 'clc', 'ri', 'del', 'rni', 'mi', 'move', 'cpi', 'copy', 'sp'))
# Read-only cmdlets a line of provider items may also hold
PS_READ_CMDLETS = frozenset(('get-item', 'gi', 'get-content', 'gc', 'get-childitem', 'gci', 'test-path',
                             'get-location', 'gl', 'pwd', 'write-output', 'echo', 'write-host'))
# The one native program such a line may also run: git, named literally (git.exe cannot remap a
# PowerShell drive, and its words meet the git rules)
PS_PLAIN_GIT = ('git', 'git.exe')
# A word that only reads an environment variable, with a literal tail: $env:TEMP, "$env:TEMP\x.txt"
PS_ENV_READ_WORD_RE = re.compile(r'(?i)^\$(?:env:[A-Za-z_]\w*|\{env:[A-Za-z_]\w*\})(?:[\\/][^$`()\[\]{}]*)?$')


def ps_line_plain(cmd):
    """True when every command on the PowerShell line `cmd` comes from is a plain call: the parser
    reads the line cleanly; each command's first word is a literal name of a cmdlet in
    PS_PROVIDER_CMDLETS or PS_READ_CMDLETS, or git (PS_PLAIN_GIT) (no call or dot-source operator,
    no assignment or other expression statement, no computed name, no Start-Process); every word is
    literal or only reads an environment variable (PS_ENV_READ_WORD_RE; no other variable, member
    call or subexpression); and neither the raw line nor any decoded word matches
    PS_DRIVE_REMAP_RE, commands.PS_SCRIPT_TEXT_RE or PS_DOTNET_RE. Anything else may remap a drive.
    The line is read again with `~` as the home its own parse read (Command.home)."""
    src = cmd.source
    if any(r.search(src) for r in (PS_DRIVE_REMAP_RE, cm.PS_SCRIPT_TEXT_RE, PS_DOTNET_RE)):
        return False
    parsed = cm.read(src, sh.PS, cmd.cwd, home=cmd.home)
    if not parsed.clean:
        return False
    for c in parsed.commands:
        if not c.words:
            return False
        inv = cm.invocation(c)
        if inv is None or inv.dyn or inv.via is not None or inv.index != 0 or c.words[0].dyn:
            return False
        git = inv.name == 'git' and c.words[0].text.lower() in PS_PLAIN_GIT
        if not git and inv.name not in PS_PROVIDER_CMDLETS | PS_READ_CMDLETS:
            return False
        for w in c.words:
            if w.dyn and not PS_ENV_READ_WORD_RE.match(w.text) or any(r.search(w.text) for r in (PS_DRIVE_REMAP_RE, cm.PS_SCRIPT_TEXT_RE, PS_DOTNET_RE)):
                return False
    return True


def ps_provider_item(cmd, w):
    """True when the Word `w`, an item a cmdlet in PS_PROVIDER_CMDLETS writes (write_targets calls
    it only for those, never for a native program, curl, wget, Start-Process or another cmdlet),
    names an item on a built-in drive that holds no files (PS_PROVIDER_ITEM_RE): a literal word,
    no `..` part, on a line of plain calls (ps_line_plain). Writing such an item writes no file. A
    native program reads env:FOO as the stream FOO of a file named env: a file write."""
    if cmd.shell != sh.PS or w.dyn or not PS_PROVIDER_ITEM_RE.match(w.text) or DOTDOT_RE.search(w.text):
        return False
    return ps_line_plain(cmd)


# a PowerShell word that is the location and a literal rest: $PWD, "${PWD}\x", $pwd/x/y
PS_PWD_WORD_RE = re.compile(r'(?i)^(?:\$pwd|\$\{pwd\})((?:[\\/][^$`]*)?)$')
PS_BRACED_VAR_RE = re.compile(r'\$\{[^}]*\}')     # ${name}: a variable, no code


def ps_pwd_path(cmd, w):
    """The path text a PowerShell word spells as $PWD (or ${PWD}) and a literal rest, with the
    location as cmd.cwd, or None: the word is not one, the directory is unknown, or the write is
    not the line's only command. $PWD can be given another value under many spellings of its
    name (sv PWD, -OutVariable ('P'+'WD'), [scriptblock]::Create(...).Invoke()), and only code
    run before the target is read can do it: another command (a ; && || or newline, a pipeline,
    a script block or ( ) group around it) or an argument of its own that runs code (a ( ),
    $( ) or { } in a computed word). Each of those fails closed."""
    if cmd.shell != sh.PS or not w.dyn or cmd.cwd is None:
        return None
    m = PS_PWD_WORD_RE.match(w.text)
    if not m:
        return None
    src = cmd.source or ''
    if cmd.start is None or cmd.nested_in is not None or cmd.groups or cmd.subs or cmd.prev_op or cmd.next_op or \
            cmd.piped_in or cmd.piped_out or src[:cmd.start].strip() or src[cmd.end:].strip():
        return None                             # not the line's one command
    args = list(cmd.words) + [r.target for r in cmd.redirects if r.target is not None]
    if any(x is not w and x.dyn and re.search(r'[({]', PS_BRACED_VAR_RE.sub('', x.text)) for x in args):
        return None                             # an argument that runs code: (...), $(...), {...}
    return cmd.cwd + m.group(1)


BASH_DEVICE_RE = re.compile(r'^/dev/')         # /dev/stderr, /dev/tty, /dev/fd/3: no file
BASH_TMP_RE = re.compile(r'^/tmp(?:/|$)')        # Git Bash mounts /tmp on a temp folder: jail.bash_tmp


def shell_writes(event, ctx, cmd, inv, cwd, role):
    d = git_jail(event, ctx, cmd, inv, cwd, role)
    if d:
        return d
    d = dotnet_unplaced(event, ctx, cmd, cwd, role) or dotnet_process_dir(event, ctx, cmd, cwd, role)
    if d:
        return d
    net = {}
    if net_mentioned(cmd):
        net = dict(((x.word.start, x.word.end), x) for x in net_writes(cmd)[0])
    into = copy_into(cmd, inv)
    searched = False
    for w in write_targets(cmd, inv):
        if cmd.shell == sh.BASH and not w.dyn and BASH_DEVICE_RE.match(w.text):
            continue
        target, why, ps_drive = write_place(ctx, cmd, w)
        if target is None and not searched:
            searched = True                     # find's start paths answer for every such target at once
            d = find_exec_writes(event, ctx, cmd, cwd, role)
            if d:
                return d
        if target is None and cmd.shell == sh.PS and w.dyn:
            ps_drive = True                     # PowerShell's, from any location: one $p or (...) can name the main checkout
        elif target is None and cmd.cwd is not None and not ps_drive and \
                ctx.place(cmd.cwd).kind not in (repo.MAIN, repo.UNKNOWN):
            continue                            # a target it cannot place, from a worktree or elsewhere
        what = '`%s` writes %s' % (cmd.text, w.text if w.text != '.' else 'files in the directory it runs in')
        # here() or ps_unread(): no word to aim
        stand_in = (w.text == '.' and cmd.source[w.start:w.end] != '.') or w.start == w.end
        part = any((x.start, x.end) == (w.start, w.end) and x.text != w.text for x in cmd.words)
        rewrite = None if stand_in or part else (lambda path, w=w: retarget(cmd, w, path))   # part: -tDIR
        if (w.start, w.end) in net and not w.dyn:
            rewrite = lambda path, x=net[(w.start, w.end)]: net_retarget(cmd, x, path)
        for t in into_folder(target, w, into, cmd.cwd, getattr(ctx, 'home', None)) if target else [target]:
            d = edit_target(event, ctx, t, cwd, role, what, why, rewrite=rewrite, run=bare(cmd))
            if d:
                return d
        if ps_drive:
            d = ps_drive_jail(event, ctx, cwd, role, what, why, rewrite)
            if d:
                return d
    return None


def write_place(ctx, cmd, w):
    """(path, why, ps_drive) of the Word `w` the command `cmd` writes, read from cmd's directory
    and shell: path None when the guard cannot place it (why says why), and ps_drive when that is
    a PowerShell drive or provider path (ps_drive_jail judges it)."""
    ps_drive = False                            # a PowerShell drive or provider it cannot place
    pwd = ps_pwd_path(cmd, w)
    if pwd is not None:
        target, why = sh.resolve_path(pwd, cmd.cwd, cmd.shell, getattr(ctx, 'home', None)), None
    elif w.dyn:
        target, why = None, 'a target that is not literal: %s' % w.text
    elif cmd.shell == sh.BASH and BASH_TMP_RE.match(w.text):
        target, why = paths.norm(os.path.join(jail.bash_tmp(ctx), w.text[5:]), fold=False), None
    elif cmd.shell == sh.PS and PS_PROVIDER_ITEM_RE.match(w.text) and sh.ps_unplaceable(w.text):
        ps_drive = True                         # ps_provider_item did not skip it: the line may remap the drive
        target, why = None, ('a PowerShell provider item (%s) on a line that is not only plain cmdlet calls, '
                             'so the drive may be remapped to a folder; run the item write in a call of '
                             'its own' % w.text)
    elif cmd.shell == sh.PS and sh.ps_unplaceable(w.text):
        ps_drive = True
        target, why = None, 'a PowerShell drive or provider path the guard cannot place: %s' % w.text
    else:
        target = sh.resolve_path(w.text, cmd.cwd, cmd.shell, getattr(ctx, 'home', None))
        why = None if target else 'the directory it runs in is unknown' if cmd.cwd is None \
            else 'a path the text cannot place: %s' % w.text
        if target is None and cmd.cwd is None and cmd.cwd_ps_drive:
            ps_drive = True                     # after cd Main:\ or cd Registry::x
            why = 'the directory it runs in is a PowerShell drive or provider path the guard cannot place'
    return target, why, ps_drive


def find_runs(cmd):
    """[(find command, its invocation)]: each bash find whose -exec, -execdir, -ok or -okdir runs
    `cmd`, nearest first, through any shell nested between (commands.expand nests only those
    words under a find)."""
    out, outer = [], cmd.nested_in
    while outer is not None:
        if outer.shell == sh.BASH:
            inv = cm.invocation(outer)
            if inv is not None and not inv.dyn and inv.name == 'find':
                out.append((outer, inv))
        outer = outer.nested_in
    return out


def find_exec_writes(event, ctx, cmd, cwd, role):
    """A write the guard cannot place by a command a bash find runs (find_runs): the found path
    filled into {}, a path the line computes ("$1" in a shell given {}), or a relative path in a
    found file's folder (-execdir). It is judged as each such find's start paths, or the directory
    find runs in when it names none, as -delete is (write_targets): every path find hands the
    command is under them. A start path the guard cannot place passes from a worktree, as any
    target does (shell_writes)."""
    for f, inv in find_runs(cmd):
        for w in _find_paths(inv.args)[0] or [here(inv.word)]:
            if not w.dyn and BASH_DEVICE_RE.match(w.text):
                continue
            target, why = write_place(ctx, f, w)[:2]
            if target is None and f.cwd is not None and ctx.place(f.cwd).kind not in (repo.MAIN, repo.UNKNOWN):
                continue
            what = '`%s` runs `%s` on what it finds in %s' % (
                bare(f), bare(cmd), w.text if w.text != '.' else 'the directory it runs in')
            stand_in = (w.text == '.' and f.source[w.start:w.end] != '.') or w.start == w.end
            part = any((x.start, x.end) == (w.start, w.end) and x.text != w.text for x in f.words)
            rewrite = None if stand_in or part else (lambda path, w=w, f=f: retarget(f, w, path))
            d = edit_target(event, ctx, target, cwd, role, what, why, rewrite=rewrite, run=bare(f))
            if d:
                return d
    return None


NET_RELATIVE = ('.NET reads a relative path against the process\'s working directory '
                '([Environment]::CurrentDirectory), not the PowerShell location: Set-Location and cd do not move it, '
                'so the file may land in the checkout the session started in.')


def dotnet_unplaced(event, ctx, cmd, cwd, role):
    """A .NET file call (net_writes) that writes a path the guard cannot place: a relative path,
    a rooted one with no drive, or a path the line computes. .NET resolves it against the
    process's directory (NET_RELATIVE), which the guard cannot tell, from a worktree too. Refused
    for a role that may not edit a main checkout (main-checkout) and for an agent session
    (write-jail); the owner's own session is not judged. A relative path is first judged where the
    PowerShell location puts it (edit_target), and the alternative is the call on that absolute
    path."""
    if not net_mentioned(cmd):
        return None
    for x in net_writes(cmd)[0]:
        w = x.word
        if net_placed(w):
            continue
        plain = not w.dyn and w.text and w.text[0] not in '\\/~' and ':' not in w.text
        fixed = paths.norm(os.path.join(cmd.cwd, w.text), fold=False) if plain and cmd.cwd else None
        start = cmd.words[0].start if cmd.words else cmd.start
        what = '`%s` writes %s' % (cmd.source[start:x.end],
                                   'a path the line computes (%s)' % w.text if w.dyn else w.text)
        rewrite = lambda path, x=x: net_retarget(cmd, x, path)
        if fixed:
            d = edit_target(event, ctx, fixed, cwd, role, what, rewrite=rewrite)
            if d:
                return d
        cfg = ctx.config(fixed or cmd.cwd) if fixed or cmd.cwd else None
        if not kconfig.permissions(event, kconfig.roles(cfg)).get('edit_main_checkout'):
            rule = 'main-checkout'
        elif agent_session(event, ctx):
            rule = 'write-jail'
        else:
            continue
        why = '%s through .NET. %s' % (what, NET_RELATIVE)
        if w.dyn:
            why += ' The guard does not read a path the line computes.'
        elif re.match(r'^[\\/]{2}', w.text):
            why += ' The guard cannot tell which folder a share path names.'
        if fixed:
            return refuse(rule, role, why + ' The same write on the absolute path:', rewrite(fixed))
        base = own_worktree(ctx, cwd) or cmd.cwd
        if base:
            return refuse(rule, role, why + ' Give it a literal absolute path:', rewrite(fwd(base) + '/<file>'))
        return refuse(rule, role, why + ' Give it a literal absolute path; to see where this session runs:', 'pwd')
    return None


def dotnet_process_dir(event, ctx, cmd, cwd, role):
    """A .NET write the guard does not read the path of, or one it cannot place (net_writes,
    net_placed), judged in each directory its process may have started in (edit_target): the
    directory the call runs in (`cwd`) and the PowerShell location of each command it is nested
    in, before and after a cd in that command's $( ) (shell.track). .NET reads a relative path there (NET_RELATIVE), so a Set-Location into a worktree does not
    take the write with it; write_targets judges the PowerShell location itself. A session that
    runs outside a worktree of its own (in the main checkout, or outside any repository) is told to
    give the call literal absolute paths, not to make a worktree (that does not move where its
    process starts)."""
    if not net_mentioned(cmd):
        return None
    net, unread = net_writes(cmd)
    if not unread and all(net_placed(x.word) for x in net):
        return None
    dirs, outer = [cwd], cmd.nested_in
    while outer is not None:
        dirs.extend((outer.cwd, outer.subs_cwd))    # subs_cwd: where it stood before a cd in its $( ) moved it
        outer = outer.nested_in
    seen = set([paths.norm(cmd.cwd)] if cmd.cwd else [])
    for d in dirs:
        if not d or paths.norm(d) in seen:
            continue
        seen.add(paths.norm(d))
        what = ('`%s` may write through .NET in %s, the directory its process started in (.NET reads a relative '
                'path there, and Set-Location does not move it; give the call literal absolute paths)'
                % (bare(cmd), fwd(d)))
        target = paths.norm(d, fold=False)
        res = edit_target(event, ctx, target, cwd, role, what, run=bare(cmd))
        place = ctx.place(target) if res and res.rule == 'main-checkout' and own_worktree(ctx, cwd) is None \
            else None
        if place is not None and place.kind == repo.MAIN and not ctx.is_pm_data(place.top):
            # the session runs outside a worktree: a worktree it makes does not move where its process starts
            return refuse('main-checkout', role, '%s is in the main checkout %s; this role may not edit a main '
                          'checkout. A new worktree does not move the directory this PowerShell process '
                          'started in, so call each .NET method directly with literal absolute paths inside '
                          'a worktree. To see where this session runs:' % (what, place.top), 'pwd')
        if res:
            return res
    return None


def ps_drive_jail(event, ctx, cwd, role, what, why, rewrite):
    """An agent session's PowerShell write to a drive or provider path the guard cannot place
    (shell.ps_unplaceable: Main:\\x after New-PSDrive, Registry::x), by a role main_checkout lets
    through (it may edit a main checkout): the jail cannot judge a place it cannot tell, so it
    refuses, as git_jail_unknown does for git."""
    if not agent_session(event, ctx):
        return None
    dest = jail.destination(jail_roots(event, ctx, cwd))
    head = '%s: the guard cannot tell where it writes (%s). Write to a literal path inside the folders an ' \
           'agent session may write.' % (what, why)
    if dest is not None and rewrite is not None:
        return refuse('write-jail', role, head + ' The same write in %s:' % fwd(dest.path),
                      rewrite(fwd(dest.path) + '/<file>'))
    return refuse('write-jail', role, head + ' To see where this session runs:', 'pwd')


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
    return bool(a and b) and paths.norm(a) == paths.norm(b)


# ---------------------------------------------------------------- root searches

# A whole-profile or whole-drive spelling that names a root whatever its value (it may be dynamic)
_ROOT_NAMES = r'(?:HOME|USERPROFILE|HOMEDRIVE|SystemDrive|HOMEPATH)'
_ROOT_VAR = (r'(?:~|\$%s|\$\{%s\}|\$env:%s|\$\{env:%s\}|%%%s%%)' %
             (_ROOT_NAMES, _ROOT_NAMES, _ROOT_NAMES, _ROOT_NAMES, _ROOT_NAMES))
ROOT_VAR_RE = re.compile(r'(?i)^%s{1,2}[\\/]?$' % _ROOT_VAR)       # $env:HOMEDRIVE$env:HOMEPATH too
WILD = re.compile(r'[*?\[]')


def is_root(text, cwd, ctx=None, shell=None):
    """True when `text` names a drive or filesystem root, the user's home, or a variable that is
    one of those; `shell` the shell whose word it is (sh.resolve_path: FileSystem::<drive root> in PS).
    `~` is ctx.home; with no ctx, or one without a home, sh.home_folder's."""
    t = text.strip()
    if not t:
        return False
    if ROOT_VAR_RE.match(t):
        return True
    p = sh.resolve_path(t, cwd, shell, getattr(ctx, 'home', None))
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
        if ROOT_VAR_RE.match(parent.strip()) or (not w.dyn and is_root(parent, where, ctx, cmd.shell)):
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
    if run is None:
        inv = cm.invocation(cmd)
        run = plain_run(cmd, inv) if inv is not None else bare(cmd)
    return 'python %s run --kind %s --repo %s --slug %s -- %s' % (
        lock_tool(ctx), entry['kind'], name, slug, run)


def run_from(cmd, cwd):
    """The sentence that says where to run a command whose directory differs from the call's."""
    if cmd.cwd and cwd and not same_dir(cmd.cwd, cwd):
        return ' Run it with %s as the working directory:' % quote(cmd.cwd)
    return ''


def kit_wrapper(ctx, cmd, inv, cwd):
    """cm.wrapper(inv) when the script it runs is the kit's own (tools/gate.py or
    tools/machine_lock.py of the kit the guard runs from, ctx.kit, the path resolved from the
    command's directory with links followed), else None. Only the kit's tool takes the machine lock
    itself, so only it may run an interpreter a repository lists as heavy: a script that shares its
    file name (a branch's copy, `python <temp>/gate.py`) is judged as any other script. A repository
    holds no copy of the kit's tools (decision 21 in docs/multi-host-plan.md section 7), so an
    opt-in stub fails closed here too."""
    kind = cm.wrapper(inv)
    if not kind:
        return None
    found = cm.python_script(inv)
    word = inv.args[found[0]] if found else inv.word
    if word is None or word.dyn:
        return None
    path = sh.resolve_path(word.text, cmd.cwd or cwd, cmd.shell, getattr(ctx, 'home', None)) or (
        word.text if os.path.isabs(word.text) else None)
    if not path:
        return None
    tools = paths.norm(os.path.join(ctx.kit, 'tools'), real=True)
    return kind if paths.norm(os.path.dirname(path), real=True) == tools else None


def heavy(event, ctx, cmd, inv, cwd, role):
    if inv is None or inv.dyn or kit_wrapper(ctx, cmd, inv, cwd):
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
    'update-ref': ('no-deref', 'stdin', 'create-reflog', 'batch-updates'),
}
GIT_LONGS['stage'] = GIT_LONGS['add']
GIT_LONG_VALUES = {'message', 'file', 'author', 'date', 'template', 'reuse-message', 'reedit-message', 'fixup',
                   'squash', 'cleanup', 'trailer', 'source', 'conflict', 'exclude', 'push-option', 'repo',
                   'receive-pack', 'exec', 'set-upstream-to', 'sort', 'format', 'contains', 'no-contains',
                   'points-at', 'pathspec-from-file', 'directory', 'whitespace', 'include', 'build-fake-ancestor'}
GIT_SHORT_VALUES = {'commit': 'mFCct', 'restore': 's', 'clean': 'e', 'push': 'o', 'stash': 'm', 'branch': 'u',
                    'apply': 'pC', 'update-ref': 'm'}


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
    d = git_send_pack_pmdata(event, ctx, inv, role) or call_subexpr_pmdata(event, ctx, cmd, inv, cwd, role)
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
    if sub == 'update-ref':
        d = git_shared_ref(event, ctx, cmd, gc, cwd, role)    # from any place: every worktree shares refs
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
            why = lander_check(event, ctx, cmd, inv, gc)[1] if place.kind == repo.MAIN else None
            if why is not None:
                return lander_refusal(ctx, cmd, gc, 'git-main-history', role, why)
            how, instead = ref_delete_hint(ctx, gc, top, cwd, short, longs, pos) or git_in_worktree(ctx, cmd, gc, top, cwd)
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
# strict reads (strict_dyn_read): reads no option of which writes a file or runs a program the line
# names, so a computed word (any number of words) leaves them reads; and reads with such an option
# (log, show and diff: --output), where a computed word stays a read only when the text pins it to
# one argument that is no option, or the value of one of GIT_DYN_VALUE_OPTS
GIT_DYN_ANY_READS = {'rev-parse', 'ls-files', 'cat-file', 'merge-base', 'for-each-ref'}
GIT_DYN_PINNED_READS = {'log', 'show', 'diff'}
GIT_DYN_VALUE_OPTS = ('--format=', '--pretty=')
# characters a pinned word may carry outside quotes: none the shell expands, splits or joins on
# (bash globs and braces, PowerShell's array comma and splat @), and no backslash or backtick
DYN_SAFE_BARE = {sh.BASH: frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./~^:=%+-@,'),
                 sh.PS: frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./~^:=%+-')}
DYN_VAR_RE = re.compile(r'\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*|[0-9])')
DYN_PS_VAR_RE = re.compile(r'(?i)\$(?:\{[A-Za-z_][A-Za-z0-9_:]*\}|(?:env:|script:|global:|local:)?[A-Za-z_][A-Za-z0-9_]*)')


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


# git archive's long options in full (git expands a unique prefix: --outp is --output), those that
# take a value, and its short ones (-o takes a value; -0..-9 are compression levels)
ARCHIVE_LONGS = ('format', 'prefix', 'add-file', 'add-virtual-file', 'output', 'worktree-attributes', 'verbose',
                 'list', 'mtime', 'remote', 'exec')
ARCHIVE_VALUES = {'format', 'prefix', 'add-file', 'add-virtual-file', 'output', 'mtime', 'remote', 'exec'}
ARCHIVE_FLAGS = set('vl0123456789')
# --remote runs git-upload-archive (a program --exec names) instead of reading this repository
ARCHIVE_RUNS = {'remote', 'exec'}
# the -c keys (lower-case) an archive may carry and stay a read: none names a program, a filter or
# another file to read config or attributes from. Any other key may make archive run a program in
# the checkout it reads (tar.<format>.command; filter.<driver>.smudge or .process with
# core.attributesFile or the checkout's own attributes; include.path), so it fails closed
ARCHIVE_SAFE_KEY_RE = re.compile(r'^(?:core\.quotepath|core\.autocrlf|core\.eol|tar\.umask|tar\..+\.remote)$')


def ps_native_unsure(w):
    """True when Windows PowerShell 5.1 may hand the word `w` to a program as other than one
    argument (observed): a double quote in it ('\\"a -c b' arrives as "a, -c, b), an empty word
    (dropped), or a blank in it and a backslash at its end (it takes in the words after it)."""
    t = w.text
    return '"' in t or not t or (t.endswith('\\') and any(c.isspace() for c in t))


def cmd_native_unsure(cmd, w):
    """True when the C runtime may split the word `w` that cmd.exe passes on other than cmd reads
    it: cmd toggles quoting at every `"` and keeps backslashes, the C runtime reads `\\"` as a quote
    and `""` inside quotes as one; so only a word with no `"`, or one wholly in one pair of quotes
    with no backslash before the closing one, is read the same (cmd reads a\\" -o f \\"b as one
    word, git.exe gets a", -o, f, "b). A word with no valid place in the source is unsure: its
    text has cmd's quotes taken out already (fail closed)."""
    src = cmd.source or ''
    if w.start is None or w.end is None or not 0 <= w.start <= w.end <= len(src):
        return True
    raw = src[w.start:w.end]
    if '"' not in raw:
        return False
    return not (len(raw) >= 2 and raw[0] == raw[-1] == '"' and '"' not in raw[1:-1] and not raw[:-1].endswith('\\'))


def native_unsure(cmd, w):
    """True when the shell may hand the word `w` to a native program as other than one argument,
    the word the guard reads (ps_native_unsure, cmd_native_unsure)."""
    if cmd.shell == sh.PS:
        return ps_native_unsure(w)
    if cmd.shell == sh.CMD:
        return cmd_native_unsure(cmd, w)
    return False


def dyn_head(cmd, w):
    """The literal text before the first variable of the computed word `w`, read from its source
    when the shell (bash or PowerShell) is sure to hand it to git as one argument that starts with
    that text: outside quotes only DYN_SAFE_BARE characters, inside '...' anything but the quote,
    inside "..." plain text and $name or ${name} only (no $( or backtick that runs code, no "$@" or
    other variable that is many words, no backslash, no embedded quote); else None. PowerShell
    ends an argument that opens with a quote at its closing quote and passes what follows as
    another ('HEAD'"$f" is two), so such a word with text after its closing quote is None. A word
    with no variable, or no valid place in the source, is None too (fail closed)."""
    src = cmd.source or ''
    if cmd.shell not in DYN_SAFE_BARE or w.start is None or w.end is None or not 0 <= w.start < w.end <= len(src):
        return None
    raw, ps = src[w.start:w.end], cmd.shell == sh.PS
    if ps and (cm.PS_SPLAT_RE.match(raw) or any(c in raw for c in '“”„‘’‚‛')):
        return None
    # a doubled quote or a backtick inside returns None below, so the first matching quote after
    # the opening one is its close
    if ps and raw[0] in '\'"' and raw.find(raw[0], 1) != len(raw) - 1:
        return None
    var_re, bare = (DYN_PS_VAR_RE if ps else DYN_VAR_RE), DYN_SAFE_BARE[cmd.shell]
    head, seen, i, quote = [], False, 0, None
    while i < len(raw):
        c = raw[i]
        if quote == "'":
            if c == "'":
                if ps and raw[i + 1:i + 2] == "'":
                    return None                 # '' is a quote inside the word
                quote = None
            elif not seen:
                head.append(c)
            i += 1
            continue
        if c in '\\`':
            return None
        if c == '"':
            if quote == '"' and ps and raw[i + 1:i + 2] == '"':
                return None                     # "" is a quote inside the word
            quote = None if quote == '"' else '"'
            i += 1
            continue
        if c == "'" and quote is None:
            quote = "'"
            i += 1
            continue
        if c == '$':
            m = var_re.match(raw, i)
            if quote != '"' or not m:
                return None
            seen = True
            i = m.end()
            continue
        if quote is None and c not in bare:
            return None
        if not seen:
            head.append(c)
        i += 1
    if quote is not None or not seen:
        return None
    return ''.join(head)


def strict_dyn_read(gc):
    """True when every computed word of git read `gc` leaves it a read whatever it stands for
    (git_reads strict): a GIT_DYN_ANY_READS subcommand in bash or PowerShell, or a
    GIT_DYN_PINNED_READS one whose every computed word is one argument (dyn_head) that starts with
    literal text that keeps it from being an option (HEAD~$n, origin/$b, ./$f) or with
    --format= or --pretty=. Residual (inferred): PowerShell 5.1 splits an argument whose value
    holds a double quote (ps_native_unsure), which the text cannot show."""
    cmd = gc.inv.cmd if gc.inv is not None else None
    if cmd is None or cmd.shell not in DYN_SAFE_BARE:
        return False
    if gc.sub in GIT_DYN_ANY_READS:
        return True
    if gc.sub not in GIT_DYN_PINNED_READS:
        return False
    for w in (w for w in gc.rest if w.dyn):
        head = dyn_head(cmd, w)
        if not head or (head.startswith('-') and not head.startswith(GIT_DYN_VALUE_OPTS)):
            return False
    return True


def ps_splat(gc, w):
    """True when the word `w` of git `gc` is a PowerShell splat (@a) the shell spreads into words
    the text does not show; a quoted '@a' is plain text."""
    return (gc.inv is not None and gc.inv.cmd.shell == sh.PS and not w.dyn and cm.PS_SPLAT_RE.match(w.text)
            and (w.start is None or not cm.quoted(gc.inv.cmd, w)))


def archive_words(gc):
    """The Words after git archive `gc`'s subcommand as the shell hands them to git (bash drops
    every backslash it would, a leading one too: `\\-o` is -o; commands.passed)."""
    cmd = gc.inv.cmd
    return [sh.Word(cm.passed(cmd, w), w.dyn, w.start, w.end, w.synth) for w in gc.inv.args[len(gc.inv.args) -
                                                                                          len(gc.rest):]]


def archive_filter_set(gc):
    """True when the line may give git archive `gc` a program to run that the checkout's own config
    does not, or the guard cannot tell: a git the text cannot place (gc.blind: --git-dir,
    --work-tree, --config-env, a GIT_DIR-like variable), any GIT_* variable outside
    commands.GIT_SAFE_NAMES or an environment change the guard cannot read (commands.mark_git_env),
    a user config moved (HOME and the like, Command.home_env), PATH changed (Command.path_env), and
    before the subcommand --exec-path (it and PATH say where git finds a filter's bare program
    name, as Git for Windows' filter.lfs.process = git-lfs), a word the guard cannot read
    (computed, or one PowerShell or cmd may split: native_unsure) or a -c whose key, read as the
    shell hands it to git (commands.passed), is not on ARCHIVE_SAFE_KEY_RE."""
    cmd = gc.inv.cmd
    if gc.blind or cmd.git_env or cmd.home_env or cmd.path_env:
        return True
    args = gc.inv.args
    pre = args[:len(args) - len(gc.rest) - 1]
    k = 0
    while k < len(pre):
        if any(w.dyn or native_unsure(cmd, w) for w in pre[k:k + 2]):
            return True
        t = cm.passed(cmd, pre[k])
        name = t.partition('=')[0]
        if name.startswith('--exec-path') or len(name) > 2 and '--exec-path'.startswith(name):
            return True
        if t == '-c':
            if k + 1 >= len(pre):
                return True
            body, k = cm.passed(cmd, pre[k + 1]), k + 2
        elif t.startswith('--config-env'):
            return True
        elif t.startswith('-c'):
            body, k = t[2:], k + 1
        else:
            k += 1
            continue
        if not ARCHIVE_SAFE_KEY_RE.match(body.partition('=')[0].lower()):
            return True
    return False


def tail_word(w, value):
    """The Word for `value`, the end of the word `w` after an option attached to it (--output=x,
    -ox, -Cdir): its span covers only the value when `w` is spelt as its text (unquoted), so a
    rewrite (retarget) keeps the option; else the whole word's."""
    start = w.end - len(value) if w.end - w.start == len(w.text) else w.start
    return sh.Word(value, w.dyn, start, w.end)


def archive_long(name):
    """The archive long option `name` (no dashes) stands for, or None when it is unknown or an
    ambiguous prefix (--add)."""
    if name in ARCHIVE_LONGS:
        return name
    hits = [o for o in ARCHIVE_LONGS if name and o.startswith(name)]
    return hits[0] if len(hits) == 1 else None


def archive_opened(words):
    """[Word]: the files git archive's first pass (builtin/archive.c cmd_archive) opens. That pass
    knows only -o/--output, --remote and --exec, keeps every other option without taking a value
    for it and reads on past words that are not options, up to `--`; so in `--prefix -o f HEAD` the
    -o is its own and f is opened, though the later pass takes -o as the prefix. Every -o/-o<f>,
    --output[=]<f> and prefix of --output counts wherever it sits (a prefix fails closed), and no
    other option takes the word after it (fail closed for --remote and --exec)."""
    outs, k = [], 0
    while k < len(words):
        w, t = words[k], words[k].text
        if t == '--':
            break
        name, eq, value = t[2:].partition('=')
        if t.startswith('--') and name and 'output'.startswith(name):
            if eq:
                outs.append(tail_word(w, value))
            elif k + 1 < len(words):
                k += 1
                outs.append(words[k])
        elif t.startswith('-o'):
            if t[2:]:
                outs.append(tail_word(w, t[2:]))
            elif k + 1 < len(words):
                k += 1
                outs.append(words[k])
        k += 1
    return outs


def archive_outputs(gc):
    """(outputs, read) for git archive `gc`: the Words its -o/--output name, those git's first pass
    opens (archive_opened) and, failing closed, those the later pass would read (-vo <f>, an -o
    after `--prefix`'s value), and True when the archive only reads the repository it runs in: no
    --remote or --exec (they run a program), no option the guard does not know (a --no-* among
    them) and no word that is not literal (an unquoted $X may hold -o) or that PowerShell or cmd
    may split (native_unsure). An unknown option does not end the scan, so an -o after it is still
    an output. Every word is read as the shell hands it to git (archive_words)."""
    words = archive_words(gc)
    read = not any(native_unsure(gc.inv.cmd, w) for w in words)
    outs, k = [], 0
    while k < len(words):
        w, t = words[k], words[k].text
        if w.dyn:
            read = False
        if t == '--':
            read = read and not any(x.dyn for x in words[k + 1:])
            break
        if t.startswith('--'):
            name, eq, value = t[2:].partition('=')
            opt = archive_long(name)
            if opt is None or opt in ARCHIVE_RUNS:
                read = False
            if opt in ARCHIVE_VALUES and not eq:
                k += 1
                if k < len(words):
                    w, value = words[k], words[k].text
                    read = read and not w.dyn
                else:
                    w = None
            if opt == 'output' and w is not None:
                outs.append(tail_word(w, value) if eq else w)
        elif t.startswith('-') and len(t) > 1:
            for j, c in enumerate(t[1:], 1):
                if c == 'o':
                    if t[j + 1:]:
                        outs.append(tail_word(w, t[j + 1:]))
                    elif k + 1 < len(words):
                        k += 1
                        outs.append(words[k])
                        read = read and not words[k].dyn
                    break
                if c not in ARCHIVE_FLAGS:
                    read = False
                    break
        k += 1
    spans = {(o.text, o.start, o.end) for o in outs}
    outs += [o for o in archive_opened(words) if (o.text, o.start, o.end) not in spans]
    return outs, read


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
    cannot place) counts only what is plainly a read: no computed word but those strict_dyn_read
    allows, no PowerShell splat (ps_splat), no --output file, no grep -O (opens_pager), and
    branch, tag and config only with options it names in full (strict_read_opts)."""
    sub = gc.sub
    words = [w.text for w in gc.rest]
    if strict and (any(ps_splat(gc, w) for w in gc.rest) or any(writes_output_file(t) for t in words)):
        return False
    if strict and any(w.dyn for w in gc.rest) and not strict_dyn_read(gc):
        return False
    if strict and sub == 'grep' and any(opens_pager(t) for t in words):
        return False
    if sub == 'archive':
        # its -o file is a write: git_targets hands it to git_jail, which judges it first; where
        # the checkout cannot be placed, neither can a relative -o, so it is no plain read
        outs, read = archive_outputs(gc)
        return read and not (strict and outs) and not archive_filter_set(gc)
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
    """[Word]: the folders and files git `gc` writes besides the checkout it runs in (worktree add,
    clone, archive's -o/--output file: archive_outputs)."""
    args = gc.rest
    if gc.sub == 'archive':
        return archive_outputs(gc)[0]
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


LANDER_VERBS = ('fetch', 'merge')
# the tests lander_check runs, in order; a refusal names the first that failed
LANDER_NO_CONFIG = 'the main checkout\'s .kit.json does not load'
LANDER_NO_ROLE = ('the main checkout\'s .kit.json does not set roles.lander.edit_main_checkout to true, so no lander '
                  'may write it; the owner sets it (docs/guard.md "Go-live", step 3)')
LANDER_NOT_ALONE = ('git is not alone on the line (at most one literal cd, Set-Location or pushd to that checkout '
                    'may come before it, joined by && or ;)')
LANDER_WRAPPED = ('a wrapper, an environment assignment, a git option other than -C <checkout>, or a word that is '
                  'not literal comes with it')
LANDER_ARGS = 'its arguments are not exactly one of the landing forms'


def lander_forms(cfg):
    """The landing verbs a lander may run in a main checkout, as git argument lists."""
    remote, main = cfg.remote, cfg.main_branch
    return {'fetch': ([remote], [remote, main]), 'merge': (['--ff-only', '%s/%s' % (remote, main)],)}


def lander_alone(event, cmd, gc, home=None):
    """True when `cmd` (git) is the whole command line of `event`, or follows one literal cd,
    Set-Location or pushd into the checkout git runs in, joined by && or ;. The line is read again
    from the event, `~` as `home` (ctx.home); an unclean parse, a pipe, a group, a nested shell or
    any third command is not alone."""
    shell = SHELLS.get(event.shell) if event is not None and event.kind == 'shell' else None
    text, cwd = (event.command, event.workdir or event.cwd) if event is not None else (None, None)
    if shell is None or not isinstance(text, str) or cmd.depth or cmd.nested_in is not None:
        return False
    parsed = cm.read(text, shell, cwd, home=home)
    cmds = parsed.commands
    if not parsed.clean or not cmds or len(cmds) > 2 or cmds[-1].start != cmd.start or cmds[-1].end != cmd.end:
        return False
    if any(c.depth or c.groups or c.piped_in or c.piped_out or c.nested_in is not None for c in cmds):
        return False
    if len(cmds) == 1:
        return True
    is_cd, where = sh.cd_target(cmds[0], cwd, home)
    if not is_cd or where is None or cmds[1].prev_op not in ('&&', ';') or cmds[0].redirects:
        return False
    return jail.real(where) == jail.real(gc.dir)


def lander_check(event, ctx, cmd, inv, gc):
    """(allowed, why) for a lander subagent's `git fetch` or `git merge` in a main checkout:
    (True, None) for one of the landing forms (lander_forms: `fetch <remote>`, `fetch <remote>
    <main>`, `merge --ff-only <remote>/<main>`, remote and main from that checkout's .kit.json repo
    section), alone on its line (lander_alone), with at most `-C <main checkout>` before the verb
    (git, git.exe or PowerShell's `& git`), in a checkout whose .kit.json sets
    roles.lander.edit_main_checkout: the one main-checkout write the jail leaves a lander that runs
    from a worktree. (False, <the first test that failed>) when one fails. (False, None) for any
    other session, verb or place: the other rules judge it as before."""
    if not subagent(event) or event.agent_role != LANDER:
        return False, None
    if gc.unknown or gc.sub not in LANDER_VERBS or not gc.dir or ctx.place(gc.dir).kind != repo.MAIN:
        return False, None
    # the repository's own settings first: while they refuse every landing form, a refusal that
    # named the command would offer an Instead that is refused too
    cfg = ctx.config(gc.dir)
    if cfg is None:
        return False, LANDER_NO_CONFIG
    table = kconfig.roles(cfg)
    if kconfig.role_name(event, table) != LANDER or not table[LANDER].get('edit_main_checkout'):
        return False, LANDER_NO_ROLE
    if not lander_alone(event, cmd, gc, getattr(ctx, 'home', None)):
        return False, LANDER_NOT_ALONE
    pre = inv.args[:len(inv.args) - len(gc.rest) - 1]
    if inv.via is not None or inv.dyn or [w.text for w in cmd.words[:inv.index]] not in ([], ['&']) or \
            [w.text for w in pre[:1]] not in ([], ['-C']) or len(pre) not in (0, 2) or \
            any(w.dyn for w in pre) or any(w.dyn for w in gc.rest):
        return False, LANDER_WRAPPED
    if [w.text for w in gc.rest] not in lander_forms(cfg)[gc.sub]:
        return False, LANDER_ARGS
    return True, None


def lander_refusal(ctx, cmd, gc, rule, role, why):
    """The refusal for a lander's landing step that failed lander_check's test `why`: it names the
    test, the forms a lander may run, and the command to use (the config read when the config is
    the cause)."""
    top = ctx.place(gc.dir).top or gc.dir
    kit = os.path.join(top, KIT_JSON)
    cfg = ctx.config(gc.dir)
    if cfg is None or why == LANDER_NO_ROLE:
        forms = 'the landing forms'
        instead, end = 'python -m json.tool %s' % quote(kit), 'Ask the coordinator to take it to the owner. To read the file:'
    else:
        remote, main = cfg.remote, cfg.main_branch
        forms = 'git fetch %s, git fetch %s %s or git merge --ff-only %s/%s' % (remote, remote, main, remote, main)
        want = '%s %s' % (remote, main) if gc.sub == 'fetch' else '--ff-only %s/%s' % (remote, main)
        instead, end = 'git -C %s %s %s' % (quote(top), gc.sub, want), 'Run it alone, as:'
    return refuse(rule, role, '`%s` is refused as a lander\'s landing step in the main checkout %s: %s. A lander may '
                  'run only %s there, each alone on its line. %s' % (bare(cmd), fwd(top), why, forms, end), instead)


def git_jail_unknown(ctx, cmd, gc, cwd, role):
    """An agent session's git that writes a checkout the text cannot place (GIT_DIR and the rest
    of commands.GIT_ENV_NAMES set, an environment change the guard cannot read
    (commands.git_env_unsafe), --git-dir, --work-tree, -c core.worktree, --config-env, a
    computed -C; a -C after any of them does not place it), by any role: it may be a main
    checkout, and the main-checkout git rules judge only history writes there, so a symbolic-ref
    or branch -m that re-points main's HEAD would pass them. Only a plain read passes
    (git_reads strict; a computed word only as strict_dyn_read allows). The hint names what hides the place (git_unplaced): drop it, or name a
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
    add or clone to a folder outside them. Its repository's other worktrees (jail.sibling) are
    not elsewhere."""
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
        target = sh.resolve_path(w.text, gc.dir, home=getattr(ctx, 'home', None))
        if not target:
            continue
        rs = rs or jail_roots(event, ctx, cwd)
        verdict, root = jail.judge(target, rs)
        if verdict is not None and not (verdict == jail.OUTSIDE and jail.sibling(ctx, target, cwd)):
            what = '`%s` makes %s' % (bare(cmd), w.text)
            return jail_refusal(role, rs, verdict, root, what, target,
                                rewrite=lambda path, w=w: retarget(cmd, w, path))
    if git_reads(gc):
        return None
    landed, why = lander_check(event, ctx, cmd, inv, gc)
    if landed:
        return None
    d = pm_data_unknown(ctx, gc.dir, cwd, role, '`%s`' % bare(cmd))
    if d:
        return d
    rs = rs or jail_roots(event, ctx, cwd)
    verdict, root = jail.judge(gc.dir, rs)
    if verdict is None or (verdict == jail.OUTSIDE and jail.sibling(ctx, gc.dir, cwd)):
        return None
    if why is not None:
        return lander_refusal(ctx, cmd, gc, 'write-jail', role, why)
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


def ref_delete_hint(ctx, gc, top, cwd, short, longs, pos):
    """(sentence, command) for `git update-ref -d <ref>`, or None. Every worktree of a repository
    shares its refs, so neither a worktree nor the lander carries a ref delete to main: a branch
    goes with git branch -d, which refuses an unmerged one; any other ref is shown, and the PM
    deletes it; a guarded main is never offered to branch -d. Both run in the session's own
    worktree of this main checkout when it has one. None too for a ref git_shared_ref lets any
    session delete (scratch_ref): only where it runs is wrong, and git_in_worktree says so."""
    if gc.sub != 'update-ref' or 'd' not in short or 'stdin' in longs or gc.blind:
        return None
    ref = pos[0] if pos else None
    name = ref.text if ref is not None and not ref.dyn else None
    if name and scratch_ref(name):
        return None
    at = quote(own_worktree(ctx, cwd, top) or top)
    shared = 'Every worktree shares this repository\'s refs, so no worktree or landing carries a ref delete to main.'
    if name and name.startswith('refs/heads/') and len(name) > len('refs/heads/'):
        branch = name[len('refs/heads/'):]
        if branch.lower() in main_names(ctx, top):
            return ('%s %s is the main branch: only a landing moves it, and no session deletes it. To show it:' %
                    (shared, name), 'git -C %s show-ref %s' % (at, name))
        return ('%s Delete a branch with git branch -d, which refuses an unmerged one; unmerged work goes to '
                'the PM, never deleted:' % shared, 'git -C %s branch -d %s' % (at, branch))
    return ('%s A ref other than a branch is the PM\'s to delete; to show it:' % shared,
            'git -C %s show-ref %s' % (at, name or '<ref>'))


# git_shared_ref: the update-ref options it reads (GIT_LONGS['update-ref'] spells the long ones),
# the refs any agent session may write (scratch_ref), and the names git reads as one loose ref file
UPDATE_REF_SHORTS = set('mdz')
PER_WORKTREE_REFS = ('refs/bisect/', 'refs/worktree/', 'refs/rewritten/')
PSEUDOREF_RE = re.compile(r'^[A-Z][A-Z0-9_]*_HEAD$')
REF_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]*$')
REF_CHAIN_MAX = 5           # symbolic refs followed before the guard stops reading
SCRATCH_WALK_MAX = 4096     # entries of a temp repository's git directory read for a link, at most
REPARSE_POINT = 0x400       # FILE_ATTRIBUTE_REPARSE_POINT: a symlink, a junction or another link


def ref_name_ok(name):
    """True for a ref name the guard reads as one file under the repository's git directory: plain
    characters only, no `..`, empty or dot-led component, and no component ending in .lock or a dot."""
    if not name or not REF_NAME_RE.match(name):
        return False
    parts = name.split('/')
    return all(p and not p.startswith('.') and not p.endswith('.') and not p.endswith('.lock') for p in parts)


def per_worktree_ref(name):
    """True for a ref git keeps in each worktree's own git directory: HEAD, a <NAME>_HEAD
    pseudoref, refs/bisect/, refs/worktree/ and refs/rewritten/."""
    return name == 'HEAD' or bool(PSEUDOREF_RE.match(name)) or name.startswith(PER_WORKTREE_REFS)


def scratch_ref(name):
    """True for a ref any agent session may write or delete from any checkout: refs/tmp/<name>
    and the per-worktree refs but HEAD."""
    return ref_name_ok(name) and name != 'HEAD' and (name.startswith('refs/tmp/') or per_worktree_ref(name))


def main_names(ctx, where):
    """The guarded mains of the repository at `where`, lower-cased, main and master always among
    them (a missing or unreadable .kit.json guards those two)."""
    names = {'main', 'master'}
    cfg = ctx.config(where) if where and os.path.isabs(where) else None
    if cfg is not None:
        names.update(str(b).lower() for b in _guarded_mains(cfg) if b)
    return names


def shared_ref_held(event, ctx):
    """True for a session git_shared_ref holds: an agent session (agent_session) but the PM's."""
    return agent_session(event, ctx) and str(session_mark(event, ctx) or '').lower() != 'pm'


def _ref_file(place, name):
    """(path of the loose file of `name`, None) under the git directory that holds it, or
    (None, why) when a link inside it carries the path elsewhere."""
    base = place.git_dir if per_worktree_ref(name) else place.common
    if not base:
        return None, 'git gave no git directory for %s' % name
    path = os.path.join(base, *name.split('/'))
    want = paths.norm(os.path.join(jail.real(base), *name.split('/')))
    if jail.real(path) != want:
        return None, 'a link in the git directory carries %s to %s' % (name, fwd(jail.real(path)))
    return path, None


def _in_temp(ctx, path):
    real = jail.real(path)
    return any(jail.inside(real, jail.real(r)) for r in jail.temp_roots(ctx))


def _scratch_repo(ctx, place):
    """True when `place` is a scratch repository: its git directories lie in a temp root
    (jail.temp_roots) and no link inside them may carry a ref write out. Every entry but what
    objects/ holds is read, at most SCRATCH_WALK_MAX; any symlink, junction or other reparse
    point, an entry that does not read, or a longer walk answers False, and the rule judges the
    repository as any other. A hard link is not seen: git writes a ref file and packed-refs to a
    lock file it renames over the old one, so a write does not go through a hard link."""
    if place is None or not place.common:
        return False
    roots = [place.common]
    if place.git_dir and not jail.inside(jail.real(place.git_dir), jail.real(place.common)):
        roots.append(place.git_dir)
    if not all(_in_temp(ctx, r) for r in roots):
        return False
    seen, stack = 0, list(roots)
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
            for e in entries:
                seen += 1
                if seen > SCRATCH_WALK_MAX or e.is_symlink() or \
                        getattr(e.stat(follow_symlinks=False), 'st_file_attributes', 0) & REPARSE_POINT:
                    return False
                if e.is_dir(follow_symlinks=False) and not (d in roots and e.name.lower() == 'objects'):
                    stack.append(e.path)
        except OSError:
            return False
    return True


def _read_ref(path):
    """(text of the loose ref file, None); ('', None) when there is none; (None, why) when it
    cannot be read."""
    try:
        with open(path, 'rb') as f:
            return f.read(4096).decode('utf-8', 'replace').strip(), None
    except FileNotFoundError:
        return '', None
    except OSError as e:
        return None, 'its file cannot be read (%s)' % e


def _ref_chain(place, name, deref):
    """([name, the refs it points to...], None), the refs a write of `name` reaches, or (None,
    why). Without --no-deref git writes where a symbolic ref points, so each is read in turn."""
    chain = [name]
    for _ in range(REF_CHAIN_MAX + 1):
        path, why = _ref_file(place, chain[-1])
        if why:
            return None, why
        if not deref:
            return chain, None
        text, why = _read_ref(path)
        if text is None:
            return None, '%s: %s' % (chain[-1], why)
        if not text:
            if chain[-1] == 'HEAD':
                return None, 'the worktree\'s HEAD file %s is missing' % fwd(path)
            return chain, None
        if not text.startswith('ref:'):
            return chain, None
        target = text[4:].strip()
        if not ref_name_ok(target):
            return None, '%s points at %r, a name the guard does not read' % (chain[-1], target)
        chain.append(target)
    return None, '%s is a chain of more than %d symbolic refs' % (name, REF_CHAIN_MAX)


def _own_branch(ctx, place, at):
    """The branch checked out in the linked worktree `place` (refs/heads/<name>), or None: a
    detached or unreadable HEAD, a guarded main, the PM data worktree or one the guard cannot tell."""
    if place.kind != repo.LINKED or not place.git_dir or ctx.is_pm_data(at) or ctx.pm_data_error(at):
        return None
    path, why = _ref_file(place, 'HEAD')
    text, why = _read_ref(path) if path else (None, why)
    if not text or not text.startswith('ref:'):
        return None
    own = text[4:].strip()
    if not own.startswith('refs/heads/') or not ref_name_ok(own):
        return None
    if own[len('refs/heads/'):].lower() in main_names(ctx, at):
        return None
    return own


def _ref_ok(name, delete, linked, own):
    if scratch_ref(name):
        return True
    if delete or not linked:
        return False
    return name == 'HEAD' or (own is not None and name == own)


def _update_ref_unread(gc, short, longs, pos):
    """Why the guard cannot tell which ref `git update-ref` writes, or None."""
    if gc.inv is not None and gc.inv.via == 'xargs':
        return 'xargs adds words the text does not show'
    if 'stdin' in longs:
        return 'it reads the refs to write from its input'
    unknown = sorted(longs - set(GIT_LONGS['update-ref']))
    if unknown:
        return 'it gives an option the guard does not read (--%s)' % unknown[0]
    odd = sorted(short - UPDATE_REF_SHORTS)
    if odd:
        return 'it gives an option the guard does not read (-%s)' % odd[0]
    if not pos:
        return 'it names no ref'
    for w in gc.rest:
        if w.dyn or w.alt is not None or w.synth:
            return 'a word before the ref holds something only the shell knows (%s)' % w.text
        if w is pos[0]:
            break
    if not ref_name_ok(pos[0].text):
        return '%r is not a ref name the guard reads' % pos[0].text
    return None


def git_shared_ref(event, ctx, cmd, gc, cwd, role):
    """`git update-ref` by a held session (shared_ref_held), from any checkout: every worktree of a
    repository shares its refs, so a worktree reaches main as surely as the main checkout. It may
    write scratch_ref names, and in a linked worktree its HEAD and the branch checked out there
    (never a guarded main); it deletes only scratch_ref names. A symbolic ref is judged by every
    ref it points to (unless --no-deref). A scratch repository (_scratch_repo: in a temp root, no
    link out of its git directory) is not judged. Neither the PM data worktree nor, while the
    guard cannot tell it (ctx.pm_data_error), any linked worktree gets the HEAD allowance."""
    if not shared_ref_held(event, ctx):
        return None
    text = bare(cmd)
    short, longs, pos = _opts('update-ref', gc.rest)
    delete = 'd' in short
    unread = _update_ref_unread(gc, short, longs, pos)
    after = False
    for w in gc.rest:
        if after and (w.dyn or w.alt is not None or w.synth):
            delete = True                       # a word only the shell knows may be -d
        after = after or (bool(pos) and w is pos[0])
    if not unread and (gc.blind or gc.unknown or not gc.dir):
        unread = 'the repository it runs in is unknown (%s)' % (gc.blind or 'a directory the text does not name')
    place = ctx.place(gc.dir) if gc.dir and not gc.unknown and not gc.blind else None
    if _scratch_repo(ctx, place):
        return None
    if not unread and place.kind not in (repo.MAIN, repo.LINKED):
        unread = 'git cannot place %s (%s)' % (fwd(gc.dir), place.detail or place.kind)
    if not unread and place.common and os.path.isdir(os.path.join(place.common, 'reftable')):
        unread = 'the repository keeps its refs in a reftable, which the guard does not read'
    top = (ctx.main_top(gc.dir) if gc.dir and not gc.unknown else None) or (place.top if place else None)
    at = own_worktree(ctx, cwd, top) if top else own_worktree(ctx, cwd)
    at = at or top or (ctx.main_top(cwd) if cwd else None) or cwd
    shared = 'every worktree of this repository shares its refs, so a change made from any of them is made for all'
    allowed = 'An agent session writes refs/tmp/<name> (its scratch space), the per-worktree refs (refs/bisect/, ' \
              'refs/worktree/, refs/rewritten/, <NAME>_HEAD) and, in its own worktree, HEAD and the branch ' \
              'checked out there; any other ref is the PM\'s.'
    if unread:
        hint = ref_delete_hint(ctx, gc, top or at, cwd, short, longs, pos) if 'd' in short else None
        if hint:
            return refuse('git-shared-ref', role, '`%s`: %s, and %s. %s' % (text, unread, shared, hint[0]), hint[1])
        return refuse('git-shared-ref', role, '`%s`: the guard cannot tell which ref it writes: %s; %s. Name one '
                      'ref literally. %s To keep a commit in your scratch space:' % (text, unread, shared, allowed),
                      'git -C %s update-ref refs/tmp/<name> <new-oid>' % quote(at))
    name = pos[0].text
    # the PM data worktree answers as MAIN; while the guard cannot tell it, any linked worktree may be it
    linked = place.kind == repo.LINKED and not ctx.pm_data_error(gc.dir)
    own = _own_branch(ctx, place, gc.dir)
    chain, why = _ref_chain(place, name, 'no-deref' not in longs)
    if chain is not None:
        bad = [r for r in chain if not _ref_ok(r, delete, linked, own)]
        if not bad:
            return None
        why = '%s is not one it may %s' % (bad[0] if bad[0] == name else '%s, where %s points,' % (bad[0], name),
                                           'delete' if delete else 'write')
    hint = ref_delete_hint(ctx, gc, top or at, cwd, short, longs, pos) if 'd' in short else None
    if hint:
        return refuse('git-shared-ref', role, '`%s` deletes %s: %s. %s' % (text, name, why, hint[0]), hint[1])
    return refuse('git-shared-ref', role, '`%s` %s %s: %s; %s. %s To show it:' % (
        text, 'deletes' if delete else 'moves', name, why, shared, allowed), 'git -C %s show-ref %s' % (quote(at), name))


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
        target = sh.resolve_path(pos[0].text, gc.dir, home=getattr(ctx, 'home', None))
    main_top =(ctx.main_top(gc.dir) if gc.dir else None) or gc.dir
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
        how = ('Merge it first; unmerged work goes to the PM, who decides (python %s -C %s %s --abandon "<why>" '
               'only on the PM\'s word).' % (kit_tool(ctx, 'worktree_remove'),
                                             quote(main_top) if main_top else '"<main checkout>"',
                                             os.path.basename(target)))
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


def pmdata_possible(ctx, path):
    """pmdata_recorded, except that a directory the text cannot tell (None) counts only when the
    machine config names pm_data (or cannot be read): with none named, an unknown directory is
    no ground to deny a command that is not a push."""
    return pmdata_recorded(ctx, path) if path else ctx.pm_data_named()


def call_subexpr_pmdata(event, ctx, cmd, inv, cwd, role):
    """PowerShell's call operator on a ( ) subexpression (`& (Get-Command git) push`,
    commands.call_subexpr), in every session but the owner's own: the program is what the ( )
    computes, so the rules that need a literal program (heavy, the write jail, the git rules)
    cannot read it. In a repository with a PM data record it is denied as git-push-pmdata (it may
    be git push --no-verify); elsewhere an agent session (agent_session) is denied as the
    fallback denies an unreadable line, a subagent `unreadable` and another jailed session
    `write-jail` (it may be git writing a checkout the jail cannot place). Only the owner and a
    session the jail does not hold (`plain`) read the line as any other."""
    if not cm.call_subexpr(inv) or is_owner_session(event, ctx):
        return None
    instead = ('& <program> %s' % ' '.join(w.text for w in inv.args)).rstrip()
    where = cmd.cwd
    if pmdata_possible(ctx, where):
        return refuse('git-push-pmdata', role, '`& %s` runs the program a PowerShell subexpression computes, and this '
                      'repository has %s (or the guard cannot tell): it may be git push --no-verify, which skips the '
                      'pre-push hook that keeps PM data off public remotes. Name the program literally:'
                      % (inv.path, pmdata.FILE_NAME), instead)
    if not agent_session(event, ctx):
        return None
    rule = 'unreadable' if role == 'subagent' else 'write-jail'
    return refuse(rule, role, '`& %s` runs the program a PowerShell subexpression computes, which the guard cannot '
                  'name: it may be git writing a checkout the write jail cannot place, or a heavy program run '
                  'without the machine lock. Name the program literally:' % inv.path, instead)


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
    the same config, takes it for the private one."""
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
    "Failure modes"). A check git cannot answer denies too (fail closed), and so does a machine
    record the guard cannot read: while the data worktree folder is deleted, every such push is
    denied until pm_data.py init --adopt re-adds it."""
    if is_owner_session(event, ctx) or not where:
        return None
    if ctx.place(where).kind not in (repo.MAIN, repo.LINKED):
        return None
    root, why = ctx.pm_root()
    if root is None and why is None:
        return None                             # the machine config names no pm_data
    if root is None:
        path = ctx.pm_data_path()
        return refuse('git-push-pmdata', role, '%s: the repository %s has no %s of its own, and the guard cannot read '
                      'the machine\'s PM data record to tell whether it holds PM data (%s), so it fails closed. When '
                      'the data worktree folder was deleted (docs/pm-data-branch.md "Failure modes"), pm_data.py init '
                      '--adopt re-adds it:' % (what, fwd(where), pmdata.FILE_NAME, why),
                      'python %s init --adopt --path %s' % (kit_tool(ctx, 'pm_data'),
                                                            quote(path) if path else '"<pm_data.path>"'))
    found = None
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


# the characters alias words are split by: git 2.55 splits a quoted alias value at a space, tab or
# CR (observed: "rev-parse<c>--git-dir" runs for those, and is "not a git command" for VT and FF).
# VT and FF are split here too: a word holding one is no git command or option git accepts, so
# splitting it can only make the guard judge more, never less
ALIAS_BLANKS = ' \t\n\v\f\r'


def split_alias(text):
    """The words of an alias value as git splits them (split_cmdline): ALIAS_BLANKS separate,
    '...' and "..." group, a backslash outside '...' takes the next character as it is. None for
    an unclosed quote or a trailing backslash."""
    words, cur, have, q, i = [], [], False, None, 0
    while i < len(text):
        c = text[i]
        if q is None and c in ALIAS_BLANKS:
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
    """A git subcommand that is not a git builtin, in a repository with a PM data record, or in
    any repository when the machine config names pm_data (a copy of the data has no record: its
    push is judged by data_copy), in every session but the owner's own: git runs the alias its
    config gives the name (alias.<name>), so the expansion is judged as the command it runs (a
    push, a hook-skipping transport, a remote rewrite, a core.hooksPath change, another alias).
    The guard reads the alias with its own environment, so a line that sets a HOME_ENV_NAMES
    variable, or a call with -c or --config-env (include.path may add any alias), is not
    judged by that read. Those, a shell alias (!...), one that starts with an option or cannot be
    split, a chain deeper than ALIAS_DEPTH, a config git cannot read, and a directory the text
    cannot tell are denied (git-push-pmdata): the guard cannot judge them. In a repository
    without a record, those count only when data_copy would deny its push. A directory the text
    cannot tell counts only when the machine config names pm_data or the call's own directory
    has a record. A name with no alias is left to git (its own error, or a git-<name> program)."""
    if gc.sub_dyn or not gc.sub or gc.sub in GIT_BUILTINS or hookless_sub(gc.sub) or is_owner_session(event, ctx):
        return None
    where = None if gc.unknown else gc.dir
    if where is None:
        if not _unknown_dir_counts(ctx, gc):
            return None
    elif not (pmdata_recorded(ctx, where) or ctx.pm_data_named()):
        return None
    moved = _alias_config_moved(gc)
    if moved:
        return _alias_unjudged(event, ctx, gc, role, where, [], moved)
    return _alias_judged(event, ctx, gc, role, where, [])


def _unknown_dir_counts(ctx, gc):
    """For a git call whose directory the text cannot tell: True when the machine config names
    pm_data (or cannot be read), or the directory the command itself runs in has a PM data record
    (pmdata_recorded). Otherwise an unknown directory is no ground to deny."""
    here = gc.inv.cmd.cwd if gc.inv is not None else None
    return bool(ctx.pm_data_named() or (here and pmdata_recorded(ctx, here)))


def _alias_config_moved(gc):
    """Why git may read alias.<gc.sub> from config the guard's own read does not see, or None."""
    home = gc.inv.cmd.home_env if gc.inv is not None else False
    if home:
        return 'the line sets %s, so git reads the user config of another folder, which the guard does not' % home
    configs = git_prefix_configs(gc) if gc.inv is not None else []
    if configs:
        return ('the call passes -c or --config-env (%s), which can add config (include.path) the guard does not '
                'read' % ', '.join(k for k, _v, _f, _l in configs))
    return None


def _alias_refusal(gc, role, where, chain, why):
    shown = ' -> '.join(chain) or 'alias.%s' % gc.sub
    return refuse('git-push-pmdata', role, '`git %s` in a repository with a PM data record runs a git alias (%s) the '
                  'guard cannot judge: %s. Write the git command itself:' % (gc.sub, shown, why),
                  'git -C %s <subcommand> <arguments>' % (quote(where) if where else '"<checkout>"'))


def _alias_unjudged(event, ctx, gc, role, where, chain, why):
    """An alias the guard cannot judge: denied in a repository with a PM data record (or one the
    text cannot tell); in one without, only when data_copy would deny its push."""
    if where is not None and not pmdata_recorded(ctx, where):
        return data_copy(event, ctx, where, role, '`git %s` runs a git alias the guard cannot judge (%s)' % (gc.sub, why))
    return _alias_refusal(gc, role, where, chain, why)


def _alias_judged(event, ctx, gc, role, where, chain):
    if where is None:
        return _alias_unjudged(event, ctx, gc, role, where, chain,
                               'the guard cannot tell the repository whose config holds it')
    val, why = ctx.gitread.alias(where, gc.sub)
    if why:
        return _alias_unjudged(event, ctx, gc, role, where, chain, 'its config cannot be read (%s)' % why)
    if val is None:
        return None                             # no alias: git's own error, or a git-<name> program
    chain = chain + ['alias.%s=%s' % (gc.sub, val)]
    if len(chain) > ALIAS_DEPTH:
        return _alias_unjudged(event, ctx, gc, role, where, chain[:2], 'the aliases nest deeper than %d' % ALIAS_DEPTH)
    if val.startswith('!'):
        return _alias_unjudged(event, ctx, gc, role, where, chain, 'it runs a shell command')
    words = split_alias(val)
    if not words or words[0].startswith('-'):
        return _alias_unjudged(event, ctx, gc, role, where, chain, 'it starts with an option or cannot be split'
                               if words else 'it cannot be split')
    rest = [sh.Word(t, False, None, None) for t in words[1:]] + list(gc.rest)
    sub = cm.GitCall(gc.dir, words[0], rest, gc.inv, gc.unknown, None, False, gc.blind)
    if hookless_sub(sub.sub):
        d = hookless_refusal(event, ctx, sub.sub, where, sub.argv, role)
    elif sub.sub == 'push':
        d = git_push(event, ctx, sub, role)
    elif sub.sub in ('remote', 'config'):
        d = git_remote_rewrite(event, ctx, sub, role)
        if d is None and sub.sub == 'config':
            d = hooks_config_write(ctx, sub, role, 'git config %s' % ' '.join(sub.argv))
    elif sub.sub not in GIT_BUILTINS:
        d = _alias_judged(event, ctx, sub, role, where, chain)
    else:
        d = None
    if d is None or len(chain) > 1:
        return d
    return reworded(d, '`git %s` runs the git alias %s. %s' % (gc.sub, chain[0], d.reason))


# ---------------------------------------------------------------- remotes repointed

REMOTE_REWRITE_VERBS = ('set-url', 'add', 'rename', 'remove', 'rm')


def remote_rewrite_key(name):
    """True for a config key or section that can point a remote somewhere else: remote.* (url,
    pushurl, ...), url.* (insteadOf, pushInsteadOf), include.* and includeIf.* (they read
    another file), case ignored as git ignores it in section and key names."""
    n = name.lower()
    return n in ('remote', 'url', 'include', 'includeif') or n.startswith(('remote.', 'url.', 'include.', 'includeif.'))


def config_rewrite(gc):
    """(what, files, scope_all) when a `git config` call writes a key or section
    remote_rewrite_key names, opens a config in an editor, or has a positional word computed at
    run time (git config $K, PowerShell's git config @a: it may be any write), else None.
    Options are read as git reads them (config_args: --glo, --fil <path>, -f<path>, --ed).
    `files`: the Words of its -f/--file paths; `scope_all`: --global or --system."""
    names, sure, pos, files = config_args(gc)
    scope_all = bool({'global', 'system'} & names)
    if 'edit' in names or (pos and not pos[0].dyn and pos[0].text == 'edit'):
        return 'opens a config file in an editor, where any remote can be changed', files, scope_all
    computed = next((w for w in pos if w.dyn), None)
    if computed is not None:
        return 'has a word computed at run time (%s), which may write any key' % computed.text, files, scope_all
    if sure & CONFIG_READS or not pos:
        return None
    verb = pos[0].text
    if verb in ('get', 'list'):
        return None
    if verb in ('set', 'unset'):
        keys = pos[1:2]
    elif verb in ('rename-section', 'remove-section'):
        keys = pos[1:3]
    elif names & CONFIG_SECTIONS:
        keys = pos[:2]
    elif names & CONFIG_WRITES or len(pos) > 1:
        keys = pos[:1]
    else:
        return None
    for w in keys:
        if remote_rewrite_key(w.text):
            return 'writes %s' % w.text, files, scope_all
    return None


def _config_file_recorded(ctx, path, where):
    """True when a config file git reads belongs to a repository with a PM data record (its
    folder, or the git common dir two folders up from a worktree's config, holds the record), or
    is a user or system config (scope_all, judged by machine_pm_data); a path that cannot be
    resolved counts (fail closed)."""
    p = sh.resolve_path(path, where, home=getattr(ctx, 'home', None)) if path else None
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
    """The git remote verb that repoints a remote (REMOTE_REWRITE_VERBS), a computed one too (a
    PowerShell splat @a among them), or None."""
    ps = gc.inv is not None and gc.inv.cmd.shell == sh.PS
    for w in gc.rest:
        if w.dyn or (ps and w.start is not None and cm.PS_SPLAT_RE.match(w.text) and not cm.quoted(gc.inv.cmd, w)):
            return w.text
        if w.text.startswith('-'):
            continue
        return w.text if w.text in REMOTE_REWRITE_VERBS else None
    return None


def git_remote_rewrite(event, ctx, gc, role):
    """git-remote-rewrite: `git remote set-url|add|rename|remove`, a `git config` write to
    remote.*, url.*, include.* or includeIf.* (any spelling: --add, --unset, set, unset, the
    section verbs) and `git config --edit`, in a repository with a PM data record (or one git
    cannot place), in every session but the owner's own. A directory the text cannot tell counts
    as git_alias_config counts it (_unknown_dir_counts: the machine config names pm_data, or the
    command's own directory has a record). A --global or --system write, or one to
    a user or system config file, counts when the machine config names pm_data; a --file git
    does not read is left alone. The pre-push leak stage tells the private remote by its
    configured URLs, so a remote pointed elsewhere would make a public one look private
    (docs/pm-data-branch.md "Pre-push guard")."""
    if gc.sub not in ('remote', 'config') or is_owner_session(event, ctx):
        return None
    where = None if gc.unknown else gc.dir
    def here_recorded():
        return pmdata_recorded(ctx, where) if where is not None else _unknown_dir_counts(ctx, gc)
    if gc.sub == 'remote':
        verb = _remote_verb(gc)
        if verb is None:
            return None
        what = 'repoints a remote (git remote %s)' % verb
        recorded = here_recorded()
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
            recorded = here_recorded() or (scope_all and machine_pm_data(ctx))
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
            return None                         # no checkout: no main branch to judge
        top = ctx.main_top(gc.dir) or gc.dir
        return refuse('git-push-main', role, '`git push` in a repository whose .kit.json does not load (%s). Fix it '
                      'so it loads (the message names the key; docs/kit-json.md lists them), then push. To see '
                      'the error:' % err, 'python -m json.tool %s' % quote(os.path.join(top, '.kit.json')))
    # A delete or a forced push of main is refused in every repository, with or without a
    # .kit.json (docs/kit-json.md "With no file"); push_block adds the gate-OK record and
    # --no-verify checks. The PM data branch (DataBranch) is judged by its own push_block only.
    gated = cfg.push_block
    if not gated and isinstance(cfg, DataBranch):
        return None
    main = cfg.main_branch
    mains = _guarded_mains(cfg)
    short, longs, pos = _opts('push', gc.rest)
    delete = 'd' in short or 'delete' in longs
    force = 'f' in short or bool({'force', 'force-with-lease', 'force-if-includes', 'mirror'} & longs)
    no_verify = 'no-verify' in longs
    dyn = [w for w in pos if w.dyn]
    # A computed refspec may be `+<src>:main` or `:main`, so with no push_block too it is refused;
    # a computed remote alone is, only with a flag that forces, deletes or skips the hook.
    if dyn and (gated or delete or force or no_verify or any(w.dyn for w in pos[1:])):
        remote = pos[0].text if pos and not pos[0].dyn else cfg.remote
        return refuse('git-push-main', role, '`git push %s` has a refspec that is not literal, so the guard cannot '
                      'tell whether it %s %s. Name the refs literally:'
                      % (rest, 'pushes' if gated else 'rewrites or deletes', main),
                      'git -C %s push %s <branch>' % (quote(gc.dir), remote))
    every = bool({'all', 'mirror', 'branches'} & longs)
    remote = pos[0].text if pos else cfg.remote
    specs = [w.text for w in pos[1:]]
    branch, _why = ctx.git.current_branch(gc.dir)
    targets = []
    if every:
        targets.extend(('refs/heads/' + m, m, False) for m in mains)
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
        if dst not in mains:
            continue
        main = dst
        what = '`git push %s` to %s/%s' % (' '.join(w.text for w in gc.rest), remote, main)
        if not src:
            return refuse('git-push-main', role, '%s deletes %s; main is never deleted. To see where it is:' %
                          (what, main), 'git -C %s ls-remote %s %s' % (quote(gc.dir), remote, main))
        if not gated:
            if force or plus:
                return refuse('git-push-main', role, '%s rewrites %s; main is never force-pushed. Merge %s/%s into '
                              'the branch, then push it without force:' % (what, main, remote, main),
                              'git -C %s push %s %s' % (quote(gc.dir), remote, ' '.join(
                                  s.lstrip('+') for s in specs) or main))
            continue
        land = landing_command(ctx, gc, cfg, remote, branch)
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


def _guarded_mains(cfg):
    """The branches git_push holds as main (Config.guarded_mains); the PM data worktree's, and a
    test's stand-in config without the method, just main_branch."""
    if isinstance(cfg, DataBranch) or not hasattr(cfg, 'guarded_mains'):
        return [cfg.main_branch]
    return cfg.guarded_mains()


def landing_command(ctx, gc, cfg, remote, branch):
    # No --expect-main: a landing merges onto the main it finds at its turn (docs/gate.md "Landing").
    tip, _ = ctx.git.resolve(branch, gc.dir) if branch else (None, None)
    main_top = (ctx.main_top(gc.dir) if gc.dir else None) or gc.dir
    return 'python %s -C %s %s --expect-tip %s' % (
        kit_tool(ctx, 'land'), quote(main_top) if main_top else '"<main checkout>"',
        branch if branch and branch != cfg.main_branch else '<branch>',
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
        bounded = '$deadline = (Get-Date).AddMinutes(%d); %s if ((Get-Date) -gt $deadline) { break }; %s' % (
            BUSY_MINUTES, loop[:cut], loop[cut:].lstrip())
        return loop, _in_outer(head, src[:head.start] + bounded + src[end:], bounded)
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
    do = _bash_loop_do(cmds, first, last)
    if do is None:
        return loop, "timeout %d bash -c '%s'%s" % (BUSY_MINUTES * 60, loop.replace("'", "'\\''"), rest.rstrip())
    cut = do.end - start
    bounded = 'SECONDS=0; %s [ $SECONDS -ge %d ] && break;%s%s' % (
        loop[:cut], BUSY_MINUTES * 60, loop[cut:] if loop[cut:cut + 1].isspace() else ' ' + loop[cut:],
        rest.rstrip())
    return loop, _in_outer(head, src[:start] + bounded + src[done_end + len(rest):], bounded)


def _bash_loop_do(cmds, first, last):
    """The `do` Word of the bash loop cmds[first..last] (not one of a loop nested in it), or None."""
    depth = 1
    for c in cmds[first + 1:last + 1]:
        w = c.words[0] if c.words else None
        if depth == 1 and w is not None and w.text == 'do' and not cm.quoted(c, w):
            return w
        if _lead(c) in ('while', 'until', 'for', 'select'):
            depth += 1
        elif _first(c) == 'done':
            depth -= 1
    return None


def _in_outer(cmd, text, alone):
    """`alone` (the bounded loop) when `cmd` is on the call's own line. When it is nested (bash -c
    '...', powershell -Command '...'), the outer command that runs `text` (the nested source with
    the loop bounded) instead: each outer command with the literal word that held the old text
    replaced by the new, single-quoted for its shell; `alone` when that word cannot be found."""
    if cmd.nested_in is None:
        return alone
    c = cmd
    while c.nested_in is not None:
        outer = c.nested_in
        hits = [w for w in outer.words if not w.dyn and w.text == c.source]
        if len(hits) != 1 or outer.start is None:
            return alone
        w = hits[0]
        quoted = "'%s'" % (text.replace("'", "''") if outer.shell == sh.PS else text.replace("'", "'\\''"))
        src = outer.source
        text = src[outer.start:w.start] + quoted + src[w.end:outer.end]
        c = outer
    return text


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
    clock or `timeout` bounding it. A call the host runs in the background (Action.background),
    or a loop sent to the background with &, is left alone: neither holds the call."""
    if event is not None and event.background:
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


# git config's long options (git 2.55, the legacy form's and its subcommands' together), so an
# abbreviation git accepts (--glo, --fil, --ed) is read as git reads it; those that take a value;
# its short options by the long one each stands for (-f and -t take a value)
CONFIG_LONGS = ('global', 'system', 'local', 'worktree', 'file', 'blob', 'get', 'get-all', 'get-regexp',
                'get-urlmatch', 'replace-all', 'add', 'unset', 'unset-all', 'rename-section', 'remove-section',
                'list', 'fixed-value', 'edit', 'get-color', 'get-colorbool', 'type', 'no-type', 'bool', 'int',
                'bool-or-int', 'bool-or-str', 'path', 'expiry-date', 'null', 'name-only', 'includes', 'show-origin',
                'show-scope', 'show-names', 'default', 'comment', 'all', 'regexp', 'value', 'url', 'append')
CONFIG_LONG_VALUES = ('file', 'blob', 'type', 'default', 'comment', 'value', 'url')
CONFIG_SHORTS = {'f': 'file', 't': 'type', 'l': 'list', 'e': 'edit', 'z': 'null'}
CONFIG_READS = frozenset(('get', 'get-all', 'get-regexp', 'get-urlmatch', 'get-color', 'get-colorbool', 'list'))
CONFIG_WRITES = frozenset(('unset', 'unset-all', 'replace-all', 'add'))
CONFIG_SECTIONS = frozenset(('remove-section', 'rename-section'))


def _config_long(body):
    """The git config long options `--body` selects: itself when exact or unknown, the one it
    abbreviates (a --no-<name> negation too, as no-<name>), or every one it could abbreviate (git
    refuses an ambiguous prefix)."""
    if body in CONFIG_LONGS or len(body) < 1:
        return [body]
    hits = [n for n in CONFIG_LONGS if n.startswith(body)]
    if body.startswith('no-'):
        hits += ['no-' + n for n in CONFIG_LONGS if n.startswith(body[3:])]
    out = []
    for n in hits:
        if n not in out:
            out.append(n)
    return out or [body]


def config_args(gc, dyn_opts=False):
    """(names, sure, pos, files) of a `git config` call's arguments, read as git reads them:
    `names` every long option name a word may select (an abbreviation as the option it
    abbreviates, an ambiguous one as each, a short one by its long name, -f<path> split), `sure`
    the names a word selects alone, `pos` the positional Words, `files` the Words its -f/--file
    options name. A computed word (a PowerShell splat @a too, which may stand for any words) is
    positional, unless `dyn_opts` and it starts with '-' (then an option whose name is unknown)."""
    names, sure, pos, files = set(), set(), [], []
    args = gc.rest
    ps = gc.inv is not None and gc.inv.cmd.shell == sh.PS
    k = 0
    while k < len(args):
        w = args[k]
        t = w.text
        if ps and not w.dyn and w.start is not None and cm.PS_SPLAT_RE.match(t) and not cm.quoted(gc.inv.cmd, w):
            w = sh.Word(t, True, w.start, w.end)
        if t == '--':
            pos.extend(args[k + 1:])
            break
        k += 1
        if not t.startswith('-') or len(t) < 2 or (w.dyn and not dyn_opts):
            pos.append(w)
            continue
        if w.dyn:
            names.add(t)
            continue
        if t.startswith('--'):
            body, eq, val = t[2:].partition('=')
            hits = _config_long(body)
            names.update(hits)
            if len(hits) != 1:
                continue
            sure.add(hits[0])
            if hits[0] not in CONFIG_LONG_VALUES:
                continue
            if eq:
                value = sh.Word(val, w.dyn, w.start, w.end)
            else:
                value = args[k] if k < len(args) else None
                k += 1
            if hits[0] == 'file' and value is not None:
                files.append(value)
            continue
        body = t[1:]
        for j, c in enumerate(body):
            name = CONFIG_SHORTS.get(c, '-' + c)
            names.add(name)
            sure.add(name)
            if c not in 'ft':
                continue
            if body[j + 1:]:
                value = sh.Word(body[j + 1:], w.dyn, w.start, w.end)
            else:
                value = args[k] if k < len(args) else None
                k += 1
            if c == 'f' and value is not None:
                files.append(value)
            break
    return names, sure, pos, files


def git_config_call(gc):
    """(key, value Word or None, writes) of a `git config` call on a config git reads, or None (no
    key, --blob, or --file names a file git does not read). Options are read as git reads them
    (config_args: --fil <path>, -f<path>, --glo). A --file the text does not name (a variable, a
    substitution) counts as one git reads: fail closed."""
    names, sure, pos, files = config_args(gc, dyn_opts=True)
    if any(not f.dyn and not repo_config_file(f.text) for f in files):
        return None                             # a file git does not read
    if not pos or 'blob' in sure:
        return None
    verb = pos[0].text
    if verb in ('get', 'list'):
        return (pos[1].text.lower() if len(pos) > 1 else ''), None, False
    if verb in ('set', 'unset'):
        if len(pos) < 2:
            return None
        return pos[1].text.lower(), (pos[2] if len(pos) > 2 else None), True
    if sure & CONFIG_READS:
        return verb.lower(), None, False
    writes = bool(names & CONFIG_WRITES) or len(pos) > 1
    return verb.lower(), (pos[1] if len(pos) > 1 else None), writes


def git_prefix_configs(gc):
    """[(key, value text, first Word, last Word)] of the -c and --config-env settings before the
    git subcommand (key lower-case), each word read as the shell hands it to git (commands.passed:
    bash's `-\\c core.hooksPat\\h=x` is -c core.hooksPath=x)."""
    a = list(gc.inv.args)
    a = a[:len(a) - len(gc.rest) - 1]
    out, k = [], 0
    while k < len(a):
        t = cm.passed(gc.inv.cmd, a[k])
        if t in ('-c', '--config-env') and k + 1 < len(a):
            key, _eq, val = cm.passed(gc.inv.cmd, a[k + 1]).partition('=')
            out.append((key.lower(), val, a[k], a[k + 1]))
            k += 2
            continue
        if (t.startswith('-c') and not t.startswith('--')) or t.startswith('--config-env='):
            body = t[2:] if t.startswith('-c') else t.split('=', 1)[1]
            key, _eq, val = body.partition('=')
            out.append((key.lower(), val, a[k], a[k]))
        k += 1
    return out


def _is_kit_hooks(text, cwd, home=None):
    p = sh.resolve_path(text, cwd, home=home) if text else None
    return bool(p) and same_dir(p, kit_githooks())


def _without(cmd, first, last):
    """The command, from its first word, with the words first..last cut out."""
    return (cmd.source[cmd.words[0].start:first.start].rstrip() + ' ' +
            cmd.source[last.end:cmd.words[-1].end].lstrip()).strip()


def _under_temp(ctx, d):
    if not d:
        return False
    top = paths.norm(ctx.tempdir, absolute=True).rstrip('\\/')
    here_ = paths.norm(d, absolute=True)
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
    """The paths a `git config` call names with -f/--file (config_args: --fil, -f<path> too)."""
    return [w.text for w in config_args(gc, dyn_opts=True)[3]]


def _config_scope_all(gc):
    """True when a git call names --global or --system; for `git config`, in any spelling git
    accepts (config_args: --glo, --sys)."""
    if gc.sub == 'config':
        return bool({'global', 'system'} & config_args(gc, dyn_opts=True)[0])
    return any(w.text in ('--global', '--system') for w in gc.rest)


# The variables git reads config settings from: GIT_CONFIG_PARAMETERS ('key'='value' ...) and
# GIT_CONFIG_KEY_<n> (with GIT_CONFIG_VALUE_<n>).
HOOKS_ENV = r'GIT_CONFIG_PARAMETERS|GIT_CONFIG_KEY_\d+'
HOOKS_WHY = ('core.hooksPath picks the folder git runs hooks from, the kit\'s pre-push checks among them; the owner '
             'sets it at install.')


def git_hooks_path(event, ctx, cmd, inv, cwd, role):
    """A change of core.hooksPath (git config, including -f on a repository's own .git/config;
    git -c, git --config-env, git clone -c; GIT_CONFIG_PARAMETERS or GIT_CONFIG_KEY_<n>), in any
    session: it switches the repository's hooks off or elsewhere. Setting it to the kit's own
    githooks (the install step), and repositories under the temp folder (not --global or
    --system), are left alone. A git alias that runs `config` is judged the same way where
    git_alias_config expands it (hooks_config_write)."""
    why = HOOKS_WHY
    for name, val in env_assignments(cmd, HOOKS_ENV):
        if HOOKS_KEY in val.lower():
            where = quote(cmd.cwd or cwd) if (cmd.cwd or cwd) else '"<checkout>"'
            return refuse('git-hooks-path', role, '`%s` sets %s to change core.hooksPath for the git commands that '
                          'read it. %s Ask the owner (through the PM) when it needs to change. To see it:' %
                          (bare(cmd), name, why), 'git -C %s config --get core.hooksPath' % where)
    gc = cm.git_call(inv)
    if gc is None:
        return None
    if _under_temp(ctx, gc.dir) and not _config_scope_all(gc):
        return None
    configs = git_prefix_configs(gc) + (clone_configs(gc) if gc.sub == 'clone' else [])
    for key, val, first, last in configs:
        if key == HOOKS_KEY and not _is_kit_hooks(val, gc.dir, getattr(ctx, 'home', None)):
            return refuse('git-hooks-path', role, '`%s` runs git with core.hooksPath=%s, which replaces the '
                          'repository\'s hooks. %s Run it without the setting:' %
                          (bare(cmd), val, why), _without(cmd, first, last))
    if gc.sub != 'config':
        return None
    return hooks_config_write(ctx, gc, role, bare(cmd))


def hooks_config_write(ctx, gc, role, shown):
    """git_hooks_path's judgement of a `git config` call `gc` (shown as `shown`): a write of
    core.hooksPath to a config git reads, other than the kit's own githooks; a repository under
    the temp folder (not --global or --system), and -f on a config under it, are left alone."""
    if _under_temp(ctx, gc.dir) and not _config_scope_all(gc):
        return None
    home = getattr(ctx, 'home', None)
    if any(_under_temp(ctx, os.path.dirname(sh.resolve_path(f, gc.dir, home=home) or '')) for f in _config_files(gc)):
        return None                              # -f on a scratch repository's config under temp
    call = git_config_call(gc)
    if call is None:
        return None
    key, value, writes = call
    if key != HOOKS_KEY or not writes:
        return None
    if value is not None and not value.dyn and _is_kit_hooks(value.text, gc.dir, home):
        return None
    where = quote(gc.dir) if gc.dir else '"<checkout>"'
    return refuse('git-hooks-path', role, '`%s` changes core.hooksPath. %s Ask the owner (through the PM) when it '
                  'needs to change. To see it:' % (shown, HOOKS_WHY), 'git -C %s config --get core.hooksPath' % where)


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
        t = sh.ps_dashed(w.text)
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
        pos.append(w.text)
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
