"""kitlib.loadnode.landroute: where a landing's merged gate runs (main or a load node), chosen
before the landing books main's lock and checked again at its turn, and the land job's ways out:
a fallback to main only while nothing ran on the node, NOT LANDED (a FAILED gate Outcome) once it
started, an OK Outcome land.py reads as runner.merged_gate's when it passed. The land job talks to
a real kitlib.loadnode.box.Box through tests/loadnode/test_job.py's link stand-in."""
import contextlib
import subprocess
import threading
import types
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.loadnode import box, job, landgate, landroute, registry, remote, route
from tests.loadnode.test_box import RefGit
from tests.loadnode.test_cli import Cli, run_reply
from tests.loadnode.test_job import Base, FakeLink, JOB, KITH, NODE, SHA, BASE_SHA
from tests.loadnode.test_registry import KEY, node
from tests.loadnode import test_route as tr
from tests.loadnode.test_remote import Runner

LAND_REPLY = run_reply(verbs=list(box.RUN_VERBS) + ['status', box.LAND_VERB])
NEED = {'cpu': 2, 'gpu': 0}
CFG = types.SimpleNamespace(remote='origin', main_branch='main')
STEPS = [{'name': 'unit', 'result': 'pass', 'seconds': 3.5},
         {'name': 'lint', 'result': 'warn', 'seconds': 1.0}]
REC = {'commit': SHA, 'tree': 't' * 40, 'base': BASE_SHA, 'config_rev': BASE_SHA, 'full': True,
       'mode': 'full', 'source': 'gate.json', 'gate_config': 'g' * 64, 'repo': 'my-repo',
       'kit': {'files': KITH}, 'steps': STEPS}


def no_ssh(argv, timeout_s):
    raise AssertionError('ssh was asked: %r' % (argv,))


# -- the choice before booking --------------------------------------------------------------------

class Plan(unittest.TestCase):
    write = Cli.write

    def setUp(self):
        Cli.setUp(self)
        self.write(node(memory_mb=4096, repos=['my-repo']), node(name='b2', repos=[]))
        self.store = types.SimpleNamespace(testing=True)
        self.main_lock = {'cpu_slots': 12, 'cpu_free': 2, 'gpu_slots': 0, 'gpu_free': 0,
                          'waiting': 0, 'unreadable': [], 'pending': False}

    def plan(self, runner=None, **kw):
        kw.setdefault('registry_file', self.file)
        kw.setdefault('is_kit', lambda root: False)
        kw.setdefault('need', lambda kind, store: NEED)
        kw.setdefault('lock', lambda store: self.main_lock)
        return landroute.plan('R', 'my-repo', 'gate', store=self.store,
                              runner=runner or Runner(out=LAND_REPLY), **kw)

    def test_a_node_with_room_that_serves_the_land_verb_is_chosen(self):
        r = self.plan()
        self.assertEqual((r.where, r.node.name, r.quiet), ('box1', 'box1', False))
        self.assertEqual([n.name for n in r.nodes], ['box1'])      # the opted-in ones
        self.assertEqual(r.need, NEED)
        self.assertEqual(landroute.route_line(r),
                         'gating on load node box1: box1 has 3 cpu slots free (main: fewer)')

    def test_a_node_whose_kit_serves_no_land_verb_is_passed_over(self):
        r = self.plan(runner=Runner(out=run_reply()))
        self.assertEqual((r.where, r.node), (route.MAIN, None))
        self.assertIn('box1: its kit serves no land verb (update it)', r.why)
        self.assertTrue(landroute.route_line(r).startswith('gating here: '))

    def test_main_with_more_room_gates_here(self):
        self.main_lock['cpu_free'] = 12
        r = self.plan()
        self.assertEqual(r.where, route.MAIN)
        self.assertEqual(landroute.route_line(r), 'gating here: main has 12 cpu slots free')

    def assert_quiet(self, r, why):
        self.assertEqual((r.where, r.node, r.quiet), (route.MAIN, None, True))
        self.assertIn(why, r.why)
        self.assertIsNone(landroute.route_line(r))

    def test_a_kit_checkout_gates_on_main_and_asks_no_node(self):
        self.assert_quiet(self.plan(runner=no_ssh, is_kit=lambda root: True), 'kit checkout')

    def test_the_kits_own_checkout_is_a_kit_checkout(self):
        from kitlib.loadnode import dispatch
        r = landroute.plan(dispatch.KIT_ROOT, 'my-repo', 'gate', store=self.store,
                           registry_file=self.file, runner=no_ssh)
        self.assert_quiet(r, 'kit checkout')

    def test_a_tests_merged_gate_gates_on_main_and_asks_no_node(self):
        self.assert_quiet(self.plan(runner=no_ssh, merged_gate_fn=lambda lander: None),
                          "merged gate is a test's")

    def test_no_registry_gates_on_main_and_asks_no_node(self):
        self.assert_quiet(self.plan(runner=no_ssh, registry_file=None), 'no load node registry')
        self.assert_quiet(self.plan(runner=no_ssh, registry_file=self.file + '.gone'),
                          'no load node registry')

    def test_no_node_opted_in_gates_on_main_and_asks_no_node(self):
        self.write(node(repos=['other-repo']))
        self.assert_quiet(self.plan(runner=no_ssh), 'no node has opted in my-repo')

    def test_a_kind_the_lock_does_not_name_gates_on_main_quietly(self):
        def need(kind, store):
            raise KeyError(kind)
        self.assert_quiet(self.plan(runner=no_ssh, need=need), 'no kind gate')

    def assert_loud(self, r, why):
        self.assertEqual((r.where, r.node, r.quiet), (route.MAIN, None, False))
        self.assertIn(why, landroute.route_line(r))

    def test_a_registry_that_cannot_be_used_says_so(self):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assert_loud(self.plan(runner=no_ssh), 'gating here: the load node registry cannot be '
                                                   'used')

    def test_a_kind_that_cannot_be_sized_says_so(self):
        def need(kind, store):
            raise ValueError('no layout')
        self.assert_loud(self.plan(runner=no_ssh, need=need), 'the kind gate cannot be sized here '
                                                              '(no layout)')

    def test_a_lock_that_cannot_be_read_says_so(self):
        def lock(store):
            raise OSError('gone')
        self.assert_loud(self.plan(runner=no_ssh, lock=lock), "this machine's lock cannot be read "
                                                              '(OSError)')


