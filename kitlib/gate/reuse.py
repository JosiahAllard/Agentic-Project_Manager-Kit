"""Gate reuse: a per-step cache of gate results, read by a branch-gate rerun and by a landing's
merged-tree gate, a batch landing included (docs/gate.md "Gate reuse").

A step opts in with `"reuse": true`, or a group name: the steps of one group are reused together
or not at all. A step is not run again when a pass is stored for its key, and no fail or warn is,
within "landing".reuse_max_age_h hours. The key is the sha256 of the canonical JSON of:
  - the step's name, its argv and its cwd (the checkout's root written `<root>`, the kit's
    `<kit>`), and the sha256 of its program and of every argv element that names a file;
  - the hash of the gate config (gate_ok.config_hash, as the gate-OK record's);
  - the kit's identity: the hash of every file under the kit's kitlib/ and tools/, the
    interpreter's path and version (kit_identity; never the kit's commit);
  - the change: the sorted paths the gate selects by (a branch gate's changed paths, a
    landing's base..merge), since a step such as the kit's `kit_tests.py --select` picks its
    tests by the change without naming {base};
  - its inputs: every file git shows in the work tree (tracked, or untracked and not ignored)
    that matches its `globs` or its `content`.globs, `exclude` NOT subtracted, by sha256.
A file git ignores is never an input: a step that reads one must not ask for reuse.

Fail closed. Anything that raises means every step runs. Nothing is read or stored:
  - for a step that runs no command, a check step, a step without `globs` or `content`.globs, a
    step whose globs match no file git shows (a mistyped glob, or one naming only ignored
    files, would key it on its command alone), a step whose cmd, full_cmd, daily_cmd or
    merged_args names {base}, or whose argv holds the base commit;
  - for an input that is not a plain file (a link, a folder: a nested repository or submodule);
  - in a --full run, a branch gate's --only, or when "landing".reuse_max_age_h is 0;
  - when the change touches `.kit.json`, a config section file, the hook module, a path matching
    "landing".reuse_never, or a strict path (kitlib.gate.strict_defaults.STRICT_DEFAULTS);
  - in a landing: --fresh; a result record that cannot be read; a land record of a tip (it got
    past its locked checks and did not land); a gate record of a tip that did not pass; a landing
    tree (kitlib.gate.land.TREE_NAME) that is not clean.
A step's result is stored only when its key (inputs, kit, program, config) is the same after the
run as before it: PASS as pass, FAIL or WARN as fail; a skipped or unrun step stores nothing.
Storing or pruning that fails says one line and never fails the gate.

The same tree (same_tree). A branch's gate.py --full stores nothing, so a landing whose merge holds
the very tree the tip passed --full on would run every step again. In a one-branch landing that
landing_why lets reuse, the tip's gate-OK record serves a step the cache does not when: it is a
--full record (verify_full, mode full) of the tip, the merge's parents are exactly (base, tip), the
merge's tree is the record's tree, the record's base is the landing's base (--expect-main), its
gate config hash is the landing's, its kit (runner.kit_identity: commit, interpreter, clean) is the
kit gating the landing, and it ended within "landing".reuse_max_age_h hours. A step is taken from
it when it asks for reuse, is no check step, has no merged_args (its landing argv never ran at the
tip) and the record says it passed. Only in that mode, a step whose command names {base} or the
base commit, or that declares no input, is not barred: the tree is the same tree, and the base is
the same commit, so {base} and every tracked file are the ones the tip's run had (the history
differs only by the merge commit, and the opt-in already promises the step does not read git
state). Tracked content only: the landing tree lacks the author worktree's ignored and generated
files, and a step that declares no input is not checked for them, so a step whose result depends
on such a file must not set reuse. The result taken is the one of the step's --full command
(full_cmd where it has one), not of the landing's cmd. Such a step gets no key and is never
stored, so the relaxation never reaches the cache. Anything that raises means every step runs.

Entries live in `<git common dir>/kit-gate-cache/<key>/<YYYYmmddTHHMMSSZ>-<pid>-<hex8>.<pass|fail>.json`,
written to a temp file (TMP_RE) and renamed. Entries older than PRUNE_H hours are pruned, and so
is a temp file last written that long ago (a crash between the write and the rename left it; a
newer one may be another gate's write in flight). Only names matching ENTRY_RE or TMP_RE in key
folders (KEY_RE) are removed, and no link out of the folder is followed.
"""
import collections
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys

