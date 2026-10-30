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
  owner-tool       a shell command running one of the owner's tools to act (tool_run reads the file
                   it runs): reserve_ui; a lock CLI owner-only write (reserve, release, hold
                   whole|half|slots, division --set, game on|off|set, top, stop/unbook --force);
                   machine setup with --apply (its PowerShell front ends with -Apply), which
                   writes the owner's email; in any session but the owner's own
  hub-secrets      a shell command with a literal path into the hub's token folder (a bare token
                   file's name, a hub-like name elsewhere, a computed word, the word hub and the
                   store are not refused; a wildcard path is read as the literal folder before
                   its first wildcard, glob_folder, so `cat ~/.pm-kit/hub/*` is), or python
                   or node code that names a token file or the folder (kitlib.guard.hub.INLINE_RE);
                   an edit of a file in the token folder or the store; a read tool's
                   read of the token folder or a file in it, or a Grep that can read one (Glob
                   is not hooked; the store stays readable); in any session but the owner's own
                   (kitlib.guard.hub)
  hub-owner-verb   a parsed hub tool run (hub_run_args: python, its options, then tools/hub.py or
                   -m tools.hub; tools/hub.py run as the program; python -m runpy tools.hub; at
                   any depth of the line) whose arguments run an owner verb
                   (answer, open, pair, unpair, stop, restart, todos done, stories add|withdraw,
                   to-pm add|send, inbox read, approvals approve|deny, todos add --owner; a word
                   the guard cannot read where the verb could be counts), in any session but the
                   owner's own. Text that only names the hub tool (prose, a pipe of a run into
                   another program) is not read
  ledger          an edit of the machine config's ledger_path, of the rulebooks' folder or a
                   `.md` file directly in it, or of the standing digest (kitlib.ledger.files
                   of_config: `rules` and STANDING.md in ledger_dir, else beside ledger_path), by
                   anyone; with a broken machine config, every subagent write
  lock-state       an agent session's edit of a file in the machine lock's state folder
  machine-config   an edit of a file in the PM repository's machine config folder
                   (<pointer>/.pm-kit) or in a .pm-kit folder at the top of a checkout, in any
                   session but the owner's own (its hub folder is hub-secrets', the owner's
                   delegation list owner-delegations')
  kit-json         an agent session's edit of a .kit.json in a checkout
  generated-file   an edit of a file tools/render_content.py writes in a kit checkout (a host's
                   agents, skills copies, plugin manifests: kitlib.guard.generated), in any session
  main-checkout    an edit in a main checkout by a role whose edit_main_checkout is false
  write-jail       an agent session's write outside its own checkout, its repository's other
                   worktrees, the temp folder and the .kit.json data roots (kitlib.guard.jail);
                   git that writes a checkout outside them
  wrapped-gate     (off by default) gate.py or `machine_lock.py run` piped into another command (P16)
  root-search      a recursive search from a drive or filesystem root
  heavy            (a warning by default) a heavy program run with no lock held (MACHINE_LOCK_RUN
                   verified by nest_check)
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
                   ref or a repository the text does not name is refused too. A coordinator's
                   main session may also delete a stray ref (refs/ outside refs/heads, refs/tags,
                   refs/stash and refs/backlog) in its own repository. The same allowance
                   judges a push into the repository itself, a fetch or pull refspec's
                   destination (a remote's own tracking ref passes), symbolic-ref with two
                   names or -d, replace and a notes write
  git-hooks-path   a change of core.hooksPath (git config, git -c, clone -c, GIT_CONFIG_*), in
                   any session
  git-stash-shared git stash in a linked worktree by an agent session that adds an untagged
                   entry or takes one it does not name (bare stash, pop, save, clear)
  owner-identity   the owner's email given to a commit, in any session but the owner's own
  kill-by-name     an agent session ending processes by name (taskkill /IM, Stop-Process -Name)
  cmd-reparse      a batch file run (.cmd, .bat, the bare kit command) or a Git Bash start run
                   with an argument cmd.exe would read again (shell.CMD_REPARSE), a computed one, a
                   splat, an unquoted bash wildcard or xargs' input, in any session (the shell
                   rules' last)
  busy-wait        (off by default) a foreground sleep loop with no counter, clock or timeout bounding it
  git-alias        (a warning by default) a git alias defined on the command line (-c alias.<name>=...)
A rule's mode (deny, warn or off) is kitlib.guard.modes'; a rule it does not name is deny.
A command line the parser cannot read cleanly also goes through kitlib.guard.fallback (its rule,
unreadable, is a warning by default).
"Agent session" and "the owner's own" are the session mark's (docs/guard.md "Session mark").
"""
import fnmatch
import json
import ntpath
import os
import posixpath
import re

from kitlib import agentname
from kitlib import config as kconfig
from kitlib.config import user as kuser
from kitlib import kitroot
from kitlib import pmdata
from kitlib.git import defaults as gitdefaults
from kitlib.git import repo
from kitlib import lockmcp as lockmcp_names
from kitlib.host import Decision

from . import commands as cm
from . import context
from . import generated
from . import hub as hubg
from . import jail
from . import modes
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
    """The refusal `rule` makes, or None when its mode (kitlib.guard.modes) is warn, which keeps the
    reason as the call's warning, or off."""
    reason = '%s Instead: %s %s' % (what.rstrip(), instead.strip(), tag(rule, role))
    m = modes.mode(rule)
    if m == modes.OFF:
        return None
    if m == modes.WARN:
        modes.note(reason)
        return None
    return Decision(kind, rule, reason, instead.strip())


def reworded(d, reason):
    """`d` with its reason replaced (a rule that adds what led to it)."""
    return Decision(d.verdict, d.rule, reason, d.instead)


def fwd(path):
    return path.replace('\\', '/')


def quote(path):
    return '"%s"' % fwd(path)


def shell_word(text):
    """`text` as one word bash and PowerShell both read as written: bare when it holds only
    characters neither shell treats specially (no `,`, which PowerShell reads as an array, and no
    leading `@`, its splat), else single-quoted (literal in both); one that holds a single quote
    too has no such word, so it is `<value>` for the agent to fill in."""
    if re.fullmatch(r'[A-Za-z0-9_=.:/%+-][A-Za-z0-9_=.:/@%+-]*', text):
        return text
    return "'%s'" % text if "'" not in text else '<value>'


def kept_word(lead, sep, value, dyn):
    """One kept option of a held form (commit_scan's `keep`) as words both shells read as
    written: `lead` (`-X`, `--strategy`) and its `value`, joined (`sep` '') or as two words (' ');
    a value the shell computes, or one holding a single quote, is `<value>` after its lead."""
    if dyn or "'" in value:
        return lead + sep + '<value>'
    return shell_word(lead) + ' ' + shell_word(value) if sep else shell_word(lead + value)


def literal_word(path):
    """`path` as one word that bash and PowerShell both read literally: single-quoted when it holds
    a `$` or a backtick (which expand inside double quotes) and no single quote; otherwise
    double-quoted, as quote() does. A path with both a single quote and one of those has no such
    word and stays double-quoted (docs/guard.md "Rules", the `heavy` row)."""
    p = fwd(path)
    if ('$' in p or '`' in p) and "'" not in p:
        return "'%s'" % p
    return quote(p)


def advice_kit(ctx):
    """The kit folder an alternative names (docs/guard.md "Kit paths in advice"): the installed
    export the sessions' kit tools run from, when the guard can know it, else the kit the guard
    runs from (ctx.kit). The guard names the export the machine's plugin marketplace names now
    (kitlib.pluginsource.current_folder under the plugin export root), when that folder is an
    export (a 12-hex-digit name, the marker and kit.json in it) whose marker names the kit checkout
    the guard is from (export_checkout): the guard's own checkout, or, for a guard run from an
    export, the checkout that export's marker names. So a guard still running from an export a
    refresh replaced names the current one, which prune keeps, and which kitlib.host.headerfollow
    then tells the session its header names. The hook is not told which folder a session's header
    named, so a session that started on an older export is still pointed at the current one."""
    kit = getattr(ctx, 'kit', None)
    if not kit:
        return kit
    from kitlib import pluginsource
    from . import rolepolicy
    source = kit
    if os.path.isfile(os.path.join(kit, pluginsource.MARKER)):
        source = pluginsource.export_checkout(kit)
        if not source:
            return kit
    user = ctx.user()
    if not isinstance(user, dict) or user.get('error'):
        return kit
    try:
        root = pluginsource.settings(user.get(pluginsource.CONFIG_KEY), ctx.environ).export_dir
    except (pluginsource.RefreshError, OSError, ValueError, TypeError):
        return kit
    name = pluginsource.current_folder(root)
    if not isinstance(name, str) or not rolepolicy.EXPORT_NAME_RE.fullmatch(name):
        return kit
    folder = os.path.join(root, name)
    if not all(os.path.isfile(os.path.join(folder, f)) for f in rolepolicy.EXPORT_FILES):
        return kit
    checkout = pluginsource.export_checkout(folder)
    if not checkout or paths.norm(checkout) != paths.norm(source):
        return kit
    return folder


def lock_tool(ctx):
    """The lock CLI an alternative names: tools/machine_lock.py of the kit folder advice names
    (advice_kit, kitlib.kitroot.lock_tool), as one literal word (literal_word) since a kit path may
    hold a space or a `$`. Its `run` takes no --lock flag: it always locks."""
    return literal_word(kitroot.lock_tool(advice_kit(ctx)))


def kit_tool(ctx, name):
    """A kit tool an alternative names: tools/<name>.py of the kit folder advice names
    (advice_kit), quoted. A repository holds no copy of the kit's tools (decision 21 in
    docs/multi-host-plan.md section 7), so an alternative never names a repository's own tools
    folder."""
    return quote(kitroot.tool(name, advice_kit(ctx)))


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
    allow or ask then meets the role policy (kitlib.guard.rolepolicy; its refusal wins over an ask), which is off for a role whose
    .kit.json entry names no mode, and, when it adds write-jail roots, the shared rules again.
    Each refusal meets its rule's mode (kitlib.guard.modes): an allow that a warn-mode rule
    refused carries the warnings (rule WARNED, reason WARNING plus each refusal's reason)."""
    from . import rolepolicy
    ctx = ctx or context.Ctx(environ=environ, profile=profile)
    with modes.judging(ctx) as warnings:
        asked = Asked()                         # an ask still meets the role policy, whose refusal wins
        d = asked.firm(judge(event, ctx))
        if d:
            return d
        d, held = rolepolicy.screen(event, ctx)
        if d is None and held is not ctx:
            d = judge(event, held)
        d = asked.firm(d) or asked.first
        if d:
            return d
        if warnings:
            return Decision(ALLOW, WARNED, WARNING + ' '.join(warnings))
    return Decision(ALLOW)


WARNED = 'warn'
WARNING = '[kit guard warning] '


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
    late = None             # cmd-reparse's, judged last: every other rule's refusal, the fallback's too, comes first
    asked = Asked()         # an ask (the unread ledger's) waits: any refusal on the line wins over it
    for cmd in parsed.commands:
        inv = cm.invocation(cmd)
        for rule in (owner_tool, hub_rules.secrets, hub_rules.owner_verb, owner_delegations, ledger_git,
                     wrapped_gate, root_search, heavy, git_rules, backlog_shell, gate_allow, git_hooks_path,
                     owner_identity, kill_by_name, shell_writes, git_plain_commit, interpreter_git):
            d = asked.firm(rule(event, ctx, cmd, inv, cwd, role))
            if d:
                return d
        late = late or asked.firm(cmd_reparse(event, ctx, cmd, inv, cwd, role))
    d = asked.firm(brace_limit(parsed, role))
    if d:
        return d
    d = asked.firm(busy_wait(event, ctx, parsed, cwd, role))
    if d:
        return d
    if not parsed.clean:
        from . import fallback
        d = asked.firm(fallback.check(event, ctx, text, cwd, role, parsed))
        if d:
            return d
    return late or asked.first


class Asked(object):
    """The first ask among a run of decisions, held back so that a refusal judged after it wins:
    firm(d) gives `d` unless it is an ask (kept in `first`, the earliest), when it gives None."""

    def __init__(self):
        self.first = None

    def firm(self, d):
        if d is not None and d.verdict == ASK:
            self.first = self.first or d
            return None
        return d


def brace_limit(parsed, role):
    """A line with a bash word whose brace groups give more than shell.BRACE_LIMIT words (rule
    brace-limit, every session): the guard does not write that word out, so no path rule sees the
    words bash makes from it, and the line is refused rather than read as a computed word. The
    alternative is the word's shell text with the word replaced by a placeholder (docs/guard.md
    "Rules")."""
    if parsed.brace_over is None:
        return None
    text, start, end = parsed.brace_over
    word = text[start:end]
    if len(word) > 80:
        word = word[:77] + '...'
    alt = (text[:start] + BRACE_NAMES % sh.BRACE_LIMIT + text[end:]).strip()
    return refuse('brace-limit', role, 'The guard could not read this command: its word %s gives more than %d '
                  'words by brace expansion, and the guard writes out at most %d, so it cannot tell what the '
                  'command names. Write the names out, or split them into words that give at most %d each.'
                  % (word, sh.BRACE_LIMIT, sh.BRACE_LIMIT, sh.BRACE_LIMIT), alt)


BRACE_NAMES = '<its names, written out or in brace words of at most %d each>'


CMD_CHAR_NAMES = {'\r': 'a carriage return', '\n': 'a line break'}


def cmd_reparse(event, ctx, cmd, inv, cwd, role):
    """A batch file run (commands.batch_run: a .cmd or .bat, cmd's call of one, the bare kit
    command) whose arguments cmd.exe would read again before the program does (rule cmd-reparse,
    every session; docs/guard.md "Batch files and cmd.exe"): an argument holding one of
    shell.CMD_REPARSE, a computed one (cmd.exe reads the value it gets), a PowerShell splat, an
    unquoted bash wildcard (bash fills in file names, which cmd.exe then reads), or arguments xargs
    gives it. Git Bash's start (commands.bash_start) hands every word to cmd.exe, whatever it
    starts, so each of its words is read the same way. The kit command's alternative is the same
    words through python and tools/kit.py, which no cmd.exe reads; another program's is to run it
    directly."""
    found = cm.batch_run(cmd, inv)
    started = None
    if found is None:
        started = cm.bash_start(cmd, inv)
        if started is None:
            return None
        progw, args = started
        prog = progw.text if progw is not None and not progw.dyn else None
        after = args[next(k for k, w in enumerate(args) if w is progw) + 1:] if progw is not None else []
    else:
        prog, args = found
        after = args
    ps = cmd.shell == sh.PS
    why = 'xargs gives it arguments the guard cannot read' if inv.via == 'xargs' else None
    for w in args:
        if why:
            break
        shown = w.text.replace('\r', '\\r').replace('\n', '\\n')
        shown = shown if len(shown) <= 60 else shown[:57] + '...'
        if w.dyn:
            why = 'its argument %s is computed, and cmd.exe reads the value it gets' % shown
        elif ps and w.start is not None and cm.PS_SPLAT_RE.match(w.text) and not cm.quoted(cmd, w):
            why = 'its argument %s is a splat, whose values the guard cannot read' % shown
        else:
            c = sh.cmd_unsafe(w.text)
            if c is not None:
                why = 'its argument %s holds %s, which cmd.exe reads' % (shown, CMD_CHAR_NAMES.get(c, c))
            elif cm.bash_glob(cmd, w):
                why = ('its argument %s is a wildcard bash fills in with file names, which cmd.exe then '
                       'reads (a file named x&whoami would run whoami)' % shown)
    if why is None:
        return None
    signs = ('& | < > run or redirect another command, ^ and quotes change the words, % and ! '
             'expand variables, a line break drops the words after it')
    if started is None:
        what = ('This runs the batch file %s, and Windows runs a batch file in cmd.exe, which reads its '
                'command line again before the program sees it: %s (%s).' % (prog, why, signs))
    else:
        what = ('This runs %s through Git Bash\'s start, a script that hands all its words to cmd.exe\'s '
                'start (escaping only &), so cmd.exe reads them before the program sees them, and again '
                'when the program is a batch file: %s (%s).'
                % (prog or 'a program', why, signs))
    if prog is not None and cm.is_batch(prog) and cm.base_name(prog.rstrip('. ')) == cm.KIT:
        words = []
        for w in after:
            if ps and w.text == '--%' and not w.dyn:
                continue
            unsure = (w.dyn or '\r' in w.text or '\n' in w.text
                      or (ps and w.start is not None and cm.PS_SPLAT_RE.match(w.text) and not cm.quoted(cmd, w)))
            if not unsure and cm.bash_glob(cmd, w):
                # through python no cmd.exe reads what bash fills in: the wildcard as written
                spelt_at = cmd.source[w.start:w.end] if w.start is not None and w.end is not None else None
                words.append(spelt_at if spelt_at and not sh.cmd_unsafe(spelt_at) else '<value>')
                continue
            words.append('<value>' if unsure else shell_word(w.text))
        instead = ' '.join(['python', kit_tool(ctx, 'kit')] + words)
    elif started is not None:
        instead = ('run %s directly, without start, so no cmd.exe reads the words, or give start words '
                   'without cmd.exe\'s characters (& | < > ^ %% ! ( ) " and line breaks) and no '
                   'unquoted wildcard.' % (prog or 'the program'))
    else:
        instead = ('run the program %s runs directly (python, node, the exe it starts), so no cmd.exe '
                   'reads the words again, or give it words without cmd.exe\'s characters '
                   '(& | < > ^ %% ! ( ) " and line breaks) and no unquoted wildcard.' % prog)
    return refuse('cmd-reparse', role, what, instead)


def backlog_shell(event, ctx, cmd, inv, cwd, role):
    """The backlog roles' shell rules (kitlib.guard.backlogrole.shell)."""
    from . import backlogrole
    return backlogrole.shell(event, ctx, cmd, inv, cwd, role)


def owner_delegations(event, ctx, cmd, inv, cwd, role):
    """A write, move or delete of the owner's delegation list (kitlib.guard.delegfile.shell)."""
    from . import delegfile
    return delegfile.shell(event, ctx, cmd, inv, cwd, role)


def delegations_edit(event, ctx, target, role, what, stand_in=False):
    """An edit of the owner's delegation list, or of a folder that holds it (kitlib.guard.delegfile.edit)."""
    from . import delegfile
    return delegfile.edit(event, ctx, target, role, what, stand_in)


def gate_allow(event, ctx, cmd, inv, cwd, role):
    """Who may run `gate.py allow` (kitlib.guard.gaterole.shell)."""
    from . import gaterole
    return gaterole.shell(event, ctx, cmd, inv, cwd, role)


def backlog_edit(event, ctx, target, role, what):
    """An edit of a folder store's records (kitlib.guard.backlogrole.raw_edit), or of the backlog's
    audit log (audit_edit)."""
    from . import backlogrole
    return backlogrole.raw_edit(event, ctx, target, role, what) or \
        backlogrole.audit_edit(event, ctx, target, role, what)


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
    full in guard_mcp_tools. Only an MCP call (Action kind 'mcp') names one. The names are the
    retired owner writes pinned in kitlib.lockmcp.OWNER_ONLY: the server no longer serves them,
    but one started from an older release still does until it restarts."""
    if kind != 'mcp' or not tool:
        return None
    owner_only = lockmcp_names.OWNER_ONLY
    prefix = mcp_prefix(ctx)
    if tool.startswith(prefix) and tool[len(prefix):] in owner_only:
        return tool[len(prefix):]
    sep = host_mcp_separator(ctx.host_profile())
    tail = tool.rsplit(sep, 1)[-1] if sep else tool
    if tail in owner_only:
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
    AGENT_MARKS: coordinator, deep-worker, consultant and pm), case
    ignored; a guard_agent_marks value replaces the default list. The rules only agents are held to (kill by name, lock state,
    kit-json, the write jail, git-stash-shared) use it. The mark is compared without the kit plugin's prefix
    (kitlib.agentname.bare_mark), so a role_resolver that does not strip it fails closed too."""
    if subagent(event):
        return True
    marks = ctx.user().get('guard_agent_marks') or AGENT_MARKS
    who = agentname.bare_mark(str(ctx.session_role(event) or ''))
    return who.lower() in {str(m).lower() for m in marks}


# every session kind the kit launcher marks but `plain` and other agents' names
AGENT_MARKS = ('coordinator', 'deep-worker', 'consultant', 'pm')


# ---------------------------------------------------------------- owner tools

# The lock's own owner check reads process ancestry, which a detached process can forge, so agents do
# not run the owner's tools: reserve_ui, the lock CLI's owner-only writes and machine setup to act.
# The rule reads the file a command runs (tool_run), never text that only names one: a read, git, a
# name the shell builds at run time and python code that imports the package are not read.
UI_NAME_RE = re.compile(r'(?i)^reserve_ui(?:\.pyw?)?$')
OPENERS = ('invoke-item', 'ii', 'explorer')     # open a file through its association: runs a .pyw
# the interpreters the guard knows: commands.PYTHON_RE plus the pyw launcher
PY_RUN_RE = re.compile(r'^(python|pythonw|py|pyw)(\d+(\.\d+)*)?$')
# python's -c or -m, alone or ending a cluster of its no-value flags (-Bm), its value maybe joined
PY_FLAG_RE = re.compile(r'^-[bBdEiIOPqsSuvx]*([cm])(.*)$')
LOCK_SCRIPT = 'machine_lock.py'
# options of either lock CLI that take a value, so their value is not read as a subcommand
LOCK_VALUE_OPTS = {'--test-root', '--test-hardware', '--test-kinds', '--test-actor', '--kind', '--repo',
                   '--slug', '--cores', '--hold', '--reservation', '--gpu-share', '--on-release',
                   '--max-wait', '--expect', '--note', '--booking', '--detached-id', '--from', '--until',
                   '--date', '--for', '--cpu', '--gpu', '--set'}
OWNER_LOCK_WHY = ('owner-only lock tools are run by the owner, not agents; the lock\'s own owner check stays '
                  'as a second layer. Ask the owner to make the change. To see the lock state:')


def tool_run(inv):
    """(name, Words) for the file the invocation `inv` runs, or None: its program's base name
    (commands.base_name) and arguments; python's script (its first operand) or -m module (as
    <its last part>.py) and the words after it; PowerShell's .ps1 script; a POSIX shell's script
    (its first operand; -c text and -s standard input are None); the file an opener
    (Invoke-Item, explorer, Git Bash's start) opens. The kit command (`kit X`, tools/kit.py X,
    -m tools.kit X) is read as the tool X it runs (kit_run). python -c, standard input and a
    program or script the text does not show are None: the guard reads what a line runs, not
    what it builds."""
    return kit_run(_tool_run(inv))


def kit_run(run):
    """The (name, Words) `run` of tool_run, with the kit command read as the tool it runs
    (commands.kit_tool): ('backlog.py', the Words after `backlog`) for `kit backlog ...`; None
    for a computed tool name; `run` itself when it names no tool (kit -h) or is not the kit."""
    if run is None or run[0] not in (cm.KIT, cm.KIT_SCRIPT):
        return run
    script, rest = cm.kit_tool(run[1])
    if script is None:
        return None
    return (script, rest) if script else run


def script_run(inv):
    """tool_run, with runpy's module argument (`python -m runpy tools.X`) read as the script it
    runs, the kit command's included (`-m runpy tools.kit X` is X.py)."""
    run = tool_run(inv)
    if run is not None and run[0] == 'runpy.py':
        words = run[1]
        if not words or words[0].dyn:
            return None
        run = kit_run((words[0].text.rsplit('.', 1)[-1].lower() + '.py', words[1:]))
    return run


def _tool_run(inv):
    if inv is None or inv.dyn:
        return None
    name, args = inv.name or '', list(inv.args)
    if name in OPENERS or (name == 'start' and inv.via != 'start-process'):
        while args and (args[0].text.startswith('/') or not args[0].text):   # start [/opts] ["title"]
            args.pop(0)
        if not args or args[0].dyn:
            return None
        if name in OPENERS:
            return cm.base_name(args[0].text), args[1:]
        name, args = cm.base_name(args[0].text), args[1:]
    if name in cm.PS_SHELLS:
        for k, a in enumerate(args):
            if a.text.lower().endswith('.ps1') and not a.dyn:
                return cm.base_name(a.text), args[k + 1:]
        return None
    if name in cm.BASH_SHELLS:                          # sh [opts] script.sh args (installer/machine-setup.sh)
        k = 0
        while k < len(args):
            t = args[k].text
            if t in cm.BASH_VALUE_OPTS:
                k += 2
                continue
            if t in ('-', '--'):
                k += 1
                break
            if t[:1] in '-+' and len(t) > 1:
                if t[:1] == '-' and not t.startswith('--') and ('c' in t[1:] or 's' in t[1:]):
                    return None                         # -c text (the nested parse reads it), -s stdin
                k += 1
                continue
            break
        if k >= len(args) or args[k].dyn:
            return None
        return cm.base_name(args[k].text), args[k + 1:]
    if not PY_RUN_RE.match(name):
        return name, args
    k = 0
    while k < len(args):
        t = args[k].text
        flag = PY_FLAG_RE.match(t)
        if t == '-' or (flag and flag.group(1) == 'c'):
            return None
        if flag:                                        # -m, -Bm, -mtools.hub: python's own -m
            if not flag.group(2):
                if k + 1 >= len(args) or args[k + 1].dyn:
                    return None
                k += 1
            mod = flag.group(2) or args[k].text
            return mod.rsplit('.', 1)[-1].lower() + '.py', args[k + 1:]
        if not t.startswith('-'):
            break
        k += 2 if t in cm.PY_VALUE_OPTS else 1
    if k >= len(args) or args[k].dyn:
        return None
    return cm.base_name(args[k].text), args[k + 1:]


def tool_words(words):
    """The texts of tool_run's Words, one holding spaces split (a Start-Process -ArgumentList string)."""
    out = []
    for w in words:
        out.extend(w.text.split() if ' ' in w.text else [w.text])
    return out


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


# Machine setup's --apply writes the machine config, the owner's email among them (plan decision 27),
# and the owner's email is what proves a commit the owner's (owner-only paths, owner-identity). An agent
# that ran it could make its own address the owner's, so only the owner runs it to act. Its dry run stays
# open. The entry points: tools/machine_setup.py, its PowerShell front ends and its Linux front end
# installer/machine-setup.sh (run as a program or as a shell's script, tool_run; it takes --apply exactly).
SETUP_SCRIPTS = {'machine_setup.py': 'py', 'machine-setup.ps1': 'ps', 'new-repo.ps1': 'ps',
                 'machine-setup.sh': 'py'}
# argparse takes a unique prefix of --apply (--ap..); PowerShell a prefix of -Apply, -Apply:$true too
SETUP_APPLY = {'py': re.compile(r'^--ap(?:p(?:l(?:y)?)?)?(?:=.*)?$'),
               'ps': re.compile(r'(?i)^-a(?:p(?:p(?:l(?:y)?)?)?)?(?::.*)?$')}
OWNER_SETUP_WHY = ('machine setup writes the machine config, and the owner\'s email there decides which commits '
                   'count as the owner\'s, so only the owner runs it to act. Ask the owner to run it. To see '
                   'what it would do (the dry run):')


def owner_tool_run(inv):
    """What the invocation `inv` runs of the owner's tools (tool_run), as text, or None: reserve_ui;
    the lock CLI with an owner-only write (lock_owner_write); a machine setup entry point
    (SETUP_SCRIPTS) with --apply / -Apply before any `--`. Its dry run is None."""
    run = tool_run(inv)
    if run is None:
        return None
    name, words = run
    if UI_NAME_RE.match(name):
        return 'reserve_ui, the owner\'s lock reservation UI'
    texts = tool_words(words)
    if name == LOCK_SCRIPT:
        found = lock_owner_write(texts)
        return 'machine_lock.py %s' % found if found else None
    style = SETUP_SCRIPTS.get(name)
    for t in texts if style else ():
        if t == '--':
            break
        if SETUP_APPLY[style].match(t):
            return '%s %s' % (name, t)
    return None


def owner_tool(event, ctx, cmd, inv, cwd, role):
    """owner-tool: a command that runs one of the owner's tools to act (owner_tool_run), in any
    session but the owner's own."""
    if inv is None or is_owner_session(event, ctx):
        return None
    found = owner_tool_run(inv)
    if not found:
        return None
    if found.split()[0] in SETUP_SCRIPTS:
        return refuse('owner-tool', role, '`%s` runs %s: %s' % (bare(cmd), found, OWNER_SETUP_WHY),
                      'python %s' % kit_tool(ctx, 'machine_setup'))
    return refuse('owner-tool', role, '`%s` runs %s, the owner\'s: %s' % (bare(cmd), found, OWNER_LOCK_WHY),
                  'python %s status' % lock_tool(ctx))


# ---------------------------------------------------------------- the Action Hub

# docs/guard.md "Hub secrets". The hub's token folder (owner.token answers as the owner,
# agent.token is every agent's, approval.key will sign the owner's approvals) and its store are
# reached by agents only through tools/hub.py and the hub MCP server, which read agent.token in
# their own process; the hub server's git sync writes the store. So an agent's own edit or shell
# command that names them literally is refused (hub-secrets), as are tools/hub.py's owner verbs on
# a hub tool run the parser reads (hub-owner-verb), the hub MCP server's owner tools (hub-mcp) and
# a browser or computer-use call aimed at the hub's own page (hub-browser). Every session but the
# owner's own is held, as owner-tool holds it: each is the owner's power, not a place an agent
# works. A name the shell or code builds at run time is not read: the rules catch mistakes, not
# an adversary.
HUB_SCRIPT = 'hub.py'
NODE_EVAL = ('-e', '--eval', '-p', '--print', '-pe')
# node's options whose value is the next word (inline_code): a module it loads first, not a script
NODE_VALUE_OPTS = ('-r', '--require', '--import', '--loader', '--experimental-loader', '-C', '--conditions',
                   '--input-type')
HUB_SECRETS_WHY = ('the Action Hub\'s token folder holds the owner\'s key to the hub (owner.token answers as the '
                   'owner) and its store is the hub server\'s to write; an agent reaches the hub only through its '
                   'tool and its MCP server, which read agent.token themselves (docs/guard.md "Hub secrets").')
HUB_OWNER_WHY = ('answering, closing, submitting, sending and approving on the Action Hub are the owner\'s alone '
                 '(docs/guard.md "Hub secrets").')


def hub_config(ctx):
    """The machine config's `hub` object: the strict read's, else (a config the strict read
    refuses) its file's own JSON (the PM repository's local.json, kitlib.config.user LOCAL_KEYS),
    else {}. Its store and ports are read even from a config with a fault elsewhere."""
    user = ctx.user()
    obj = user.get('hub')
    if isinstance(obj, dict):
        return obj
    if not user.get('error'):
        return {}
    loc = ctx.config_location()
    try:
        with open(loc.local or loc.path, encoding='utf-8-sig') as f:
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


def hub_secret_folders(ctx):
    """[(path, real)]: the hub's token folders, each as written and as jail.real gives it. `hub`
    in the home folder's machine config folder (kitlib.config.user.DIR_NAME), where the hub kept
    them before the move and the move leaves its source; the one beside the machine config the
    guard reads (kitlib.guard.hub.secret_folder of Location.new, where kitlib.hubsite.auth.folder
    finds them); and `hub` in the PM repository's machine config folder
    (jail.pointer_config_dir), where the owner's machine setup copies the hub's files before the
    hub reads them from there. A folder named twice is listed once, and one jail.real cannot take
    is left out."""
    out = []
    pointer = jail.pointer_config_dir(ctx)
    folders = [os.path.join(ctx.home, kuser.DIR_NAME, hubg.FOLDER_NAME),
               hubg.secret_folder(ctx.config_location().new)]
    if pointer:
        folders.append(os.path.join(pointer, hubg.FOLDER_NAME))
    for p in folders:
        r = hub_real(p)
        if r and r not in [x[1] for x in out]:
            out.append((p, r))
    return out


class HubPlaces(object):
    """The hub's folders for one guard call: `secrets` the token folders (hub_secret_folders)
    and `stores` the store folders (kitlib.guard.hub.stores), each as written and as jail.real
    gives it."""

    def __init__(self, ctx):
        self.secrets = hub_secret_folders(ctx)
        self.store_paths = hubg.stores(hub_config(ctx), ctx.pm_data_path())
        self.stores = [s for s in (hub_real(p) for p in self.store_paths) if s]

    def secret_of(self, path):
        """The token folder (as written) the absolute `path` is in, or None."""
        here_ = hub_real(path)
        if here_ is None:
            return None
        for p, r in self.secrets:
            if jail.inside(here_, r):
                return p
        return None

    def where(self, path):
        """'secret', 'store' or None for the absolute `path` (real()ed here)."""
        if self.secret_of(path):
            return 'secret'
        here_ = hub_real(path)
        if here_ is not None and any(jail.inside(here_, s) for s in self.stores):
            return 'store'
        return None

    def label(self, where, path):
        """What a refusal names for `path`, which where() placed at `where`."""
        if where == 'secret':
            return 'the hub token folder %s' % quote(self.secret_of(path))
        return 'the hub store'


def hub_session(event, ctx):
    return 'a subagent' if subagent(event) else 'marked %s' % ctx.session_role(event)


def hub_read_alternative(ctx):
    return 'python %s questions' % kit_tool(ctx, 'hub')


def hub_ask_alternative(ctx):
    # a question needs a record or a class (docs/action-hub.md "A question's class")
    return ('python %s questions add --title "<what the owner should decide>" '
            '(--record <repo>:<id> | --class <rule|machine|cross-repo|go>)' % kit_tool(ctx, 'hub'))


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
    return hub_secrets_refusal(event, ctx, role, '%s is in %s' % (what, places.label(where, target)))


def hub_secret_read(event, ctx, cwd, role):
    """hub-secrets for a read tool (Action kind 'read'), in any session but the owner's own: a
    read of the hub's token folder or a file in it, and a contents search that can read one. The
    store stays readable. A search starting in the folder is refused; one from a parent is refused
    unless its file filter can take in none of the folder's files (hub.filter_reaches). A search
    with no filter is refused at every parent: any of its output modes, counts and file lists too,
    would answer for a token's text. A names search (Glob) is not hooked (it reads no token's text; docs/guard.md
    "Hub secrets"). Each token folder (hub_secret_folders) is held."""
    if is_owner_session(event, ctx):
        return None
    for secret_path, secret in hub_secret_folders(ctx):
        d = hub_folder_read(event, ctx, cwd, role, secret_path, secret)
        if d:
            return d
    return None


def hub_folder_read(event, ctx, cwd, role, secret_path, secret):
    """hub_secret_read for the one token folder `secret_path` (real()ed: `secret`)."""
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
    if not jail.inside(secret, root):
        return None
    flt = params.get('filter')
    rel = [p for p in ntpath.relpath(secret, root).split('\\') if p]
    names = hub_secret_names(secret) if flt is not None else None
    if flt is None or names is None or hubg.filter_reaches(flt, rel, names):
        how = 'with no file filter' if flt is None else 'with a file filter (%s) that can take in a file in it' % flt
        return hub_secrets_refusal(event, ctx, role, '%s, a parent of %s, %s' % (what, label, how))
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


def hub_path_texts(text):
    """The texts of one word that may be paths: the word, and the value of `--opt=value` or
    PowerShell's `-Opt:value`; an option alone is none."""
    if not text.startswith('-'):
        return [text]
    for sep in ('=', ':'):
        if sep in text:
            return [text.split(sep, 1)[1]]
    return []


def glob_folder(text, chars=hubg.GLOB_CHARS):
    """The literal folder a wildcard path `text` starts with (everything before the separator
    ahead of its first wildcard character, one of `chars`), or None when it has none:
    `<token folder>/o*` is in the token folder whatever the wildcard matches."""
    first = min(text.index(c) for c in chars if c in text)
    cut = max(text.rfind('/', 0, first), text.rfind('\\', 0, first))
    return text[:cut] if cut > 0 else None


def inline_code(cmd, inv, skip_pipe=None):
    """The code python or node runs from its own command line (-c, -e) or from standard input
    (a heredoc, a here-string, the command piped in), as texts; [] for a script run or another
    program. The command line's texts are every argument (a path the code is given counts).
    `skip_pipe(c)` true leaves out the text of the command `c` piped in when the code is on the
    command line (it is then the code's data, not code); code read from standard input always
    counts what is piped in."""
    if inv is None:
        return []
    py, node = PY_RUN_RE.match(inv.name or '') is not None, inv.name == 'node'
    if not (py or node):
        return []
    args = [w.text for w in inv.args]
    code, stdin, k = False, True, 0
    while k < len(args):
        t = args[k]
        if (py and t in cm.PY_VALUE_OPTS) or (node and t in NODE_VALUE_OPTS):
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
            if c is not cmd and not (code and skip_pipe is not None and skip_pipe(c)):
                texts.append(c.text)
            texts.extend(r.target.text for r in c.redirects
                         if r.target is not None and (r.body or r.op == '<<<'))
    return texts


def hub_secret_shell(ctx, cmd, inv, places):
    """What the command `cmd` names of the hub's secrets, as text, or None: a literal path in
    the token folder (a word that only names a token file or looks like the folder, with
    no path into it, passes), and python or node code that names a token file or the folder
    (kitlib.guard.hub.INLINE_RE; docs/guard-v2.md W3: a parsed hub tool run of an agent verb piped into
    code given on the command line is that code's data, hub_run_verb). A wildcard path is read as the literal folder before its first
    wildcard (glob_folder); a DOS wildcard (LEDGER_GLOB_CHARS: `<`, `>`, `"`) is read both ways,
    as that folder and as the literal path (hub_texts); the store and the word hub are not refused (the guard blocks only
    what it can place; an edit tool's write into the store is still hub_secret_edit's).
    A path may start with the home folder as `~` or a home variable ($HOME, $env:USERPROFILE,
    %USERPROFILE%, $env:HOMEDRIVE$env:HOMEPATH: shell.home_text, read as ctx.home), and a
    relative one is read from the directory a cd to the home folder, or a bare cd, moved to
    (the command's cwd_home). `places()` gives the HubPlaces."""
    targets = [r.target for r in cmd.redirects if r.target is not None and not r.body and r.op != '<<<']
    for w in list(cmd.words) + targets:
        for text in hub_path_texts(w.text):
            for text in hub_texts(sh.home_text(text, ctx.home, w.dyn)):
                try:
                    p = sh.resolve_path(text, cmd.cwd_home, cmd.shell, ctx.home)
                except (OSError, ValueError):
                    p = None
                if not p:
                    continue
                p_ = places()
                if p_.where(p) == 'secret':
                    return 'reaches %s (%s)' % (p_.label('secret', p), w.text)
    for text in inline_code(cmd, inv, lambda c: hub_run_verb(c) == ''):
        m = hubg.INLINE_RE.search(text)
        if m:
            return 'runs code that names %s' % m.group(0)
    return None


def hub_texts(text):
    """The paths hub_secret_shell judges for one path text: a wildcard one as its literal folder
    (glob_folder); one with a DOS wildcard (`<`, `>`, `"`: FindFirstFile reads them as wildcards)
    also as the literal folder before the first of those, and, when it has no other wildcard, as
    the literal path too (each reading only adds a path judged)."""
    if text is None:
        return []
    out = [glob_folder(text) if hubg.has_glob(text) else text]
    if any(c in text for c in '<>"'):
        out.append(glob_folder(text, LEDGER_GLOB_CHARS))
    return [t for t in out if t is not None]


def hub_run_args(inv):
    """The Words after the hub tool when `inv` runs it (script_run: python with tools/hub.py as
    its script, or -m tools.hub; tools/hub.py run as the program itself; runpy's module argument,
    `python -m runpy tools.hub`, read as the script; the kit command's `kit hub`), else None."""
    run = script_run(inv)
    if run is None or run[0] != HUB_SCRIPT:
        return None
    return run[1]


def hub_owner_run(parsed):
    """The owner verb a parsed hub tool run on the line `parsed` runs (hub_run_args, every
    command at any depth; kitlib.guard.hub.owner_verb), as text, or None. A word the guard cannot
    read counts as unreadable there, and so does a PowerShell word holding a double quote, which
    Windows PowerShell 5.1 passes on unescaped (it can split into --owner)."""
    for cmd in parsed.commands:
        verb = hub_run_verb(cmd)
        if verb:
            return verb
    return None


def hub_run_verb(cmd):
    """For the command `cmd`: None when it is not a parsed hub tool run (hub_run_args), else the
    owner verb it runs (kitlib.guard.hub.owner_verb, read as hub_owner_run reads it), or '' for
    an agent verb."""
    args = hub_run_args(cm.invocation(cmd))
    if args is None:
        return None
    ps = cmd.shell == sh.PS
    return hubg.owner_verb([hubg.Word(w.text, w.dyn or (ps and '"' in w.text)) for w in args]) or ''


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
        self._done = False

    def places(self):
        if self._places is None:
            self._places = HubPlaces(self.ctx)
        return self._places

    def owner_verb(self, event, ctx, cmd, inv, cwd, role):
        """hub-owner-verb, read once for the whole line: a parsed hub tool run running an owner
        verb (hub_owner_run)."""
        if self.owner or self._done:
            return None
        self._done = True
        verb = hub_owner_run(self.parsed)
        if not verb:
            return None
        return refuse('hub-owner-verb', role, '`%s` runs the hub tool\'s owner verb (%s): %s This session is %s. '
                      'Ask the owner through a question:' % (hub_line(self.parsed.text), verb, HUB_OWNER_WHY,
                                                             hub_session(event, ctx)), hub_ask_alternative(ctx))

    def secrets(self, event, ctx, cmd, inv, cwd, role):
        """hub-secrets: a shell command that names the hub's secrets (hub_secret_shell)."""
        if self.owner:
            return None
        found = hub_secret_shell(ctx, cmd, inv, self.places)
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

def stream_free(path):
    """`path` with an NTFS stream suffix (`:name`, `::$DATA`, `:name:$DATA`) dropped from each of
    its parts after the drive: `STANDING.md::$DATA` opens STANDING.md itself."""
    drive, rest = os.path.splitdrive(path)
    parts = re.split(r'([\\/])', rest)
    return drive + ''.join(p.split(':', 1)[0] if i % 2 == 0 else p for i, p in enumerate(parts))


LONG_PREFIX = re.compile(r'[\\/]{2}[?.][\\/]|(?:[A-Za-z]:)?[\\/]\?\?[\\/]')


def unprefixed(path):
    r"""`path` without a Win32 namespace prefix (`\\?\`, `\\.\`, `\??\`, either slash), which
    opens the same file: `\\?\<drive>:\pm\STANDING.md` is `<drive>:\pm\STANDING.md`, and
    `\\?\UNC\host\share` is `\\host\share`. `\??\` is also dropped after a drive
    (`<drive>:\??\<drive>:\pm`): abspath reads it as a
    folder of the current drive, but Windows opens it as the NT name it is, and no folder is
    named `??`. A prefixed name of another form (`\\?\Volume{...}\`) is left whole for realpath,
    which gives the drive name of a file that exists."""
    m = LONG_PREFIX.match(path)
    if not m:
        return path
    rest = path[m.end():]
    if rest[:4].lower() in ('unc\\', 'unc/'):
        return '\\\\' + rest[4:]
    return rest if re.match(r'[A-Za-z]:(?:[\\/]|$)', rest) else path


def file_key(path):
    """`path` as the filesystem opens it, to compare: a namespace prefix dropped (unprefixed),
    absolute, a stream suffix dropped (stream_free), a trailing dot or space dropped (abspath:
    `alpha.md.` is `alpha.md`), then links, junctions and 8.3 short names resolved and case folded
    (jail.real), as lock_state compares; a prefix realpath keeps or gives (a volume GUID name) is
    dropped from that too."""
    return unprefixed(jail.real(os.path.abspath(stream_free(os.path.abspath(unprefixed(path))))))


def ledger_guarded(ctx):
    """[(kind, real path)] of what the ledger rule holds: 'log' for ledger_path, 'rulebook' for
    the rulebooks' folder and 'digest' for the standing digest (kitlib.ledger.files.of_config over
    the machine config, Ctx.ledger_files). Paths are file_key()ed."""
    out = []
    ledger = ctx.user().get('ledger_path')
    if ledger:
        out.append(('log', file_key(ledger)))
    files = ctx.ledger_files()
    if files is not None:
        if files.rules:
            out.append(('rulebook', file_key(files.rules)))
        if files.digest:
            out.append(('digest', file_key(files.digest)))
    return out


def ledger_kind(ctx, target, above=False):
    r"""What of the ledger `target` is: 'log' for the ledger file, 'rulebook' for the rulebooks'
    folder or a `.md` file directly in it, 'digest' for the standing digest, else None. Names
    compare as the filesystem opens them (file_key: case, a stream suffix, a trailing dot or
    space, a namespace prefix, links, junctions and 8.3 names), first as the literal name (an
    edit's path is never a wildcard: `[PM]` is a folder name), then, when it has a wildcard
    character after its namespace prefix (a DOS one too, LEDGER_GLOB_CHARS), as a wildcard that is any of them it can match
    (ledger_glob; the prefix's own `?` is no wildcard). A `\??\` unprefixed leaves (no drive after
    it) is read both ways: as a prefix, and as a `??` wildcard folder (`<drive>:/??/STANDING.md`
    names `<drive>:\pm\STANDING.md`). With `above`, a folder above a held path is that path's
    kind too, and a wildcard that can name such a folder (ledger_glob): removing or moving the
    folder writes what it holds."""
    full = file_key(target)
    for kind, real in ledger_guarded(ctx):
        if full == real or (kind == 'rulebook' and os.path.dirname(full) == real and full.endswith('.md')):
            return kind
    if above:
        for kind, real in ledger_guarded(ctx):
            if jail.inside(real, full):
                return kind
    bare_target = unprefixed(target)
    end = prefix_end(bare_target)
    starts = (end, 0) if end and not re.match(r'[\\/]{2}', bare_target) else (end,)
    for start in starts:
        if any(c in LEDGER_GLOB_CHARS for c in bare_target[start:]):
            kind = ledger_glob(ctx, bare_target, start, above)
            if kind:
                return kind
    return None


# FindFirstFile also reads `<` (DOS_STAR), `>` (DOS_QM) and `"` (DOS_DOT) as wildcards: `cmd /c del
# "STANDING<"` deletes STANDING.md. ledger_glob matches each as `*`, which matches all they can
# (fail closed); no file name holds one, so none is read as a literal name.
LEDGER_GLOB_CHARS = hubg.GLOB_CHARS | frozenset('<>"')
DOS_WILDCARDS = str.maketrans('<>"', '***')


def prefix_end(path):
    r"""Where a namespace prefix unprefixed leaves (`\\?\Volume{...}\`) ends in `path`, else 0."""
    m = LONG_PREFIX.match(path)
    return m.end() if m else 0


def ledger_glob(ctx, target, start=0, above=False):
    """ledger_kind for the wildcard path `target`: the literal folder it starts in (file_key), and
    each part after it matched (case folded, a stream suffix dropped, as glob matching reads it)
    against the matching part of a held path below that folder. A file is matched by a wildcard
    of the same depth; the rulebooks' folder also by one a part deeper (a file in it), and every
    wildcard that starts in it is a rulebook (fail closed: `rules/*.txt`); `**` or `..` after the
    wildcard matches every held path below the folder. A part also matches a name it spells
    literally (`[PM]` the folder `[PM]`). A wildcard is searched for from `start` (past a namespace
    prefix, prefix_end, whose `?` is none); a DOS wildcard (LEDGER_GLOB_CHARS) is one too, matched
    as `*`. A part matches the 8.3 short name too (glob_part, short_parts), as FindFirstFile
    matches: `A-LONG~?` names `a-long-folder`. With `above`, a wildcard shallower than a held
    path whose parts each match the folders it passes through names a folder above it
    (ledger_kind's `above`)."""
    first = min(target.index(c, start) for c in LEDGER_GLOB_CHARS if c in target[start:])
    posix = paths.posix()
    cut = target.rfind('/', 0, first) if posix else max(target.rfind('/', 0, first), target.rfind('\\', 0, first))
    base, rest = (target[:cut + 1], target[cut + 1:]) if cut >= 0 else ('.', target)
    b = file_key(base)
    if posix:
        # Linux: `/` alone separates, and each part folds as the held path's own does (paths.fold,
        # by the mount that holds it), so both sides compare the same way
        raw = [x.split(':', 1)[0] for x in rest.split('/') if x and x != '.']
        top = paths.norm(os.path.abspath(base), real=True, fold=False).rstrip('/')
        folded = paths.fold('/'.join([top] + raw)).split('/')
        parts = [x.translate(DOS_WILDCARDS) for x in folded[len(folded) - len(raw):]] if raw else []
    else:
        parts = [x.split(':', 1)[0].lower().translate(DOS_WILDCARDS) for x in re.split(r'[\\/]', rest)
                 if x and x != '.']
    wide = any('**' in x or x == '..' for x in parts)
    for kind, real in ledger_guarded(ctx):
        if kind == 'rulebook' and b == real:
            return kind
        if real == b or not jail.inside(real, b):
            continue
        below = real[len(b.rstrip('\\/')) + 1:].split(os.sep)
        shorts = short_parts(real, len(below))
        deeper = kind == 'rulebook' and len(parts) == len(below) + 1
        shallower = above and len(parts) < len(below)
        if wide or ((len(parts) == len(below) or deeper or shallower) and
                    all(n == x or glob_part(n, s, x) for n, s, x in zip(below, shorts, parts))):
            return kind
    return None


def glob_part(name, short, pattern):
    """Whether the wildcard part `pattern` matches a path part, as FindFirstFile matches it: its
    long name `name` or its 8.3 short name `short` (None when it has none), both case folded."""
    return fnmatch.fnmatchcase(name, pattern) or (short is not None and fnmatch.fnmatchcase(short, pattern))


def short_parts(real, depth):
    """The last `depth` parts of the 8.3 short spelling of the path `real` (GetShortPathNameW),
    case folded, to match a wildcard against as FindFirstFile does. A part that does not exist
    yet (a held file not written) is None, and its nearest existing folder gives the parts above
    it; None for every part when the volume gives none (the long names alone)."""
    path, tail = real, 0
    short = _short_path(path)
    while short is None and tail < depth:
        parent = os.path.dirname(path)
        if not parent or parent == path:
            break
        path, tail = parent, tail + 1
        short = _short_path(path)
    need = depth - tail
    parts = re.split(r'[\\/]', short.lower().rstrip('\\/')) if short else []
    head = parts[-need:] if need > 0 and len(parts) >= need else [None] * need
    return head + [None] * tail


def _short_path(path):
    """GetShortPathNameW of `path`, or None when it does not resolve (a missing path)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.WinDLL('kernel32').GetShortPathNameW(path, buf, len(buf))
    except (ImportError, OSError, AttributeError, ValueError):
        return None
    return buf.value if 0 < n < len(buf) else None


def ledger_git(event, ctx, cmd, inv, cwd, role):
    """git run in a work tree that holds part of the ledger (outside git: run in a folder that
    holds it) whose pathspec reaches that part (ledger_kind's files): rm and mv, restore (not
    --staged alone, and only when every long option is one GIT_LONGS names: `--w` is
    `--worktree` to git, and _canon_long leaves a one-letter body as written), checkout-index, and checkout of paths (after `--`, or every positional word
    without one: git reads a word that names no branch as a path). A pathspec reaches a held path
    as kitlib.guard.delegfile.covers reads it (the path, a folder above it, a wildcard whose
    literal folder is above it; a computed word or unread magic always), or is a `.md` file
    directly in the rulebooks' folder (a wildcard there: its literal folder, that folder). A
    branch name is read as a path too (fail closed: `git checkout pm` from
    the folder above the ledger's). Paths read from elsewhere (git_every_path:
    `--pathspec-from-file`, checkout-index `-a` or `--stdin`) reach every path of the tree.
    In such a tree, for the sessions ledger_wide_binds names, the forms that rewrite the whole
    tree are refused too (ledger_git_tree_form: checkout or switch that discards changes, reset
    --hard, --merge or --keep, a stash that is not a read, a clean that is not a dry run with no
    pathspec or one that reaches a held path); and a git call whose directory the guard cannot
    place (a computed -C, GIT_DIR, --git-dir, --work-tree and the like: gc.blind) is refused in
    any of those writing forms while a ledger is configured, since it can act on the ledger's
    tree (fail closed); a read there passes.
    A subcommand the shell computes (`git $x LEDGER.md`), or the one xargs appends to a bare
    `xargs git`, is unread: a tree form (it can be reset --hard), and in a placed tree its words
    are judged as paths. git that xargs runs takes arguments the guard cannot read: a path
    subcommand's are every path (git_every_path's reading), and checkout, switch, reset or clean
    with them is a tree form. In a placed tree, for the same sessions, git that moves the tree
    to other content is a tree form too (ledger_git_moves: switch, checkout of a branch, merge,
    pull, apply that writes the tree, read-tree -u); a checkout whose word names a held path is
    refused as that path."""
    gc = git_call_judged(inv)
    if gc is None:
        return None
    sub, args = gc.sub, gc.rest
    held = ledger_guarded(ctx)
    if not held and not ctx.user().get('ledger_unread'):
        return None
    from . import delegfile
    xargs = gc.inv is not None and gc.inv.via == 'xargs'
    unread = ('a git subcommand the shell computes' if gc.sub_dyn
              else 'the subcommand xargs appends' if xargs and not sub else None)
    short, longs, pos = _opts(sub, args, wide=True)
    tree_form = unread or ledger_git_tree_form(sub, args, short, longs, xargs)
    asked = ledger_git_unread(event, ctx, cmd, sub, args, short, longs, xargs, tree_form, role)
    d = ledger_git_held(event, ctx, cmd, gc, sub, args, short, longs, pos, xargs, unread, tree_form, held, role) \
        if held else None
    return d or asked                           # a held path's refusal before the unread ask


def ledger_git_held(event, ctx, cmd, gc, sub, args, short, longs, pos, xargs, unread, tree_form, held, role):
    """ledger_git's refusal of the git call `gc` over the held ledger paths `held`, or None."""
    from . import delegfile
    if not gc.dir or gc.blind or (gc.unknown and not gc.sub_dyn):
        if not ledger_wide_binds(event, ctx):
            return None
        form = tree_form or ledger_git_path_form(sub, args, short, longs, held, xargs)
        if not form:
            return None
        kind = held[0][0]
        where = '`%s` (%s, in a place the guard cannot tell: it can be the tree of the ledger)' % (bare(cmd), form)
        if checkout_makes_branch(sub, short, longs) and not tree_form:
            return refuse('ledger', role, UNPLACED_BRANCH_WHY % where, unplaced_branch_instead(gc))
        return refuse('ledger', role, STANDING_WHY[kind] % where, ledger_instead(ctx, kind))
    moves = None if tree_form else ledger_git_moves(sub, args, short, longs)
    tree_form = tree_form or moves
    if sub not in ('rm', 'mv', 'restore', 'checkout-index', 'checkout') and not tree_form:
        return None
    if sub == 'restore' and restore_index_only(short, longs) and not xargs:
        return None                             # the index alone
    place = ctx.place(gc.dir)
    top = place.top if place.kind in (repo.MAIN, repo.LINKED) and place.top else gc.dir
    tree = jail.real(top)
    held = [(kind, real) for kind, real in held if jail.inside(real, tree)]
    if not held:
        return None
    if sub == 'stash' and tree_form:
        pos = delegfile.after_dashes(args) or []    # push's pathspecs after `--`; none: the tree
    specs = sub in ('clean', 'stash') and tree_form and ledger_wide_binds(event, ctx)
    if sub == 'checkout' and moves and held_spec(ctx, pos, gc.dir, top, held):
        tree_form = None                        # a word that names a held path: refused as that path below
    if tree_form and ledger_wide_binds(event, ctx) and (not specs or not pos or xargs or all(
            delegfile.spec_real(ctx, w, gc.dir, top) is False for w in pos)):
        kind = held[0][0]                       # no pathspec, or exclusions alone: the tree
        return refuse('ledger', role, STANDING_WHY[kind] % ('`%s` (%s)' % (bare(cmd), tree_form)),
                      ledger_instead(ctx, kind))
    if sub == 'checkout':
        after = delegfile.after_dashes(args)
        pos = after if after is not None else pos
    elif sub not in ('rm', 'mv', 'restore', 'checkout-index') and not unread:
        if not specs:
            return None                         # a tree form the session is not held to
    every = git_every_path(sub, short, longs) or (
        'the paths xargs appends' if xargs and (unread or sub in ('rm', 'mv', 'restore', 'checkout-index',
                                                                 'checkout')) else None)
    if every:
        kind = held[0][0]
        return refuse('ledger', role, STANDING_WHY[kind] % ('`%s` (%s)' % (bare(cmd), every)),
                      ledger_instead(ctx, kind))
    hit = held_spec(ctx, pos, gc.dir, top, held)
    if hit:
        kind, text = hit
        return refuse('ledger', role, STANDING_WHY[kind] % ('`%s` (%s)' % (bare(cmd), text)),
                      ledger_instead(ctx, kind))
    return None


def ledger_git_unread(event, ctx, cmd, sub, args, short, longs, xargs, tree_form, role):
    """ledger_unread for a git call while a ledger key could not be read, wherever it runs (the
    ledger may be in any tree): a path form (rm, mv, restore but the index alone, checkout-index,
    checkout of paths) in any session, and a form that rewrites or moves the tree
    (ledger_git_tree_form, ledger_git_moves) in the sessions ledger_wide_binds names; else None."""
    if not ctx.user().get('ledger_unread'):
        return None
    moves = None if tree_form else ledger_git_moves(sub, args, short, longs)
    path_form = sub in ('rm', 'mv', 'checkout-index') or (
        sub == 'restore' and (xargs or not restore_index_only(short, longs))) or (sub == 'checkout' and not moves)
    if not path_form and not ((tree_form or moves) and ledger_wide_binds(event, ctx)):
        return None
    return ledger_unread(ctx, role, '`%s` (%s)' % (bare(cmd), tree_form or moves or 'a path form'))


def held_spec(ctx, words, cwd, top, held):
    """(kind, text) for the first of the pathspec Words `words` (read from `cwd` in the tree at
    `top`) that reaches a held path ((kind, real) pairs): the path, a folder above it, a computed
    word, or a `.md` file directly in a held rulebooks' folder; an exclusion names none. Else None."""
    from . import delegfile
    for w in words:
        spec = delegfile.spec_real(ctx, w, cwd, top)
        if spec is False:
            continue                            # an exclusion names nothing to write
        for kind, real in held:
            if spec is True or jail.inside(real, spec) or (
                    kind == 'rulebook' and os.path.dirname(spec) == real and spec.endswith('.md')):
                return kind, w.text
    return None


def ledger_wide_binds(event, ctx):
    """Whether the ledger rule's wider refusals hold the session of `event`: a folder above a held
    path or a wildcard that can name one (ledger_rule's `above`), git that rewrites a tree holding
    the ledger (ledger_git_tree_form), and a writing git call in a place the guard cannot tell.
    The one place their sessions are chosen: a marked session but the PM's (marked_not_pm), so
    a subagent, a `plain` or other agent's mark and an unknown mark are bound (fail closed); the
    owner's unmarked session and the PM's pass. Whether to widen them is the owner's open
    question: `return True` here binds every session, as the rest of the ledger rule does (only
    ledger.py writes the ledger); that change is made here alone."""
    return marked_not_pm(event, ctx)


def marked_not_pm(event, ctx):
    """True for a marked session but the PM's: a subagent, or a main session whose launcher mark
    (without the kit plugin's prefix, case ignored) is anything but the owner's and `pm`, an
    unknown mark included. False for the owner's unmarked session and the PM's."""
    if subagent(event):
        return True
    who = agentname.bare_mark(str(ctx.session_role(event) or context.OWNER)).lower()
    return who not in (context.OWNER, 'pm')


def ledger_git_tree_form(sub, args, short, longs, xargs=False):
    """The option or form of a git call (sub, its argument Words, and _opts(wide=True)) that
    rewrites the work tree as a whole, discarding what is there: checkout or switch with -f or
    --force (switch's --discard-changes; any abbreviation git could read as either, an
    ambiguous one too: fail closed), reset --hard, --merge or --keep (delegfile.GIT_RESETS), a
    stash that is not a read (stash_reads), a clean that is not a dry run (clean_dry_run). None
    for any other call. ledger_git judges a clean's pathspecs, and a stash's after `--`, as a
    path form's: one that misses the ledger passes; none, or exclusions alone, is the tree.
    Checkout, switch or reset with a word the shell computes before `--` (not the value of an
    option that names a branch, commands.GIT_SUB_VALUES) can be any of these (`git reset "$m"`),
    and so can checkout, switch, reset or clean that xargs runs (`xargs`: it appends words)."""
    from . import delegfile
    if xargs and sub in ('checkout', 'switch', 'reset', 'clean'):
        return 'arguments xargs appends'
    if sub in ('checkout', 'switch', 'reset'):
        dyn = computed_option_word(sub, args)
        if dyn:
            return 'a word the shell computes (%s)' % dyn
    if sub in ('checkout', 'switch'):
        names = ('force', 'discard-changes') if sub == 'switch' else ('force',)
        if 'f' in short:
            return '-f'
        for n in sorted(longs):
            if n and any(name.startswith(n) for name in names):
                return '--' + n
        return None
    if sub == 'reset':
        hit = sorted(delegfile.GIT_RESETS & longs)
        return '--' + hit[0] if hit else None
    if sub == 'stash':
        return None if stash_reads(args) else 'stash'
    if sub == 'clean':
        return None if clean_dry_run(args) else 'clean'
    return None


def computed_option_word(sub, args):
    """The text of the first word the shell computes among a git call's arguments `args` before
    a literal `--`, skipping the value of an option commands.GIT_SUB_VALUES names for `sub` (a
    branch name: `checkout -b "$b"`), else None."""
    values = cm.GIT_SUB_VALUES.get(sub, ())
    k = 0
    while k < len(args):
        w = args[k]
        if w.text == '--' and not w.dyn:
            return None
        if w.dyn:
            return w.text or '?'
        k += 2 if w.text in values else 1
    return None


def ledger_git_moves(sub, args, short, longs):
    """The form of a git call (sub, its argument Words, and _opts(wide=True)) that moves a placed
    work tree to other content, which can overwrite or take away the ledger's files there:
    switch (but `-c`/`-C <branch>` with no start, which keeps the tree as it is), checkout of a
    branch or commit as switch (checkout_moves), merge, pull,
    apply unless it only reads the patch or writes the index alone (--check, --stat, --numstat,
    --summary, --cached; any other option, -3 or an abbreviation fails closed), and read-tree
    -u. None for any other call. ledger_git judges it only where it can place the tree:
    unplaced, `git switch main` is a plain switch the guard cannot tell from one in another
    repository."""
    if sub == 'switch' and switch_creates_here(args):
        return None
    if sub == 'checkout':
        return checkout_moves(args, longs)
    if sub in ('switch', 'merge', 'pull'):
        return sub
    if sub == 'apply':
        reads = {'check', 'stat', 'numstat', 'summary'}
        if longs <= reads | {'cached'} and longs & (reads | {'cached'}) and '3' not in short:
            return None
        return 'apply'
    if sub == 'read-tree' and 'u' in short:
        return 'read-tree -u'
    return None


def switch_creates_here(args):
    """Whether git switch with arguments `args` only creates a branch at HEAD: exactly `-c`,
    `-C`, `--create` or `--force-create` and its literal name, nothing else (a start point, any
    other option, a computed word: False)."""
    return (len(args) == 2 and not any(w.dyn for w in args)
            and args[0].text in ('-c', '-C', '--create', '--force-create') and not args[1].text.startswith('-'))


def checkout_moves(args, longs):
    """The form of a git checkout (its argument Words, and _opts(wide=True)'s long names) that
    moves the tree as switch does, else None: `--orphan` (any abbreviation of two letters or
    more), whatever follows it, or exactly one positional word before any `--` once the values
    of -b, -B and --conflict (any abbreviation) are left out (`checkout main`, `-`, `-b new
    main`, `-t origin/x`; a word that names no branch fails closed). A `--` with nothing after
    it is no `--` (`checkout main --` moves the tree). None with paths after `--` or two or more
    positional words (a tree-ish and paths, judged as paths), and with none (`checkout`,
    `-b new`: a branch at HEAD keeps the tree, as switch -c does)."""
    from . import delegfile
    if any(len(n) > 1 and 'orphan'.startswith(n) for n in longs):
        return 'checkout --orphan'
    if delegfile.after_dashes(args):
        return None
    pos, k = 0, 0
    while k < len(args):
        t = args[k].text
        k += 1
        if t == '--':
            break
        if t.startswith('--'):
            body, eq, _v = t[2:].partition('=')
            names = _canon_long('checkout', body)
            k += bool(not eq and ((len(names) == 1 and names[0] in GIT_LONG_VALUES)
                                  or (body and 'conflict'.startswith(body))))   # --conflict <style>, --conf too
            continue
        if not t.startswith('-') or t == '-':
            pos += 1
            continue
        body = t[1:]
        for j, c in enumerate(body):
            if c in 'bB':
                k += not body[j + 1:]           # -b <name>; -bname carries it
                break
    return 'checkout' if pos == 1 else None


def ledger_git_path_form(sub, args, short, longs, held=(), xargs=False):
    """The subcommand of a git call that writes the paths it names, where the guard cannot place
    the tree: rm, mv, restore (not the index alone), checkout-index, and a checkout that can
    write paths (checkout_branch_only is False). With `xargs` (git that xargs runs, appending
    words the guard cannot read) a checkout or a restore always can. None for any other call."""
    if sub in ('rm', 'mv', 'checkout-index'):
        return sub
    if sub == 'checkout' and (xargs or not checkout_branch_only(args, short, longs, held)):
        return sub
    if sub == 'restore' and (xargs or not restore_index_only(short, longs)):
        return sub
    return None


def checkout_branch_only(args, short, longs, held=()):
    """Whether a checkout (its argument Words, and _opts(wide=True)) can only switch to a branch
    or a commit, the reading where the guard cannot place the tree; anything else fails closed.
    True for no positional word, or exactly one that is a plain name: not `.`, not computed, no
    wildcard or pathspec magic, no path separator (so `kit/x` stays refused: without the tree the
    guard cannot tell a branch from a folder), no `~` short name, and not a part of a held path
    (`LEDGER.md`, its folders, their 8.3 short names: git reads a word that names no branch as
    a path), nor a `.md` name while a rulebooks' folder is held (a rulebook in it). False with `--`, paths read from elsewhere (git_every_path), -p or --patch (any
    abbreviation git could read as it), or two or more positional words."""
    from . import delegfile
    if delegfile.after_dashes(args) is not None or git_every_path('checkout', short, longs):
        return False
    if 'p' in short or any(n and 'patch'.startswith(n) for n in longs):
        return False
    pos = _opts('checkout', args, wide=True)[2]
    if not pos:
        return True
    if len(pos) > 1:
        return False
    w = pos[0]
    t = w.text
    if (getattr(w, 'dyn', False) or not t or t in ('.', '..') or t.startswith(':')
            or any(c in t for c in '*?[]\\/~')):
        return False
    name = t.rstrip('. ').lower()
    if name.endswith('.md') and any(kind == 'rulebook' for kind, _real in held):
        return False                            # a rulebook: a `.md` file in the held folder
    return name not in held_names(held)


UNPLACED_BRANCH_WHY = ('%s creates a branch and checks out its start, which can rewrite the files of the rulings '
                       'ledger or a rulebook there; only ledger.py writes those, so the guard refuses it where it '
                       'cannot see the tree.')
UNPLACED_BRANCH_INSTEAD = ('run it with a literal `git -C <worktree>` so the guard can place the tree, or create '
                           'the branch with `git switch -c <branch> <start>`.')
BLIND_BRANCH_INSTEAD = ('drop %s (a -C does not undo it, so the guard cannot place the tree), then run it with a '
                        'literal `git -C <worktree>`, or create the branch with `git -C <worktree> switch -c <branch> '
                        '<start>`.')


def unplaced_branch_instead(gc):
    """The Instead of an unplaced checkout that creates a branch (checkout_makes_branch): with
    what points git at a place the text does not show (gc.blind: GIT_DIR, --git-dir and the like),
    drop that first, since a -C cannot undo it; else UNPLACED_BRANCH_INSTEAD."""
    if gc.blind:
        return BLIND_BRANCH_INSTEAD % gc.blind
    return UNPLACED_BRANCH_INSTEAD


def checkout_makes_branch(sub, short, longs):
    """Whether a checkout creates a branch (-b, -B, --orphan, any abbreviation of two letters or
    more git reads as it: `--orph`): its refusal names that, and its Instead the forms the guard
    can judge, not the ledger tool."""
    return sub == 'checkout' and bool('b' in short or 'B' in short or any(
        len(n) > 1 and 'orphan'.startswith(n) for n in longs))


def held_names(held):
    """The lower-case names of every part of each held path ((kind, real) pairs) and of its 8.3
    short name, trailing dots and spaces dropped as Windows drops them."""
    out = set()
    for _kind, real in held:
        for p in (real, _short_path(real)):
            if p:
                out.update(n.rstrip('. ').lower() for n in re.split(r'[\\/]', p) if n)
    return out


def git_every_path(sub, short, longs):
    """The option of a ledger_git call that takes its paths from where the guard cannot read
    them, so they can be every path, else None: `--pathspec-from-file` (rm, restore, checkout; any
    abbreviation git could read as it), and checkout-index's `-a`/`--all` and `--stdin`."""
    if sub in ('rm', 'restore', 'checkout'):
        for n in sorted(longs):
            if len(n) > 1 and 'pathspec-from-file'.startswith(n):
                return '--' + n
    if sub == 'checkout-index':
        if 'a' in short:
            return '-a'
        for n in sorted(longs):
            if n and ('all'.startswith(n) or 'stdin'.startswith(n)):
                return '--' + n
    return None


def ledger_instead(ctx, kind):
    if kind == 'log':
        return ('python %s add --repo <repo> --prov <prov> --q "<question>" --a "<answer>" '
                '--decides "<what it decides>" --source "<source>"' % kit_tool(ctx, 'ledger'))
    return 'python %s conflicts --repo <repo> --topic "<topic>" --key <key> --text "<rule>"' % kit_tool(ctx, 'ledger')


STANDING_WHY = {
    'log': '%s writes the rulings ledger, which only ledger.py writes. Add the entry with the kit\'s '
           'tools/ledger.py add (docs/ledger.md).',
    'rulebook': '%s writes a rulebook, which only ledger.py writes, each rule change paired with a log entry: read '
                'the proposed rule against the others with conflicts, then add it with add --rule KEY --topic '
                'TOPIC --checked HASH, or remove one with rule-drop (docs/ledger.md "Rulebooks").',
    'digest': '%s writes the standing digest, which the rulebooks replace: it is read, never written, until it '
              'goes, and a standing rule goes in a rulebook through ledger.py: conflicts, then add --rule '
              '(docs/ledger.md "Rulebooks").',
}


def ledger_rule(event, ctx, target, role, what, stand_in=False, unread=True):
    """The ledger rule over an edit of `target`: what of the ledger it is (ledger_kind), and, for
    the sessions ledger_wide_binds names, a folder above a held path or a wildcard that can name
    one (`above`), unless the target is a stand-in for what the guard cannot read (edit_target's
    `stand_in`: the folder python runs in), as delegfile.reaches reads folders. A broken machine
    config refuses every subagent write; one with a ledger key the guard could not read asks the
    owner about every main-session write a held path does not refuse (ledger_unread; `unread`
    False leaves that to the caller, which judges it after its other rules: edit_target)."""
    user = ctx.user()
    broken = user.get('broken')
    if broken and role == 'subagent':
        return refuse('ledger', role, '%s: the machine config %s is broken (%s), so the '
                      'guard cannot tell where the rulings ledger is and refuses every subagent write. The owner '
                      'or the PM session fixes it.' % (what, ctx.config_location().display(), broken),
                      'python -m json.tool %s' % quote(ctx.config_location().path))
    kind = ledger_kind(ctx, target, above=not stand_in and ledger_wide_binds(event, ctx)) if target else None
    if kind is None:
        return ledger_unread(ctx, role, what) if unread else None
    return refuse('ledger', role, STANDING_WHY[kind] % ledger_what(what), ledger_instead(ctx, kind))


def ledger_unread(ctx, role, what):
    """While the machine config holds a ledger key the guard could not read (Ctx.user's
    'ledger_unread': the ledger may be at any path), the write `what`: asked of the owner in a
    main session, denied in a subagent's; else None. No kit tool's ledger write is asked: ledger.py
    reads the strict config and refuses while it is broken."""
    why = ctx.user().get('ledger_unread')
    if not why:
        return None
    loc = ctx.config_location()
    if role == 'subagent':
        return refuse('ledger', role, '%s: the machine config %s is broken (%s), so the guard cannot tell where '
                      'the rulings ledger is and refuses every subagent write. The owner or the PM session fixes '
                      'it.' % (what, loc.display(), why), 'python -m json.tool %s' % quote(loc.path))
    return refuse('ledger', role, '%s: the machine config %s is broken (%s), so the guard cannot tell where the '
                  'rulings ledger is, so every write of this session needs the owner\'s yes until it is fixed (only '
                  'ledger.py writes the ledger, and it refuses while the config is broken). To read the machine '
                  'config:' % (what, loc.display(), why), 'python -m json.tool %s' % quote(loc.path), kind=ASK)


def ledger_what(what):
    """`what` for STANDING_WHY, which says what is written: "`cmd` writes X" becomes "`cmd` (X)",
    so the reason does not say "writes" twice; any other `what` is kept."""
    i = what.rfind('` writes ')
    if what.startswith('`') and i > 0:
        return '%s (%s)' % (what[:i + 1], what[i + len('` writes '):])
    return what


def lock_state_dir(ctx):
    """The machine lock's state folder: <home>/.claude/machine-lock (kitlib.lock.store.real_root).
    It may be a junction to the store on another volume; lock_state judges its target, by either
    spelling, through jail.real (tests/guard/test_lock_state_junction.py)."""
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
    data worktree no worktree is offered (ctx.is_pm_data): a sentence saying so, and a read. The
    `<main checkout>` placeholder (git_main_rules, where git cannot place the checkout) is text,
    never a path: placed, it would resolve against the folder the hook runs in."""
    if top and os.path.isabs(top) and ctx.is_pm_data(top):
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


def edit_target(event, ctx, target, cwd, role, what, unknown_why=None, rewrite=None, run=None, redirect=False,
                shell=None, item=False, stand_in=False):
    """Deny an edit of `target` in a main checkout (or a place git cannot tell) by a role that
    may not edit one, and an agent session's write outside its roots (write_jail). `rewrite(path)`
    gives the same edit aimed at `path` (the alternative); `run` the command to rerun from the
    worktree when it writes into the directory it runs in; `redirect`, a shell redirect's target:
    output, so the alternative with no worktree of the session's own is the temp folder; `shell`,
    the dialect of the line `run` is on (the cd line of worktree_alternative); `item`, a target the
    guard cannot place that is a provider item on a line of more than plain calls (ps_provider_word);
    `stand_in`, a target that stands for what the guard cannot read (the folder python runs in, the
    pipeline's paths), which counts as the owner's delegation list only when it is that file
    (delegfile.edit)."""
    d = ledger_rule(event, ctx, target, role, what, stand_in, unread=False) or pm_data_unknown(ctx, target, cwd, role, what) or \
        lock_state(event, ctx, target, role, what) or hub_secret_edit(event, ctx, target, role, what) or \
        delegations_edit(event, ctx, target, role, what, stand_in) or \
        machine_config(event, ctx, target, role, what) or kit_json(event, ctx, target, role, what) or \
        gate_result(event, ctx, target, role, what) or \
        generated_file(ctx, target, role, what) or backlog_edit(event, ctx, target, role, what)
    if d:
        return d
    return main_checkout(event, ctx, target, cwd, role, what, unknown_why, rewrite, run, redirect, shell, item) or \
        write_jail(event, ctx, target, cwd, role, what, rewrite, run) or \
        ledger_unread(ctx, role, what)          # last: any refusal above wins over the unread ask


# A write the guard cannot place: a computed or unread target, code given on the line (python -c,
# node -e), a git or .NET call whose directory or path the text does not tell, a place git cannot
# tell. Its default mode is warn (kitlib.guard.modes): only a write that resolves literally into a
# main checkout or outside an agent session's folders is denied (docs/guard.md "Rule modes").
UNPLACED = 'unplaced'
# the rules a write the guard cannot place would otherwise be refused under (unplaced())
PLACE_RULES = frozenset(['main-checkout', 'write-jail', 'git-main-history', 'git-restore', 'git-checkout',
                         'git-reset-hard', 'git-clean', 'git-stash', 'git-bulk-add', 'git-commit-all',
                         'dev-drive'])


def unplaced(d, role):
    """The refusal `d` of a write the guard cannot place, made again under the rule unplaced (same
    reason, alternative and verdict): None when that rule's mode is warn or off. A Decision of
    any rule outside PLACE_RULES (a protected file, the PM data) is returned as it is."""
    if d is None or d.rule not in PLACE_RULES:
        return d
    tail = ' Instead: %s %s' % (d.instead, tag(d.rule, role))
    what = d.reason[:-len(tail)] if d.reason.endswith(tail) else d.reason
    return refuse(UNPLACED, role, what, d.instead, kind=d.verdict)


def in_git_dir(target):
    """True when `target`, or a folder it is in, is named .git."""
    return '.git' in paths.norm(target).replace('/', '\\').split('\\')


GATE_RESULT = 'kit-gate-result'                 # kitlib.gate.result.DIR_NAME


def gate_result(event, ctx, target, role, what):
    """An agent session's literal write into a repository's gate result folder
    (`<git common dir>/kit-gate-result/`, kitlib.gate.result): the records there tell a waiting
    session how a gate or a landing ended, and only the gate writes them. The folder is matched by
    name under a git directory (a folder named .git, or one holding HEAD and objects)."""
    if not target or not agent_session(event, ctx):
        return None
    for p in (target, jail.real(target)):
        parts = paths.norm(p, fold=False).replace('/', '\\').split('\\')
        for k in range(1, len(parts)):
            if parts[k].lower() != GATE_RESULT:
                continue
            gitdir = ('/' if paths.posix() else '\\').join(parts[:k])
            if parts[k - 1].lower() == '.git' or (os.path.isfile(os.path.join(gitdir, 'HEAD')) and
                                                  os.path.isdir(os.path.join(gitdir, 'objects'))):
                return refuse('gate-result', role, '%s: %s is in the gate result folder %s, which only the '
                              'gate writes (kitlib.gate.result); a record written by hand would tell a waiting '
                              'session a gate or a landing ended as it did not. To wait for a result:'
                              % (what, fwd(target), fwd(os.path.join(gitdir, parts[k]))),
                              'python %s wait-result <branch> --tip <sha>' % kit_tool(ctx, 'gate'))
    return None


# The refusal of a provider item the guard reads as a file only because of the line around it
# (ps_provider_word): it says so in one sentence, and its Instead runs the whole line from the
# worktree (whole_line)
PROVIDER_ITEM_WHY = ('%s, an item on a drive that holds no files, which the guard skips only on a line of plain '
                     'calls with literal words, so on this line it is a file the guard cannot place; run the whole '
                     'line from your worktree:')


def main_checkout(event, ctx, target, cwd, role, what, unknown_why=None, rewrite=None, run=None, redirect=False,
                  shell=None, item=False):
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
    no_wt, new_wt = new_worktree(ctx, main_top)
    if target is None:
        mine = own_worktree(ctx, cwd)
        if item and run and main_top and not no_wt:
            return refuse(UNPLACED, role, PROVIDER_ITEM_WHY % what,
                          cd_line(fwd(mine) if mine else '<worktree>', run, shell or sh.PS))
        only = ''
        if rewrite is not None and (mine or redirect):
            instead = rewrite(fwd(mine or ctx.tempdir) + '/<file>')
        elif no_wt or not main_top:
            instead = new_wt
        else:
            # from the main checkout the session's worktree is unknown, not missing (only_new)
            only = only_new(new_wt)
            instead = rewrite('<worktree>/<file>') if rewrite is not None else \
                'git -C %s worktree list' % quote(main_top)
        return refuse(UNPLACED, role, '%s: the guard cannot tell where it writes (%s). Name a literal path '
                      'inside your worktree%s (the Edit or Write tool for a file)%s.%s' %
                      (what, unknown_why or 'unknown path', ' or, for output, the temp folder' if redirect else '',
                       only, '' if rewrite is not None or not only else ' To see the worktrees:'),
                      instead)
    place = ctx.place(target)
    if place.kind not in (repo.MAIN, repo.UNKNOWN):
        return None
    if place.kind == repo.UNKNOWN:
        mine = own_worktree(ctx, cwd)
        name, only, see = os.path.basename(target), '', ''
        if mine and rewrite is not None:
            instead = rewrite(fwd(os.path.join(mine, name)))
        elif no_wt or not main_top:
            instead = new_wt
        else:
            # a new worktree is never the Instead: from the main checkout only in a clause (only_new)
            only = '' if mine else only_new(new_wt)
            if rewrite is not None:
                instead = rewrite('<worktree>/' + name)
            else:
                instead, see = 'git -C %s worktree list' % quote(main_top), ' To see the worktrees:'
        # a git directory is placed by its name: git answers no work tree there, and its config,
        # hooks and refs are the repository's own
        rule = 'main-checkout' if in_git_dir(target) else UNPLACED
        return refuse(rule, role, '%s: git cannot tell whether %s is in a main checkout (%s); '
                      'this role may not edit one. Write inside a path git reports as a linked worktree%s.%s' %
                      (what, target, place.detail, only, see), instead)
    what2, instead = worktree_alternative(ctx, target, place.top, cwd, rewrite, run, redirect, shell)
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


MACHINE_CONFIG = 'machine-config'
MACHINE_CONFIG_WHY = ('a .pm-kit folder at the top of a checkout is the machine config folder the PM repository '
                      'carries (docs/machine-setup.md): its config sets the guard\'s rules for every session on this '
                      'machine and its hub folder holds the hub\'s tokens, so only the owner changes it.')


def machine_config_folder(ctx, target):
    """The machine config folder the write target `target` is or lies in, as written, or None:
    the PM repository's, <pointer>/.pm-kit (jail.pointer_config_dir, compared as jail.real gives
    both), else a folder named .pm-kit (kitlib.config.user.DIR_NAME, case ignored) at the top of
    the checkout git places the target in (Ctx.place: a main checkout or a linked worktree),
    whatever the pointer says. A .pm-kit folder deeper in a checkout, a folder that only holds
    one, and one outside git are not: the home folders are the write jail's (jail.touches_config),
    and a delete of a folder that holds the owner's delegation list is that rule's."""
    t = hub_real(target)
    if t is None:
        return None
    folder = jail.pointer_config_dir(ctx)
    r = hub_real(folder) if folder else None
    if r and jail.inside(t, r):
        return folder
    place = ctx.place(target)
    if place.kind not in (repo.MAIN, repo.LINKED) or not place.top:
        return None
    top = hub_real(place.top)
    if top is None or t == top or not jail.inside(t, top):
        return None
    first = t[len(top.rstrip('\\/')) + 1:].split(os.sep)[0]
    if first.lower() != kuser.DIR_NAME.lower():
        return None
    return os.path.join(place.top, kuser.DIR_NAME)


def machine_config(event, ctx, target, role, what):
    """machine-config: an edit of a file in the PM repository's machine config folder, or in a
    .pm-kit folder at the top of a checkout (machine_config_folder), in any session but the
    owner's own. The hub's token folder in it is hub-secrets' and the owner's delegation list
    its own rule's, both checked first (edit_target)."""
    if not target or is_owner_session(event, ctx):
        return None
    folder = machine_config_folder(ctx, target)
    if folder is None:
        return None
    return machine_config_refusal(event, ctx, role, '%s is in %s' % (what, quote(folder)), folder)


def machine_config_refusal(event, ctx, role, what, folder):
    """The machine-config refusal of `what`, a change in the machine config folder `folder`
    (machine_config; kitlib.guard.delegfile's wildcard and git checks)."""
    return refuse(MACHINE_CONFIG, role, '%s: %s This session is %s. Ask the owner (through the PM) to make the '
                  'change. To list the folder:' % (what, MACHINE_CONFIG_WHY, hub_session(event, ctx)),
                  'Get-ChildItem -Force %s' % quote(folder))


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
    return jail_view(event, ctx, cwd)[0]


# The outer bound on the write jail (docs/guard.md "Write jail", "Outer bound"): an agent session's
# write the roots allow is refused when it is off the Dev Drive (the machine config's dev_drive).
DEV_DRIVE = 'dev-drive'


def jail_view(event, ctx, cwd):
    """(roots, dev, dropped) for an agent session running in `cwd`: its write roots (jail.roots),
    the Dev Drive's root while the outer bound judges (rule dev-drive not off; None when dev_drive
    is unset) and, while it denies, the data and mark roots dropped for lying off the Dev Drive,
    each said as a warning naming the entry and its path."""
    m = modes.mode(DEV_DRIVE)
    dev = None if m == modes.OFF else jail.dev_root(ctx)
    dropped = []
    rs = jail.roots(ctx, cwd, lambda p: is_root(p, None, ctx), session_mark(event, ctx),
                    dev=dev if m == modes.DENY else None, dropped=dropped)
    for r in dropped:
        modes.note('the %s root %s is not on the Dev Drive (%s), so it is no write root: an agent session writes only '
                   'on the Dev Drive. The owner moves the folder onto the Dev Drive and the entry with it (%s).'
                   % (r.kind, quote(r.path), fwd(dev),
                      '.kit.json guard.data_roots' if r.kind == jail.DATA else 'the machine config\'s guard_mark_roots'))
    return rs, dev, dropped


JAIL_ASK = 'Ask the coordinator (a coordinator asks the PM) when the work needs a folder outside them.'


def jail_aim(event, ctx, cwd, dest, rewrite):
    """The folder a rewrite names in place of the jail's destination `dest`, or None: when `dest`
    is a main checkout this role may not edit (main-checkout refuses the same write there), its
    own worktree of it, <worktree> when the call shows none."""
    if dest is None or rewrite is None:
        return None
    place = ctx.place(dest.path)
    try:
        barred = place.kind == repo.MAIN and not kconfig.permissions(
            event, kconfig.roles(ctx.config(place.top or dest.path))).get('edit_main_checkout')
    except Exception:  # noqa: BLE001 - the advice only: a config that does not read leaves it as it was
        barred = False
    return (own_worktree(ctx, cwd, place.top) or '<worktree>') if barred else None


def dev_drive_bound(event, ctx, cwd, rs, dev, target, role, what, rewrite=None, run=None):
    """The outer bound: `target`, which the jail's roots allow (or a dropped root holds), refused
    when it is not on the Dev Drive `dev` (jail.on_drive: links and junctions resolved). None when
    `dev` is None (dev_drive unset, or the rule off) or the target is on it. The alternative aims
    at the session's own checkout, else temp, on the Dev Drive (jail.destination)."""
    if not dev or not target or jail.on_drive(target, dev):
        return None
    why = ('%s: %s is not on the Dev Drive (%s): an agent session writes only there, whatever folder it may '
           'otherwise write. %s' % (what, fwd(target), fwd(dev), JAIL_ASK))
    dest = jail.destination(rs, dev)
    if dest is None:
        return refuse(DEV_DRIVE, role, why + ' To see where this session runs:', 'pwd')
    if rewrite is not None:
        name = os.path.basename(target.rstrip('\\/')) or 'out'
        there = jail_aim(event, ctx, cwd, dest, rewrite) or dest.path
        return refuse(DEV_DRIVE, role, why + ' The same write in %s:' % fwd(there),
                      rewrite(fwd(there) + '/' + name if there.startswith('<') else fwd(os.path.join(there, name))))
    return refuse(DEV_DRIVE, role, why + ' Run it with %s as the working directory:' % quote(dest.path),
                  run or 'cd %s' % quote(dest.path))


def in_dropped(target, dropped, dev):
    """True when `target` is off the Dev Drive `dev` and lies in a root jail_view dropped for lying
    off it. A dropped root that holds the Dev Drive (Z:/ around Z:/dev) opens nothing on it: a
    target there is judged by the other roots alone."""
    if not dropped or jail.on_drive(target, dev):
        return False
    t = jail.real(target)
    return any(jail.inside(t, r.real) for r in dropped)


def write_jail(event, ctx, target, cwd, role, what, rewrite=None, run=None):
    """An agent session's write outside its roots (kitlib.guard.jail): its checkout, its
    repository's other worktrees (jail.sibling), temp and the .kit.json data roots, judged by the
    resolved path; then, last, the outer bound (dev_drive_bound) on a write they allow."""
    if not target or not agent_session(event, ctx):
        return None
    rs, dev, dropped = jail_view(event, ctx, cwd)
    verdict, root = jail.judge(target, rs)
    if verdict is None or (verdict == jail.OUTSIDE and (jail.sibling(ctx, target, cwd) or in_dropped(target, dropped, dev))):
        return dev_drive_bound(event, ctx, cwd, rs, dev, target, role, what, rewrite, run)
    aim = jail_aim(event, ctx, cwd, jail.destination(rs), rewrite)
    return jail_refusal(role, rs, verdict, root, what, target, rewrite, run, aim)


def jail_why(rs, verdict, root, what, target):
    """The sentence a write-jail refusal of `target` opens with, for its verdict."""
    if verdict == jail.ADD_ONLY:
        return ('%s: %s already exists in %s, an add-only data folder: an agent session creates new files there and '
                'never overwrites, moves or deletes one. %s' % (what, fwd(target), fwd(root.path), JAIL_ASK))
    if verdict == jail.READ_ONLY:
        return ('%s: %s is in %s, a data folder that is read-only for agent sessions (.kit.json guard.data_roots). %s'
                % (what, fwd(target), fwd(root.path), JAIL_ASK))
    return ('%s: %s is outside the folders an agent session may write: %s. %s'
            % (what, fwd(target), jail.describe(rs), JAIL_ASK))


def jail_refusal(role, rs, verdict, root, what, target, rewrite=None, run=None, aim=None):
    """The write-jail refusal; `aim`, when given, is the folder the rewrite names in place of the
    jail's destination (write_jail: a main checkout the role may not edit)."""
    dest = jail.destination(rs)
    why = jail_why(rs, verdict, root, what, target)
    if verdict == jail.ADD_ONLY:
        if rewrite is not None:
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            return refuse('write-jail', role, why + ' To write a new file:',
                          rewrite(fwd(folder) + '/<new file name>'))
        return refuse('write-jail', role, why + ' To see the folder\'s files:', 'ls %s' % quote(root.path))
    if dest is None:
        return refuse('write-jail', role, why + ' To see where this session runs:', 'pwd')
    if rewrite is not None:
        name = os.path.basename(target.rstrip('\\/')) or 'out'
        there = aim or dest.path
        return refuse('write-jail', role, why + ' The same write in %s:' % fwd(there),
                      rewrite(fwd(there) + '/' + name if there.startswith('<') else fwd(os.path.join(there, name))))
    return refuse('write-jail', role, why + ' Run it with %s as the working directory:' % quote(dest.path),
                  run or 'cd %s' % quote(dest.path))


def worktree_alternative(ctx, target, main_top, cwd, rewrite=None, run=None, redirect=False, shell=None):
    """(sentence, command): the same edit in the session's own worktree of this main checkout, or
    a new worktree to make it in; a redirect's output with no worktree of the session's own, the
    same redirect into the temp folder; a run that writes through the main checkout it runs in,
    the same run from the worktree, its cd in the same call (cd_line)."""
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
    if redirect and rewrite is not None:
        name = os.path.basename(target.rstrip('\\/')) or 'out'
        return REDIRECT_HINT, rewrite(fwd(os.path.join(ctx.tempdir, name)))
    if rewrite is None and run is not None and cwd and paths.norm(target) == paths.norm(cwd):
        # it writes through the main checkout it runs in
        return MAIN_CWD_HINT % instead, cd_line('<worktree>', run, shell)
    if rewrite is not None:
        # the session may have a worktree the call does not run in: aim at it, a new one only for a session with none
        there = '' if rel == '.' else ' (%s there)' % fwd(rel)
        return ('Make the change in your worktree%s%s; the lander brings it to main:' % (there, only_new(instead)),
                rewrite('<worktree>' if rel == '.' else '<worktree>/' + fwd(rel)))
    # no word to aim (cp -t<dir>): the worktrees to make it in; a new one only in a clause (only_new)
    there = '' if rel == '.' else ' (%s there)' % fwd(rel)
    return ('Make the change in your worktree%s%s; the lander brings it to main. To see the worktrees:'
            % (there, '' if mine else only_new(instead)), 'git -C %s worktree list' % quote(main_top))


def only_new(new_wt):
    """The clause a refusal from the main checkout offers a new worktree in: the call does not
    say whether the session has one, so the Instead aims at <worktree> and only a session with
    none makes one: the new worktree is never the Instead itself."""
    return '; only a session with no worktree makes one, first (%s)' % new_wt


REDIRECT_HINT = ('A redirect writes output: send it to the temp folder (a change to the repository is made in a '
                 'worktree of it, which the lander brings to main):')
# commands whose file is the output they are given, like a redirect's (their refusal names the temp folder)
OUTPUT_WRITERS = ('out-file', 'tee-object', 'tee')
MAIN_CWD_HINT = ('It writes through its working directory, which is the main checkout: run it from your worktree, '
                 'with the cd in the same call (no tool keeps a cd between calls), or run a script or module by '
                 'absolute path (python <worktree>/tools/x.py -C <worktree>; python -m unittest discover -s '
                 '<worktree>/tests/<pkg> -t <worktree>). To read files, use the Read and Grep tools. Only a session '
                 'with no worktree makes one, first (%s). From your worktree:')


def cd_line(where, run, shell):
    """`run` from the folder `where`, the cd in the same call: PowerShell runs it only when the cd
    worked (`if ($?)`), any other shell after `&&`."""
    if shell == sh.PS:
        return 'cd "%s"; if ($?) { %s }' % (where, run)
    return 'cd "%s" && %s' % (where, run)


def outer_shell(cmd):
    """The dialect of the line `cmd` is on: its outermost shell (a bash -c inside PowerShell is on a
    PowerShell line)."""
    while cmd.nested_in is not None:
        cmd = cmd.nested_in
    return cmd.shell


def whole_line(ctx, cmd):
    """The text of the whole line `cmd` is on, as its outermost shell reads it (outer_shell), for
    a refusal that runs it from the worktree (cd_line): a cd into the top of a main checkout at
    its start (each one before the first other command, a ; or a line break after it) is left
    out, as is a comment after its last command. A line with any other cd, or a Pop-Location,
    would move off the worktree: it gives the call alone (bare)."""
    outer = cmd
    while outer.nested_in is not None:
        outer = outer.nested_in
    src = outer.source or ''
    _, cmds = sh.parse(src, outer.shell, outer.cwd)
    top = [c for c in cmds if c.depth == 0 and c.start is not None]
    if not top:
        return src.strip()
    start = top[0].start
    for i, c in enumerate(top):
        is_cd, where = sh.cd_target(c, outer.cwd)
        if not is_cd:
            break
        place = ctx.place(where) if where else None
        after = src[c.end:top[i + 1].start] if i + 1 < len(top) else None
        if (place is None or place.kind != repo.MAIN or paths.norm(where) != paths.norm(place.top)
                or after is None or after.strip() not in ('', ';')):
            return bare(cmd)
        start = top[i + 1].start
    if any(sh.cd_target(c, outer.cwd)[0] for c in cmds if c.start is None or c.start >= start):
        return bare(cmd)
    end = max(c.end for c in top)
    tail = src[end:]
    cut = tail.find('#')
    if cut >= 0:
        tail = tail[:cut - 1 if cut and tail[cut - 1] == '<' else cut]
    return (src[start:end] + tail).strip()


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
    writes (`'*'` in pos: every further positional binds to the last slot). `piped`: the groups of
    those it binds from the pipeline when the line binds no parameter of the group (a path the
    text does not show, PIPE_UNPLACED). `here`: the parameter whose absence makes it write the
    location it runs in (Copy-Item with no -Destination). `provider`: a provider-aware item or
    content cmdlet, whose item on a drive that holds no files is no file (PS_PROVIDER_CMDLETS)."""

    def __init__(self, values, switches=(), pos=(), writes=(), aliases=None, piped=(), here=None, provider=False):
        self.values = tuple(values) + PS_COMMON_VALUES
        self.switches = tuple(switches) + PS_COMMON_SWITCHES
        self.pos, self.writes = tuple(pos), tuple(writes)
        self.aliases = dict(PS_COMMON_ALIASES, **(aliases or {}))
        self.piped, self.here, self.provider = tuple(piped), here, provider

    def names(self):
        return self.values + self.switches


_PATHS = ('path', 'literalpath')
_FILTERS = ('filter', 'include', 'exclude', 'credential')
PS_CMDLETS = {
    'out-file': PSCmdlet(('filepath', 'literalpath', 'encoding', 'width', 'inputobject'),
                         ('append', 'force', 'noclobber', 'nonewline'), ('filepath', 'encoding'),
                         ('filepath', 'literalpath'), {'path': 'filepath'}),
    'set-content': PSCmdlet(_PATHS + _FILTERS + ('value', 'encoding', 'stream'),
                            ('passthru', 'force', 'nonewline', 'asbytestream'), ('path', 'value'), _PATHS,
                            piped=(_PATHS,), provider=True),
    'add-content': PSCmdlet(_PATHS + _FILTERS + ('value', 'encoding', 'stream'),
                            ('passthru', 'force', 'nonewline', 'asbytestream'), ('path', 'value'), _PATHS,
                            piped=(_PATHS,), provider=True),
    'clear-content': PSCmdlet(_PATHS + _FILTERS + ('stream',), ('force',), ('path',), _PATHS,
                              piped=(_PATHS,), provider=True),
    'tee-object': PSCmdlet(('filepath', 'literalpath', 'variable', 'inputobject', 'encoding'), ('append',),
                           ('filepath',), ('filepath', 'literalpath'), {'path': 'filepath'}),
    'new-item': PSCmdlet(('path', 'name', 'itemtype', 'value', 'credential'), ('force',), ('path',), ('path',),
                         {'type': 'itemtype'}, piped=(('path',),), provider=True),
    'remove-item': PSCmdlet(_PATHS + _FILTERS + ('stream',), ('recurse', 'force'), ('path', '*'), _PATHS,
                            piped=(_PATHS,), provider=True),
    # the paths Copy-Item takes from the pipeline are what it reads; its -Destination is the write
    'copy-item': PSCmdlet(_PATHS + _FILTERS + ('destination', 'tosession', 'fromsession'),
                          ('container', 'force', 'recurse', 'passthru'), ('path', 'destination'), ('destination',),
                          piped=(('destination',),), here='destination', provider=True),
    'move-item': PSCmdlet(_PATHS + _FILTERS + ('destination',), ('force', 'passthru'), ('path', 'destination'),
                          _PATHS + ('destination',), piped=(_PATHS, ('destination',)), here='destination',
                          provider=True),
    'rename-item': PSCmdlet(_PATHS + ('newname', 'credential'), ('force', 'passthru'), ('path', 'newname'), _PATHS,
                            piped=(_PATHS,), provider=True),
    'expand-archive': PSCmdlet(_PATHS + ('destinationpath',), ('force', 'passthru'), ('path', 'destinationpath'),
                               ('destinationpath',), here='destinationpath'),
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
                                 ('path', 'name', 'value'), _PATHS, piped=(_PATHS,), provider=True),
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
                'unlink', 'shred', 'dos2unix', 'unix2dos', 'mac2unix', 'unix2mac', 'sponge'}
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


# Why a path a cmdlet takes from the pipeline is one the guard cannot place
PIPE_UNPLACED = 'the text does not show what the command before the | sends'
PIPE_WORD = '(a path from the pipeline)'


def pipe_stand_in(cmd):
    """The Word write_targets gives for a path a PowerShell cmdlet takes from the pipeline
    (PSCmdlet piped): no text to place, at the command's end (pipe_stand_in_word)."""
    return sh.Word(PIPE_WORD, False, cmd.end, cmd.end)


def pipe_stand_in_word(cmd, w):
    """True when the write `w` is pipe_stand_in(cmd)."""
    return w.text == PIPE_WORD and not w.dyn and w.start == w.end == cmd.end


class NetWrite(object):
    """One path a .NET file call writes (net_writes): `word` its argument, dyn when the line
    computes it (a variable, an expression, a string with $ or a backtick), with its span in the
    source; `end` the offset just past the call's closing parenthesis; `paths` every path argument
    of the call, written or read (Copy's source too), `word` among them (net_retarget)."""
    __slots__ = ('word', 'end', 'paths')

    def __init__(self, word, end, paths=None):
        self.word, self.end, self.paths = word, end, paths if paths is not None else [word]


NET_QUOTES = '‘’‚‛“”„'     # PowerShell reads these as quotes too


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


def _net_ctor(src, kind, args, end, out):
    """Add the path a StreamWriter or FileStream constructor with `args` writes to `out`: its first
    argument. True when the guard does not read it: no arguments, or a StreamWriter's first argument
    that is not a plain string (a Stream it writes through, judged where it is opened, or a path the
    line computes). A FileStream opened only to read counts as a write (docs/guard-v2.md v2:114)."""
    if not args:
        return True
    text, s, e = args[0]
    if text is None and (kind == 'streamwriter' or src[s:e].lower() == '$null'):
        return kind == 'streamwriter'
    out.append(NetWrite(sh.Word(src[s:e] if text is None else text, text is None, s, e), end))
    return False


def net_writes(cmd):
    """([NetWrite], unread): each path the PowerShell command's .NET file calls (IO_FILE_RE) and
    StreamWriter and FileStream constructors (NET_TYPE_RE: ::new(...) or New-Object) write, and True
    when a match is a write the guard does not read the path of, so write_targets judges the
    directory the command runs in, as a path it cannot place: one inside a quoted word, a method named with no '(' after it (called
    later, through .Invoke or a variable), a call with fewer arguments than the ones it writes
    (_net_written), one whose arguments the parse may have lost its place in (_net_lost), a type
    named any other way (a cast, [type] as a value) and a New-Object call _net_new_object does
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
            unread = True                       # [type] passed as a value, a cast
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
        words = {}
        for i in sorted(set(written) | ({0} if method.startswith('copy') and args else set())):
            text, s, e = args[i]
            if text is None and src[s:e].lower() == '$null':
                continue
            words[i] = sh.Word(src[s:e] if text is None else text, text is None, s, e)
        for i in written:
            if i in words:
                out.append(NetWrite(words[i], end, [words[j] for j in sorted(words)]))
    for x in out:
        m = None if x.word.dyn else NET_DEVICE_RE.match(x.word.text)
        if m:                                   # \\?\<drive>:\x is <drive>:\x; the span stays the argument's
            x.word = sh.Word(x.word.text[m.end():], False, x.word.start, x.word.end)
    return out, unread


def net_placed(w):
    """True when the .NET argument Word `w` is a literal absolute path on a drive (NET_ABS_RE;
    net_writes reads \\\\?\\<drive>:\\x as <drive>:\\x). A share (\\\\host\\<drive>$\\x) is not: the guard cannot tell
    which folder it is, so write_targets judges it as a path it cannot place."""
    return not w.dyn and bool(NET_ABS_RE.match(w.text))


def net_relative(w):
    """True when the .NET argument Word `w` is a literal relative path (no drive, no leading slash,
    no ~): write_targets judges it where the PowerShell location puts it, as a bash one is, though
    .NET reads it against the process's directory (NET_PROCESS_DIR), which the guard cannot see;
    so an agent session's is refused as well (net_process_dir)."""
    return not w.dyn and bool(w.text) and w.text[0] not in '\\/~' and ':' not in w.text


# where .NET reads a path that is not absolute
NET_PROCESS_DIR = ('.NET reads a relative or driveless path against the process\'s working directory '
                   '([Environment]::CurrentDirectory), not the PowerShell location')
# why a .NET write the guard cannot place is counted as a write to the directory the line runs in
NET_UNPLACED = ('a .NET call whose path the guard does not read or cannot place: a driveless path, a share or '
                'device path, or one the line computes. ' + NET_PROCESS_DIR)


def net_process_dir(event, ctx, role, what, relative, target, rewrite):
    """An agent session's .NET write whose call names a literal relative path (net_relative, the
    texts `relative`: the path written, or Copy's source), already judged where the PowerShell
    location puts it (`target`): refused all the same, under write-jail, since .NET reads it
    against the process's directory, which Set-Location does not move and the guard cannot see (a
    docs-writer's [IO.File]::WriteAllText('docs/ROSTER.md', ...) from its worktree wrote the main
    checkout). The alternative is the same call with every relative path the absolute one the
    location gives (net_retarget), which .NET reads the same from any directory."""
    if not agent_session(event, ctx):
        return None
    return refuse('write-jail', role, '%s; the call names the relative path %s: .NET reads it against the process\'s '
                  'working directory ([Environment]::CurrentDirectory), which cd and Set-Location do not move, so '
                  'the guard cannot tell what it names. An agent session gives .NET absolute paths. The same call '
                  'with the paths the PowerShell location gives:' % (what, ', '.join(relative)), rewrite(fwd(target)))


def net_retarget(cmd, x, path, place=None):
    """The command `cmd` through the end of its .NET call, with the NetWrite `x`'s argument
    replaced by `path` and, with `place` (Word -> path or None), each other literal relative path
    argument of the call (net_relative: Copy's source, Move's other path) by the path it gives, so
    the alternative names no path .NET would read from the process's directory."""
    start = cmd.words[0].start if cmd.words else cmd.start
    out, k = [], start
    for o in sorted(x.paths, key=lambda o: o.start):
        own = (o.start, o.end) == (x.word.start, x.word.end)     # net_writes may have re-read x.word (a device path)
        new = path if own else place(o) if place is not None and net_relative(o) else None
        if new is None:
            continue
        out.append(cmd.source[k:o.start] + literal_word(new))
        k = o.end
    return (''.join(out) + cmd.source[k:x.end]).strip()


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


# gawk 5's options (its getopt list "+F:f:v:W;bcCd::D::e:E:ghi:l:L::nNo::Op::MPrSstVYZ:", options
# ending at the first operand): the short ones that take a value, the ones whose value is only the
# rest of their word, and the long names (a unique prefix names one) that take a value, take one
# only after `=`, or take none
AWK_VALUE_SHORTS = 'FfvieElZ'
AWK_OPTIONAL_SHORTS = 'dDLop'
AWK_LONGS = {'assign': 'v', 'exec': 'E', 'field-separator': 'F', 'file': 'f', 'include': 'i', 'load': 'l',
             'source': 'e', 'locale': 'Z'}
AWK_OPTIONAL_LONGS = {'dump-variables': 'd', 'debug': 'D', 'lint': 'L', 'pretty-print': 'o', 'profile': 'p'}
AWK_FLAG_LONGS = ('bignum', 'characters-as-bytes', 'copyright', 'csv', 'gen-pot', 'help', 'lint-old',
                  'no-optimize', 'non-decimal-data', 'optimize', 'parsedebug', 'persist', 'posix',
                  're-interval', 'sandbox', 'trace', 'traditional', 'use-lc-numeric', 'version')
AWK_ASSIGN_RE = re.compile(r'^[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?=')   # an operand gawk reads as var=value


def _awk_inplace_lib(w):
    """True when the -i/-f/-l value Word `w` may load gawk's inplace library: its name, or a value
    the line computes (fail closed)."""
    if w is None or w.dyn:
        return w is not None
    base = re.split(r'[\\/]', w.text)[-1].lower()
    return base in ('inplace', 'inplace.awk')


def _awk_inplace_text(w):
    """True when the program text Word `w` may @include the inplace library."""
    return w is not None and not w.dyn and '@include' in w.text and 'inplace' in w.text


def _awk_long(name):
    """The letter the long option `name` (without its dashes) stands for, or None when gawk does
    not know it or it is ambiguous (gawk then runs nothing)."""
    names = list(AWK_LONGS) + list(AWK_OPTIONAL_LONGS) + list(AWK_FLAG_LONGS)
    if name in names:
        hits = [name]
    else:
        hits = [n for n in names if n.startswith(name)]
    if len(hits) != 1:
        return None
    return AWK_LONGS.get(hits[0]) or AWK_OPTIONAL_LONGS.get(hits[0]) or ''


def _awk(args, cmd_word):
    """What awk/gawk writes: every file operand under the inplace library (`-i inplace` in any
    spelling, `-f inplace`, `-l inplace`, `@include "inplace"` in the program text; counted even
    with -v inplace::enable=0), and the file -d, -o or -p names (else the directory it runs in,
    for awkvars.out or awkprof.out). A run that is neither writes nothing."""
    in_place, program_given, dumps = False, False, []
    k = 0

    def take(letter, value):
        nonlocal in_place, program_given
        if letter in 'ifl' and _awk_inplace_lib(value):
            in_place = True
        if letter in 'feE':
            program_given = True
        if letter == 'e' and _awk_inplace_text(value):
            in_place = True
        if letter in 'dop':
            dumps.append(value if value is not None and value.text else here(cmd_word))

    def word(t, w):
        return sh.Word(t, w.dyn, w.start, w.end)

    while k < len(args):
        w = args[k]
        t = w.text
        if t == '--':
            k += 1
            break
        if not t.startswith('-') or len(t) < 2:
            break
        k += 1
        if t.startswith('--'):
            spec = t[2:]
        else:
            body, spec = t[1:], None
            for j, c in enumerate(body):
                rest = body[j + 1:]
                if c == 'W':
                    if rest:
                        spec = rest
                    elif k < len(args):
                        spec = args[k].text
                        w = args[k]
                        k += 1
                    break
                if c in AWK_VALUE_SHORTS:
                    if rest:
                        value = word(rest, w)
                    else:
                        value = args[k] if k < len(args) else None
                        k += 1
                    take(c, value)
                    break
                if c in AWK_OPTIONAL_SHORTS:
                    take(c, word(rest, w) if rest else None)
                    break
            if spec is None:
                if c == 'E':
                    break                       # -E file: the rest are the program's arguments
                continue
        name, eq, val = spec.partition('=')     # --name[=value], or -W name[=value]
        letter = _awk_long(name)
        if letter is None or letter == '':
            continue
        if eq:
            value = word(val, w)
        elif letter in AWK_OPTIONAL_SHORTS:
            value = None
        else:
            value = args[k] if k < len(args) else None
            k += 1
        take(letter, value)
        if letter == 'E':
            break
    rest = args[k:]
    if not program_given and rest:
        if _awk_inplace_text(rest[0]):
            in_place = True
        rest = rest[1:]
    if not in_place:
        return dumps
    files = [w for w in rest if w.dyn or (w.text != '-' and not AWK_ASSIGN_RE.match(w.text))]
    return dumps + files


def _ruby(args, cmd_word):
    """ruby -i[ext]: the files it edits, as perl -i (after the script unless -e gives the code),
    placed under each -C or -x folder in turn; without -i, code on the line (_evals)."""
    code = _evals('ruby', args, cmd_word)
    in_place, script, pos, dirs = False, False, [], []
    k = 0
    while k < len(args):
        w = args[k]
        t = w.text
        if t == '--':
            pos.extend(args[k + 1:])
            break
        if t.startswith('--') or t == '-':
            if t == '-':
                pos.append(w)
            k += 1
            continue                            # a long option: any value it takes is an operand (fail closed)
        if t.startswith('-') and len(t) > 1:
            body = t[1:]
            k += 1
            for j, c in enumerate(body):
                rest = body[j + 1:]
                if c == 'i':
                    in_place = True
                    break                       # -i.bak: the rest is the suffix
                if c in 'eIrCE':
                    script = script or c == 'e'
                    value = sh.Word(rest, w.dyn, w.start, w.end) if rest else (args[k] if k < len(args) else None)
                    if not rest:
                        k += 1
                    if c == 'C' and value is not None:
                        dirs.append(value)
                    break
                if c == 'x':
                    if rest:
                        dirs.append(sh.Word(rest, w.dyn, w.start, w.end))
                    break
                if c in 'FWT0K':
                    break                       # their value is the rest of the word
            continue
        pos.append(w)
        k += 1
    if not in_place:
        return code
    files = pos if script else pos[1:]
    for d in reversed(dirs):
        files = [_under(d, f) for f in files]
    return code + files


# vi, vim, nvim, ex and ed: a scripted run writes the files it is given, so every file operand
# counts, an operand-only read too; the options whose value is the next word (a short one last in
# its word, as `-sc <cmd>`) are passed over, bar -w/-W's script output file, which is written
EDITORS = {'vi', 'vim', 'nvim', 'ex', 'ed'}
EDITOR_VALUE_SHORTS = 'cuUiTtwW'


def _editor(name, args):
    out, k = [], 0
    while k < len(args):
        w = args[k]
        t = w.text
        if t == '--':
            out.extend(args[k + 1:])
            break
        if name != 'ed' and t.startswith('+'):
            k += 1                              # +cmd
            continue
        if t.startswith('-') and len(t) > 1:
            short = not t.startswith('--')
            if (t == '-p') if name == 'ed' else (t == '--cmd' or (short and t[-1] in EDITOR_VALUE_SHORTS)):
                if short and t[-1] in 'wW' and k + 1 < len(args):
                    out.append(args[k + 1])     # the script output file
                k += 2
            else:
                k += 1
            continue
        out.append(w)
        k += 1
    return [w for w in out if w.dyn or w.text != '-']


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
                placed = _under(folder, v)
                out.append(placed)
                if folder is not None and placed is v:
                    out.append(Under(folder, v))    # curl joins --output-dir to an absolute name too
        elif o in ('-D', '--dump-header', '-c', '--cookie-jar') and v is not None:
            if v.text != '-':
                out.append(v)
        elif o in ('-O', '--remote-name', '--remote-name-all'):
            out.append(folder if folder is not None else here(cmd_word))
    return out


class Under(sh.Word):
    """A file a folder option places (_under): its text is the folder's joined with the name's;
    `folder` and `name` are the two words, which only the refusal's rewrite reads (word_rewrite)."""
    __slots__ = ('folder', 'name')

    def __init__(self, folder, name):
        cut = '/' if paths.posix() else '/\\'          # on Linux `\` is a name's character
        sh.Word.__init__(self, folder.text.rstrip(cut) + '/' + name.text, folder.dyn or name.dyn,
                         name.start, name.end)
        self.folder, self.name = folder, name


def _under(folder, w):
    """`w` placed in `folder` (curl --output-dir), unless it is absolute (on Linux: starts with `/`)."""
    if folder is None or (w.text.startswith('/') if paths.posix() else re.match(r'^(?:[A-Za-z]:)?[\\/]', w.text)):
        return w
    return Under(folder, w)


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
            if paths.posix():                   # Linux: `\` is a name's character, no drives
                rooted, join = folder.text.startswith(('/', '~')), posixpath.join
            else:
                rooted = folder.text.startswith(('/', '\\', '~')) or ntpath.splitdrive(folder.text)[0]
                join = ntpath.join
            new.append(folder if b is None or rooted else
                       sh.Word(join(b.text, folder.text), folder.dyn or b.dyn, folder.start, folder.end))
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


def _tar_stdout(t):
    """True when tar's word `t`, no value of the word before it, is -O/--to-stdout (GNU tar and
    bsdtar alike: -x prints each file and writes none): --to-stdout, or an O in a cluster before
    any letter that takes or may take a value (-xvOf; in -xfO the O is f's file)."""
    if t.startswith('--'):
        return t == '--to-stdout'
    if not t.startswith('-'):
        return False
    for c in t[1:]:
        if c == 'O':
            return True
        if c not in TAR_FLAGS:
            return False
    return False


def _tar(args, cmd_word):
    """[Word]: the folders tar -x writes: each folder a -C/--directory names, relative to the one
    before it (_TarDirs), with none the working directory, and the working directory too once a -C
    was read that may not be one (_tar_word, _tar_old: fail closed). None with -O/--to-stdout
    where tar reads it as that option (_tar_stdout; any O of the old-style first word)."""
    first = args[0].text if args else ''
    extract = bool(re.match(r'^[A-Za-z]*x', first)) and not first.startswith('-')       # tar xf a.tar
    dirs, k, state = _TarDirs(), 0, None
    stdout = extract and 'O' in first
    if extract:
        k, state = _tar_old(args, dirs)
    while k < len(args):
        t = args[k].text
        if t == '--' and state is None:
            break
        if t in ('--extract', '--get') or re.match(r'^-[A-Za-z]*x', t) and not t.startswith('--'):
            extract = True
        stdout = stdout or state is None and _tar_stdout(t)
        state = _tar_word(args, k, state, dirs)
        k += 1
    if not extract or stdout:
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
    """(kind, value): how python reads its program from `args`. kind is 'code' (value the -c
    Word), 'module' (-m), 'stdin' (`-`), 'script' or 'none' (no program named: python reads
    standard input)."""
    k = 0
    if name == 'py' and args and PY_LAUNCHER_VER_RE.match(args[0].text):
        k = 1
    while k < len(args):
        w = args[k]
        t = w.text
        if t == '-':
            return 'stdin', None
        if t == '--':
            return ('script' if k + 1 < len(args) else 'none'), None
        if t.startswith('--'):
            k += 2 if t in PY_LONG_VALUE else 1
            continue
        if not t.startswith('-') or len(t) < 2:
            return 'script', None
        for j in range(1, len(t)):
            ch = t[j]
            if ch not in PY_SHORT_VALUE:
                continue
            if t[j + 1:]:
                value, k = sh.Word(t[j + 1:], w.dyn, w.start, w.end), k + 1
            else:
                value, k = (args[k + 1] if k + 1 < len(args) else None), k + 2
            if ch == 'c':
                return 'code', value
            if ch == 'm':
                return 'module', value
            break
        else:
            k += 1
    return 'none', None


def _python(cmd, name, args, cmd_word, via=None):
    return [here(cmd_word)] if python_unread(cmd, name, args, via) else []


PY_SP_WHY = 'Start-Process hands python a command line the guard does not read'
PY_PS_WHY = 'PowerShell may hand python other words than the guard read'
PY_UNREAD_WHY = 'the guard does not read what the code python is given writes'


def python_unread(cmd, name, args, via=None):
    """Why a python run counts as a write of the directory it runs in, or None when its own file
    decides what it writes (a script or module, or nothing to run)."""
    # Start-Process hands python one command line the guard re-split with its quotes taken out
    # (commands._split_list), so what python reads is not the words below
    if via == 'start-process':
        return PY_SP_WHY
    c = cmd
    while c is not None:                        # every shell on the way to python
        # a word PowerShell may hand on as other than one argument (dropped when empty, split at a
        # double quote): python's own words, or the text of a bash -c around it, are not the
        # ones read here
        if c.shell == sh.PS and any(ps_native_unsure(w) for w in c.words):
            return PY_PS_WHY
        c = c.nested_in
    kind, _ = py_program(name, args)
    if kind in ('code', 'stdin'):
        return PY_UNREAD_WHY                    # the guard does not read what the code writes
    if kind in ('module', 'script'):
        return None                             # its own file decides what it writes
    if cmd.piped_in or any(r.op in ('<<', '<<<', '<') for r in cmd.redirects):
        return PY_UNREAD_WHY                    # a script read from standard input
    return None


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
        # a literal path is judged where it points, a relative one where the location puts it (and,
        # for an agent session, refused: net_process_dir), a computed one as a cmdlet's computed
        # target is; for any other the directory the command runs in stands in, as a path it
        # cannot place (NET_UNPLACED)
        out.extend(x.word for x in net if net_placed(x.word) or net_relative(x.word) or x.word.dyn)
        if unread or any(not (net_placed(x.word) or net_relative(x.word) or x.word.dyn) for x in net):
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
            piped = [g for g in spec.piped if cmd.piped_in and not any(bound.get(p) for p in g)]
            if piped:
                out.append(pipe_stand_in(cmd))
            if spec.here and not bound.get(spec.here) and not any(spec.here in g for g in piped):
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
    if name == 'ruby' and cmd.shell != sh.PS:
        return out + _ruby(args, word)
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
    if name in ('awk', 'gawk'):
        return out + _awk(args, word)
    if name in EDITORS:
        return out + _editor(name, args)
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
        if s.dyn:
            base = ''
        elif paths.posix():
            base = s.text.rstrip('/').split('/')[-1]    # Linux: `\` is a name's character
        else:
            base = re.split(r'[\\/]', s.text.rstrip('\\/'))[-1]
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
# The provider-aware item and content cmdlets (PSCmdlet provider), as the line names them: the
# canonical name or one of PS_PROVIDER_ALIASES (any other alias is judged as a file write). Their
# item on a drive that holds no files is no file. Script functions (Compress-Archive, mkdir) and
# the file-only writers (Out-File, Tee-Object, Export-*, Start-Transcript, Invoke-WebRequest
# -OutFile) are not here and are judged as files.
PS_PROVIDER_ALIASES = ('ni', 'sc', 'ac', 'clc', 'ri', 'del', 'rni', 'mi', 'move', 'cpi', 'copy', 'sp')
PS_PROVIDER_CMDLETS = frozenset([n for n, s in PS_CMDLETS.items() if s.provider] +
                                [a for a in PS_PROVIDER_ALIASES if PS_CMDLETS[PS_ALIASES[a]].provider])
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


def ps_provider_word(cmd, inv, w):
    """True when the Word `w`, written by the command `cmd` (invocation `inv`), is a literal item
    of a provider-aware cmdlet on a drive that holds no files (PS_PROVIDER_ITEM_RE, no `..` part),
    whatever the line around it: ps_provider_item without ps_line_plain. Its refusal says why
    (PROVIDER_ITEM_WHY) instead of naming a file to write."""
    return cmd.shell == sh.PS and inv is not None and inv.via is None and inv.name in PS_PROVIDER_CMDLETS and \
        not w.dyn and bool(PS_PROVIDER_ITEM_RE.match(w.text)) and not DOTDOT_RE.search(w.text)


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


def py_unread(cmd, inv, w):
    """Why python's stand-in write `w` (_python's here()) is one (python_unread), or None for any
    other write: the reason a refusal gives, not a file it names. The verdict is unchanged."""
    if inv is None or not cm.PYTHON_RE.match(inv.name or '') or w.text != '.' or \
            cmd.source[w.start:w.end] == '.':
        return None
    return python_unread(cmd, inv.name, inv.args, inv.via) or PY_UNREAD_WHY


def unread_write(cmd, inv, w):
    """True when the write `w` stands for the directory a program runs in (here()) because the
    guard does not read what the program writes: python's code (py_unread), node -e, ruby -e or
    perl -e code, the names xargs reads, a .NET call's path. Such a write is unplaced(); a stand-in
    for a program that does write there (tar -x, curl -O, ln) is not."""
    if w.text != '.' or cmd.source[w.start:w.end] == '.':
        return False
    if py_unread(cmd, inv, w) is not None:
        return True
    if inv is not None and (inv.name in EVALS or inv.name == 'perl' or inv.via == 'xargs'):
        return True
    return net_stand_in(cmd, w)


def net_stand_in(cmd, w):
    """True when the write `w` is the stand-in write_targets gives a .NET write it cannot place
    (NET_UNPLACED): the directory the command runs in, at its first word."""
    return net_mentioned(cmd) and bool(cmd.words) and w.text == '.' and         (w.start, w.end) == (cmd.words[0].start, cmd.words[0].end) and cmd.source[w.start:w.end] != '.'


def shell_writes(event, ctx, cmd, inv, cwd, role):
    d = git_jail(event, ctx, cmd, inv, cwd, role)
    if d:
        return d
    net = {}
    if net_mentioned(cmd):
        net = dict(((x.word.start, x.word.end), x) for x in net_writes(cmd)[0])
    into = copy_into(cmd, inv)
    searched = False
    # A Start-Process program with a -WorkingDirectory runs there (commands.run_dir), while
    # PowerShell places the call's redirections and the -RedirectStandard* files from its own
    # location, the command's directory. The program's own targets are judged from both folders,
    # so a workdir never lets a write pass that the command's own directory refuses. git (and a
    # git-* program) gives none: the git rules judge its writes, from the workdir (commands.git_call).
    moved = len(write_targets(cmd, None)) + len(inv.writes) \
        if inv is not None and inv.via == 'start-process' and inv.workdir is not None else None
    asked = Asked()                             # every target's refusal wins over another's ask
    for k, w in enumerate(write_targets(cmd, inv)):
        if cmd.shell == sh.BASH and not w.dyn and BASH_DEVICE_RE.match(w.text):
            continue
        bases = (OUTER,) if moved is None or k < moved else (OUTER, cm.run_dir(inv))
        for base in bases:
            d, searched = _write_from(event, ctx, cmd, inv, cwd, role, w, base, net, into, searched)
            d = asked.firm(d)
            if d:
                return d
    return asked.first


def _write_from(event, ctx, cmd, inv, cwd, role, w, base, net, into, searched):
    """(Decision or None, searched) for the write target `w` of shell_writes read from `base`
    (OUTER, or the folder a Start-Process -WorkingDirectory names)."""
    runs_in = cmd.cwd if base is OUTER else base
    asked = Asked()                             # find's ask waits for this target's own refusal
    piped = pipe_stand_in_word(cmd, w)
    target, why = (None, PIPE_UNPLACED) if piped else write_place(ctx, cmd, w, base)
    if target is None and w.dyn and (w.start, w.end) in net:
        why = '%s; %s' % (why, NET_PROCESS_DIR)
    unread = py_unread(cmd, inv, w) or (NET_UNPLACED if net_stand_in(cmd, w) else None)
    # a kit tool acts on the checkout its -C names: that folder, not the one it runs in
    checked = kit_tool_dir(event, ctx, cmd, inv, cwd) if base is OUTER and unread == PY_PS_WHY else None
    if checked is not None:
        target, why = checked[1], None
    if target is None and not searched:
        searched = True                         # find's start paths answer for every such target at once
        d = asked.firm(find_exec_writes(event, ctx, cmd, cwd, role))
        if d:
            return d, searched
    computed = target is None and cmd.shell == sh.PS and w.dyn
    if computed:
        pass                                    # PowerShell's, from any location: one $p or (...) can name the main checkout (B15)
    elif target is None and runs_in is not None and \
            ctx.place(runs_in).kind not in (repo.MAIN, repo.UNKNOWN):
        # a target it cannot place, from a worktree or elsewhere: passes, but while a ledger key is
        # unread it can be the ledger
        return asked.first or ledger_unread(ctx, role, '`%s` writes %s' % (cmd.text, w.text)), searched
    what = '`%s` writes the path the pipeline gives it' % cmd.text if piped else \
        '`%s` writes %s' % (cmd.text, w.text if w.text != '.' else 'files in the directory it runs in') \
        if unread is None else \
        'the directory `%s` runs in (counted as written: %s)' % (cmd.text, unread) if checked is None else \
        'the checkout `%s` acts on, %s (counted as written: %s)' % (cmd.text, checked[0].text, unread)
    # here() or ps_unread(): no word to aim
    stand_in = (w.text == '.' and cmd.source[w.start:w.end] != '.') or w.start == w.end
    # a value its option's word also spells (of=, --output=, -tDIR) keeps that option before it
    rewrite = None if stand_in else word_rewrite(cmd, w)
    attached = attached_lead(cmd, w) if rewrite is not None and not isinstance(w, Under) else None
    if (w.start, w.end) in net:
        rewrite, attached = (lambda path, x=net[(w.start, w.end)]: net_retarget(
            cmd, x, path, lambda o: write_place(ctx, cmd, o, base)[0])), None
    if checked is not None:
        x = checked[0]
        # -C'dir' spelt other than its text: the value's span is the whole word, so the rewrite spells -C again
        lead = '' if any(a is x for a in inv.args) or cmd.source[x.start:x.end] == x.text else '-C '
        rewrite = (lambda path: retarget(cmd, x, path if path.startswith('<') else
                                         fwd(paths.norm(path, fold=False)), lead)) if x.start is not None else None
    if stand_in and base is not OUTER and base is not None:
        # the folder it runs in is the -WorkingDirectory value: aim that word instead
        rewrite = lambda path, x=inv.workdir: retarget(cmd, x, paths.norm(path, fold=False))
    placed = target is not None and not unread_write(cmd, inv, w)
    output = any(r.writes and r.target is not None and r.target.start is not None and
                 (r.target.start, r.target.end) == (w.start, w.end) for r in cmd.redirects) or (getattr(inv, 'name', None) or '').lower() in OUTPUT_WRITERS
    item = target is None and ps_provider_word(cmd, inv, w)
    for t in into_folder(target, w, into, runs_in, getattr(ctx, 'home', None)) if target else [target]:
        aim = rewrite
        if attached is not None and checked is None and t != target and rewrite is not None:
            # -tDIR names the folder: the same copy into the folder that holds the file it would write
            aim = lambda path, r=rewrite: r(fwd(path).rsplit('/', 1)[0] if '/' in fwd(path) else path)
        # a provider item's refusal runs the whole line from the worktree: a statement before the
        # call may set what it reads (PROVIDER_ITEM_WHY)
        d = edit_target(event, ctx, t, cwd, role, what, why, rewrite=aim,
                        run=whole_line(ctx, cmd) if item else bare(cmd), redirect=output,
                        shell=outer_shell(cmd), item=item,
                        stand_in=piped or w.start == w.end or unread_write(cmd, inv, w))
        if not placed:
            d = unplaced(d, role) or ledger_unread(ctx, role, what)   # a warned refusal leaves the ask
        d = asked.firm(d)
        if d:
            return d, searched
    # the relative path this write names, or a relative one the call only reads (Copy's source);
    # another relative path it writes is judged as its own target first (Move's into the main checkout)
    others = net[(w.start, w.end)].paths if (w.start, w.end) in net else []
    relative = [o.text for o in others if net_relative(o)]
    if target is not None and base is OUTER and others and \
            (net_relative(w) or any(net_relative(o) and (o.start, o.end) not in net for o in others)):
        d = asked.firm(net_process_dir(event, ctx, role, what, relative, target, rewrite))
        if d:
            return d, searched
    # A computed PowerShell target, or a path read from a directory the guard cannot tell (after a
    # `cd $x`): for a role main_checkout lets through, the jail cannot judge it either.
    if computed or (target is None and runs_in is None):
        d = ps_drive_jail(event, ctx, cwd, role, what, why, rewrite)
        if d:
            return d, searched
    return asked.first, searched


OUTER = object()                                # write_place: the command's own directory


def write_place(ctx, cmd, w, base=OUTER):
    """(path, why) of the Word `w` the command `cmd` writes, read from cmd's directory and shell:
    path None when the guard cannot place it (why says why), a PowerShell drive or provider path
    shell.resolve_path cannot place among them (docs/guard-v2.md B16). `base`, when given, is the
    folder a relative `w` is read from instead of cmd's directory: the -WorkingDirectory a
    Start-Process program runs in (commands.run_dir), None when the guard cannot place that."""
    here_dir = cmd.cwd if base is OUTER else base
    pwd = ps_pwd_path(cmd, w)
    if pwd is not None:
        target, why = sh.resolve_path(pwd, cmd.cwd, cmd.shell, getattr(ctx, 'home', None)), None
    elif w.dyn:
        target, why = None, 'a target that is not literal: %s' % w.text
    elif cmd.shell == sh.BASH and BASH_TMP_RE.match(w.text) and not paths.posix():
        target, why = paths.norm(os.path.join(jail.bash_tmp(ctx), w.text[5:]), fold=False), None
    else:
        target = sh.resolve_path(w.text, here_dir, cmd.shell, getattr(ctx, 'home', None))
        why = None if target else 'the directory it runs in is unknown' if here_dir is None \
            else 'a path the text cannot place: %s' % w.text
        if target is None and base is None:
            why = 'the folder its Start-Process -WorkingDirectory names is computed or not one the guard can place'
    return target, why


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
    target does (shell_writes), unless a ledger key is unread (ledger_unread). An ask waits for
    every start path's refusal (Asked)."""
    asked = Asked()
    for f, inv in find_runs(cmd):
        for w in _find_paths(inv.args)[0] or [here(inv.word)]:
            if not w.dyn and BASH_DEVICE_RE.match(w.text):
                continue
            target, why = write_place(ctx, f, w)
            if target is None and f.cwd is not None and ctx.place(f.cwd).kind not in (repo.MAIN, repo.UNKNOWN):
                d = asked.firm(ledger_unread(ctx, role, '`%s` runs `%s` on what it finds in %s'
                                             % (bare(f), bare(cmd), w.text)))
                if d:
                    return d                    # a subagent's refusal
                continue
            what = '`%s` runs `%s` on what it finds in %s' % (
                bare(f), bare(cmd), w.text if w.text != '.' else 'the directory it runs in')
            stand_in = (w.text == '.' and f.source[w.start:w.end] != '.') or w.start == w.end
            part = any((x.start, x.end) == (w.start, w.end) and x.text != w.text for x in f.words)
            swaps = execdir_swaps(f, cmd)
            if swaps:
                what += ' (with -exec in place of -execdir, find hands it the same files by their full paths)'
            rewrite = None if stand_in or part else \
                (lambda path, w=w, f=f, s=swaps: retarget(f, w, path, swaps=s))
            d = edit_target(event, ctx, target, cwd, role, what, why, rewrite=rewrite, run=bare(f),
                            shell=outer_shell(f))
            if target is None:
                d = unplaced(d, role) or ledger_unread(ctx, role, what)   # a warned refusal leaves the ask
            d = asked.firm(d)
            if d:
                return d
    return asked.first


def execdir_swaps(f, cmd):
    """[(word, '-exec' or '-ok')] for the one -execdir or -okdir of the find command `f` when it runs
    `cmd` and `cmd` takes only options and {}: -exec runs it on the same files, named by full path,
    which the guard can place; [] otherwise (a relative name the command writes would land
    elsewhere)."""
    dirs = [i for i, x in enumerate(f.words) if not x.dyn and x.text in ('-execdir', '-okdir')]
    words = [x.text for x in cmd.words]
    # find's {} is the one word computed at run time (the path it fills in)
    if len(dirs) != 1 or len(words) < 2 or any(x.dyn and x.text != '{}' for x in cmd.words):
        return []
    if not all(t == '{}' or t.startswith('-') for t in words[1:]) or '{}' not in words[1:]:
        return []
    i = dirs[0]
    x = f.words[i]
    # the words after it are the command find runs, as `cmd` spells them (a nested command has its own source)
    if x.start is None or [y.text for y in f.words[i + 1:i + 1 + len(words)]] != words:
        return []
    return [(x, x.text.replace('dir', ''))]


def ps_drive_jail(event, ctx, cwd, role, what, why, rewrite):
    """An agent session's PowerShell write to a computed target ($p, a ( ) expression: _write_from),
    or any shell's write of a path read from a directory the guard cannot tell (after `cd $x`),
    by a role main_checkout lets through (it may edit a main checkout): the jail cannot judge a
    place it cannot tell, so it refuses, as git_jail_unknown does for git."""
    if not agent_session(event, ctx):
        return None
    dest = jail.destination(jail_roots(event, ctx, cwd))
    head = '%s: the guard cannot tell where it writes (%s). Write to a literal path inside the folders an ' \
           'agent session may write.' % (what, why)
    if dest is not None and rewrite is not None:
        return refuse(UNPLACED, role, head + ' The same write in %s:' % fwd(dest.path),
                      rewrite(fwd(dest.path) + '/<file>'))
    return refuse(UNPLACED, role, head + ' To see where this session runs:', 'pwd')


DUPS_RE = re.compile(r'(?:[ \t]*[0-9]*[<>]&[0-9-]+)+')


def retarget(cmd, w, path, lead='', swaps=()):
    """The command `cmd` (from its program word, redirections kept) with the word `w` replaced
    by `path`, quoted so both shells read it literally (literal_word: a `::$DATA` stream name is
    not expanded), after `lead` (an option the word's span also covers, as -C'dir' does); each
    (word, text) of `swaps` replaces another word of the line as well."""
    start = cmd.words[0].start if cmd.words else w.start
    start = min(start, w.start)
    end = max(cmd.end, w.end)
    m = DUPS_RE.match(cmd.source, end)          # a dup (2>&1) has no word, so no span
    end = m.end() if m else end
    out, at = [], start
    for x0, x1, text in sorted([(w.start, w.end, lead + literal_word(path))] +
                               [(x.start, x.end, t) for x, t in swaps]):
        out.append(cmd.source[at:x0] + text)
        at = x1
    return (''.join(out) + cmd.source[at:end]).strip()


def attached_lead(cmd, w):
    """The option text the word holding the value Word `w` also spells before it (`of=`,
    `--output=`, `-d`, `-t`), or None when `w` is a word of its own or not such a value."""
    for x in cmd.words:
        if (x.start, x.end) != (w.start, w.end) or x.text == w.text:
            continue
        if w.dyn or x.dyn or len(x.text) <= len(w.text) or not x.text.endswith(w.text):
            return None
        lead = x.text[:-len(w.text)]
        return lead if ATTACHED_LEAD_RE.match(lead) else None
    return None


# an option an attached value follows: -d, -sSLo, --output=, of=
ATTACHED_LEAD_RE = re.compile(r'^(?:--?[A-Za-z][\w-]*=?|[A-Za-z]+=)$')


def word_rewrite(cmd, w):
    """rewrite(path) for the write target Word `w` of `cmd`: the word aimed at `path`, an attached
    value after its option (attached_lead), a file curl or ruby places under a folder (Under) by
    aiming that folder; None when no word of the line can be aimed."""
    if isinstance(w, Under):
        aim = word_rewrite(cmd, w.folder) if w.folder.start is not None and not w.folder.dyn else None
        if aim is None:
            return None
        name = fwd(w.name.text)

        def under(path):
            p = fwd(path)
            # the folder that keeps the name as written; a destination named by its last part only: its folder
            p = p[:-len(name) - 1] if p.endswith('/' + name) else p.rsplit('/', 1)[0]
            return aim(p)
        return under
    if not any((x.start, x.end) == (w.start, w.end) and x.text != w.text for x in cmd.words):
        return lambda path: retarget(cmd, w, path)
    lead = attached_lead(cmd, w)
    if lead is None:
        return None
    return lambda path: retarget(cmd, w, path, lead)


# ---------------------------------------------------------------- wrapped gates (P16)

def bare(cmd):
    if not cmd.words:
        return cmd.text
    return cmd.source[cmd.words[0].start:cmd.words[-1].end]


def bare_wrapper(ctx, cmd, inv, cwd):
    """(command, from_dir) the wrapped_gate alternative names: the call from its first word to its
    last (bare: no pipe, no redirection), the script by absolute path, and for a gate.py run with
    no -C and no subcommand ` -C <the directory it ran in>` after it, so it runs the same from any
    directory. The script is the kit's (tools/<name>.py of advice_kit) when it is the kit the guard
    runs from or no such file is there (a repository holds no copy of the kit's tools); a script
    that is there, such as the kit repository's own tools/gate.py, keeps its folder. from_dir is
    the directory the alternative must still run in (a lock run's program, or a gate run with a
    subcommand), else None. The kit command's run (`kit gate`, tools/kit.py gate) keeps its words
    as written up to the tool's name, kit.py re-pathed as any script, and the -C goes after that
    name. (bare(cmd), from_dir) when the script word is not literal, or no python runs one (a
    gate.py run as a program). When the directory it runs in is unknown (a
    computed cd before it: cmd.cwd None) a relative script is kept as written, no -C is added and
    from_dir is None: wrapped_gate then says to run it from the worktree root."""
    where = cmd.cwd
    at = cm.tool_at(inv) if inv.via != 'start-process' else None
    found = cm.python_script(inv) if at else None
    word = inv.args[found[0]] if found else None
    name = inv.args[at[0]] if at and at[0] >= 0 else None      # the word that names the tool
    if name is None or name.dyn or name.synth or not cmd.words or name.end > len(cmd.source) or \
            word is not None and (word.dyn or word.synth):
        return bare(cmd), where
    rest = at[2]
    if rest and rest[0].synth and rest[0].start == name.start:  # the tool's name word holds more
        return bare(cmd), where
    tail = cmd.source[name.end:cmd.words[-1].end] if rest else ''
    if word is None:                                            # the kit command run as a program
        head = cmd.source[cmd.words[0].start:name.end]
    else:
        path = sh.resolve_path(word.text, where, cmd.shell, getattr(ctx, 'home', None))
        if not path:
            return bare(cmd), where
        kit_tools = paths.norm(os.path.join(ctx.kit, 'tools'), real=True)
        if paths.norm(os.path.dirname(path), real=True) == kit_tools or not os.path.isfile(path):
            path = os.path.join(advice_kit(ctx), 'tools', ntpath.basename(path.replace('/', '\\')))
        head = cmd.source[cmd.words[0].start:word.start] + literal_word(path) + cmd.source[word.end:name.end]
    from_dir = where
    if cm.wrapper(inv) == 'gate' and not (rest and not rest[0].text.startswith('-')):    # no subcommand
        given = None
        for k, a in enumerate(rest):
            if a.text == '-C':
                given = rest[k + 1] if k + 1 < len(rest) else a
                break
            if a.text.startswith('-C'):
                given = sh.Word(a.text[2:], a.dyn, a.start, a.end)
                break
        if given is None and where:
            head += ' -C %s' % literal_word(where)
            from_dir = None
        elif given is not None and not given.dyn and os.path.isabs(given.text):
            from_dir = None                     # -C <absolute>: it runs the same from anywhere
    return (head + tail).strip(), from_dir


def wrapped_gate(event, ctx, cmd, inv, cwd, role):
    kind = cm.wrapper(inv)
    if not kind or not cmd.piped_out or kind == 'lock-run' and lock_run_help(inv) \
            or kind == 'gate' and gate_runs_nothing(inv):
        return None
    instead, from_dir = bare_wrapper(ctx, cmd, inv, cwd)
    read = 'read the log gate.py names' if kind == 'gate' else 'read its output'
    where = ' with %s as the working directory' % quote(from_dir) if from_dir and not same_dir(from_dir, cwd) \
        else ' from the worktree root' if cmd.cwd is None else ''
    how = ('Run it bare, as a call of its own in the background%s (run_in_background: true; no pipe and no '
           'redirection), and when its completion notice comes, %s:' % (where, read))
    return refuse('wrapped-gate', role, '%s is piped into another command; a wrapped gate reads to the '
                  'classifier as something else and is refused (refusal pattern P16). %s' % (bare(cmd), how), instead)


def lock_run_help(inv):
    """True when `machine_lock.py run` `inv` has exactly one word after run, a literal --help or -h:
    it prints its usage and runs nothing, so a pipe changes nothing the lock sees."""
    found = cm.tool_at(inv)
    if not found:
        return False
    rest = found[2][1:]
    return len(rest) == 1 and not rest[0].dyn and rest[0].text in ('--help', '-h')


GATE_NOTHING = ('-h', '--help', '--plan', '--measure')


def gate_runs_nothing(inv):
    """True when gate.py `inv` names no subcommand (its first word is a literal option: gate.py
    reads land and wait-result only there) and has a literal -h, --help, --plan or --measure: it
    runs no gate step and takes no lock, so a pipe changes nothing (docs/guard-v2.md "Kit tools
    may be piped")."""
    found = cm.tool_at(inv)
    if not found:
        return False
    rest = found[2]
    if not rest or rest[0].dyn or rest[0].synth or not rest[0].text.startswith('-'):
        return False
    return any(not a.dyn and not a.synth and a.text in GATE_NOTHING for a in rest)


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
    if paths.posix():
        return p in ('/', '//')                 # Linux: no drives; `/` (and `//`) is the root
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


def kit_wrapper(ctx, cmd, inv, cwd, event=None):
    """cm.wrapper(inv) when the script it runs is the kit's own (tools/gate.py or
    tools/machine_lock.py of the kit the guard runs from, ctx.kit, or, given the Action `event`, of
    an export of it the session cannot write, rolepolicy.kit_export, the folder advice names
    (advice_kit) among them; the path resolved from the command's directory with links followed),
    else None. Only the kit's tool takes the machine lock itself, so only it may run an
    interpreter a repository lists as heavy: a script that shares its file name (a branch's copy,
    `python <temp>/gate.py`) is judged as any other script. A repository holds no copy of the
    kit's tools (decision 21 in docs/multi-host-plan.md section 7), so an opt-in stub fails closed
    here too."""
    kind = cm.wrapper(inv)
    if not kind:
        return None
    found = cm.python_script(inv)
    word = inv.args[found[0]] if found else inv.word
    return kind if kit_script(ctx, cmd, word, cwd, event, inv) else None


def kit_script(ctx, cmd, word, cwd, event=None, inv=None):
    """True when the program or script Word `word` is one of the kit's own tools: in the tools
    folder of the kit the guard runs from (ctx.kit) or, given the Action `event`, of an export of
    it the session cannot write (rolepolicy.kit_export), the path resolved, links followed, from
    the directory the program runs in: the command's, or for a Start-Process call `inv` with a
    -WorkingDirectory the folder that names (cm.run_dir), where only a path with a drive or a
    share is read when the guard cannot place that folder (a rooted \\x takes that folder's
    drive). The heavy program's repository (heavy) is still the command's."""
    if word is None or word.dyn:
        return False
    if inv is not None and inv.via == 'start-process' and inv.workdir is not None:
        base = cm.run_dir(inv)
    else:
        base = cmd.cwd or cwd
    path = sh.resolve_path(word.text, base, cmd.shell, getattr(ctx, 'home', None)) or (
        word.text if sh.is_absolute(word.text) else None)
    if not path:
        return False
    tools = paths.norm(os.path.join(ctx.kit, 'tools'), real=True)
    folder = paths.norm(os.path.dirname(path), real=True)
    if folder == tools:
        return True
    if event is None or os.path.basename(folder) != 'tools':
        return False
    from . import rolepolicy
    return bool(rolepolicy.kit_export(ctx, os.path.dirname(folder), event, cwd))


# a piece Windows PowerShell 5.1 may split out of an unsure word (ps_native_unsure) that python's
# argparse may read as a -C: a dash after a blank or a quote, then a dash (a long option, which
# argparse also takes abbreviated) or a run with a C in it (-C, a cluster -vC)
PS_SPLIT_C_RE = re.compile(r'(?:^|[\s"])-(?:-|\S*C)')


def kit_tool_dir(event, ctx, cmd, inv, cwd):
    """(Word, path) of the one literal absolute -C <dir> (or -C<dir>) a kit tool (kit_script) is
    given, which is the checkout it acts on, or None. Only for a python that PowerShell runs
    directly (not Start-Process), on no PowerShell line around it with a word it may hand on as
    other than one argument, and only when no such word comes before the -C value or, after it,
    may split into a word argparse reads as another -C (PS_SPLIT_C_RE); a second -C, or a --root,
    is none either."""
    if inv is None or inv.via is not None or not cm.PYTHON_RE.match(inv.name or ''):
        return None
    c = cmd.nested_in
    while c is not None:
        if c.shell == sh.PS and any(ps_native_unsure(w) for w in c.words):
            return None
        c = c.nested_in
    found = cm.python_script(inv)
    if not found or not kit_script(ctx, cmd, inv.args[found[0]], cwd, event):
        return None
    args = inv.args
    marks = [k for k, w in enumerate(args) if k > found[0] and
             (w.text.startswith('-C') or w.text.split('=')[0] in ('--r', '--ro', '--roo', '--root'))]
    if len(marks) != 1 or args[marks[0]].text.startswith('--'):
        return None
    k = marks[0]
    w = args[k]
    if w.text == '-C':
        if k + 1 >= len(args):
            return None
        value, last = args[k + 1], k + 1
    else:
        value, last = tail_word(w, w.text[2:]), k
    if value.dyn or not value.text or not (value.text.startswith(('/', '\\')) or
                                           ntpath.splitdrive(value.text)[0]):
        return None
    words = [inv.word] + list(args) if inv.word is not None else list(args)
    cut = len(words) - (len(args) - last)
    if cmd.shell == sh.PS:
        if any(ps_native_unsure(x) for x in words[:cut + 1]):
            return None
        after = words[cut + 1:]
        first = next((i for i, x in enumerate(after) if ps_native_unsure(x)), None)
        if first is not None and any(PS_SPLIT_C_RE.search(x.text) for x in after[first:]):
            return None
    path = sh.resolve_path(value.text, cmd.cwd or cwd, cmd.shell, getattr(ctx, 'home', None))
    return (value, path) if path else None


def heavy(event, ctx, cmd, inv, cwd, role):
    if inv is None or inv.dyn or kit_wrapper(ctx, cmd, inv, cwd, event):
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
    # --prefix and --stage take a value; left out of GIT_LONG_VALUES, the value is read as a path
    # (fail closed)
    'checkout-index': ('index', 'quiet', 'force', 'all', 'no-create', 'prefix', 'stage', 'temp',
                       'ignore-skip-worktree-bits', 'stdin'),
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
    abbreviates, or every one it could abbreviate (an ambiguous prefix fails closed). git reads
    `--no-<prefix>` as the negation of an option too (`--no-st` is `--no-staged`), so `--no`,
    `--no-` and `--no-<prefix>` also select `no-<name>` for each name the prefix abbreviates."""
    table = GIT_LONGS.get(sub, ())
    if body in table or len(body) < 2:
        return [body]
    hits = [n for n in table if n.startswith(body)]
    if body.startswith('no-') or 'no-'.startswith(body):
        hits += ['no-' + n for n in table if n.startswith(body[3:]) and not n.startswith('no-')
                 and 'no-' + n not in hits]
    return hits or [body]


def restore_index_only(short, longs):
    """Whether git restore with these options (_opts) writes the index alone: --staged (-S)
    without --worktree (-W), and every long option one GIT_LONGS names (fail closed: _canon_long
    leaves a one-letter body as written, and `--w` is `--worktree` to git; `--no-st` is
    `--no-staged`, which it names and the table does not; `--no` and `--no-` git rejects as
    ambiguous, and _canon_long's names for them are not in the table either)."""
    return (('S' in short or 'staged' in longs) and not ('W' in short or 'worktree' in longs)
            and longs <= set(GIT_LONGS['restore']))


def _one_letter(sub, body):
    """The long option a one-letter `--body` abbreviates for `sub` when exactly one starts with it
    (git reads `--h` as reset's `--hard`), else None (ambiguous or unknown: git rejects it)."""
    hits = [n for n in GIT_LONGS.get(sub, ()) if len(body) == 1 and n.startswith(body)]
    return hits[0] if len(hits) == 1 else None


def _opts(sub, args, wide=False):
    """(short option letters, long option names, positional Words) of a git subcommand's
    arguments, option values left out. `wide` also names the long option a one-letter body
    abbreviates (_one_letter) beside the body itself, for a check that refuses on that option:
    the body stays, so a check that passes only on known options (restore_index_only) still
    fails closed, and a caller that passes on an option it names (a listing, --no-deref) keeps
    the narrow reading."""
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
            one = _one_letter(sub, body) if wide else None
            if one:
                longs.add(one)
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


def git_alias(ctx, cmd, gc, cwd, role, event=None):
    name, val = gc.alias
    rest = ' '.join(w.text for w in gc.rest)
    if val.startswith('!'):
        how, instead = 'Run the command itself:', ('%s %s' % (val[1:], rest)).strip()
        instead = alias_heavy_run(ctx, cmd, cwd, instead) or instead
    else:
        how, instead = 'Run the git command itself:', ('git %s %s' % (val if gc.sub == name else gc.sub, rest)).strip()
        # the command git runs makes a commit, in an agent session's worktree: plain git there is
        # refused for its missing trailer (git_plain_commit), so the advice is that rule's kit tool
        place = ctx.place(cmd.cwd or cwd) if (cmd.cwd or cwd) and event is not None and \
            agent_session(event, ctx) else None
        alt = commit_alternative(ctx, inline_alias(gc), place.top) \
            if place is not None and place.kind == repo.LINKED and place.top else None
        if alt is not None:
            how, instead = 'Run the git command itself, which makes a commit: %s' % alt[0][0].lower() + alt[0][1:], alt[1]
    return refuse('git-alias', role, '`%s` defines a git alias on the command line (alias.%s=%s), which hides '
                  'the command git runs. %s' % (bare(cmd), name, val, how), instead)


def alias_heavy_run(ctx, cmd, cwd, text):
    """The machine_lock run line for a shell alias's command `text` when it is one heavy program
    (heavy) and the session holds no machine lock, so the alternative git_alias prints is not
    refused as heavy in turn; else None."""
    where = cmd.cwd or cwd
    try:
        parsed = cm.read(text, cmd.shell, where, home=getattr(ctx, 'home', None))
    except Exception:  # noqa: BLE001 - an alternative's text only: the plain one stands
        return None
    if not parsed.clean or len(parsed.commands) != 1:
        return None
    inv = cm.invocation(parsed.commands[0])
    if inv is None or inv.dyn:
        return None
    cfg = ctx.config(where) if where else None
    entry = kconfig.heavy_match(inv.path, inv.argv, kconfig.heavy_programs(cfg))
    if not entry or ctx.lock_held()[0]:
        return None
    return lock_alternative(ctx, cmd, entry, cwd, text)


def inline_alias(gc):
    """`gc` as git runs it when its subcommand names an alias the line itself defines (-c
    alias.<name>=<value>): the value's words are the subcommand and its first arguments, so the
    rules judge the command git runs. A builtin name (git ignores an alias that would hide one),
    --config-env (its value is in the environment), a computed setting, a shell alias (!...) and
    a value that starts with an option or cannot be split end the expansion where it is
    (git_alias warns). A chain is followed at most ALIAS_DEPTH steps: the call is then judged as
    the last expansion reached."""
    if gc is None or gc.inv is None or not gc.alias or gc.sub_dyn:
        return gc
    inline = {}
    for key, val, first, last in git_prefix_configs(gc):
        if key.startswith('alias.') and not cm.passed(gc.inv.cmd, first).startswith('--') and \
                not first.dyn and not last.dyn:
            inline[key[len('alias.'):].lower()] = val   # the last setting of a name wins, as in git
    for _depth in range(ALIAS_DEPTH):
        val = inline.get(gc.sub.lower())
        if val is None or gc.sub in GIT_BUILTINS or val.startswith('!'):
            return gc
        words = split_alias(val)
        if not words or words[0].startswith('-'):
            return gc
        rest = [sh.Word(t, False, None, None) for t in words[1:]] + list(gc.rest)
        gc = cm.GitCall(gc.dir, words[0], rest, gc.inv, gc.unknown, gc.alias, False, gc.blind,
                        len(words) - 1 + gc.synth)
    return gc


def git_call_judged(inv):
    """commands.git_call, with an alias the line defines expanded (inline_alias)."""
    return inline_alias(cm.git_call(inv))


def git_rules(event, ctx, cmd, inv, cwd, role):
    d = git_send_pack_pmdata(event, ctx, inv, role) or call_subexpr_pmdata(event, ctx, cmd, inv, cwd, role)
    if d:
        return d
    gc = cm.git_call(inv)
    if gc is None:
        return None
    if gc.alias:
        d = git_alias(ctx, cmd, gc, cwd, role, event)
        if d:
            return d                            # a warning by default: the command git runs is judged below
        gc = inline_alias(gc)
    if gc.sub_dyn or (gc.sub == '' and gc.inv is not None and gc.inv.via == 'xargs'):
        d = git_push_pmdata(event, ctx, gc, role)       # a computed subcommand may be push
        if d:
            return d
    d = git_remote_rewrite(event, ctx, gc, role)
    if d:
        return d
    sub, args = gc.sub, gc.rest
    text = bare(cmd)
    if sub == 'worktree':
        # from any place: remove removes another tree, add -B or -f writes a shared branch
        return git_worktree_remove(ctx, gc, role, text) or git_shared_ref_forms(event, ctx, cmd, gc, cwd, role)
    if sub == 'push':
        # git_push's refusal first, so git-shared-ref in warn mode never hides it
        return git_push(event, ctx, gc, role) or git_shared_push(event, ctx, cmd, gc, cwd, role)
    if sub in SHARED_REF_FORMS:
        d = git_shared_ref_forms(event, ctx, cmd, gc, cwd, role)    # from any place, as update-ref
        if d:
            return d
    if sub == 'branch':
        d = git_branch_delete(ctx, gc, role) or git_shared_ref_forms(event, ctx, cmd, gc, cwd, role)
        if d:
            return d
    if sub == 'update-ref':
        d = git_shared_ref(event, ctx, cmd, gc, cwd, role)    # from any place: every worktree shares refs
        if d:
            return d
    placed = gc.dir and not gc.unknown
    place = ctx.place(gc.dir) if placed else \
        repo.Place(repo.UNKNOWN, None, 'the directory git runs in is unknown', None, None)
    if place.kind == repo.LINKED and sub == 'stash':
        return git_stash_shared(event, ctx, cmd, gc, role, place)
    if place.kind not in (repo.MAIN, repo.UNKNOWN):
        return None
    d = git_main_rules(event, ctx, cmd, inv, gc, cwd, role, place)
    if place.kind == repo.UNKNOWN and not gc.blind and not (placed and in_git_dir(gc.dir)):
        return unplaced(d, role)                # a computed -C, or a folder git cannot tell
    # gc.blind (GIT_DIR and the like, --git-dir, --work-tree, core.worktree) re-points git on
    # purpose, at a place the line nearly always names literally: its refusal stands
    return d


# git verbs that make a commit, each refused for an agent session in a linked worktree
# (git_plain_commit): plain git writes no Co-Authored-By trailer, so the gate's trailer check fails
# the branch. Known limits, none read here:
# - a git alias from git's config files (an inline -c alias.<name> is expanded);
# - a verb the shell computes or a script runs;
# - commit-tree and rebase;
# - a computed -C, --git-dir or --work-tree: no place is known, so the unplaced handling and
#   git-main-history judge the line, not this rule;
# - a --continue of a revert or cherry-pick sequence started with --no-commit makes no commit, but
#   it is refused like any other: git keeps that mode in the sequence's state, which is not read;
# - a word the shell computes after a hold (`git cherry-pick -n $X`) is read as a commit, though
#   it may be an option that undoes the hold (`--commit`): a computed commit is the common case,
#   so failing closed there would refuse honest lines;
# - commit.py commits only the change in progress of a revert, cherry-pick or am sequence: it does
#   not go on with the commits or patches left, so the refusal says to end the sequence (--quit).
COMMIT_VERBS = ('commit', 'merge', 'revert', 'cherry-pick', 'am', 'pull')
# the words after which a verb makes no commit, spelled exactly
COMMIT_STOPS = {'commit': ('--dry-run',), 'merge': ('--abort', '--quit'), 'revert': ('--abort', '--quit'),
                'cherry-pick': ('--abort', '--quit'),
                'am': ('--abort', '--quit', '--show-current-patch', '--show-current-patch=diff',
                       '--show-current-patch=raw')}
# the options, spelled exactly, that leave the verb's change uncommitted; an abbreviation is none
# (revert's and cherry-pick's -n is read as --no-commit)
COMMIT_HOLDS = {'merge': ('--no-commit', '--squash', '--ff-only'), 'pull': ('--no-commit', '--squash', '--ff-only'),
                'revert': ('--no-commit',), 'cherry-pick': ('--no-commit',)}
# the long options that undo a hold (a prefix of one counts): the last of --commit and --no-commit
# (or -n) wins, of --squash and --no-squash, and of --ff, --no-ff and --ff-only
COMMIT_UNDOES = {'commit': '--no-commit', 'no-squash': '--squash', 'ff': '--ff-only', 'no-ff': '--ff-only'}
# the words that go on with a sequence: the commit is made by commit.py, not by the verb again
COMMIT_STEPS = ('--continue', '--skip', '--resolved')
# the short options that take a value (attached, or the next word), per verb (git 2.5x)
COMMIT_SHORT_VALUES = {'commit': 'mFcCt', 'merge': 'mFsX', 'pull': 'sXj', 'revert': 'mX', 'cherry-pick': 'mX',
                       'am': 'pC'}
# the short options whose value, if any, is attached only (-S<key>, commit's -u<mode>): each ends
# its cluster and takes no next word
COMMIT_SHORT_ATTACHED = {'commit': 'Su', 'merge': 'S', 'pull': 'S', 'revert': 'S', 'cherry-pick': 'S', 'am': 'S'}
# the short options that take no value, per verb: any other letter may take the rest of its cluster
# or the next word, so a hold after it is not counted (commit_scan)
COMMIT_SHORT_FLAGS = {'commit': 'apsnevqzio', 'merge': 'nqve', 'pull': 'nqvrfkpa46t', 'revert': 'ens',
                      'cherry-pick': 'enxs', 'am': 'skcq3imunrb'}
# the long options that take a value in the next word when no `=` gives it
COMMIT_LONG_VALUES = {'message', 'file', 'strategy', 'strategy-option', 'cleanup', 'into-name', 'mainline',
                      'depth', 'deepen', 'shallow-since', 'shallow-exclude', 'upload-pack', 'jobs',
                      'negotiation-tip', 'server-option', 'refmap', 'reuse-message', 'reedit-message',
                      'fixup', 'squash', 'author', 'date', 'template', 'trailer', 'pathspec-from-file',
                      'directory', 'exclude', 'include', 'patch-format', 'quoted-cr', 'whitespace', 'resolvemsg',
                      'empty'}
# the options of a revert or cherry-pick that its --no-commit form keeps (commit_scan's `keep`)
COMMIT_KEPT_LONG = ('strategy', 'strategy-option')
# the long options, spelled in full, that take no next word; any other long option without `=`
# (one git does not know, or an abbreviation, which may be of one that takes a value) may take the
# next word, so a hold after it is not counted (commit_scan)
COMMIT_LONG_FLAGS = {
    'commit', 'no-commit', 'squash', 'no-squash', 'ff', 'no-ff', 'ff-only', 'edit', 'no-edit', 'signoff',
    'no-signoff', 'stat', 'no-stat', 'summary', 'no-summary', 'log', 'no-log', 'verbose', 'quiet', 'progress',
    'no-progress', 'verify', 'no-verify', 'verify-signatures', 'no-verify-signatures', 'gpg-sign', 'no-gpg-sign',
    'allow-unrelated-histories', 'autostash', 'no-autostash', 'rerere-autoupdate', 'no-rerere-autoupdate',
    'overwrite-ignore', 'no-overwrite-ignore', 'rebase', 'no-rebase', 'all', 'append', 'force', 'keep', 'prune',
    'tags', 'no-tags', 'unshallow', 'update-shallow', 'ipv4', 'ipv6', 'recurse-submodules',
    'no-recurse-submodules', 'allow-empty', 'allow-empty-message', 'keep-redundant-commits', 'no-reference',
    'amend', 'no-post-rewrite', 'reset-author', 'short', 'branch', 'porcelain', 'long', 'null', 'only',
    'interactive', 'patch', 'status', 'no-status', '3way', 'no-3way', 'keep-cr', 'no-keep-cr', 'scissors',
    'no-scissors', 'utf8', 'no-utf8', 'keep-non-patch', 'ignore-space-change', 'ignore-whitespace',
    'committer-date-is-author-date', 'ignore-date', 'message-id', 'no-message-id', 'reject', 'retry', 'binary'}


def commit_scan(sub, args):
    """(makes, revs, mainline, keep): whether git `sub` (COMMIT_VERBS) with the Words `args` after
    it makes a commit, the words that are neither options nor their values (a revert's or
    cherry-pick's commits; None after an option the scan cannot place that may take a value, so a
    word after it may be that value),
    the -m/--mainline value given, or None, and the strategy options of a revert or cherry-pick
    (-X, COMMIT_KEPT_LONG) its --no-commit form keeps, as kept_word's (lead, sep, value, dyn). Read left to right as git
    reads them: a stop word (COMMIT_STOPS) makes none; a hold (COMMIT_HOLDS, or -n in a short
    cluster of revert or cherry-pick; merge's and pull's -n is --no-stat) holds until a long option
    that undoes it (COMMIT_UNDOES), or a prefix of one, comes after it; a sequence's next step
    (COMMIT_STEPS) commits. Option values are skipped, so `-m --no-commit` is a message; `--` ends
    the options. It fails closed: after a word it cannot place (a computed word, a long option
    without `=` that COMMIT_LONG_FLAGS and COMMIT_LONG_VALUES do not name, or a short letter
    COMMIT_SHORT_FLAGS does not), no later hold or stop word counts, since that word may have taken
    it as its value."""
    stops, holds = COMMIT_STOPS.get(sub, ()), COMMIT_HOLDS.get(sub, ())
    values, attached = COMMIT_SHORT_VALUES.get(sub, ''), COMMIT_SHORT_ATTACHED.get(sub, '')
    flags = COMMIT_SHORT_FLAGS.get(sub, '')
    held, revs, mainline, unsure, keep, unplaced = set(), [], None, False, [], False
    k = 0
    while k < len(args):
        w = args[k]
        t = w.text
        k += 1
        if w.dyn or not t.startswith('-') or t == '-':
            revs.append(t)
            unsure = unsure or w.dyn
            continue
        if t == '--':
            revs.extend(x.text for x in args[k:])
            break
        if t.startswith('--'):
            body, eq, val = t[2:].partition('=')
            if t in stops and not unsure:
                return False, None if unplaced else revs, mainline, keep
            if t in COMMIT_STEPS:
                return True, None if unplaced else revs, mainline, keep
            if t in holds:
                if not unsure:
                    held.add(t)
                continue
            if len(body) >= 2:
                held -= {h for n, h in COMMIT_UNDOES.items() if n.startswith(body)}
            kept = body in COMMIT_KEPT_LONG and sub in ('revert', 'cherry-pick')
            if eq:
                if body == 'mainline':
                    mainline = val
                if kept:
                    keep.append(('--%s=' % body, '', val, False))
                continue
            if body in COMMIT_LONG_VALUES:
                if body == 'mainline' and k < len(args):
                    mainline = args[k].text
                if kept and k < len(args):
                    keep.append((t, ' ', args[k].text, args[k].dyn))
                k += 1
            elif body not in COMMIT_LONG_FLAGS:
                unsure = True
                # an abbreviation of flags alone takes no value, so the words after it stay commits
                unplaced = unplaced or any(n.startswith(body) for n in COMMIT_LONG_VALUES) or \
                    not any(n.startswith(body) for n in COMMIT_LONG_FLAGS)
            continue
        for j, c in enumerate(t[1:]):
            if c in values:
                rest, dyn = t[j + 2:], False
                if not rest:
                    rest, dyn = (args[k].text, args[k].dyn) if k < len(args) else ('', False)
                    k += 1
                if c == 'm' and sub in ('revert', 'cherry-pick'):
                    mainline = rest
                if c == 'X' and sub in ('revert', 'cherry-pick'):
                    keep.append(('-X', '', rest, dyn))
                break
            if c in attached:
                break
            if c not in flags:
                unsure = unplaced = True
                break
            if c == 'n' and sub in ('revert', 'cherry-pick') and not unsure:
                held.add('--no-commit')
    return sub in ('commit', 'am') or not held, None if unplaced else revs, mainline, keep


def git_plain_commit(event, ctx, cmd, inv, cwd, role):
    """A git verb that makes a commit (COMMIT_VERBS, commit_scan), run by an agent session
    (agent_session) in a linked worktree: plain git writes no Co-Authored-By trailer, so the gate's
    trailer check fails the branch. It runs after every other shell rule, so a refusal another
    rule owns (owner-identity, write-jail and the rest) stands. The alternative is the kit tool
    that does the job with the trailer: commit.py for a commit or a sequence's next step,
    sync_base.py for a merge or pull, the verb's --no-commit form (its -m, -X and --strategy kept) and then commit.py
    for a revert or cherry-pick, git apply --index and then commit.py for am. A --skip drops the
    change in progress and goes on with plain commits: the alternative ends the sequence, drops the
    change and takes what is left one at a time."""
    gc = git_call_judged(inv)
    if gc is None or gc.sub not in COMMIT_VERBS or not gc.dir or gc.unknown or gc.blind:
        return None
    if not agent_session(event, ctx):
        return None
    place = ctx.place(gc.dir)
    if place.kind != repo.LINKED or not place.top:
        return None
    alt = commit_alternative(ctx, gc, place.top)
    if alt is None:
        return None
    why = '`%s` makes a commit with plain git, which writes no Co-Authored-By trailer: the gate\'s trailer check ' \
          'then fails the branch. ' % bare(cmd)
    return refuse('git-plain-commit', role, why + alt[0], alt[1])


def commit_alternative(ctx, gc, where):
    """(advice, command) for git `gc` when it makes a commit (COMMIT_VERBS, commit_scan), run in
    the linked worktree `where`: the kit tool that does the job with the trailer (git_plain_commit
    names the forms); None when it makes no commit."""
    if gc.sub not in COMMIT_VERBS:
        return None
    sub = gc.sub
    makes, revs, mainline, kept = commit_scan(sub, gc.rest)
    if not makes:
        return None
    top = quote(where)
    commit = 'python %s -C %s -m "<subject>" --model "<your model>" <each path you changed>' % (
        kit_tool(ctx, 'commit'), top)
    words = {w.text for w in gc.rest}
    one = 'git -C %s apply --index <patch>' % top if sub == 'am' else \
        'git -C %s %s --no-commit <commit>' % (top, sub)
    # --quit forgets the commits or patches still to come: the advice says where to read them first
    left = 'the patches from the one in progress on are in the folder git -C %s rev-parse --git-path ' \
        'rebase-apply names' % top if sub == 'am' else \
        'the commits still to come are listed in the file git -C %s rev-parse --git-path sequencer/todo ' \
        'names' % top
    note = ' --quit forgets what is left, so read it first: %s.' % left
    if sub in ('revert', 'cherry-pick', 'am') and '--skip' in words:
        return ('--skip drops the change in progress and goes on with the rest as plain commits.%s End the sequence '
                'here, then in their own calls drop the change in progress with git -C %s reset --merge and take '
                'each one left with %s, then %s:' % (note, top, one, commit),
                'git -C %s %s --quit' % (top, sub))
    if sub == 'commit' or words & set(COMMIT_STEPS) or (sub == 'am' and '-r' in words):
        rest = ''
        if sub in ('revert', 'cherry-pick', 'am'):
            rest = ' The kit tool commits only the change in progress: it does not go on with the rest of the ' \
                   'sequence. After it, end the sequence with git -C %s %s --quit and take each one left with %s, ' \
                   'then commit.py.%s' % (top, sub, one, note)
        return 'Commit with the kit tool, which stages the paths you name and adds the trailer.%s The commit:' % rest, \
            commit
    if sub in ('merge', 'pull'):
        return ('Sync with the commit your coordinator named; the tool merges exactly it and adds the trailer (to '
                'merge anything else, run git -C %s merge --no-commit <rev>, then in its own call %s):' % (top, commit),
                'python %s -C %s <hash> --model "<your model>"' % (kit_tool(ctx, 'sync_base'), top))
    if sub == 'am':
        return ('Apply the patch to the index, then in its own call commit it with %s:' % commit,
                'git -C %s apply --index <patch>' % top)
    keep = ''
    if mainline is not None:
        keep = ' -m %s' % (mainline if mainline.isdigit() else '<n>')
    keep += ''.join(' ' + kept_word(*w) for w in kept)
    return ('Run it with --no-commit, then in its own call commit what it changed with %s:' % commit,
            'git -C %s %s --no-commit%s %s' % (top, sub, keep, ' '.join(revs or ()) or '<commit>'))


# git that python or node code given on the line runs (interpreter_git): the guard reads git only
# as a typed shell command, so every git rule (commit.py's trailer, the shared refs, the main
# checkout) is blind to it. Known limits, none read here:
# - a script file (one in a temp or scratch folder too) and python -m: only code on the line
#   (-c, -e) or on standard input (a heredoc, a here-string, text piped in) is read;
# - a program or a git name the code builds at run time ('g' + 'it', importlib, getattr);
# - other interpreters (ruby, perl, deno) and compiled programs; PowerShell's Start-Process and
#   .NET's Process;
# - a library that writes the repository itself (dulwich, pygit2, open('.git/refs/...'));
# - a line the parser cannot read: the fallback does not run this rule;
# - a git command that is not at the start of its string ('cd x && git commit', shell=True);
# - a git name the code is given from outside its text: an argument (python -c
#   "...subprocess.run(sys.argv[1:])" git commit) or an environment variable (G=git python -c
#   "...os.environ['G']...");
# - python or node run by a launcher (uv run python -c ..., npx node -e ...): the line's program
#   is the launcher, and its arguments are not read as code;
# - an option that takes the next word as its value and is not named (python's
#   --check-hash-based-pycs always, a node option outside NODE_VALUE_OPTS): its value is read as
#   the script, so the code after it is not read.
# It refuses honest code in two ways, both kept: a git read (rev-parse, log) is refused like a
# write, since a read allowance would pass a verb the code computes; and a 'git ...' string in code
# whose launch runs another program is read as git.
# a call that starts a program: subprocess, os.system and its kin, asyncio's, node's child_process
INTERPRETER_LAUNCH_RE = re.compile(r'\b(subprocess|os\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)|from os import'
                                   r'|pty\.spawn|create_subprocess_\w+|child_process)\b')
# git named as a program: a quoted string that is git or git.exe (a path before it allowed), or
# starts with `git ` (a list's first element, a shell=True string, an f-string, shutil.which('git'))
INTERPRETER_GIT_RE = re.compile(r'''(['"`])(?:[^'"`\s]*[\\/]|[A-Za-z]:[\\/][^'"`\n]*[\\/])?git(?:\.exe)?'''
                                r'''(?:\1|\s)''', re.I)
# a git verb near the git name, which only chooses the alternative's line
INTERPRETER_VERB_RE = re.compile(r'''(?:['"`]|\bgit(?:\.exe)?\s+(?:-C\s+\S+\s+)?)'''
                                 r'''(commit-tree|commit|revert|cherry-pick|am|merge|pull)(?=['"`\s])''')
INTERPRETER_COMMITS = ('commit-tree', 'commit', 'revert', 'cherry-pick', 'am')


def interpreter_git(event, ctx, cmd, inv, cwd, role):
    """git run by python or node code given on the line (rules.inline_code: -c, -e, a heredoc, a
    here-string, text piped in), in an agent session (agent_session), from any directory: one
    text of that code that both starts a program (INTERPRETER_LAUNCH_RE) and names git as one
    (INTERPRETER_GIT_RE). The guard reads git only as a typed shell command, so no git rule judges
    git that code runs. It runs after every other shell rule, so a refusal another rule owns (the
    main checkout's unplaced code, owner-identity and the rest) stands. The alternative is the git
    command typed: commit.py for a commit the code makes, sync_base.py for a merge or pull, else
    `git -C <worktree> <the git command your code ran>`; a git verb in the code only chooses it."""
    if not agent_session(event, ctx):
        return None
    texts = [t for t in inline_code(cmd, inv, skip_pipe=lambda c: True)
             if INTERPRETER_LAUNCH_RE.search(t) and INTERPRETER_GIT_RE.search(t)]
    if not texts:
        return None
    verbs = {m.group(1) for t in texts for m in INTERPRETER_VERB_RE.finditer(t)}
    top = own_worktree(ctx, cwd)
    wt = quote(top) if top else '"<worktree>"'
    commit = 'python %s -C %s -m "<subject>" --model "<your model>" <each path you changed>' % (
        kit_tool(ctx, 'commit'), wt)
    sync = 'python %s -C %s <hash> --model "<your model>"' % (kit_tool(ctx, 'sync_base'), wt)
    which = 'node' if inv.name == 'node' else 'python'
    why = ('`%s` runs git from %s code. The guard reads git only as a typed shell command, so none of its git '
           'rules (the shared refs, the main checkout, the commit trailer) judge git that code runs. Type the git '
           'command itself, one per call; a commit goes through the kit\'s commit.py and a sync through its '
           'sync_base.py. If the guard refuses the typed git command, that refusal stands: do not route it through '
           'a program or a script file.' % (hub_line(bare(cmd)), which))
    if verbs & set(INTERPRETER_COMMITS):
        return refuse('interpreter-git', role, why + ' The commit:', commit)
    if verbs & {'merge', 'pull'}:
        return refuse('interpreter-git', role, why + ' The sync:', sync)
    return refuse('interpreter-git', role, why + ' The git command:',
                  'git -C %s <the git command your code ran>' % wt)


STASH_READS = ('list', 'show', 'create')        # no entry added or taken
STASH_NAMED = ('apply', 'drop')                 # pass with the entry named: `apply <sha>`, `drop stash@{1}`


def clean_dry_run(args):
    """True when git clean with arguments `args` (the Words after `clean`) is a dry run, read in
    order as git reads it: the last of `-n` (in a short cluster), `--dry-run` (an abbreviation of
    two letters or more) and its negation `--no-dry-run` (`--no`, `--no-`, `--no-d`: git rejects
    the ambiguous ones, and a refused line deletes nothing) wins; `-fn --no-dry-run` deletes. A
    one-letter `--d` is `--dry-run` to git (-d has no long form), but is left unread: the line is
    refused (fail closed). The value of `-e`/`--exclude` (any abbreviation, `--e` too) is skipped,
    so `-f -e -n` is no dry run; a word the shell computes cancels one. Words after `--` are paths."""
    dry = False
    values = GIT_SHORT_VALUES['clean']
    k = 0
    while k < len(args):
        w = args[k]
        t = w.text
        k += 1
        if w.dyn:
            dry = False                         # a word the shell computes may be --no-dry-run
            continue
        if t == '--':
            break
        if t.startswith('--'):
            body, eq, _v = t[2:].partition('=')
            if body.startswith('no') and 'no-dry-run'.startswith(body):
                dry = False
            elif not eq and len(body) > 1 and 'dry-run'.startswith(body):
                dry = True
            elif body and 'exclude'.startswith(body) and not eq:
                k += 1
            continue
        if t.startswith('-') and len(t) > 1:
            body = t[1:]
            for j, c in enumerate(body):
                if c == 'n':
                    dry = True
                if c in values:
                    if not body[j + 1:]:
                        k += 1
                    break
    return dry


def stash_reads(args):
    """True when git stash with arguments `args` (the Words after `stash`) only reads: `list` or
    `show` as the first word. git reads a verb only there: a line that starts with an option or
    `--` is a push (`git stash -- show` stashes the path `show`) or one git refuses (`git stash -q
    list` exits 128), never a list or show (git 2.55, observed)."""
    return bool(args) and args[0].text in ('list', 'show')


def git_stash_shared(event, ctx, cmd, gc, role, place):
    """git stash in a linked worktree by an agent session (agent_session), when it adds an entry
    with no tag or takes one it does not name: every worktree of the repository shares the stash
    stack, so a bare `pop` can take another session's entry, and while the stack holds any entry
    the gate's stash guard refuses every gate and landing (docs/gate.md "The stash guard").
    Passes: list, show, create, a tagged push (-m/--message), apply or drop naming the entry,
    branch <name> <stash>. git reads a verb only as the first word after `stash`: a line that
    starts with an option or `--` is a push (`git stash -- show` stashes the path `show`) or one
    git refuses (`git stash -q list`), so only the first word is taken as the verb. An entry or branch name the shell computes
    (`drop $E`) fails closed: empty, it leaves a bare drop of the top entry. A main checkout's
    stash is git-stash's (git_main_rules)."""
    if not agent_session(event, ctx) or not stash_shared(gc):
        return None
    how, instead = stash_alternative(ctx, place.top)
    return refuse('git-stash-shared', role, '`%s` adds to or takes from the stash stack every worktree of this '
                  'repository shares; while it holds an entry every gate and landing refuses (docs/gate.md "The '
                  'stash guard"). %s' % (bare(cmd), how), instead)


def stash_shared(gc):
    """True when git stash `gc` adds an entry with no tag or takes one it does not name
    (git_stash_shared)."""
    first = gc.rest[0] if gc.rest else None
    if first is not None and (first.dyn or not first.text.startswith('-')):
        verb, args = first, gc.rest[1:]
    else:
        verb, args = None, gc.rest              # no verb, or an option or `--` first: a push (or a refused line)
    short, longs, pos = _opts('stash', args)
    if verb is not None and not verb.dyn:
        need = 2 if verb.text == 'branch' else 1
        named = len(pos) >= need and not any(w.dyn for w in pos[:need])
        if verb.text in STASH_READS or (verb.text in STASH_NAMED + ('branch',) and named):
            return False
    if (verb is None or (verb.text == 'push' and not verb.dyn)) and \
            ('m' in short or any(n and 'message'.startswith(n) for n in longs)):
        return False                            # a tagged push: its owner finds it again by the tag
    return True


def stash_alternative(ctx, where):
    """(advice, command): the WIP commit with the kit tool in the linked worktree `where` that
    takes the place of a shared stash entry (git_stash_shared)."""
    return ('Set the work aside as a WIP commit on your branch with the kit tool, which stages each path you name, '
            'a new file too, and adds the trailer:',
            'python %s -C %s -m "WIP: <what>" --model "<your model>" <each path you changed>'
            % (kit_tool(ctx, 'commit'), quote(where)))


def git_main_rules(event, ctx, cmd, inv, gc, cwd, role, place):
    """git_rules' checks of git that changes a main checkout, or a checkout git cannot tell
    (`place`: kitlib.git.repo.Place of the directory git runs in)."""
    sub, args = gc.sub, gc.rest
    text = bare(cmd)
    where = 'the main checkout %s' % place.top if place.kind == repo.MAIN else \
        'a checkout git cannot place (%s)' % place.detail
    top = place.top or '<main checkout>'
    short, longs, pos = _opts(sub, args, wide=True)
    if writes_main(sub, short, longs):
        cfg = ctx.config(place.top or gc.dir)
        if not kconfig.permissions(event, kconfig.roles(cfg)).get('edit_main_checkout'):
            why = lander_check(event, ctx, cmd, inv, gc)[1] if place.kind == repo.MAIN else None
            if why is not None:
                return lander_refusal(ctx, cmd, gc, 'git-main-history', role, why)
            how, instead = ref_delete_hint(ctx, gc, top, cwd, short, longs, pos) or \
                git_in_worktree(ctx, cmd, gc, top, cwd, agent_session(event, ctx))
            return refuse('git-main-history', role, '`%s` writes history or files in %s; this role may not change a '
                          'main checkout. %s' % (text, where, how), instead)
    no_wt, wt = new_worktree(ctx, place.top)
    if sub == 'restore':
        if not restore_index_only(short, longs):
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
        if place.kind == repo.MAIN and not kconfig.permissions(
                event, kconfig.roles(ctx.config(place.top or gc.dir))).get('edit_main_checkout'):
            # reset --keep changes the main checkout too, which git-main-history refuses this role
            mine = own_worktree(ctx, cwd, place.top)
            # <worktree> for a session the call shows no worktree of: a new one only in a clause (only_new)
            return refuse('git-reset-hard', role, '`%s` discards work in %s; this role may not change a main '
                          'checkout. In your worktree%s, reset --keep refuses to drop uncommitted changes:'
                          % (text, where, '' if mine or no_wt or not wt else only_new(wt)),
                          'git -C %s reset --keep %s' % (quote(mine or '<worktree>'), rev))
        return refuse('git-reset-hard', role, '`%s` discards work in %s. Reset --keep refuses to drop uncommitted '
                      'changes:' % (text, where), 'git -C %s reset --keep %s' % (quote(top), rev))
    if sub == 'clean':
        if clean_dry_run(args):
            return None
        return refuse('git-clean', role, '`%s` deletes untracked files in %s. Remove what must go one named path at a '
                      'time; to list them:' % (text, where), 'git -C %s clean -n' % quote(top))
    if sub == 'stash':
        if stash_reads(args):
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
             'verify-tag', 'show-branch', 'fsck', 'patch-id'}
# git apply --check applies nothing, nor do --stat, --numstat and --summary: each turns applying
# off, and only --apply turns it back on (git 2.55, observed). These are the only options such an
# apply may carry, spelt in full: git expands a unique prefix (--app is --apply, which applies
# after all), so any other option fails closed (apply_checks)
APPLY_REPORTS = ('--check', '--stat', '--numstat', '--summary')
APPLY_CHECK_OPTS = set(APPLY_REPORTS) | {'-v', '--verbose', '-R', '--reverse', '--'}
APPLY_STRIP_RE = re.compile(r'-p\d+$')
# subcommands that only read when their first word that is no option is one of these (None: when
# they have none); stash's verb is its first word of all (stash_reads)
GIT_READ_VERBS = {'worktree': ('list',), 'remote': (None, 'show', 'get-url'),
                  'reflog': (None, 'show', 'exists'), 'notes': (None, 'list', 'show'),
                  'submodule': (None, 'status', 'summary')}
GIT_LIST_FLAGS = {'-l', '--list', '--show-current', '--merged', '--no-merged', '--contains', '--no-contains',
                  '--points-at'}
# git symbolic-ref <name> with only these options, spelt in full, prints where <name> points (a
# second name, -d/--delete or -m writes it)
SYMREF_READ_OPTS = {'-q', '--quiet', '--short', '--recurse', '--no-recurse'}
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
    return list(gc.rest[:gc.synth]) + [sh.Word(cm.passed(cmd, w), w.dyn, w.start, w.end, w.synth)
                                       for w in gc.after()]


def archive_filter_set(gc):
    """True when the line may give git archive `gc` a program to run that the checkout's own config
    does not, or the guard cannot tell: a git the text cannot place (gc.blind: --git-dir,
    --work-tree, --config-env, a GIT_DIR-like variable), a commands.GIT_ENV_NAMES variable the
    line sets (commands.mark_git_env), a user config moved (HOME and the like, Command.home_env), PATH changed (Command.path_env), and
    before the subcommand --exec-path (it and PATH say where git finds a filter's bare program
    name, as Git for Windows' filter.lfs.process = git-lfs), a word the guard cannot read
    (computed, or one PowerShell or cmd may split: native_unsure) or a -c whose key, read as the
    shell hands it to git (commands.passed), is not on ARCHIVE_SAFE_KEY_RE."""
    cmd = gc.inv.cmd
    if gc.blind or cmd.git_env or cmd.home_env or cmd.path_env:
        return True
    pre = gc.before()
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
    rewrite (retarget) keeps the option; else the whole word's. A word with no span in the line
    (one an alias's value gave, inline_alias) gives a value with none either."""
    if w.start is None or w.end is None:
        return sh.Word(value, w.dyn, None, None)
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
    option the guard does not know (a --no-* among them) and no word that is not literal (an unquoted $X may hold -o) or that PowerShell or cmd
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
            if opt is None:
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
    allows, no PowerShell splat (ps_splat), no --output file, and branch, tag and config only with
    options it names in full (strict_read_opts). symbolic-ref reads with one ref and only
    SYMREF_READ_OPTS."""
    sub = gc.sub
    words = [w.text for w in gc.rest]
    if strict and (any(ps_splat(gc, w) for w in gc.rest) or any(writes_output_file(t) for t in words)):
        return False
    if strict and any(w.dyn for w in gc.rest) and not strict_dyn_read(gc):
        return False
    if sub == 'archive':
        # its -o file is a write: git_targets hands it to git_jail, which judges it first; where
        # the checkout cannot be placed, neither can a relative -o, so it is no plain read
        outs, read = archive_outputs(gc)
        return read and not (strict and outs) and not archive_filter_set(gc)
    if sub in GIT_READS:
        return True
    if sub == 'stash':
        return stash_reads(gc.rest)
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
        return config_reads(gc)
    if sub == 'apply':
        return apply_checks(gc)
    if sub == 'symbolic-ref':
        return len(pos) == 1 and all(t in SYMREF_READ_OPTS for t in words if t.startswith('-'))
    return False


def apply_checks(gc):
    """True when git apply `gc` only checks or reports on its patch: one of APPLY_REPORTS among its
    options (the words before the first --; after it git reads every word as a patch file,
    `--check` too) and every option one APPLY_CHECK_OPTS names (or -p<n>; --apply, which applies
    after all, is not one), read as the shell hands it to git (archive_words), and no
    word the guard cannot read (computed, a PowerShell splat, or one PowerShell or cmd may split:
    native_unsure). Any other apply writes the checkout."""
    if gc.inv is None:
        return False
    words = archive_words(gc)
    if any(w.dyn or ps_splat(gc, w) or native_unsure(gc.inv.cmd, w) for w in words):
        return False
    texts = [w.text for w in words]
    if '--' in texts:                           # git reads no option after --: those words are patches
        texts = texts[:texts.index('--')]
    opts = [t for t in texts if t.startswith('-') and t != '-']
    return any(t in APPLY_REPORTS for t in opts) and all(t in APPLY_CHECK_OPTS or APPLY_STRIP_RE.match(t)
                                                          for t in opts)


def config_reads(gc):
    """True when git config `gc` carries a read flag (CONFIG_READ_OPTS, spelt in full) as its own
    option. git config reads options only up to its first word that is no option (git 2.55,
    observed: `config a.b --list` sets a.b to "--list"), so the flag counts only there, before any
    `--` and not as the value of an option that takes one (-f/--file, -t/--type, --blob, --default,
    --comment, --value, --url, abbreviated too: _config_long). A computed word there ends the scan
    (fail closed), as does a first `--`, which git 2.55 passes over (`config -- --list` lists)."""
    words, k = gc.rest, 0
    while k < len(words):
        w, t = words[k], words[k].text
        if w.dyn or t == '--' or len(t) < 2 or not t.startswith('-'):
            return False
        if t in CONFIG_READ_OPTS:
            return True
        if t.startswith('--'):
            body, eq, _v = t[2:].partition('=')
            if not eq and any(n in CONFIG_LONG_VALUES for n in _config_long(body)):
                k += 1                          # its value, whatever it says
        else:
            at = min((t.find(c, 1) for c in 'ft' if c in t[1:]), default=-1)
            if at == len(t) - 1:
                k += 1                          # -f <file>, -t <type>: the value is the next word
        k += 1
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
    pre = gc.before()
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
    of commands.GIT_ENV_NAMES set, --git-dir, --work-tree, -c core.worktree, --config-env, a
    computed -C; a -C after any of them does not place it), by any role: it may be a main
    checkout, and the main-checkout git rules judge only history writes there, so a symbolic-ref
    or branch -m that re-points main's HEAD would pass them. Only a plain read passes
    (git_reads strict; a computed word only as strict_dyn_read allows). The hint names what hides the place (git_unplaced): drop it, or name a
    computed -C literally. A computed -C is refused under UNPLACED (warn by default); the
    re-pointing forms (gc.blind) stay write-jail."""
    if git_reads(gc, strict=True):
        return None
    if gc.blind:                                # a deliberate re-pointing: denied, not unplaced
        how, instead = git_unplaced(ctx, cmd, gc, cwd, True)
        return refuse('write-jail', role, '`%s` writes a checkout the guard cannot place: %s can point git at a '
                      'git directory, work tree, index or config the text does not show, and a -C does not undo '
                      'it. %s' % (bare(cmd), gc.blind, how), instead)
    return refuse(UNPLACED, role, '`%s` writes a checkout the guard cannot place (a computed -C, or a '
                  'folder it cannot tell). Name the checkout literally:' % bare(cmd),
                  ('git -C "<checkout>" %s %s' % (gc.sub, ' '.join(w.text for w in gc.rest))).rstrip())


def git_jail(event, ctx, cmd, inv, cwd, role):
    """An agent session's git that writes a checkout (or makes a folder) outside its roots
    (kitlib.guard.jail): git -C <elsewhere> commit, a cd chain into another checkout, worktree
    add or clone to a folder outside them. Its repository's other worktrees (jail.sibling) are
    not elsewhere."""
    gc = git_call_judged(inv)
    if gc is None or not agent_session(event, ctx):
        return None
    if not gc.dir:                              # commands.git_call: no place the text can tell
        return git_jail_unknown(ctx, cmd, gc, cwd, role)
    if gc.unknown:
        return None                             # a computed subcommand: the unreadable rule's
    view = None
    for w in git_targets(gc):
        if w.dyn:
            continue
        target = sh.resolve_path(w.text, gc.dir, home=getattr(ctx, 'home', None))
        if not target:
            continue
        view = view or jail_view(event, ctx, cwd)
        rs, dev, dropped = view
        verdict, root = jail.judge(target, rs)
        what = '`%s` makes %s' % (bare(cmd), w.text)
        allowed = verdict is None or (verdict == jail.OUTSIDE and (jail.sibling(ctx, target, cwd) or
                                                                   in_dropped(target, dropped, dev)))
        if allowed and (not dev or jail.on_drive(target, dev)):
            continue
        # a word an inline alias gave has no span in the line to aim (inline_alias)
        rewrite = None if w.start is None else git_retarget(ctx, cmd, inv, gc, w, cwd)
        if allowed:
            d = dev_drive_bound(event, ctx, cwd, rs, dev, target, role, what, rewrite)
            if d:
                return d
            continue
        return jail_refusal(role, rs, verdict, root, what, target, rewrite=rewrite)
    if git_reads(gc):
        return None
    rs, dev, dropped = view or jail_view(event, ctx, cwd)
    changes = '`%s` changes the checkout %s' % (bare(cmd), fwd(gc.dir))
    landed, why = lander_check(event, ctx, cmd, inv, gc)
    if landed:
        return dev_drive_bound(event, ctx, cwd, rs, dev, gc.dir, role, changes)
    d = pm_data_unknown(ctx, gc.dir, cwd, role, '`%s`' % bare(cmd))
    if d:
        return d
    verdict, root = jail.judge(gc.dir, rs)
    if verdict is None or (verdict == jail.OUTSIDE and (jail.sibling(ctx, gc.dir, cwd) or in_dropped(gc.dir, dropped, dev))):
        return dev_drive_bound(event, ctx, cwd, rs, dev, gc.dir, role, changes)
    if why is not None:
        return lander_refusal(ctx, cmd, gc, 'write-jail', role, why)
    what = '`%s` changes the checkout %s' % (bare(cmd), fwd(gc.dir))
    dest = jail.destination(rs)
    if dest is None:
        return jail_refusal(role, rs, verdict, root, what, gc.dir)
    if ctx.place(dest.path).kind not in (repo.MAIN, repo.LINKED):
        # the folder it may write is no checkout (the temp folder, a plain folder): git there has no
        # repository to act on, so no same command is offered
        return refuse('write-jail', role, jail_why(rs, verdict, root, what, gc.dir) +
                      ' To see where this session runs:', 'pwd')
    if verdict == jail.OUTSIDE:
        why = '%s: it is outside the folders an agent session may write: %s. %s' % (what, jail.describe(rs), JAIL_ASK)
    else:
        why = jail_why(rs, verdict, root, what, gc.dir)
    note, instead = git_elsewhere(ctx, cmd, gc, dest.path, True)
    return refuse('write-jail', role, why + (' In %s, %s' % (fwd(dest.path), note) if note else
                                             ' The same command in %s:' % fwd(dest.path)), instead)


def git_elsewhere(ctx, cmd, gc, where, agent):
    """(note, command): git `gc` run again in the checkout `where`, `git -C <where> <sub> <rest>`
    and no note. In a linked worktree an agent session's commit (commit_alternative) or shared
    stash entry (stash_shared) is refused there too (git_plain_commit, git_stash_shared): the kit
    tool's form, the note saying why."""
    plain = ('', ('git -C %s %s %s' % (quote(where), gc.sub, git_rest_text(cmd, gc))).rstrip())
    if not agent:
        return plain
    place = ctx.place(where)
    if place.kind != repo.LINKED or not place.top:
        return plain
    alt = commit_alternative(ctx, gc, place.top)
    if alt is not None:
        return ('plain git writes no Co-Authored-By trailer, which the gate\'s trailer check fails. ' + alt[0],
                alt[1])
    if gc.sub == 'stash' and stash_shared(gc):
        how, instead = stash_alternative(ctx, place.top)
        return 'the stash stack is shared by every worktree of the repository. ' + how, instead
    return plain


def git_retarget(ctx, cmd, inv, gc, w, cwd):
    """rewrite(path) for the refused output word `w` of git `gc`: the same command with `w` aimed
    at `path`. A git archive with no -C of its own also names the checkout it archives: an agent's
    shell keeps no cd between calls, so the bare command run again from a main checkout archives
    main, not the branch. That is the session's own worktree of the main checkout git runs in
    (`<worktree>` when it has none, its output aimed there too), else the folder git runs in."""
    def rewrite(path):
        if gc.sub != 'archive' or inv.via is not None or inv.word is None or inv.word.start is None or \
                any(x.text.startswith('-C') for x in gc.before()):
            return retarget(cmd, w, path)
        place = ctx.place(gc.dir)
        where = gc.dir
        if place.kind == repo.MAIN and place.top:
            where = own_worktree(ctx, cwd, place.top) or '<worktree>'
            top = paths.norm(place.top)
            if jail.inside(paths.norm(path), top):
                path = fwd(where) + fwd(paths.norm(path, fold=False)[len(top):])
        line = retarget(cmd, w, path)
        at = inv.word.end - min(cmd.words[0].start, w.start)
        return line[:at] + ' -C ' + quote(fwd(where)) + line[at:]
    return rewrite


def git_in_worktree(ctx, cmd, gc, main_top, cwd, agent=False):
    """(sentence, command): the same git command in the session's own worktree of this main
    checkout, when it has one (git_elsewhere: an agent session's commit there is the kit tool's),
    else the worktree to make; git_unplaced when what hides the place is in the text."""
    if gc.blind:
        return git_unplaced(ctx, cmd, gc, cwd, agent)
    mine = own_worktree(ctx, cwd, main_top)
    if mine:
        note, instead = git_elsewhere(ctx, cmd, gc, mine, agent)
        return 'Run it in your worktree:' + (' ' + note if note else ''), instead
    no_wt, instead = new_worktree(ctx, main_top)
    if no_wt:
        return no_wt, instead
    return 'Work in a worktree and run git %s there; the lander brings it to main:' % gc.sub, instead


def ref_delete_hint(ctx, gc, top, cwd, short, longs, pos, cleanup=False):
    """(sentence, command) for `git update-ref -d <ref>`, or None. Every worktree of a repository
    shares its refs, so neither a worktree nor the lander carries a ref delete to main: a branch
    goes with git branch -d, which refuses an unmerged one; a stray ref (stray_ref) is shown, and
    a coordinator deletes it in its own repository (`cleanup`: the session is one); any other ref
    is shown, and the PM deletes it; a guarded main is never offered to branch -d. Both run in
    the session's own worktree of this main checkout when it has one. None too for a ref
    git_shared_ref lets any session delete (scratch_ref): only where it runs is wrong, and
    git_in_worktree says so."""
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
    if name and stray_ref(name):
        who = 'you delete it in your own repository, naming the ref literally' if cleanup else \
            'your coordinator deletes it from its own session in its own repository, so send it the ref'
        return ('%s A ref outside refs/heads, refs/tags, refs/stash and refs/backlog is a coordinator\'s '
                'to delete: %s. To show it:' %
                (shared, who), 'git -C %s show-ref %s' % (at, name))
    return ('%s A ref other than a branch is the PM\'s to delete; to show it:' % shared,
            'git -C %s show-ref %s' % (at, name or '<ref>'))


# git_shared_ref: the update-ref options it reads (GIT_LONGS['update-ref'] spells the long ones),
# the refs any agent session may write (scratch_ref), and the names git reads as one loose ref file
UPDATE_REF_SHORTS = set('mdz')
PER_WORKTREE_REFS = ('refs/bisect/', 'refs/worktree/', 'refs/rewritten/')
PSEUDOREF_RE = re.compile(r'^[A-Z][A-Z0-9_]*_HEAD$')
REF_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]*$')
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


# the main sessions that may delete a stray ref in their own repository (the coordinator only),
# and the refs that are no stray ref, compared without case: NTFS reads refs/HEADS/main as the
# loose file of refs/heads/main
REF_CLEANUP_MARKS = ('coordinator',)
NOT_STRAY = ('refs/heads', 'refs/tags', 'refs/stash', 'refs/backlog')


def stray_ref(name):
    """True for a ref a coordinator may delete in its own repository: a ref_name_ok name under
    refs/, outside refs/heads, refs/tags, refs/stash (every worktree's stash entries) and
    refs/backlog (the backlog store, in any case)."""
    low = (name or '').lower()
    return ref_name_ok(name) and low.startswith('refs/') and not low.startswith(NOT_STRAY)


def cleanup_session(event, ctx):
    """True for a main session whose mark (session_mark: None for every subagent, a call a host
    cannot tell apart included) is one of REF_CLEANUP_MARKS, case ignored."""
    return str(session_mark(event, ctx) or '').lower() in REF_CLEANUP_MARKS


def _deref_unsure(place, name):
    """Why `git update-ref -d <name>` without --no-deref may delete another ref (git follows a
    symbolic ref and deletes where it points), or None: a reftable repository, where the guard
    reads no ref file, a loose ref file that does not read, or one that is a symbolic ref."""
    if os.path.isdir(os.path.join(place.common, 'reftable')):
        return 'its refs are in a reftable, where the guard cannot tell a symbolic ref'
    text, why = _read_ref(os.path.join(place.common, *name.split('/')))
    if text is None:
        return why
    if text.startswith('ref:'):
        return 'it is a symbolic ref to %s, and git deletes that ref instead' % text[4:].strip()
    return None


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


def _in_temp(ctx, path):
    """True when `path` lies in a temp root (jail.temp_roots). It lifts checks, so it compares with
    the jail's strict fold (jail.real strict: on Linux a mount in doubt does not fold)."""
    strict = paths.posix()
    real = jail.real(path, strict)
    return any(jail.inside(real, jail.real(r, strict)) for r in jail.temp_roots(ctx))


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


def _head_ref(place):
    """The ref the HEAD file of `place`'s git directory names, '' when HEAD is detached, None when
    there is no such file or it cannot be read."""
    if not place.git_dir:
        return None
    text, _ = _read_ref(os.path.join(place.git_dir, 'HEAD'))
    if not text:
        return None
    return text[4:].strip() if text.startswith('ref:') else ''


def _own_branch(ctx, place, at):
    """The branch checked out in the linked worktree `place` (refs/heads/<name>), or None: a
    detached or unreadable HEAD, a guarded main, the PM data worktree or one the guard cannot tell."""
    if place.kind != repo.LINKED or not place.git_dir or ctx.is_pm_data(at) or ctx.pm_data_error(at):
        return None
    own = _head_ref(place)
    if not own or not own.startswith('refs/heads/') or not ref_name_ok(own):
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


def _stray_delete(ctx, gc, cwd, place, name, longs, text, top, role):
    """A coordinator's `git update-ref -d <stray_ref>`: None (allowed) in its own repository (the
    main checkout `top` of the place git runs in is the one of its session's folder `cwd`) when
    git deletes that ref itself (--no-deref, or _deref_unsure finds no other ref it may reach);
    else the refusal."""
    mine = ctx.main_top(cwd) if cwd else None
    at = quote(top or gc.dir)
    if not top or not mine or paths.norm(mine) != paths.norm(top):
        return refuse('git-shared-ref', role, '`%s` deletes %s in a repository other than the one your session '
                      'runs in (%s): a coordinator deletes a stray ref only in its own repository, and that '
                      'repository\'s coordinator deletes this one. To show it:' % (text, name, fwd(mine or cwd or '?')),
                      'git -C %s show-ref %s' % (at, name))
    why = None if 'no-deref' in longs else _deref_unsure(place, name)
    if why is None:
        return None
    return refuse('git-shared-ref', role, '`%s` may delete another ref than %s: %s. Delete the ref itself with '
                  '--no-deref:' % (text, name, why), 'git -C %s update-ref --no-deref -d %s' % (at, name))


SHARED_REFS = 'every worktree of this repository shares its refs, so a change made from any of them is made for all'
SHARED_REF_ALLOWED = 'An agent session writes refs/tmp/<name> (its scratch space), the per-worktree refs ' \
                     '(refs/bisect/, refs/worktree/, refs/rewritten/, <NAME>_HEAD) and, in its own worktree, HEAD ' \
                     'and the branch checked out there; a coordinator deletes a ref outside refs/heads, refs/tags, ' \
                     'refs/stash and refs/backlog in its own repository; any other ref is the PM\'s.'


def _shared_scene(ctx, gc, cwd):
    """(place, top, at, why) of a ref write by git `gc`: the Place git runs in (None when the text
    does not name it), its main checkout, the checkout a refusal's Instead names (the session's own
    worktree of it when it has one), and why the guard cannot judge that place (None when it can)."""
    why = None
    if gc.blind or gc.unknown or not gc.dir:
        why = 'the repository it runs in is unknown (%s)' % (gc.blind or 'a directory the text does not name')
    place = ctx.place(gc.dir) if gc.dir and not gc.unknown and not gc.blind else None
    if not why and place.kind not in (repo.MAIN, repo.LINKED):
        why = 'git cannot place %s (%s)' % (fwd(gc.dir), place.detail or place.kind)
    top = (ctx.main_top(gc.dir) if gc.dir and not gc.unknown else None) or (place.top if place else None)
    at = own_worktree(ctx, cwd, top) if top else own_worktree(ctx, cwd)
    at = at or top or (ctx.main_top(cwd) if cwd else None) or cwd
    return place, top, at, why


def git_shared_ref(event, ctx, cmd, gc, cwd, role):
    """`git update-ref` by a held session (shared_ref_held), from any checkout: every worktree of a
    repository shares its refs, so a worktree reaches main as surely as the main checkout. It may
    write scratch_ref names, and in a linked worktree its HEAD and the branch checked out there
    (never a guarded main); it deletes only scratch_ref names, and a coordinator's main session
    (cleanup_session) a stray_ref in its own repository too (_stray_delete). A ref is judged by its name; HEAD
    (unless --no-deref) by the branch it is on too, so a worktree on a guarded main does not move
    it. A scratch repository (_scratch_repo: in a temp root, no
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
    place, top, at, where = _shared_scene(ctx, gc, cwd)
    if _scratch_repo(ctx, place):
        return None
    unread = unread or where
    shared, allowed = SHARED_REFS, SHARED_REF_ALLOWED
    cleanup = cleanup_session(event, ctx)
    if unread:
        hint = ref_delete_hint(ctx, gc, top or at, cwd, short, longs, pos, cleanup) if 'd' in short else None
        if hint:
            return refuse('git-shared-ref', role, '`%s`: %s, and %s. %s' % (text, unread, shared, hint[0]), hint[1])
        return refuse('git-shared-ref', role, '`%s`: the guard cannot tell which ref it writes: %s; %s. Name one '
                      'ref literally. %s To keep a commit in your scratch space:' % (text, unread, shared, allowed),
                      'git -C %s update-ref refs/tmp/<name> <new-oid>' % quote(at))
    name = pos[0].text
    # the PM data worktree answers as MAIN; while the guard cannot tell it, any linked worktree may be it
    linked = place.kind == repo.LINKED and not ctx.pm_data_error(gc.dir)
    own = _own_branch(ctx, place, gc.dir)
    chain, why = [name], None
    if name == 'HEAD' and 'no-deref' not in longs:
        head = _head_ref(place)
        if head is None:
            chain, why = None, 'the HEAD file of %s cannot be read' % fwd(gc.dir)
        elif head:
            chain.append(head)
    if chain is not None:
        bad = [r for r in chain if not _ref_ok(r, delete, linked, own)]
        if not bad:
            return None
        if cleanup and 'd' in short and stray_ref(name):
            return _stray_delete(ctx, gc, cwd, place, name, longs, text, top, role)
        why = '%s is not one it may %s' % (bad[0] if bad[0] == name else '%s, where %s points,' % (bad[0], name),
                                           'delete' if delete else 'write')
    hint = ref_delete_hint(ctx, gc, top or at, cwd, short, longs, pos, cleanup) if 'd' in short else None
    if hint:
        return refuse('git-shared-ref', role, '`%s` deletes %s: %s. %s' % (text, name, why, hint[0]), hint[1])
    return refuse('git-shared-ref', role, '`%s` %s %s: %s; %s. %s To show it:' % (
        text, 'deletes' if delete else 'moves', name, why, shared, allowed), 'git -C %s show-ref %s' % (quote(at), name))


# ---------------------------------------------------------------- other git forms that write refs
# git-shared-ref judges these as it judges update-ref (_judge_refs): a push into the repository
# itself (git_shared_push), a fetch or pull refspec's destination (a forced `tag <name>` too),
# symbolic-ref with two names or -d, replace, a notes write, a forced or moving branch, a forced
# or deleted tag, checkout -B and switch -C, --ignore-other-worktrees, worktree add -B or -f,
# rebase --update-refs, a remote's remove, rename, set-head or mirror add, fast-import and
# filter-branch (git_shared_ref_forms; branch after git_branch_delete, worktree after
# git_worktree_remove). Known limits (docs/guard.md "What git-shared-ref does not read"): refspecs, remotes and
# rebase.updateRefs git takes from its config files, a named remote whose URL is this repository,
# a fetch or pull refspec the shell computes whole, a push into another local repository, rebase
# <upstream> <branch> (it moves that branch), git replay and filter-repo.
SHARED_REF_FORMS = ('fetch', 'pull', 'symbolic-ref', 'replace', 'notes', 'tag', 'checkout', 'switch', 'rebase',
                    'remote', 'fast-import', 'filter-branch')
# per form: (long flags, long options that take a value, short flags, short options that take a
# value, short options whose value may only be attached)
FETCH_FLAGS = ('all', 'append', 'atomic', 'dry-run', 'write-fetch-head', 'force', 'keep', 'multiple',
               'auto-maintenance', 'auto-gc', 'write-commit-graph', 'prefetch', 'prune', 'prune-tags', 'tags',
               'recurse-submodules', 'quiet', 'verbose', 'progress', 'update-head-ok', 'update-shallow',
               'unshallow', 'ipv4', 'ipv6', 'show-forced-updates', 'set-upstream', 'stdin', 'porcelain',
               'negotiate-only')
FETCH_VALUES = ('upload-pack', 'jobs', 'depth', 'deepen', 'shallow-since', 'shallow-exclude', 'refmap',
                'server-option', 'negotiation-tip', 'filter', 'submodule-prefix', 'recurse-submodules-default')
PULL_FLAGS = FETCH_FLAGS + ('commit', 'edit', 'ff', 'ff-only', 'squash', 'stat', 'summary', 'compact-summary',
                            'verify', 'verify-signatures', 'autostash', 'allow-unrelated-histories', 'rebase',
                            'signoff', 'log', 'gpg-sign')
PULL_VALUES = FETCH_VALUES + ('strategy', 'strategy-option', 'cleanup')
BRANCH_FLAGS = ('delete', 'move', 'copy', 'force', 'list', 'all', 'remotes', 'verbose', 'quiet', 'track',
                'unset-upstream', 'color', 'abbrev', 'column', 'omit-empty', 'show-current', 'create-reflog',
                'edit-description', 'ignore-case', 'recurse-submodules')
BRANCH_VALUES = ('set-upstream-to', 'contains', 'no-contains', 'merged', 'no-merged', 'sort', 'points-at', 'format')
TAG_FLAGS = ('list', 'delete', 'verify', 'annotate', 'sign', 'edit', 'force', 'create-reflog', 'color', 'column',
             'omit-empty', 'ignore-case')
TAG_VALUES = ('message', 'file', 'trailer', 'cleanup', 'local-user', 'contains', 'no-contains', 'merged',
              'no-merged', 'sort', 'points-at', 'format')
CHECKOUT_FLAGS = ('quiet', 'progress', 'merge', 'detach', 'force', 'ours', 'theirs', 'patch', 'guess', 'overlay',
                  'overwrite-ignore', 'ignore-other-worktrees', 'ignore-skip-worktree-bits', 'pathspec-file-nul',
                  'recurse-submodules', 'track', 'auto-advance')
CHECKOUT_VALUES = ('conflict', 'orphan', 'unified', 'inter-hunk-context', 'pathspec-from-file')
SWITCH_FLAGS = ('guess', 'discard-changes', 'quiet', 'recurse-submodules', 'progress', 'merge', 'detach', 'force',
                'overwrite-ignore', 'ignore-other-worktrees', 'track', 'auto-advance')
SWITCH_VALUES = ('create', 'force-create', 'conflict', 'orphan')
REBASE_FLAGS = ('keep-base', 'verify', 'quiet', 'verbose', 'stat', 'signoff', 'committer-date-is-author-date',
                'reset-author-date', 'ignore-whitespace', 'force-rebase', 'ff', 'continue', 'skip', 'abort', 'quit',
                'edit-todo', 'show-current-patch', 'apply', 'merge', 'interactive', 'rerere-autoupdate',
                'autosquash', 'update-refs', 'gpg-sign', 'autostash', 'rebase-merges', 'fork-point', 'root',
                'reschedule-failed-exec', 'reapply-cherry-picks', 'keep-empty')
REBASE_VALUES = ('onto', 'trailer', 'whitespace', 'empty', 'exec', 'strategy', 'strategy-option')
WORKTREE_ADD_FLAGS = ('force', 'detach', 'checkout', 'lock', 'orphan', 'quiet', 'track', 'guess-remote',
                      'relative-paths')
REF_FORM_OPTS = {'fetch': (FETCH_FLAGS, FETCH_VALUES, 'afkmnPpqtuv46', 'jo', ''),
                 'pull': (PULL_FLAGS, PULL_VALUES, 'aefknpqrtuv46', 'joXs', 'S'),
                 'symbolic-ref': (('quiet', 'delete', 'short', 'recurse', 'no-recurse'), (), 'qd', 'm', ''),
                 'branch': (BRANCH_FLAGS, BRANCH_VALUES, 'vqrladDmMcCfi', 'u', 't'),
                 'tag': (TAG_FLAGS, TAG_VALUES, 'ldvasefi', 'mFu', 'n'),
                 'checkout': (CHECKOUT_FLAGS, CHECKOUT_VALUES, 'qmdf23plt', 'bBU', ''),
                 'switch': (SWITCH_FLAGS, SWITCH_VALUES, 'qmdft', 'cC', ''),
                 'rebase': (REBASE_FLAGS, REBASE_VALUES, 'qvnfmik', 'xsXC', 'Sr'),
                 'worktree add': (WORKTREE_ADD_FLAGS, ('reason',), 'fdq', 'bB', ''),
                 'remote add': (('fetch', 'tags', 'mirror'), ('track', 'master'), 'f', 'tm', '')}
# git replace that only lists: these options (and --format=<f>), with a pattern only after -l
REPLACE_LIST_OPTS = ('-l', '--list')
NOTES_READ_VERBS = (None, 'list', 'show', 'get-ref')
REMOTE_FETCH_KEY_RE = re.compile(r'^remote\.(.+)\.fetch$')
PRUNE_TAGS_KEY_RE = re.compile(r'^(?:fetch|remote\..+)\.prunetags$')
FALSE_WORDS = ('false', 'no', 'off', '0')


def _computed(w):
    """True for a Word whose text is not what git gets: computed, an alias's, or one bash changes."""
    return bool(w.dyn or w.alt is not None or w.synth)


def _long_hit(known, body):
    """(name, negated) of the long option `body` (the text after `--`, before any `=`) among the
    names `known`: its exact name, a `no-` one, or the one name it abbreviates; None when it names
    none or more than one."""
    neg = body.startswith('no-') and body not in known
    stem = body[3:] if neg else body
    hits = [stem] if stem in known else [n for n in known if n.startswith(stem)] if stem else []
    return (hits[0], neg) if len(hits) == 1 else None


def _form_words(sub, args):
    """(short flags, {long name: [(value text, computed)]}, positional Words, unread option texts)
    of git `sub`'s arguments by REF_FORM_OPTS: a long option is its exact name, a `no-` one, or the
    one name it abbreviates; one the table does not name, or an ambiguous prefix, is unread."""
    return _form_scan(sub, args)[:4]


def _form_scan(sub, args):
    """_form_words, and the option names in the order given (a long one by name, a negated one as
    no-<name>, a short flag as -<letter>) and how many positional words come before a `--`."""
    flags, values, s_flags, s_values, s_attached = REF_FORM_OPTS[sub]
    known = set(flags) | set(values)
    shorts, longs, pos, unread, seq = set(), {}, [], [], []
    k = 0
    npre = None
    while k < len(args):
        w = args[k]
        t = w.text
        k += 1
        if t == '--':
            npre = len(pos)
            pos.extend(args[k:])
            break
        if t.startswith('--'):
            body, eq, v = t[2:].partition('=')
            hit = _long_hit(known, body)
            if hit is None:
                unread.append(t)
                continue
            name, neg = hit
            seq.append('no-' + name if neg else name)
            if name in values and not neg:
                if not eq and k < len(args):
                    v, computed = args[k].text, _computed(args[k])
                    k += 1
                else:
                    computed = _computed(w)
                longs.setdefault(name, []).append((v, computed))
            else:
                longs.setdefault('no-' + name if neg else name, []).append((v, _computed(w)))
            continue
        if t.startswith('-') and len(t) > 1:
            body = t[1:]
            for j, c in enumerate(body):
                if c in s_values:
                    if not body[j + 1:] and k < len(args):
                        longs.setdefault('-' + c, []).append((args[k].text, _computed(args[k])))
                        k += 1
                    else:
                        longs.setdefault('-' + c, []).append((body[j + 1:], _computed(w)))
                    break
                if c in s_attached:
                    break
                if c in s_flags:
                    shorts.add(c)
                    seq.append('-' + c)
                else:
                    unread.append('-' + c)
            continue
        pos.append(w)
    return shorts, longs, pos, unread, seq, len(pos) if npre is None else npre


def fetch_dst_ref(dst):
    """The local ref a fetch refspec's destination names (git's get_local_ref): a refs/ name as
    it is, heads/, tags/ and remotes/ under refs/, any other name a branch."""
    if dst.startswith('refs/'):
        return dst
    if dst.startswith(('heads/', 'tags/', 'remotes/')):
        return 'refs/' + dst
    return 'refs/heads/' + dst


def _tracking_ref(src, dst, remote):
    """True for the remote-tracking map a fetch makes: `dst` refs/remotes/<remote>/<x> (remote and
    folder compared without case) from `src` <x> or refs/heads/<x>, one `*` in both allowed; <x>
    never HEAD."""
    if not remote or not ref_name_ok(remote):
        return False
    prefix = 'refs/remotes/%s/' % remote
    if dst[:len(prefix)].lower() != prefix.lower():
        return False
    x = dst[len(prefix):]
    if not x or x == 'HEAD' or x.count('*') > 1 or not ref_name_ok(x.replace('*', 'x')):
        return False
    return src in (x, 'refs/heads/' + x)


def fetch_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None): the refs git fetch or pull `gc` writes
    beyond FETCH_HEAD and the remote-tracking map (_tracking_ref): each refspec destination after
    the remote (fetch_dst_ref; a `^` exclusion and an empty destination write nothing), each
    --refmap and inline -c remote.<name>.fetch mapping, local tags under --prune-tags (or an inline
    *.pruneTags), a forced --tags and a forced `tag <name>`. Unread: --stdin, a refspec the shell
    computes with a literal `:` or `+`, a forced `tag` whose name it computes, an option the guard
    does not read on a line with a `:` word."""
    shorts, longs, pos, unknown = _form_words(gc.sub, gc.rest)
    remote = pos[0] if pos else None
    rname = remote.text if remote is not None and not _computed(remote) else None
    # with --multiple every word names a remote (and --all takes none)
    many = 'm' in shorts or 'multiple' in longs
    specs, tagged = [], []          # tagged: (name text, computed) of each `tag <name>` (refs/tags/<name>)
    words = [] if many else pos[1:]
    j = 0
    while j < len(words):
        w = words[j]
        if w.text == 'tag' and not _computed(w) and j + 1 < len(words):
            tagged.append((words[j + 1].text, _computed(words[j + 1])))
            j += 2
            continue
        specs.append((w.text, _computed(w), rname))
        j += 1
    specs += [(v, c, rname) for v, c in longs.get('refmap', ())]
    if gc.inv is not None:
        for key, val, first, last in git_prefix_configs(gc):
            # --config-env names a variable that holds the value: the text is not the value
            computed = _computed(last) or first.text.startswith('--config-env')
            m = REMOTE_FETCH_KEY_RE.match(key)
            if m:
                specs.append((val, computed, m.group(1)))
            elif PRUNE_TAGS_KEY_RE.match(key) and (computed or val.strip().lower() not in FALSE_WORDS):
                longs.setdefault('prune-tags', []).append((val, computed))
    if 'stdin' in longs:
        return [], 'it reads refspecs from its input'
    if unknown and (any(':' in w.text for w in pos) or any(':' in t for t in unknown)):
        return [], 'it gives an option the guard does not read (%s)' % unknown[0]
    instead = 'Fetch into a remote-tracking ref (refs/remotes/<remote>/<branch>) or your scratch space:'
    out = []
    for text, computed, rem in specs:
        spec = text.strip('"\'') if computed else text
        if computed and (':' in spec or spec.startswith('+')):
            return [], 'a refspec holds something only the shell knows (%s)' % text
        if computed:
            continue                            # a refspec computed whole: a known limit
        spec = spec[1:] if spec.startswith('+') else spec
        src, colon, dst = spec.partition(':')
        if spec.startswith('^') or not colon or not dst:
            continue
        ref = fetch_dst_ref(dst)
        if _tracking_ref(src, ref, rem):
            continue
        out.append((ref, False, None, (instead, 'git -C <at> fetch %s %s:refs/tmp/<name>' % (rem or '<remote>',
                                                                                             src or 'HEAD'))))
    forced = 'f' in shorts or 'force' in longs
    for name, computed in tagged if forced else ():
        # unforced, git refuses to move a tag that is there; forced, it moves it
        if computed:
            return [], 'a tag name holds something only the shell knows (%s)' % name
        out.append(('refs/tags/' + name, False, 'moves the tag %s to the remote\'s' % name,
                    ('Fetch it into your scratch space:', 'git -C <at> fetch %s %s' % (
                        rname or '<remote>', spelt('refs/tags/%s:refs/tmp/%s' % (name, name))))))
    tags = ('without --prune-tags or a forced --tags (a fetch never moves a tag you have):',
            'git -C <at> fetch %s' % (rname or '<remote>'))
    if 'P' in shorts or 'prune-tags' in longs:
        out.append(('refs/tags/*', True, 'deletes every tag the remote does not have', ('Fetch ' + tags[0], tags[1])))
    if ('t' in shorts or 'tags' in longs) and ('f' in shorts or 'force' in longs):
        out.append(('refs/tags/*', False, 'moves every tag to the remote\'s', ('Fetch ' + tags[0], tags[1])))
    return out, None


def symbolic_ref_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git symbolic-ref `gc`: none for a
    read (one name, no -d); with -d the name, deleted; with two names the name, written (HEAD
    itself: symbolic-ref does not follow it), and the ref it then points at, since later writes go
    through it. A computed word or an option the guard does not read is unread."""
    shorts, longs, pos, unknown = _form_words('symbolic-ref', gc.rest)
    delete = 'd' in shorts or 'delete' in longs
    if any(_computed(w) for w in gc.rest):
        return [], 'a word holds something only the shell knows (%s)' % next(w.text for w in gc.rest if _computed(w))
    if unknown:
        return [], 'it gives an option the guard does not read (%s)' % unknown[0]
    if not pos or (len(pos) < 2 and not delete):
        return [], None
    name = pos[0].text
    switch = ('To put your worktree on another branch:', 'git -C <at> switch <branch>') if name == 'HEAD' else None
    if delete:
        return [(name, True, None, switch)], None
    target = pos[1].text
    return [(name, False, None, switch), (target, False, 'points %s at %s, so later writes through it move it' % (
        name, target), switch)], None


