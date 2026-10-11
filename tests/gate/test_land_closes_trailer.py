"""A landing closes the records a branch commit names in a `Closes:` trailer, whoever pulled them:
a record pulled from the main checkout holds `pulled_by: main`,
which no landing matches. kitlib.backlog.at_landing.close_landed with the tip and base that
kitlib.gate.land.Lander.close_pulled passes, on a ref store in a scratch repository."""
import os
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.backlog import at_landing, offer

from tests.backlog.helpers import priced
from tests.gate import test_land_store as tls
from tests.gate.test_land_store import BRANCH, StoreLandCase, pulled

TRAILERS = 'Co-Authored-By: Test Model <noreply@anthropic.com>'


class ClosesByTrailer(StoreLandCase):

    def branch_with(self, message, name='feature', path='a.py'):
        """kit/<name> with one commit, adding `path`, whose message is `message`: its tip."""
        wt = self.repo.worktree(name)
        self.repo.write(path, 'x = 1\n', root=wt)
        return self.repo.commit(message, path, cwd=wt)

    def land(self, tip):
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def warnings(self, out):
        return [l for l in out.lines if l.startswith('WARNING')]

    def notes(self, out):
        return [l for l in out.lines if l.startswith('NOTE')]

    def test_a_record_a_trailer_names_is_closed_though_main_pulled_it(self):
        self.seed(pulled('D2', by='main'), pulled('KEEP', by='main'), pulled('FOO'))
        out = self.land(self.branch_with('fix it\n\nCloses: D2\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['KEEP'])
        self.assertEqual(sorted(self.subjects()[:2]),
                         ['closes D2: landed %s' % out.merge, 'closes FOO: landed %s' % out.merge])
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 2, out.lines)
        d2 = [l for l in closed if l.startswith('closed D2 ')]
        self.assertEqual(len(d2), 1, out.lines)
        self.assertIn('named by a Closes trailer of %s; it was pulled by main' % BRANCH, d2[0])
        self.assertEqual(self.warnings(out), [])

    def test_one_trailer_may_name_several_records(self):
        # BBB's holder is a live branch: closed, and its builder must be told. DDD's holder has
        # no branch left (a dead pull) and AAA's is main: closed with nothing to tell.
        self.repo.git('branch', 'kit/other', self.main)
        self.seed(pulled('AAA', by='main'), pulled('BBB', by='kit/other'),
                  pulled('DDD', by='kit/gone'), pulled('CCC', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: AAA, BBB (both done), DDD\n%s\n'
                                         % TRAILERS))
        self.assertEqual(self.stems(), ['CCC'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('BBB', warn[0])
        self.assertIn('kit/other holds it pulled', warn[0])
        self.assertIn('tell the builder of kit/other', warn[0])
        self.assertEqual(self.notes(out), [])

    def test_a_live_holder_that_git_cannot_probe_is_warned_of_too(self):
        self.seed(pulled('BBB', by='kit/other'))
        tip = self.branch_with('fix\n\nCloses: BBB\n%s\n' % TRAILERS)
        with mock.patch.object(offer, 'live_branches', return_value=(None, 'probe failed')):
            out = self.land(tip)
        self.assertEqual(self.stems(), [])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('kit/other holds it pulled', warn[0])
        self.assertIn('probe failed', warn[0])

    def test_a_wrapped_trailer_value_is_read_unfolded(self):
        self.seed(pulled('AAA', by='main'))
        tip = self.branch_with('fix\n\nCloses: AAA (done\n  in two parts)\n%s\n' % TRAILERS)
        # folded, the continuation line would read as the id `in`
        self.assertEqual(at_landing.closes_trailers(self.repo.root, tip, self.main), (['AAA'], None))
        out = self.land(tip)
        self.assertEqual(self.stems(), [])
        self.assertEqual(self.notes(out), [])
        self.assertEqual(self.warnings(out), [])

    def test_an_owner_held_record_main_pulled_is_a_warning_and_stays(self):
        self.seed(pulled('HELD', by='main', priority='-1', priority_by='-',
                         owner_ask='may this ship?'))
        out = self.land(self.branch_with('fix\n\nCloses: HELD\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['HELD'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('owner-held', warn[0])
        self.assertIn('which a Closes trailer of its commits names', warn[0])
        # the same command (and owner's flag) as a held record the branch pulled itself:
        # test_land_store.CloseEdges checks that flag
        self.assertIn('close HELD --reason "landed %s"' % out.merge, warn[0])

    def test_a_batch_reads_the_trailers_of_each_branch(self):
        self.seed(pulled('ONE', by='main'), pulled('TWO', by='main'), pulled('KEEP', by='main'))
        tip_a = self.branch_with('a\n\nCloses: ONE\n%s\n' % TRAILERS)
        tip_b = self.branch_with('b\n\nCloses: TWO\n%s\n' % TRAILERS, name='other', path='b.py')
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b]).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), ['KEEP'])
        closed = sorted(l.split()[1] for l in out.lines if l.startswith('closed '))
        self.assertEqual(closed, ['ONE', 'TWO'], out.lines)
        self.assertTrue(any('named by a Closes trailer of kit/other' in l for l in out.lines),
                        out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_a_trailer_naming_a_record_that_is_not_pulled_is_a_warning_with_the_command(self):
        self.seed(priced('OPEN'))
        out = self.land(self.branch_with('fix\n\nCloses: OPEN\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['OPEN'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('names OPEN', warn[0])
        self.assertIn('not pulled', warn[0])
        self.assertIn('close OPEN --reason "landed %s"' % out.merge, warn[0])

    def test_ids_no_store_holds_are_one_note_line(self):
        # A typo, or a case that differs from the record's file name (the store's lookup key).
        before = self.seed(priced('BAZ'), pulled('D2', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: NOPE, d2\nCloses: D2\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['BAZ'])
        self.assertNotEqual(self.remote_store(), before)
        self.assertEqual(self.warnings(out), [])
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out.lines)
        self.assertIn('NOPE, d2', notes[0])
        self.assertIn('no backlog store', notes[0])

    def test_a_closes_line_above_a_separate_attribution_paragraph_closes(self):
        # tools/commit.py's shape: the attribution is its own last paragraph, so the Closes line
        # is not in git's trailer block; it still closes the record.
        self.seed(pulled('D2', by='main'), pulled('KEEP', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: D2\n\n%s\nClaude-Session: test-session\n'
                                         % TRAILERS))
        self.assertEqual(self.stems(), ['KEEP'])
        self.assertTrue(any(l.startswith('closed D2 ') for l in out.lines), out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_prose_that_mentions_closes_closes_nothing(self):
        before = self.seed(pulled('D2', by='main'), pulled('D3', by='main'),
                           pulled('D4', by='main'), pulled('D5', by='main'))
        out = self.land(self.branch_with(
            'fix\n\nThis closes D2 at last, and the old code said Closes: D3 too.\n'
            'closes: D4\n  Closes: D5\n\n%s\n' % TRAILERS))
        self.assertEqual(self.remote_store(), before)
        self.assertEqual(self.stems(), ['D2', 'D3', 'D4', 'D5'])
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_a_trailer_already_on_main_is_not_read(self):
        self.seed(pulled('OLD', by='kit/other'))
        self.repo.write('b.py', 'y = 1\n')
        self.main = self.repo.commit('earlier\n\nCloses: OLD\n%s\n' % TRAILERS, 'b.py')
        self.repo.git('push', '-q', 'origin', 'main')
        out = self.land(self.branch_with('fix\n\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['OLD'])
        self.assertEqual(self.warnings(out), [])

    def test_unreadable_trailers_are_a_warning_and_the_branchs_own_pulls_still_close(self):
        self.seed(pulled('FOO'), pulled('D2', by='main'))
        tip = self.branch_with('fix\n\nCloses: D2\n%s\n' % TRAILERS)
        with mock.patch.object(at_landing, 'closes_trailers', return_value=(None, 'boom')):
            out = self.land(tip)
        self.assertEqual(self.stems(), ['D2'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('Closes trailers', warn[0])
        self.assertIn('boom', warn[0])


class ClosesByTrailerInThePmDataStore(StoreLandCase):
    """The PM data branch's folder store (test_land_store.PmDataClose's set-up) takes the
    trailer's ids too, while the configured store has none of them: no NOTE."""
    setUp = tls.PmDataClose.setUp
    pm_worktree = tls.PmDataClose.pm_worktree
    record = tls.PmDataClose.record
    branch_with = ClosesByTrailer.branch_with

    def test_a_record_a_trailer_names_is_closed_in_the_pm_data_store(self):
        wt = self.pm_worktree(pulled('D2', by='main'), pulled('KEEP', by='main'))
        tip = self.branch_with('fix\n\nCloses: D2\n%s\n' % TRAILERS)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(os.path.exists(self.record(wt, 'D2')), out.lines)
        self.assertTrue(os.path.exists(self.record(wt, 'KEEP')))
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 1, out.lines)
        self.assertIn('closed D2 in the backlog store kit/backlog in ', closed[0])
        self.assertIn('named by a Closes trailer of %s; it was pulled by main' % BRANCH, closed[0])
        self.assertFalse([l for l in out.lines if l.startswith(('WARNING', 'NOTE'))], out.lines)


class TrailerValues(unittest.TestCase):

    def test_ids_are_the_first_word_of_each_comma_separated_value(self):
        self.assertEqual(at_landing.closes_ids(['EX-43', 'A, B (both)', 'kit.x_y', '#12, (C)',
                                                '  ', 'E done']),
                         ['EX-43', 'A', 'B', 'kit.x_y', 'E'])

    def test_closes_lines_are_read_anywhere_in_the_message_and_unfolded(self):
        message = ('subject Closes: NO1\n\nCloses: EX-1\n\tand more\nprose closes: NO2\n'
                   'closes: NO3\n  Closes: NO4\nCLOSES: NO5\nCloses:EX-2\n\n'
                   'Co-Authored-By: someone\n')
        self.assertEqual(at_landing.closes_values(message), ['EX-1 and more', 'EX-2'])
        self.assertEqual(at_landing.closes_values(''), [])


if __name__ == '__main__':
    unittest.main()
