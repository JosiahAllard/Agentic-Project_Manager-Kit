"""kitlib.loadnode.dispatch: where a `machine_lock.py run` goes (here or a load node), what it says
when it stays here, and what each way out of a node returns. The registry is a temp PM
repository's, a runner stands in for ssh and NodeJob.run (or its steps) for the node."""
import contextlib
import os
import types
import unittest
from unittest import mock

from kitlib.loadnode import dispatch, job, registry, remote
from tests import gitfixture
from tests.loadnode.test_cli import REAL_RUN, Cli, run_reply
from tests.loadnode.test_registry import node
from tests.loadnode.test_remote import Runner, ok_reply


def no_ssh(argv, timeout_s):
    raise AssertionError('ssh was asked: %r' % (argv,))


class RouterCase(unittest.TestCase):
    write = Cli.write

    def setUp(self):
        Cli.setUp(self)
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.repo.write('a.txt', 'one\n')
        self.sha = self.repo.commit('add a', 'a.txt')
        self.said, self.node_runs, self.node_result = [], [], 5
        self.store = types.SimpleNamespace(say=self.said.append, testing=True)
        self.main_lock = {'cpu_slots': 12, 'cpu_free': 2, 'gpu_slots': 0, 'gpu_free': 0,
                          'waiting': 0, 'unreadable': [], 'pending': False}

        def node_run(nj):
            self.node_runs.append((nj.node.name, nj.sha, nj.cmd, nj.artifacts, nj.note))
            if isinstance(self.node_result, BaseException):
                raise self.node_result
            return self.node_result
        p = mock.patch.object(job.NodeJob, 'run', node_run)
        p.start()
        self.addCleanup(p.stop)
        self.write(node(memory_mb=4096, repos=['my-repo']), node(name='b2', repos=[]))

    def router(self, where='auto', runner=None, cwd=None, need=None, lock=None, **kw):
        return dispatch.Router(where, kw.pop('artifacts', ()), registry_file=kw.pop('registry', self.file),
                               runner=runner or Runner(out=run_reply()),
                               open_link=lambda n: contextlib.nullcontext(object()),
                               environ=kw.pop('environ', {}), cwd=cwd or self.repo.root,
                               need=need or (lambda kind, store: {'cpu': 2, 'gpu': 0}),
                               lock=lock or (lambda store: self.main_lock), reattach_s=0)

    def route(self, router, cmd=('python', 'x.py'), repo='my-repo', **kw):
        kw.setdefault('store', self.store)
        return router(list(cmd), 'engine-test', repo, 's1', kw)

    def text(self):
        return '\n'.join(self.said)


