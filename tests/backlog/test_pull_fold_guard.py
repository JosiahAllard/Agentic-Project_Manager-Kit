"""The fold-in guard on `pull` (docs/backlog.md, "The fold-in guard"): a pull whose scope overlaps
a path another live pull's branch changed since its merge base with the code main belongs on that
branch, so it is refused unless `--reason` and `--ruling R-nnnn` override it, the same route as a
collision with a live pull's scope. The caller's own branch never counts. End to end through
Store.write on a ref store with a bare remote."""
import unittest
from unittest import mock

from kitlib.backlog import plan as pmod
from kitlib.backlog.errors import Failure, Refusal, Usage
from kitlib.git import cmd, refs

from tests.backlog.helpers import BRANCH, TODAY, StoreCase, priced
from tests.backlog.test_add_found_on import _Wrap

HOLDER = 'kit/holder'
FOLD = 'fold it into kit/holder, or pull it after kit/holder lands'


def held(rid, by, scope, session=None):
    kw = {'session': session} if session else {}
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=by, scope=scope, **kw)


class _Branches(StoreCase):

    def branch_with(self, branch, *paths, orphan=False, existing=False):
        """`branch` with one commit that adds `paths`, cut from main (or an orphan, or on the
        `existing` branch); main stays checked out."""
        if existing:
            self.repo.git('checkout', '-q', branch)
        else:
            self.repo.git('checkout', '-q', '--orphan' if orphan else '-b', branch)
        for p in paths:
            self.repo.write(p, 'x\n')
        self.repo.git('add', '-A')      # not by name: `[a]` in a pathspec is a glob
        self.repo.git('commit', '-q', '-m', 'work on %s' % branch)
        self.repo.git('checkout', '-q', '-f', 'main')

    def failing(self, module, **over):
        patch = mock.patch.object(pmod, module, _Wrap(getattr(pmod, module), **over))
        patch.start()
        self.addCleanup(patch.stop)


