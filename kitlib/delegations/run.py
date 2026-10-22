"""Run one operation of the owner's delegation list (docs/delegations.md "Running").

`run(op, pairs, trigger, cfg, ...)` -> Result(entry, argv, returncode, error), or RunRefused with
nothing run and nothing written. `request(op, pairs, cfg, hub, title, ...)` asks the owner on the
hub to approve one run: steps 2 and 3 and the hub approval's scope of step 4, then the request;
the new approval's id. Run in this order:

  1. the trigger: a hub question id (`Q-nnnn`, the op lists hub-answer and is reversible), an
     owner ledger id (the op lists owner-session) or a hub approval `hub:A-nnn` (the op lists
     hub-approval). The literal `session` and the standing triggers are refused.
  2. the arguments: exactly the declared ones, each passing schema.check_arg. They fill whole
     `{name}` tokens of the command and `{name}` inside precondition strings; `<kit>` becomes the
     kit folder. The filled command and precondition strings are checked again with
     schema.exclusion_hits.
  3. the program: argv[0] is an absolute `.exe` path or a bare name found as `<name>.exe` in an
     absolute folder of the machine and user PATH read from the registry, never the caller's
     PATH. The working folder is never searched; nothing else (a .bat, a .cmd, a
     script) is started, so no shell ever runs.
  4. the order: the run line `run D-nnn k=v ...` (arguments sorted by name) must be what the owner
     chose. A hub approval: kitlib.delegations.place.check passes every path the run names
     (on the Dev Drive, outside checkouts and owner-only places), no ledger entry records the
     approval, and the hub verifies it for this operation, its repo and the run line's SHA-256.
     A hub trigger: the logged question's chosen option label is the run line, chosen in
     the last MAX_AGE, and its ruling is a live owner ledger entry whose Source names the
     question. An owner ledger id: a live owner entry, written in the last MAX_AGE, whose Decides
     is the run line.
  5. the preconditions: file-exists, branch-landed, checklist-line. A hub approval is then
     consumed on the hub, pinned to the version it verified at: of two racing runners one wins,
     and one that cannot consume it runs nothing and logs nothing.
  6. the claim, under the runner lock: no live runner entry (Source `under <anchor>: D-nnn
     <trigger>`) has this anchor or this trigger, then one ledger entry, provenance delegated,
     is added (not committed). Each trigger and each owner entry orders one run. A hub approval
     is first recorded as the owner's own entry (provenance selected, Source `hub:A-nnn`,
     Decides the run line: the owner signed it on the hub), and the run entry is `under` it.
  7. the child: the argv list with shell=False, stdin closed, in the kit folder (a hub
     approval's: in a new empty folder under the machine's temp folder, removed after if still
     empty). Its environment
     is only the FROM_CALLER names of the caller's and the registry's PATH, PATHEXT,
     PSModulePath and FROM_SYSTEM names (the system and profile folders).

Nothing here prints, parses arguments or exits (ARCHITECTURE.md, Layers); tools/delegated.py is
the entry point.
"""
import collections
import datetime
import os
import re
import subprocess

from kitlib.config import user as kuser
from kitlib.fs import writelock
from kitlib.hubsite import approvals
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import serve as hubserve
from kitlib.ledger import files as lfiles
from kitlib.ledger import fmt
from kitlib.ledger import read as lread
from kitlib.ledger import write as lwrite
from kitlib.ledger.errors import Refused as LedgerRefused
from kitlib.ledger.errors import Usage as LedgerUsage
from kitlib.proc import spawn as procspawn

from . import place, schema

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_AGE = datetime.timedelta(hours=24)       # how old an owner's order may be
SKEW = datetime.timedelta(minutes=5)         # how far in the future a stamp may be (clock drift)
HUB, OWNER, APPROVAL = 'hub', 'owner', 'approval'
SESSION = 'session'
Q_ID = re.compile(r'Q-[0-9]+')
# A hub approval: kitlib.guard.ownercommits.APPROVAL_RE, copied (the guard is not imported here).
APPROVAL_RE = re.compile(r'hub:(A-(?:(?!000)[0-9]{3}|[1-9][0-9]{3,}))\Z')
APPROVAL_TRIGGER = 'hub-approval'
CWD_PREFIX = 'kit-delegated-'
# The provenance of the entry that records a verified hub approval: the owner chose it on the hub.
APPROVAL_PROV = 'selected'
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
    """HUB for a question id, OWNER for a ledger id, APPROVAL for `hub:A-nnn`; RunRefused for
    anything else."""
    t = trigger or ''
    if APPROVAL_RE.match(t):
        return APPROVAL
    if t.startswith('hub:'):
        raise RunRefused('--approval takes hub:A-nnn, not %r' % t)
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