class Auto(RouterCase):

    def test_a_node_with_more_room_runs_it_and_its_code_comes_back(self):
        code = self.route(self.router(artifacts=['out/*.txt']), note='n1', wait_s=60)
        self.assertEqual(code, 5)
        self.assertEqual(self.node_runs, [('box1', self.sha, ['python', 'x.py'], ['out/*.txt'],
                                           'n1')])
        self.assertIn('running on load node box1: box1 has 3 cpu slots free', self.text())

    def test_main_with_more_room_runs_it_here(self):
        self.main_lock['cpu_free'] = 12
        self.assertIsNone(self.route(self.router()))
        self.assertEqual(self.node_runs, [])
        self.assertIn('running here: main has 12 cpu slots free', self.text())

    def test_node_main_runs_it_here_and_asks_no_node(self):
        self.main_lock['cpu_free'] = 0
        self.assertIsNone(self.route(self.router('main', runner=no_ssh)))
        self.assertEqual((self.node_runs, self.said), ([], []))

    def test_runs_no_node_takes_stay_here_and_say_nothing(self):
        self.main_lock['cpu_free'] = 0
        missing = os.path.join(self.folder, 'none.json')
        cases = [('no registry', {'registry': missing}, {}),
                 ('not opted in', {}, {'repo': 'other-repo'}),
                 ('nested', {'environ': {dispatch.NEST_ENV: 'r-1:2:3'}}, {})]
        cases += [(k, {}, {k: v}) for k, v in (
            ('cores', 1), ('hold', 'h-1'), ('gpu_share', 'training'), ('within', [8, 9]),
            ('resource', 'vm:x'), ('on_release_cmd', 'echo'), ('rid', 'r-1'), ('detached', True),
            ('env', {'A': '1'}))]
        for why, rkw, kw in cases:
            with self.subTest(why=why):
                del self.said[:]
                self.assertIsNone(self.route(self.router(runner=no_ssh, **rkw), **kw))
                self.assertEqual((self.node_runs, self.said), ([], []))

    def test_a_test_store_with_no_registry_named_never_reads_the_machines(self):
        r = self.router(runner=no_ssh, registry=None)
        with mock.patch.object(registry, 'path', side_effect=AssertionError('registry.path')):
            self.assertIsNone(self.route(r))
        self.assertEqual(self.said, [])

    def test_a_windows_command_stays_here_with_its_reason(self):
        self.main_lock['cpu_free'] = 0
        for cmd in (['build.bat'], ['tools\\x.CMD'], ['run.ps1', 'a'], ['C:/Py/python.exe', '-V'],
                    ['powershell', '-c', 'x'], ['pwsh'], ['cmd', '/c', 'x'], ['PwSh.EXE'],
                    ['python', 'C:/x.py'], ['python', 'out\\a.txt'], ['python', '--in=D:\\a'],
                    ['python', 'd:']):
            with self.subTest(cmd=cmd):
                del self.said[:]
                self.assertIsNone(self.route(self.router(runner=no_ssh), cmd=cmd))
                self.assertEqual(len(self.said), 1, self.said)
                self.assertIn('running here, not on a load node:', self.said[0])
                self.assertIn('Windows', self.said[0])
        self.assertEqual(self.node_runs, [])

    def test_a_posix_command_is_not_a_windows_one(self):
        for cmd in (['python', 'x.py', 'a/b', 'http://h:80/x', 'key=a:b', '--t=1:30'],
                    ['bash', '-c', 'echo $HOME'], ['./run.sh'], ['make', 'build']):
            self.assertEqual(dispatch.windows_only(cmd), '', cmd)

    def test_a_folder_that_is_not_the_top_or_uncommitted_work_stays_here(self):
        self.main_lock['cpu_free'] = 0
        sub = os.path.join(self.repo.root, 'sub')
        os.mkdir(sub)
        self.assertIsNone(self.route(self.router(runner=no_ssh, cwd=sub)))
        self.assertIn("a node runs in the checkout's top", self.text())
        self.repo.write('a.txt', 'two\n')
        del self.said[:]
        self.assertIsNone(self.route(self.router(runner=no_ssh)))
        self.assertIn('tracked changes not committed', self.text())
        self.assertEqual(self.node_runs, [])

    def test_a_job_the_node_would_refuse_stays_here(self):
        self.main_lock['cpu_free'] = 0
        for kw, want in (({'note': 'n' * 201}, 'note'), ({'wait_s': 8 * 86400.0}, 'wait_s'),
                         ({'expect_s': 8 * 86400.0}, 'expect_s')):
            with self.subTest(kw=list(kw)):
                del self.said[:]
                self.assertIsNone(self.route(self.router(runner=no_ssh), **kw))
                self.assertIn('a node would refuse it: %s' % want, self.text())
        self.assertEqual(self.node_runs, [])

    def test_an_unknown_kind_is_left_to_the_lock_here(self):
        def need(kind, store):
            raise KeyError(kind)
        self.main_lock['cpu_free'] = 0
        self.assertIsNone(self.route(self.router(runner=no_ssh, need=need)))
        self.assertEqual(self.said, [])

    def test_a_lock_here_that_cannot_be_read_stays_here(self):
        def lock(store):
            raise OSError('no store')
        self.assertIsNone(self.route(self.router(lock=lock)))
        self.assertIn("this machine's lock cannot be read (OSError)", self.text())
        self.assertEqual(self.node_runs, [])

    def test_a_node_that_fails_before_its_start_or_is_busy_gives_way_to_here(self):
        self.main_lock['cpu_free'] = 0
        for err in (job.JobError(job.EXIT_NODE, 'box1: the push failed', fallback=True),
                    job.JobError(job.EXIT_BUSY, 'box1 is busy', fallback=True),
                    ConnectionResetError(104, 'reset')):
            with self.subTest(err=type(err).__name__):
                self.node_result, self.node_runs[:] = err, []
                self.assertIsNone(self.route(self.router()))
                self.assertEqual(len(self.node_runs), 1)
                self.assertIn('running here instead', self.text())

    def test_a_failure_after_the_start_never_runs_here(self):
        self.main_lock['cpu_free'] = 0
        for result, want in ((job.JobError(job.EXIT_AFTER_START, 'box1: no such job'),
                              job.EXIT_AFTER_START), (job.EXIT_LOST, job.EXIT_LOST),
                             (job.EXIT_ARTIFACTS, job.EXIT_ARTIFACTS)):
            with self.subTest(want=want):
                self.node_result, self.said[:] = result, []
                self.assertEqual(self.route(self.router()), want)
                self.assertNotIn('running here instead', self.text())

    def test_the_post_start_codes_are_not_the_lock_tools_codes(self):
        from kitlib.lock import store as st
        lock_codes = {st.EXIT_FAIL, st.EXIT_USAGE, st.EXIT_BUSY, st.EXIT_TIMEOUT, st.EXIT_REQUIRE,
                      st.EXIT_RESERVED, st.EXIT_HOLD}
        self.assertEqual({job.EXIT_LOST, job.EXIT_ARTIFACTS, job.EXIT_AFTER_START} & lock_codes,
                         set())
        self.assertEqual(len({job.EXIT_LOST, job.EXIT_ARTIFACTS, job.EXIT_AFTER_START}), 3)