from kitlib.config import schema
from kitlib.config.load import CONFIG_NAME
from kitlib.gate import gitstatus, result, runner
from kitlib.gate import steps as S
from kitlib.gate.strict_defaults import STRICT_DEFAULTS
from kitlib.git import cmd, refs, repo
from kitlib.records import gate_ok

# reused: {step name: why}; lines to say; keep(results) -> lines, after the run (None: store nothing)
Plan = collections.namedtuple('Plan', 'reused lines keep', defaults=(None,))
NOTHING = Plan({}, [])
SKEW_S = 60         # an entry stored up to this far in the future (clock skew) still counts
PREFIX = 'gate: reuse: '
DIR_NAME = 'kit-gate-cache'
SCHEMA = 1
PRUNE_H = 168       # entries older than this are pruned (the largest reuse_max_age_h)
STAMP = '%Y%m%dT%H%M%SZ'
KEY_RE = re.compile(r'^[0-9a-f]{64}$')
ENTRY_RE = re.compile(r'^(\d{8}T\d{6}Z)-(\d+)-([0-9a-f]{8})\.(pass|fail)\.json$')
TMP = '.tmp-%d-%s'  # (pid, hex8): an entry being written, renamed to its ENTRY_RE name
TMP_RE = re.compile(r'^\.tmp-\d+-[0-9a-f]{8}$')
STORE_AS = {'PASS': 'pass', 'FAIL': 'fail', 'WARN': 'fail'}
CHUNK = 1 << 20
KIT_DIRS = ('kitlib', 'tools')


class NotCacheable(Exception):
    """One step's key cannot be made: it runs, and nothing is stored for it."""


class NoInputs(NotCacheable):
    """No file git shows is one of the step's inputs (step_key)."""


SameTree = collections.namedtuple('SameTree', 'record tip tree')


# ---------------------------------------------------------------- the gate-wide checks

def prior_records(root, tips):
    """The result records that name any of `tips`, read before a landing writes its own: a list
    of dicts, from the result folder and the old one (result.record_dirs: an older kit's red
    record still counts), or None when a folder cannot be read or there is no result folder
    (then nothing is reused)."""
    out = []
    try:
        for folder in result.record_dirs(root):
            names = os.listdir(folder) if os.path.isdir(folder) else []
            for n in sorted(names):
                if not n.endswith('.json') or n.startswith('.'):
                    continue
                rec, why = result.read_path(os.path.join(folder, n))
                if rec is None:
                    return None     # a record nobody can read might be the red one
                if rec.get('tip') in tips:
                    out.append(rec)
    except Exception:       # noqa: BLE001 - cannot tell: reuse nothing
        return None
    return out


def _group(spec):
    r = spec.get('reuse')
    if r is True:
        return '=' + spec['name']           # its own group ('=' never starts a step name)
    return r if isinstance(r, str) and r else None


def config_hits(cfg, changed):
    """The paths of `changed` that are the gate's own config under `cfg`: `.kit.json`, a config
    section file and the hook module."""
    own = [CONFIG_NAME] + [f for f in (cfg.section_file(n) for n in schema.SECTIONS) if f]
    hook = cfg.hook()
    if hook:
        own.append('%s/%s.py' % (cfg.tools_dir.replace('\\', '/').strip('/'), hook['module']))
    own = [n.replace('\\', '/').lower() for n in own]
    return [p for p in changed if p.replace('\\', '/').lower() in own]


def never_hits(cfg, changed):
    """The paths of `changed` reuse never crosses: the gate's config (config_hits) and
    "landing".reuse_never."""
    own = set(config_hits(cfg, changed))
    globs = cfg.section('landing')['reuse_never']
    return [p for p in changed if p in own or S._match(p, globs)]


