"""The backlog roles in the guard (docs/guard.md "Backlog roles"; docs/backlog.md "Roles").

  backlog-role       a subagent's run of tools/backlog.py, judged by kitlib.backlog.roles for the
                     role its hook payload names (Action.agent_role), from the command's words
                     (kitlib.backlog.policy.read_argv). A PM_KIT_ROLE the line sets changes
                     nothing. A builder's own pull (roles.OWN_PULL) is read from the store the
                     run's folder opens, as it is locally, only when it alone would decide; an
                     --as branch is its own from that branch's linked worktree (own_pull_refusal).
  backlog-raw-write  a write of the backlog that skips the tool and its role check: a subagent's
                     python code importing kitlib.backlog (-c, standard input, -m, runpy) or naming
                     tools/backlog.py; any session's but the owner's python code naming the hub's
                     record writer by its module name, not its file's path
                     (kitlib.hubsite.recordwrite, plan_answer: hub_writer_code);
                     any session's but the owner's git write of the store's ref
                     (refs/backlog/*; the backlog branch when the store is `branch`); any
                     session's but the owner's edit of a folder store's records.
  pm-data-audit      any session's but the owner's write in <pm_data>/audit/, the backlog's audit
                     log (kitlib.backlog.audit), which only tools/backlog.py appends to.

A main session's backlog.py run is the tool's own check (policy.check, by its session mark).
A host with no subagent identity (kitlib.host.hookmain.UNIDENTIFIED) gives a subagent its
session's mark, so the tool's own check judges it there too.
"""
import os
import re

from kitlib import agentname
from kitlib.git import repo

from . import commands as cm
from . import jail
from . import rules

ROLE_RULE = 'backlog-role'
RAW_RULE = 'backlog-raw-write'
AUDIT_RULE = 'pm-data-audit'
AUDIT_FOLDER = 'audit'                          # kitlib.backlog.audit.FOLDER
SCRIPT = 'backlog.py'
TOOL = 'backlog'                                # rules.kit_tool's name for it
UNIDENTIFIED = 'unidentified'                   # kitlib.host.hookmain.UNIDENTIFIED
ASK = ('Ask a session whose role may take it (your coordinator; a coordinator asks the PM) to run it, '
       'and say why. Every role may read the record:')
# python code that reaches the backlog's library or runs its tool: an import of kitlib.backlog
# in any spelling, a name of tools/backlog.py, or the kit command naming it (['kit', 'backlog', ...])
CODE_RE = re.compile(r'kitlib\s*[./\\]\s*backlog\b|from\s+kitlib\s+import\s+[^\n;]*\bbacklog\b|'
                     r'\btools\s*[./\\]\s*backlog\b|\bbacklog\.py\b|\bkit(?:\.py|\.cmd|\.exe)?\W{1,4}backlog\b')
# python code that names the hub's record writer or the store's answer filing: hubsite then
# recordwrite across a dot; any attribute .recordwrite (an alias: serve.recordwrite,
# h.recordwrite; a backslash line break allowed after the dot); `from kitlib.hubsite import`
# naming it, on one line, across backslash line breaks or inside its parentheses; or plan_answer
# (kitlib.backlog.plan). Only the hub runs them. The file's path (kitlib/hubsite/recordwrite.py,
# with either slash) is not read: code that reads the file writes nothing, and the file run as a
# script stops at its relative import (`from .store import`) before it writes.
HUB_WRITER_RE = re.compile(r'\bhubsite\s*\.\s*recordwrite\b|\.(?:\s|\\\n)*recordwrite\b|'
                           r'from\s+kitlib\s*\.\s*hubsite\s+import\s+'
                           r'(?:\([^)]*|[^\n;]*(?:\\\n[^\n;]*)*)\brecordwrite\b|\bplan_answer\b')
HUB_WRITER_MODULE = 'kitlib.hubsite.recordwrite'
HUB_FILE = 'questions file [<question id>]'     # tools/hub.py's agent verb that files bound answers
REF_DIR = 'refs/backlog/'                     # kitlib.backlog.store.STORE_REF's folder
BRANCH = 'backlog'                              # kitlib.backlog.store.BRANCH
REMOTE_BRANCH_RE = re.compile(r'^refs/remotes/[^/]+/%s$' % BRANCH)


