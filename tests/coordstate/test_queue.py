"""The note's verdict section on real queue entries (docs/coord-state.md "The state note"): what
still waits to land, read against main as last fetched; a landed or superseded entry is left out
and stays on disk."""
import unittest
from unittest import mock

from kitlib.coordstate import log, render
from kitlib.gate import verdicts as V
from tests import inproc
from tests.coordstate.helpers import SESSION, SLUG, TempAgentFolder
from tests.gate.fixtures import GateRepoCase
from tests.gate.test_verdicts import given
from tests.gitfixture import fixture_env

COORD = inproc.load_tool('coord_state')


class Queue(TempAgentFolder, GateRepoCase):
    def setUp(self):
        super().setUp()
        self.common = self.repo.common_dir()

    def queued(self, name, **over):
        """A real branch with an entry of this session: (its worktree, the entry)."""
        wt, tip = self.branch(name, {'%s.txt' % name: name + '\n'})
        return wt, V.add(self.common, given('kit/%s' % name, tip, **dict({'session': SESSION}, **over)))[1]

    def land(self, entry):
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % entry['tip'])
        self.repo.git('fetch', '-q', 'origin')

    def shown(self):
        got, why = render.queued_for(self.common, SESSION, *render.main_as_fetched(self.repo.root))
        self.assertIsNone(why)
        return [e['branch'] for e in got]

    def on_disk(self):
        return sorted(e['branch'] for e in V.entries(self.common))

    def test_a_landed_entry_is_left_out_and_stays_on_disk(self):
        _, landed = self.queued('landed')
        self.queued('waiting')
        self.land(landed)
        self.assertEqual(self.shown(), ['kit/waiting'])
        self.assertEqual(self.on_disk(), ['kit/landed', 'kit/waiting'])

    def test_a_superseded_entry_is_left_out_and_stays_on_disk(self):
        wt, gone = self.queued('gone')
        self.repo.git('worktree', 'remove', '--force', wt)
        self.repo.git('branch', '-D', gone['branch'])
        self.queued('waiting')
        self.assertEqual(self.shown(), ['kit/waiting'])
        self.assertEqual(self.on_disk(), ['kit/gone', 'kit/waiting'])

    def test_another_sessions_entry_is_left_out(self):
        self.queued('theirs', session='coord-other')
        self.queued('mine')
        self.assertEqual(self.shown(), ['kit/mine'])

    def test_with_no_main_every_entry_is_kept_and_it_is_said(self):
        _, landed = self.queued('landed')
        self.land(landed)
        got, why = render.queued_for(self.common, SESSION, None, 'origin/main: gone')
        self.assertEqual([e['branch'] for e in got], ['kit/landed'])
        self.assertIn('origin/main: gone', why)
        self.assertIn('landed entries can be listed', why)

    def test_main_as_fetched_is_the_fetched_ref(self):
        _, landed = self.queued('landed')
        self.land(landed)
        self.assertEqual(render.main_as_fetched(self.repo.root), (landed['tip'], None))

    def test_a_queue_that_cannot_be_read_is_said(self):
        with mock.patch.object(V, 'entries', side_effect=V.VerdictError('torn')):
            self.assertEqual(render.queued_for(self.common, SESSION, 'a' * 40), (None, 'torn'))

    def test_show_lists_what_waits_in_the_real_line(self):
        _, landed = self.queued('landed')
        _, waiting = self.queued('waiting', risk='High')
        self.land(landed)
        log.start(self.env, slug=SLUG)
        rc, out, err = inproc.run_main(COORD.main, ['show', '-C', self.repo.root],
                                       fixture_env(self.env), self.repo.root)
        self.assertEqual(rc, 0, err)
        self.assertIn('Verdicts queued by %s (1)\n' % SESSION, out)
        self.assertIn('- QUEUED kit/waiting %s High LAND queued %s\n'
                      % (waiting['tip'][:10], waiting['queued_utc']), out)
        self.assertNotIn('kit/landed', out)

    def test_a_failed_entry_says_so(self):
        _, entry = self.queued('red')
        self.assertTrue(render._queued_line(dict(entry, state='failed')).startswith('- FAILED kit/red '))


if __name__ == '__main__':
    unittest.main()