def strict_hits(cfg, changed):
    """The paths of `changed` that match a strict path: STRICT_DEFAULTS (imported, never
    copied). `cfg` is not read: `.kit.json` has no key that adds a strict path."""
    return [p for p in changed if S._match(p, list(STRICT_DEFAULTS))]


def docs_only_change(cfg, gate, changed):
    """steps.docs_only under `gate` (the config the decision is read from: the branch's in a
    branch gate, main's in a landing), barring every path reuse never crosses (never_hits: the
    config files and the hook module) and every strict path (strict_hits)."""
    return S.docs_only(gate, changed, barred=never_hits(cfg, changed) + strict_hits(cfg, changed))


def _change_why(cfg, changed):
    if cfg.section('landing')['reuse_max_age_h'] <= 0:
        return '"landing".reuse_max_age_h is 0'
    hit = never_hits(cfg, changed)
    if hit:
        return 'the change touches %s' % ', '.join(hit[:5])
    hit = strict_hits(cfg, changed)
    if hit:
        return 'a strict path changes: %s' % ', '.join(hit[:5])
    return None


def landing_why(root, cfg, base, merge, prior, fresh, full):
    """Why a landing reads and stores nothing, or None."""
    if fresh:
        return '--fresh'
    if full:
        return '--full'
    if prior is None:
        return 'the result records cannot all be read'
    for r in prior:
        tip = str(r.get('tip'))[:10]
        if r.get('kind') == 'land':
            return ('%s at %s was landed before (%s) and did not land: its tree is gated again'
                    % (r.get('branch'), tip, r.get('verdict')))
        if r.get('kind') == 'gate' and r.get('verdict') != 'pass':
            return 'a gate of %s on %s ended %s' % (tip, r.get('branch'), r.get('verdict'))
    changed, why = runner.changed_between(root, base, merge)
    if changed is None:
        return 'git cannot list the merged paths: %s' % why
    why = _change_why(cfg, changed)
    if why:
        return why
    entries, why = gitstatus.status(root)
    if entries != []:
        return 'the landing tree is not clean%s' % (': %s' % why if why else '')
    return None


