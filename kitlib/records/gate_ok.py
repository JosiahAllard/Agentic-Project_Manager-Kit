"""The gate-OK record: the gate's word that it passed exactly one commit (ARCHITECTURE.md
"Gate-OK record").

gate.py writes `<dev_drive>/pm-kit/kit-gate-ok/<repo key>/<commit>.json` (record_dir; the repo
key is kitlib.gate.result.repo_key's, one for every worktree of a repository) for the commit it
passed; landings, the kit guard and the pre-push hook read it. The folder is outside every
checkout and every agent's temp folder, where agents' own file writes do not reach; the git
common dir, where records were before, is writable by any session that can write `.git`. With no
`dev_drive` there is no record folder: a write is a RecordError and nothing is written. One
record per commit; a later pass of the same commit replaces it (atomically: a temp file in the
same folder, then os.replace).

The grace window (until KIT-GATE-OK-LEGACY-READ-RETIRE and KIT-GATE-OK-GIT-COPIES-RETIRE): every
write also writes the record's .git copy, `<git common dir>/kit-gate-ok/<commit>.json`
(legacy_dir), for an older kit's readers. A reader with no record in the new folder reads the
.git copy: read(legacy=ANY), the pre-push hook's, takes any; every other reader (TRUSTED, the
default: a landing, the guard's push-to-main rule, the worktree and review tools) takes one only
for a tip on the frozen list, with the bytes it had then. The frozen list (frozen_path,
`<repo key>.frozen.json` beside the record folder) is the sha256 of every .git record file
there was when this kit first wrote or read that repository's records (freeze): made once, by a
rename that never replaces it, and never extended, so a .git record written after the switch,
forged or by an older kit, never counts for a trusted reader.

The record (SCHEMA 1), every key always present:

  schema        1
  verdict       "OK" (only a pass is ever written)
  commit        the full hash passed; also the file name
  tree          that commit's tree hash
  base          the base commit the gate compared against, or null
  gate_config   sha256 (hex) of the canonical JSON of the gate config the run used
                (config_hash: sorted keys, no spaces, UTF-8)
  kit           the kit's identity: {"version": ..., "commit": ...} as the caller states it
  author        the commit's author as git records it, `Name <email>`: the identity owner
                checks were made against
  repo          the repository name (`.kit.json` "repo".name)
  started_utc   when the gate started, ended_utc when it passed (YYYY-MM-DDTHH:MM:SSZ)
  steps         [{"name", "result", "seconds"}] in run order

Added keys (ADDED_KEYS; written on every new record, not required of an older one by verify()
unless the caller asks for what they carry):

  config_rev    the full hash of the commit whose `.kit.json` the gate config came from (the
                branch gate: the commit gated; a landing: --expect-main), or null
  source        where the gate's steps came from: "gate.json" (the "gate" section of
                `.kit.json`, inline or in its file) or "auto" (detected from the tree,
                kitlib.gate.detect), or null. A reader takes a record without it as "gate.json"
                (source_of)
  full          true when the gate ran `--full` (every non-manual step, each by its full_cmd when
                it has one; kitlib.gate.steps.select), false for any other run. Only the JSON
                true counts (full_of): a record without the key was written by a gate from before
                records said which (an older kit, or a kit branch's own gate.py), and a reader that needs a full gate (verify_full; a landing's branch tips)
                takes it as not full. Nothing in the older keys tells the two apart: the steps
                carry names only, and a step with a full_cmd has the same name in both modes
  mode          what the gate selected (MODES): "full" (--full; `full` is true), "select" (the
                steps the change selects, plus "gate".smoke_steps; kitlib.gate.steps.select) or
                "docs" (a docs-only change: only "gate".docs_only steps ran;
                kitlib.gate.steps.docs_only). A record without it is "full" when full_of says so,
                else unknown (mode_of: None), and a landing takes an unknown mode only as --full

verify() re-derives everything git can answer: the record's file name, `commit`, `tree` and
`author` must match the repository now, and `gate_config` must match the caller's config when it
passes one. With `config_at` (rev -> that commit's gate config) it instead requires `config_rev`,
a commit in `commit`'s history (or `commit` itself), and the hash of config_at(config_rev). A
record is evidence for a reader that trusts the record folder; it is not a signature.
"""
import datetime
import hashlib
import json
import os
import secrets
import time

from kitlib.git import cmd, refs, repo