def read_record(root, rid):
    """The record `rid` as the store the checkout `root` opens holds it locally (no fetch), or
    None: the tool's own opener, so a linked worktree whose .kit.json names no store reads its PM
    data worktree's live store (kitlib.backlog.store.worktree_live_store). The guard's one read of
    the store (patched in most tests)."""
    from kitlib.backlog import opener
    return opener.open_store(root, fetch=False).store.load().get(rid)


def shell(event, ctx, cmd, inv, cwd, role):
    """The rules.shell_command rule: role_run, raw_code, then store_ref_write."""
    return role_run(event, ctx, cmd, inv, cwd, role) or raw_code(event, ctx, cmd, inv, role) or \
        store_ref_write(event, ctx, cmd, inv, cwd, role)


def _judged_subagent(event):
    return rules.subagent(event) and getattr(event, 'agent', None) != UNIDENTIFIED


# ---------------------------------------------------------------- a run of the tool

def py_module(inv):
    """The module python's -m runs, as written (`python -m runpy <module>`: that module), or None
    for a script, -c, standard input or a module the text does not show."""
    if inv is None or inv.dyn or not rules.PY_RUN_RE.match(inv.name or ''):
        return None
    args = list(inv.args)
    k = 0
    while k < len(args):
        t = args[k].text
        flag = rules.PY_FLAG_RE.match(t)
        if t == '-' or (flag and flag.group(1) == 'c'):
            return None
        if flag:
            if flag.group(2):
                mod, rest = flag.group(2), args[k + 1:]
            elif k + 1 < len(args) and not args[k + 1].dyn:
                mod, rest = args[k + 1].text, args[k + 2:]
            else:
                return None
            if mod == 'runpy' and rest and not rest[0].dyn and not rest[0].text.startswith('-'):
                return rest[0].text
            return mod
        if not t.startswith('-'):
            return None
        k += 2 if t in cm.PY_VALUE_OPTS else 1
    return None


def run_words(inv):
    """The Words after tools/backlog.py when `inv` runs it (rules.tool_run: the script, -m
    tools.backlog; runpy's module read as the script; the kit command's `kit backlog`:
    rules.script_run), else None."""
    run = rules.script_run(inv)
    if run is None or run[0] != SCRIPT:
        return None
    return run[1]


def argv_words(cmd, words):
    """read_argv's (text, readable) pairs: a computed word is unreadable, and so is a PowerShell
    word holding a double quote (Windows PowerShell 5.1 passes it on unescaped, so it can split).
    A bash word that is all one double-quoted string (`"$PWD"`, `"$M/x"`; not `"$@"` or
    `${a[@]}`, which split) is policy.ONE: one word, its text unknown.
    A Start-Process -ArgumentList string comes already split as Windows splits it
    (commands._split_list): a double-quoted segment is one word, spaces and all."""
    from kitlib.backlog import policy
    ps = cmd.shell == rules.sh.PS
    out = []
    for w in words:
        if not w.dyn:
            out.append((w.text, not (ps and '"' in w.text)))
        else:
            out.append((w.text, policy.ONE if _one_word(cmd, w) else False))
    return out


def _one_word(cmd, w):
    """True when the bash word `w` is written as one double-quoted string holding no `"` or `@`."""
    src = cmd.source or ''
    if cmd.shell != rules.sh.BASH or w.synth or w.start is None or w.end is None or \
            not 0 <= w.start < w.end <= len(src):
        return False
    return '"' not in w.text and '@' not in w.text and src[w.start:w.end] == '"%s"' % w.text


def _unread(pairs, argv, who):
    """The refusal's sentence naming the words read_argv could not read when they decided it, else
    '': no verb read, or the run without its unread words, as its own pull, would be allowed (an
    unread `-C "$PWD"` on a `close` the role may never take decides nothing)."""
    from kitlib.backlog import policy, roles
    words = [t for t, ok in pairs if ok is not True]
    if not words:
        return ''
    if argv.verb is not None:
        given = dict(argv.given)
        given.pop(policy.UNKNOWN, None)
        if policy.refusal(who, policy.sort(argv.verb, given, _Unheld()), (roles.OWN_PULL,)):
            return ''
    return ' The guard cannot read %s (computed, or a word that can split), so it judged the run ' \
           'as the strictest those words could make it; write them out.' \
           % ', '.join('`%s`' % t for t in words)