def replace_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git replace `gc`: none when it only
    lists (no word, or REPLACE_LIST_OPTS and --format=<f> with a pattern only after -l); else the
    refs/replace/<object> ref it writes or deletes (-d)."""
    words = [w.text for w in gc.rest]
    if any(_computed(w) for w in gc.rest):
        return [], 'a word holds something only the shell knows (%s)' % next(w.text for w in gc.rest if _computed(w))
    opts = [t for t in words if t.startswith('-')]
    objs = [t for t in words if not t.startswith('-')]
    if all(t in REPLACE_LIST_OPTS or t.startswith('--format=') for t in opts) and \
            (not objs or any(t in REPLACE_LIST_OPTS for t in opts)):
        return [], None
    delete = '-d' in opts or any(len(t) > 2 and '--delete'.startswith(t) for t in opts)
    ref = 'refs/replace/%s' % (objs[0] if objs else '<object>')
    return [(ref, delete, None, ('To list the replace refs:', 'git -C <at> replace -l'))], None


def notes_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git notes `gc`: none for a verb in
    NOTES_READ_VERBS; any other writes the notes ref (--ref <ref> under refs/notes/, else
    refs/notes/commits). A computed verb, or an option before it other than --ref, is unread."""
    args, k, ref = gc.rest, 0, 'refs/notes/commits'
    while k < len(args) and args[k].text.startswith('-'):
        w = args[k]
        body, eq, v = w.text[2:].partition('=')
        if w.text == '--' or not w.text.startswith('--') or not body or not 'ref'.startswith(body):
            return [], 'it gives an option the guard does not read (%s)' % w.text
        if not eq:
            k += 1
            v = args[k].text if k < len(args) else ''
        ref = v if v.startswith('refs/notes/') else 'refs/' + v if v.startswith('notes/') else 'refs/notes/' + v
        k += 1
    verb = args[k] if k < len(args) else None
    if verb is not None and _computed(verb):
        return [], 'its verb holds something only the shell knows (%s)' % verb.text
    if (verb.text if verb is not None else None) in NOTES_READ_VERBS:
        return [], None
    return [(ref, verb.text == 'remove', None, ('To show the notes:', 'git -C <at> notes list'))], None


