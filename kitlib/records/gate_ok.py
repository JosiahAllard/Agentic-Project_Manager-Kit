"""The gate-OK record: the gate's word that it passed exactly one commit (ARCHITECTURE.md
"Gate-OK record").

gate.py writes `<dev_drive>/pm-kit/kit-gate-ok/<repo key>/<commit>.json` (record_dir; the repo
key is kitlib.gate.result.repo_key's, one for every worktree of a repository) for the commit it
passed; landings, the kit guard and the pre-push hook read it. The folder is outside every
checkout and every agent's temp folder, where agents' own file writes do not reach; the git
common dir, where records were before, is writable by any session that can write `.git`. With no
`dev_drive` there is no record folder: a write writes only the .git copy, then raises
NoRecordFolder. One record per commit; a later pass of the same commit replaces it (atomically: a
temp file in the same folder, then os.replace).

The grace window (until the legacy read and the .git copies are retired, later work): every
write also writes the record's .git copy, `<git common dir>/kit-gate-ok/<commit>.json`
(legacy_dir), after the folder's record, for an older kit's readers (write). Every reader (a landing, the guard, the pre-push
hook, the worktree and review tools) reads the record folder first and, when it has no record
for the commit or there is no record folder (no dev_drive), the .git copy, so a branch gated by
an older kit or before the record folder moved needs no fresh gate.

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
  input_keys    {"schema": 1, "steps": {name: {part: sha256}}}: for each step that asks for reuse
                and passed, the digests a landing compares to serve it from this record when
                main moved (kitlib.gate.reuse, "The input tree"; INPUT_PARTS), or null (no step
                has one, or the gate wrote none: --only, a partial or docs-only gate, a work tree
                not clean at its start)

One more key, only on a record of a gate that ran on a load node (import_record, a branch's
gate: docs/load-nodes.md "Gating a branch on a node"; build with `ran_on`, a landing's merged gate
main checked with kitlib.loadnode.landgate); a record without it ran on this machine. verify()
neither requires nor checks it:

  ran_on        {"node": its registry name, "host_key": the SHA256 fingerprint of the host key
                main pinned for it, "job": the node job id}, written by main from its own
                registry, never taken from the node. Its record's input_keys is always null

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

from kitlib.fs import replace as fsreplace
from kitlib.git import cmd, refs, repo

DIR_NAME = 'kit-gate-ok'
SCHEMA = 1
VERDICT = 'OK'
KEYS = ('schema', 'verdict', 'commit', 'tree', 'base', 'gate_config', 'kit', 'author', 'repo',
        'started_utc', 'ended_utc', 'steps')
ADDED_KEYS = ('config_rev', 'source', 'full', 'mode', 'input_keys')
MODES = ('full', 'select', 'docs')
RAN_ON_KEYS = ('node', 'host_key', 'job')
RETRIES = 5         # tries of a record's os.replace while it fails with PermissionError
RETRY_SLEEP = 0.2   # seconds between them
# RETRIES as a wait for kitlib.fs.replace: try k starts at least k-1 pauses in, so the last (at
# 0.8 s or later) is past this bound and the one before it (at 0.6 s) is not; the half pause is
# the margin for a slow try.
REPLACE_WAIT = (RETRIES - 1.5) * RETRY_SLEEP


class RecordError(Exception):
    """A record could not be written, or git could not answer what writing it needs."""


class NoRecordFolder(RecordError):
    """There is no record folder: the machine config sets no dev_drive, or cannot be read."""


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
    (kitlib.gate.result.kit_folder). NoRecordFolder when there is no dev_drive."""
    r = _result()
    try:
        return r.kit_folder(DIR_NAME, 'gate-OK folder')
    except r.ResultError as e:
        raise NoRecordFolder(str(e))


