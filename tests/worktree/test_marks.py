"""kitlib.worktree.marks (`worktree_remove.py <branch> --mark-stale "<why>"`, `--unmark-stale`):
one marks file per repository, read the same from every worktree; every refusal writes nothing;
remark, unmark and an unreadable file. Throwaway repositories only (tests/gitfixture.py); the
liveness probe is a seam except where the real one is the point.
"""
import json
import os
import tempfile
import time
import unittest

from kitlib.worktree import marks, remove, sweep
from tests import gitfixture
from tests.worktree.test_worktree_remove import RemoveCase


def idle(main, branch):
    return None, 'tip'


class MarkCase(RemoveCase):

    def marks_path(self):
        return os.path.join(self.fx.common_dir(), marks.FILE_NAME)

    def mark(self, branch, why='superseded by kit/other', top=None, **kw):
        kw.setdefault('liveness', idle)
        got = marks.mark(top or self.repo, branch, why, **kw)
        return got.code, '\n'.join(got.lines)

    def unmark(self, branch, top=None):
        got = marks.unmark(top or self.repo, branch)
        return got.code, '\n'.join(got.lines)

    def stored(self):
        got, why = marks.load(self.marks_path())
        self.assertIsNone(why)
        return got

    def file_text(self):
        try:
            with open(self.marks_path(), encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return None

    def assert_refused(self, rc, out, words, before=None):
        self.assertEqual(rc, remove.REFUSED, out)
        self.assertIn('refused   ', out)
        self.assertIn(words, out)
        self.assertIn('nothing was marked', out)
        self.assertEqual(self.file_text(), before, out)


class Mark(MarkCase):

    def test_a_mark_made_from_one_worktree_is_read_from_another(self):
        a = self.workstream('a')
        b = self.workstream('b')
        rc, out = self.mark('kit/a', top=a,
                            environ={'PM_KIT_SESSION_NAME': 'coord-k', 'PM_KIT_ROLE': 'coordinator'})
        self.assertEqual(rc, 0, out)
        self.assertIn('marked    kit/a at ', out)
        self.assertIn('the nightly sweep abandons it under archive/a', out)
        self.assertIn(' kit/a --unmark-stale', out)
        ctx, refused = remove.open_ctx(b)
        self.assertIsNone(refused)
        self.assertEqual(os.path.normcase(marks.file_of(ctx)), os.path.normcase(self.marks_path()))
        got, why = marks.load(marks.file_of(ctx))
        self.assertIsNone(why)
        self.assertEqual(len(got), 1, got)
        mark = got[0]
        self.assertEqual(mark['branch'], 'kit/a')
        self.assertEqual(mark['tip'], self.git('rev-parse', 'kit/a'))
        self.assertEqual(os.path.normcase(mark['path']), os.path.normcase(a))
        self.assertEqual(mark['why'], 'superseded by kit/other')
        self.assertEqual(mark['by'], 'coord-k (coordinator)')
        self.assertLess(abs(marks.at_epoch(mark) - time.time()), 60)
        self.assertFalse(os.path.exists(os.path.join(a, marks.FILE_NAME)))
        self.assertEqual(self.git('status', '--porcelain', cwd=a), '')

    def test_a_second_mark_replaces_the_first(self):
        self.workstream('a')
        self.mark('kit/a', why='first')
        rc, out = self.mark('kit/a', why='second')
        self.assertEqual(rc, 0, out)
        self.assertIn('remarked  kit/a', out)
        self.assertEqual([m['why'] for m in self.stored()], ['second'])

    def test_marks_of_two_branches_are_kept_in_branch_order(self):
        self.workstream('b')
        self.workstream('a')
        self.mark('kit/b')
        self.mark('kit/a')
        self.assertEqual([m['branch'] for m in self.stored()], ['kit/a', 'kit/b'])

    def test_the_save_leaves_no_temp_file(self):
        self.workstream('a')
        self.mark('kit/a')
        left = [n for n in os.listdir(self.fx.common_dir()) if n.endswith('.tmp')]
        self.assertEqual(left, [])

    def test_an_empty_reason_is_refused(self):
        self.workstream('a')
        rc, out = self.mark('kit/a', why='   ')
        self.assert_refused(rc, out, '--mark-stale needs a reason')

    def test_no_such_branch_is_refused(self):
        rc, out = self.mark('kit/none')
        self.assert_refused(rc, out, 'there is no local branch kit/none')

    def test_a_branch_named_in_another_case_is_refused(self):
        self.workstream('a')
        rc, out = self.mark('KIT/A')
        self.assert_refused(rc, out, 'git has kit/a, whose name and KIT/A differ only in case')

    def test_the_main_branch_is_refused(self):
        rc, out = self.mark('main')
        self.assert_refused(rc, out, 'main is the main branch')

    def test_a_branch_with_no_worktree_is_pointed_to_branch_abandon(self):
        self.orphan('o')
        rc, out = self.mark('kit/o')
        self.assert_refused(rc, out, 'no worktree is on kit/o')
        self.assertIn('kit/o --branch --abandon "<why>"', out)

    def test_a_worktree_outside_the_worktree_folder_is_refused(self):
        root = os.path.join(os.path.realpath(self.fx.base), 'outside')
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, 'out')
        self.git('worktree', 'add', '-q', '-b', 'kit/out', path)
        self.fx.write('o.txt', 'o\n', root=path)
        self.fx.commit('o', 'o.txt', cwd=path)
        rc, out = self.mark('kit/out')
        self.assert_refused(rc, out, 'is not under a worktree folder')

    def test_a_locked_worktree_is_refused(self):
        path = self.workstream('a')
        self.git('worktree', 'lock', '--reason', 'kept', path)
        self.addCleanup(self.git, 'worktree', 'unlock', path, check=False)
        rc, out = self.mark('kit/a')
        self.assert_refused(rc, out, 'is locked with `git worktree lock`: kept')

    def test_landed_work_is_refused(self):
        self.workstream('a')
        self.land('kit/a')
        rc, out = self.mark('kit/a')
        self.assert_refused(rc, out, 'landed work needs no mark')

    def test_a_dirty_tree_is_refused(self):
        path = self.workstream('a')
        self.fx.write('wip.txt', 'x\n', root=path)
        rc, out = self.mark('kit/a')
        self.assert_refused(rc, out, 'has uncommitted or untracked files (1): ?? wip.txt')

    def test_a_live_tree_is_refused(self):
        self.workstream('a')
        rc, out = self.mark('kit/a', liveness=lambda m, b: ('active 2 min ago', 'tip'))
        self.assert_refused(rc, out, "kit/a is live by --classify's L rule: active 2 min ago")

    def test_the_real_liveness_probe_refuses_a_tree_just_worked_in(self):
        self.workstream('a')
        rc, out = self.mark('kit/a', liveness=None)
        self.assert_refused(rc, out, "kit/a is live by --classify's L rule")

    def test_a_refusal_leaves_an_existing_file_unchanged(self):
        self.workstream('a')
        path = self.workstream('b')
        self.mark('kit/a')
        before = self.file_text()
        self.fx.write('wip.txt', 'x\n', root=path)
        rc, out = self.mark('kit/b')
        self.assert_refused(rc, out, 'uncommitted or untracked', before=before)

    def test_an_unreadable_file_refuses_the_mark_and_stays(self):
        self.workstream('a')
        with open(self.marks_path(), 'w', encoding='utf-8') as f:
            f.write('not json\n')
        rc, out = self.mark('kit/a')
        self.assert_refused(rc, out, 'the stale marks cannot be read (', before='not json\n')

    def test_the_idle_hours_it_prints_are_the_sweeps(self):
        self.assertEqual(marks.SWEEP_IDLE_H * 3600, sweep.SWEEP_IDLE_S)


