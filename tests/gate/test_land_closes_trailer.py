"""A landing closes the records a branch commit names in a `Closes:` trailer, whoever pulled them:
a record pulled from the main checkout holds `pulled_by: main`,
which no landing matches. kitlib.backlog.at_landing.close_landed with the tip and base that
kitlib.gate.land.Lander.close_pulled passes, on a ref store in a scratch repository."""
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.backlog import at_landing

from tests.backlog.helpers import priced
from tests.gate.test_land_store import BRANCH, StoreLandCase, pulled

TRAILERS = 'Co-Authored-By: Test Model <noreply@anthropic.com>'


class ClosesByTrailer(StoreLandCase):

    def branch_with(self, message, name='feature'):
        """kit/<name> with one commit whose message is `message`: its tip."""
        wt = self.repo.worktree(name)
        self.repo.write('a.py', 'x = 1\n', root=wt)
        return self.repo.commit(message, 'a.py', cwd=wt)

    def land(self, tip):
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def warnings(self, out):
        return [l for l in out.lines if l.startswith('WARNING')]

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

    def test_one_trailer_may_name_several_records_in_any_case_of_the_key(self):
        self.seed(pulled('AAA', by='main'), pulled('BBB', by='kit/other'), pulled('CCC', by='main'))
        out = self.land(self.branch_with('fix\n\ncloses: AAA, BBB (both done)\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['CCC'])
        self.assertEqual(self.warnings(out), [])

    def test_a_trailer_naming_a_record_that_is_not_pulled_is_a_warning_with_the_command(self):
        self.seed(priced('OPEN'))
        out = self.land(self.branch_with('fix\n\nCloses: OPEN\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['OPEN'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('names OPEN', warn[0])
        self.assertIn('not pulled', warn[0])
        self.assertIn('close OPEN --reason "landed %s"' % out.merge, warn[0])

    def test_a_trailer_naming_no_record_says_nothing(self):
        before = self.seed(priced('BAZ'))
        out = self.land(self.branch_with('fix\n\nCloses: NOPE\n%s\n' % TRAILERS))
        self.assertEqual(self.remote_store(), before)
        self.assertEqual(self.warnings(out), [])

    def test_a_closes_line_that_is_not_a_trailer_closes_nothing(self):
        before = self.seed(pulled('D2', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: D2\n\nmore words after it\n'))
        self.assertEqual(self.remote_store(), before)
        self.assertEqual(self.stems(), ['D2'])
        self.assertEqual(self.warnings(out), [])

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


class TrailerValues(unittest.TestCase):

    def test_ids_are_the_first_word_of_each_comma_separated_value(self):
        self.assertEqual(at_landing.closes_ids(['EX-43', 'A, B (both)', 'kit.x_y', '#12, (C)',
                                                '  ', 'E done']),
                         ['EX-43', 'A', 'B', 'kit.x_y', 'E'])


if __name__ == '__main__':
    unittest.main()