DIR_NAME = 'kit-gate-ok'
SCHEMA = 1
VERDICT = 'OK'
KEYS = ('schema', 'verdict', 'commit', 'tree', 'base', 'gate_config', 'kit', 'author', 'repo',
        'started_utc', 'ended_utc', 'steps')
ADDED_KEYS = ('config_rev', 'source', 'full', 'mode')
MODES = ('full', 'select', 'docs')
RETRIES = 5
RETRY_SLEEP = 0.2


FROZEN_SUFFIX = '.frozen.json'
TRUSTED = 'trusted'     # the new folder; a .git copy only for a tip on the frozen list
ANY = 'any'             # the new folder, else any .git copy: the pre-push hook's grace read
LEGACY = (TRUSTED, ANY)


class RecordError(Exception):
    """A record could not be written, or git could not answer what writing it needs."""


class FrozenMissing(RecordError):
    """The frozen list is not there though the record folder holds records (freeze)."""


def utc(when=None):
    return (when or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def config_hash(gate_config):
    """sha256 hex of the canonical JSON of `gate_config` (any JSON value)."""
    return hashlib.sha256(canonical_json(gate_config).encode('utf-8')).hexdigest()


def _result():
    from kitlib.gate import result      # result imports this module
    return result


def _common(root):
    common, why = repo.common_dir(root)
    if not common:
        raise RecordError('no git common dir for %s: %s' % (root, why))
    return common


def machine_dir():
    """`<dev_drive>/pm-kit/kit-gate-ok`: the folder every repository's record folder is in
    (kitlib.gate.result.kit_folder). RecordError when there is no dev_drive."""
    r = _result()
    try:
        return r.kit_folder(DIR_NAME, 'gate-OK folder')
    except r.ResultError as e:
        raise RecordError(str(e))


def record_dir(root):
    """The record folder of the repository `root` is in: `<dev_drive>/pm-kit/kit-gate-ok/<repo
    key>` (kitlib.gate.result.repo_key: one for every worktree of a repository). RecordError when
    it cannot be worked out (no git common dir, no dev_drive)."""
    common = _common(root)
    return os.path.join(machine_dir(), _result().repo_key(common))


def legacy_dir(root):
    """The old record folder, `<git common dir>/kit-gate-ok`: written beside the new one until
    KIT-GATE-OK-GIT-COPIES-RETIRE, read as the module docstring says."""
    return os.path.join(_common(root), DIR_NAME)


def frozen_path(root):
    """The frozen list's file: `<dev_drive>/pm-kit/kit-gate-ok/<repo key>.frozen.json`, beside
    the record folder (never in it, so no listing of records meets it)."""
    common = _common(root)
    return os.path.join(machine_dir(), _result().repo_key(common) + FROZEN_SUFFIX)


def _name(commit):
    if not refs.is_sha(commit):
        raise RecordError('a gate-OK record is named by a full commit hash, not %r' % (commit,))
    return commit + '.json'


def record_path(root, commit):
    return os.path.join(record_dir(root), _name(commit))


def legacy_path(root, commit):
    return os.path.join(legacy_dir(root), _name(commit))


def is_record_name(name):
    """Whether a file name in a record folder is a record's (`<full hash>.json`), not a temp file."""
    return name.endswith('.json') and refs.is_sha(name[:-len('.json')])


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _snapshot(folder):
    """{commit: sha256 of its file's bytes} of every record file in `folder` ({} when there is
    none). RecordError when the folder or a file cannot be read."""
    try:
        names = sorted(n for n in os.listdir(folder) if is_record_name(n))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RecordError('cannot list %s: %s' % (folder, e))
    out = {}
    for n in names:
        try:
            with open(os.path.join(folder, n), 'rb') as f:
                out[n[:-len('.json')]] = _digest(f.read())
        except FileNotFoundError:
            continue
        except OSError as e:
            raise RecordError('cannot read %s: %s' % (os.path.join(folder, n), e))
    return out


def _read_frozen(path):
    """The frozen list's records ({commit: digest}), or None when the file is not there.
    RecordError when it is there and is not one freeze() wrote."""
    try:
        with open(path, encoding='utf-8') as f:
            body = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise RecordError('the frozen list %s cannot be read: %s' % (path, e))
    got = body.get('records') if isinstance(body, dict) and body.get('schema') == SCHEMA else None
    if not isinstance(got, dict) or not all(
            refs.is_sha(k) and isinstance(v, str) for k, v in got.items()):
        raise RecordError('the frozen list %s is not one this kit wrote' % path)
    return got


def freeze(root):
    """The frozen list of `root`'s repository, {commit: sha256 of its .git copy's bytes}: the
    tips whose .git copy (legacy_dir) land, the guard and every other trusted read still take
    (the module docstring). Made once, by the first write or trusted read of a kit that has the
    new folder: a snapshot of every record file in legacy_dir then, written to a temp file and
    renamed onto frozen_path (Windows' rename never replaces a file, so one process's snapshot
    wins and every other reads it). It is never extended. RecordError, and no .git copy counts,
    when it cannot be read or made, and when it is missing while the record folder already
    holds records (it was removed after it was made: made again it would take whatever .git
    holds now)."""
    path = frozen_path(root)
    got = _read_frozen(path)
    if got is not None:
        return got
    folder = record_dir(root)
    try:
        held = any(is_record_name(n) for n in os.listdir(folder))
    except FileNotFoundError:
        held = False
    except OSError as e:
        raise RecordError('cannot list %s: %s' % (folder, e))
    if held:
        raise FrozenMissing('the frozen list %s is missing while %s holds records, so no .git copy '
                          'counts' % (path, folder))
    old = legacy_dir(root)
    body = {'schema': SCHEMA, 'frozen_utc': utc(), 'legacy_dir': old, 'records': _snapshot(old)}
    parent = os.path.dirname(path)
    tmp = os.path.join(parent, '.%s.%s.tmp' % (os.path.basename(path), secrets.token_hex(4)))
    try:
        os.makedirs(parent, exist_ok=True)
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(body, sort_keys=True, indent=1) + '\n')
            os.rename(tmp, path)
        except FileExistsError:
            pass                # another process froze first: its list is the one
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise RecordError('cannot write the frozen list %s: %s' % (path, e))
    got = _read_frozen(path)
    if got is None:
        raise RecordError('the frozen list %s was not there after it was written' % path)
    return got


