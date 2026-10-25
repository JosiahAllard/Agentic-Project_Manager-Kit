"""The one-shot move of the machine config from the home folder into the PM repository
(docs/kit-json.md "Moving the machine config into the PM repo").

The pointer (kitlib.config.pointer) names the PM repository; the move writes its `.pm-kit` folder:

- config.json, tracked: the policy keys (POLICY), with the RELATIVE_KEYS paths that lie inside the
  PM repository written relative to it, forward slashes;
- local.json, git-ignored: the machine-local and personal keys (LOCAL) and the notes;
- .gitignore, tracked: GITIGNORE;
- owner-delegations.json (tracked), managed-settings.json and hub/* (git-ignored), byte for byte.

Dropped (DROPPED): ledger_refs, standing_delegation and daily_red_ruling (retired, user.RETIRED),
coordinator_start's old key (OLD_START: carried over as coordinator_start when that is absent),
and a pm_data that matches the pointer. A pm_data that does not match is a refusal. Never copied: NEVER_COPIED, named as files to delete.

survey() reads and plans and writes nothing; apply() writes what survey() planned and checks it
(verify()). Nothing here deletes, commits or pushes: lines() prints the owner's commands. Messages
name keys and files, never a key's value. Under the kit's tests the home folder is the caller's
and the pointer the test fence's (kitlib.config.pointer.read_raw).
"""
import collections
import fnmatch
import json
import os
import re
import tempfile

from kitlib import pmdata
from kitlib.coordinatorstart import LEGACY_KEY as OLD_START
from kitlib.git import cmd as gitcmd
from kitlib.git import refs, repo

from . import pointer, user
from .types import ConfigError, Obj, is_note, parse_json

# Every machine config key in exactly one of these (tests/config/test_move.py checks the union is
# user.SCHEMA plus user.RETIRED).
POLICY = ('ledger_path', 'ledger_dir', 'ledger_repos', 'pending_path',
          'guard_rules', 'guard_mark_roots', 'guard_agent_marks',
          'guard_mcp_tools', 'guard_mcp_prefix', 'pm_start', 'coordinator_start', 'pm_watch',
          'backlog_lint', 'machine_lock', 'temp_dir', 'agent_temp_sweep', 'machine_path_folders',
          'plugin')
LOCAL = ('owner_email', 'scrub_patterns', 'launcher', 'hub', 'dev_drive')
DROPPED = ('ledger_refs', OLD_START, 'pm_data', 'standing_delegation', 'daily_red_ruling')
# The policy keys written relative to the PM repository when they lie inside it.
RELATIVE_KEYS = ('ledger_dir', 'ledger_path', 'pending_path')

FOLDER = user.DIR_NAME
# The suffix of the move's own temp files (_write_new); git ignores them (GITIGNORE).
TMP_SUFFIX = '.move-tmp'
POLICY_FILE = user.FILE_NAME
LOCAL_FILE = 'local.json'
IGNORE_FILE = '.gitignore'
DELEGATIONS_FILE = 'owner-delegations.json'      # kitlib.delegations.load.FILE_NAME
MANAGED_FILE = 'managed-settings.json'           # kitlib.machinesetup.managed.OVERLAY
HUB_DIR = 'hub'                                  # kitlib.hubsite.auth.FOLDER_NAME
GITIGNORE = 'local.json\nhub/\nauto-tasks/\nmanaged-settings.json\n*.bak*\n*%s\n' % TMP_SUFFIX
NEVER_COPIED = ('*.bak*', 'hub_backlog_state*.json')
COMMIT_MESSAGE = 'Move the machine config into the PM repo'

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+')
FIX = ('change the value in the home config (a personal detail belongs in a local.json key, '
       'never in the tracked config.json), then run again')

OK, REFUSED = 0, 1

# One file the move writes: `rel` under FOLDER, `data` the bytes it gets, `source` the file it
# is copied from (None: rendered here), `tracked` whether git keeps it, `state` new, same or differs.
Target = collections.namedtuple('Target', 'rel data source tracked state')


class Plan(object):
    def __init__(self):
        self.home = self.source = self.pm = self.folder = None
        self.policy, self.local = {}, {}
        self.dropped = []          # (key, why)
        self.relative = []         # keys rewritten relative
        self.targets = []
        self.delete = []           # files never copied
        self.unknown = []          # files the move does not know
        self.leftovers = []        # home folders to remove after the switch
        self.refusals = []
        self.warnings = []


def _read(path):
    with open(path, 'rb') as f:
        return f.read()