def role_run(event, ctx, cmd, inv, cwd, role):
    if not _judged_subagent(event):
        return None
    mod = py_module(inv)
    if mod and _backlog_module(mod):
        return None                             # raw_code's
    words = run_words(inv)
    if words is None:
        return None
    from kitlib.backlog import policy, roles
    pairs = argv_words(cmd, words)
    argv = policy.read_argv(pairs)
    if argv.help:
        return None
    who = roles.role_of(agentname.bare(getattr(event, 'agent_role', None) or ''), True)
    rows = policy.sort(argv.verb, argv.given)
    text = policy.refusal(who, rows)
    form, instead = '', None
    here = cm.run_dir(inv)
    if text and argv.rid and not argv.given.get(policy.UNKNOWN) and \
            policy.refusal(who, rows, (roles.OWN_PULL,)) is None:
        text, form, instead = own_pull_refusal(ctx, cmd, argv, who, here, pairs)
    if not text:
        return None
    # the rows were sorted as if the record were held (not read: the strictest); when the role
    # may not take the unheld row either, that row is the one to name
    text = policy.refusal(who, policy.sort(argv.verb, argv.given, _Unheld())) or text
    return rules.refuse(ROLE_RULE, role, '`%s`: %s (kitlib/backlog/roles.py).%s%s%s'
                        % (rules.bare(cmd), text, _unread(pairs, argv, who), form, '' if instead else ' ' + ASK),
                        instead or read_alternative(ctx, cmd, here, argv))


def _root(cmd, here, argv):
    """The folder the run names: its -C, read from `here`, the directory the tool runs in
    (commands.run_dir: the command's, or a Start-Process -WorkingDirectory), else `here` itself.
    None, fail closed, when -C is a word the guard cannot read (policy.UNREAD) or when the folder
    rests on a `here` the text cannot tell (run_dir's None: a cd it cannot read, a computed or
    unplaceable -WorkingDirectory); never the hook's directory in its place."""
    from kitlib.backlog import policy
    if argv.root is policy.UNREAD:
        return None
    return rules.sh.resolve_path(argv.root, here, cmd.shell) if argv.root is not None else here


def read_alternative(ctx, cmd, here, argv):
    """The tool's read of the record the run names (`show <ID>`; `list` when it names none), at
    the folder it runs on (_root: its -C, else the directory it runs in; <worktree> when unknown)."""
    root = _root(cmd, here, argv)
    where = rules.quote(root) if root else '<worktree>'
    what = 'show %s' % argv.rid if argv.rid and re.match(r'^[\w.-]+$', argv.rid) else 'list'
    return 'python %s -C %s %s' % (rules.kit_tool(ctx, TOOL), where, what)


class _Unheld(object):
    """A record read as neither held nor priced, for naming the row a refusal is about."""

    def get(self, key):
        return None

    def all(self, key):
        return []


def _branch_at(ctx, where):
    """The branch checked out at `where` (ctx.git.current_branch), or None."""
    try:
        return ctx.git.current_branch(where)[0] or None
    except Exception:                           # noqa: BLE001 - no branch: not its own
        return None


def _worktree_on(ctx, where, branch):
    """True when the directory `where` is in a linked worktree whose checked-out branch is
    `branch`, as written: the --as branch's own worktree. ctx.place reads the real path (a
    junction or symlink on the way goes to git) and answers the main checkout, and the PM data
    worktree, as MAIN, so neither is ever one."""
    if not where or not isinstance(branch, str) or not branch:
        return False
    place = ctx.place(where)
    if place.kind != repo.LINKED or not place.top:
        return False
    return _branch_at(ctx, place.top) == branch