def author_of(root, commit):
    """`(Name <email>, None)` of `commit`'s author as git records it, or `(None, why)`."""
    why = refs.bad_rev(commit)
    if why:
        return None, why
    out, why = cmd.run_text(['show', '-s', '--format=%an <%ae>', commit, '--'], root)
    if out is None or not out.strip():
        return None, why or 'git show printed no author for %s' % commit
    return out.strip(), None


def _facts(root, rev):
    commit, why = refs.resolve(rev, root)
    if not commit:
        raise RecordError('cannot resolve %s: %s' % (rev, why))
    tree, why = refs.tree_of(commit, root)
    if not tree:
        raise RecordError('cannot read the tree of %s: %s' % (commit, why))
    author, why = author_of(root, commit)
    if not author:
        raise RecordError('cannot read the author of %s: %s' % (commit, why))
    return commit, tree, author


def _step(s, i):
    if not isinstance(s, dict) or not isinstance(s.get('name'), str) or not s['name']:
        raise RecordError('steps[%d] must be an object with a name' % i)
    secs = s.get('seconds')
    if secs is not None and (isinstance(secs, bool) or not isinstance(secs, (int, float))):
        raise RecordError('steps[%d].seconds must be a number' % i)
    return {'name': s['name'], 'result': s.get('result', 'pass'), 'seconds': secs}


def source_of(record):
    """A record's `source`: "auto" or "gate.json"; a record written before the key, or with
    null, is "gate.json" (every gate then ran its configured steps)."""
    return record.get('source') or 'gate.json'


def full_of(record):
    """True only when the record says its gate ran --full (`full` is the JSON true); a record
    written before the key, or with anything else there, is not a full gate."""
    return record.get('full') is True


def mode_of(record):
    """A record's mode: its `mode` when it is one of MODES and agrees with `full` ("full" exactly
    when full_of); a record without the key is "full" when full_of, else None (unknown), as is
    one whose mode is anything else."""
    mode = record.get('mode')
    if mode is None:
        return 'full' if full_of(record) else None
    if mode not in MODES or (mode == 'full') != full_of(record):
        return None
    return mode


def verify_full(root, commit, legacy=TRUSTED):
    """`(record, None)` when verify() takes `commit`'s record and it is from a --full gate
    (full_of), else `(None, why)`: verify's reason first, then that the gate was not --full."""
    rec, why = verify(root, commit, legacy=legacy)
    if rec is None:
        return None, why
    if full_of(rec):
        return rec, None
    if 'full' not in rec:
        return None, ('the record for %s does not say whether the gate ran --full (written by a '
                      'gate from before records said so), so it does not count' % rec['commit'])
    return None, 'the record for %s is from a gate run without --full' % rec['commit']