class MarkExempt(MarkCase):

    CONFIG = {'worktree_remove': {'exempt': {'daily': 'kept by ruling X'}}}

    def test_an_exempt_tree_is_refused(self):
        self.workstream('daily')
        rc, out = self.mark('kit/daily')
        self.assert_refused(rc, out, 'is exempt (kept by ruling X)')


class MarkDisposable(MarkCase):

    CONFIG = {'worktree_remove': {'disposable_untracked': ['*.tmp']}}

    def test_a_tree_holding_a_disposable_file_is_refused(self):
        """The sweep counts a disposable_untracked file as dirt (sweep.sweep_status), so a mark on
        such a tree would promise an abandon the sweep never makes."""
        path = self.workstream('a')
        self.fx.write('scratch.tmp', 'x\n', root=path)
        self.assertIn('uncommitted or untracked files (1): ?? scratch.tmp',
                      sweep.sweep_status(remove.open_ctx(self.repo, None)[0], path))
        rc, out = self.mark('kit/a')
        self.assert_refused(rc, out, 'has untracked files (1): ?? scratch.tmp - they match'
                                     ' worktree_remove.disposable_untracked')
        self.assertTrue(os.path.exists(os.path.join(path, 'scratch.tmp')))

    def test_once_they_are_deleted_it_is_marked(self):
        path = self.workstream('a')
        self.fx.write('scratch.tmp', 'x\n', root=path)
        os.remove(os.path.join(path, 'scratch.tmp'))
        rc, out = self.mark('kit/a')
        self.assertEqual(rc, 0, out)


class MarkTemplate(MarkCase):

    CONFIG = {'worktree_remove': {'branch_template': 'phase/{slug}'}}

    def test_a_branch_off_its_template_is_refused(self):
        self.workstream('t', branch='kit/t')
        rc, out = self.mark('kit/t')
        self.assert_refused(rc, out, 'is on kit/t, not phase/t')

    def test_a_branch_on_its_template_is_marked(self):
        self.workstream('t', branch='phase/t')
        rc, out = self.mark('phase/t')
        self.assertEqual(rc, 0, out)


class MarkOpenClaim(MarkCase):

    CONFIG = {'backlog': {'store': 'main'}}

    def test_a_claim_in_another_case_is_refused(self):
        self.workstream('claim')
        self.fx.write('docs/backlog/T-002.md', 'id: T-002\nstate: pulled\npulled_by: KIT/Claim\n')
        self.fx.commit('claim', 'docs/backlog/T-002.md')
        self.fx.git('push', '-q', 'origin', 'main')
        rc, out = self.mark('kit/claim')
        self.assert_refused(rc, out, 'a backlog record is still pulled by kit/claim (an open'
                                     ' claim): T-002')


