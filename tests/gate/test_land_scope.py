"""kitlib.gate.land's scope check (docs/gate.md "Landing", step 4): every path a branch changes
must fall in the scope of a record it holds - pulled by it, or named by a `Closes:` trailer of its
own commits - besides tests/**, docs/INDEX.md and a folder store's own folder. With
land.SCOPE_REFUSES a miss is REFUSED before any merge; without it the landing goes ahead with a
WARNING after the LANDED line. Scratch repository and bare remote in temp only."""
import os
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.backlog import record as rmod
from kitlib.gate import land
from tests.gate import test_land
from tests.gate import test_land_store as tls
from tests.gate.test_land_store import BRANCH, StoreLandCase, pulled

TRAILERS = 'Co-Authored-By: Test Model <noreply@anthropic.com>'
OUTSIDE = 'outside the scopes of the records it holds'
NO_RECORD = 'holds no pulled record; its paths were not checked against a scope'


class ScopeCase(StoreLandCase):
    REFUSES = True

    def setUp(self):
        StoreLandCase.setUp(self)
        patch = mock.patch.object(land, 'SCOPE_REFUSES', self.REFUSES)
        patch.start()
        self.addCleanup(patch.stop)

    def land(self, tip, branch=BRANCH, expect_main=None):
        return self.lander(branch, expect_main or self.main, tip).run()

    def landed(self, out):
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def refused(self, out):
        self.assert_untouched(out, G.REFUSED)
        self.assertEqual(len(out.lines), 1, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: '), out.lines)
        self.assertTrue(out.lines[0].endswith('; nothing was merged'), out.lines)
        return out.lines[0]

    def scope_lines(self, out):
        return [l for l in out.lines if OUTSIDE in l or NO_RECORD in l or 'scope check' in l]

    def commit_on(self, wt, message, files):
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        return self.repo.commit(message, *files, cwd=wt)

    def on_main(self, files, message='main work'):
        for rel, text in files.items():
            self.repo.write(rel, text)
        self.main = self.repo.commit(message, *files)
        self.repo.git('push', '-q', 'origin', 'main')
        return self.main


class Refuses(ScopeCase):
    def test_a_path_outside_the_scopes_is_refused_before_any_merge(self):
        before = self.seed(pulled('FOO', scope=['area/foo/**']), pulled('KEEP', by='kit/other'))
        _, tip = self.branch('feature', {'area/foo/x.py': 'x\n', 'b.py': 'b\n'})
        line = self.refused(self.land(tip))
        self.assertIn('kit/feature changes 1 path(s) %s (FOO): b.py; ' % OUTSIDE, line)
        self.assertIn('set checks the new members against every live pull\'s scope', line)
        self.assertIn('backlog.py" -C "<a worktree of this repository>" set FOO '
                      '--scope "area/foo/**" --scope "b.py"; or land them on their own branch',
                      line)
        self.assertEqual(self.remote_store(), before)        # nothing closed
        self.assertEqual(self.stems(), ['FOO', 'KEEP'])

    def test_paths_in_scope_tests_and_the_doc_index_land(self):
        self.seed(pulled('FOO', scope=['area/foo/**']))
        _, tip = self.branch('feature', {'area/foo/x.py': 'x\n', 'tests/test_x.py': 't\n',
                                         'docs/INDEX.md': 'i\n'})
        out = self.landed(self.land(tip))
        self.assertEqual(self.scope_lines(out), [])
        self.assertEqual(self.stems(), [])                   # FOO closed as before
        self.assertTrue(out.lines[-1].startswith('closed FOO '), out.lines)

    def test_members_compare_whole_components(self):
        self.seed(pulled('FOO', scope=['src/Foo.py']))
        _, tip = self.branch('feature', {'src/Foobar.py': 'x\n'})
        self.assertIn('(FOO): src/Foobar.py;', self.refused(self.land(tip)))

    def test_members_compare_casefolded(self):
        self.seed(pulled('FOO', scope=['SRC/foo/**']))
        _, tip = self.branch('feature', {'src/FOO/x.py': 'x\n'})
        self.assertEqual(self.scope_lines(self.landed(self.land(tip))), [])

    def test_the_scopes_of_every_record_held_are_one_allowance(self):
        self.seed(pulled('FOO', scope=['a/**']), pulled('BAR', scope=['b/**']))
        _, tip = self.branch('feature', {'a/x.py': 'x\n', 'b/y.py': 'y\n'})
        self.assertEqual(self.scope_lines(self.landed(self.land(tip))), [])

    def test_the_paths_outside_name_every_record_held(self):
        self.seed(pulled('FOO', scope=['a/**']), pulled('BAR', scope=['b/**']))
        _, tip = self.branch('feature', {'a/x.py': 'x\n', 'c/z.py': 'z\n'})
        line = self.refused(self.land(tip))
        self.assertIn('changes 1 path(s) %s (BAR, FOO): c/z.py;' % OUTSIDE, line)
        self.assertIn('set BAR --scope "b/**" --scope "c/z.py";', line)

    def test_none_covers_nothing(self):
        self.seed(pulled('FOO', scope=['none']))
        _, tip = self.branch('feature', {'a.py': 'x\n'})
        line = self.refused(self.land(tip))
        self.assertIn('(FOO): a.py;', line)
        self.assertIn('set FOO --scope "a.py";', line)       # `none` is not kept beside a path

    def test_more_paths_than_shown_name_the_diff_that_lists_them(self):
        self.seed(pulled('FOO', scope=['a/**']))
        files = {'z/f%02d.py' % i: 'x\n' for i in range(land.SCOPE_SHOWN + 2)}
        _, tip = self.branch('feature', files)
        line = self.refused(self.land(tip))
        self.assertIn('changes %d path(s)' % (land.SCOPE_SHOWN + 2), line)
        self.assertIn('z/f%02d.py, and 2 more (git diff --name-only --no-renames %s %s);'
                      % (land.SCOPE_SHOWN - 1, self.main, tip), line)
        self.assertNotIn('z/f%02d.py' % land.SCOPE_SHOWN, line)
        self.assertIn('set FOO --scope "a/**" --scope "<each path, or a folder/** that covers '
                      'them>";', line)

    def test_a_record_main_pulled_and_named_by_a_trailer_counts(self):
        self.seed(pulled('D2', by='main', scope=['d2/**']))
        wt = self.repo.worktree('feature')
        tip = self.commit_on(wt, 'fix\n\nCloses: D2\n%s\n' % TRAILERS, {'d2/x.py': 'x\n'})
        out = self.landed(self.land(tip))
        self.assertEqual(self.scope_lines(out), [])
        self.assertEqual(self.stems(), [])

    def closed_early(self):
        """EARLY, pulled from main and closed already in the store; KEEP stays."""
        self.seed(pulled('EARLY', by='main', scope=['e/**']), pulled('KEEP', by='kit/other'))
        backend = self.backend()
        backend.publish(backend.load(rmod.Rules()), {'EARLY': None}, 'closes EARLY: done\n')
        self.assertEqual(self.stems(), ['KEEP'])

    def test_a_record_closed_early_and_named_by_a_trailer_counts(self):
        self.closed_early()
        wt = self.repo.worktree('feature')
        tip = self.commit_on(wt, 'fix\n\nCloses: EARLY\n%s\n' % TRAILERS, {'e/x.py': 'x\n'})
        self.assertEqual(self.scope_lines(self.landed(self.land(tip))), [])

    def test_a_record_closed_early_and_not_named_holds_nothing(self):
        self.closed_early()
        _, tip = self.branch('feature', {'e/x.py': 'x\n'})
        out = self.landed(self.land(tip))
        self.assertEqual(self.scope_lines(out), ['NOTE: kit/feature %s' % NO_RECORD])

    def test_main_merged_into_the_branch_brings_no_path_of_its_own(self):
        self.seed(pulled('FOO', scope=['a/**']))
        wt, _ = self.branch('feature', {'a/x.py': 'x\n'})
        self.on_main({'b.py': 'b\n'})
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt)
        tip = self.repo.head(wt)
        self.assertEqual(self.scope_lines(self.landed(self.land(tip))), [])

    def test_a_rename_out_of_scope_names_both_paths(self):
        self.on_main({'b/x.py': 'x\n', 'a/keep.py': 'k\n'})     # the fake git mv needs a/
        self.seed(pulled('FOO', scope=['a/**']))
        wt = self.repo.worktree('feature')
        self.repo.git('mv', 'b/x.py', 'a/x.py', cwd=wt)
        self.repo.git('commit', '-q', '-m', 'move', cwd=wt)
        tip = self.repo.head(wt)
        self.assertIn('(FOO): b/x.py;', self.refused(self.land(tip)))

    def test_a_path_a_scope_cannot_name_is_outside_and_left_out_of_the_command(self):
        self.seed(pulled('FOO', scope=['a/**']))
        _, tip = self.branch('feature', {'a/x,y.py': 'x\n'})
        line = self.refused(self.land(tip))
        self.assertIn('(FOO): a/x,y.py (undecidable: a comma in a path', line)
        self.assertNotIn('--scope "a/x,y.py"', line)
        self.assertIn(OUTSIDE, line)

    def test_a_branch_holding_no_record_lands_with_a_note_before_landed(self):
        self.seed(pulled('QUX', by='kit/other'))
        _, tip = self.branch('feature', {'a.py': 'x\n'})
        out = self.landed(self.land(tip))
        note = 'NOTE: kit/feature %s' % NO_RECORD
        self.assertEqual(self.scope_lines(out), [note])
        landed = [i for i, l in enumerate(out.lines) if l.startswith('LANDED ')]
        self.assertLess(out.lines.index(note), landed[0], out.lines)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)

    def test_no_store_says_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'x\n'})
        out = self.landed(self.land(tip))
        self.assertEqual(self.scope_lines(out), [])
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)

    def test_an_unreadable_store_refuses(self):
        before = self.seed(pulled('FOO', scope=['a/**']), {'BAD': 'not a header line\n'})
        _, tip = self.branch('feature', {'a/x.py': 'x\n'})
        line = self.refused(self.land(tip))
        self.assertIn('cannot read the backlog store to check kit/feature\'s paths against the '
                      'scopes of the records it holds', line)
        self.assertIn('BAD', line)
        self.assertEqual(self.remote_store(), before)

    def test_a_check_that_stops_refuses(self):
        self.seed(pulled('FOO', scope=['a/**']))
        _, tip = self.branch('feature', {'a/x.py': 'x\n'})
        with mock.patch.object(land.Lander, '_scope_of', side_effect=RuntimeError('boom')):
            line = self.refused(self.land(tip))
        self.assertIn('the scope check of kit/feature stopped (RuntimeError: boom)', line)