def record_dir(root):
    """The record folder of the repository `root` is in: `<dev_drive>/pm-kit/kit-gate-ok/<repo
    key>` (kitlib.gate.result.repo_key: one for every worktree of a repository). RecordError when
    it cannot be worked out (no git common dir; NoRecordFolder: no dev_drive)."""
    common = _common(root)
    return os.path.join(machine_dir(), _result().repo_key(common))


def legacy_dir(root):
    """The old record folder, `<git common dir>/kit-gate-ok`: written beside the new one until
    the .git copies are retired, and read when the new one has no record until the legacy read
    is retired (read)."""
    return os.path.join(_common(root), DIR_NAME)


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


def verify_full(root, commit):
    """`(record, None)` when verify() takes `commit`'s record and it is from a --full gate
    (full_of), else `(None, why)`: verify's reason first, then that the gate was not --full."""
    rec, why = verify(root, commit)
    if rec is None:
        return None, why
    if full_of(rec):
        return rec, None
    if 'full' not in rec:
        return None, ('the record for %s does not say whether the gate ran --full (written by a '
                      'gate from before records said so), so it does not count' % rec['commit'])
    return None, 'the record for %s is from a gate run without --full' % rec['commit']


def _input_keys(keys):
    """`keys` when it is None or {"schema": int, "steps": {name: {part: 64-hex}}}, else
    RecordError."""
    if keys is None:
        return None
    bad = RecordError('input_keys is {"schema": int, "steps": {name: {part: sha256}}}')
    if not isinstance(keys, dict) or set(keys) != {'schema', 'steps'} \
            or isinstance(keys['schema'], bool) or not isinstance(keys['schema'], int) \
            or not isinstance(keys['steps'], dict):
        raise bad
    for name, parts in keys['steps'].items():
        if not isinstance(name, str) or not name or not isinstance(parts, dict) or not parts:
            raise bad
        for part, digest in parts.items():
            if not isinstance(part, str) or not isinstance(digest, str) or len(digest) != 64 \
                    or digest.strip('0123456789abcdef'):
                raise bad
    return {'schema': keys['schema'],
            'steps': {n: dict(parts) for n, parts in keys['steps'].items()}}


def _ran_on(ran_on):
    """`ran_on` as a new dict when it is {"node", "host_key", "job"}, each a non-empty string,
    else RecordError."""
    if not isinstance(ran_on, dict) or set(ran_on) != set(RAN_ON_KEYS) or not all(
            isinstance(ran_on[k], str) and ran_on[k] for k in RAN_ON_KEYS):
        raise RecordError('ran_on is {%s}, each a string' % ', '.join(RAN_ON_KEYS))
    return dict(ran_on)


def build(root, rev, gate_config, kit, repo_name, started_utc, steps, base=None, ended_utc=None,
          config_rev=None, source=None, full=False, mode=None, input_keys=None, ran_on=None):
    """The record for `rev` (resolved here: commit, tree and author come from git, never the
    caller). `config_rev`: the commit the gate config was read from (resolved to a full hash).
    `source`: "gate.json" or "auto" (kitlib.gate.detect), or None. `full`: the gate ran --full.
    `mode`: one of MODES; None: "full" with `full`, else "select". "full" goes with `full` and
    only with it (RecordError). `input_keys`: kitlib.gate.reuse.record_keys, or None. `ran_on`:
    the steps ran on a load node ({"node", "host_key", "job"}, from main's registry), so the
    record gets that key and no input_keys (RecordError when both are given)."""
    if ran_on is not None:
        ran_on = _ran_on(ran_on)
        if input_keys is not None:
            raise RecordError('a record of a gate run on a load node has no input_keys')
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
    input_keys = _input_keys(input_keys)
    rec = {
        'schema': SCHEMA, 'verdict': VERDICT, 'commit': commit, 'tree': tree, 'base': base,
        'gate_config': config_hash(gate_config), 'kit': dict(kit), 'author': author,
        'repo': repo_name or '', 'started_utc': started_utc, 'ended_utc': ended_utc or utc(),
        'steps': [_step(s, i) for i, s in enumerate(steps or [])],
        'config_rev': config_rev, 'source': source, 'full': full, 'mode': mode,
        'input_keys': input_keys,
    }
    if ran_on is not None:
        rec['ran_on'] = ran_on
    return rec