class Unmark(MarkCase):

    def test_unmark_drops_only_that_mark(self):
        self.workstream('a')
        self.workstream('b')
        self.mark('kit/a', why='a is stale')
        self.mark('kit/b')
        rc, out = self.unmark('kit/a')
        self.assertEqual(rc, 0, out)
        self.assertIn('cleared   the stale mark of kit/a (marked ', out)
        self.assertIn('a is stale', out)
        self.assertEqual([m['branch'] for m in self.stored()], ['kit/b'])

    def test_unmark_of_no_mark_is_nothing(self):
        rc, out = self.unmark('kit/none')
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   kit/none has no stale mark', out)

    def test_unmark_matches_the_branch_exactly(self):
        self.workstream('a')
        self.mark('kit/a')
        rc, out = self.unmark('KIT/A')
        self.assertIn('nothing   KIT/A has no stale mark', out)
        self.assertEqual(len(self.stored()), 1)

    def test_an_unreadable_file_refuses_the_unmark_and_stays(self):
        with open(self.marks_path(), 'w', encoding='utf-8') as f:
            f.write('{"version": 2, "marks": []}\n')
        rc, out = self.unmark('kit/a')
        self.assertEqual(rc, remove.REFUSED, out)
        self.assertIn('has version 2', out)
        self.assertIn('nothing was cleared', out)
        self.assertEqual(self.file_text(), '{"version": 2, "marks": []}\n')


GOOD = {'branch': 'kit/a', 'tip': 'a' * 40, 'path': 'Z:\\r\\.claude\\worktrees\\a', 'why': 'w',
        'at': '2026-10-01T10:00:00.000000Z', 'by': 'unmarked session'}


class Load(unittest.TestCase):

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='kit-marks-'))
        self.addCleanup(gitfixture.rmtree, self.dir)
        self.path = os.path.join(self.dir, marks.FILE_NAME)

    def load_of(self, data):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return marks.load(self.path)

    def test_no_file_is_no_marks(self):
        self.assertEqual(marks.load(self.path), ([], None))

    def test_a_good_file_round_trips(self):
        marks.save(self.path, [GOOD])
        self.assertEqual(marks.load(self.path), ([GOOD], None))

    def test_every_bad_shape_makes_the_whole_file_unreadable(self):
        bad = [
            ('[]', 'must be an object'),
            ({'version': 1}, 'must be an object'),
            ({'version': 1, 'marks': [], 'x': 1}, 'must be an object'),
            ({'version': 2, 'marks': []}, 'has version 2'),
            ({'version': 1, 'marks': {}}, '"marks" must be a list'),
            ({'version': 1, 'marks': [dict(GOOD, extra='x')]}, 'must hold exactly'),
            ({'version': 1, 'marks': [{k: v for k, v in GOOD.items() if k != 'by'}]},
             'must hold exactly'),
            ({'version': 1, 'marks': [dict(GOOD, why=' ')]}, 'why must be a non-empty string'),
            ({'version': 1, 'marks': [dict(GOOD, tip='A' * 40)]}, 'is not a full lower-case'),
            ({'version': 1, 'marks': [dict(GOOD, tip='abc')]}, 'is not a full lower-case'),
            ({'version': 1, 'marks': [dict(GOOD, at='2026-10-01 10:00')]}, 'at '),
            ({'version': 1, 'marks': [GOOD, dict(GOOD)]}, 'is marked twice'),
            ('{', 'is not valid JSON'),
            ('[' * 100000 + ']' * 100000, 'nested too deeply'),
        ]
        for data, words in bad:
            with self.subTest(data=str(data)[:80]):
                got, why = self.load_of(data)
                self.assertIsNone(got)
                self.assertIn(words, why)

    def test_the_lock_is_held_for_its_block(self):
        with marks.locked(self.path):
            with self.assertRaises(marks.MarksError) as cm:
                with marks.locked(self.path, wait=0.2):
                    pass
        self.assertIn('waited 0.2 s', str(cm.exception))
        with marks.locked(self.path, wait=0.2):
            pass


class Who(unittest.TestCase):

    def test_by_names_the_session_and_role_it_has(self):
        self.assertEqual(marks.by({}), 'unmarked session')
        self.assertEqual(marks.by({'PM_KIT_SESSION_NAME': 'c1'}), 'c1')
        self.assertEqual(marks.by({'PM_KIT_ROLE': 'coordinator'}), 'coordinator')
        self.assertEqual(marks.by({'PM_KIT_SESSION_NAME': 'c1', 'PM_KIT_ROLE': 'coordinator'}),
                         'c1 (coordinator)')

    def test_the_tag_message_carries_when_who_and_why(self):
        self.assertEqual(marks.tag_why(GOOD), 'stale (marked 2026-10-01 10:00 UTC by unmarked'
                                              ' session): w')


if __name__ == '__main__':
    unittest.main()
