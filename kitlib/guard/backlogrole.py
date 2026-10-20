"""The backlog roles in the guard (docs/guard.md "Backlog roles"; docs/backlog.md "Roles").

  backlog-role       a subagent's run of tools/backlog.py, judged by kitlib.backlog.roles for the
                     role its hook payload names (Action.agent_role), from the command's words
                     (kitlib.backlog.policy.read_argv). A PM_KIT_ROLE the line sets changes
                     nothing. A builder's own pull (roles.OWN_PULL) is read from the store, as it
                     is locally, only when it alone would decide.
  backlog-raw-write  a write of the backlog that skips the tool and its role check: a subagent's
                     python code importing kitlib.backlog (-c, standard input, -m, runpy) or naming
                     tools/backlog.py; any session's but the owner's git write of the store's ref
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
# in any spelling, or a name of tools/backlog.py
CODE_RE = re.compile(r'kitlib\s*[./\\]\s*backlog\b|from\s+kitlib\s+import\s+[^\n;]*\bbacklog\b|'
                     r'\btools\s*[./\\]\s*backlog\b|\bbacklog\.py\b')
REF_DIR = 'refs/backlog/'                       # kitlib.backlog.store.STORE_REF's folder
BRANCH = 'backlog'                              # kitlib.backlog.store.BRANCH
REMOTE_BRANCH_RE = re.compile(r'^refs/remotes/[^/]+/%s$' % BRANCH)


def read_record(root, rid):
    """The record `rid` as the store of the checkout `root` holds it locally (no fetch), or None.
    The guard's one read of the store (patched in the tests)."""
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
    tools.backlog; runpy's module read as the script), else None."""
    run = rules.tool_run(inv)
    if run is not None and run[0] == 'runpy.py':
        words = run[1]
        if not words or words[0].dyn:
            return None
        run = words[0].text.rsplit('.', 1)[-1].lower() + '.py', words[1:]
    if run is None or run[0] != SCRIPT:
        return None
    return run[1]


def argv_words(cmd, inv, words):
    """read_argv's (text, readable) pairs: a computed word is unreadable, and so is a PowerShell
    word holding a double quote (Windows PowerShell 5.1 passes it on unescaped, so it can split);
    a Start-Process -ArgumentList string is split as Windows joins it."""
    ps = cmd.shell == rules.sh.PS
    out = []
    for w in words:
        ok = not w.dyn and not (ps and '"' in w.text)
        texts = w.text.split() if inv.via == 'start-process' and ok and ' ' in w.text else [w.text]
        out.extend((t, ok) for t in texts)
    return out


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
    argv = policy.read_argv(argv_words(cmd, inv, words))
    if argv.help:
        return None
    who = roles.role_of(agentname.bare(getattr(event, 'agent_role', None) or ''), True)
    rows = policy.sort(argv.verb, argv.given)
    text = policy.refusal(who, rows)
    if text and argv.rid and not argv.given.get(policy.UNKNOWN) and \
            policy.refusal(who, rows, (roles.OWN_PULL,)) is None:
        text = own_pull_refusal(ctx, cmd, cwd, argv, who)
    if not text:
        return None
    # the rows were sorted as if the record were held (not read: the strictest); when the role
    # may not take the unheld row either, that row is the one to name
    text = policy.refusal(who, policy.sort(argv.verb, argv.given, _Unheld())) or text
    return rules.refuse(ROLE_RULE, role, '`%s`: %s (kitlib/backlog/roles.py). %s'
                        % (rules.bare(cmd), text, ASK), read_alternative(ctx, cmd, cwd, argv))


def read_alternative(ctx, cmd, cwd, argv):
    """The tool's read of the record the run names (`show <ID>`; `list` when it names none), at
    the folder it runs on (its -C, else the command's directory)."""
    base = cmd.cwd or cwd
    root = rules.sh.resolve_path(argv.root, base, cmd.shell) if argv.root is not None else base
    where = rules.quote(root) if root else '<worktree>'
    what = 'show %s' % argv.rid if argv.rid and re.match(r'^[\w.-]+$', argv.rid) else 'list'
    return 'python %s -C %s %s' % (rules.kit_tool(ctx, TOOL), where, what)


class _Unheld(object):
    """A record read as neither held nor priced, for naming the row a refusal is about."""

    def get(self, key):
        return None

    def all(self, key):
        return []


