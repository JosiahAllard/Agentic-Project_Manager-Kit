"""Run one operation of the owner's delegation list (docs/delegations.md "Running").

`run(op, pairs, trigger, cfg, ...)` -> Result(entry, argv, returncode, error), or RunRefused with
nothing run and nothing written. In this order:

  1. the trigger: a hub question id (`Q-nnnn`, the op lists hub-answer and is reversible) or an
     owner ledger id (the op lists owner-session). The literal `session` and the standing
     triggers are refused.
  2. the arguments: exactly the declared ones, each passing schema.check_arg. They fill whole
     `{name}` tokens of the command and `{name}` inside precondition strings; `<kit>` becomes the
     kit folder. The filled command and precondition strings are checked again with
     schema.exclusion_hits.
  3. the program: argv[0] is an absolute `.exe` path or a bare name found as `<name>.exe` in an
     absolute PATH folder. The working folder is never searched; nothing else (a .bat, a .cmd, a
     script) is started, so no shell ever runs.
  4. the order: the run line `run D-nnn k=v ...` (arguments sorted by name) must be what the owner
     chose. A hub trigger: the logged question's chosen option label is the run line, chosen in
     the last MAX_AGE, and its ruling is a live owner ledger entry whose Source names the
     question. An owner ledger id: a live owner entry, written in the last MAX_AGE, whose Decides
     is the run line.
  5. the preconditions: file-exists, branch-landed, checklist-line.
  6. the claim, under the runner lock: no live runner entry (Source `under <anchor>: D-nnn
     <trigger>`) has this anchor or this trigger, then one ledger entry, provenance delegated,
     is added (not committed). Each trigger and each owner entry orders one run.
  7. the child: the argv list with shell=False, stdin closed, in the kit folder, every PYTHON*
     variable dropped from its environment.

Nothing here prints, parses arguments or exits (ARCHITECTURE.md, Layers); tools/delegated.py is
the entry point.
"""
import collections
import datetime
import os
import re
import subprocess

from kitlib.fs import writelock
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import serve as hubserve
from kitlib.ledger import files as lfiles
from kitlib.ledger import fmt
from kitlib.ledger import read as lread
from kitlib.ledger import write as lwrite
from kitlib.ledger.errors import Refused as LedgerRefused
from kitlib.ledger.errors import Usage as LedgerUsage
from kitlib.proc import spawn as procspawn

from . import schema

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_AGE = datetime.timedelta(hours=24)       # how old an owner's order may be
SKEW = datetime.timedelta(minutes=5)         # how far in the future a stamp may be (clock drift)
HUB, OWNER = 'hub', 'owner'
SESSION = 'session'
Q_ID = re.compile(r'Q-[0-9]+')
# The Source of an entry this runner wrote: under <anchor>: <operation> <trigger>.
SOURCE_RE = re.compile(r'^under (%s): (%s) (\S+)$' % (fmt.ID, schema.ID))
LOCK_NAME = 'delegated-runs'                 # the runner lock's file name, in the ledger folder
LOCK_PREFIX = 'kit-delegated'
LOCK_WAIT = 30.0
LEDGER_WHEN = '%Y-%m-%d %H:%MZ'
PROV = 'delegated'

Result = collections.namedtuple('Result', 'entry argv returncode error')
Order = collections.namedtuple('Order', 'kind trigger anchor')


class RunRefused(Exception):
    """Nothing was run and nothing was written."""


def _utc(now):
    return now if now is not None else datetime.datetime.now(datetime.timezone.utc)


# --- The operation, its trigger and its arguments ---------------------------------------------

def find(operations, op_id):
    """The Operation `op_id` of the delegation list, or RunRefused."""
    for op in operations:
        if op.id == op_id:
            return op
    raise RunRefused('%s is not in the delegation list (it holds %s)'
                     % (op_id, ', '.join(o.id for o in operations) or 'nothing'))


def trigger_kind(trigger):
    """HUB for a question id, OWNER for a ledger id; RunRefused for anything else."""
    t = trigger or ''
    if t == SESSION:
        raise RunRefused('`session` is not a trigger the runner takes: give the owner ledger id '
                         'whose Decides is the run line')
    if t.startswith('standing:') or 'standing:' + t in schema.TRIGGERS:
        raise RunRefused('%s: standing triggers are not run yet (docs/delegations.md "Known limits")'
                         % t)
    if Q_ID.fullmatch(t):
        return HUB
    if fmt.is_id(t):
        return OWNER
    raise RunRefused('%r is not a trigger: give a hub question id Q-nnnn or an owner ledger id' % t)