def _first_computed(words):
    """'a word holds something only the shell knows (<text>)' for the first computed Word of
    `words`, or None."""
    for w in words:
        if _computed(w):
            return 'a word holds something only the shell knows (%s)' % w.text
    return None


def _last_of(seq, names):
    """The last of `names` in `seq` (_form_scan's option names in order), or None."""
    for n in reversed(seq):
        if n in names:
            return n
    return None


def branch_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git branch `gc`: a move (-m, -M,
    --move) deletes the old name (with one name, the branch HEAD names where git runs: none when it
    is detached, unread when the guard cannot read it) and writes the new; a forced copy (-C, or
    -c with -f) writes the new name; -f with a name writes it. A
    plain create, an unforced copy, -d and -D (git_branch_delete judges -D), a listing and the
    other modes write no ref here. A computed word, or an option the guard does not read, in a
    form that writes is unread."""
    shorts, longs, pos, unknown = _form_words('branch', gc.rest)
    move = bool(shorts & set('mM')) or 'move' in longs
    copy = bool(shorts & set('cC')) or 'copy' in longs
    force = bool(shorts & set('fMC')) or 'force' in longs
    if not pos or not (move or force):
        return [], None
    others = {'delete', 'list', 'edit-description', 'show-current', 'set-upstream-to', 'unset-upstream',
              'contains', 'no-contains', 'merged', 'no-merged', 'points-at'}
    if shorts & set('dDlu') or others & set(longs) or '-u' in longs:
        return [], None
    why = _first_computed(gc.rest)
    if why or unknown:
        return [], why or 'it gives an option the guard does not read (%s)' % unknown[0]
    names = [w.text for w in pos]
    if move or copy:
        old, new = (names[0], names[1]) if len(names) > 1 else (None, names[0])
        new_ref = 'refs/heads/' + new
        if move:
            if old is None:
                # one name: git renames the branch checked out where it runs (HEAD's), and a
                # detached HEAD has none to rename
                cur = _head_ref(place) if place is not None else None
                if cur == '':
                    return [], None
                if not cur or not cur.startswith('refs/heads/') or not ref_name_ok(cur):
                    return [], 'it renames the branch checked out where git runs, and the guard cannot read its HEAD'
                old = cur[len('refs/heads/'):]
            make = ('To make a branch of the new name and leave %s as it is:' % old,
                    'git -C <at> branch %s %s' % (spelt(new), spelt(old)))
            return [('refs/heads/' + old, True, 'renames refs/heads/%s to %s, deleting the old name' % (old, new_ref),
                     make),
                    (new_ref, False, None, make)], None
        copy_to = ('To copy it to a new name:', 'git -C <at> branch -c %s<new-name>' % (spelt(old) + ' ' if old else ''))
        return [(new_ref, False, 'writes over %s with a copy' % new_ref, copy_to)], None
    start = ' ' + spelt(names[1]) if len(names) > 1 else ''
    return [('refs/heads/' + names[0], False, 'moves refs/heads/%s' % names[0],
             ('To make a new branch there:', 'git -C <at> branch <new-name>%s' % start))], None


def tag_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git tag `gc`: -d deletes each tag
    named; -f with a name writes it. A plain create, a listing and -v write no ref here."""
    shorts, longs, pos, unknown = _form_words('tag', gc.rest)
    delete = 'd' in shorts or 'delete' in longs
    force = 'f' in shorts or 'force' in longs
    if not pos or not (delete or force) or 'l' in shorts or 'list' in longs or 'v' in shorts or 'verify' in longs:
        return [], None
    why = _first_computed(gc.rest)
    if why or unknown:
        return [], why or 'it gives an option the guard does not read (%s)' % unknown[0]
    if delete:
        return [('refs/tags/' + w.text, True, None, None) for w in pos], None
    start = ' ' + spelt(pos[1].text) if len(pos) > 1 else ''
    return [('refs/tags/' + pos[0].text, False, 'moves refs/tags/%s' % pos[0].text,
             ('To make a new tag there:', 'git -C <at> tag <new-name>%s' % start))], None