def _record_why(rec, cfg, base, tip, merge, root, now):
    """Why the --full record `rec` of `tip`, whose tree the merge holds, serves no step, or None."""
    parents, why = cmd.run_text(['rev-list', '--parents', '-n', '1', merge], root)
    if parents is None:
        return 'git cannot read the parents of %s: %s' % (merge[:10], why)
    if parents.split()[1:] != [base, tip]:
        return 'the merge %s is not the merge of %s onto %s' % (merge[:10], tip[:10], base[:10])
    if gate_ok.mode_of(rec) != 'full':
        return 'the record is not a --full record (mode %s)' % gate_ok.mode_of(rec)
    if rec.get('base') != base:
        return 'the record gated against %s, not %s' % (str(rec.get('base'))[:10], base[:10])
    if rec.get('gate_config') != gate_ok.config_hash(runner.gate_config(cfg)):
        return 'the record was made with another gate config'
    kit = runner.kit_identity()
    if not isinstance(rec.get('kit'), dict) or kit.get('clean') is not True \
            or any(rec['kit'].get(k) != kit.get(k) for k in ('commit', 'python', 'clean')):
        return 'the record was made by another kit (%s)' % str((rec.get('kit') or {}).get('commit'))[:10]
    max_h = cfg.section('landing')['reuse_max_age_h']
    ended = datetime.datetime.strptime(rec['ended_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(
        tzinfo=datetime.timezone.utc)
    if not now - datetime.timedelta(hours=max_h) <= ended <= now + datetime.timedelta(seconds=SKEW_S):
        return 'the record ended %s, not within "landing".reuse_max_age_h (%s)' % (
            rec['ended_utc'], max_h)
    return None


def same_tree(root, cfg, branch, tip, base, merge, now=None):
    """`(SameTree or None, the line to say or None)` for a one-branch landing of `tip` onto
    `base` as `merge` (module docstring "The same tree"). Nothing to say when `tip` has no --full
    record or the merge's tree is not its tree. Raises when git cannot tell (every step runs)."""
    rec, _ = gate_ok.verify_full(root, tip)
    if rec is None:
        return None, None
    tree, why = refs.tree_of(merge, root)
    if not tree:
        raise RuntimeError('git cannot read the tree of %s: %s' % (merge, why))
    if tree != rec['tree']:
        return None, None
    why = _record_why(rec, cfg, base, tip, merge, root, now or _now())
    if why:
        return None, 'SAME TREE %s: the --full record of %s at %s is not used: %s' % (
            tree[:10], branch, tip[:10], why)
    return SameTree(rec, tip, tree), ('SAME TREE %s: %s at %s passed gate.py --full on this tree '
                                      '(record ended %s)' % (tree[:10], branch, tip[:10],
                                                             rec['ended_utc']))


def _from_record(same, spec, step):
    """The REUSED reason when the same-tree record `same` serves `step` (module docstring), or
    None."""
    if same is None or undeclared(spec, step, None, same_tree=True) or spec.get('merged_args'):
        return None
    results = [s.get('result') for s in same.record.get('steps') or [] if s.get('name') == step.name]
    if results != ['pass']:
        return None
    return 'passed in the --full gate of %s on the same tree (ended %s)' % (
        same.tip[:10], same.record['ended_utc'])


def branch_why(cfg, changed, full, only):
    """Why a branch gate reads and stores nothing, or None. `changed`: the paths the gate selects
    by (committed since the merge base, uncommitted, untracked)."""
    if full:
        return '--full'
    if only:
        return '--only'
    return _change_why(cfg, changed)


# ---------------------------------------------------------------- the key

def _file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                return h.hexdigest()
            h.update(block)


def _raise(e):
    raise e


def kit_files_hash():
    """sha256 over (path, sha256) of every file under the kit's kitlib/ and tools/ (bytecode
    left out), sorted. Read afresh on each call: a plan reads it before the run and its keep
    after, so a kit edited while the gate ran stores nothing. A folder that cannot be read
    raises."""
    h = hashlib.sha256()
    for top in KIT_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(S.KIT_ROOT, top),
                                                    onerror=_raise):
            dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
            for n in sorted(filenames):
                if n.endswith(('.pyc', '.pyo')):
                    continue
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, S.KIT_ROOT).replace('\\', '/')
                h.update(gate_ok.canonical_json([rel, _file_hash(full)]).encode('utf-8'))
                h.update(b'\n')
    return h.hexdigest()


def kit_identity():
    """The kit in a cache key: its files (kit_files_hash) and the interpreter running it. Not
    the kit's commit: two commits with the same files are one kit, and a dirty checkout is
    another."""
    return {'files': kit_files_hash(),
            'python': os.path.normcase(os.path.abspath(sys.executable)),
            'version': sys.version}


def undeclared(spec, step, base, same_tree=False):
    """Why the step `step` (its config object `spec`) is never cached, or None. `same_tree`: why
    it is never taken from the tip's record in a same-tree landing (module docstring), where
    {base}, the base commit and declared inputs do not bar it."""
    if spec is None:
        return 'not a configured step'
    if step.check or spec.get('check'):
        return 'a check step'
    if not step.cmd:
        return 'it runs no command'
    if same_tree:
        return None
    for k in ('cmd', 'full_cmd', 'daily_cmd', 'merged_args'):
        if any('{base}' in str(a) for a in spec.get(k) or []):
            return 'its command names {base}: it reads git history'
    if base and any(base.lower() in str(a).lower() for a in step.cmd):
        return 'its command names the base commit %s' % base[:10]
    if not (spec.get('globs') or (spec.get('content') or {}).get('globs')):
        return 'no declared inputs (no "globs" and no "content".globs)'
    return None


def list_files(root):
    """Every file git shows in the work tree at `root`: tracked, or untracked and not ignored
    (relative, sorted). Raises when git cannot say."""
    out, why = cmd.run(['ls-files', '-c', '-o', '--exclude-standard', '-z'], root)
    if out is None:
        raise RuntimeError('git cannot list the files of %s: %s' % (root, why))
    return sorted({p for p in cmd.text(out).split('\0') if p})