def fill_args(op, pairs):
    """{name: value} from `pairs` [(name, value)]: every declared argument once, nothing else,
    each value passing schema.check_arg. RunRefused naming every problem."""
    declared = {a.name: a for a in op.args}
    values, errs = {}, []
    for name, value in pairs:
        if name in values:
            errs.append('%s is given twice' % name)
        elif name not in declared:
            errs.append('%s is not an argument of %s (it takes: %s)'
                        % (name, op.id, ', '.join(sorted(declared)) or 'none'))
        else:
            values[name] = value
    for name in sorted(declared):
        if name not in values:
            errs.append('%s is missing' % name)
    for name in sorted(values):
        errs.extend(schema.check_arg(declared[name], values[name]))
    if errs:
        raise RunRefused('%s: %s' % (op.id, '; '.join(errs)))
    return values


def run_line(op, values):
    """The run line the owner chooses: `run D-nnn` and `name=value` for each, sorted by name."""
    return ' '.join(['run', op.id] + ['%s=%s' % (k, values[k]) for k in sorted(values)])


def _fill_token(tok, values):
    m = schema.PLACEHOLDER.fullmatch(tok)
    return values[m.group(1)] if m else tok


def _fill_text(text, values):
    return schema.PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def _kit_token(tok, kit_root):
    return kit_root + tok[len(schema.KIT_SIGIL):] if tok.startswith(schema.KIT_SIGIL) else tok


def filled(op, values, kit_root):
    """`(argv, preconditions)` with the arguments and `<kit>` filled in. The filled command and
    every filled precondition string are checked with schema.exclusion_hits before `<kit>` is
    (the kit folder is the kit's own, not the owner's text). RunRefused on a hit."""
    argv = [_fill_token(t, values) for t in op.argv]
    hits = ['command: %r is %s (%s)' % (tok, why, eid) for eid, tok, why in schema.exclusion_hits(argv)]
    pres = []
    for j, pre in enumerate(op.preconditions):
        d = {}
        for k, v in pre:
            if k != 'kind' and isinstance(v, str):
                v = _fill_text(v, values)
                hits.extend('preconditions[%d]: %r is %s (%s)' % (j, tok, why, eid)
                            for eid, tok, why in schema.exclusion_hits(v.split()))
                v = v.replace(schema.KIT_SIGIL, kit_root)
            d[k] = v
        pres.append(d)
    if hits:
        raise RunRefused('%s, filled in, hits a hard exclusion (never delegable): %s'
                         % (op.id, '; '.join(hits)))
    return [_kit_token(t, kit_root) for t in argv], pres


# --- The program ------------------------------------------------------------------------------

def path_dirs(environ=None):
    """The PATH folders of `environ` (default os.environ), quotes dropped."""
    env = os.environ if environ is None else environ
    path = next((v for k, v in env.items() if k.upper() == 'PATH'), '')
    return [d.strip().strip('"') for d in path.split(os.pathsep)]


def program(name, dirs):
    """The absolute path of the `.exe` that `name` (argv[0]) starts; RunRefused for anything else.
    An absolute name must be an existing `.exe`; a bare one is looked up as `<name>.exe` (or
    `name` when it ends `.exe`) in the absolute folders of `dirs` only, never the working folder."""
    if os.path.isabs(name):
        if not name.lower().endswith('.exe'):
            raise RunRefused('%s is not an .exe: the runner starts programs only, never a script '
                             'or a .bat or .cmd (which run in cmd.exe)' % name)
        if not os.path.isfile(name):
            raise RunRefused('%s is not there' % name)
        return os.path.normpath(name)
    if any(c in name for c in '/\\:'):
        raise RunRefused('%s is a relative path: name a program in full or by a bare name on PATH'
                         % name)
    ext = os.path.splitext(name)[1].lower()
    if ext and ext != '.exe':
        raise RunRefused('%s is not an .exe: the runner starts programs only, never a script or a '
                         '.bat or .cmd (which run in cmd.exe)' % name)
    exe = name if ext == '.exe' else name + '.exe'
    for d in dirs:
        if not d or not os.path.isabs(d):
            continue                        # an empty or relative PATH entry is the working folder
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return os.path.normpath(p)
    raise RunRefused('%s is in no absolute PATH folder' % exe)