class Batches(ScopeCase):
    def test_a_batch_names_the_branch_outside_its_scopes(self):
        self.seed(pulled('FOO', scope=['a/**']), pulled('BAR', by='kit/other', scope=['c/**']))
        _, tip_a = self.branch('feature', {'a/x.py': 'x\n'})
        _, tip_b = self.branch('other', {'b/y.py': 'y\n'})
        line = self.refused(self.land([tip_a, tip_b], branch=[BRANCH, 'kit/other']))
        self.assertIn('kit/other changes 1 path(s) %s (BAR): b/y.py;' % OUTSIDE, line)
        self.assertEqual(self.stems(), ['BAR', 'FOO'])

    def test_a_stacked_branch_is_checked_on_its_own_paths(self):
        # kit/other starts at a1 of kit/feature and lands after it: a/x.py is kit/feature's.
        self.seed(pulled('FOO', scope=['a/**']), pulled('BAR', by='kit/other', scope=['c/**']))
        wt = self.repo.worktree('feature')
        a1 = self.commit_on(wt, 'a1', {'a/x.py': 'x\n'})
        tip_a = self.commit_on(wt, 'a2', {'a/y.py': 'y\n'})
        other = os.path.join(self.repo.root, '.claude', 'worktrees', 'other')
        self.repo.git('worktree', 'add', '-q', other, '-b', 'kit/other', a1)
        tip_b = self.commit_on(other, 'b', {'c/z.py': 'z\n'})
        out = self.landed(self.land([tip_a, tip_b], branch=[BRANCH, 'kit/other']))
        self.assertEqual(self.scope_lines(out), [])
        self.assertEqual(self.stems(), [])