def _render(obj):
    return (json.dumps(obj, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def _never(name):
    return any(fnmatch.fnmatch(name.lower(), p) for p in NEVER_COPIED)


def _strings(value):
    """Every string in a JSON value, object keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            for s in _strings(v):
                yield s
    elif isinstance(value, list):
        for v in value:
            for s in _strings(v):
                yield s


def _slashed(path):
    return os.path.normcase(os.path.normpath(path)).replace('\\', '/')


def profiles(home):
    """The profile folders a tracked value must not name: the home moved from, and the profile
    folder Windows reports (when it answers)."""
    out = [home]
    try:
        out.append(user.real_profile())
    except Exception:
        pass
    return sorted({_slashed(p) for p in out if p})


def personal(value, owner_email, folders):
    """The kind of personal detail in a JSON value ('an email address' or 'a profile path'), or
    None. Never the detail itself."""
    for s in _strings(value):
        if EMAIL.search(s) or (owner_email and owner_email.lower() in s.lower()):
            return 'an email address'
        low = s.replace('\\', '/').lower()
        if any(f in low for f in folders):
            return 'a profile path'
    return None


def split(data, pm, plan):
    """Fill plan.policy, plan.local and plan.dropped from the config object `data`."""
    for k, v in data.items():
        if is_note(k) or k in LOCAL:
            plan.local[k] = v
        elif k in POLICY:
            plan.policy[k] = v
        elif k in user.RETIRED:
            plan.dropped.append((k, 'retired'))
        elif k == OLD_START:
            if 'coordinator_start' in data:
                plan.dropped.append((k, 'coordinator_start is set, so this key was never read'))
            else:
                plan.policy['coordinator_start'] = v
                plan.dropped.append((k, 'carried over as coordinator_start'))
        elif k == 'pm_data':
            why = pm_data_mismatch(v, pm)
            if why:
                plan.refusals.append('pm_data does not match the pointer: %s; make them agree, '
                                     'then run again' % why)
            else:
                plan.dropped.append((k, 'matches the pointer'))
        else:
            plan.refusals.append('%s: a key the move does not know' % k)


def pm_data_mismatch(value, pm):
    """None when the pm_data value names the pointer's work tree, branch and remote; else why not,
    naming fields only. With a kit-pm-data.json record the branch and remote are the record's;
    without one the branch is the one the pointer is on, and the remote one the PM repo has."""
    try:
        s = pmdata.check_settings(value, 'pm_data')
    except pmdata.PmDataError:
        return 'it is not a valid pm_data value'
    if not repo.same_path(s['path'], pm):
        return 'its path is another folder'
    place = repo.classify(pm)
    try:
        record = pmdata.read(place.common) if place.common else None
    except pmdata.PmDataError:
        return 'the PM data record cannot be read'
    if record is not None:
        if s['branch'] != record.branch:
            return 'its branch is not the recorded data branch'
        if s['remote'] != record.remote:
            return 'its remote is not the recorded remote'
        return None
    branch, _why = refs.current_branch(pm)
    if s['branch'] != branch:
        return 'its branch is not the branch the pointer is on'
    if gitcmd.call(['config', '--get', 'remote.%s.url' % s['remote']], pm).rc != 0:
        return 'its remote is not a remote of the PM repo'
    return None


def relativise(plan):
    for k in RELATIVE_KEYS:
        v = plan.policy.get(k)
        if isinstance(v, str) and os.path.isabs(v) and repo.inside(v, plan.pm) \
                and not repo.same_path(v, plan.pm):
            plan.policy[k] = os.path.relpath(os.path.normpath(v), plan.pm).replace('\\', '/')
            plan.relative.append(k)


def resolved(policy, pm):
    """`policy` with its relative RELATIVE_KEYS paths made absolute against `pm` again."""
    out = dict(policy)
    for k in RELATIVE_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v and not os.path.isabs(v):
            out[k] = os.path.normpath(os.path.join(pm, v))
    return out


def _target(plan, rel, data, source, tracked):
    full = os.path.join(plan.folder, *rel.split('/'))
    if os.path.lexists(full):
        try:
            state = 'same' if os.path.isfile(full) and _read(full) == data else 'differs'
        except OSError:
            state = 'differs'
    else:
        state = 'new'
    t = Target(rel, data, source, tracked, state)
    plan.targets.append(t)
    if state == 'differs':
        plan.refusals.append('%s exists and differs from what the move would write: compare it '
                             'with %s, then remove or fix it' % (full, source or 'the home config'))
    return t


def _home_files(folder):
    """Every file under `folder`, as (relative path with /, full path)."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            out.append((os.path.relpath(full, folder).replace('\\', '/'), full))
    return out


def dirty(pm):
    """(the paths under FOLDER that git reports changed or untracked, relative to FOLDER; None
    when git could not tell, and why)."""
    out, why = gitcmd.run(['status', '--porcelain=v1', '-z', '--untracked-files=all', '--',
                           FOLDER], pm)
    if out is None:
        return None, why
    paths, items, i = [], out.split(b'\0'), 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        status, path = item[:2], item[3:].decode('utf-8', 'replace')
        if status[:1] in (b'R', b'C'):
            i += 1                                  # the rename's source follows
        prefix = FOLDER + '/'
        paths.append(path[len(prefix):] if path.startswith(prefix) else path)
    return paths, None


def survey(home=None):
    """The Plan of the move: what it would write, drop, leave and refuse. Writes nothing."""
    plan = Plan()
    plan.home = home = home or user.home_dir()
    ptr = pointer.read()
    if ptr.state != pointer.OK:
        plan.refusals.append('no PM repo pointer (%s): set it first with machine_setup.py --pm-repo '
                             '(docs/machine-setup.md "PM repo pointer")'
                             % (ptr.error or 'unset'))
        return plan
    plan.pm = os.path.normpath(ptr.path)
    plan.folder = os.path.join(plan.pm, FOLDER)
    loc = user.home_location(home)
    if loc.error:
        plan.refusals.append(loc.error)
        return plan
    if loc.state == user.NONE:
        plan.refusals.append('no machine config at %s: nothing to move' % loc.new)
        return plan
    plan.source = loc.path
    try:
        with open(loc.path, encoding='utf-8-sig') as f:
            data = parse_json(f.read(), loc.path)
    except (ConfigError, OSError, UnicodeDecodeError) as e:
        plan.refusals.append('the machine config cannot be read: %s' % e)
        return plan
    if not isinstance(data, dict):
        plan.refusals.append('the machine config %s is not a JSON object' % loc.path)
        return plan
    try:
        user.user_config(location=loc)
    except ConfigError:
        # Key names only: a schema message may quote the value.
        plan.refusals.append('the machine config fails its schema in key(s) %s: fix them '
                             '(docs/kit-json.md "Machine config keys"), then run again'
                             % (', '.join(user.failing_keys(data)) or 'unknown'))
        return plan
    split(data, plan.pm, plan)
    relativise(plan)
    email = data.get('owner_email') if isinstance(data.get('owner_email'), str) else None
    folders = profiles(home)
    for k, v in plan.policy.items():
        kind = personal(v, email, folders)
        if kind:
            plan.refusals.append('%s key %s holds %s: %s' % (POLICY_FILE, k, kind, FIX))
    patterns = []
    for p in data.get('scrub_patterns') or ():
        try:
            patterns.append(re.compile(p, re.I))
        except re.error:
            pass
    for k, v in plan.policy.items():
        if any(rx.search(s) for s in _strings(v) for rx in patterns):
            plan.warnings.append('%s key %s matches a scrub pattern: check it holds no personal '
                                 'detail before you commit' % (POLICY_FILE, k))

    _target(plan, IGNORE_FILE, GITIGNORE.encode('utf-8'), None, True)
    _target(plan, POLICY_FILE, _render(plan.policy), None, True)
    if plan.local:
        _target(plan, LOCAL_FILE, _render(plan.local), None, False)

    folders_home = [os.path.dirname(loc.new), os.path.dirname(loc.old)]
    known = {os.path.normcase(loc.path)}
    copies = {os.path.normcase(os.path.join(folders_home[0], DELEGATIONS_FILE)): (DELEGATIONS_FILE, True),
              os.path.normcase(os.path.join(os.path.dirname(loc.path), MANAGED_FILE)): (MANAGED_FILE, False)}
    hub = os.path.normcase(os.path.join(folders_home[0], HUB_DIR))
    for folder in folders_home:
        if not os.path.isdir(folder):
            continue
        plan.leftovers.append(folder)
        for rel, full in _home_files(folder):
            key = os.path.normcase(full)
            if _never(os.path.basename(full)):
                plan.delete.append(full)
            elif key in known:
                continue
            elif key in copies:
                name, tracked = copies[key]
                try:
                    data_bytes = _read(full)
                except OSError as e:
                    plan.refusals.append('cannot read %s: %s' % (full, e))
                    continue
                if name == DELEGATIONS_FILE:
                    try:
                        obj = json.loads(data_bytes.decode('utf-8-sig'))
                    except (ValueError, UnicodeDecodeError):
                        plan.refusals.append('%s is not valid JSON: fix it, then run again' % full)
                    else:
                        kind = personal(obj, email, folders)
                        if kind:
                            plan.refusals.append('%s holds %s: %s' % (DELEGATIONS_FILE, kind, FIX))
                        if any(rx.search(t) for t in _strings(obj) for rx in patterns):
                            plan.warnings.append('%s matches a scrub pattern: check it holds no '
                                                 'personal detail before you commit'
                                                 % DELEGATIONS_FILE)
                _target(plan, name, data_bytes, full, tracked)
            elif key.startswith(hub + os.sep):
                try:
                    data_bytes = _read(full)
                except OSError as e:
                    plan.refusals.append('cannot read %s: %s' % (full, e))
                    continue
                _target(plan, HUB_DIR + '/' + os.path.relpath(full, os.path.join(folders_home[0], HUB_DIR)).replace('\\', '/'),
                        data_bytes, full, False)
            else:
                plan.unknown.append(full)

    paths, why = dirty(plan.pm)
    if paths is None:
        plan.refusals.append('cannot tell whether %s is clean: %s' % (plan.folder, why))
    else:
        same = {t.rel for t in plan.targets if t.state == 'same'}
        others = [p for p in paths if p not in same]
        if others:
            plan.refusals.append('%s has uncommitted changes (%s): commit or remove them, then run '
                                 'again' % (plan.folder, ', '.join(sorted(others))))
    return plan


def _write_new(full, data):
    """Write `data` to `full`, which must not exist: a new temp file beside it (named
    .<name>.*TMP_SUFFIX, which GITIGNORE ignores), then a rename that fails when the target
    appeared meanwhile. On any failure the temp file is removed."""
    os.makedirs(os.path.dirname(full), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.%s.' % os.path.basename(full), suffix=TMP_SUFFIX,
                               dir=os.path.dirname(full))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.rename(tmp, full)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def ignored(pm, rels):
    """The paths of `rels` (relative to FOLDER) that git does not ignore in `pm`; None and why
    when git could not tell."""
    paths = [FOLDER + '/' + r for r in rels]
    r = gitcmd.call(['check-ignore', '--'] + paths, pm)
    if r.rc not in (0, 1):
        return None, r.err or 'git check-ignore exited %s' % r.rc
    got = {line.strip() for line in gitcmd.text(r.out).splitlines() if line.strip()}
    return [p for p, full in zip(rels, paths) if full not in got], None


def apply(plan):
    """Write the plan's new files, then verify(). Returns the lines of what was done or refused.
    Nothing is written when the plan has a refusal."""
    if plan.refusals:
        return []
    out = []
    try:
        return _apply(plan, out)
    except OSError as e:
        plan.refusals.append('cannot write %s: %s; the files listed as wrote are in place, and a '
                             'rerun resumes (they show as same)'
                             % (e.filename or 'a file', e.strerror or type(e).__name__))
        return out


def _apply(plan, out):
    by_rel = {t.rel: t for t in plan.targets}
    gi = by_rel[IGNORE_FILE]
    if gi.state == 'new':
        _write_new(os.path.join(plan.folder, IGNORE_FILE), gi.data)
        out.append('wrote   %s' % IGNORE_FILE)
    private = [t.rel for t in plan.targets if not t.tracked]
    probe = private or [LOCAL_FILE, HUB_DIR + '/probe']
    missed, why = ignored(plan.pm, probe)
    if missed is None or missed:
        plan.refusals.append('git does not ignore %s in %s: %s; nothing secret was written'
                             % (', '.join(missed or probe), plan.pm, why or 'fix the .gitignore'))
        return out
    for t in plan.targets:
        if t.state == 'new' and t.rel != IGNORE_FILE:
            _write_new(os.path.join(plan.folder, *t.rel.split('/')), t.data)
            out.append('wrote   %s' % t.rel)
    return out + verify(plan)


def verify(plan):
    """Check what apply() wrote: config.json and local.json parse, each key is in its file and
    the two together pass user.SCHEMA (relative paths resolved), every file is byte-equal to the
    plan, and git ignores every private file. Each failure is a refusal."""
    out, policy, local = [], None, {}
    for rel in (POLICY_FILE, LOCAL_FILE):
        full = os.path.join(plan.folder, rel)
        if rel == LOCAL_FILE and not os.path.exists(full):
            continue
        try:
            with open(full, encoding='utf-8') as f:
                obj = parse_json(f.read(), full)
        except (ConfigError, OSError, UnicodeDecodeError) as e:
            plan.refusals.append('%s does not parse: %s' % (full, e))
            continue
        if not isinstance(obj, dict):
            plan.refusals.append('%s is not a JSON object' % full)
            continue
        allowed = POLICY if rel == POLICY_FILE else LOCAL
        wrong = sorted(k for k in obj if k not in allowed and not (rel == LOCAL_FILE and is_note(k)))
        if wrong:
            plan.refusals.append('%s holds keys of the other file: %s' % (full, ', '.join(wrong)))
        if rel == POLICY_FILE:
            policy = obj
        else:
            local = obj
    if policy is not None:
        union = dict(local)
        union.update(resolved(policy, plan.pm))
        union = {k: v for k, v in union.items() if not is_note(k)}
        errs = []
        Obj(user.SCHEMA, what='machine config').check(union, 'the moved config', errs)
        errs = errs or user.ledger_errors(union)
        if errs:
            plan.refusals.append('the moved config fails the machine config schema: %s'
                                 % (', '.join(user.failing_keys(union)) or 'see the schema'))
        else:
            out.append('verify  %s and %s parse and pass the machine config schema'
                       % (POLICY_FILE, LOCAL_FILE))
    differ = []
    for t in plan.targets:
        full = os.path.join(plan.folder, *t.rel.split('/'))
        try:
            if _read(full) != t.data:
                differ.append(t.rel)
        except OSError:
            differ.append(t.rel)
    if differ:
        plan.refusals.append('not byte-equal to the plan: %s' % ', '.join(differ))
    else:
        out.append('verify  %d file(s) byte-equal to their source' % len(plan.targets))
    private = [t.rel for t in plan.targets if not t.tracked]
    if private:
        missed, why = ignored(plan.pm, private)
        if missed is None or missed:
            plan.refusals.append('git does not ignore %s: %s' % (', '.join(missed or private),
                                                                 why or 'fix the .gitignore'))
        else:
            out.append('verify  git ignores %s' % ', '.join(private))
    return out


def shown(path, home):
    """`path` as a line for reading names it: ~/... under `home`, else as it is."""
    if home and repo.inside(path, home) and not repo.same_path(path, home):
        return '~/' + os.path.relpath(path, home).replace('\\', '/')
    return path


def lines(plan):
    """What the move does, as the tool prints it: names and paths, never a value."""
    out = []
    if plan.source:
        out.append('source  %s' % shown(plan.source, plan.home))
    if plan.folder:
        out.append('target  %s' % plan.folder)
    if plan.source:
        out.append('policy  %s: %s' % (POLICY_FILE, ', '.join(plan.policy) or '(none)'))
        out.append('local   %s: %s' % (LOCAL_FILE, ', '.join(plan.local) or '(none)'))
        for k, why in plan.dropped:
            out.append('drop    %s (%s)' % (k, why))
        for k in plan.relative:
            out.append('relative %s: written relative to the PM repo' % k)
        for t in plan.targets:
            out.append('%-7s %s%s' % ('same' if t.state == 'same' else 'write', t.rel,
                                      '' if t.tracked else ' (git-ignored)'))
        for f in plan.delete:
            out.append('delete  %s (never copied: a backup or old hub state)' % shown(f, plan.home))
        for f in plan.unknown:
            out.append('NOTE    %s is not moved (the move does not know it): move it yourself '
                       'before removing the folder' % shown(f, plan.home))
    for w in plan.warnings:
        out.append('warn  %s' % w)
    return out


def commands(plan):
    """The owner's commands after a successful --apply: check, add, commit, and the folder
    removal once the kit reads the PM repo's config."""
    pm = plan.pm.replace('\\', '/')
    add = ['%s/%s' % (FOLDER, t.rel) for t in plan.targets if t.tracked]
    out = ['next    git -C "%s" status --short -- %s' % (pm, FOLDER),
           'next    git -C "%s" add %s' % (pm, ' '.join(add)),
           'next    git -C "%s" commit -m "%s"' % (pm, COMMIT_MESSAGE),
           'next    add to %s/.gitignore as a backstop: %s' % (
               pm, ', '.join('%s/%s' % (FOLDER, line) for line in GITIGNORE.split())),
           ]
    for folder in plan.leftovers:
        out.append('later   once the kit reads the PM repo\'s config: Remove-Item -Recurse -Force "%s"'
                   % folder)
    return out


def run(apply_it=False, home=None):
    """(exit code, lines) of a dry run, or of the move with `apply_it`."""
    plan = survey(home)
    done = apply(plan) if apply_it and not plan.refusals else []
    out = lines(plan) + done + ['REFUSE  %s' % r for r in plan.refusals]
    if plan.refusals:
        return REFUSED, out
    if apply_it:
        out += commands(plan)
    else:
        out.append('dry run: nothing written; run again with --apply to copy')
    return OK, out