class Recheck(unittest.TestCase):
    """At the landing's turn the nodes are asked again; main's own lock no longer counts."""

    def recheck(self, nodes, results):
        with mock.patch.object(remote, 'query_all', lambda ns, runner=None: results):
            return landroute.recheck(nodes, 'my-repo', NEED)

    def ok(self, name, **kw):
        return tr.ok(name, verbs=list(box.RUN_VERBS) + [box.LAND_VERB], **kw)

    def test_the_node_with_the_most_free_that_qualifies_now(self):
        c = self.recheck([tr.node('n1'), tr.node('n2')], [self.ok('n1', cpu_free=2),
                                                          self.ok('n2', cpu_free=4)])
        self.assertEqual((c.where, c.node.name), ('n2', 'n2'))
        self.assertEqual(c.why, 'n2 has 4 cpu slots free')

    def test_a_tie_goes_to_the_first_in_the_registry(self):
        c = self.recheck([tr.node('n1'), tr.node('n2')], [self.ok('n2'), self.ok('n1')])
        self.assertEqual(c.where, 'n1')

    def test_a_node_now_busy_or_without_the_land_verb_is_passed_over(self):
        c = self.recheck([tr.node('n1'), tr.node('n2')],
                         [self.ok('n1', cpu_free=1), tr.ok('n2')])
        self.assertEqual((c.where, c.node), (route.MAIN, None))
        self.assertIn('no load node qualifies now', c.why)
        self.assertIn('n1: 1 of 2 cpu slots free', c.why)
        self.assertIn('n2: its kit serves no land verb', c.why)

    def test_a_node_that_does_not_answer_is_passed_over(self):
        c = self.recheck([tr.node('n1')], [remote.Result('n1', 'unreachable', None, 'x')])
        self.assertEqual(c.where, route.MAIN)
        self.assertIn('n1: unreachable', c.why)