def inputs(root, spec, files):
    """{path: sha256 or 'absent'} of the files of `files` that match the step's globs or its
    content.globs. NotCacheable for one that is not a plain file or cannot be read."""
    globs = list(spec.get('globs') or []) + list((spec.get('content') or {}).get('globs') or [])
    out = {}
    for rel in files:
        if not S._match(rel, globs):
            continue
        if '�' in rel or rel.endswith('/'):
            raise NotCacheable('input %s is not a plain file' % rel)
        full = os.path.join(root, rel)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            out[rel] = 'absent'         # tracked and deleted in the work tree
            continue
        except OSError as e:
            raise NotCacheable('input %s cannot be read: %s' % (rel, e))
        if not stat.S_ISREG(st.st_mode) or getattr(st, 'st_reparse_tag', 0):
            raise NotCacheable('input %s is not a plain file' % rel)
        try:
            out[rel] = _file_hash(full)
        except OSError as e:
            raise NotCacheable('input %s cannot be read: %s' % (rel, e))
    return out


def _subs(root):
    """(path spelling, token), longest first: the checkout's root and the kit's."""
    out = []
    for path, token in ((root, '<root>'), (S.KIT_ROOT, '<kit>')):
        p = os.path.abspath(path)
        for spelling in {p, p.replace('\\', '/'), p.replace('/', '\\')}:
            out.append((spelling, token))
    return sorted(out, key=lambda x: -len(x[0]))


def _norm(text, subs):
    for path, token in subs:
        text = re.sub(re.escape(path), lambda _m, t=token: t, text, flags=re.I)
    return text


def tools_of(step, subs):
    """{argv element: sha256} of the step's program (found as the launcher finds it) and of
    every argv element that names a file. NotCacheable when the program cannot be found."""
    out = {}
    exe = shutil.which(step.cmd[0])
    if not exe or not os.path.isfile(exe):
        raise NotCacheable('its program %s is not found' % step.cmd[0])
    try:
        out['0:' + _norm(step.cmd[0], subs)] = _file_hash(exe)
        for i, a in enumerate(step.cmd[1:], 1):
            p = os.path.join(step.cwd, a)
            if a and os.path.isfile(p):
                out['%d:%s' % (i, _norm(a, subs))] = _file_hash(p)
    except OSError as e:
        raise NotCacheable('a file its command names cannot be read: %s' % e)
    return out


def changed_list(changed):
    """The change as the key holds it: sorted, distinct, posix paths."""
    return sorted({p.replace('\\', '/') for p in changed})


def step_key(root, cfg, spec, step, files, kit, changed=()):
    """`(key, inputs)` of `step` in the work tree at `root` (`files`: list_files; `kit`:
    kit_identity; `changed`: the paths the gate selects by). NotCacheable when no file is an
    input: the key would not change with whatever the step really reads."""
    subs = _subs(root)
    ins = inputs(root, spec, files)
    if not ins:
        raise NoInputs('its "globs" and "content".globs match no file git shows in the work '
                           'tree')
    material = {
        'schema': SCHEMA, 'step': step.name,
        'argv': [_norm(a, subs) for a in step.cmd],
        'cwd': os.path.relpath(os.path.abspath(step.cwd), os.path.abspath(root)).replace('\\', '/'),
        'tools': tools_of(step, subs),
        'config': gate_ok.config_hash(runner.gate_config(cfg)),
        'kit': kit,
        'inputs': ins,
        'changed': changed_list(changed),
    }
    key = hashlib.sha256(gate_ok.canonical_json(material).encode('utf-8')).hexdigest()
    return key, ins


# ---------------------------------------------------------------- the store

def cache_dir(root):
    common, why = repo.common_dir(root)
    if not common:
        raise RuntimeError('no git common dir for %s: %s' % (root, why))
    return os.path.join(common, DIR_NAME)