def own_pull_refusal(ctx, cmd, argv, who, here=None, pairs=None):
    """(policy.refusal, the form sentence, the form or None) for a run only a builder's own pull
    would allow: the record read where the tool runs (_root: its -C folder, else `here`; none
    when unknown), through the store that folder opens (read_record, the tool's own opener: a
    linked worktree whose .kit.json names no store reads its PM data worktree's live store,
    kitlib.backlog.store.worktree_live_store), its pull compared with the branch the run acts as.
    That is the branch checked out at that folder, so `-C <worktree>` alone, from any directory,
    is a builder's own form; or an --as branch when `here`, the directory the tool runs in
    (commands.run_dir: as the line leaves it, or a Start-Process -WorkingDirectory; None, never
    evidence, when the text cannot tell), is that branch's linked worktree (_worktree_on). Any
    other --as is not this session's. The main checkout and the PM data worktree are never a
    linked worktree on the pulling branch, so a run from either is never its own. When the own
    pull alone would allow the run, the form sentence names the accepted way, else it is ''; the
    form is that way written out (own_form) when the guard sees the branch's worktree and the
    store it opens holds the record pulled by that branch, else None. Nothing own_form reads
    changes the decision."""
    from kitlib.backlog import policy, roles
    root = _root(cmd, here, argv)                # None: no record read, so never its own pull
    rec, branch = None, None
    if root:
        branch = _branch_at(ctx, root)
        if argv.branch is not None and argv.branch != branch and _worktree_on(ctx, here, argv.branch):
            branch = argv.branch
        if branch and argv.branch in (None, branch):
            try:
                rec = read_record(root, argv.rid)
            except Exception:                   # noqa: BLE001 - a store that does not read: not its own
                rec = None
    facts = (roles.OWN_PULL,) if rec is not None and policy.own_pull(rec, branch) else ()
    rows = policy.sort(argv.verb, argv.given, rec)
    text = policy.refusal(who, rows, facts)
    form, instead = '', None
    if text and policy.refusal(who, rows, (roles.OWN_PULL,)) is None:
        found = own_form(ctx, argv, who, root, here, pairs)
        if found is not None:
            b, wt, instead = found
            form = OWN_FOUND % (b, rules.fwd(wt))
        else:
            form = OWN_FORM % (rules.kit_tool(ctx, TOOL), argv.verb,
                               argv.rid if re.match(r'^[\w.-]+$', argv.rid) else '<ID>')
    return text, form, instead


def own_form(ctx, argv, who, root, here, pairs):
    """(branch, worktree, form) for a run its own pull would allow but that was refused: the
    branch the run acts as (its --as, else the branch checked out at its folder), that branch's
    linked worktree as the guard sees it (the folder the tool runs in when it is one on that
    branch, else the run's -C folder when that is one), and the run written again as
    `python <tool> -C <worktree> <verb> <words>`, with no --as and no cd: one bare call, which
    reads the store the worktree opens (read_record; kitlib.backlog.store.worktree_live_store) and
    acts as its branch. None when no such worktree shows, its store does not hold the record
    pulled by that branch (someone else's pull, or none), or the words do not read. It never
    names a branch the run does not act as, nor a worktree whose store it did not read the record
    in; the guard does not look a branch's worktree up elsewhere."""
    from kitlib.backlog import policy, roles
    b = argv.branch if argv.branch is not None else (_branch_at(ctx, root) if root else None)
    if not root or not isinstance(b, str) or not b:
        return None
    hp = ctx.place(here) if here else None
    if hp is not None and hp.kind == repo.LINKED and hp.top:
        wt = hp.top if _branch_at(ctx, hp.top) == b else None
    else:
        wt = ctx.place(root).top if _worktree_on(ctx, root, b) else None
    if not wt:
        return None
    try:
        rec = read_record(wt, argv.rid)
    except Exception:                           # noqa: BLE001 - a store that does not read: not there
        return None
    if not policy.own_pull(rec, b) or \
            policy.refusal(who, policy.sort(argv.verb, argv.given, rec), (roles.OWN_PULL,)) is not None:
        return None
    words = _rerun_words(pairs)
    if words is None:
        return None
    return b, wt, 'python %s -C %s %s' % (rules.kit_tool(ctx, TOOL), rules.quote(rules.fwd(wt)), words)


def _rerun_words(pairs):
    """The run's words from its verb on, after the global options it gives before the verb but -C
    and --as (own_form writes its own -C and needs no --as), each a word both shells read as written
    (rules.shell_word); None when a word does not read."""
    from kitlib.backlog import policy
    if not pairs or any(ok is not True for _t, ok in pairs):
        return None
    out, k = [], 0
    while k < len(pairs):
        text = pairs[k][0]
        name, eq, _value = text.partition('=')
        if text == '--':
            k += 1
            continue
        if not text.startswith('-'):
            break
        if name.startswith('-C') and not name.startswith('--') and len(name) > 2:
            k += 1
            continue
        if name not in policy.GLOBAL:
            return None
        take = 2 if policy.GLOBAL[name] and not eq else 1
        if name not in ('-C', '--as'):
            out.extend(t for t, _ok in pairs[k:k + take])
        k += take
    if k >= len(pairs):
        return None
    out.extend(t for t, _ok in pairs[k:])
    return ' '.join(rules.shell_word(t) for t in out)