def _branch_seen(place, name):
    """True when the repository of `place` (the Place git runs in) has branch `name`: a loose
    refs/heads/<name>, a packed-refs line, or a worktree's HEAD naming it; False when it has none
    of them; None when the guard cannot tell (no place or common dir, or a reftable store)."""
    common = place.common if place is not None else None
    if not common or os.path.isdir(os.path.join(common, 'reftable')):
        return None
    ref = 'refs/heads/' + name
    if os.path.isfile(os.path.join(common, *ref.split('/'))) or ref in (gitdefaults._packed(common) or ()):
        return True
    heads = [os.path.join(common, 'HEAD')]
    try:
        heads += [os.path.join(common, 'worktrees', n, 'HEAD') for n in os.listdir(os.path.join(common, 'worktrees'))]
    except OSError:
        pass
    return any((gitdefaults._read(h, 4096) or '').strip() == 'ref: ' + ref for h in heads)


def _checked_out(word, place):
    """([(ref, ...)], why) for the branch `word` that --ignore-other-worktrees (or worktree add
    -f) checks out though another worktree has it, git running in `place`: refs/heads/<name> when the
    repository has that branch (_branch_seen; one the guard cannot tell counts); none for a word
    that names no branch there (a tag, a commit, a new branch git makes from a remote's), HEAD or a
    commit expression; `-` and @{...} name a branch the text does not show."""
    t = word.text
    if _computed(word):
        return None, 'a word holds something only the shell knows (%s)' % t
    detach = ('To check it out detached, so no branch moves with your commits:', 'git -C <at> switch --detach %s'
              % spelt(t))
    if t == '-' or t.startswith('@{'):
        return ('the branch %s names' % t, detach), None
    if t == 'HEAD' or not ref_name_ok(t) or _branch_seen(place, t) is False:
        return None, None
    return ('refs/heads/' + t, detach), None


