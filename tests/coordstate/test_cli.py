"""tools/coord_state.py's verbs and exit codes (docs/coord-state.md "Commands"): start, show
(written to state-<session>.md, or printed only; another session's with --session), reported,
stop; 1 when the log is off, 2 on usage. Run in process against a temp repository."""
import os
import unittest
from unittest import mock

from kitlib import sessionmark
from kitlib.coordstate import log
from kitlib.gate import result
from kitlib.hubsite import landings
from tests import inproc
from tests.coordstate.helpers import SESSION, SLUG, TempAgentFolder
from tests.gitfixture import TempRepo, fixture_env
from tests.home import result_drive

COORD = inproc.load_tool('coord_state')
TIP = 'd' * 40


class Cli(TempAgentFolder):
    def setUp(self):
        super().setUp()
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        result_drive(self)

    def run_tool(self, *args, env=None):
        e = fixture_env(self.env)
        e.update(env or {})
        return inproc.run_main(COORD.main, list(args) + ['-C', self.repo.root], e, self.repo.root)

    def ok(self, *args, env=None):
        rc, out, err = self.run_tool(*args, env=env)
        self.assertEqual(rc, 0, out + err)
        return out, err

    def test_start_show_reported_stop(self):
        out, _ = self.ok('start', '--slug', SLUG)
        self.assertTrue(out.startswith('STARTED '), out)
        f = log.files(log.locate(self.env), SESSION)
        log.note('land_submit', self.env, branches=['kit/b'], tips=[TIP], output=None)
        out, _ = self.ok('show')
        self.assertIn('Landings in flight (1)', out)
        self.assertIn('- kit/b at dddddddddd', out)
        with open(f.state, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), out)
        out, _ = self.ok('reported', 'kit/b', '--tip', TIP[:8])
        self.assertEqual(out.strip(), 'MARKED kit/b at %s' % TIP[:8])
        out, _ = self.ok('show', '--no-write')
        self.assertIn('## Reported (latest 1 of 1)', out)
        out, _ = self.ok('stop')
        self.assertTrue(out.startswith('STOPPED '), out)
        rc, _out, err = self.run_tool('show')
        self.assertEqual(rc, 1)
        self.assertIn('NOT ON', err)

    def test_a_landing_whose_feed_post_failed_stays_to_report_until_marked(self):
        self.ok('start', '--slug', SLUG)
        output = os.path.join(self.temp, 'kit-b.1.land.log')
        with open(output, 'w', encoding='utf-8') as fh:
            fh.write('result record: X\n%s\nLANDED kit/b on main at abc\n'
                     % landings._warning(['kit/b'], 'no hub running'))
        log.note('land_submit', self.env, branches=['kit/b'], tips=[TIP], output=output)
        result.write(self.repo.root, result.build('kit/b', TIP, None, 'land', 0, summary='landed'))
        out, _ = self.ok('show')
        self.assertIn('Landings to report to the PM (1)', out)
        self.assertIn('was not posted to the hub feed', out)
        self.ok('reported', 'kit/b')
        out, _ = self.ok('show')
        self.assertIn('Landings to report to the PM (0)', out)
        self.assertIn('## Reported (latest 1 of 1)', out)

    def test_a_successor_reads_its_predecessors_note(self):
        self.ok('start', '--slug', SLUG)
        with open(log.files(log.locate(self.env), SESSION).judgments, 'w', encoding='utf-8') as fh:
            fh.write('hold kit/x until the owner answers\n')
        successor = {sessionmark.SESSION_NAME_ENV: 'coord-next'}
        out, _ = self.ok('show', '--session', SESSION, '--no-write', env=successor)
        self.assertIn('hold kit/x until the owner answers', out)
        rc, _out, err = self.run_tool('show', env=successor)
        self.assertEqual(rc, 1)
        self.assertIn('coord-next', err)

    def test_show_compacts_a_large_log(self):
        self.ok('start', '--slug', SLUG)
        for i in range(40):
            log.note('backlog', self.env, verb='set', id='R%d' % i)
        with mock.patch.object(log, 'COMPACT_AT', 100):
            _out, err = self.ok('show')
        self.assertIn('COMPACTED', err)
        events = log.read_events(log.files(log.locate(self.env), SESSION).events)[0]
        self.assertEqual([e['id'] for e in events], ['R%d' % i for i in range(25, 40)])

    def test_off_and_usage(self):
        rc, _out, err = self.run_tool('reported', 'kit/b')
        self.assertEqual(rc, 1)
        self.assertIn('NOT ON', err)
        rc, _out, _err = self.run_tool('stop')
        self.assertEqual(rc, 1)
        rc, _out, err = self.run_tool('bogus')
        self.assertEqual(rc, 2)
        self.assertIn('USAGE:', err)
        env = {sessionmark.SESSION_NAME_ENV: ''}
        rc, _out, err = self.run_tool('start', '--slug', SLUG, env=env)
        self.assertEqual(rc, 1)
        self.assertIn(sessionmark.SESSION_NAME_ENV, err)
        self.assertEqual(os.listdir(self.temp), [])


if __name__ == '__main__':
    unittest.main()