class FolderStore(ScopeCase):
    """A folder store ('main'): its records land with the branch, read at the tip; its folder is
    always allowed."""
    KIT = dict(test_land.LandCase.KIT, backlog={'store': 'main'})

    def test_a_record_the_branch_carries_counts_and_its_folder_is_allowed(self):
        rec = pulled('FOO', scope=['a/**'])['FOO']
        _, tip = self.branch('feature', {'docs/backlog/FOO.md': rec, 'a/x.py': 'x\n'})
        self.assertEqual(self.scope_lines(self.landed(self.land(tip))), [])

    def test_a_path_outside_is_refused(self):
        rec = pulled('FOO', scope=['a/**'])['FOO']
        _, tip = self.branch('feature', {'docs/backlog/FOO.md': rec, 'b.py': 'x\n'})
        line = self.refused(self.land(tip))
        self.assertIn('(FOO): b.py;', line)
        self.assertIn('set FOO --scope "a/**" --scope "b.py";', line)


class PmDataStore(ScopeCase):
    """The PM data branch's folder store (test_land_store.PmDataClose's set-up)."""
    pm_worktree = tls.PmDataClose.pm_worktree
    pm_root = tls.PmDataClose.pm_root

    def setUp(self):
        tls.PmDataClose.setUp(self)
        patch = mock.patch.object(land, 'SCOPE_REFUSES', self.REFUSES)
        patch.start()
        self.addCleanup(patch.stop)

    def seeded(self, *records):
        wt = self.pm_worktree(*records)
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        return wt

    def test_a_record_held_there_counts(self):
        self.seeded(pulled('FOO', scope=['a/**']))
        _, tip = self.branch('feature', {'a/x.py': 'x\n'})
        self.assertEqual(self.scope_lines(self.landed(self.land(tip))), [])

    def test_a_path_outside_names_the_pm_data_worktree_in_the_command(self):
        wt = self.seeded(pulled('FOO', scope=['a/**']))
        _, tip = self.branch('feature', {'b.py': 'x\n'})
        line = self.refused(self.land(tip))
        self.assertIn('(FOO): b.py;', line)
        self.assertIn('backlog.py" -C "%s" set FOO --scope "a/**" --scope "b.py";'
                      % self.pm_root(wt), line)

    def test_no_worktree_on_the_pm_data_branch_refuses(self):
        _, tip = self.branch('feature', {'a/x.py': 'x\n'})
        line = self.refused(self.land(tip))
        self.assertIn('cannot read the backlog store to check kit/feature\'s paths', line)
        self.assertIn('no worktree has the PM data branch pmdata checked out', line)