def child_env(environ):
    """`environ` without any PYTHON* variable: the child's Python is not steered from here."""
    return {k: v for k, v in environ.items() if not k.upper().startswith('PYTHON')}


# --- The ledger -------------------------------------------------------------------------------

def target(found, repo):
    """`(repo column, path, split)` of the file the run's entry goes in. `*` is `pm` in a split
    ledger and `all` in the one-file ledger."""
    if found.split:
        col = 'pm' if repo == '*' else repo
        path = found.repos.get(col)
        if path is None:
            raise RunRefused('repo %r is not one of ledger_repos (%s): the run has no ledger file'
                             % (col, ', '.join(found.repos)))
        return col, path, True
    if not found.frozen:
        raise RunRefused('no ledger file to log the run in')
    return (fmt.ALL_REPOS if repo == '*' else repo), found.frozen, False


def ledger_texts(found):
    """[(path, text)] of every ledger file there is; RunRefused when one cannot be read."""
    out = []
    for _, p in lfiles.readable(found):
        if not os.path.exists(p):
            continue
        text, why = lread.read_file(p)
        if text is None:
            raise RunRefused('cannot read the ledger: %s' % why)
        out.append((p, text.replace('\r\n', '\n')))
    return out


def entry(texts, rid):
    """The parsed entry `rid` (kitlib.ledger.fmt.parse_block), or None."""
    for _, text in texts:
        for i, block in fmt.split_entries(text)[1]:
            if i == rid:
                return fmt.parse_block(block)[0]
    return None


def owner_entry(texts, rid):
    """The live owner entry `rid`, or RunRefused."""
    e = entry(texts, rid)
    if e is None:
        raise RunRefused('%s is not in the ledger' % rid)
    if e.get('retired'):
        raise RunRefused('%s is retired' % rid)
    if e.get('prov') not in fmt.OWNER_PROVS:
        raise RunRefused("%s is not the owner's: its provenance is %s, not one of %s"
                         % (rid, e.get('prov'), ', '.join(fmt.OWNER_PROVS)))
    return e