def checkout_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git checkout or switch `gc`: -B
    (checkout) or -C/--force-create (switch) writes the branch it names; the last of
    --ignore-other-worktrees and its no- form, with no branch option or --detach, checks out the
    branch named (_checked_out) though another worktree has it, so commits here move it."""
    shorts, longs, pos, unknown, seq, npre = _form_scan(gc.sub, gc.rest)
    if gc.sub == 'checkout':
        forced = longs.get('-B') or []
    else:
        forced = (longs.get('-C') or []) + (longs.get('force-create') or [])
    start = ' ' + spelt(pos[0].text) if pos and not _computed(pos[0]) else ''
    if forced:
        v, computed = forced[-1]
        if computed:
            return [], 'a branch name holds something only the shell knows (%s)' % v
        return [('refs/heads/' + v, False, 'resets refs/heads/%s' % v,
                 ('To make a new branch and switch to it:', 'git -C <at> switch -c <new-name>%s' % start))], None
    if _last_of(seq, ('ignore-other-worktrees', 'no-ignore-other-worktrees')) != 'ignore-other-worktrees':
        return [], None
    if set(longs) & {'-b', '-c', 'create', 'orphan', 'detach'} or 'd' in shorts or not npre:
        return [], None
    hit, why = _checked_out(pos[0], place)
    if why or not hit:
        return [], why
    return [(hit[0], False, 'checks out %s though another worktree may have it, so commits here move it' % hit[0],
             hit[1])], None


def worktree_add_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git worktree add `gc`: -B writes
    the branch it names; -f with no -b, -B, --detach or --orphan checks out the branch named (the
    commit-ish, else the path's last part) though another worktree may have it. Any other worktree
    verb, and a plain add (-b <new>, or an existing branch git refuses when it is out elsewhere),
    write no ref here."""
    args = gc.rest
    if not args or args[0].text != 'add' or _computed(args[0]):
        return [], None
    shorts, longs, pos, unknown, seq, npre = _form_scan('worktree add', args[1:])
    force = 'f' in shorts or 'force' in longs
    path = spelt(pos[0].text) if pos and not _computed(pos[0]) else '<path>'
    if '-B' in longs:
        v, computed = longs['-B'][-1]
        if computed:
            return [], 'a branch name holds something only the shell knows (%s)' % v
        start = ' ' + spelt(pos[1].text) if len(pos) > 1 and not _computed(pos[1]) else ''
        return [('refs/heads/' + v, False, 'resets refs/heads/%s' % v,
                 ('To make a new branch for it:', 'git -C <at> worktree add -b <new-name> %s%s' % (path, start)))], None
    if not force or not pos or '-b' in longs or 'd' in shorts or set(longs) & {'detach', 'orphan'}:
        return [], None
    if unknown:
        return [], 'it gives an option the guard does not read (%s)' % unknown[0]
    if len(pos) > 1:
        hit, why = _checked_out(pos[1], place)
    elif _computed(pos[0]):
        hit, why = None, 'a word holds something only the shell knows (%s)' % pos[0].text
    else:
        base = re.split(r'[\\/]', pos[0].text.rstrip('\\/'))[-1]
        named = ref_name_ok(base) and base != 'HEAD' and _branch_seen(place, base) is not False
        hit, why = (('refs/heads/' + base, None), None) if named else (None, None)
    if why or not hit:
        return [], why
    commit = spelt(pos[1].text) if len(pos) > 1 else '<commit-ish>'
    return [(hit[0], False, 'checks out %s though another worktree may have it, so commits there move it' % hit[0],
             ('To check the commit out detached in the new worktree:',
              'git -C <at> worktree add --detach %s %s' % (path, commit)))], None


