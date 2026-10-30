"""A landing's merged gate on a load node (docs/load-nodes.md "A landing's merged gate on a
node"): the side of a land job that reads the gate and record modules, kept out of box, job and
dispatch for the reason gaterecord is (every module the lock tool reaches is a lock path,
kitlib.lock.release.LOCK_PATHS). Only optional/loadnode/loadnode.py (the node's `job` and
`landgate` runs) and a landing on main import this.

  node      node_main / land_gate run runner.merged_gate on the job's tree (the merge, checked
            out on its base) exactly as main's landing would, and write the merge's gate-OK
            record there; read_record is box.run_land's verify; kit_files is the node kit's
            files hash box.land compares with the spec's
  main      expect reads what main's own merged gate would select (runner.merged_steps) and
            check_record checks the node's record against it, field by field (NodeJob's checker)

A node's record is never trusted: main takes it only when its commit, tree and author are what
main's git says, its gate config hash is the one main reads at the base, its base and config_rev
are the base, its mode, source and step names are what main's merged gate would have run, every
step passed or warned (a step skipped for a missing tool, or served from a gate cache, does not
count as run), its repo is main's and its kit's files hash is main's kit's.
"""
from kitlib.loadnode import box
from kitlib.loadnode.job import RecordRefused

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 4
RAN = ('pass', 'warn')      # the step results a node's record may hold


def kit_files():
    """This kit's kitlib.gate.reuse.kit_files_hash: the files a land job's two sides compare."""
    from kitlib.gate import reuse
    return reuse.kit_files_hash()


def read_record(root, commit):
    """The merge's gate-OK record in the job's tree, as gate_ok.verify reads it: (record, None)
    or (None, why). box.run_land's verify."""
    from kitlib.records import gate_ok
    return gate_ok.verify(root, commit)


def _say(line):
    print(line, flush=True)


def node_main(job, base_dir=None, say=None):
    """`loadnode.py landgate <job>`: the land job's merged gate in its tree; the gate's exit code
    (2 when the job is no land job)."""
    say = say or _say
    b = box.Box(base_dir)
    spec = None
    if box.JOB_RE.fullmatch(job or ''):
        spec = box._read_json(b._p(job, 'spec.json'))
    try:
        spec = box.check_land_spec(spec if isinstance(spec, dict) else {})
    except box.SpecError as e:
        say('REFUSED: job %s is no land job this node can run (%s)' % (job, e))
        return EXIT_USAGE
    return land_gate(b.tree(spec), spec, say)


def land_gate(wt, spec, say, merged_gate=None, kit_hash=None):
    """Run the merged gate of the merge spec['sha'] checked out in `wt` against spec['base'],
    with spec's full and docs, as main's landing runs it (runner.merged_gate, the config read
    at the base); on a pass, write its gate-OK record in `wt` with this kit's files hash in its
    `kit`. The exit code: 0 passed and recorded, 1 failed (or the record could not be written,
    or the gate left the tree changed), 4 refused (another kit, the tree is not at the merge,
    the config cannot be read), or the gate's own code. merged_gate and kit_hash: for tests."""
    import kitlib.config as kconfig
    from kitlib.config.types import ConfigError
    from kitlib.gate import runner
    from kitlib.git import cmd, refs
    from kitlib.records import gate_ok
    sha, base = spec['sha'], spec['base']
    full, docs = spec['full'] is True, spec['docs'] is True
    mine = (kit_hash or kit_files)()
    if mine != spec['kit_files']:
        say("REFUSED: this node's kit files are not main's; a landing's merged gate runs only on "
            "exactly main's kit")
        return EXIT_REFUSED
    head, why = refs.head(wt)
    if head != sha:
        say('REFUSED: the job tree is at %s, not the merge %s (%s)'
            % (str(head)[:12], sha[:12], why or 'moved'))
        return EXIT_REFUSED
    try:
        cfg = kconfig.load_at(wt, base)
    except ConfigError as e:
        say('REFUSED: the config at the base %s: %s' % (base[:10], e))
        return EXIT_REFUSED
    started = gate_ok.utc()
    out = (merged_gate or runner.merged_gate)(wt, cfg, base, full=full, slug=spec['slug'],
                                              say=say, tree=wt, docs=docs)
    for line in out.lines:
        say(line)
    if out.code != EXIT_OK:
        return out.code
    head, why = refs.head(wt)
    status, swhy = cmd.run_text(['--no-optional-locks', 'status', '--porcelain'], wt)
    if head != sha or status is None or status:
        say('GATE FAILED: the merged gate left the job tree %s; no gate-OK record is written'
            % ('at %s' % str(head)[:12] if head != sha else
               'unreadable (%s)' % swhy if status is None else 'changed'))
        return EXIT_FAILED
    docs = bool(out.facts.get('docs'))
    try:
        gate_ok.record_pass(wt, sha, runner.gate_config(cfg),
                            dict(runner.kit_identity(), files=mine), runner.repo_name(cfg, wt),
                            started, runner.record_steps(out.facts.get('results') or []),
                            base=base, config_rev=base, source=out.facts.get('source'),
                            full=full and not docs, mode='docs' if docs else None)
    except gate_ok.NoRecordFolder:
        # Kept for load nodes set up without a dev_drive: the .git copy, which read_record reads.
        pass
    except gate_ok.RecordError as e:
        say('GATE FAILED: the gate-OK record could not be written: %s' % e)
        return EXIT_FAILED
    return EXIT_OK