# the form refusal sentence for a run its own pull would allow (own_pull_refusal)
OWN_FORM = (' It is your own pull only when the record is pulled by the branch checked out in the '
            'run\'s -C folder: run it as one call, `python %s -C <your worktree> %s %s ...`, with -C '
            'your branch\'s linked worktree, no --as and no cd (it reads the store your worktree '
            'opens); a worktree on another branch, the main checkout, the PM data worktree, or a '
            'branch that does not hold the pull, does not count.')
# the form refusal sentence when own_form found the worktree and the record in its store
OWN_FOUND = (' The record is pulled by %s: run it as one call with -C that branch\'s worktree %s, no '
             '--as and no cd:')


# ---------------------------------------------------------------- code that skips the tool

def _backlog_module(mod):
    return mod == 'kitlib.backlog' or mod.startswith('kitlib.backlog.')


def raw_code(event, ctx, cmd, inv, role):
    """A subagent's python that imports kitlib.backlog or runs tools/backlog.py as code: -m or
    runpy of a kitlib.backlog module, -c or standard-input code (rules.inline_code). Then, in any
    session's but the owner's, code that names the hub's record writer (hub_writer_code). A script
    file is not read."""
    return backlog_code(event, ctx, cmd, inv, role) or hub_writer_code(event, ctx, cmd, inv, role)


def hub_writer_code(event, ctx, cmd, inv, role):
    """Any session's but the owner's python that names kitlib.hubsite.recordwrite or plan_answer
    (HUB_WRITER_RE; -m or runpy of that module): the hub's own writes of a record, an answer the
    owner gave filed into it, which no agent session makes, main sessions included."""
    if rules.is_owner_session(event, ctx):
        return None
    if py_module(inv) == HUB_WRITER_MODULE:
        how = 'runs %s' % HUB_WRITER_MODULE
    else:
        how = None
        for text in rules.inline_code(cmd, inv):
            m = HUB_WRITER_RE.search(text)
            if m:
                how = 'runs code that names %s' % m.group(0)
                break
    if not how:
        return None
    return rules.refuse(RAW_RULE, role, '`%s` %s: the hub\'s record writer (kitlib/hubsite/recordwrite.py, the '
                        'store\'s plan_answer) files the owner\'s answers and record-page writes, and only the hub '
                        'runs it; no agent session does. Let the hub file a bound question\'s answer (any other '
                        'record write is tools/backlog.py\'s verb):'
                        % (rules.hub_line(rules.bare(cmd)), how),
                        'python %s %s' % (rules.kit_tool(ctx, 'hub'), HUB_FILE))


def backlog_code(event, ctx, cmd, inv, role):
    """A subagent's python that reaches the backlog's library (raw_code's first rule)."""
    if not _judged_subagent(event):
        return None
    mod = py_module(inv)
    if mod and _backlog_module(mod):
        how = 'runs %s' % mod
    else:
        how = None
        for text in rules.inline_code(cmd, inv):
            m = CODE_RE.search(text)
            if m:
                how = 'runs code that names %s' % m.group(0)
                break
    if not how:
        return None
    return rules.refuse(RAW_RULE, role, '`%s` %s: code that reaches the backlog\'s library skips tools/backlog.py '
                        'and its role check, so it is a raw write (kitlib/backlog/roles.py). Run the tool\'s '
                        'verb:' % (rules.hub_line(rules.bare(cmd)), how),
                        'python %s -C <worktree> <verb> <arguments>' % rules.kit_tool(ctx, TOOL))


# ---------------------------------------------------------------- the store's refs

def store_kind(ctx, path):
    """The "backlog".store of the .kit.json that holds for `path`, or None when none reads."""
    return _store_read(ctx, path)[0]


def _store_read(ctx, path):
    """(store_kind for `path`, why the .kit.json there did not read: a text, or None). A load that
    raises other than ConfigError (a file that is not UTF-8: kitlib.config lets that through, so
    ctx.config_error has no word of it) answers no store and its fault."""
    try:
        cfg = ctx.config(path)
        return (cfg.section('backlog')['store'] if cfg is not None else None), None
    except Exception as e:                      # noqa: BLE001 - no store read: refs/backlog only
        return None, 'its .kit.json does not read: %s: %s' % (type(e).__name__, e)


