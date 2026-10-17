"""The gate-OK record: the gate's word that it passed exactly one commit (ARCHITECTURE.md
"Gate-OK record").

gate.py writes `<git common dir>/kit-gate-ok/<commit>.json` for the commit it passed; the kit guard and
the pre-push hook read it. The common dir is shared by every checkout of the repository and is
never tracked. One record per commit; a later pass of the same commit replaces it (atomically:
a temp file in the same folder, then os.replace).

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

verify() re-derives everything git can answer: the record's file name, `commit`, `tree` and
`author` must match the repository now, and `gate_config` must match the caller's config when it
passes one. With `config_at` (rev -> that commit's gate config) it instead requires `config_rev`,
a commit in `commit`'s history (or `commit` itself), and the hash of config_at(config_rev). A
record is evidence for a reader that trusts the git dir; it is not a signature.
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
ADDED_KEYS = ('config_rev', 'source', 'full')
RETRIES = 5
RETRY_SLEEP = 0.2


class RecordError(Exception):
    """A record could not be written, or git could not answer what writing it needs."""


def utc(when=None):
    return (when or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def config_hash(gate_config):
    """sha256 hex of the canonical JSON of `gate_config` (any JSON value)."""
    return hashlib.sha256(canonical_json(gate_config).encode('utf-8')).hexdigest()


def record_dir(root):
    common, why = repo.common_dir(root)
    if not common:
        raise RecordError('no git common dir for %s: %s' % (root, why))
    return os.path.join(common, DIR_NAME)


def record_path(root, commit):
    if not refs.is_sha(commit):
        raise RecordError('a gate-OK record is named by a full commit hash, not %r' % (commit,))
    return os.path.join(record_dir(root), commit + '.json')


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


def build(root, rev, gate_config, kit, repo_name, started_utc, steps, base=None, ended_utc=None,
          config_rev=None, source=None, full=False):
    """The record for `rev` (resolved here: commit, tree and author come from git, never the
    caller). `config_rev`: the commit the gate config was read from (resolved to a full hash).
    `source`: "gate.json" or "auto" (kitlib.gate.detect), or None. `full`: the gate ran --full."""
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
        'config_rev': config_rev, 'source': source, 'full': full is True,
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
    """Write `record` (from build()) atomically; returns its path."""
    path = record_path(root, record.get('commit'))
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
                config_rev=None, source=None, full=False):
    """build() then write(): `(path, record)`. Only a passing gate calls this."""
    rec = build(root, rev, gate_config, kit, repo_name, started_utc, steps, base,
                config_rev=config_rev, source=source, full=full)
    return write(root, rec), rec


def read(root, commit):
    """`(record, None)`, `(None, 'none')` when there is no record, or `(None, why)` when the file
    is there and cannot be read as one."""
    try:
        path = record_path(root, commit)
    except RecordError as e:
        return None, str(e)
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return None, 'none'
    except OSError as e:
        return None, 'cannot read %s: %s' % (path, e)
    try:
        rec = json.loads(text)
    except ValueError as e:
        return None, '%s is not a gate-OK record: %s' % (path, e)
    if not isinstance(rec, dict):
        return None, '%s is not a gate-OK record' % path
    return rec, None


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