def _when(stamp):
    return datetime.datetime.strptime(stamp, STAMP).replace(tzinfo=datetime.timezone.utc)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _write_entry(folder, name, body):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    tmp = os.path.join(folder, TMP % (os.getpid(), secrets.token_hex(4)))
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(body, sort_keys=True, indent=1) + '\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def store_entry(folder, key, step, res, now=None, **facts):
    """Store `res` ('pass' or 'fail') of `step` under `key`: the entry's path."""
    if not KEY_RE.match(key) or res not in ('pass', 'fail'):
        raise ValueError('not a cache key and result: %r %r' % (key, res))
    now = now or _now()
    body = dict(facts, schema=SCHEMA, key=key, step=step, result=res,
                stored_utc=gate_ok.utc(now))
    name = '%s-%d-%s.%s.json' % (now.strftime(STAMP), os.getpid(), secrets.token_hex(4), res)
    return _write_entry(os.path.join(folder, key), name, body)


def _read_entry(path, key, res, stamp):
    try:
        with open(path, encoding='utf-8') as f:
            body = json.load(f)
    except (OSError, ValueError) as e:
        return None, str(e)
    if not isinstance(body, dict) or body.get('schema') != SCHEMA or body.get('key') != key \
            or body.get('result') != res or body.get('stored_utc') != gate_ok.utc(stamp) \
            or not isinstance(body.get('step'), str):
        return None, 'it does not match its name'
    return body, None


def lookup(folder, key, max_h, now):
    """`(the latest pass, None)` when a pass of `key` is stored within `max_h` hours of `now`
    (SKEW_S into the future) and no fail is stored since `max_h` hours before `now` (however far
    into the future); else `(None, why)`. An entry that cannot be read is a miss."""
    short = key[:12]
    try:
        names = sorted(os.listdir(os.path.join(folder, key)))
    except FileNotFoundError:
        names = []
    except OSError as e:
        return None, 'the cache folder of key %s cannot be read: %s' % (short, e)
    lo = now - datetime.timedelta(hours=max_h)
    hi = now + datetime.timedelta(seconds=SKEW_S)
    seen, passes, fails = False, [], []
    for n in names:
        m = ENTRY_RE.match(n)
        if not m:
            continue                # not an entry: a temp file, or something foreign
        seen = True
        try:
            when = _when(m.group(1))
        except ValueError:
            return None, 'entry %s of key %s cannot be read: a bad time' % (n, short)
        if when < lo or (when > hi and m.group(4) == 'pass'):
            continue                # a fail from the future still counts
        body, why = _read_entry(os.path.join(folder, key, n), key, m.group(4), when)
        if body is None:
            return None, 'entry %s of key %s cannot be read: %s' % (n, short, why)
        (passes if m.group(4) == 'pass' else fails).append((when, body))
    if fails:
        last = max(fails, key=lambda x: x[0])[1]
        return None, 'failed or warned on these inputs at %s (key %s)' % (last['stored_utc'], short)
    if passes:
        return max(passes, key=lambda x: x[0])[1], None
    if seen:
        return None, 'no pass within "landing".reuse_max_age_h (%s) for key %s' % (max_h, short)
    return None, 'no result stored for key %s' % short


def _is_link(entry):
    try:
        return entry.is_symlink() or bool(getattr(entry, 'is_junction', lambda: False)())
    except OSError:
        return True


def prune(folder, now):
    """Remove entries older than PRUNE_H hours, temp files (TMP_RE) last written that long ago,
    and key folders left empty. A newer temp file stays: another gate may be between its write
    and its rename. Only ENTRY_RE and TMP_RE names that are plain files in KEY_RE folders that
    are plain folders inside `folder` are touched. Returns [] or one line naming the failures."""
    cutoff = now - datetime.timedelta(hours=PRUNE_H)
    try:
        with os.scandir(folder) as it:
            tops = list(it)
    except FileNotFoundError:
        return []
    except OSError as e:
        return [PREFIX + 'prune: %s cannot be read: %s' % (folder, e)]
    real = os.path.normcase(os.path.realpath(folder))
    errors = []
    for d in tops:
        try:
            if not KEY_RE.match(d.name) or _is_link(d) or not d.is_dir(follow_symlinks=False):
                continue
            if os.path.dirname(os.path.normcase(os.path.realpath(d.path))) != real:
                continue
            with os.scandir(d.path) as it:
                entries = list(it)
            left = 0
            for e in entries:
                m = ENTRY_RE.match(e.name)
                tmp = TMP_RE.match(e.name)
                if not (m or tmp) or _is_link(e) or not e.is_file(follow_symlinks=False):
                    left += 1
                    continue
                try:
                    if m:
                        old = _when(m.group(1)) < cutoff
                    else:                   # a temp file's name holds no time: its last write
                        old = e.stat(follow_symlinks=False).st_mtime < cutoff.timestamp()
                except ValueError:
                    old = False
                if not old:
                    left += 1
                    continue
                os.remove(e.path)
            if not left:
                os.rmdir(d.path)
        except OSError as e:
            errors.append('%s: %s' % (d.name[:12], e))
    if not errors:
        return []
    return [PREFIX + 'prune: %d folder(s) not pruned; first %s' % (len(errors), errors[0])]