# -- main ----------------------------------------------------------------------------------------

def expect(root, cfg, base, full, docs, tree=None):
    """What main's own merged gate of HEAD of `tree` (default `root`) on `base` would record, with
    `cfg` the config read at the base: ({'head', 'steps': [names], 'mode', 'source', 'full',
    'gate_config': its hash}, None), or (None, why) when main's gate would refuse it."""
    from kitlib.gate import runner
    from kitlib.records import gate_ok
    info, why = runner.merged_steps(root, cfg, base, full=full, tree=tree, docs=docs)
    if info is None:
        return None, why
    docs = bool(info['docs'])
    full = bool(full) and not docs
    return {'head': info['head'], 'steps': list(info['steps']),
            'mode': 'docs' if docs else 'full' if full else 'select', 'full': full,
            'source': info['source'],
            'gate_config': gate_ok.config_hash(runner.gate_config(cfg))}, None


def check_record(root, rec, merge, base, expected, repo_name, kit_hash, gate_config):
    """`rec`, the record a land job's node wrote for the merge `merge` (a full hash), when main
    takes it: checked against main's git in `root` (commit, tree, author), the gate config main
    read at `base` (`gate_config`, runner.gate_config of it), `expected` (expect()'s dict), main's
    repository name and main's kit files hash. RecordRefused naming the first check it fails."""
    from kitlib.records import gate_ok
    if not isinstance(rec, dict):
        raise RecordRefused('the node sent no gate-OK record')
    if rec.get('commit') != merge:
        raise RecordRefused('the record names commit %s, not the merge %s'
                            % (str(rec.get('commit'))[:12], merge[:12]))
    if expected.get('head') not in (None, merge):
        raise RecordRefused("main's selection was read at %s, not the merge %s"
                            % (str(expected.get('head'))[:12], merge[:12]))
    if rec.get('input_keys') is not None:
        raise RecordRefused('the record has input_keys; a landing serves no step from it')
    if 'ran_on' in rec:
        raise RecordRefused('the record carries ran_on, which only main writes')
    why = gate_ok._check(root, merge, rec, gate_config=gate_config)
    if why is not None:
        raise RecordRefused(why)
    if rec['gate_config'] != expected['gate_config']:
        raise RecordRefused('the record was made with another gate config than main reads at '
                            'the base')
    if rec.get('base') != base:
        raise RecordRefused('the record names base %s, not %s' % (str(rec.get('base'))[:12],
                                                                  base[:12]))
    if rec.get('config_rev') != base:
        raise RecordRefused('the record names config_rev %s, not the base %s'
                            % (str(rec.get('config_rev'))[:12], base[:12]))
    mode = gate_ok.mode_of(rec)
    if mode != expected['mode'] or gate_ok.full_of(rec) != expected['full']:
        raise RecordRefused('the record is a %s gate (full %r); main would run a %s gate'
                            % (mode, rec.get('full'), expected['mode']))
    if gate_ok.source_of(rec) != expected['source']:
        raise RecordRefused('the record says the gate came from %r; main reads %r'
                            % (gate_ok.source_of(rec), expected['source']))
    steps = rec.get('steps')
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise RecordRefused('the record has no list of steps')
    names = [s.get('name') for s in steps]
    if sorted(map(str, names)) != sorted(expected['steps']) or len(set(names)) != len(names):
        raise RecordRefused('the record ran %s; main would run %s'
                            % (', '.join(map(str, names)) or 'no step',
                               ', '.join(expected['steps']) or 'no step'))
    bad = ['%s (%s)' % (s.get('name'), s.get('result')) for s in steps if s.get('result') not in RAN]
    if bad:
        raise RecordRefused('steps that did not run and pass on the node: %s' % ', '.join(bad))
    if rec.get('repo') != repo_name:
        raise RecordRefused('the record is of repository %r, not %r' % (rec.get('repo'), repo_name))
    kit = rec.get('kit')
    if not isinstance(kit, dict) or kit.get('files') != kit_hash:
        raise RecordRefused("the record's kit files are not main's kit's")
    return rec


def checker(root, merge, base, cfg, repo_name, full, docs, tree=None, kit_hash=None):
    """NodeJob's checker for a landing's land job: record -> the record, RecordRefused. What
    main's merged gate would select is read once, here (RecordRefused when main's gate would
    refuse the merge)."""
    from kitlib.gate import runner
    expected, why = expect(root, cfg, base, full, docs, tree=tree)
    if expected is None:
        raise RecordRefused("main's own merged gate would refuse the merge: %s" % why)
    if expected['head'] != merge:
        raise RecordRefused('the landing tree is at %s, not the merge %s'
                            % (str(expected['head'])[:12], merge[:12]))
    gate_config = runner.gate_config(cfg)
    mine = kit_hash if kit_hash is not None else kit_files()

    def check(rec):
        return check_record(tree or root, rec, merge, base, expected, repo_name, mine, gate_config)
    return check

