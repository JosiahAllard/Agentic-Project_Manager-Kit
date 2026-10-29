"""Where a landing's merged gate runs, main or a load node, and the land job that runs it there
(docs/load-nodes.md "A landing's merged gate on a node"). Main's side only: it imports landgate,
so job, dispatch and anything else the lock tool reaches never import it (every module the lock
tool reaches is a lock path, kitlib.lock.release.LOCK_PATHS).

  plan        the choice, made before the landing books main's lock: the nodes that opted the
              repository in, asked now, against main's own lock (route.choose with the land verb)
  route_line  the line to say for a plan: `gating here: <why>` or `gating on load node <n>: ...`
  recheck     at the landing's turn: the nodes asked again, the best that qualifies now, or main
  same_tree_why  why a one-branch landing gates on main: the tip's --full record has the merge's
              tree, so main's merged gate reuses its steps (kitlib.gate.reuse.same_tree) and a
              node's never does
  run_on_node the land job on one node: a gate Outcome land.py reads as it reads
              runner.merged_gate's, or Fallback
  verdict_of  how a land job ended: FALLBACK, NOT_LANDED or PASSED

A failure before the node started the job (no node qualifies, ssh or the push failed, the node
is busy or refused the spec, its kit's files are not main's, main's kit hash or main's own
selection cannot be read) raises Fallback: nothing ran there, and main may gate the merge
itself. So does the node's lock answering busy (the gate never ran). Once the node started the
job there is never a fallback, or the merge would be gated twice: every other way out is a
FAILED Outcome, NOT LANDED for the landing (the start's reply lost, job.EXIT_AFTER_START; the
connection lost, job.EXIT_LOST; the runner vanished; a red merged gate; a record main refused,
job.EXIT_RECORD, which includes a step skipped for a tool the node lacks; the wall-time ceiling).
Nothing here prints: `say(text)` gets every line.
"""
import collections
import os
import threading

from kitlib.loadnode import box, dispatch, job, landgate, registry, remote, route

LAND_VERBS = (box.LAND_VERB,)    # route.node_fits: the verbs a land job needs the node to serve
# The longest a land job may take, wall time from its push, before main stops it on the node and
# the landing does not land. An estimate, not measured: nothing in the kit records how long a
# merged gate may take.
CEILING_S = 4 * 3600
POLL_S = 5                       # how often the ceiling's watcher looks for a started job
FALLBACK, NOT_LANDED, PASSED = 'fallback', 'not landed', 'passed'

Route = collections.namedtuple('Route', 'where node why quiet nodes need')


def _main(why, quiet=False, nodes=(), need=None):
    return Route(route.MAIN, None, why, quiet, list(nodes), need)


def plan(root, repo, kind, store=None, merged_gate_fn=None, registry_file=None, runner=None,
         need=None, lock=None, is_kit=None):
    """A Route for the merged gate of a landing in the checkout `root` of the repository `repo`
    (runner.repo_name), booked as the lock kind `kind`: where MAIN or a node's name, node the
    registry Node or None, why the reason, quiet True when nothing needs saying (a kit checkout,
    a test's merged gate, no registry, no node opted in, a kind the lock does not name), nodes
    the opted-in Nodes and need the kind's slots here (for recheck). registry_file: the registry
    (None: registry.path(), and none on a test store); runner the ssh stand-in; need(kind, store),
    lock(store) and is_kit(root) the tests' stand-ins for route.need_of, dispatch.main_lock and
    kitlib.content.oncommit.is_kit."""
    if is_kit is None:
        from kitlib.content.oncommit import is_kit
    if is_kit(root):
        return _main('a kit checkout gates its landings on main', quiet=True)
    if merged_gate_fn is not None:
        return _main("the landing's merged gate is a test's", quiet=True)
    if store is None:
        from kitlib.lock.store import Store
        store = Store()
    path = registry_file
    if path is None and not getattr(store, 'testing', False):
        path = registry.path()
    if not path or not os.path.isfile(path):
        return _main('no load node registry', quiet=True)
    try:
        nodes = registry.load(path)
    except registry.RegistryError as e:
        return _main('the load node registry cannot be used: %s' % e)
    nodes = [n for n in nodes if repo in n.repos]
    if not nodes:
        return _main('no node has opted in %s' % repo, quiet=True)
    try:
        want = (need or (lambda k, s: route.need_of(k, store=s)))(kind, store)
    except KeyError:                    # an unknown kind: main's lock refuses it
        return _main('no kind %s' % kind, quiet=True)
    except Exception as e:
        return _main('the kind %s cannot be sized here (%s)' % (kind, e))
    try:
        mine = (lock or dispatch.main_lock)(store)
    except Exception as e:
        return _main("this machine's lock cannot be read (%s)" % type(e).__name__)
    results = remote.query_all(nodes, runner=runner or remote.default_runner)
    c = route.choose(nodes, results, repo, mine, want, verbs=LAND_VERBS)
    return Route(c.where, c.node, c.why, False, nodes, want)