def _inline_on(gc, name):
    """(on, why unread or None) of the last inline -c `name` (a boolean setting) before git's
    subcommand: on unless its value is false (a bare key is true); None when the line sets none.
    A value the shell computes, or one --config-env names, is unread."""
    on = None
    if gc.inv is not None:
        for key, val, first, final in git_prefix_configs(gc):
            if key == name.lower():
                if _computed(final) or first.text.startswith('--config-env'):
                    return None, 'its -c %s holds something only the shell knows' % name
                on = val.strip().lower() not in FALSE_WORDS
    return on, None


def rebase_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git rebase `gc`: with
    --update-refs (the last of it and its no- form, else an inline -c rebase.updateRefs not
    false) it moves every branch that points into the commits it rebases."""
    shorts, longs, pos, unknown, seq, npre = _form_scan('rebase', gc.rest)
    last = _last_of(seq, ('update-refs', 'no-update-refs'))
    on = last == 'update-refs'
    if last is None:
        on, why = _inline_on(gc, 'rebase.updateRefs')
        if why:
            return [], why
    if not on:
        return [], None
    keep = []
    for w in gc.rest:
        body = w.text[2:].partition('=')[0] if w.text.startswith('--') else None
        hit = _long_hit(set(REBASE_FLAGS) | set(REBASE_VALUES), body) if body else None
        if not (hit and hit[0] == 'update-refs'):
            keep.append(spelt(w.text))
    return [('refs/heads/*', False, 'moves every branch that points into the commits it rebases',
             ('Rebase without moving other branches:', ('git -C <at> rebase --no-update-refs %s' % ' '.join(keep)).rstrip()))], None


def pull_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git pull `gc`: what its fetch
    writes (fetch_refs), and, when an inline -c rebase.updateRefs is on and the pull may rebase,
    every branch that points into the commits it rebases. It may rebase unless the line says it
    merges: the last of --rebase[=<v>], -r and --no-rebase, else an inline -c pull.rebase (the
    guard does not read the repository's config). git pull takes no --update-refs of its own."""
    refs, why = fetch_refs(gc, place)
    on, cwhy = _inline_on(gc, 'rebase.updateRefs')
    if not on or why:
        return refs, why or cwhy
    shorts, longs, pos, unknown, seq, npre = _form_scan('pull', gc.rest)
    last = _last_of(seq, ('rebase', 'no-rebase', '-r'))
    if last == 'no-rebase':
        return refs, None
    if last == 'rebase':
        v, computed = longs['rebase'][-1]
        if computed:
            return refs, 'its --rebase holds something only the shell knows'
        if v.strip().lower() in FALSE_WORDS:
            return refs, None
    elif last is None:
        rebases, cwhy = _inline_on(gc, 'pull.rebase')
        if cwhy:
            return refs, cwhy
        if rebases is False:
            return refs, None
    rest = ' '.join(spelt(w.text) for w in gc.rest)
    return refs + [('refs/heads/*', False, 'may rebase with rebase.updateRefs on, which moves every branch that '
                    'points into the commits it rebases',
                    ('Pull without moving other branches:',
                     ('git -C <at> -c rebase.updateRefs=false pull %s' % rest).rstrip()))], None