class RealRun(RouterCase):
    """The real NodeJob.run with its network steps stood in: its started mark decides."""

    def steps(self, follow, stop=None):
        ps = [mock.patch.object(job.NodeJob, 'run', REAL_RUN),
              mock.patch.object(job.NodeJob, 'push', lambda nj: None),
              mock.patch.object(job.NodeJob, 'start', lambda nj: {'ok': True}),
              mock.patch.object(job.NodeJob, 'follow', follow)]
        if stop is not None:
            ps.append(mock.patch.object(job.NodeJob, 'stop', stop))
        stack = contextlib.ExitStack()
        for p in ps:
            stack.enter_context(p)
        return stack

    def test_an_os_error_after_the_start_is_the_after_start_code(self):
        def follow(nj):
            raise PermissionError(13, 'denied', 'out/a.txt')
        self.main_lock['cpu_free'] = 0
        with self.steps(follow):
            self.assertEqual(self.route(self.router()), job.EXIT_AFTER_START)
        self.assertIn('it is not run again on main', self.text())
        self.assertNotIn('running here instead', self.text())

    def test_a_lost_connection_is_9_with_the_follow_and_stop_lines(self):
        self.main_lock['cpu_free'] = 0
        with self.steps(lambda nj: None):
            self.assertEqual(self.route(self.router()), job.EXIT_LOST)
        self.assertIn('LOST: the connection to box1', self.text())
        self.assertIn('node.py" follow box1 j-', self.text())

    def test_ctrl_c_after_the_start_asks_the_node_to_stop_and_says_how_to_follow(self):
        stops = []

        def follow(nj):
            raise KeyboardInterrupt

        def stop(nj):
            stops.append(nj.job)
            return {'ok': True, 'asked': 'term'}
        self.main_lock['cpu_free'] = 0
        with self.steps(follow, stop):
            self.assertEqual(self.route(self.router()), 130)
        self.assertEqual(len(stops), 1)
        self.assertIn('Ctrl+C: job %s on box1 asked to stop' % stops[0], self.text())
        self.assertIn('follow box1 %s' % stops[0], self.text())

    def test_ctrl_c_before_any_job_runs_nothing_here_or_there(self):
        r = self.router()
        with mock.patch.object(remote, 'query_all', side_effect=KeyboardInterrupt):
            self.assertEqual(self.route(r), 130)
        self.assertIn('Ctrl+C: nothing was started on a load node', self.text())
        self.assertEqual(self.node_runs, [])


class Named(RouterCase):

    def test_the_named_node_runs_it_without_asking_any_node(self):
        self.main_lock['cpu_free'] = 12
        self.assertEqual(self.route(self.router('box1', runner=no_ssh)), 5)
        self.assertEqual([r[0] for r in self.node_runs], ['box1'])

    def test_what_the_named_node_cannot_take_is_usage(self):
        for kw, want in (({}, 'no node named nope'), ({'cmd': ['x.bat']}, 'Windows program'),
                         ({'cores': 2}, 'does not take --cores'),
                         ({'resource': 'vm:x'}, 'does not take --resource'),
                         ({'note': 'n' * 201}, 'a node would refuse it: note')):
            name = 'nope' if 'nope' in want else 'box1'
            with self.subTest(want=want):
                del self.said[:]
                self.assertEqual(self.route(self.router(name, runner=no_ssh), **kw), 2)
                self.assertTrue(self.text().startswith('usage: --node %s: ' % name), self.said)
                self.assertIn(want, self.text())
        del self.said[:]
        self.assertEqual(self.route(self.router('b2', runner=no_ssh)), 2)
        self.assertIn('b2 has not opted in my-repo', self.text())
        self.assertEqual(self.node_runs, [])

    def test_a_named_node_that_cannot_be_used_is_10_and_never_runs_here(self):
        self.node_result = job.JobError(job.EXIT_NODE, 'box1: the push failed', fallback=True)
        self.assertEqual(self.route(self.router('box1')), job.EXIT_NODE)
        self.assertNotIn('running here instead', self.text())


class StatusLines(unittest.TestCase):
    write = Cli.write

    def setUp(self):
        Cli.setUp(self)

    def test_no_registry_is_no_line(self):
        self.assertEqual(dispatch.status_lines(os.path.join(self.folder, 'none.json'),
                                               runner=no_ssh), [])
        with mock.patch.object(registry, 'path', side_effect=AssertionError('registry.path')):
            self.assertEqual(dispatch.status_lines(None, testing=True, runner=no_ssh), [])

    def test_a_line_per_node_with_its_room_or_its_state(self):
        self.write(node(), node(name='b2'))

        def runner(argv, timeout_s):
            if 'HostKeyAlias=pmkit-node-b2' in argv:
                return 255, b'', b'Host key verification failed.\n'
            return 0, ok_reply(), b''
        lines = dispatch.status_lines(self.file, runner=runner)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith('node: box1  ok  cpu 3/4 free  gpu 0/0 free  memory '
                                            '8.9/15.5 GB free  1 running, 0 waiting'), lines[0])
        self.assertTrue(lines[1].startswith('node: b2  host key mismatch'), lines[1])

    def test_a_registry_that_cannot_be_used_is_one_line(self):
        self.write(node(address='-oProxyCommand=x'))
        lines = dispatch.status_lines(self.file, runner=no_ssh)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('node: the load node registry cannot be used'))


if __name__ == '__main__':
    unittest.main()