def own_pull_refusal(ctx, cmd, cwd, argv, who):
    """policy.refusal for a run only a builder's own pull would allow: the record read where the
    tool runs (its -C folder, else the command's directory), its pull compared with the branch
    checked out there; an --as naming another branch is not this session's."""
    from kitlib.backlog import policy, roles
    base = cmd.cwd or cwd
    root = rules.sh.resolve_path(argv.root, base, cmd.shell) if argv.root is not None else base
    rec, branch = None, None
    if root:
        try:
            branch = ctx.git.current_branch(root)[0]
        except Exception:                       # noqa: BLE001 - no branch: not its own
            branch = None
        if branch and argv.branch in (None, branch):
            try:
                rec = read_record(root, argv.rid)
            except Exception:                   # noqa: BLE001 - a store that does not read: not its own
                rec = None
    facts = (roles.OWN_PULL,) if rec is not None and policy.own_pull(rec, branch) else ()
    return policy.refusal(who, policy.sort(argv.verb, argv.given, rec), facts)


# ---------------------------------------------------------------- code that skips the tool

def _backlog_module(mod):
    return mod == 'kitlib.backlog' or mod.startswith('kitlib.backlog.')


def raw_code(event, ctx, cmd, inv, role):
    """A subagent's python that imports kitlib.backlog or runs tools/backlog.py as code: -m or
    runpy of a kitlib.backlog module, -c or standard-input code (rules.inline_code). A script file
    is not read."""
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
    try:
        cfg = ctx.config(path)
        return cfg.section('backlog')['store'] if cfg is not None else None
    except Exception:                           # noqa: BLE001 - no store read: refs/backlog only
        return None


def is_store_ref(name, store, short=False):
    """True when the ref `name` is the store's: under refs/backlog/ always, and with a `branch`
    store the backlog branch (refs/heads/backlog, refs/remotes/<remote>/backlog; `short`: its
    bare name too, as push and branch read one). A wildcard matches when it would take one."""
    name = name.lstrip('+')
    if '*' in name:
        import fnmatch
        samples = [REF_DIR + 'main'] + (['refs/heads/' + BRANCH] if store == 'branch' else [])
        return any(fnmatch.fnmatchcase(s, name) for s in samples)
    if name.startswith(REF_DIR) or name == REF_DIR.rstrip('/'):
        return True
    if store != 'branch':
        return False
    return name == 'refs/heads/' + BRANCH or bool(REMOTE_BRANCH_RE.match(name)) or (short and name == BRANCH)


def _written_refs(gc, store):
    """The store refs the git call `gc` writes (texts), and why it may write every ref (a text) when
    its words do not name them: a mirror push, or update-ref reading its commands from its input."""
    sub = gc.sub
    short, longs, pos = rules._opts(sub, gc.rest)
    texts = [w.text for w in pos if not w.dyn]
    if sub == 'update-ref':
        if 'stdin' in longs:
            return [], 'reads the refs it writes from its input, which may name the backlog store\'s'
        return [t for t in texts[:1] if is_store_ref(t, store)], None
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
            if colon and dst and is_store_ref(dst, store, short=False):
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
    or a fetch whose destination is it, a mirror push, `update-ref --stdin` (its refs are in its
    input, which no rule reads, so it fails closed; not in a scratch repository, rules._scratch_repo,
    as git-shared-ref), and with a `branch` store a branch write of the backlog branch. The store
    tool writes it (its compare-and-swap) as a process, which no rule reads."""
    if rules.is_owner_session(event, ctx):
        return None
    gc = rules.git_call_judged(inv)
    if gc is None or gc.sub_dyn or gc.sub not in ('update-ref', 'push', 'fetch', 'branch'):
        return None
    where = gc.dir if gc.dir and not gc.unknown else (cmd.cwd or cwd)
    store = store_kind(ctx, where) if where else None
    refs, whole = _written_refs(gc, store)
    if not refs and not whole:
        return None
    if not refs and gc.sub == 'update-ref' and gc.dir and not gc.unknown and not gc.blind and \
            rules._scratch_repo(ctx, ctx.place(gc.dir)):
        return None                             # --stdin in a scratch repository: its refs are its own
    what = whole or 'writes the backlog store\'s ref (%s)' % ', '.join(refs)
    return rules.refuse(RAW_RULE, role, '`%s` %s: records are written only through tools/backlog.py (its '
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