# ---------------------------------------------------------------- the plan

def _asking(cfg, chosen):
    """`(specs, the chosen steps that ask for reuse)`."""
    specs = {s['name']: s for s in cfg.section('gate').get('steps') or []}
    return specs, [s for s in chosen if s.name in specs and _group(specs[s.name])]


def _none(names, why):
    return Plan({}, [PREFIX + 'NONE %s: %s' % (n, why) for n in names])


def plan_steps(root, cfg, chosen, base, changed, now=None, facts=None, same=None):
    """The Plan for `chosen` in the work tree at `root`, the gate-wide checks passed (`changed`:
    the paths the gate selects by, part of each key). `same`: a same-tree landing's SameTree,
    whose record serves a step the cache does not (_from_record)."""
    specs, asking = _asking(cfg, chosen)
    if not asking:
        return NOTHING
    now = now or _now()
    max_h = cfg.section('landing')['reuse_max_age_h']
    folder = cache_dir(root)
    files = list_files(root)
    kit = kit_identity()
    said, keys, hits, taken = {}, {}, {}, {}

    def take(s):
        why = _from_record(same, specs[s.name], s)
        if why:
            taken[s.name] = why
            said[s.name] = 'HIT %s: the --full record of %s (same tree %s, ended %s)' % (
                s.name, same.tip[:10], same.tree[:10], same.record['ended_utc'])

    for s in asking:
        why = undeclared(specs[s.name], s, base)
        if why:
            said[s.name] = 'NONE %s: %s' % (s.name, why)
            take(s)
            continue
        try:
            key, ins = step_key(root, cfg, specs[s.name], s, files, kit, changed)
        except NoInputs as e:
            said[s.name] = 'NONE %s: %s' % (s.name, e)
            take(s)
            continue
        except NotCacheable as e:
            said[s.name] = 'NONE %s: %s' % (s.name, e)
            continue
        keys[s.name] = (key, ins)
        hit, why = lookup(folder, key, max_h, now)
        if hit is None:
            said[s.name] = 'MISS %s: %s' % (s.name, why)
            take(s)
        else:
            hits[s.name] = hit
            said[s.name] = 'HIT %s: key %s, stored %s' % (s.name, key[:12], hit['stored_utc'])
    groups = collections.defaultdict(list)
    for s in asking:
        groups[_group(specs[s.name])].append(s.name)
    for g, names in groups.items():
        missing = [n for n in names if n not in hits and n not in taken]
        if missing:
            for n in names:
                if n in hits or n in taken:
                    hits.pop(n, None)
                    taken.pop(n, None)
                    said[n] = 'MISS %s: %s of its group %s runs' % (n, ', '.join(missing), g)
    reused = {n: 'passed on the same inputs (key %s, stored %s)' % (keys[n][0][:12],
                                                                    hit['stored_utc'])
              for n, hit in hits.items()}
    reused.update(taken)
    keep = _keeper(root, cfg, specs, [s for s in asking if s.name in keys and s.name not in reused],
                   keys, folder, facts or {}, changed)
    return Plan(reused, [PREFIX + said[s.name] for s in asking], keep)


