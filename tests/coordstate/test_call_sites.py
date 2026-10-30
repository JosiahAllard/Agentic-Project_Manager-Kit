"""The kit tools that append to a coordinator's event log (docs/coord-state.md "Who appends"):
land.py --submit and gate.py land --submit append one land_submit event once a submission is
taken, and none when it is not; backlog.py appends one backlog event after a write it published.
With the log off they write nothing. Run in process: the submission itself is stubbed."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib import sessionmark
from kitlib.coordstate import log
from kitlib.gate import submit as submitmod
from tests import inproc
from tests.backlog.helpers import ADD_FLAGS, kit_json_ref
from tests.coordstate.helpers import TempAgentFolder
from tests.gitfixture import TempRepo, fixture_env, rmtree
from tests.home import machine_config

LAND = inproc.load_tool('land')
GATE = inproc.load_tool('gate')
BACKLOG = inproc.load_tool('backlog')
TIP = 'c' * 40
OUTPUT = 'Z:/results/kit-b.123.land.log'


class Submissions(TempAgentFolder):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix='kit-coordstate-root-')
        self.addCleanup(rmtree, self.root)

    def run_land(self, tool, code=G.OK):
        taken = G.Outcome(code, ['SUBMITTED kit/b at %s' % TIP[:10]], output=OUTPUT)
        if tool is LAND:
            argv = ['kit/b', '--expect-tip', TIP]
        else:
            argv = ['land', 'kit/b', '--expect-tip', TIP]
        argv += ['--submit', submitmod.REVIEWED_BY, 'analyst-1', '-C', self.root]
        with mock.patch.object(submitmod, 'submit', return_value=taken) as stub:
            rc, out, err = inproc.run_main(tool.main, argv, fixture_env(self.env), self.root)
        self.assertEqual(stub.call_count, 1, out + err)
        self.assertEqual(rc, code, out + err)
        self.assertIn('SUBMITTED kit/b', out)

    def test_a_taken_submission_appends_its_event(self):
        for tool in (LAND, GATE):
            with self.subTest(tool=tool.__name__):
                f = self.turn_on()
                self.run_land(tool)
                events = self.events(f)
                self.assertEqual(len(events), 1)
                ev = events[0]
                self.assertEqual((ev['kind'], ev['branches'], ev['tips'], ev['output']),
                                 ('land_submit', ['kit/b'], [TIP], OUTPUT))
                self.assertIn('analyst-1', ev['reviewed_by'])
                log.stop(self.env)
                os.remove(f.off)

    def test_a_refused_submission_appends_nothing(self):
        for tool in (LAND, GATE):
            with self.subTest(tool=tool.__name__):
                f = self.turn_on()
                self.run_land(tool, code=G.REFUSED)
                self.assertEqual(self.events(f), [])

    def test_with_the_log_off_nothing_is_written(self):
        self.run_land(LAND)
        self.run_land(GATE)
        self.assertEqual(os.listdir(self.temp), [])


class BacklogWrites(TempAgentFolder):
    def setUp(self):
        super().setUp()
        self.repo = TempRepo(bare_remote=True, prepare=kit_json_ref,
                             prepare_key='test_cli.Cli.kit_json_ref')
        self.addCleanup(self.repo.cleanup)
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, self.home)
        machine_config(self.home, {}, case=self)
        self.tool_env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        self.tool_env.pop(sessionmark.ENV, None)
        self.tool_env.update(self.env)

    def ok(self, *args):
        rc, out, err = inproc.run_main(BACKLOG.main, ['-C', self.repo.root] + list(args),
                                       self.tool_env, self.repo.root)
        self.assertEqual(rc, 0, out + err)

    def add(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)

    def test_a_write_appends_one_event(self):
        f = self.turn_on()
        self.add()
        self.ok('show', 'FOO')                       # a read appends nothing
        events = self.events(f)
        self.assertEqual([(e['kind'], e['verb'], e['id']) for e in events], [('backlog', 'add', 'FOO')])

    def test_with_the_log_off_a_write_writes_nothing_here(self):
        self.add()
        self.assertEqual(os.listdir(self.temp), [])


if __name__ == '__main__':
    unittest.main()