def route_line(r):
    """The line to say for the Route `r`, or None when it is quiet."""
    if r.quiet:
        return None
    if r.node is None:
        return 'gating here: %s' % r.why
    return 'gating on load node %s: %s' % (r.where, r.why)


def recheck(nodes, repo, need, runner=None):
    """At the landing's turn: `nodes` (plan's) asked again, a route.Choice of the node with the
    most cpu slots free that qualifies now (route.node_fits with the land verb; a tie goes to the
    first in the registry), or MAIN with why each was passed over."""
    results = remote.query_all(list(nodes), runner=runner or remote.default_runner)
    by_name = {r.name: r for r in results}
    best, skipped = None, []
    for n in nodes:
        r = by_name.get(n.name)
        why = route.node_fits(n, r, repo, need, LAND_VERBS)
        if why:
            skipped.append('%s: %s' % (n.name, why))
            continue
        free = r.reply['lock'].get('cpu_free', 0)
        if best is None or free > best[0]:
            best = (free, n)
    if best is None:
        return route.Choice(route.MAIN, None, 'no load node qualifies now (%s)'
                            % ('; '.join(skipped) or 'none opted in'))
    return route.Choice(best[1].name, best[1], '%s has %d cpu slots free' % (best[1].name, best[0]))


def same_tree_why(root, cfg, branch, tip, base, merge, batch=1, fresh=False, full=False):
    """Why the merged gate of `merge` gates on main, or None: a one-branch landing (batch 1, not
    --fresh or --full) whose tip's --full record has the merge's tree, so main's merged gate
    serves its steps from that record (kitlib.gate.reuse.same_tree) and a node's never does.
    None when git cannot tell: main's gate then runs every step as well."""
    if batch != 1 or fresh or full:
        return None
    from kitlib.gate import reuse
    try:
        same, line = reuse.same_tree(root, cfg, branch, tip, base, merge)
    except Exception:                   # noqa: BLE001 - main would run every step too
        return None
    if same is None:
        return None
    return '%s; main reuses its steps and a load node never does' % line


class Fallback(Exception):
    """Nothing ran on the node: main may gate the merge itself. why: the line to say; code: the
    land job's exit code, or None."""

    def __init__(self, why, code=None):
        Exception.__init__(self, why)
        self.why, self.code = why, code


def verdict_of(nj, code=None, error=None, expired=False):
    """How the land job `nj` (None: it was never made) ended, from run()'s `code` or the
    job.JobError or OSError it raised (`error`), `expired` when the ceiling stopped it: PASSED
    (exit 0 and main took its record), FALLBACK (the module docstring) or NOT_LANDED."""
    started = nj is not None and nj.started
    if expired and started:
        return NOT_LANDED
    if isinstance(error, job.StartUnknown):
        return NOT_LANDED
    if isinstance(error, job.JobError) and error.fallback:     # before the start, or busy
        return FALLBACK
    if error is not None:
        return NOT_LANDED if started else FALLBACK
    if code == 0 and nj is not None and isinstance(nj.land_record, dict):
        return PASSED
    return NOT_LANDED


def outcome_of(nj, merge):
    """The OK gate Outcome of the land job `nj`, which passed and whose record main took: land.py
    reads it as runner.merged_gate's (verdict, results, docs, source, head), with ran_on, node
    and job for the record the landing writes."""
    from kitlib import gate as G
    from kitlib.gate import runner
    from kitlib.records import gate_ok
    rec = nj.land_record
    results = [{'name': s.get('name'), 'status': str(s.get('result')).upper(),
                'seconds': s.get('seconds') or 0, 'reasons': []} for s in rec.get('steps') or ()]
    _ok, line = runner.verdict(results)
    return G.Outcome(G.OK, [line, 'ran on load node %s as job %s' % (nj.node.name, nj.job)],
                     verdict=line, results=results, docs=gate_ok.mode_of(rec) == 'docs',
                     source=gate_ok.source_of(rec), head=merge, log_dir=None, ran_on=nj.ran_on,
                     node=nj.node.name, job=nj.job, record=rec)


def _not_landed(nj, node, code, error, expired, ceiling_s):
    """The FAILED gate Outcome of a land job that started and did not pass. Its lines never say
    NOT LANDED: the landing adds that line itself."""
    from kitlib import gate as G
    j = nj.job if nj is not None else '?'
    if expired:
        why = 'job %s ran past the ceiling of %d s and was stopped' % (j, ceiling_s)
    elif error is not None:
        why = 'job %s: %s' % (j, getattr(error, 'message', None) or error)
    elif code == job.EXIT_LOST:
        why = 'the connection was lost; job %s may still run there' % j
    elif code == job.EXIT_AFTER_START:
        why = 'job %s ended with no exit code of its own, or its start had no reply' % j
    elif code == job.EXIT_RECORD:
        why = 'the merged gate passed there, and main did not take its record (job %s)' % j
    else:
        why = 'the merged gate failed there (job %s, exit %s)' % (j, code)
    line = 'GATE FAILED on load node %s: %s' % (node.name, why)
    lines = [line, 'the merge is not gated again on main: the job started on the node']
    if nj is not None and code in (job.EXIT_LOST, job.EXIT_AFTER_START, job.EXIT_RECORD):
        lines.extend(nj.follow_lines())
    return G.Outcome(G.FAILED, lines, verdict=line, results=[], log_dir=None, ran_on=None,
                     node=node.name, job=None if nj is None else nj.job, node_exit=code)