def _age(at, now, what):
    if at > now + SKEW:
        raise RunRefused('%s is in the future (%s)' % (what, at.isoformat()))
    if now - at > MAX_AGE:
        raise RunRefused('%s is more than %d h old (%s): ask the owner again'
                         % (what, MAX_AGE.total_seconds() // 3600, at.isoformat()))


def used(texts, anchor, trigger):
    """The id of a live runner entry with this anchor or this trigger, or None."""
    for _, text in texts:
        for rid, block in fmt.split_entries(text)[1]:
            e = fmt.parse_block(block)[0]
            if not e or e.get('retired') or e.get('prov') != PROV:
                continue
            m = SOURCE_RE.match((e.get('Source') or '').strip())
            if m and (m.group(1) == anchor or m.group(3) == trigger):
                return rid
    return None


# --- The order --------------------------------------------------------------------------------

def hub_order(op, qid, line, hub, texts, now):
    """The Order of a hub question; RunRefused unless the owner chose the run line on it."""
    if 'hub-answer' not in op.trigger:
        raise RunRefused('%s does not take a hub answer (its triggers: %s)' % (op.id, ', '.join(op.trigger)))
    if not op.reversible:
        raise RunRefused('%s is not reversible: a hub answer never starts it' % op.id)
    if hub is None:
        raise RunRefused('no hub to read %s from' % qid)
    try:
        items = hub.listing('questions', status='logged', role=hubclient.AGENT)
    except (hubserve.ServeError, OSError, ValueError) as e:
        raise RunRefused('cannot read %s from the hub: %s' % (qid, e))
    q = next((d for d in items if isinstance(d, dict) and d.get('id') == qid), None)
    if q is None:
        raise RunRefused('%s is not a logged hub question: the owner answers it, the PM logs the '
                         'answer in the ledger and on the hub, then it runs' % qid)
    ans = q.get('answer') if isinstance(q.get('answer'), dict) else {}
    choice = ans.get('choice')
    if not isinstance(choice, int) or isinstance(choice, bool):
        raise RunRefused('%s: the owner did not choose an option (choice %r)' % (qid, choice))
    if ans.get('label') != line:
        raise RunRefused('%s: the chosen option is %r, not the run line %r'
                         % (qid, ans.get('label'), line))
    q_repo = (q.get('repo') or '').strip().lower() if isinstance(q.get('repo'), str) else ''
    if q_repo and op.repo != '*' and q_repo != op.repo:
        raise RunRefused('%s is a question of repo %s; %s acts on %s' % (qid, q_repo, op.id, op.repo))
    try:
        at = datetime.datetime.fromisoformat(ans.get('at') or '')
    except (TypeError, ValueError):
        raise RunRefused('%s: the answer time %r cannot be read' % (qid, ans.get('at')))
    if at.tzinfo is None:
        raise RunRefused('%s: the answer time %r has no offset' % (qid, ans.get('at')))
    _age(at, now, '%s\'s answer' % qid)
    ruling = q.get('ruling').strip() if isinstance(q.get('ruling'), str) else ''
    if not fmt.is_id(ruling):
        raise RunRefused('%s: its ruling %r is not a ledger id' % (qid, ruling))
    e = owner_entry(texts, ruling)
    if not re.search(r'\b%s\b' % re.escape(qid), e.get('Source') or ''):
        raise RunRefused("%s: its ruling %s's Source does not name %s" % (qid, ruling, qid))
    return Order(HUB, qid, ruling)


def owner_order(op, rid, line, texts, now):
    """The Order of an owner ledger entry; RunRefused unless its Decides is the run line."""
    if 'owner-session' not in op.trigger:
        raise RunRefused('%s does not take an owner session order (its triggers: %s)'
                         % (op.id, ', '.join(op.trigger)))
    e = owner_entry(texts, rid)
    if (e.get('Decides') or '').strip() != line:
        raise RunRefused('%s decides %r, not the run line %r' % (rid, e.get('Decides'), line))
    if op.repo != '*' and e.get('repo') != op.repo:
        raise RunRefused('%s is a ruling of repo %s; %s acts on %s' % (rid, e.get('repo'), op.id, op.repo))
    try:
        at = datetime.datetime.strptime(e.get('when') or '', LEDGER_WHEN).replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        raise RunRefused('%s: its time %r cannot be read' % (rid, e.get('when')))
    _age(at, now, rid)
    return Order(OWNER, rid, rid)


# --- The preconditions ------------------------------------------------------------------------

def file_exists(pre):
    path = pre.get('path') or ''
    if not os.path.isabs(path):
        return '%s is not an absolute path' % path
    if not os.path.isfile(path):
        return '%s is not a file' % path
    return None


def branch_landed(pre):
    """None when the branch's tip has landed (kitlib.landed.look). A branch that is neither a
    local nor a remote-tracking branch any more fails closed: its tip cannot be told."""
    from kitlib import landed
    from kitlib.config import ConfigError, load
    from kitlib.git import refs
    root, branch = pre.get('repo_path') or '', pre.get('branch') or ''
    if not os.path.isdir(root):
        return '%s is not a folder' % root
    try:
        remote = load(root).remote
    except ConfigError as e:
        return 'cannot read the kit configuration of %s: %s' % (root, e)
    sha = ''
    for ref in ('refs/heads/%s' % branch, 'refs/remotes/%s/%s' % (remote, branch)):
        sha, why = refs.resolve(ref, root, known=True)
        if sha is None:
            return 'cannot read %s in %s: %s' % (ref, root, why)
        if sha:
            break
    if not sha:
        return ('%s is no branch in %s (neither local nor %s/%s): a branch deleted after it landed '
                'cannot be told from one never pushed' % (branch, root, remote, branch))
    out = landed.look(root, sha)
    if out.code != landed.LANDED:
        return '%s (%s) has not landed: %s' % (branch, sha[:12], (out.lines or ['?'])[-1])
    return None


def checklist_line(pre):
    """None when a line of the PM start checklist that is not a failed check holds the text."""
    from kitlib import pmstart
    from kitlib.config import ConfigError
    text = pre.get('text') or ''
    try:
        cfg = pmstart.load_config()
    except ConfigError as e:
        return 'cannot read the PM start config: %s' % e
    if cfg is None:
        return 'no pm_start config: the PM start checklist cannot be read'
    for ln in pmstart.checklist(cfg).split('\n'):
        if ln.startswith('- ') and not ln[2:].startswith('could not check') and text in ln:
            return None
    return 'no PM start checklist line holds %r' % text


CHECKS = {'file-exists': file_exists, 'branch-landed': branch_landed, 'checklist-line': checklist_line}


def preconditions(pres, checks):
    """RunRefused naming the first precondition that does not hold."""
    for j, pre in enumerate(pres):
        check = checks.get(pre.get('kind'))
        why = check(pre) if check else 'unknown kind %r' % pre.get('kind')
        if why:
            raise RunRefused('precondition %d (%s) does not hold: %s' % (j, pre.get('kind'), why))


# --- The run ----------------------------------------------------------------------------------

def _claim(op, order, line, found, cfg, now, lock_wait, pause):
    """Add the run's ledger entry under the runner lock; its id. RunRefused when the trigger or
    its anchor already ran, or the ledger refuses the entry."""
    col, path, split = target(found, op.repo)
    folder = os.path.dirname(os.path.abspath(path))      # every ledger file's folder: one lock
    if order.kind == HUB:
        answer = "the owner chose this run on %s, logged as %s" % (order.trigger, order.anchor)
    else:
        answer = "the owner's ruling %s orders this run" % order.anchor
    try:
        with writelock.locked(os.path.join(folder, LOCK_NAME), LOCK_PREFIX, lock_wait,
                              what='delegated run'):
            prior = used(ledger_texts(found), order.anchor, order.trigger)
            if prior:
                raise RunRefused('%s already ran on %s (%s): each order runs once'
                                 % (order.trigger, order.anchor, prior))
            if pause:
                pause()
            others = ()
            if split:
                others, whys = lfiles.others(found, path)
                if whys:
                    raise RunRefused('cannot read the other ledger files: %s' % '; '.join(whys))
            added = lwrite.add(path, col, PROV, 'Run delegated operation %s (%s)?' % (op.id, op.what),
                               answer, line, 'under %s: %s %s' % (order.anchor, op.id, order.trigger),
                               when=now.strftime(LEDGER_WHEN), repos=cfg.get('ledger_repos'),
                               split=split, others=others)
    except writelock.WriteLockError as e:
        raise RunRefused('cannot take the runner lock: %s' % e)
    except (LedgerRefused, LedgerUsage) as e:
        raise RunRefused('the ledger refused the run entry: %s' % e)
    return added.id


def run(op, pairs, trigger, cfg, hub=None, now=None, spawn=procspawn.run, checks=None,
        environ=None, kit_root=KIT_ROOT, pause=None, lock_wait=LOCK_WAIT):
    """Run the Operation `op` with the arguments `pairs` [(name, value)] on `trigger`; the module
    docstring gives the order. `cfg`: the machine config (kitlib.config.user.user_config). `hub`:
    a kitlib.hubsite.client.Client for a question trigger. `spawn`, `checks` (the precondition
    checks by kind, default CHECKS), `environ`, `now` (aware UTC) and `pause` (called between the
    use-once scan and the ledger add) are for tests. Result, or RunRefused with nothing done."""
    now = _utc(now)
    environ = dict(os.environ if environ is None else environ)
    kind = trigger_kind(trigger)
    values = fill_args(op, pairs)
    line = run_line(op, values)
    argv, pres = filled(op, values, kit_root)
    exe = program(argv[0], path_dirs(environ))
    found = lfiles.of_config(cfg)
    if found is None:
        raise RunRefused('no ledger_dir or ledger_path in the machine config: a run is logged first')
    target(found, op.repo)
    texts = ledger_texts(found)
    if kind == HUB:
        order = hub_order(op, trigger, line, hub, texts, now)
    else:
        order = owner_order(op, trigger, line, texts, now)
    preconditions(pres, CHECKS if checks is None else checks)
    rid = _claim(op, order, line, found, cfg, now, lock_wait, pause)
    full = [exe] + argv[1:]
    try:
        done = spawn(full, executable=exe, cwd=kit_root, env=child_env(environ), shell=False,
                     stdin=subprocess.DEVNULL)
    except OSError as e:
        return Result(rid, full, None, 'could not start %s: %s' % (exe, e))
    return Result(rid, full, done.returncode, None)