def write(root, record):
    """Write `record` (from build()) to the record folder, then to its .git copy (legacy_dir),
    each atomically; returns the record folder's path. The folder first, so a failed write leaves
    no copy that read() would take; when the copy then fails the folder's record is removed, so
    the record is there only when its copy is. RecordError when either write fails.
    NoRecordFolder, with only the .git copy written, when there is no record folder (no
    dev_drive)."""
    commit = record.get('commit')
    old = legacy_path(root, commit)
    try:
        path = record_path(root, commit)
    except NoRecordFolder:
        _write_file(old, record)
        raise
    _write_file(path, record)
    try:
        _write_file(old, record)
    except RecordError as e:
        try:
            os.remove(path)
        except OSError as gone:
            raise RecordError('%s; and %s could not be removed: %s' % (e, path, gone))
        raise
    return path


def _write_file(path, record):
    """Write `record` to `path`: a temp file, then os.replace, retried while a reader holds the
    old file open (PermissionError; kitlib.fs.replace for REPLACE_WAIT). RecordError on failure."""
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, '.%s.%s.tmp' % (record['commit'], secrets.token_hex(4)))
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(record, sort_keys=True, indent=1) + '\n')
            fsreplace.replace(tmp, path, wait=REPLACE_WAIT, pause=RETRY_SLEEP)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise RecordError('cannot write %s: %s' % (path, e))
    return path


def record_pass(root, rev, gate_config, kit, repo_name, started_utc, steps, base=None,
                config_rev=None, source=None, full=False, mode=None, input_keys=None,
                ran_on=None):
    """build() then write(): `(path, record)`. Only a passing gate calls this."""
    rec = build(root, rev, gate_config, kit, repo_name, started_utc, steps, base,
                config_rev=config_rev, source=source, full=full, mode=mode,
                input_keys=input_keys, ran_on=ran_on)
    return write(root, rec), rec


def _load(path):
    """`(record, None)`, `(None, 'none')` when there is no file, or `(None, why)`."""
    try:
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
    except FileNotFoundError:
        return None, 'none'
    except OSError as e:
        return None, 'cannot read %s: %s' % (path, e)
    except ValueError as e:
        return None, '%s is not a gate-OK record: %s' % (path, e)
    if not isinstance(rec, dict):
        return None, '%s is not a gate-OK record' % path
    return rec, None


def read(root, commit):
    """`(record, None)`, `(None, 'none')` when there is no record, or `(None, why)` when the file
    is there and cannot be read as one. The record folder's file first; when it has none, or
    there is no record folder (no dev_drive), the .git copy (legacy_path), until the legacy read
    is retired (the module docstring)."""
    try:
        old = legacy_path(root, commit)
    except RecordError as e:
        return None, str(e)
    try:
        path = record_path(root, commit)
    except RecordError:
        path = None
    if path is not None:
        rec, why = _load(path)
        if why != 'none':
            return rec, why
    return _load(old)


def verify(root, commit, gate_config=None, config_at=None):
    """`(record, None)` when `commit` has a gate-OK record git still agrees with, else
    `(None, why)`. `gate_config` given: the record's hash must be that config's. `config_at` given
    (a callable: full hash -> the gate config that commit holds; it may raise RecordError): the
    record's `config_rev` must be a full hash in `commit`'s history and the record's hash must be
    config_at(config_rev)'s."""
    if not refs.is_sha(commit):
        resolved, why = refs.resolve(commit, root) if isinstance(commit, str) else (None, None)
        if not resolved:
            return None, 'cannot resolve %r: %s' % (commit, why)
        commit = resolved
    rec, why = read(root, commit)
    if rec is None:
        return None, ('no gate-OK record for %s' % commit) if why == 'none' else why
    why = _check(root, commit, rec, gate_config, config_at)
    return (None, why) if why is not None else (rec, None)