MACHINE_ENV = r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
USER_ENV = 'Environment'
VOLATILE_ENV = 'Volatile Environment'
NT_CURRENT = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion'
WIN_CURRENT = r'SOFTWARE\Microsoft\Windows\CurrentVersion'
PROFILE_LIST = NT_CURRENT + r'\ProfileList'
# The %NAME% values a registry value may use, read from the registry and never from the caller:
# name (upper case) -> (hive, key, value name). SYSTEMDRIVE is the drive of SYSTEMROOT; any other
# name is the user's, then the machine's, environment value of that name.
SYSTEM_NAMES = {
    'SYSTEMROOT': ('HKLM', NT_CURRENT, 'SystemRoot'),
    'WINDIR': ('HKLM', NT_CURRENT, 'SystemRoot'),
    'COMSPEC': ('HKLM', MACHINE_ENV, 'ComSpec'),
    'PROGRAMFILES': ('HKLM', WIN_CURRENT, 'ProgramFilesDir'),
    'PROGRAMFILES(X86)': ('HKLM', WIN_CURRENT, 'ProgramFilesDir (x86)'),
    'PROGRAMW6432': ('HKLM', WIN_CURRENT, 'ProgramW6432Dir'),
    'COMMONPROGRAMFILES': ('HKLM', WIN_CURRENT, 'CommonFilesDir'),
    'COMMONPROGRAMFILES(X86)': ('HKLM', WIN_CURRENT, 'CommonFilesDir (x86)'),
    'PROGRAMDATA': ('HKLM', PROFILE_LIST, 'ProgramData'),
    'ALLUSERSPROFILE': ('HKLM', PROFILE_LIST, 'ProgramData'),
    'PUBLIC': ('HKLM', PROFILE_LIST, 'Public'),
}
SYSTEM_NAMES.update((n.upper(), ('HKCU', VOLATILE_ENV, n)) for n in (
    'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'USERNAME', 'USERDOMAIN'))
EXPAND_DEPTH = 8
VAR_RE = re.compile(r'%([^%]+)%')
# The child's system and profile folders, filled from the registry as a %NAME% is (_named), never
# from the caller: git finds its global config through the profile names, PowerShell its modules
# through ProgramFiles. A name with no registry value is left out.
FROM_SYSTEM = ('SystemRoot', 'windir', 'SystemDrive', 'ComSpec', 'ProgramFiles',
               'ProgramFiles(x86)', 'ProgramW6432', 'CommonProgramFiles', 'CommonProgramFiles(x86)',
               'ProgramData', 'ALLUSERSPROFILE', 'PUBLIC', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
               'APPDATA', 'LOCALAPPDATA')
# The child's variables taken from the caller as they are (docs/delegations.md "Known limits").
# PATH, PATHEXT, PSModulePath and FROM_SYSTEM come from the registry (registry_env). Nothing else
# reaches the child.
FROM_CALLER = ('USERNAME', 'USERDOMAIN', 'COMPUTERNAME', 'TEMP', 'TMP', 'NUMBER_OF_PROCESSORS',
               'PROCESSOR_ARCHITECTURE', 'OS')


def read_registry(hive, key, name):
    """`(value, expandable)` of the string value `name` of `key` under `hive` ('HKLM' or 'HKCU',
    the 64-bit view), or None when there is none or it is not a string. Under the kit's tests
    (kitlib.config.user.fenced) the registry is not read: None."""
    if kuser.fenced():
        return None
    try:
        import winreg
    except ImportError:
        return None
    root = winreg.HKEY_LOCAL_MACHINE if hive == 'HKLM' else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(root, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, kind = winreg.QueryValueEx(k, name)
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        return None
    return value, kind == winreg.REG_EXPAND_SZ


def _named(name, read, depth):
    upper = name.upper()
    if upper == 'SYSTEMDRIVE':
        root = _named('SystemRoot', read, depth)
        return (os.path.splitdrive(root)[0] or None) if root else None
    if upper in SYSTEM_NAMES:
        got = read(*SYSTEM_NAMES[upper])
    else:
        got = read('HKCU', USER_ENV, name) or read('HKLM', MACHINE_ENV, name)
    if got is None:
        return None
    value, expandable = got
    return expand(value, read, depth + 1) if expandable else value


def expand(text, read, depth=0):
    """`text` with each %NAME% filled from the registry (SYSTEM_NAMES, then the user's and the
    machine's environment values), never from the caller's environment. None when a name has no
    value, or the values nest past EXPAND_DEPTH."""
    if depth > EXPAND_DEPTH:
        return None
    missing = []

    def fill(m):
        v = _named(m.group(1), read, depth)
        if v is None:
            missing.append(m.group(1))
            return m.group(0)
        return v
    out = VAR_RE.sub(fill, text)
    return None if missing else out


def _entries(got, read):
    if got is None:
        return []
    value, expandable = got
    out = []
    for part in value.split(os.pathsep):
        part = expand(part, read) if expandable else part
        if part is not None and part.strip():
            out.append(part)
    return out


def registry_env(read=None):
    """The child's PATH, PATHEXT, PSModulePath and FROM_SYSTEM names, from the registry only. PATH
    and PSModulePath are the machine's value then the user's (MACHINE_ENV, USER_ENV); PATHEXT is
    the user's, or else the machine's; a FROM_SYSTEM name is filled as a %NAME% is. An entry whose
    %NAME% has no registry value is dropped, and a name with no value is left out. `read` is
    read_registry's stand-in for tests."""
    read = read or read_registry
    out = {}
    for name in FROM_SYSTEM:
        value = _named(name, read, 0)
        if value:
            out[name] = value
    for name, value in (('PATH', 'Path'), ('PSModulePath', 'PSModulePath')):
        parts = (_entries(read('HKLM', MACHINE_ENV, value), read)
                 + _entries(read('HKCU', USER_ENV, value), read))
        if parts:
            out[name] = os.pathsep.join(parts)
    ext = (_entries(read('HKCU', USER_ENV, 'PATHEXT'), read)
           or _entries(read('HKLM', MACHINE_ENV, 'PATHEXT'), read))
    if ext:
        out['PATHEXT'] = os.pathsep.join(ext)
    return out


def path_dirs(system):
    """The folders of the registry PATH, `system['PATH']` (registry_env), quotes dropped."""
    return [d.strip().strip('"') for d in system.get('PATH', '').split(os.pathsep)]


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


def child_env(environ, system):
    """The child's environment: the FROM_CALLER names of the caller's `environ`, and the
    registry's PATH, PATHEXT, PSModulePath and FROM_SYSTEM names (`system`, registry_env). Nothing
    else: no GIT_*, no PYTHON*, no PATH or profile folder of the caller's."""
    upper = {k.upper(): v for k, v in environ.items()}
    env = {n: upper[n.upper()] for n in FROM_CALLER if n.upper() in upper}
    env.update(system)
    return env


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
    """The id of a live runner entry with this anchor or this trigger, or None. A hub approval
    (`trigger` hub:A-nnn): also of a live entry whose Source is that approval (its record)."""
    for _, text in texts:
        for rid, block in fmt.split_entries(text)[1]:
            e = fmt.parse_block(block)[0]
            if not e or e.get('retired'):
                continue
            if (APPROVAL_RE.match(trigger or '')
                    and (e.get('Source') or '').strip() == trigger):
                return rid
            if e.get('prov') != PROV:
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


def approval_scope(op, approval, values, tokens, cfg, kit_root, home, environ):
    """RunRefused unless `op` lists hub-approval and place.check passes every path the run names
    (`values` the filled arguments, `tokens` the filled command after its program)."""
    if APPROVAL_TRIGGER not in op.trigger:
        raise RunRefused('%s does not list %s among its triggers (its triggers: %s)'
                         % (op.id, APPROVAL_TRIGGER, ', '.join(op.trigger)))
    whys = place.check(values, tokens, cfg, kit_root, home=home, environ=environ)
    if whys:
        raise RunRefused('%s on %s would act where no approved run may: %s'
                         % (op.id, approval, '; '.join(whys)))


def approval_order(op, approval, line, hub, texts):
    """(Order, version) of a hub approval `hub:A-nnn`: no ledger entry records it, and the hub
    verifies it for `op`, its repo and the run line's SHA-256 at that version. Fails closed: no
    hub, or one that cannot answer, refuses."""
    prior = used(texts, None, approval)
    if prior:
        raise RunRefused('%s was used already by %s' % (approval, prior))
    if hub is None:
        raise RunRefused('no hub to verify %s on' % approval)
    doc_id = APPROVAL_RE.match(approval).group(1)
    ok, why, version = approvals.verify_op(hub, doc_id, op.id, op.repo,
                                           approvals.line_sha256(line))
    if not ok:
        if why.startswith(approvals.WHY_SERVER.split('%')[0]):
            raise RunRefused('the hub cannot be reached: %s; nothing was run' % why)
        raise RunRefused('the hub refused %s: %s' % (approval, why))
    return Order(APPROVAL, approval, None), version


def consume_approval(hub, order, line, version):
    """Mark the approval used on the hub at the version it verified at; RunRefused when the hub
    does not answer that it is consumed."""
    doc_id = APPROVAL_RE.match(order.trigger).group(1)
    try:
        got = approvals.consume_op(hub, doc_id, approvals.line_sha256(line), version)
    except Exception as e:      # fail closed: an unrecorded use runs nothing
        raise RunRefused('the hub did not record the use of %s: %s; nothing was run'
                         % (order.trigger, str(e) or type(e).__name__))
    status = got.get('status') if isinstance(got, dict) else None
    if status != approvals.CONSUMED:
        raise RunRefused('the hub did not record the use of %s: it answered status %r; nothing '
                         'was run' % (order.trigger, status))


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
    elif order.kind == APPROVAL:
        answer = None                   # it names the approval's record, written below
    else:
        answer = "the owner's ruling %s orders this run" % order.anchor
    try:
        with writelock.locked(os.path.join(folder, LOCK_NAME), LOCK_PREFIX, lock_wait,
                              what='delegated run'):
            prior = used(ledger_texts(found), order.anchor, order.trigger)
            if prior:
                raise RunRefused('%s already ran on %s (%s): each order runs once'
                                 % (order.trigger, order.anchor or order.trigger, prior))
            if pause:
                pause()
            others = ()
            if split:
                others, whys = lfiles.others(found, path)
                if whys:
                    raise RunRefused('cannot read the other ledger files: %s' % '; '.join(whys))
            when, repos = now.strftime(LEDGER_WHEN), cfg.get('ledger_repos')
            q = 'Run delegated operation %s (%s)?' % (op.id, op.what)
            anchor = order.anchor
            if order.kind == APPROVAL:
                anchor = lwrite.add(
                    path, col, APPROVAL_PROV, q,
                    'approved on the hub as %s: the hub verified the owner\'s signed approval '
                    'for %s on %s and this run line, and recorded its use' % (
                        order.trigger, op.id, op.repo),
                    line, order.trigger, when=when, repos=repos, split=split,
                    others=others).id
                answer = "the owner approved this run on the hub as %s, logged as %s" % (
                    order.trigger, anchor)
            added = lwrite.add(path, col, PROV, q, answer, line,
                               'under %s: %s %s' % (anchor, op.id, order.trigger),
                               when=when, repos=repos, split=split, others=others)
    except writelock.WriteLockError as e:
        raise RunRefused('cannot take the runner lock: %s' % e)
    except (LedgerRefused, LedgerUsage) as e:
        raise RunRefused('the ledger refused the run entry: %s' % e)
    return added.id


def _empty_cwd(make_cwd, home):
    try:
        return make_cwd() if make_cwd else kuser.mkdtemp(CWD_PREFIX, home=home)
    except OSError as e:
        raise RunRefused('cannot make the empty working folder: %s' % e)


def _drop(folder):
    """Remove `folder` when it is still empty."""
    try:
        os.rmdir(folder)
    except OSError:
        pass


def request(op, pairs, cfg, hub, title, reason=None, environ=None, kit_root=KIT_ROOT,
            registry=None, home=None):
    """Ask the owner on `hub` to approve one run of `op` with `pairs`: the arguments, the
    program and the approval's scope (approval_scope) are checked as `run` checks them, then the
    request is added with the run line. The new approval's id, `A-nnn`; RunRefused with nothing
    asked."""
    environ = dict(os.environ if environ is None else environ)
    values = fill_args(op, pairs)
    line = run_line(op, values)
    argv, _ = filled(op, values, kit_root)
    program(argv[0], path_dirs(registry_env(registry)))
    approval_scope(op, 'a request', values, argv[1:], cfg, kit_root, home, environ)
    if hub is None:
        raise RunRefused('no hub to ask on')
    try:
        got = approvals.request_op(hub, op.id, op.repo, line, title, reason=reason, what=op.what)
    except (hubserve.ServeError, OSError, ValueError) as e:
        raise RunRefused('the hub did not take the request: %s' % (str(e) or type(e).__name__))
    rid = got.get('id') if isinstance(got, dict) else None
    if not isinstance(rid, str) or not rid:
        raise RunRefused('the hub answered without the new approval\'s id: %r' % (got,))
    return rid


def run(op, pairs, trigger, cfg, hub=None, now=None, spawn=procspawn.run, checks=None,
        environ=None, kit_root=KIT_ROOT, pause=None, lock_wait=LOCK_WAIT, registry=None,
        home=None, make_cwd=None):
    """Run the Operation `op` with the arguments `pairs` [(name, value)] on `trigger`; the module
    docstring gives the order. `cfg`: the machine config (kitlib.config.user.user_config). `hub`:
    a kitlib.hubsite.client.Client for a question trigger or a hub approval. `spawn`, `checks`
    (the precondition checks by kind, default CHECKS), `environ` (the caller's), `registry`
    (read_registry's stand-in), `now` (aware UTC), `pause` (called between the use-once scan and
    the ledger add), `home` (the home folder place.check reads, default the user's) and
    `make_cwd` (makes an approval run's empty working folder) are for tests. Result, or
    RunRefused with nothing done."""
    now = _utc(now)
    environ = dict(os.environ if environ is None else environ)
    kind = trigger_kind(trigger)
    values = fill_args(op, pairs)
    line = run_line(op, values)
    argv, pres = filled(op, values, kit_root)
    system = registry_env(registry)
    exe = program(argv[0], path_dirs(system))
    if kind == APPROVAL:
        approval_scope(op, trigger, values, argv[1:], cfg, kit_root, home, environ)
    found = lfiles.of_config(cfg)
    if found is None:
        raise RunRefused('no ledger_dir or ledger_path in the machine config: a run is logged first')
    target(found, op.repo)
    texts = ledger_texts(found)
    version = None
    if kind == APPROVAL:
        order, version = approval_order(op, trigger, line, hub, texts)
    elif kind == HUB:
        order = hub_order(op, trigger, line, hub, texts, now)
    else:
        order = owner_order(op, trigger, line, texts, now)
    preconditions(pres, CHECKS if checks is None else checks)
    cwd = kit_root
    if kind == APPROVAL:
        cwd = _empty_cwd(make_cwd, home)
        try:
            consume_approval(hub, order, line, version)
            rid = _claim(op, order, line, found, cfg, now, lock_wait, pause)
        except BaseException:
            _drop(cwd)
            raise
    else:
        rid = _claim(op, order, line, found, cfg, now, lock_wait, pause)
    full = [exe] + argv[1:]
    try:
        done = spawn(full, executable=exe, cwd=cwd, env=child_env(environ, system),
                     shell=False, stdin=subprocess.DEVNULL)
    except OSError as e:
        return Result(rid, full, None, 'could not start %s: %s' % (exe, e))
    finally:
        if cwd != kit_root:
            _drop(cwd)
    return Result(rid, full, done.returncode, None)