def remote_refs(gc, place=None):
    """([(ref, delete, what, instead)], why unread or None) of git remote `gc`: remove (rm) and
    rename delete the remote's remote-tracking refs, set-head writes or deletes <remote>/HEAD, and
    add --mirror (=fetch) makes later fetches write every ref. Its other verbs (prune and update
    as fetch --prune, show, get-url and the like) write no ref here."""
    args, k = gc.rest, 0
    while k < len(args) and args[k].text.startswith('-'):
        t = args[k].text
        if t == '-h':
            return [], None
        if _computed(args[k]) or not (t == '-v' or (len(t) > 2 and '--verbose'.startswith(t))):
            return [], 'it gives an option the guard does not read (%s)' % t
        k += 1
    if k >= len(args):
        return [], None
    verb, rest = args[k], args[k + 1:]
    if _computed(verb):
        return [], 'its verb holds something only the shell knows (%s)' % verb.text
    names = [w for w in rest if not w.text.startswith('-')]
    listing = ('To list the remotes:', 'git -C <at> remote -v')
    if verb.text in ('remove', 'rm', 'rename', 'set-head'):
        why = _first_computed(rest)
        if why:
            return [], why
        if not names:
            return [], None
        n = names[0].text
        if verb.text == 'set-head':
            delete = any(w.text in ('-d', '--delete') for w in rest)
            return [('refs/remotes/%s/HEAD' % n, delete, None,
                     ('To see which branch the remote\'s HEAD names:', 'git -C <at> remote show %s' % spelt(n)))], None
        if verb.text == 'rename':
            new = names[1].text if len(names) > 1 else '<new-name>'
            return [('refs/remotes/%s/*' % n, True, 'renames every remote-tracking ref of %s to %s' % (n, new),
                     ('To add the same URL under the new name and leave %s as it is:' % n,
                      'git -C <at> remote add %s <url>' % spelt(new)))], None
        return [('refs/remotes/%s/*' % n, True, 'deletes every remote-tracking ref of %s' % n, listing)], None
    if verb.text != 'add':
        return [], None
    shorts, longs, pos, unknown, seq, npre = _form_scan('remote add', rest)
    if _last_of(seq, ('mirror', 'no-mirror')) != 'mirror':
        return [], None
    v, computed = longs['mirror'][-1]
    if computed:
        return [], 'its --mirror holds something only the shell knows'
    if v not in ('', 'fetch'):
        return [], None
    plain = ' '.join(spelt(w.text) for w in pos[:2]) or '<name> <url>'
    return [('refs/*', False, 'sets the remote up as a mirror, so a fetch from it writes over every ref',
             ('To add it with its own remote-tracking refs (refs/remotes/<name>/):', 'git -C <at> remote add %s' % plain))], None