class SameTree(unittest.TestCase):

    def setUp(self):
        from kitlib.gate import reuse
        self.reuse, self.calls, self.answer = reuse, [], (None, None)

        def same_tree(root, cfg, branch, tip, base, merge, now=None):
            self.calls.append((branch, tip, base, merge))
            if isinstance(self.answer, BaseException):
                raise self.answer
            return self.answer
        p = mock.patch.object(reuse, 'same_tree', same_tree)
        p.start()
        self.addCleanup(p.stop)

    def why(self, **kw):
        return landroute.same_tree_why('R', CFG, 'b1', 'f' * 40, BASE_SHA, SHA, **kw)

    def test_a_tip_record_with_the_merges_tree_gates_on_main(self):
        self.answer = (self.reuse.SameTree({}, 'f' * 40, 't' * 40), 'SAME TREE tttttttttt: b1 at '
                       'ffffffffff passed gate.py --full on this tree (record ended X)')
        why = self.why()
        self.assertIn('SAME TREE tttttttttt', why)
        self.assertIn('a load node never does', why)
        self.assertEqual(self.calls, [('b1', 'f' * 40, BASE_SHA, SHA)])

    def test_no_such_record_leaves_the_node(self):
        self.assertIsNone(self.why())
        self.answer = (None, 'SAME TREE x: the --full record of b1 at y is not used: z')
        self.assertIsNone(self.why())

    def test_git_that_cannot_tell_leaves_the_node(self):
        self.answer = RuntimeError('git cannot read the tree')
        self.assertIsNone(self.why())

    def test_a_batch_fresh_or_full_landing_reuses_nothing_and_is_not_asked(self):
        for kw in ({'batch': 2}, {'fresh': True}, {'full': True}):
            self.assertIsNone(self.why(**kw))
        self.assertEqual(self.calls, [])


# -- the land job ---------------------------------------------------------------------------------

class Verdict(unittest.TestCase):
    """Every way a land job ends, classed: a fallback only while nothing ran on the node."""

    def nj(self, started, record=None):
        return types.SimpleNamespace(started=started, land_record=record)

    def test_each_way_out(self):
        F, N, P = landroute.FALLBACK, landroute.NOT_LANDED, landroute.PASSED
        cases = (
            ('passed, record taken', self.nj(True, REC), 0, None, False, P),
            ('exit 0 with no record', self.nj(True), 0, None, False, N),
            ('red gate', self.nj(True), 1, None, False, N),
            ('start reply lost', self.nj(True), job.EXIT_AFTER_START, None, False, N),
            ('LOST', self.nj(True), job.EXIT_LOST, None, False, N),
            ('record refused', self.nj(True), job.EXIT_RECORD, None, False, N),
            ('ceiling', self.nj(True, REC), 0, None, True, N),
            ('StartUnknown raised', self.nj(True), None, job.StartUnknown('x'), False, N),
            ('follow failed', self.nj(True), None, job.JobError(14, 'x'), False, N),
            ('OSError after start', self.nj(True), None, OSError('x'), False, N),
            ('busy after start', self.nj(True), None, job.JobError(6, 'x', True), False, F),
            ('push failed', self.nj(False), None, job.JobError(10, 'x', True), False, F),
            ('spec refused', self.nj(False), None, job.JobError(2, 'x'), False, F),
            ('ssh before start', self.nj(False), None, OSError('x'), False, F),
            ('no link at all', None, None, OSError('x'), False, F),
            ('ceiling before start', self.nj(False), None, job.JobError(10, 'x', True), True, F),
        )
        for name, nj, code, error, expired, want in cases:
            with self.subTest(name):
                self.assertEqual(landroute.verdict_of(nj, code, error, expired), want)