def build(root, rev, gate_config, kit, repo_name, started_utc, steps, base=None, ended_utc=None,
          config_rev=None, source=None, full=False, mode=None):
    """The record for `rev` (resolved here: commit, tree and author come from git, never the
    caller). `config_rev`: the commit the gate config was read from (resolved to a full hash).
    `source`: "gate.json" or "auto" (kitlib.gate.detect), or None. `full`: the gate ran --full.
    `mode`: one of MODES; None: "full" with `full`, else "select". "full" goes with `full` and
    only with it (RecordError)."""
    full = full is True
    if mode is None:
        mode = 'full' if full else 'select'
    if mode not in MODES or (mode == 'full') != full:
        raise RecordError('mode %r with full %r: the mode is one of %s, and "full" exactly when '
                          'the gate ran --full' % (mode, full, ', '.join(MODES)))
    commit, tree, author = _facts(root, rev)
    if base is not None and not refs.is_sha(base):
        resolved, why = refs.resolve(base, root)
        if not resolved:
            raise RecordError('cannot resolve the base %s: %s' % (base, why))
        base = resolved
    if config_rev is not None and not refs.is_sha(config_rev):
        resolved, why = refs.resolve(config_rev, root)
        if not resolved:
            raise RecordError('cannot resolve the config rev %s: %s' % (config_rev, why))
        config_rev = resolved
    if not isinstance(kit, dict) or not kit:
        raise RecordError('the kit identity is an object such as {"version": ..., "commit": ...}')
    return {
        'schema': SCHEMA, 'verdict': VERDICT, 'commit': commit, 'tree': tree, 'base': base,
        'gate_config': config_hash(gate_config), 'kit': dict(kit), 'author': author,
        'repo': repo_name or '', 'started_utc': started_utc, 'ended_utc': ended_utc or utc(),
        'steps': [_step(s, i) for i, s in enumerate(steps or [])],
        'config_rev': config_rev, 'source': source, 'full': full, 'mode': mode,
    }


def _retry(fn, *args):
    for i in range(RETRIES):
        try:
            return fn(*args)
        except PermissionError:
            if i == RETRIES - 1:
                raise
            time.sleep(RETRY_SLEEP)


def write(root, record):
    """Write `record` (from build()) to the record folder, and its .git copy to legacy_dir, each
    atomically; returns the record folder's path. The frozen list is made first when it is not
    there yet (freeze; a FrozenMissing one is left so: no .git copy counts), then the .git copy,
    then the record itself, so the record a trusted reader takes is there only when every write
    before it was. RecordError, and nothing written, when there is no record folder (no
    dev_drive)."""
    commit = record.get('commit')
    path = record_path(root, commit)
    try:
        freeze(root)
    except FrozenMissing:
        pass
    _write_file(legacy_path(root, commit), record)
    return _write_file(path, record)


def _freeze_early(root):
    """freeze() when the frozen list is not there yet, its errors dropped: a trusted read that
    finds no record at all still makes the list as early as it can, so a .git copy written later
    is never on it. The read's answer does not depend on it."""
    try:
        if not os.path.exists(frozen_path(root)):
            freeze(root)
    except RecordError:
        pass


