"""The gate job's side that reads the gate and record modules (docs/load-nodes.md "Gating a
branch on a node"), kept out of box, job and dispatch: the lock tool imports those
(tools/machine_lock.py, `run` sending a run to a node) and every module it reaches at any depth is
a lock path (kitlib.lock.release.LOCK_PATHS, tests/lock/test_release.py LockPaths), so a gate or
record module imported there would make each of them one. Only tools/node.py (main) and
optional/loadnode/loadnode.py `job` (the node's job runner) import this.

  main      gate_facts and repo_of read what the checkout's own gate would; import_gate_record
            takes a node's record into the checkout's record folder (NodeJob's importer)
  node      read_record reads the record the node's gate.py --full wrote (box.run_gate's verify)
"""
from kitlib.loadnode import registry
from kitlib.loadnode.job import EXIT_USAGE, JobError, RecordRefused


def read_record(root, commit):
    """The gate-OK record of a --full pass of `commit` in the checkout `root`, as
    gate_ok.verify_full reads it: (record, None) or (None, why)."""
    from kitlib.records import gate_ok
    return gate_ok.verify_full(root, commit)


def gate_facts(top):
    """What a gate job of the checkout `top` sends: {repo, kind, remote, main, base, slug}, read
    as its own gate reads them (its `.kit.json`, runner.repo_name, the gate's lock kind, the
    default base: kitlib.git.refs.default_base, as a full hash). JobError(EXIT_USAGE) naming
    what cannot be read or does not resolve."""
    import kitlib.config as kconfig
    from kitlib.config.types import ConfigError
    from kitlib.gate import runner
    from kitlib.git import refs
    try:
        cfg = kconfig.load(top)
        kind = cfg.section('gate').get('kind')
    except ConfigError as e:
        raise JobError(EXIT_USAGE, 'the config of %s cannot be read: %s' % (top, e))
    ref, _note = refs.default_base(top, cfg.remote, cfg.main_branch)
    base, why = refs.resolve(ref, top)
    if not base:
        raise JobError(EXIT_USAGE, 'the default base of %s does not resolve (%s): neither %s/%s '
                       'nor %s' % (top, why, cfg.remote, cfg.main_branch, cfg.main_branch))
    branch, _ = refs.current_branch(top)
    slug = runner.slug_of(branch or 'detached')
    return {'repo': runner.repo_name(cfg, top), 'kind': kind, 'remote': cfg.remote or '',
            'main': cfg.main_branch, 'base': base,
            'slug': slug if registry.repo_ok(slug) else 'gate'}


def repo_of(top):
    """The repository name the gate of the checkout `top` writes in its records
    (runner.repo_name), or None when its config cannot be read."""
    import kitlib.config as kconfig
    from kitlib.config.types import ConfigError
    from kitlib.gate import runner
    try:
        return runner.repo_name(kconfig.load(top), top)
    except ConfigError:
        return None


def import_gate_record(top, rec, commit, ran_on, repo_name):
    """Take the gate-OK record `rec` a node's gate wrote for `commit` into the record folder of
    the checkout `top` (kitlib.records.gate_ok.import_record, checked against the gate config main
    reads at the commit): `(path, [warnings])`; a refusal is job.RecordRefused. With no record folder (no
    dev_drive) only the .git copy is written: its path, with a warning, as the gate itself does.
    A record whose kit commit is not main's kit's is taken with a warning: the config hash
    check is what refuses."""
    from kitlib.gate import runner
    from kitlib.records import gate_ok

    def config_at(rev):
        try:
            return runner.gate_config_at(top, rev)
        except runner.ConfigError as e:
            raise gate_ok.RecordError('the gate config at %s: %s' % (rev[:10], e))
    warnings = []
    try:
        path, _out = gate_ok.import_record(top, rec, commit, ran_on, config_at, repo_name)
    except gate_ok.NoRecordFolder as e:            # a RecordError too: caught first
        path = gate_ok.legacy_path(top, commit)
        warnings.append('the record could not go to the record folder (%s); .git copy only' % e)
    except gate_ok.RecordError as e:
        raise RecordRefused(str(e)) from e
    theirs = (rec.get('kit') or {}).get('commit') if isinstance(rec.get('kit'), dict) else None
    try:
        mine = runner.kit_identity().get('commit')
    except Exception:                   # noqa: BLE001 - only a warning rides on it
        mine = None
    if theirs and mine and theirs != mine:
        warnings.append("the node's kit is at %s and this machine's at %s; the record's gate "
                        "config matched, so it is taken" % (str(theirs)[:12], str(mine)[:12]))
    return path, warnings