def _stop(nj, say):
    try:
        d = nj.stop()
        asked = 'had ended already' if d.get('ended') else 'asked to stop' if d.get('ok') \
            else 'not stopped: %s' % str(d.get('error'))[:200]
    except (job.JobError, OSError) as e:
        asked = 'not stopped: %s' % e
    say('job %s on %s %s' % (nj.job, nj.node.name, asked))


def _watch(holder, done, state, ceiling_s, poll_s, say):
    """The ceiling: once `ceiling_s` passed with the job not done, mark it expired and stop it as
    soon as it started."""
    if done.wait(ceiling_s):
        return
    state['expired'] = True
    while not done.is_set():
        nj = holder[0] if holder else None
        if nj is not None and nj.started:
            say('job %s on %s reached the ceiling of %d s' % (nj.job, nj.node.name, ceiling_s))
            _stop(nj, say)
            return
        done.wait(poll_s)


def run_on_node(node, root, merge, base, cfg, repo, slug, full=False, docs=False, tree=None,
                kind=None, say=None, out=None, open_link=None, make_job=None, checker=None,
                kit_hash=None, ceiling_s=CEILING_S, poll_s=POLL_S, notice_dir=None,
                tool=dispatch.NODE_TOOL, job_kw=None):
    """The merged gate of the landing's merge `merge` (a full hash, in `tree`, the landing tree,
    default `root`) on `base`, run on `node` as a land job: an OK gate Outcome (outcome_of) when
    it passed and main took its record, a FAILED one when it started and did not (the module
    docstring), Fallback when nothing ran there. cfg: the config read at the base; repo:
    runner.repo_name; full, docs: the merged gate's own arguments. open_link(node) is a context
    giving a remote.Link (default remote.Link), make_job(link) the job.NodeJob, checker and
    kit_hash landgate's (default: read here), job_kw more NodeJob arguments: the tests'
    stand-ins. Ctrl+C asks the node to stop a started job and says how to follow it; before the
    start it says how to stop the job should it have started; either way it goes on up."""
    say = say or (lambda text: None)
    if kit_hash is None:
        try:
            kit_hash = landgate.kit_files()
        except Exception as e:          # noqa: BLE001 - told as its kind only
            raise Fallback("main's kit files cannot be read (%s)" % type(e).__name__)
    if checker is None:
        try:
            checker = landgate.checker(root, merge, base, cfg, repo, full, docs, tree=tree,
                                       kit_hash=kit_hash)
        except (job.RecordRefused, OSError, ValueError) as e:
            raise Fallback(str(e))
    land = {'base': base, 'remote': cfg.remote, 'main': cfg.main_branch, 'full': bool(full),
            'docs': bool(docs), 'kit_files': kit_hash}
    if make_job is None:
        def make_job(link):
            return job.NodeJob(node, link, tree or root, merge, repo, slug, kind, [], land=land,
                               checker=checker, say=say, out=out, notice_dir=notice_dir,
                               tool=tool, **(job_kw or {}))
    holder, done, state = [], threading.Event(), {'expired': False}
    watcher = threading.Thread(target=_watch, args=(holder, done, state, ceiling_s, poll_s, say),
                               daemon=True)
    watcher.start()
    code = error = None
    try:
        # every stop goes out while the link is open: its pinned known_hosts goes with it
        with (open_link or remote.Link)(node) as link:
            holder.append(make_job(link))
            try:
                code = holder[0].run()
            except KeyboardInterrupt:
                done.set()
                watcher.join()
                nj = holder[0]
                if nj.started:
                    _stop(nj, say)
                    for line in nj.follow_lines():
                        say(line)
                else:   # its start may be on the node already, its reply not back yet
                    say('Ctrl+C: job %s was not seen to start on %s; if it did, stop it: %s stop %s %s'
                        % (nj.job, node.name, getattr(nj, 'tool', tool), node.name, nj.job))
                raise
            finally:
                done.set()
                watcher.join()
    except (job.JobError, OSError) as e:
        error = e
    finally:
        done.set()
    nj = holder[0] if holder else None
    v = verdict_of(nj, code, error, state['expired'])
    if v == PASSED:
        return outcome_of(nj, merge)
    if v == FALLBACK:
        if isinstance(error, job.JobError):
            raise Fallback(error.message, error.code)
        raise Fallback('%s could not be used: %s' % (node.name, error), job.EXIT_NODE)
    return _not_landed(nj, node, code, error, state['expired'], ceiling_s)