def _check(root, commit, rec, gate_config=None, config_at=None):
    """Why the record `rec` (a dict) filed under the full hash `commit` is not one git still
    agrees with, or None: verify()'s checks, shared with import_record()."""
    missing = [k for k in KEYS if k not in rec]
    if missing:
        return 'the record for %s lacks %s' % (commit, ', '.join(missing))
    if rec['schema'] != SCHEMA:
        return 'the record for %s is schema %r, not %d' % (commit, rec['schema'], SCHEMA)
    if rec['verdict'] != VERDICT:
        return 'the record for %s says %r, not OK' % (commit, rec['verdict'])
    if rec['commit'] != commit:
        return 'the record filed under %s names commit %s' % (commit, rec['commit'])
    try:
        _, tree, author = _facts(root, commit)
    except RecordError as e:
        return str(e)
    if rec['tree'] != tree:
        return 'the record for %s names tree %s; git says %s' % (commit, rec['tree'], tree)
    if rec['author'] != author:
        return 'the record for %s names author %r; git says %r' % (commit, rec['author'], author)
    if gate_config is not None and rec['gate_config'] != config_hash(gate_config):
        return 'the record for %s was made with another gate config' % commit
    if config_at is not None:
        rev = rec.get('config_rev')
        if not refs.is_sha(rev):
            return 'the record for %s names no config_rev (%r)' % (commit, rev)
        inside, why = refs.is_ancestor(rev, commit, root)
        if inside is not True:
            return 'the record for %s names config_rev %s, which is not in its history%s' % (
                commit, rev, ': %s' % why if why else '')
        try:
            cfg = config_at(rev)
        except RecordError as e:
            return str(e)
        if rec['gate_config'] != config_hash(cfg):
            return ('the record for %s was made with another gate config than its '
                    'config_rev %s holds' % (commit, rev[:10]))
    return None


def import_record(root, rec, commit, ran_on, config_at, repo_name):
    """Take `rec`, the record a load node's `gate.py --full` wrote for the full hash `commit`
    (the commit main pushed it), into this machine's record folder: `(path, record)` or
    RecordError naming why not. Checked here, never taken from the node: everything verify()
    checks with `config_at` (the hash of the gate config main's own kit reads at `config_rev`,
    which is `commit` itself, as for any branch gate), that it is a --full pass (full_of, mode
    "full") and that `repo` is `repo_name`. `ran_on` ({"node", "host_key", "job"}, from main's
    registry) is added; `input_keys` is set to null, so a landing serves no step from a run on
    another machine. A local record of the same commit is replaced, as a later pass replaces it."""
    if not isinstance(rec, dict):
        raise RecordError('the node sent no gate-OK record')
    if not refs.is_sha(commit):
        raise RecordError('a gate-OK record is imported for a full commit hash, not %r' % (commit,))
    ran_on = _ran_on(ran_on)
    if config_at is None:
        raise RecordError('an imported record is checked against the gate config at its config_rev')
    why = _check(root, commit, rec, config_at=config_at)
    if why is not None:
        raise RecordError(why)
    if rec.get('config_rev') != commit:
        raise RecordError('the record for %s names config_rev %s, not the commit itself'
                          % (commit, rec.get('config_rev')))
    if not full_of(rec) or mode_of(rec) != 'full':
        raise RecordError('the record for %s is not from a --full gate' % commit)
    if rec.get('repo') != repo_name:
        raise RecordError('the record for %s is of repository %r, not %r'
                          % (commit, rec.get('repo'), repo_name))
    out = dict(rec)
    out['input_keys'] = None
    out['ran_on'] = dict(ran_on)
    return write(root, out), out