class LandRun(Base):
    """run_on_node with a real NodeJob talking to a real Box."""

    def setUp(self):
        Base.setUp(self)
        self.node_git = RefGit({'refs/node/%s-base' % JOB: BASE_SHA}, head=SHA)
        self.box.git = self.node_git
        self.box.kit_files = lambda: KITH
        self.result = {'exit': 0, 'how': 'ended', 'gate': True, 'land': True, 'record': REC,
                       'record_why': None}
        self.checked, self.check_error = [], None
        self.link = FakeLink(self.box)

    def checker(self, rec):
        self.checked.append(rec)
        if self.check_error:
            raise self.check_error
        return rec

    def run_on(self, link=None, **kw):
        if link is not None:
            self.link = link
        kw.setdefault('checker', self.checker)
        kw.setdefault('kit_hash', KITH)
        kw.setdefault('open_link', lambda n: contextlib.nullcontext(self.link))
        return landroute.run_on_node(
            NODE, self.top, SHA, BASE_SHA, CFG, 'my-repo', 'my-slug', full=True, docs=False,
            kind='gate', say=self.said.append, tool='node.py',
            job_kw={'git': self.git, 'sleep': self.sleep, 'clock': lambda: self.t[0], 'job': JOB,
                    'reattach_s': 60}, **kw)

    def fallback(self, **kw):
        with self.assertRaises(landroute.Fallback) as cm:
            self.run_on(**kw)
        return cm.exception

    def assert_not_landed(self, out, why):
        self.assertEqual(out.code, G.FAILED)
        self.assertTrue(out.lines[0].startswith('GATE FAILED on load node box1: '), out.lines)
        self.assertIn(why, out.lines[0])
        self.assertEqual(out.verdict, out.lines[0])
        self.assertIn('not gated again on main', '\n'.join(out.lines))
        self.assertFalse(any('NOT LANDED' in l for l in out.lines))   # the landing says that
        self.assertIsNone(out.ran_on)
        self.assertEqual(out.results, [])

    # passed

    def test_a_pass_is_an_ok_outcome_land_reads_as_the_merged_gates_with_ran_on(self):
        from kitlib.gate import runner
        out = self.run_on()
        self.assertEqual(out.code, G.OK, self.said)
        self.assertEqual(self.verbs(), ['land', 'follow', 'clean'])
        sent = self.link.calls[0][1]
        self.assertEqual((sent['full'], sent['docs'], sent['kit_files'], sent['base'],
                          sent['remote'], sent['main']),
                         (True, False, KITH, BASE_SHA, 'origin', 'main'))
        self.assertEqual(self.checked, [REC])
        self.assertEqual(out.verdict, 'GATE OK (warned: lint)')
        self.assertEqual(out.lines[0], out.verdict)
        self.assertEqual(out.ran_on, {'node': 'box1', 'host_key': registry.fingerprint(KEY),
                                      'job': JOB})
        self.assertEqual((out.node, out.job, out.head, out.docs, out.source),
                         ('box1', JOB, SHA, False, 'gate.json'))
        self.assertEqual(out.record, REC)
        # land.py writes its record from these results: the node's steps, as they were
        self.assertEqual(runner.record_steps(out.results), STEPS)

    def test_a_docs_gate_on_the_node_is_a_docs_outcome(self):
        rec = dict(REC, mode='docs', full=False, source='auto')
        nj = types.SimpleNamespace(land_record=rec, ran_on={'node': 'box1'}, job=JOB, node=NODE)
        out = landroute.outcome_of(nj, SHA)
        self.assertEqual((out.docs, out.source, out.ran_on), (True, 'auto', {'node': 'box1'}))

    # before the start: a fallback to main

    def test_a_failed_push_falls_back_and_starts_nothing(self):
        self.push_fails = 'fatal: unable to access the mirror\n'
        e = self.fallback()
        self.assertEqual(e.code, job.EXIT_NODE)
        self.assertIn('the push to box1 failed', e.why)
        self.assertNotIn('land', self.verbs())

    def test_a_link_that_cannot_open_falls_back(self):
        def open_link(n):
            raise OSError('no ssh here')
        e = self.fallback(open_link=open_link)
        self.assertEqual(e.why, 'box1 could not be used: no ssh here')

    def test_ssh_that_does_not_start_falls_back(self):
        class NoSsh(FakeLink):
            def call(self, verb, payload, timeout_s=None):
                raise FileNotFoundError(2, 'no ssh')
        self.assertEqual(self.fallback(link=NoSsh(self.box)).code, job.EXIT_NODE)

    def test_a_busy_node_falls_back(self):
        class Busy(FakeLink):
            def call(self, verb, payload, timeout_s=None):
                self.calls.append((verb, payload))
                return 0, b'{"protocol":1,"ok":false,"busy":true,"error":"2 jobs, its most"}\n', b''
        e = self.fallback(link=Busy(self.box))
        self.assertEqual(e.code, job.EXIT_BUSY)
        self.assertIn('box1 is busy', e.why)

    def test_a_spec_the_node_would_refuse_falls_back_and_pushes_nothing(self):
        e = self.fallback(kit_hash='not-a-hash')
        self.assertEqual(e.code, job.EXIT_USAGE)
        self.assertEqual(self.pushes, [])

    def test_a_node_with_another_kit_falls_back(self):
        self.box.kit_files = lambda: 'f' * 64
        e = self.fallback()
        self.assertEqual(e.code, job.EXIT_NODE)
        self.assertIn("the node kit's files are not main's", e.why)

    def test_a_node_kit_with_no_land_verb_falls_back(self):
        class Old(FakeLink):
            def call(self, verb, payload, timeout_s=None):
                return 2, (b'{"protocol":1,"ok":false,"error":"refused: not a command this '
                           b'node serves"}\n'), b''
        self.assertIn('serves no land verb', self.fallback(link=Old(self.box)).why)

    def test_a_lock_on_the_node_that_answers_busy_falls_back_though_it_started(self):
        self.result = {'exit': 6, 'how': 'busy', 'why': 'busy: no slot now'}
        e = self.fallback()
        self.assertEqual(e.code, job.EXIT_BUSY)
        self.assertEqual(self.verbs()[-1], 'clean')

    def test_mains_kit_hash_that_cannot_be_read_falls_back(self):
        def bad():
            raise OSError('unreadable')
        with mock.patch.object(landgate, 'kit_files', bad):
            e = self.fallback(kit_hash=None)
        self.assertIn("main's kit files cannot be read (OSError)", e.why)
        self.assertEqual(self.link.calls, [])

    def test_a_merge_mains_own_gate_would_refuse_falls_back(self):
        def refuse(*a, **kw):
            raise job.RecordRefused("main's own merged gate would refuse the merge: no steps")
        with mock.patch.object(landgate, 'checker', refuse):
            e = self.fallback(checker=None)
        self.assertIn("main's own merged gate would refuse", e.why)
        self.assertEqual(self.link.calls, [])

    # after the start: NOT LANDED, never a fallback

    def test_a_start_whose_reply_is_lost_is_not_landed(self):
        class Slow(FakeLink):
            def call(self, verb, payload, timeout_s=None):
                self.calls.append((verb, payload))
                raise subprocess.TimeoutExpired(['ssh'], timeout_s)
        out = self.run_on(link=Slow(self.box))
        self.assert_not_landed(out, 'no exit code of its own, or its start had no reply')
        self.assertEqual(out.node_exit, job.EXIT_AFTER_START)
        self.assertIn('  stop it:   node.py stop box1 %s' % JOB, out.lines)

    def test_a_lost_connection_is_not_landed(self):
        out = self.run_on(link=FakeLink(self.box, cut=['down'] * 50))
        self.assert_not_landed(out, 'the connection was lost')
        self.assertEqual(out.node_exit, job.EXIT_LOST)

    def test_a_vanished_runner_is_not_landed(self):
        self.result = None
        out = self.run_on()
        self.assert_not_landed(out, 'no exit code of its own')
        self.assertEqual(out.node_exit, job.EXIT_AFTER_START)

    def test_a_red_merged_gate_is_not_landed(self):
        self.result.update(exit=1, record=None)
        out = self.run_on()
        self.assert_not_landed(out, 'the merged gate failed there (job %s, exit 1)' % JOB)
        self.assertEqual(self.checked, [])

    def test_a_record_main_refuses_is_not_landed(self):
        self.check_error = job.RecordRefused('the record ran a; main would run a, b')
        out = self.run_on()
        self.assert_not_landed(out, 'main did not take its record')
        self.assertEqual(out.node_exit, job.EXIT_RECORD)

    def test_a_step_skipped_on_the_node_is_a_refused_record(self):
        from kitlib.records import gate_ok
        self.result['record'] = dict(REC, steps=[dict(STEPS[0]), dict(STEPS[1], result='skip')])
        expected = {'head': SHA, 'steps': ['unit', 'lint'], 'mode': 'full', 'full': True,
                    'source': 'gate.json', 'gate_config': REC['gate_config']}

        def check(rec):
            return landgate.check_record(self.top, rec, SHA, BASE_SHA, expected, 'my-repo', KITH,
                                         {})
        with mock.patch.object(gate_ok, '_check', lambda *a, **kw: None):
            out = self.run_on(checker=check)
        self.assert_not_landed(out, 'main did not take its record')
        self.assertIn('steps that did not run and pass on the node: lint (skip)',
                      '\n'.join(self.said))

    # the ceiling

    def test_the_ceiling_stops_a_started_job_and_it_is_not_landed(self):
        slow = SlowJob()
        out = self.run_on(open_link=OpenLink, make_job=slow.made, ceiling_s=0.05, poll_s=0.01)
        self.assertTrue(slow.stopped.is_set())
        self.assertEqual(slow.link_open, True)          # its pinned known_hosts still there
        self.assert_not_landed(out, 'ran past the ceiling of 0 s and was stopped')
        self.assertIn('job %s on box1 asked to stop' % JOB, self.said)

    def test_the_ceiling_waits_for_the_start_before_it_stops(self):
        slow = SlowJob(start_after=0.1)
        out = self.run_on(make_job=lambda link: slow, ceiling_s=0.01, poll_s=0.01)
        self.assertTrue(slow.stopped.is_set())
        self.assertTrue(slow.stopped_after_start)
        self.assertEqual(out.code, G.FAILED)

    def test_a_job_done_inside_the_ceiling_is_never_stopped(self):
        out = self.run_on(ceiling_s=30, poll_s=0.01)
        self.assertEqual(out.code, G.OK)
        self.assertNotIn('stop', self.verbs())

    # Ctrl+C

    def test_ctrl_c_stops_a_started_job_and_goes_on_up(self):
        slow = SlowJob(interrupt=True)
        with self.assertRaises(KeyboardInterrupt):
            self.run_on(open_link=OpenLink, make_job=slow.made)
        self.assertTrue(slow.stopped.is_set())
        self.assertEqual(slow.link_open, True)
        self.assertIn('  stop it:   node.py stop box1 %s' % JOB, self.said)

    def test_ctrl_c_before_the_start_stops_nothing(self):
        slow = SlowJob(interrupt=True, start_after=None)
        with self.assertRaises(KeyboardInterrupt):
            self.run_on(make_job=lambda link: slow)
        self.assertFalse(slow.stopped.is_set())
        # its start may be on the node already: the owner is told how to stop it
        self.assertIn('Ctrl+C: job %s was not seen to start on box1; if it did, stop it: '
                      'node.py stop box1 %s' % (JOB, JOB), self.said)


