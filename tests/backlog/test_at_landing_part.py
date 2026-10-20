"""A landing that is one part of a record: a `Part-of: <ID>` line in the branch's own commits
makes the landing close only what its `Closes:` lines name and release every other record the
branch holds pulled (kitlib.backlog.at_landing.close_landed; docs/backlog-store.md "Closing at
landing"). Without such a line the branch's pulls close as before. Scratch repositories with a
ref store and with the PM data branch's folder store, landed by kitlib.gate.land.Lander."""
import os
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import gate as G
from kitlib.backlog import at_landing
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod

from tests.backlog.helpers import priced
from tests.gate import test_land_store as tls
from tests.gate.test_land_store import BRANCH, StoreLandCase, pulled

TRAILERS = 'Co-Authored-By: Test Model <noreply@anthropic.com>'


def release_reason(merge, branch=BRANCH):
    return 'part landed %s (a Part-of line of %s); the rest is not done' % (merge, branch)


class PartLanding(StoreLandCase):

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
        # The trailer NOTEs; not the landing scope check's (tests/gate/test_land_scope.py).
        return [l for l in out.lines if l.startswith('NOTE')
                and 'its paths were not checked against a scope' not in l]

    def started(self, out, word):
        return [l for l in out.lines if l.startswith(word + ' ')]

    def get(self, rid):
        return self.backend().load(rmod.Rules()).get(rid)

    def assert_open(self, rid):
        rec = self.get(rid)
        self.assertIsNotNone(rec, rid)
        self.assertEqual(rec.get('state'), 'open')
        self.assertIsNone(rec.get('pulled_by'))
        self.assertIsNone(rec.get('pulled_at'))

    def test_a_part_of_line_releases_the_record_its_branch_holds(self):
        self.seed(pulled('FOO'), pulled('QUX', by='kit/other'), priced('BAZ'))
        out = self.land(self.branch_with('part one\n\nPart-of: FOO\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['BAZ', 'FOO', 'QUX'])
        self.assert_open('FOO')
        # The landing's cleanup deletes the branch, so the release names no kept branch.
        self.assertNotIn('Stopped on branch', self.get('FOO').body or '')
        self.assertEqual(self.get('QUX').get('pulled_by'), 'kit/other')
        # One commit, found by `git log --grep "^releases FOO: "`.
        self.assertEqual(self.subjects()[0], 'releases FOO: %s' % release_reason(out.merge))
        released = self.started(out, 'released')
        self.assertEqual(len(released), 1, out.lines)
        self.assertTrue(released[0].startswith('released FOO in the backlog store: %s (store at '
                                               % release_reason(out.merge)), released[0])
        self.assertEqual(self.started(out, 'closed'), [], out.lines)
        landed = [i for i, l in enumerate(out.lines) if l.startswith('LANDED ')]
        self.assertEqual(len(landed), 1, out.lines)
        self.assertGreater(out.lines.index(released[0]), landed[0], out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_without_a_part_of_line_the_branchs_pulls_still_close(self):
        self.seed(pulled('FOO'), priced('BAZ'))
        out = self.land(self.branch_with('all of it\n\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['BAZ'])
        self.assertEqual(self.subjects()[0], 'closes FOO: landed %s' % out.merge)
        self.assertEqual(self.started(out, 'released'), [], out.lines)

    def test_closes_and_part_of_split_the_branchs_records(self):
        self.seed(pulled('FOO'), pulled('BAR'))
        out = self.land(self.branch_with('fix\n\nCloses: FOO\nPart-of: BAR\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['BAR'])
        self.assert_open('BAR')
        self.assertEqual([l.split()[1] for l in self.started(out, 'closed')], ['FOO'], out.lines)
        self.assertEqual([l.split()[1] for l in self.started(out, 'released')], ['BAR'],
                         out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_any_part_of_line_releases_every_record_no_closes_line_names(self):
        self.seed(pulled('FOO'), pulled('BAR'))
        out = self.land(self.branch_with('fix\n\nPart-of: FOO\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['BAR', 'FOO'])
        self.assert_open('FOO')
        self.assert_open('BAR')
        self.assertEqual(self.started(out, 'closed'), [], out.lines)
        self.assertEqual(sorted(l.split()[1] for l in self.started(out, 'released')),
                         ['BAR', 'FOO'], out.lines)

    def test_a_mistyped_part_of_id_releases_rather_than_closes(self):
        self.seed(pulled('FOO'))
        out = self.land(self.branch_with('fix\n\nPart-of: FOOO\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['FOO'])
        self.assert_open('FOO')
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out.lines)
        self.assertIn('Part-of line', notes[0])
        self.assertIn('FOOO', notes[0])
        self.assertIn('it released FOO', notes[0])     # what the landing did, by name
        self.assertEqual(self.warnings(out), [])

    def test_a_part_of_value_with_no_plain_id_still_releases(self):
        self.seed(pulled('FOO'))
        tip = self.branch_with('fix\n\nPart-of: #12\n%s\n' % TRAILERS)
        base = self.main
        out = self.land(tip)
        self.assertEqual(self.stems(), ['FOO'])
        self.assert_open('FOO')
        self.assertEqual(self.started(out, 'closed'), [], out.lines)
        self.assertEqual(self.notes(out), [])
        self.assertEqual(at_landing.part_trailers(self.repo.root, tip, base), ((True, []), None))

    def test_an_id_in_closes_and_part_of_is_closed_with_a_note(self):
        self.seed(pulled('FOO'))
        out = self.land(self.branch_with('fix\n\nPart-of: FOO\nCloses: FOO\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), [])
        self.assertEqual([l.split()[1] for l in self.started(out, 'closed')], ['FOO'], out.lines)
        self.assertEqual(self.started(out, 'released'), [], out.lines)
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out.lines)
        self.assertIn('FOO', notes[0])
        self.assertIn('both a Closes line and a Part-of line', notes[0])
        self.assertIn('was closed, not released', notes[0])

    def test_an_id_in_closes_and_part_of_that_no_store_holds_is_not_said_closed(self):
        self.seed(pulled('FOO'))
        out = self.land(self.branch_with('fix\n\nPart-of: NOPE\nCloses: NOPE\n%s\n' % TRAILERS))
        self.assert_open('FOO')
        both = [n for n in self.notes(out) if 'both a Closes line and a Part-of line' in n]
        self.assertEqual(len(both), 1, out.lines)
        self.assertIn('NOPE', both[0])
        self.assertIn('was not closed', both[0])
        self.assertNotIn('closed, not released', both[0])

    def test_an_id_in_closes_and_part_of_in_a_store_not_read_is_not_said_closed(self):
        self.seed(pulled('FOO'))
        tip = self.branch_with('fix\n\nPart-of: FOO\nCloses: FOO\n%s\n' % TRAILERS)
        cfg = kconfig.load_at(self.repo.root, self.main)
        with mock.patch.object(smod, 'require_readable',
                               side_effect=at_landing.Refusal('a record cannot be read')):
            lines = at_landing.close_landed(self.repo.root, cfg, BRANCH, 'f' * 40, tip=tip,
                                            base=self.main)
        self.assertEqual(self.get('FOO').get('state'), 'pulled')
        both = [l for l in lines if 'both a Closes line and a Part-of line' in l]
        self.assertEqual(len(both), 1, lines)
        self.assertIn('was not closed', both[0])
        self.assertNotIn('closed, not released', both[0])

    def test_a_stacked_batch_reads_part_of_only_from_the_branch_that_carries_it(self):
        # kit/other starts at a1, kit/feature's `Part-of: ONE` commit, and lands after it: its
        # messages are read down to kit/feature's merge, so its own pull still closes.
        self.seed(pulled('ONE', scope=['a.py', 'a2.py']),
                  pulled('TWO', by='kit/other', scope=['b.py']))
        wt = self.repo.worktree('feature')
        self.repo.write('a.py', 'x = 1\n', root=wt)
        a1 = self.repo.commit('a1\n\nPart-of: ONE\n%s\n' % TRAILERS, 'a.py', cwd=wt)
        self.repo.write('a2.py', 'x = 2\n', root=wt)
        tip_a = self.repo.commit('a2\n\n%s\n' % TRAILERS, 'a2.py', cwd=wt)
        other = os.path.join(self.repo.root, '.claude', 'worktrees', 'other')
        self.repo.git('worktree', 'add', '-q', other, '-b', 'kit/other', a1)
        self.repo.write('b.py', 'y = 1\n', root=other)
        tip_b = self.repo.commit('b\n\n%s\n' % TRAILERS, 'b.py', cwd=other)
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b]).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), ['ONE'])
        self.assert_open('ONE')
        self.assertEqual([l.split()[1] for l in self.started(out, 'closed')], ['TWO'], out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_part_of_naming_a_record_another_branch_holds_leaves_it(self):
        self.seed(pulled('FOO'), pulled('QUX', by='kit/other'))
        out = self.land(self.branch_with('fix\n\nPart-of: QUX\n%s\n' % TRAILERS))
        self.assertEqual(self.get('QUX').get('pulled_by'), 'kit/other')
        self.assertEqual(self.get('QUX').get('state'), 'pulled')
        self.assert_open('FOO')
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out.lines)
        self.assertIn('QUX', notes[0])
        self.assertEqual(self.warnings(out), [])

    def test_a_record_re_pulled_by_another_branch_mid_release_is_left_as_it_is(self):
        self.seed(pulled('FOO'))
        tip = self.branch_with('fix\n\nPart-of: FOO\n%s\n' % TRAILERS)
        test, real_init = self, smod.Store.__init__

        def init(store, *args, **kw):
            real_init(store, *args, **kw)

            def repull(attempt):
                if attempt == 0:
                    test.seed(pulled('FOO', by='kit/other'), message='re-pull\n')
            store.before_publish = repull

        with mock.patch.object(smod.Store, '__init__', init):
            out = self.land(tip)
        self.assertEqual(self.warnings(out), [])
        self.assertIn('FOO is no longer pulled by %s; left as it is' % BRANCH, out.lines)
        self.assertEqual(self.subjects()[0], 're-pull')
        self.assertEqual(self.get('FOO').get('pulled_by'), 'kit/other')

    def test_a_part_of_line_already_on_main_is_not_read(self):
        self.seed(pulled('FOO'))
        self.repo.write('b.py', 'y = 1\n')
        self.main = self.repo.commit('earlier\n\nPart-of: FOO\n%s\n' % TRAILERS, 'b.py')
        self.repo.git('push', '-q', 'origin', 'main')
        out = self.land(self.branch_with('fix\n\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), [])
        self.assertEqual(self.started(out, 'released'), [], out.lines)

    def test_a_batch_reads_part_of_per_branch(self):
        self.seed(pulled('ONE'), pulled('TWO', by='kit/other', scope=['b.py']))
        tip_a = self.branch_with('a\n\nPart-of: ONE\n%s\n' % TRAILERS)
        tip_b = self.branch_with('b\n\n%s\n' % TRAILERS, name='other', path='b.py')
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b]).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), ['ONE'])
        self.assert_open('ONE')
        self.assertEqual([l.split()[1] for l in self.started(out, 'closed')], ['TWO'], out.lines)
        self.assertEqual([l.split()[1] for l in self.started(out, 'released')], ['ONE'],
                         out.lines)
        self.assertEqual(self.warnings(out), [])

    def test_unreadable_trailers_close_the_branchs_pulls_and_say_part_of_was_not_read(self):
        # The pinned behaviour (tests/gate/test_land_closes_trailer.py): the branch's own pulls
        # still close; the WARNING says its Part-of lines were not read either.
        self.seed(pulled('FOO'))
        tip = self.branch_with('fix\n\nPart-of: FOO\n%s\n' % TRAILERS)
        with mock.patch.object(at_landing, 'closes_trailers', return_value=(None, 'boom')):
            out = self.land(tip)
        self.assertEqual(self.stems(), [])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('Closes trailers', warn[0])
        self.assertIn('Part-of lines', warn[0])
        self.assertIn('boom', warn[0])

    def test_a_close_landed_call_releases_with_the_tip_and_base(self):
        self.seed(pulled('FOO'))
        tip = self.branch_with('fix\n\nPart-of: FOO\n%s\n' % TRAILERS)
        cfg = kconfig.load_at(self.repo.root, self.main)
        lines = at_landing.close_landed(self.repo.root, cfg, BRANCH, 'f' * 40, tip=tip,
                                        base=self.main)
        self.assertTrue(lines[0].startswith('released FOO in the backlog store: '), lines)
        self.assertFalse([l for l in lines if l.startswith(('closed ', 'WARNING', 'NOTE'))],
                         lines)
        self.assert_open('FOO')
        # Without a tip and base no line is read: the branch's pulls close as before.
        self.seed(pulled('BAR'))
        lines = at_landing.close_landed(self.repo.root, cfg, BRANCH, 'f' * 40)
        self.assertTrue(lines[0].startswith('closed BAR in the backlog store: '), lines)


class PartLandingInThePmDataStore(StoreLandCase):
    """The PM data branch's folder store (test_land_store.PmDataClose's set-up): a release there
    rewrites the record file and commits it on the PM data branch."""
    setUp = tls.PmDataClose.setUp
    pm_worktree = tls.PmDataClose.pm_worktree
    record = tls.PmDataClose.record
    pm_root = tls.PmDataClose.pm_root
    branch_with = PartLanding.branch_with

    def land_part(self, rid='FOO'):
        tip = self.branch_with('fix\n\nPart-of: %s\n%s\n' % (rid, TRAILERS))
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def test_a_release_in_the_pm_data_store_is_committed(self):
        wt = self.pm_worktree(pulled('FOO'), priced('BAZ'))
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        before = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.land_part()
        with open(self.record(wt, 'FOO'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('\nstate: open\n', '\n' + text)
        self.assertNotIn('pulled_by:', text)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        self.assertEqual(self.repo.git('rev-parse', tip + '^', cwd=wt), before)
        self.assertEqual(self.repo.git('diff-tree', '--no-commit-id', '--no-renames',
                                       '--name-status', '-r', tip, cwd=wt),
                         'M\tkit/backlog/FOO.md')
        message = self.repo.git('log', '-1', '--format=%B', tip, cwd=wt)
        self.assertTrue(message.startswith('releases FOO: %s' % release_reason(out.merge)),
                        message)
        self.assertIn('\nSession: ', message)
        self.assertEqual(self.repo.git('status', '--porcelain', '--', 'kit/backlog', cwd=wt), '')
        released = [l for l in out.lines if l.startswith('released ')]
        self.assertEqual(len(released), 1, out.lines)
        self.assertIn('released FOO in the backlog store kit/backlog in ', released[0])
        self.assertIn('(committed %s)' % tip[:10], released[0])
        self.assertFalse([l for l in out.lines if l.startswith(('closed ', 'WARNING'))],
                         out.lines)

    def test_a_refused_release_names_the_release_command(self):
        wt = self.pm_worktree(pulled('FOO'))
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        with open(self.record(wt, 'FOO'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nthe PM wrote this by hand\n')
        before = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.land_part()
        self.assertEqual(self.repo.git('rev-parse', 'HEAD', cwd=wt), before)
        with open(self.record(wt, 'FOO'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('the PM wrote this by hand', text)
        self.assertIn('pulled_by: %s' % BRANCH, text)
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('FOO, which it holds pulled, was not released', warn[0])
        self.assertTrue(warn[0].endswith('backlog.py" -C "%s" --as %s release FOO --reason "%s"'
                                         % (self.pm_root(wt), BRANCH,
                                            release_reason(out.merge))), warn[0])
        self.assertNotIn(' close FOO', warn[0])
        self.assertFalse([l for l in out.lines if l.startswith(('closed ', 'released '))],
                         out.lines)

    def test_a_stray_part_of_note_does_not_say_a_refused_release_was_made(self):
        wt = self.pm_worktree(pulled('FOO'))
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        with open(self.record(wt, 'FOO'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nthe PM wrote this by hand\n')
        out = self.land_part('FOOO')
        self.assertEqual(len([l for l in out.lines if l.startswith('WARNING')]), 1, out.lines)
        notes = [l for l in out.lines if l.startswith('NOTE') and 'FOOO' in l]
        self.assertEqual(len(notes), 1, out.lines)
        self.assertNotIn('was released', notes[0])
        self.assertIn('FOO, which it held pulled, was not released', notes[0])


class PartOfValues(unittest.TestCase):

    def test_part_of_lines_are_read_like_closes_lines(self):
        message = ('subject Part-of: NO1\n\nPart-of: EX-1\n\tand more\nprose Part-of: NO2\n'
                   'part-of: NO3\n  Part-of: NO4\nPART-OF: NO5\nPart-of:EX-2\nCloses: EX-3\n\n'
                   'Co-Authored-By: someone\n')
        self.assertEqual(at_landing.trailer_values(message, at_landing.PART_KEY),
                         ['EX-1 and more', 'EX-2'])
        self.assertEqual(at_landing.trailer_values(message, at_landing.CLOSES_KEY), ['EX-3'])
        self.assertEqual(at_landing.closes_values(message), ['EX-3'])


if __name__ == '__main__':
    unittest.main()