class Warns(ScopeCase):
    """SCOPE_REFUSES False (the default for now): the landing goes ahead, and the WARNING follows
    the LANDED line."""
    REFUSES = False

    def assert_warned(self, out, text):
        self.landed(out)
        self.assertEqual(self.main_now(), out.merge)
        landed = [i for i, l in enumerate(out.lines) if l.startswith('LANDED ')]
        self.assertEqual(len(landed), 1, out.lines)
        warn = out.lines[landed[0] + 1]
        self.assertTrue(warn.startswith('WARNING: '), out.lines)
        self.assertIn(text, warn)
        self.assertTrue(warn.endswith('; the landing went ahead: the scope check only warns for '
                                      'now (kitlib.gate.land SCOPE_REFUSES)'), warn)
        self.assertEqual(self.scope_lines(out), [warn])
        return warn

    def test_a_path_outside_lands_with_a_warning_after_landed(self):
        self.seed(pulled('FOO', scope=['a/**']))
        _, tip = self.branch('feature', {'b.py': 'x\n'})
        warn = self.assert_warned(self.land(tip), 'kit/feature changes 1 path(s) %s (FOO): b.py;'
                                  % OUTSIDE)
        self.assertIn('set FOO --scope "a/**" --scope "b.py"', warn)
        self.assertEqual(self.stems(), [])                   # FOO closed: the landing stands

    def test_an_unreadable_store_lands_with_a_warning(self):
        self.seed(pulled('FOO', scope=['a/**']), {'BAD': 'not a header line\n'})
        _, tip = self.branch('feature', {'a/x.py': 'x\n'})
        self.assert_warned(self.land(tip), 'cannot read the backlog store to check')

    def test_a_check_that_stops_lands_with_a_warning(self):
        _, tip = self.branch('feature', {'a/x.py': 'x\n'})
        with mock.patch.object(land.Lander, '_scope_of', side_effect=RuntimeError('boom')):
            out = self.land(tip)
        self.assert_warned(out, 'the scope check of kit/feature stopped (RuntimeError: boom)')


class Default(unittest.TestCase):
    def test_the_scope_check_warns_for_now(self):
        self.assertIs(land.SCOPE_REFUSES, False)
        self.assertEqual(land.SCOPE_ALWAYS, ('tests/**', 'docs/INDEX.md'))


if __name__ == '__main__':
    unittest.main()