HISTORY_TOOLS = {'fast-import': 'writes the refs its input names',
                 'filter-branch': 'rewrites the branches it names and keeps their old tips under refs/original/'}


def history_tool_refs(gc, place=None):
    """([(ref, delete, what, instead)], None) of git fast-import or filter-branch `gc`: every ref
    (refs/*), unless it only asks for help (-h)."""
    if [w.text for w in gc.rest] in (['-h'], ['--help']):
        return [], None
    return [('refs/*', False, HISTORY_TOOLS[gc.sub],
             ('Run it in a clone under the temp folder, where the guard does not judge refs:',
              'git clone <at> "<temp>/<name>"'))], None


REF_FORM_READERS = {'fetch': fetch_refs, 'pull': pull_refs, 'symbolic-ref': symbolic_ref_refs,
                    'replace': replace_refs, 'notes': notes_refs, 'branch': branch_refs, 'tag': tag_refs,
                    'checkout': checkout_refs, 'switch': checkout_refs, 'rebase': rebase_refs,
                    'remote': remote_refs, 'worktree': worktree_add_refs, 'fast-import': history_tool_refs,
                    'filter-branch': history_tool_refs}


def _judge_refs(event, ctx, cmd, gc, cwd, role, refs, unread):
    """git-shared-ref for a git form other than update-ref: `refs` [(ref, delete, what, instead)]
    the refs it writes or deletes (`what` a phrase for the refusal, None for "writes/deletes
    <ref>"; `instead` (sentence, command with <at> and maybe <temp>, the temp folder), None for show-ref), `unread` why the guard
    cannot tell them (None when it can). As git_shared_ref: only a held session
    (shared_ref_held), not in a scratch repository (_scratch_repo), each ref by _ref_ok; no form
    here writes through HEAD."""
    if (not refs and not unread) or not shared_ref_held(event, ctx):
        return None
    place, _top, at, where = _shared_scene(ctx, gc, cwd)
    if _scratch_repo(ctx, place):
        return None
    text = bare(cmd)
    unread = unread or where
    if unread:
        return refuse('git-shared-ref', role, '`%s`: the guard cannot tell which refs it writes: %s; %s. Name each '
                      'ref literally. %s To keep a commit in your scratch space:' % (
                          text, unread, SHARED_REFS, SHARED_REF_ALLOWED),
                      'git -C %s update-ref refs/tmp/<name> <new-oid>' % quote(at))
    linked = place.kind == repo.LINKED and not ctx.pm_data_error(gc.dir)
    own = _own_branch(ctx, place, gc.dir)
    for name, delete, what, instead in refs:
        if _ref_ok(name, delete, linked, own):
            continue
        what = what or '%s %s' % ('deletes' if delete else 'writes', name)
        how, command = instead or ('To show it:', 'git -C <at> show-ref %s' % name)
        temp = jail.temp_roots(ctx)
        return refuse('git-shared-ref', role, '`%s` %s: %s is not one it may %s; %s. %s %s' % (
            text, what, name, 'delete' if delete else 'write', SHARED_REFS, SHARED_REF_ALLOWED, how),
            command.replace('<at>', quote(at)).replace('<temp>', fwd(temp[0]) if temp else '<temp folder>'))
    return None


def git_shared_ref_forms(event, ctx, cmd, gc, cwd, role):
    """git-shared-ref for the forms REF_FORM_READERS reads (SHARED_REF_FORMS, branch and worktree),
    each given the Place git runs in (None when the text does not name it)."""
    if not shared_ref_held(event, ctx):
        return None
    place = ctx.place(gc.dir) if gc.dir and not gc.unknown and not gc.blind else None
    refs, unread = REF_FORM_READERS[gc.sub](gc, place)
    return _judge_refs(event, ctx, cmd, gc, cwd, role, refs, unread)


def _repo_common(path):
    """The git common dir of the repository git opens at `path` (a work tree, its .git, a git dir,
    or <path>.git, as git's enter_repo tries them), or None."""
    for p in (path, path + '.git'):
        common = gitdefaults.common_dir(p)
        if common:
            return common
        if os.path.isfile(os.path.join(p, 'HEAD')):
            named = gitdefaults._read(os.path.join(p, 'commondir'), 4096)
            return paths.norm(os.path.join(p, named.strip()), fold=False) if named and named.strip() else p
    return None


def _push_self(gc, place, t, computed, home=None):
    """True when git push's repository `word` is the repository git runs in: `.`, or a local path
    or file:// URL (resolved against where git runs and its work tree's top) that opens this
    repository's common dir; False for a URL, scp-like host:path, or a name that opens no such
    repository; None when the guard cannot tell (a computed word, or a path with no place to
    compare). A leading ~ is the home folder `home`, as git reads it. A named remote is taken as
    a remote, whatever its URL (a known limit)."""
    if computed:
        return None
    if t == '.':
        return True
    if t == '~' or t.startswith(('~/', '~\\')):
        if not home:
            return None
        t = os.path.join(home, t[2:]) if len(t) > 2 else home
    elif t.startswith('~'):
        return None                             # ~user: another user's home
    if t.lower().startswith('file://'):
        t = t[len('file://'):]
        t = t[1:] if re.match(r'^/[A-Za-z]:', t) else t
    elif '://' in t or (not re.match(r'^[A-Za-z]:', t) and re.match(r'^[^/\\]*:', t)):
        return False
    absolute = os.path.isabs(t) or bool(re.match(r'^[A-Za-z]:[\\/]', t))
    if place is None or not place.common:
        return None if absolute or '/' in t or '\\' in t or t.startswith('.') else False
    bases = [t] if absolute else [os.path.join(b, t) for b in (gc.dir, place.top) if b]
    mine = paths.norm(jail.real(place.common))
    for p in bases:
        common = _repo_common(paths.norm(p, fold=False))
        if common and paths.norm(jail.real(common)) == mine:
            return True
    return False


def _long_value(sub, args, name):
    """(value text, computed) of git `sub`'s long option `name` (or an abbreviation of it), the
    last one given, or None."""
    out, k = None, 0
    while k < len(args):
        t = args[k].text
        if t == '--':
            break
        if t.startswith('--'):
            body, eq, v = t[2:].partition('=')
            if _canon_long(sub, body) == [name]:
                if eq:
                    out = (v, _computed(args[k]))
                elif k + 1 < len(args):
                    out = (args[k + 1].text, _computed(args[k + 1]))
                    k += 1
        k += 1
    return out


def git_shared_push(event, ctx, cmd, gc, cwd, role):
    """git-shared-ref for a push into the repository itself (_push_self): it writes this
    repository's refs, so each refspec destination is judged as update-ref's ref. A destination
    must be a full refs/ name (push matches a short one against the refs there); `:dst` and -d
    delete; a refspec with no `:` is a no-op. --all, --mirror, --branches, --tags, --prune, no
    refspec, a computed refspec, or a repository the guard cannot tell with a refspec that writes,
    are unread."""
    if not shared_ref_held(event, ctx):
        return None
    short, longs, pos = _opts('push', gc.rest)
    remote = (pos[0].text, _computed(pos[0])) if pos else _long_value('push', gc.rest, 'repo')
    if remote is None:
        return None
    specs = pos[1:]
    delete = 'd' in short or 'delete' in longs
    every = sorted({'all', 'mirror', 'branches', 'tags', 'prune'} & longs)
    place = ctx.place(gc.dir) if gc.dir and not gc.unknown and not gc.blind else None
    self_ = _push_self(gc, place, *remote, home=getattr(ctx, 'home', None))
    if self_ is False:
        return None
    writes = delete or every or any(':' in w.text or _computed(w) for w in specs)
    if self_ is None:
        why = 'the guard cannot tell whether %s is this repository' % remote[0]
        return _judge_refs(event, ctx, cmd, gc, cwd, role, [], why) if writes else None
    refs, unread = [], None
    if every:
        unread = 'with --%s it pushes refs it does not name into this repository' % every[0]
    elif not specs:
        unread = 'it names no refspec, so git takes them from its config'
    for w in specs:
        if unread:
            break
        if _computed(w):
            unread = 'a refspec holds something only the shell knows (%s)' % w.text
            break
        spec = w.text[1:] if w.text.startswith('+') else w.text
        src, colon, dst = spec.partition(':')
        if spec.startswith('^') or (not colon and not delete):
            continue                            # an exclusion, or a ref pushed onto itself
        dst = dst if colon else src
        if not dst or not dst.startswith('refs/'):
            unread = '%r names no full ref (refs/...); push into this repository names it in full' % spec
            break
        refs.append((dst, delete or not src, None, None))
    return _judge_refs(event, ctx, cmd, gc, cwd, role, refs, unread)


SAFE_WORD_RE = re.compile(r'^[\w@%+=:,./-]+$')


def spelt(text):
    """`text` as one shell word: bare when it needs no quoting, else single-quoted (double-quoted,
    with bash's escapes, when it holds a single quote)."""
    if SAFE_WORD_RE.match(text):
        return text
    if "'" not in text:
        return "'%s'" % text
    return '"%s"' % re.sub(r'(["\\$`])', r'\\\1', text)


def git_rest_text(cmd, gc):
    """The text of `gc.rest` for a hint: the words an alias the line defines gave (gc.synth, no
    span in the line) spelt as git split them, then the line's own words as written."""
    words = [spelt(w.text) for w in gc.rest[:gc.synth]]
    own = gc.rest[gc.synth:]
    if own and own[0].start is not None and own[-1].end is not None:
        words.append(cmd.source[own[0].start:own[-1].end])
    elif own:
        words.extend(spelt(w.text) for w in own)
    return ' '.join(words)


def git_unplaced(ctx, cmd, gc, cwd, agent=False):
    """(sentence, command) for git whose place `gc.blind` hides (GIT_DIR and the like, --git-dir,
    --work-tree, core.worktree, --config-env, a computed -c): drop it and run the same git
    command in the session's own worktree (git_elsewhere), or where it runs when it is in none."""
    mine = own_worktree(ctx, cwd)
    if mine:
        note, instead = git_elsewhere(ctx, cmd, gc, mine, agent)
        return 'Drop %s and run it in your worktree:%s' % (gc.blind, ' ' + note if note else ''), instead
    rest = git_rest_text(cmd, gc)
    return 'Drop %s and run it in your worktree:' % gc.blind, ('git %s %s' % (gc.sub, rest)).rstrip()


def git_branch_delete(ctx, gc, role):
    short, longs, pos = _opts('branch', gc.rest, wide=True)
    force = 'D' in short or (('delete' in longs or 'd' in short) and ('force' in longs or 'f' in short))
    if not force:
        return None
    names = ' '.join(w.text for w in pos) or '<branch>'
    from . import backlogrole
    store = backlogrole.store_kind(ctx, gc.dir) if gc.dir and not gc.unknown else None
    held = [w.text for w in pos if not w.dyn and '*' not in w.text and
            backlogrole.is_store_ref(w.text, store, short=True)]
    if held:
        # -d of it is a raw write of the store's ref (backlog-raw-write): no delete form passes
        return refuse('git-branch-force-delete', role, '`git branch %s` deletes a branch whether or not it is '
                      'merged, and %s is the backlog store\'s branch, which only tools/backlog.py writes; no '
                      'session deletes it. To see it:' % (' '.join(w.text for w in gc.rest), held[0]),
                      'git log --oneline -1 %s' % held[0])
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
        # a worktree the guard cannot place (warn by default); git still refuses a dirty one without --force
        return refuse(UNPLACED, role, '`%s`: the guard cannot tell which worktree it removes. Name it '
                      'by a literal path:' % text,
                      'git -C %s worktree remove "<worktree path>"' % (quote(main_top) if main_top else '"<main checkout>"'))
    if _in_temp(ctx, target):
        return None                             # a scratch worktree under a temp root
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
        word, mark_why = stale_mark_word(ctx, target, head) if merged is False and head else (False, None)
        if word:
            return None                         # a stale mark that holds: the PM's word
        how = ('Merge it first; unmerged work goes to the PM, who decides (python %s -C %s %s --abandon "<why>" '
               'only on the PM\'s word).' % (kit_tool(ctx, 'worktree_remove'),
                                             quote(main_top) if main_top else '"<main checkout>"',
                                             os.path.basename(target)))
        if mark_why:
            how += ' A stale mark does not count: %s.' % mark_why
        return refuse('git-worktree-remove', role, '`%s` removes a worktree whose commit is not merged into %s (%s). '
                      '%s Its unmerged commits:' % (text, main, why2 or 'not an ancestor', how),
                      'git -C %s log --oneline %s..HEAD' % (quote(target), main))
    return None


def stale_mark_word(ctx, target, head):
    """`(True, None)` when the clean worktree at `target`, HEAD `head`, carries a stale mark that
    holds (kitlib.worktree.marks: a mark recorded by `worktree_remove.py --mark-stale`, read from
    `<git common dir>/kit-stale-marks.json`): the PM's word to remove it unmerged. It holds on the
    nightly sweep's void checks (sweep.sweep_skip): the mark names this path and this tip, and
    nothing in the tree, its git dir (but index, index.lock and the folder's own time), its
    branch's reflog or the tip's commit date is newer than the mark. The sweep's other checks are
    not made here: the idle bar, `git worktree lock`, exemption, worktree-folder placement, a branch
    checked out elsewhere, holders, open claims, machine lock and landing hold. marks.mark makes
    them all when it marks (with classify.live_reason) but the DELETE-access holder probe
    (holders.blockers): of holders it checks only processes' current folders, through the L
    rule. So what is open is that probe and a change after the mark: the mark is the decision that
    the tree is idle and later activity voids it; a later `git worktree lock` writes `locked` into
    the walked git dir and voids it while it is held (no `--force` is read); a mark naming another
    path is void. Known limits: a file held open in the
    tree other than as a current folder (neither the mark nor this runs holders.blockers), and a
    later exemption, second checkout, holder (no process probe runs here), claim, machine-lock
    run or landing that writes nothing there, are not seen (docs/guard.md, the
    git-worktree-remove row). Else `(False, why)`: why is None when the tree's branch has no
    mark (detached included), else why no mark counts. Fails closed: a marks file, branch, git
    common dir or last activity that cannot be read is no word.
    The tree walk (sweep.last_activity) runs only here, on an unmerged remove that found a mark."""
    from kitlib.worktree import marks, sweep  # the walk and its git reads: only on this path
    place = ctx.place(target)
    if not place.common:
        return False, 'git cannot place its repository (%s)' % (place.detail or 'no git common dir')
    branch, why = ctx.git.current_branch(target)
    if branch is None:
        return False, 'its branch cannot be read (%s)' % why
    if not branch:
        return False, None                    # a detached HEAD: a mark names a branch
    got, why = marks.load(os.path.join(place.common, marks.FILE_NAME))
    if got is None:
        return False, 'the stale marks cannot be read (%s)' % why
    mark = marks.find(got, branch)
    if mark is None:
        return False, None
    void = marks.void_reason(mark, target, head)
    if void is None:
        seen, why = sweep.last_activity(ctx.main_top(target) or target, target, branch)
        if seen is None:
            return False, 'its stale mark cannot be checked: its last activity cannot be read (%s)' % why
        void = marks.void_reason(mark, target, head, seen)
    if void:
        return False, 'its stale mark is void: %s' % void
    return True, None


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
    be git push --no-verify); elsewhere an agent session (agent_session) is refused as unplaced
    (it may be git writing a checkout the jail cannot place; warn by default). Only the owner and
    a session the jail does not hold (`plain`) read the line as any other."""
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
    return refuse(UNPLACED, role, '`& %s` runs the program a PowerShell subexpression computes, which the guard cannot '
                  'name: it may be git writing a checkout the write jail cannot place, or a heavy program run '
                  'without the machine lock. Name the program literally:' % inv.path, instead)


def _no_verify_word(word):
    t = word.text
    return t.startswith('--') and 'no-verify' in _canon_long('push', t[2:].partition('=')[0])


def _push_names_branches(pos):
    """True when a `git push`'s positional words (remote, then refspecs) name at least one refspec
    and none takes its source from HEAD or @: a push the same from any checkout of the repository."""
    specs = [w.text for w in pos[1:]]
    if not specs:
        return False
    for t in specs:
        src = t.lstrip('+').partition(':')[0]
        if src.startswith('@') or src.startswith('HEAD'):
            return False
    return True


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
    # an agent session's push from the main checkout it may not write: the same push from its own
    # worktree of that checkout, which shares its branches and remotes (the write jail lets that one
    # through), only when it names each branch: with none, or HEAD (@), it pushes the branch checked
    # out where it runs, the worktree's there, so it stays in the main checkout
    run_in = gc.dir
    if gc.dir and agent_session(event, ctx) and gc.inv is not None and ctx.place(gc.dir).kind == repo.MAIN \
            and _push_names_branches(_pos):
        run_in = own_worktree(ctx, gc.inv.cmd.cwd, ctx.main_top(gc.dir)) or gc.dir
    return refuse('git-push-pmdata', role, '`git push %s` skips the pre-push hook, which keeps PM data off public '
                  'remotes in this repository (it has %s). Push with the hook:'
                  % (' '.join(w.text for w in gc.rest), pmdata.FILE_NAME),
                  ('git -C %s push %s' % (quote(run_in), ' '.join(words))).rstrip())


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
        sub, where, args = inv.name[len('git-'):], cm.run_dir(inv), [w.text for w in inv.args]
    else:
        gc = git_call_judged(inv)
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
    """A push or send-pack (`what`) from a repository without a PM data record of its own, when
    the machine config names pm_data but the guard cannot read the machine's record, in every
    session but the owner's own: it fails closed, so while the data worktree folder is deleted
    every such push is denied until the owner re-adds it with git (docs/pm-data-branch.md
    "Failure modes"; init refuses while the PM-repo pointer is set). Whether the repository holds a copy of the data (its root commit, a shallow
    clone) is not read: the guard catches honest mistakes, not evasion."""
    if is_owner_session(event, ctx) or not where:
        return None
    if ctx.place(where).kind not in (repo.MAIN, repo.LINKED):
        return None
    root, why = ctx.pm_root()
    if root is None and why is None:
        return None                             # the machine config names no pm_data
    if root is None:
        path = ctx.pm_data_path() or ctx.pm_repo_pointer()
        path = quote(path) if path else '"<pm_data.path>"'
        return refuse('git-push-pmdata', role, '%s: the repository %s has no %s of its own, and the guard cannot read '
                      'the machine\'s PM data record to tell whether it holds PM data (%s), so it fails closed. When '
                      'the data worktree folder was deleted (docs/pm-data-branch.md "Failure modes"), the owner '
                      're-adds it from the PM repository\'s main checkout, one git command at a time (pm_data.py init '
                      'refuses while the PM-repo pointer is set):' % (what, fwd(where), pmdata.FILE_NAME, why),
                      'git -C "<PM repo main checkout>" worktree unlock %s, then worktree remove %s, worktree add '
                      '%s <data branch>, worktree lock --reason "PM data" %s' % (path, path, path, path))
    return None


# ---------------------------------------------------------------- git aliases

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


def _unknown_dir_counts(ctx, gc):
    """For a git call whose directory the text cannot tell: True when the machine config names
    pm_data (or cannot be read), or the directory the command itself runs in has a PM data record
    (pmdata_recorded), or the folder a Start-Process -WorkingDirectory starts it in
    (commands.run_dir) has one. Otherwise an unknown directory is no ground to deny."""
    if gc.inv is None:
        return bool(ctx.pm_data_named())
    heres = [d for d in (gc.inv.cmd.cwd, cm.run_dir(gc.inv)) if d]
    return bool(ctx.pm_data_named() or any(pmdata_recorded(ctx, d) for d in heres))


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
    when _unknown_dir_counts says so (the machine config names pm_data, or the command's own
    directory has a record). A --global or --system write, or one to
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
    # A computed remote or refspec with a flag or a literal `+` or `:` that forces, deletes or
    # skips the hook is refused. Without one it is a push the guard cannot place: a warning by
    # default (a computed source or destination is left to the pre-push
    # hook), and the literal refspecs are judged below.
    marked = any(w.text.lstrip('"\'').startswith(('+', ':')) for w in dyn)
    if dyn and (gated or delete or force or no_verify or any(w.dyn for w in pos[1:])):
        remote = pos[0].text if pos and not pos[0].dyn else cfg.remote
        d = refuse('git-push-main' if delete or force or no_verify or marked else UNPLACED, role,
                   '`git push %s` has a refspec that is not literal, so the guard cannot '
                   'tell whether it %s %s. Name the refs literally:'
                   % (rest, 'pushes' if gated else 'rewrites or deletes', main),
                   'git -C %s push %s <branch>' % (quote(gc.dir), remote))
        if d:
            return d
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
    a = gc.before()
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
    """True when the folder `d` is the temp folder or below it. It lifts the core.hooksPath checks,
    so on Linux it compares with the strict fold (a mount in doubt does not fold) and a `\\` is a
    name's character."""
    if not d:
        return False
    strict = paths.posix()
    top = paths.norm(ctx.tempdir, absolute=True, strict=strict).rstrip('/' if strict else '\\/')
    here_ = paths.norm(d, absolute=True, strict=strict)
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
    --system), are left alone. A git alias the line defines that runs `config` is judged the same
    way (inline_alias)."""
    why = HOOKS_WHY
    for name, val in env_assignments(cmd, HOOKS_ENV):
        if HOOKS_KEY in val.lower():
            where = quote(cmd.cwd or cwd) if (cmd.cwd or cwd) else '"<checkout>"'
            return refuse('git-hooks-path', role, '`%s` sets %s to change core.hooksPath for the git commands that '
                          'read it. %s Ask the owner (through the PM) when it needs to change. To see it:' %
                          (bare(cmd), name, why), 'git -C %s config --get core.hooksPath' % where)
    gc = git_call_judged(inv)
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
    gc = git_call_judged(inv)
    found = _owner_identity_use(cmd, gc, email)
    if found is None:
        return None
    d = (gc.dir if gc is not None else None) or where
    return refuse('owner-identity', role, '`%s` %s. Owner-only paths are judged by the author email, so that '
                  'passes an agent\'s change as the owner\'s. Commit as yourself, with the identity git config '
                  'holds; to see it:' % (bare(cmd), found), 'git -C %s config --get user.email' % quote(d))
