"""kitlib.gate.land with a landing's merged gate routed to a load node (docs/load-nodes.md "A
landing's merged gate on a node"): the route chosen before the booking, a 1-slot run while the
node gates, the node's pass written with ran_on, its failure and any error NOT LANDED with no
second gate, and every fallback to main releasing the 1-slot run before it books main's full run.
The node side is a stand-in for kitlib.loadnode.landroute; the landing, git, the remote and the
scratch lock store are real."""
import collections
import json
import threading
import time
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land
from kitlib.lock import queue as lockqueue
from kitlib.lock import run as lockrun
from kitlib.loadnode import landroute, route
from kitlib.records import gate_ok
from tests.gate.test_land import BRANCH, HANG_S, LandCase

Node = collections.namedtuple('Node', 'name')
N1, N2 = Node('n1'), Node('n2')
NEED = {'cpu': 2, 'gpu': 0}
RAN_ON = {'node': 'n1', 'host_key': 'SHA256:pinned', 'job': 'j-0001'}
NEED_BASE = {'cpu': 4, 'gpu': 0}
SAME = 'the tip has the merge\'s tree; main reuses its steps and a load node never does'


class Job(object):
    """A land job that passed and whose record main took (what landroute.outcome_of reads)."""

    def __init__(self, node):
        self.node, self.job = node, 'j-0001'
        self.ran_on = dict(RAN_ON, node=node.name)
        self.started = True
        self.land_record = {'steps': [{'name': 'unit', 'result': 'pass', 'seconds': 1}],
                            'source': 'gate.json', 'full': False, 'mode': 'select'}

    def follow_lines(self):
        return ['follow it: node.py follow %s %s' % (self.node.name, self.job)]


def passing(node, merge, kw):
    return landroute.outcome_of(Job(node), merge)


def failing(node, merge, kw):
    return landroute._not_landed(Job(node), node, 1, None, False, landroute.CEILING_S)


class Routes(object):
    """landroute's stand-in: plan routes to `where` (None: main, said), recheck picks `pick`
    (None: no node qualifies), same_tree_why says `same`, run_on_node is `run`."""
    Fallback = landroute.Fallback
    route_line = staticmethod(landroute.route_line)

    def __init__(self, where=N1, pick=N1, same=None, run=passing):
        self.where, self.pick, self.same, self.run = where, pick, same, run
        self.need = NEED_BASE
        self.calls = []

    def names(self):
        return [c[0] for c in self.calls]

    def plan(self, root, repo, kind, store=None, merged_gate_fn=None):
        self.calls.append(('plan', repo, kind))
        if self.where is None:
            return landroute.Route(route.MAIN, None, 'n1: busy', False, [N1], NEED)
        return landroute.Route(self.where.name, self.where, '%s has 3 cpu slots free'
                               % self.where.name, False, [N1, N2], NEED)

    def need_of(self, kind, store):
        self.calls.append(('need_of', kind))
        if isinstance(self.need, Exception):
            raise self.need
        return self.need

    def recheck(self, nodes, repo, need):
        self.calls.append(('recheck', [n.name for n in nodes], repo, need))
        if self.pick is None:
            return route.Choice(route.MAIN, None, 'no load node qualifies now (n1: busy)')
        return route.Choice(self.pick.name, self.pick, '%s has 2 cpu slots free' % self.pick.name)

    def same_tree_why(self, root, cfg, branch, tip, base, merge, batch=1, fresh=False,
                      full=False):
        self.calls.append(('same_tree', branch, tip, base, merge, batch, fresh, full))
        return self.same

    def run_on_node(self, node, root, merge, base, cfg, repo, slug, **kw):
        self.calls.append(('run', node.name, merge, base, repo, slug, kw))
        return self.run(node, merge, kw)


def fallback(node, merge, kw):
    raise landroute.Fallback('%s did not take the job: busy' % node.name, 4)


def broken(node, merge, kw):
    raise RuntimeError('boom')