def _write_file(path, record):
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, '.%s.%s.tmp' % (record['commit'], secrets.token_hex(4)))
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(record, sort_keys=True, indent=1) + '\n')
            _retry(os.replace, tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise RecordError('cannot write %s: %s' % (path, e))
    return path


def record_pass(root, rev, gate_config, kit, repo_name, started_utc, steps, base=None,
                config_rev=None, source=None, full=False, mode=None):
    """build() then write(): `(path, record)`. Only a passing gate calls this."""
    rec = build(root, rev, gate_config, kit, repo_name, started_utc, steps, base,
                config_rev=config_rev, source=source, full=full, mode=mode)
    return write(root, rec), rec


def _load(path):
    """`(record, None, digest)`, `(None, 'none', None)` when there is no file, or
    `(None, why, digest)`: digest is the sha256 of the file's bytes."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return None, 'none', None
    except OSError as e:
        return None, 'cannot read %s: %s' % (path, e), None
    digest = _digest(data)
    try:
        rec = json.loads(data.decode('utf-8'))
    except ValueError as e:
        return None, '%s is not a gate-OK record: %s' % (path, e), digest
    if not isinstance(rec, dict):
        return None, '%s is not a gate-OK record' % path, digest
    return rec, None, digest


def read(root, commit, legacy=TRUSTED):
    """`(record, None)`, `(None, 'none')` when there is no record, or `(None, why)` when the file
    is there and cannot be read as one, or cannot be taken. The record folder's file first; when
    it has none, the .git copy (legacy_path): with `legacy` TRUSTED (the default) only for a tip
    on the frozen list whose copy has the bytes it had when frozen (freeze), with ANY whatever is
    there. No record folder (no dev_drive): TRUSTED says why; ANY reads the .git copy."""
    if legacy not in LEGACY:
        raise ValueError('legacy is one of %s, not %r' % (', '.join(LEGACY), legacy))
    try:
        old = legacy_path(root, commit)
    except RecordError as e:
        return None, str(e)
    try:
        path = record_path(root, commit)
    except RecordError as e:
        if legacy != ANY:
            return None, str(e)
        path = None
    if path is not None:
        rec, why, _ = _load(path)
        if why != 'none':
            return rec, why
    rec, why, digest = _load(old)
    if legacy == ANY:
        return rec, why
    if why == 'none':
        _freeze_early(root)
        return rec, why
    try:
        frozen = freeze(root)
    except RecordError as e:
        return None, ('the record for %s is only in %s, and no .git copy counts: %s'
                      % (commit, legacy_dir(root), e))
    if commit not in frozen:
        return None, ('the record for %s is only in %s, and it is not one the frozen list (%s) '
                      'took when the record folder moved to %s, so it does not count'
                      % (commit, legacy_dir(root), frozen_path(root), record_dir(root)))
    if frozen[commit] != digest:
        return None, ('the .git copy of the record for %s has changed since the frozen list '
                      '(%s) took it, so it does not count' % (commit, frozen_path(root)))
    return rec, why


def verify(root, commit, gate_config=None, config_at=None, legacy=TRUSTED):
    """`(record, None)` when `commit` has a gate-OK record git still agrees with, else
    `(None, why)`. `gate_config` given: the record's hash must be that config's. `config_at` given
    (a callable: full hash -> the gate config that commit holds; it may raise RecordError): the
    record's `config_rev` must be a full hash in `commit`'s history and the record's hash must be
    config_at(config_rev)'s. `legacy`: which .git copies count (read)."""
    if not refs.is_sha(commit):
        resolved, why = refs.resolve(commit, root) if isinstance(commit, str) else (None, None)
        if not resolved:
            return None, 'cannot resolve %r: %s' % (commit, why)
        commit = resolved
    rec, why = read(root, commit, legacy=legacy)
    if rec is None:
        return None, ('no gate-OK record for %s' % commit) if why == 'none' else why
    missing = [k for k in KEYS if k not in rec]
    if missing:
        return None, 'the record for %s lacks %s' % (commit, ', '.join(missing))
    if rec['schema'] != SCHEMA:
        return None, 'the record for %s is schema %r, not %d' % (commit, rec['schema'], SCHEMA)
    if rec['verdict'] != VERDICT:
        return None, 'the record for %s says %r, not OK' % (commit, rec['verdict'])
    if rec['commit'] != commit:
        return None, 'the record filed under %s names commit %s' % (commit, rec['commit'])
    try:
        _, tree, author = _facts(root, commit)
    except RecordError as e:
        return None, str(e)
    if rec['tree'] != tree:
        return None, 'the record for %s names tree %s; git says %s' % (commit, rec['tree'], tree)
    if rec['author'] != author:
        return None, 'the record for %s names author %r; git says %r' % (commit, rec['author'], author)
    if gate_config is not None and rec['gate_config'] != config_hash(gate_config):
        return None, 'the record for %s was made with another gate config' % commit
    if config_at is not None:
        rev = rec.get('config_rev')
        if not refs.is_sha(rev):
            return None, 'the record for %s names no config_rev (%r)' % (commit, rev)
        inside, why = refs.is_ancestor(rev, commit, root)
        if inside is not True:
            return None, 'the record for %s names config_rev %s, which is not in its history%s' % (
                commit, rev, ': %s' % why if why else '')
        try:
            cfg = config_at(rev)
        except RecordError as e:
            return None, str(e)
        if rec['gate_config'] != config_hash(cfg):
            return None, ('the record for %s was made with another gate config than its '
                          'config_rev %s holds' % (commit, rev[:10]))
    return rec, None
