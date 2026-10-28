"""`node.py run --node auto`: where a job of one kind runs, the main machine or a load node
(docs/load-nodes.md "Choosing a machine").

A machine qualifies when its machine lock has the job's slots free now and nobody waits there:
cpu_free and gpu_free at least the kind's need, waiting 0. On the main machine the need is the
kind's size in main's layout (kitlib.lock.layout.need_for). A node's lock has its own slots, so a
node's need is the same size capped at its slots (a heavy kind takes the node's whole cpu pool),
and a kind that needs a gpu needs one there. A node also qualifies only when it answered OK,
serves the run verbs, lists the repository in its registry entry's `repos` (the owner's opt-in),
runs fewer jobs than its registry `slots`, its lock is fully readable with no layout change
pending, and, when it says, its free memory is at least its registry memory_mb shared out over its
slots. memory_mb is only this check: what a job needs is the owner's to know.

Of those that qualify the one with the most free cpu slots wins; a tie goes to the main machine,
then to the node first in the registry. When none qualifies the job runs on the main machine,
whose lock queues it. Nothing here touches the network or prints: the caller asks the nodes
(kitlib.loadnode.remote.query_all) and passes their results in.
"""
import collections

from kitlib.loadnode import box, remote

MAIN = 'main'

Choice = collections.namedtuple('Choice', 'where node why')     # where: MAIN or a node's name


def need_of(kind, store=None, table=None, layout=None):
    """{'cpu', 'gpu'}: the slots a run of `kind` books on this machine. KeyError for a kind the
    kinds table does not name; kitlib.lock.kinds.KindsError for one it cannot size."""
    from kitlib.lock import kinds as kinds_mod
    from kitlib.lock import layout as lay
    if table is None or layout is None:
        from kitlib.lock.store import Store
        store = store or Store()
        table = table if table is not None else kinds_mod.load(store.kinds_path)
        layout = layout if layout is not None else lay.in_force(store, adopt=False)['layout']
    return lay.need_for(layout, table[kind])


def fits(lock, need):
    """'' when the lock `lock` (a reply's lock dict) has `need` free now, else why not."""
    if lock.get('waiting'):
        return '%d run%s waiting' % (lock['waiting'], '' if lock['waiting'] == 1 else 's')
    if lock.get('cpu_free', 0) < need['cpu']:
        return '%d of %d cpu slots free' % (lock.get('cpu_free', 0), need['cpu'])
    if lock.get('gpu_free', 0) < need['gpu']:
        return '%d of %d gpu slots free' % (lock.get('gpu_free', 0), need['gpu'])
    return ''


def node_need(need, lock):
    """The main machine's `need` on a node whose lock is `lock`: capped at its slots. None when
    the kind needs a gpu and the node has none."""
    if need['gpu'] and not lock.get('gpu_slots'):
        return None
    return {'cpu': min(need['cpu'], lock.get('cpu_slots', 0)),
            'gpu': min(need['gpu'], lock.get('gpu_slots', 0))}


def node_fits(node, result, repo, need):
    """'' when `node` (a registry Node, `result` its remote.Result) may take a job of `repo` with
    main's `need` now, else why not."""
    if repo not in node.repos:
        return 'not opted in for %s' % repo
    if result is None or result.state != remote.OK:
        return result.state if result else 'not asked'
    rep = result.reply
    if not set(box.RUN_VERBS) <= set(rep.get('verbs') or ()):
        return 'its kit serves no run verbs (update it)'
    lock = rep['lock']
    if lock.get('unreadable') or lock.get('pending'):
        return 'its lock is not fully readable or a layout change is pending'
    jobs = rep.get('jobs') or 0
    if jobs >= node.slots:
        return 'runs %d job%s, its most' % (jobs, '' if jobs == 1 else 's')
    free = ((rep.get('hardware') or {}).get('memory_mb') or {}).get('free')
    want = node.memory_mb // max(1, node.slots)
    if free is not None and free < want:
        return '%d MB memory free of %d' % (free, want)
    n = node_need(need, lock)
    if n is None:
        return 'no gpu'
    return fits(lock, n)


def choose(nodes, results, repo, main_lock, need):
    """A Choice for a job of `repo` that books `need` on main (need_of). `nodes` the registry's
    Nodes, `results` their remote.Results in any order, `main_lock` main's own reply lock dict
    (kitlib.loadnode.reply.build of api.snapshot)."""
    by_name = {r.name: r for r in results}
    best = None
    why_main = fits(main_lock, need)
    if not why_main:
        best = (main_lock.get('cpu_free', 0), MAIN, None)
    skipped = []
    for n in nodes:
        r = by_name.get(n.name)
        why = node_fits(n, r, repo, need)
        if why:
            skipped.append('%s: %s' % (n.name, why))
            continue
        free = r.reply['lock'].get('cpu_free', 0)
        if best is None or free > best[0]:
            best = (free, n.name, n)
    passed = ''.join('; ' + s for s in skipped)
    if best is None:
        return Choice(MAIN, None, 'no machine has the slots free now (main: %s%s); main queues it'
                      % (why_main, passed))
    if best[1] == MAIN:
        return Choice(MAIN, None, 'main has %d cpu slots free%s' % (best[0], passed))
    return Choice(best[1], best[2], '%s has %d cpu slots free (main: %s%s)'
                  % (best[1], best[0], why_main or 'fewer', passed))