class NodeCase(LandCase):
    def setUp(self):
        LandCase.setUp(self)
        self.said, self.seen = [], []
        self.checks = []        # fn(n, kw), called before the n-th booking books

    def book(self, kind, repo, slug, **kw):
        """Lander(book=): records what the landing books (its book_kw has no cores, so a cores
        here is the landing's own), then books for real with one slot when none was named (the
        scratch store keeps runs to one slot)."""
        self.seen.append(dict(kw, kind=kind, slug=slug, said=len(self.said)))
        for check in self.checks:
            check(len(self.seen), kw)
        return lockrun.book(kind, repo, slug, **dict(kw, cores=kw.get('cores') or 1))

    def node_lander(self, routes, tip, **kw):
        kw.setdefault('book_kw', {'store': self.store, 'env': self.env})
        kw.setdefault('say', self.said.append)
        branch = kw.pop('branch', BRANCH)
        return self.lander(branch, self.main, tip, node_route=routes, book=self.book, **kw)

    def landing_books(self):
        return [s for s in self.seen if s['slug'] == 'kit-feature']

    def no_main_gate(self):
        p = mock.patch.object(land.runner, 'merged_gate',
                              side_effect=AssertionError("main's merged gate ran"))
        p.start()
        self.addCleanup(p.stop)

    def assert_full_rebook(self, lander):
        first, second = self.landing_books()
        self.assertEqual(first['cores'], 1)
        self.assertNotIn('cores', second)       # the kind's own size
        for b in (first, second):
            self.assertEqual(b['resource'], land.queue_key(lander.lock.common))
            self.assertEqual(b['priority'], 'land')
            self.assertTrue(b['gate'])
        self.assertIsNone(second['wait_s'])
        self.assertFalse(second['no_wait'])
        line = [s for s in self.said if s.startswith("releasing the landing's 1-slot run")]
        self.assertEqual(len(line), 1, self.said)
        self.assertLess(self.said.index(line[0]), second['said'])

    def nothing_held(self, n, kw):
        """A check: at the landing's second booking (main's full run after a fallback), nothing
        in the machine lock holds the landing resource. Booked beside the 1-slot run it would
        wait on itself with no limit; this makes that a failure, not a hang."""
        if n < 2:
            return
        held = [e for e in lockqueue.load(self.store)['entries']
                if e.get('resource') == kw['resource']]
        if held:
            raise AssertionError('the 1-slot run is still held: %r' % held)

    def assert_landed_here(self, out):
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertNotIn('ran_on', rec)
        return rec