def is_store_ref(name, store, short=False):
    """True when the ref `name` is the store's: under refs/backlog/ always, and with a `branch`
    store the backlog branch (refs/heads/backlog, refs/remotes/<remote>/backlog; `short`: its
    bare name too, as push and branch read one). A wildcard matches when it would take one. Case
    is not compared: on this machine's file system refs/Backlog/x is the file refs/backlog/x."""
    name = name.lstrip('+').lower()
    if '*' in name:
        import fnmatch
        samples = [REF_DIR + 'main'] + (['refs/heads/' + BRANCH] if store == 'branch' else [])
        return any(fnmatch.fnmatchcase(s, name) for s in samples)
    if name.startswith(REF_DIR) or name == REF_DIR.rstrip('/'):
        return True
    if store != 'branch':
        return False
    return name == 'refs/heads/' + BRANCH or bool(REMOTE_BRANCH_RE.match(name)) or (short and name == BRANCH)


def _store_unsure(ctx, gc, where, fault=None):
    """Why store_kind's None for `where` is no proof that no store reads there (a text), or None:
    git re-pointed (gc.blind), a directory the text does not name (gc.unknown), a .kit.json that
    does not load (ctx.config_error) or read (`fault`, _store_read), a place git cannot tell
    about, and any other place in a checkout: one whose main checkout the guard does not find
    (ctx.main_top: a linked worktree whose git common dir is not named .git, as a bare clone's
    or a --separate-git-dir repository's), so no .kit.json was read there. A folder in no
    checkout (repo.NONE) is the one sure None."""
    if gc.blind:
        return 'git is re-pointed: %s' % gc.blind
    if gc.unknown:
        return 'the directory git runs in is unknown'
    why = ctx.config_error(where) if where else None
    if why:
        return 'its .kit.json does not load: %s' % why
    if fault:
        return fault
    place = ctx.place(where)
    if place.kind == repo.UNKNOWN:
        return 'git cannot tell the place: %s' % place.detail
    if place.kind != repo.NONE:
        return ('the guard finds no main checkout for the place %s (%s, git common dir %s), so it read no '
                '.kit.json there' % (place.top, place.kind, place.common))
    return None


def _written_refs(gc, store, unsure=None):
    """The store refs the git call `gc` writes (texts), and why it may write the store's ref (a
    text) when its words do not name it: a mirror push, or, where a store reads (`store` not None)
    or may (`unsure`, _store_unsure), an update-ref whose ref the guard cannot read
    (rules._update_ref_unread: --stdin, an option it does not know, xargs, a computed word, no
    ref). Where none reads, such an update-ref still writes every store ref its words name
    literally."""
    sub = gc.sub
    short, longs, pos = rules._opts(sub, gc.rest)
    texts = [w.text for w in pos if not w.dyn]
    if sub == 'update-ref':
        unread = rules._update_ref_unread(gc, short, longs, pos)
        if unread:
            if store is None and not unsure:
                return [t for t in texts if is_store_ref(t, store)], None
            held = ('this repository reads a backlog store (%s)' % store if store is not None else
                    'the guard cannot tell whether a backlog store reads there (%s)' % unsure)
            return [], ('may write the backlog store\'s ref: the guard cannot tell which ref it writes (%s), '
                        'and %s' % (unread, held))
        return [pos[0].text] if is_store_ref(pos[0].text, store) else [], None
    if sub == 'push':
        if 'mirror' in longs:
            return [], 'pushes every ref, the backlog store\'s among them'
        delete = 'd' in short or 'delete' in longs
        out = []
        for spec in texts[1:]:
            src, colon, dst = spec.lstrip('+').partition(':')
            dst = dst if colon or delete else src
            if delete and not colon:
                dst = src
            if dst and is_store_ref(dst, store, short=True):
                out.append(spec)
        return out, None
    if sub == 'fetch':
        out = []
        for spec in texts[1:]:
            _src, colon, dst = spec.lstrip('+').partition(':')
            # a short destination is a branch (or under refs/) as git's fetch reads it
            if colon and dst and is_store_ref(rules.fetch_dst_ref(dst), store, short=False):
                out.append(spec)
        return out, None
    if sub == 'branch' and store == 'branch':
        listing = 'l' in short or bool({'list', 'show-current', 'merged', 'no-merged', 'contains', 'no-contains',
                                        'points-at'} & longs)
        writes = bool(set('dDmMcCf') & short) or bool({'delete', 'move', 'copy', 'force'} & longs)
        if listing and not writes:
            return [], None
        return [t for t in texts if is_store_ref(t, store, short=True)], None
    return [], None