class PullFold(_Branches):

    def setUp(self):
        super().setUp()
        # The holder's declared scope is docs/held.md, but its branch changed kitlib/x/a.py: the
        # scope-collision rule cannot see that work, the fold guard does.
        self.branch_with(HOLDER, 'kitlib/x/a.py')
        self.seed(held('HOLD', HOLDER, 'docs/held.md'))

    def test_a_scope_another_live_branch_changed_is_refused(self):
        self.seed(priced('FOO', scope='kitlib/x/**'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        msg = str(cm.exception)
        self.assertIn('cannot pull FOO: its scope overlaps the work of kit/holder', msg)
        self.assertIn('kitlib/x/** vs kitlib/x/a.py (changed on kit/holder)', msg)
        self.assertIn(FOLD, msg)
        self.assertIn('--ruling R-nnnn', msg)
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_reason_alone_never_overrides_it(self):
        self.seed(priced('FOO', scope='kitlib/x/**'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO', reason='the owner wants it today')
        self.assertIn(FOLD, str(cm.exception))
        self.assertIn('--ruling R-nnnn', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_ruling_alone_is_refused_asking_for_a_reason(self):
        self.seed(priced('FOO', scope='kitlib/x/**'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO', ruling='R-9001')
        self.assertIn('--reason', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_reason_and_a_ruling_write_it_and_cite_the_ruling(self):
        self.seed(priced('FOO', scope='kitlib/x/**'))
        change = self.verb('pull', 'FOO', reason='the owner wants it today', ruling='R-9001')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.get('preempt')),
                         ('pulled', BRANCH, 'the owner wants it today (PM R-9001)'))
        self.assertEqual(change.ruling, 'R-9001')
        self.assertNotIn('FOO', ' '.join(r.all('displaced')))

    def test_a_bad_ruling_shape_is_usage(self):
        self.seed(priced('FOO', scope='kitlib/x/**'))
        with self.assertRaises(Usage):
            self.verb('pull', 'FOO', reason='the owner wants it today', ruling='9001')
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_an_out_of_order_pull_that_folds_names_the_ruling(self):
        self.seed(priced('TOP', priority='1', scope='docs/top.md'),
                  priced('FOO', priority='5', scope='kitlib/x/a.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('--ruling R-nnnn', str(cm.exception))

    def test_a_disjoint_scope_pulls_plainly(self):
        self.seed(priced('FOO', scope='kitlib/other/**'))
        change = self.verb('pull', 'FOO')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')
        self.assertIsNone(change.ruling)

    def test_the_callers_own_branch_diff_never_counts(self):
        self.branch_with(BRANCH, 'kitlib/own/b.py', existing=True)
        self.seed(held('MINE', BRANCH, 'docs/mine.md'), priced('FOO', scope='kitlib/own/**'))
        change = self.verb('pull', 'FOO', reason='bundled with MINE')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')
        self.assertIsNone(change.ruling)

    def test_a_branch_that_holds_no_pull_does_not_count(self):
        self.branch_with('kit/idle', 'kitlib/z/a.py')
        self.seed(priced('FOO', scope='kitlib/z/**'))
        self.verb('pull', 'FOO')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def test_a_dead_pull_does_not_count(self):
        self.seed(held('GONE', 'kit/gone', 'docs/gone.md'), priced('FOO', scope='kitlib/q/**'))
        self.verb('pull', 'FOO')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def far_on_origin_only(self):
        self.branch_with('kit/far', 'kitlib/r/a.py')
        self.repo.git('push', '-q', 'origin', 'kit/far')
        self.repo.git('branch', '-q', '-D', 'kit/far')
        self.seed(held('FAR', 'kit/far', 'docs/far.md'))

    def test_a_live_branch_only_a_remote_has_is_compared_as_last_fetched(self):
        self.far_on_origin_only()
        self.seed(priced('FOO', scope='kitlib/r/**'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('kitlib/r/** vs kitlib/r/a.py (changed on origin/kit/far)', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_remote_only_branch_that_does_not_overlap_is_noted_and_pulls(self):
        self.far_on_origin_only()
        self.seed(priced('FOO', scope='kitlib/other/**'))
        change = self.verb('pull', 'FOO')
        self.assertIn('NOTE: branch kit/far is not a local branch; its diff was compared as last '
                      'fetched from origin', change.lines)
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def test_a_diff_path_that_cannot_be_compared_is_refused(self):
        self.branch_with('kit/odd', 'kitlib/y/[a].py')
        self.seed(held('ODD', 'kit/odd', 'docs/odd.md'), priced('FOO', scope='docs/y.md'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('kitlib/y/[a].py', str(cm.exception))
        self.assertIn('cannot be compared', str(cm.exception))

    def test_several_records_on_one_branch_diff_it_once(self):
        self.seed(held('HOLD2', HOLDER, 'docs/held2.md'), priced('FOO', scope='kitlib/x/**'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertEqual(str(cm.exception).count('(changed on kit/holder)'), 1)

    # --- the code main and git failures ------------------------------------------------------

    def test_a_code_main_that_does_not_resolve_is_noted_and_skips_the_diffs(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=self.ctx.branch, today=TODAY,
                                code_main='trunk')
        self.seed(priced('FOO', scope='kitlib/x/**'))
        change = self.verb('pull', 'FOO')
        self.assertIn('NOTE: code main trunk does not resolve here; branch diffs not compared',
                      change.lines)
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def test_a_live_branch_with_no_merge_base_is_noted_and_skipped(self):
        # No history shared with the code main: the code main is not this branch's history (a
        # store whose code_main is unset), the same case as a code main that does not resolve.
        self.branch_with('kit/orphan', 'docs/y.md', orphan=True)
        self.seed(held('ORPH', 'kit/orphan', 'docs/orph.md'), priced('FOO', scope='docs/y.md'))
        change = self.verb('pull', 'FOO')
        base = refs.default_base(self.repo.root, 'origin', 'main')[0]
        self.assertIn('NOTE: code main %s shares no history with kit/orphan; set '
                      'backlog.code_main; branch diffs not compared' % base, change.lines)
        self.assertEqual(self.get('FOO').get('state'), 'pulled')
        self.assertIsNone(change.ruling)

    def assert_git_failure(self):
        self.seed(priced('FOO', scope='docs/y.md'))
        with self.assertRaises(Failure) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('git broke', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_code_main_git_cannot_read_fails(self):
        real = refs.resolve
        base = refs.default_base(self.repo.root, 'origin', 'main')[0]
        self.failing('refs', resolve=lambda ref, root, **kw: (None, 'git broke')
                     if ref == base else real(ref, root, **kw))
        self.assert_git_failure()

    def test_a_live_branch_git_cannot_read_fails(self):
        real = refs.resolve
        self.failing('refs', resolve=lambda ref, root, **kw: (None, 'git broke')
                     if ref == 'refs/heads/' + HOLDER else real(ref, root, **kw))
        self.assert_git_failure()

    def test_a_merge_base_git_cannot_find_fails(self):
        self.failing('refs', merge_base=lambda a, b, root: (None, 'git broke'))
        self.assert_git_failure()

    def test_a_failed_diff_fails(self):
        real = cmd.run
        self.failing('cmd', run=lambda args, root, **kw: (None, 'git broke')
                     if args[:2] == ['diff', '--no-renames'] else real(args, root, **kw))
        self.assert_git_failure()


class SetScopeFold(_Branches):
    """`set --scope` that widens a pulled record runs the fold-in
    guard as `pull` does, on the members it adds, so `set` is not a door around it."""

    def setUp(self):
        super().setUp()
        self.branch_with(HOLDER, 'kitlib/x/a.py')
        self.branch_with(BRANCH, 'docs/mine.md', existing=True)
        self.seed(held('HOLD', HOLDER, 'docs/held.md'), held('MINE', BRANCH, 'docs/mine.md'))

    def test_widening_into_another_branchs_work_is_refused(self):
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'MINE', [('scope', ['docs/mine.md', 'kitlib/x/**'])])
        msg = str(cm.exception)
        self.assertIn('kitlib/x/** vs kitlib/x/a.py (changed on kit/holder)', msg)
        self.assertIn('--ruling R-nnnn', msg)
        self.assertEqual(self.get('MINE').all('scope'), ['docs/mine.md'])

    def test_widening_into_another_branchs_work_with_a_ruling_is_written(self):
        change = self.verb('set', 'MINE', [('scope', ['docs/mine.md', 'kitlib/x/**'])],
                           ruling='R-9001')
        self.assertEqual(self.get('MINE').all('scope'), ['docs/mine.md', 'kitlib/x/**'])
        self.assertEqual(change.ruling, 'R-9001')
        self.assertIn('  folds into the work of kit/holder on R-9001: kitlib/x/** vs kitlib/x/a.py '
                      '(changed on kit/holder)', change.lines)

    def test_widening_into_free_space_is_free(self):
        change = self.verb('set', 'MINE', [('scope', ['docs/mine.md', 'kitlib/free/**'])])
        self.assertEqual(self.get('MINE').all('scope'), ['docs/mine.md', 'kitlib/free/**'])
        self.assertIsNone(change.ruling)

    def test_the_records_own_branch_diff_never_counts(self):
        self.branch_with(BRANCH, 'kitlib/own/b.py', existing=True)
        self.verb('set', 'MINE', [('scope', ['docs/mine.md', 'kitlib/own/**'])])
        self.assertEqual(self.get('MINE').all('scope'), ['docs/mine.md', 'kitlib/own/**'])

    def test_an_open_record_is_not_checked(self):
        self.seed(priced('FOO', scope='docs/foo.md'))
        self.verb('set', 'FOO', [('scope', ['kitlib/x/**'])])
        self.assertEqual(self.get('FOO').all('scope'), ['kitlib/x/**'])


class TakeoverFold(_Branches):
    """The dead-pull takeover is the same door: one that folds into a live branch's work needs
    `--ruling`, and takes no `--reason`, as a colliding takeover does."""

    def setUp(self):
        super().setUp()
        self.store.session = 'kit'
        self.branch_with(HOLDER, 'kitlib/x/a.py')
        self.seed(held('HOLD', HOLDER, 'docs/held.md', session='other'),
                  priced('DEAD', state='pulled', pulled_at='2026-10-01', pulled_by='kit/gone',
                         scope='kitlib/x/**', session='kit'))

    def test_a_folding_takeover_is_refused_without_a_ruling(self):
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        msg = str(cm.exception)
        self.assertIn('kitlib/x/** vs kitlib/x/a.py (changed on kit/holder)', msg)
        self.assertIn('--ruling R-nnnn', msg)
        self.assertEqual(self.get('DEAD').get('pulled_by'), 'kit/gone')

    def test_a_folding_takeover_with_a_ruling_is_written(self):
        change = self.verb('pull', 'DEAD', ruling='R-9001')
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)
        self.assertEqual(change.ruling, 'R-9001')

    def test_a_folding_takeover_takes_no_reason(self):
        with self.assertRaises(Usage):
            self.verb('pull', 'DEAD', reason='the owner wants it', ruling='R-9001')


if __name__ == '__main__':
    unittest.main()