class OnANode(NodeCase):
    def test_the_route_is_said_before_a_one_slot_booking(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes()
        self.no_main_gate()
        lander = self.node_lander(routes, tip, wait_s=77)
        out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.said[0], 'gating on load node n1: n1 has 3 cpu slots free')
        [b] = self.landing_books()
        self.assertGreater(b['said'], 0)            # the route line came first
        self.assertEqual(b['cores'], 1)
        self.assertEqual(b['priority'], 'land')
        self.assertEqual(b['resource'], land.queue_key(lander.lock.common))
        self.assertEqual(b['wait_s'], 77)
        self.assertEqual(routes.names(), ['plan', 'same_tree', 'recheck', 'run'])

    def test_a_pass_on_the_node_lands_with_ran_on(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes()
        self.no_main_gate()
        lander = self.node_lander(routes, tip, full=False)
        out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[-1].startswith('LANDED kit/feature on main at %s' % out.merge))
        self.assertIn('ran on load node n1 as job j-0001', out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual(rec['ran_on'], RAN_ON)
        self.assertIsNone(rec.get('input_keys'))
        self.assertEqual([s['name'] for s in rec['steps']], ['unit'])
        self.assertEqual(rec['base'], self.main)
        [run] = [c for c in routes.calls if c[0] == 'run']
        _, name, merge, base, repo, slug, kw = run
        self.assertEqual((name, merge, base, slug), ('n1', out.merge, self.main, 'kit-feature'))
        self.assertIs(kw['docs'], True)
        self.assertEqual(kw['tree'], lander.tree)
        self.assertFalse(kw['full'])
        self.assertEqual(kw['kind'], lander.cfg.section('gate')['kind'])
        self.assertIs(kw['store'], lander.queue_store())    # its lock-window notices' config
        self.assertEqual(len(self.landing_books()), 1)

    def test_another_node_that_qualifies_at_the_turn_takes_it(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes(pick=N2)
        self.no_main_gate()
        out = self.node_lander(routes, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('gating on load node n2: n2 has 2 cpu slots free', self.said)
        self.assertEqual([c[1] for c in routes.calls if c[0] == 'run'], ['n2'])
        rec, _ = gate_ok.verify(self.repo.root, out.merge)
        self.assertEqual(rec['ran_on']['node'], 'n2')
        self.assertEqual(len(self.landing_books()), 1)

    def test_a_failure_on_the_node_is_not_landed_and_not_gated_again(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.no_main_gate()
        out = self.node_lander(Routes(run=failing), tip).run()
        self.assertTrue(out.lines[0].startswith('GATE FAILED on load node n1'), out.lines)
        self.assertIn('NOT LANDED: the merge of kit/feature was undone', out.lines[-1])
        self.assert_untouched(out, G.FAILED)
        self.assertEqual(len(self.landing_books()), 1)

    def test_an_error_from_the_node_is_not_landed_and_never_a_fallback(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.no_main_gate()
        out = self.node_lander(Routes(run=broken), tip).run()
        self.assertIn('NOT LANDED: RuntimeError: boom in phase gate', out.lines[-1])
        self.assert_untouched(out, G.FAILED)
        self.assertEqual(len(self.landing_books()), 1)
        self.assertFalse([s for s in self.said if s.startswith('gating here')], self.said)


class RefreshesTheMirrors(NodeCase):
    """After a landing lands and holds nothing, its merge goes to the nodes' mirrors of main
    (kitlib.loadnode.mirror.refresh, stood in for here)."""

    def refresh_with(self, effect):
        self.refreshed = []
        p = mock.patch('kitlib.loadnode.mirror.refresh', side_effect=effect)
        self.addCleanup(p.stop)
        return p.start()

    def test_landed_refreshes_with_the_merge_once_nothing_is_held(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.no_main_gate()
        lander = self.node_lander(Routes(), tip)

        def refresh(root, repo, main, sha, store=None, say=None):
            held = [e for e in lockqueue.load(self.store)['entries']
                    if e.get('resource') == land.queue_key(lander.lock.common)]
            self.refreshed.append((root, repo, main, sha, store, lander.token, held))
            say("pushed %s to load node n1's mirror of main" % sha[:10])
        self.refresh_with(refresh)
        out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        repo = land.runner.repo_name(lander.cfg, lander.root)
        self.assertEqual(self.refreshed, [(self.repo.root, repo, 'main', out.merge, self.store,
                                           None, [])])  # the lock file and the slot released
        pushed = "pushed %s to load node n1's mirror of main" % out.merge[:10]
        self.assertEqual(out.lines[-2:], [pushed, 'LANDED kit/feature on main at %s (pushed and '
                                          'verified on origin)' % out.merge])

    def test_not_landed_does_not_refresh(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.no_main_gate()
        refresh = self.refresh_with(None)
        out = self.node_lander(Routes(run=failing), tip).run()
        self.assert_untouched(out, G.FAILED)
        self.assertFalse(refresh.called)

    def test_a_refresh_that_raises_still_lands_with_a_warning(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.no_main_gate()
        self.refresh_with(RuntimeError('boom'))
        out = self.node_lander(Routes(), tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(out.lines[-2], "WARNING: refreshing the load nodes' mirrors of main "
                                        'stopped (RuntimeError: boom); the landing stands')
        self.assertTrue(out.lines[-1].startswith('LANDED kit/feature on main at %s' % out.merge))
        self.assertIsNotNone(out.facts['result_record'])


class FallsBackToMain(NodeCase):
    def setUp(self):
        NodeCase.setUp(self)
        self.checks.append(self.nothing_held)   # one landing: nobody else holds the resource

    def test_nothing_ran_on_the_node_gates_here_in_a_full_run(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander = self.node_lander(Routes(run=fallback), tip)
        out = lander.run()
        self.assert_landed_here(out)
        self.assertIn('gating here: n1 did not take the job: busy (nothing ran on load node n1)',
                      self.said)
        self.assert_full_rebook(lander)

    def test_a_red_merge_after_a_fallback_is_not_landed(self):
        _, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.node_lander(Routes(run=fallback), tip).run()
        self.assertTrue(out.verdict.startswith('GATE FAILED: unit'), out.verdict)
        self.assert_untouched(out, G.FAILED)

    def test_no_node_qualifying_at_the_turn_gates_here(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes(pick=None)
        lander = self.node_lander(routes, tip)
        out = lander.run()
        self.assert_landed_here(out)
        self.assertIn('gating here: no load node qualifies now (n1: busy)', self.said)
        self.assertNotIn('run', routes.names())
        self.assert_full_rebook(lander)

    def test_the_tips_tree_gates_here(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes(same=SAME)
        lander = self.node_lander(routes, tip)
        out = lander.run()
        self.assert_landed_here(out)
        self.assertIn('gating here: %s' % SAME, self.said)
        self.assertEqual(routes.names(), ['plan', 'same_tree'])
        [same] = [c for c in routes.calls if c[0] == 'same_tree']
        self.assertEqual(same[1:6], (BRANCH, tip, self.main, out.merge, 1))
        self.assert_full_rebook(lander)

    def test_the_fallback_books_with_its_one_slot_run_released(self):
        # Booking main's full run while the 1-slot run is held may wait on itself (a kind of
        # every slot, an owner whole hold, a gpu pool): at the second booking this landing holds
        # nothing in the machine lock. The scratch store keeps runs to one slot, so the full run
        # could never start beside the 1-slot one.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander = self.node_lander(Routes(run=fallback), tip)

        def no_slot(n, kw):
            if n == 2 and lander.slot is not None:
                raise AssertionError("the landing still holds its 1-slot run's slot")
        self.checks.append(no_slot)
        out = lander.run()
        self.assert_landed_here(out)
        self.assertEqual(len(self.landing_books()), 2)

    def test_the_machine_lock_refusing_the_full_run_is_not_landed(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def refuse(n, kw):
            if n == 2:
                raise land.LockError(9, 'refused: the owner holds the machine')
        self.checks.append(refuse)
        out = self.node_lander(Routes(run=fallback), tip).run()
        self.assertTrue(out.lines[0].startswith(
            "the machine lock did not give main's merged gate its full"), out.lines)
        self.assertIn('refused: the owner holds the machine', out.lines[0])
        self.assert_untouched(out, G.FAILED)

    def test_a_plan_that_raises_gates_here(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes()
        routes.plan = mock.Mock(side_effect=OSError('the registry is unreadable'))
        out = self.node_lander(routes, tip).run()
        self.assert_landed_here(out)
        self.assertEqual(self.said[0],
                         'gating here: the load-node plan failed: OSError: the registry is '
                         'unreadable')
        [b] = self.landing_books()
        self.assertNotIn('cores', b)

    def test_a_plan_for_main_books_as_before(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        routes = Routes(where=None)
        out = self.node_lander(routes, tip).run()
        self.assert_landed_here(out)
        self.assertEqual(self.said[0], 'gating here: n1: busy')
        [b] = self.landing_books()
        self.assertNotIn('cores', b)
        self.assertEqual(routes.names(), ['plan'])


class GivesTheTurnBack(NodeCase):
    def test_a_landing_queued_behind_a_fallback_gives_its_turn_back(self):
        # A falls back after B joined the queue for the turn: A's release hands B the turn while
        # A holds the lock file. B sees A waiting for the turn, gives it back and books again
        # behind A; both land, B on A's merge. Without that B would wait at the lock file and A
        # for the turn, until B's --wait ran out.
        self.two_landings(lambda tip_b, said_b: self.lander(
            'kit/other', self.main, tip_b, wait_s=120, poll_s=0.05, say=said_b.append))

    def test_a_node_routed_landing_books_one_slot_again_when_it_gives_the_turn_back(self):
        routes_b = Routes()
        self.two_landings(lambda tip_b, said_b: self.node_lander(
            routes_b, tip_b, branch='kit/other', wait_s=120, poll_s=0.05, say=said_b.append))
        books_b = [b for b in self.seen if b['slug'] == 'kit-other']
        self.assertEqual([b['cores'] for b in books_b], [1, 1], books_b)
        self.assertEqual(books_b[1]['priority'], 'land')
        self.assertEqual(routes_b.names(), ['plan', 'same_tree', 'recheck', 'run'])

    def two_landings(self, make_b):
        """A (node-routed) falls back while B holds the turn; B made by make_b(tip_b, said_b)."""
        _, tip_a = self.branch('feature', {'a.py': 'a\n'})
        _, tip_b = self.branch('other', {'b.py': 'b\n'})
        inside, go = threading.Event(), threading.Event()

        def held_fallback(node, merge, kw):
            inside.set()
            self.assertTrue(go.wait(HANG_S))
            raise landroute.Fallback('the node went away', 5)
        results, said_b = {}, []
        a = threading.Thread(target=lambda: results.__setitem__(
            'a', self.node_lander(Routes(run=held_fallback), tip_a).run()))
        a.start()
        self.addCleanup(a.join, HANG_S)
        self.addCleanup(go.set)
        end = time.monotonic() + HANG_S
        while not inside.wait(0.05):        # A ending before its gate fails here, not at HANG_S
            self.assertTrue(a.is_alive(), results)
            self.assertLess(time.monotonic(), end)
        b = threading.Thread(target=lambda: results.__setitem__('b', make_b(tip_b,
                                                                            said_b).run()))
        b.start()
        self.addCleanup(b.join, HANG_S)
        end = time.monotonic() + HANG_S
        while not [e for e in lockqueue.load(self.store)['entries']
                   if e.get('slug') == 'kit-other' and e.get('state') == 'waiting']:
            self.assertLess(time.monotonic(), end, said_b)
            time.sleep(0.02)
        go.set()
        for t in (a, b):
            t.join(HANG_S)
            self.assertFalse(t.is_alive())
        self.assertEqual(results['a'].code, G.OK, results['a'].lines)
        self.assertEqual(results['b'].code, G.OK, (results['b'].lines, said_b))
        self.assertEqual(self.repo.git('rev-parse', results['b'].merge + '^1'), results['a'].merge)
        gave = [s for s in said_b if s.startswith('the landing holding the landing lock '
                                                  '(kit/feature) waits')]
        self.assertEqual(len(gave), 1, said_b)
        self.assertIsNone(self.lock().read())

    def test_a_turn_granted_again_at_once_is_given_back_once_a_look(self):
        # The booking after a yield may be granted at once (a backfill: the holder's start is far
        # off) while the holder still waits: the landing gives it back again only a look
        # (poll_s) later, and says so once, not in a loop until --wait runs out.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander = self.lander(BRANCH, self.main, tip, poll_s=0.1, say=self.said.append)
        holder = {'branches': ['kit/other'], 'pid': 1, 'token': 't'}
        books = []

        class Slot(object):
            def release(self, code, how):
                pass

        def try_take():
            lander.holder = holder
            return 'held by kit/other', False

        def book_slot(wait_s, cores=None):
            books.append((wait_s, cores))
            lander.slot = Slot()
        lander.try_take, lander.book_slot = try_take, book_slot
        lander.turn_waiters = lambda: [{'slug': 'kit-other', 'state': 'waiting'}]
        lander.slot, lander.deadline = Slot(), time.monotonic() + 1.0
        self.assertEqual(lander.take(), 'held by kit/other')
        self.assertLessEqual(len(books), 12, len(books))
        self.assertGreaterEqual(len(books), 2)
        self.assertEqual({c for _, c in books}, {None})       # routed to main: the kind's size
        gave = [s for s in self.said if s.startswith('the landing holding the landing lock')]
        self.assertEqual(len(gave), 1, gave)

    def test_a_holder_waiting_for_nothing_keeps_the_turn(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        held = self.lock().take(['kit/other'])
        self.addCleanup(lambda: self.lock().read() and self.lock().release(held['token']))
        out = self.node_lander(Routes(where=None), tip, wait_s=1, poll_s=0.05).run()
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertEqual(len(self.landing_books()), 1)
        self.assertFalse([s for s in self.said if s.startswith('the landing holding')], self.said)



class BaseNamesAnotherKind(NodeCase):
    """Main moved on before the landing's turn to a base whose config names another
    "gate".kind than the one the landing booked with (the config at preconditions): the merged
    gate runs at the base's kind (Lander.follow, Lander.rebook_kind)."""

    def setUp(self):
        NodeCase.setUp(self)
        self.checks.append(self.nothing_held)   # one landing: nobody else holds the resource

    def move_main(self, kind):
        """Push to the remote's main a commit whose .kit.json books `kind` (unchanged: the
        same .kit.json and another file): the moved main."""
        other = self.repo.worktree('other')
        with open(self.repo.path('.kit.json'), encoding='utf-8') as f:
            kit = json.load(f)
        kit['gate']['kind'] = kind
        self.repo.write('.kit.json', json.dumps(kit, indent=1) + '\n', root=other)
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('move main', '.kit.json', 'o.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        return moved

    def test_a_main_routed_landing_releases_then_books_the_bases_kind(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        moved = self.move_main('cpu-eval')
        routes = Routes(where=None)
        lander = self.node_lander(routes, tip)
        out = lander.run()
        self.assert_landed_here(out)
        self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), moved)
        first, second = self.landing_books()
        self.assertEqual((first['kind'], second['kind']), ('test', 'cpu-eval'))
        self.assertNotIn('cores', second)       # the base's kind at its own size
        self.assertEqual(second['resource'], land.queue_key(lander.lock.common))
        self.assertEqual(second['priority'], 'land')
        self.assertIsNone(second['wait_s'])
        self.assertFalse(second['no_wait'])
        line = [s for s in self.said if s.startswith('the base %s books' % moved[:10])]
        self.assertEqual(len(line), 1, self.said)
        self.assertIn("releasing the landing's test run and booking a cpu-eval run",
                      line[0])
        self.assertLess(self.said.index(line[0]), second['said'])
        self.assertEqual(routes.names(), ['plan'])

    def test_a_base_kind_the_machine_lock_refuses_is_not_landed(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        moved = self.move_main('build')        # not in the scratch store's kinds table
        self.main = moved                       # assert_untouched: main stays at the base
        out = self.node_lander(Routes(where=None), tip).run()
        self.assertTrue(out.lines[0].startswith(
            'the machine lock did not give the merged gate its build run: '), out.lines)
        self.assertIn('NOT LANDED: the merge of kit/feature was undone', out.lines[-1])
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.remote_main(), moved)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.records(), [])

    def test_a_base_with_the_same_kind_books_once(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.move_main('test')
        out = self.node_lander(Routes(where=None), tip).run()
        self.assert_landed_here(out)
        self.assertEqual([b['kind'] for b in self.landing_books()], ['test'])

    def test_a_base_naming_the_same_kind_by_its_retired_name_books_once(self):
        """The base's config names the kind the landing booked under its retired name (an alias
        in the lock's kinds table): it is the same kind, so no release and second booking, and
        a node-routed gate is not sized again. Red before: the names as written differed."""
        with open(self.kinds_path, encoding='utf-8') as f:
            doc = json.load(f)
        doc['aliases'] = {'old-test': {'to': 'test', 'why': 'renamed'}}
        with open(self.kinds_path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.move_main('old-test')
        out = self.node_lander(Routes(where=None), tip).run()
        self.assert_landed_here(out)
        self.assertEqual([b['kind'] for b in self.landing_books()], ['test'])
        self.assertFalse([s for s in self.said if 'books "gate".kind' in s], self.said)

    def test_a_node_routed_landing_asks_the_nodes_again_at_the_bases_kind(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.move_main('cpu-eval')
        routes = Routes()
        self.no_main_gate()
        out = self.node_lander(routes, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(routes.names(), ['plan', 'same_tree', 'need_of', 'recheck', 'run'])
        self.assertEqual([c[1] for c in routes.calls if c[0] == 'need_of'], ['cpu-eval'])
        [recheck] = [c for c in routes.calls if c[0] == 'recheck']
        self.assertEqual(recheck[3], NEED_BASE)
        [run] = [c for c in routes.calls if c[0] == 'run']
        self.assertEqual(run[6]['kind'], 'cpu-eval')
        [b] = self.landing_books()              # the node gates: the 1-slot run stays
        self.assertEqual((b['kind'], b['cores']), ('test', 1))

    def test_a_base_kind_that_cannot_be_sized_gates_here_at_that_kind(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.move_main('cpu-eval')
        routes = Routes()
        routes.need = KeyError('cpu-eval')
        lander = self.node_lander(routes, tip)
        out = lander.run()
        self.assert_landed_here(out)
        self.assertNotIn('recheck', routes.names())
        why = [s for s in self.said if s.startswith('gating here: the base')]
        self.assertEqual(len(why), 1, self.said)
        self.assertIn('cpu-eval cannot be sized here', why[0])
        self.assert_full_rebook(lander)
        self.assertEqual([b['kind'] for b in self.landing_books()], ['test', 'cpu-eval'])


if __name__ == '__main__':
    unittest.main()