def store_ref_write(event, ctx, cmd, inv, cwd, role):
    """Any session's but the owner's git write of the backlog store's ref: update-ref, a push
    or a fetch whose destination is it, a mirror push, an update-ref whose ref the guard cannot
    read where a store reads or the guard cannot tell (store_kind, _store_unsure; it fails closed:
    --stdin, xargs, a computed ref; not in a
    scratch repository, rules._scratch_repo, as git-shared-ref), and with a `branch` store a branch
    write of the backlog branch. The store tool writes it (its compare-and-swap) as a process,
    which no rule reads."""
    if rules.is_owner_session(event, ctx):
        return None
    gc = rules.git_call_judged(inv)
    if gc is None or gc.sub_dyn or gc.sub not in ('update-ref', 'push', 'fetch', 'branch'):
        return None
    where = gc.dir if gc.dir and not gc.unknown else (cmd.cwd or cwd)
    store, fault = _store_read(ctx, where) if where else (None, None)
    refs, whole = _written_refs(gc, store, None if store is not None else _store_unsure(ctx, gc, where, fault))
    if not refs and not whole:
        return None
    if not refs and gc.sub == 'update-ref' and gc.dir and not gc.unknown and not gc.blind and \
            rules._scratch_repo(ctx, ctx.place(gc.dir)):
        return None                             # unread, in a scratch repository: its refs are its own
    what = whole or 'writes the backlog store\'s ref (%s)' % ', '.join(refs)
    return rules.refuse(RAW_RULE, role, '`%s` %s. Records are written only through tools/backlog.py (its '
                        'compare-and-swap and its role check), so a direct ref write is a raw write, which no agent '
                        'session makes (kitlib/backlog/roles.py). Run the tool\'s verb:' % (rules.bare(cmd), what),
                        'python %s -C <worktree> <verb> <arguments>' % rules.kit_tool(ctx, TOOL))


# ---------------------------------------------------------------- a folder store's records

def raw_edit(event, ctx, target, role, what):
    """Any session's but the owner's edit of a file in a folder store (`main`: docs/backlog;
    another path: that folder) in the checkout it is in. An edit of the folder's records skips
    the tool and its role check."""
    if not target or rules.is_owner_session(event, ctx):
        return None
    store = store_kind(ctx, target)
    if not store or store in ('ref', 'branch'):
        return None
    top = ctx.place(target).top
    if not top:
        return None
    folder = os.path.join(top, *('docs/backlog' if store == 'main' else store).replace('\\', '/').split('/'))
    if not jail.inside(jail.real(target), jail.real(folder)):
        return None
    return rules.refuse(RAW_RULE, role, '%s: %s is the backlog\'s record folder (store %s); records are written '
                        'only through tools/backlog.py and its role check, so an edit there is a raw write, which no '
                        'agent session makes (kitlib/backlog/roles.py). Run the tool\'s verb:'
                        % (what, rules.fwd(folder), store),
                        'python %s -C %s <verb> <arguments>' % (rules.kit_tool(ctx, TOOL), rules.quote(top)))


def audit_edit(event, ctx, target, role, what):
    """Any session's but the owner's write in <pm_data>/audit/ (the machine config's pm_data.path):
    the backlog's audit log, which tools/backlog.py alone appends to after its writes. With no
    pm_data named there is no log, and nothing to refuse."""
    if not target or rules.is_owner_session(event, ctx):
        return None
    data = ctx.pm_data_path()
    if not data:
        return None
    folder = os.path.join(data, AUDIT_FOLDER)
    if not jail.inside(jail.real(target), jail.real(folder)):
        return None
    return rules.refuse(AUDIT_RULE, role, '%s: %s is the backlog\'s audit log, which only tools/backlog.py appends '
                        'to, after its own writes; no session but the owner\'s writes there. Make the change '
                        'through the tool, which logs it:' % (what, rules.fwd(folder)),
                        'python %s -C <worktree> <verb> <arguments>' % rules.kit_tool(ctx, TOOL))