class OpenLink(object):
    """open_link's stand-in: says whether the link is still open."""

    def __init__(self, node):
        self.open = False

    def __enter__(self):
        self.open = True
        return self

    def __exit__(self, *exc):
        self.open = False


class SlowJob(object):
    """A land job that runs until it is stopped: started at once, after `start_after` seconds,
    or never (None); `interrupt` raises Ctrl+C from run()."""

    def __init__(self, start_after=0, interrupt=False):
        self.node, self.job, self.started, self.land_record = NODE, JOB, False, None
        self.start_after, self.interrupt = start_after, interrupt
        self.stopped, self.stopped_after_start = threading.Event(), False
        self.link = self.link_open = None

    def made(self, link):
        self.link = link
        return self

    def run(self):
        if self.start_after is not None:
            if self.start_after:
                self.stopped.wait(self.start_after)
            self.started = True
        if self.interrupt:
            raise KeyboardInterrupt
        self.stopped.wait(5)
        return 143

    def stop(self):
        self.stopped_after_start = self.started
        self.link_open = getattr(self.link, 'open', None)
        self.stopped.set()
        return {'ok': True}

    def follow_lines(self):
        return ['  follow it: node.py follow box1 %s' % JOB,
                '  stop it:   node.py stop box1 %s' % JOB]


if __name__ == '__main__':
    unittest.main()
