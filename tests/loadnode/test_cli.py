"""tools/node.py: its arguments and exit codes. The registry is a temp PM repository's; a runner
stands in for ssh."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.loadnode import box, job, registry
from tests import gitfixture
from tests import home as thome
from tests.loadnode.test_registry import node, text
from tests.loadnode.test_remote import Runner, ok_reply
from tools import node as node_cli

REAL_RUN = job.NodeJob.run       # Run stands NodeJob.run in; a test that needs the real one asks


class Cli(unittest.TestCase):

    def setUp(self):
        self.home = thome.temp_home(self)
        self.folder = thome.machine_config(self.home, {})
        self.file = os.path.join(self.folder, registry.FILE_NAME)
        p = mock.patch.object(registry, 'ignored', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def write(self, *nodes):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write(text(*nodes))

    def run_cli(self, argv, runner=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = node_cli.main(argv, runner=runner)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_list_needs_no_network(self):
        self.write(node(), node(name='b2', identity_file='~/.ssh/k'))
        code, out, _ = self.run_cli(['list'], runner=mock.Mock(side_effect=AssertionError))
        self.assertEqual(code, 0)
        self.assertIn('box1  pmnode@192.168.1.50:22', out)
        self.assertIn('~/.ssh/k (MISSING)', out)

    def test_status_every_node_answers_is_0(self):
        self.write(node(), node(name='b2'))
        code, out, _ = self.run_cli(['status'], Runner(out=ok_reply()))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)

    def test_status_one_node_failing_is_1_and_every_node_is_listed(self):
        self.write(node(), node(name='b2'))

        def runner(argv, timeout_s):
            return (255, b'', b'ssh: connect: No route to host\n') if 'HostKeyAlias=pmkit-node-b2' in argv \
                else (0, ok_reply(), b'')
        code, out, _ = self.run_cli(['status'], runner)
        self.assertEqual(code, 1)
        self.assertIn('box1', out)
        self.assertIn('b2: unreachable: ssh: connect: No route to host', out)

    def test_status_of_named_nodes_and_json(self):
        self.write(node(), node(name='b2'))
        run = Runner(out=ok_reply())
        code, out, _ = self.run_cli(['status', 'b2', '--json', '--timeout', '5'], run)
        self.assertEqual(code, 0)
        got = json.loads(out)
        self.assertEqual(list(got), ['b2'])
        self.assertEqual((got['b2']['state'], run.timeout), ('ok', 5))

    def test_an_unknown_name_is_usage(self):
        self.write(node())
        code, _, err = self.run_cli(['status', 'nope'], Runner())
        self.assertEqual(code, 2)
        self.assertIn('no node named nope', err)

    def test_a_bad_or_missing_registry_is_2(self):
        code, _, err = self.run_cli(['list'])
        self.assertEqual(code, 2)
        self.assertIn('does not exist', err)
        self.write(node(address='-oProxyCommand=x'))
        code, _, err = self.run_cli(['status'], Runner())
        self.assertEqual(code, 2)
        self.assertIn('REFUSED', err)

    def test_a_registry_git_does_not_ignore_is_2(self):
        self.write(node())
        with mock.patch.object(registry, 'ignored', return_value='not ignored by git'):
            code, _, err = self.run_cli(['list'])
        self.assertEqual(code, 2)
        self.assertIn('not ignored', err)

    def test_usage_errors_are_2(self):
        for argv in ([], ['run'], ['status', '--timeout', '0'], ['status', '--timeout', 'x'],
                     ['list', 'extra'], ['status', '--jso']):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv)[0], 2)

    def test_an_empty_registry(self):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write('{"version": 1, "nodes": []}')
        code, out, _ = self.run_cli(['status'], Runner())
        self.assertEqual((code, 'no load nodes' in out), (0, True))


    def test_the_registry_seam_reads_the_file_named(self):
        d = tempfile.mkdtemp(prefix='kit-cli-reg-')
        self.addCleanup(shutil.rmtree, d, True)
        other = os.path.join(d, 'nodes.json')
        with open(other, 'w', encoding='utf-8') as f:
            f.write(text(node(name='elsewhere')))
        code, out, _ = self.run_cli(['--registry', other, 'list'])
        self.assertEqual(code, 0)
        self.assertIn('elsewhere', out)
        self.assertNotIn('--registry', node_cli.parser().format_help())


def run_reply(**over):
    d = json.loads(ok_reply().decode())
    d.update({'verbs': list(box.RUN_VERBS) + ['status'], 'jobs': 0})
    d.update(over)
    return json.dumps(d).encode() + b'\n'


class Run(unittest.TestCase):
    """node.py run: where a job goes and what each way out returns. The node's side is
    tests/loadnode/test_job.py; here NodeJob.run and the main machine's lock run are stood in."""

    JOB = 'j-20261028T120000Z-abcdef'
    write, run_cli = Cli.write, Cli.run_cli

    def setUp(self):
        Cli.setUp(self)
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.repo.write('a.txt', 'one\n')
        self.sha = self.repo.commit('add a', 'a.txt')
        self.mains, self.node_runs = [], []
        self.main_lock = {'cpu_slots': 12, 'cpu_free': 2, 'gpu_slots': 0, 'gpu_free': 0,
                          'waiting': 0, 'unreadable': [], 'pending': False}
        self.node_result = 0

        def run_main(a, cmd, top):
            self.mains.append((cmd, os.path.normcase(top)))
            return 7

        def node_run(nj):
            self.node_runs.append((nj.node.name, nj.sha, nj.cmd, nj.artifacts))
            if isinstance(self.node_result, Exception):
                raise self.node_result
            return self.node_result

        for name, value in (('_run_main', run_main), ('_need', lambda kind: {'cpu': 2, 'gpu': 0}),
                            ('_main_lock', lambda: self.main_lock),
                            ('_link', lambda n: contextlib.nullcontext(object())),
                            ('_notice_dir', lambda kind: None)):
            p = mock.patch.object(node_cli, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(job.NodeJob, 'run', node_run)
        p.start()
        self.addCleanup(p.stop)
        self.write(node(memory_mb=4096, repos=['my-repo']), node(name='b2', repos=[]))

    def run_(self, *extra, runner=None, cmd=('python', 'x.py')):
        argv = ['run', '--kind', 'engine-test', '--repo', 'my-repo', '--slug', 's1',
                '-C', self.repo.root] + list(extra) + ['--'] + list(cmd)
        return self.run_cli(argv, runner or Runner(out=run_reply()))

    def top(self):
        return os.path.normcase(os.path.normpath(self.repo.root))

    def test_usage_errors_are_2_and_run_nothing(self):
        for extra, cmd, want in (
                ([], (), 'no command'),
                (['--node', 'nope'], ('x',), 'no node named nope'),
                (['--node', 'b2'], ('x',), 'b2 has not opted in my-repo'),
                (['--artifact', '../x'], ('x',), 'not a relative glob'),
                (['--artifact', '.git/config'], ('x',), 'not a relative glob'),
                (['--artifact', 'out/caf%s.txt' % chr(0xe9)], ('x',), 'ASCII'),
                (['--no-wait', '--max-wait', '5m'], ('x',), 'not allowed with'),
                (['--max-wait', '9d'], ('x',), 'at most 7d'),
                (['--reattach', '-1'], ('x',), 'from 0 to 86400')):
            with self.subTest(extra=extra, cmd=cmd):
                code, _, err = self.run_(*extra, cmd=cmd)
                self.assertEqual(code, 2)
                self.assertIn(want, err)
        self.assertEqual((self.mains, self.node_runs), ([], []))

    def test_an_unknown_kind_is_2(self):
        def need(kind):
            raise KeyError(kind)
        with mock.patch.object(node_cli, '_need', need):
            code, _, err = self.run_()
        self.assertEqual(code, 2)
        self.assertIn('no kind engine-test', err)

    def test_a_named_node_runs_head_and_returns_the_job_exit(self):
        self.node_result = 3
        code, _, _ = self.run_('--node', 'box1', '--artifact', 'out/*.txt')
        self.assertEqual(code, 3)
        self.assertEqual(self.node_runs, [('box1', self.sha, ['python', 'x.py'], ['out/*.txt'])])
        self.assertEqual(self.mains, [])

    def test_a_named_node_with_uncommitted_changes_is_2(self):
        self.repo.write('a.txt', 'two\n')
        code, _, err = self.run_('--node', 'box1')
        self.assertEqual(code, 2)
        self.assertIn('commit first', err)
        self.assertEqual(self.node_runs, [])

    def test_a_named_node_that_fails_never_falls_back(self):
        self.node_result = job.JobError(job.EXIT_NODE, 'box1: the push failed', fallback=True)
        code, _, err = self.run_('--node', 'box1')
        self.assertEqual(code, job.EXIT_NODE)
        self.assertIn('the push failed', err)
        self.assertEqual(self.mains, [])

    def test_auto_takes_the_node_with_more_free(self):
        code, _, err = self.run_()
        self.assertEqual(code, 0)
        self.assertEqual([r[0] for r in self.node_runs], ['box1'])
        self.assertIn('running on box1', err)

    def test_auto_asks_only_the_nodes_that_opted_in(self):
        run = Runner(out=run_reply())
        self.run_(runner=run)
        self.assertIn('HostKeyAlias=pmkit-node-box1', run.argv)

    def test_auto_stays_on_main_when_main_has_as_many_free(self):
        self.main_lock['cpu_free'] = 3
        code, _, err = self.run_()
        self.assertEqual((code, self.mains), (7, [(['python', 'x.py'], self.top())]))
        self.assertIn('running on main', err)

    def test_auto_falls_back_to_main_when_the_node_fails_before_its_job(self):
        for err in (job.JobError(job.EXIT_BUSY, 'box1 is busy', fallback=True),
                    job.JobError(job.EXIT_NODE, 'box1: the push failed', fallback=True)):
            with self.subTest(err=err.message):
                self.mains = []
                self.node_result = err
                code, _, text_ = self.run_()
                self.assertEqual((code, len(self.mains)), (7, 1))
                self.assertIn('running on main instead', text_)

    def test_auto_keeps_a_node_failure_after_its_job_started(self):
        self.node_result = job.JobError(job.EXIT_AFTER_START, 'box1: no reply', fallback=False)
        code, _, _ = self.run_()
        self.assertEqual((code, self.mains), (job.EXIT_AFTER_START, []))

    def test_an_os_error_after_the_node_took_the_job_never_runs_it_on_main(self):
        # the real NodeJob.run, so its started mark is what tells node.py the node took the job
        def follow(nj):
            self.node_runs.append(nj.node.name)
            raise PermissionError(13, 'The process cannot access the file', 'out/a.txt')
        for extra in ((), ('--node', 'box1')):
            with self.subTest(extra=extra):
                self.mains, self.node_runs = [], []
                with mock.patch.object(job.NodeJob, 'run', REAL_RUN), \
                        mock.patch.object(job.NodeJob, 'push', lambda nj: None), \
                        mock.patch.object(job.NodeJob, 'start', lambda nj: {'ok': True}), \
                        mock.patch.object(job.NodeJob, 'follow', follow):
                    code, _, err = self.run_(*extra)
                self.assertEqual((code, self.mains, self.node_runs),
                                 (job.EXIT_AFTER_START, [], ['box1']))
                self.assertIn('it is not run again on main', err)
                self.assertNotIn('running on main instead', err)

    def test_an_os_error_before_the_node_took_the_job_falls_back(self):
        self.node_result = OSError('ssh would not start')
        code, _, err = self.run_()
        self.assertEqual((code, len(self.mains)), (7, 1))
        self.assertIn('box1 could not be used', err)

    def test_auto_runs_on_main_for_each_reason_a_node_cannot_be_asked(self):
        def no_lock():
            raise OSError('x')
        for why, setup in (
                ('not committed', lambda: self.repo.write('a.txt', 'two\n')),
                ('no node has opted in', lambda: self.write(node(repos=[]))),
                ('no load node', lambda: os.remove(self.file)),
                ('lock cannot be read', lambda: setattr(node_cli, '_main_lock', no_lock))):
            with self.subTest(why=why):
                self.repo.write('a.txt', 'one\n')
                self.write(node(memory_mb=4096, repos=['my-repo']))
                self.mains = []
                saved = node_cli._main_lock
                setup()
                try:
                    code, _, err = self.run_()
                finally:
                    node_cli._main_lock = saved
                self.assertEqual((code, len(self.mains), self.node_runs), (7, 1, []))
                self.assertIn(why, err)

    def test_a_node_whose_kit_has_no_run_verbs_is_passed_over(self):
        code, _, err = self.run_(runner=Runner(out=ok_reply()))
        self.assertEqual((code, len(self.mains)), (7, 1))
        self.assertIn('no run verbs', err)

    def test_follow_and_stop_check_the_node_and_the_job_id(self):
        for argv, want in ((['follow', 'nope', self.JOB], 'no node named nope'),
                           (['follow', 'box1', 'j-1'], 'not a job id'),
                           (['stop', 'box1', '../x'], 'not a job id'),
                           (['stop', 'box1', self.JOB, '--', 'x'], 'only run takes')):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn(want, err)

    def test_follow_that_loses_the_node_is_9_with_the_lines(self):
        with mock.patch.object(job.NodeJob, 'follow', return_value=None):
            code, _, err = self.run_cli(['follow', 'box1', self.JOB])
        self.assertEqual(code, job.EXIT_LOST)
        self.assertIn('node.py" follow box1 %s' % self.JOB, err)
        self.assertIn('node.py" stop box1 %s' % self.JOB, err)

    def test_follow_finishes_with_the_job_exit(self):
        with mock.patch.object(job.NodeJob, 'follow', return_value={'exit': 4, 'how': 'ended'}), \
                mock.patch.object(job.NodeJob, 'clean', return_value=True):
            code, _, _ = self.run_cli(['follow', 'box1', self.JOB])
        self.assertEqual(code, 4)

    def test_stop_says_what_the_node_did(self):
        for d, code_, want in (({'ok': True, 'asked': 'TERM'}, 0, 'asked to stop (TERM)'),
                               ({'ok': True, 'ended': True}, 0, 'had ended already'),
                               ({'ok': False, 'error': 'no job here'}, 1, 'refused: no job here')):
            with self.subTest(want=want):
                with mock.patch.object(job.NodeJob, 'stop', return_value=d):
                    code, out, err = self.run_cli(['stop', 'box1', self.JOB])
                self.assertEqual(code, code_)
                self.assertIn(want, out + err)


if __name__ == '__main__':
    unittest.main()
