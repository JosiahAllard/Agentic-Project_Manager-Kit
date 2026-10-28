"""kitlib.loadnode.route: `node.py run --node auto` picks the machine with the most cpu slots free
now among main and the nodes that opted in for the repository; a tie goes to main; none free
leaves the job on main, whose lock queues it."""
import unittest

from kitlib.loadnode import box, remote, route
from kitlib.loadnode.registry import Node
from tests.loadnode.test_registry import KEY


def node(name, slots=2, memory_mb=4096, repos=('my-repo',)):
    return Node(name, '10.0.0.%d' % (len(name) + 1), 'pmnode', KEY, slots, memory_mb, 22, None,
                tuple(repos))


def lock(cpu_free=4, gpu_free=0, cpu_slots=4, gpu_slots=0, waiting=0, **kw):
    d = {'cpu_free': cpu_free, 'gpu_free': gpu_free, 'cpu_slots': cpu_slots,
         'gpu_slots': gpu_slots, 'waiting': waiting, 'unreadable': False, 'pending': False}
    d.update(kw)
    return d


def ok(name, jobs=0, mem_free=8192, verbs=None, **lk):
    reply = {'verbs': list(box.RUN_VERBS) + ['status'] if verbs is None else verbs,
             'jobs': jobs, 'lock': lock(**lk), 'hardware': {'memory_mb': {'free': mem_free}}}
    return remote.Result(name, remote.OK, reply, '')


NEED = {'cpu': 2, 'gpu': 0}


class Fits(unittest.TestCase):

    def test_free_slots_and_no_one_waiting(self):
        self.assertEqual(route.fits(lock(cpu_free=2), NEED), '')
        self.assertIn('1 of 2 cpu', route.fits(lock(cpu_free=1), NEED))
        self.assertIn('0 of 1 gpu', route.fits(lock(), {'cpu': 1, 'gpu': 1}))
        self.assertIn('1 run waiting', route.fits(lock(waiting=1), NEED))

    def test_a_node_need_is_capped_at_its_slots(self):
        self.assertEqual(route.node_need({'cpu': 12, 'gpu': 0}, lock(cpu_slots=4)),
                         {'cpu': 4, 'gpu': 0})
        self.assertIsNone(route.node_need({'cpu': 1, 'gpu': 1}, lock()))
        self.assertEqual(route.node_need({'cpu': 1, 'gpu': 2}, lock(gpu_slots=1)),
                         {'cpu': 1, 'gpu': 1})


class NodeFits(unittest.TestCase):

    def test_each_reason_a_node_is_passed_over(self):
        n = node('n1')
        cases = (
            (node('n1', repos=()), ok('n1'), 'not opted in'),
            (n, None, 'not asked'),
            (n, remote.Result('n1', 'unreachable', None, 'x'), 'unreachable'),
            (n, ok('n1', verbs=['status']), 'no run verbs'),
            (n, ok('n1', unreadable=True), 'not fully readable'),
            (n, ok('n1', pending=True), 'pending'),
            (n, ok('n1', jobs=2), 'runs 2 jobs'),
            (n, ok('n1', mem_free=1000), '1000 MB memory free of 2048'),
            (n, ok('n1', cpu_free=1), '1 of 2 cpu'),
        )
        for nd, res, why in cases:
            with self.subTest(why=why):
                self.assertIn(why, route.node_fits(nd, res, 'my-repo', NEED))
        self.assertEqual(route.node_fits(n, ok('n1'), 'my-repo', NEED), '')
        self.assertEqual(route.node_fits(n, ok('n1', mem_free=None), 'my-repo', NEED), '')
        self.assertIn('no gpu', route.node_fits(n, ok('n1'), 'my-repo', {'cpu': 1, 'gpu': 1}))

    def test_a_heavy_kind_takes_the_whole_node(self):
        self.assertEqual(route.node_fits(node('n1'), ok('n1', cpu_free=4), 'my-repo',
                                         {'cpu': 12, 'gpu': 0}), '')
        self.assertIn('3 of 4 cpu', route.node_fits(node('n1'), ok('n1', cpu_free=3), 'my-repo',
                                                    {'cpu': 12, 'gpu': 0}))


class Choose(unittest.TestCase):

    def test_the_most_free_wins_and_a_tie_goes_to_main(self):
        nodes = [node('n1'), node('n2')]
        res = [ok('n2', cpu_free=4), ok('n1', cpu_free=3)]
        c = route.choose(nodes, res, 'my-repo', lock(cpu_free=2, cpu_slots=12), NEED)
        self.assertEqual((c.where, c.node.name), ('n2', 'n2'))
        c = route.choose(nodes, res, 'my-repo', lock(cpu_free=4, cpu_slots=12), NEED)
        self.assertEqual((c.where, c.node), (route.MAIN, None))

    def test_a_tie_between_nodes_goes_to_the_first_in_the_registry(self):
        nodes = [node('n1'), node('n2')]
        res = [ok('n2', cpu_free=4), ok('n1', cpu_free=4)]
        c = route.choose(nodes, res, 'my-repo', lock(cpu_free=0, cpu_slots=12), NEED)
        self.assertEqual(c.where, 'n1')
        self.assertIn('main: 0 of 2 cpu', c.why)

    def test_none_free_leaves_it_on_main_and_says_why(self):
        nodes = [node('n1'), node('n2', repos=())]
        res = [ok('n1', cpu_free=0), ok('n2')]
        c = route.choose(nodes, res, 'my-repo', lock(cpu_free=1, cpu_slots=12), NEED)
        self.assertEqual((c.where, c.node), (route.MAIN, None))
        self.assertIn('main queues it', c.why)
        self.assertIn('n1: 0 of 2 cpu', c.why)
        self.assertIn('n2: not opted in', c.why)

    def test_main_busy_and_a_node_free(self):
        c = route.choose([node('n1')], [ok('n1')], 'my-repo',
                         lock(cpu_free=12, cpu_slots=12, waiting=2), NEED)
        self.assertEqual(c.where, 'n1')

    def test_no_nodes_is_main(self):
        self.assertEqual(route.choose([], [], 'my-repo', lock(), NEED).where, route.MAIN)


class NeedOf(unittest.TestCase):

    def test_the_kind_sized_by_main_layout(self):
        from kitlib.lock import layout as lay
        table = {'engine-test': object()}
        calls = []
        orig = lay.need_for
        lay.need_for = lambda layout, spec: calls.append((layout, spec)) or {'cpu': 3, 'gpu': 0}
        self.addCleanup(setattr, lay, 'need_for', orig)
        self.assertEqual(route.need_of('engine-test', table=table, layout='L'), {'cpu': 3, 'gpu': 0})
        self.assertEqual(calls, [('L', table['engine-test'])])
        with self.assertRaises(KeyError):
            route.need_of('no-such-kind', table=table, layout='L')


if __name__ == '__main__':
    unittest.main()