def _keeper(root, cfg, specs, steps, keys, folder, facts, changed):
    """keep(results) -> lines: store each of `steps`' results whose key (`keys`: {name: (key,
    inputs)}, made before the run) is the same after it, then prune. Never raises."""
    def keep(results):
        lines = []
        try:
            by_name = {r['name']: r for r in results}
            todo = [s for s in steps if STORE_AS.get((by_name.get(s.name) or {}).get('status'))]
            files = list_files(root) if todo else []
            kit = kit_identity() if todo else None
            for s in todo:
                try:
                    key, ins = step_key(root, cfg, specs[s.name], s, files, kit, changed)
                    if key != keys[s.name][0]:
                        what = ('its inputs changed while the gate ran' if ins != keys[s.name][1]
                                else 'its key changed while the gate ran (the kit, a program or '
                                'the config)')
                        lines.append(PREFIX + 'NOT STORED %s: %s' % (s.name, what))
                        continue
                    res = STORE_AS[by_name[s.name]['status']]
                    store_entry(folder, key, s.name, res, inputs=len(ins), **facts)
                    lines.append(PREFIX + 'STORED %s: %s, key %s' % (s.name, res, key[:12]))
                except Exception as e:      # noqa: BLE001 - a store never fails the gate
                    lines.append(PREFIX + 'NOT STORED %s: %s: %s' % (s.name, type(e).__name__, e))
            lines += prune(folder, _now())
        except Exception as e:              # noqa: BLE001 - a store never fails the gate
            lines.append(PREFIX + 'NOT STORED: %s: %s' % (type(e).__name__, e))
        return lines
    return keep


def _guarded(cfg, chosen, make):
    try:
        return make()
    except Exception as e:      # noqa: BLE001 - fail closed: every step runs, nothing is stored
        why = '%s: %s' % (type(e).__name__, e)
        try:
            names = [s.name for s in _asking(cfg, chosen)[1]]
        except Exception:       # noqa: BLE001
            names = []
        return _none(names, why) if names else Plan({}, [PREFIX + 'NONE: %s' % why])


def planner(root, cfg, branch, tip, base, merge, prior, fresh=False, full=False, batch=1,
            now=None):
    """`reuse(chosen) -> Plan` for kitlib.gate.runner.merged_gate: a landing of `merge` (onto
    `base`, --expect-main) in the landing tree `root` (kitlib.gate.land, step 5b; the main
    checkout is never merged into). `branch`, `tip`: the first of the
    landing's; `batch` is the number of branches (a batch reuses per step like one branch; only
    a one-branch landing reads the tip's record, same_tree). Anything that raises means every
    step runs (the reason is said)."""
    def reuse(chosen):
        def make():
            _, asking = _asking(cfg, chosen)
            if not asking:
                return NOTHING
            why = landing_why(root, cfg, base, merge, prior, fresh, full)
            if why:
                return _none([s.name for s in asking], why)
            changed, why = runner.changed_between(root, base, merge)
            if changed is None:
                raise RuntimeError('git cannot list the merged paths: %s' % why)
            facts = {'head': merge, 'branch': branch, 'batch': batch}
            same, line = same_tree(root, cfg, branch, tip, base, merge, now=now) if batch == 1 \
                else (None, None)
            plan = plan_steps(root, cfg, chosen, base, changed, now=now, facts=facts, same=same)
            return plan._replace(lines=[PREFIX + line] + plan.lines) if line else plan
        return _guarded(cfg, chosen, make)
    return reuse


def branch_plan(top, cfg, chosen, base, changed, full=False, only=None, head=None, branch=None,
                now=None):
    """The Plan for a branch gate's `chosen` steps in the checkout `top` (`base`: the merge base;
    `changed`: what the gate selects by). Anything that raises means every step runs."""
    def make():
        _, asking = _asking(cfg, chosen)
        if not asking:
            return NOTHING
        why = branch_why(cfg, changed, full, only)
        if why:
            return _none([s.name for s in asking], why)
        return plan_steps(top, cfg, chosen, base, changed, now=now,
                          facts={'head': head, 'branch': branch})
    return _guarded(cfg, chosen, make)
