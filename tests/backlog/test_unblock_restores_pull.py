"""An unblock that opens a record whose pull a `block` stopped gives the pull back while its
branch still holds the stopped tip, unlanded (kitlib.backlog.plan restore_pull, plan_unblock,
plan_answer; docs/backlog.md "Commands", the block/unblock row). A scope that collides with
another live pull or folds into one's work is never restored: that is a lead's call (the
pull-collision row). A record opened any other way (dependent_rewrites) gets a line that ends the
stopped pull, so no later unblock revives it."""
import os
import unittest
from unittest import mock

from kitlib.backlog import offer as omod
from kitlib.backlog.errors import Failure
from kitlib.backlog import plan as pmod
from kitlib.git import refs
from tests.backlog.helpers import BRANCH, TODAY, StoreCase, priced

OTHER = 'tools/other-branch'
NEWCOMER = 'tools/newcomer'
WHY = 'waits on the owner before it goes on'
ASK = 'Q-101: keep the cap?'
AT = '2026-10-24T04:40:00-07:00'


class RestoreCase(StoreCase):
    remote = False

    def setUp(self):
        super().setUp()
        self.advance(OTHER)
        # The holder's worktree, as the classifier sees it: idle, so anybody may stop its pull.
        self.ctx.liveness = lambda b: (None, self.tip(b))

    def tip(self, branch):
        return self.repo.git('rev-parse', 'refs/heads/%s' % branch)

    def advance(self, branch, start='main', what='work'):
        """Point `branch` at a new commit on top of `start`, one main does not hold."""
        tree = self.repo.git('rev-parse', 'main^{tree}')
        sha = self.repo.git('commit-tree', tree, '-p', start, '-m', '%s on %s' % (what, branch))
        self.repo.git('branch', '-f', branch, sha)
        return sha

    def pulled(self, rid='FOO', holder=OTHER, **over):
        self.seed(priced(rid, state='pulled', pulled_at='2026-10-01', pulled_by=holder, **over))

    def block(self, rid='FOO', on=('owner',)):
        kw = dict(owner_ask=ASK) if 'owner' in on else {}
        return self.verb('block', rid, list(on), reason='%s: %s' % (ASK[:5], WHY), **kw)

    def unblock(self, rid='FOO', **kw):
        kw.setdefault('role_checked', True)
        return self.verb('unblock', rid, **kw)

    def assert_pulled(self, rid, holder):
        r = self.get(rid)
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.get('pulled_at'),
                          r.all('blocked_on')), ('pulled', holder, TODAY, ['-']))
        holding, _dead, _why = omod.in_flight_split(self.store.load(), self.repo.root, 'origin')
        self.assertIn(rid, [h.id for h in holding])

    def assert_open(self, rid, why=None):
        r = self.get(rid)
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.all('blocked_on')),
                         ('open', None, ['-']))
        if why is not None:
            self.assertIn(why, r.body)


class Unblock(RestoreCase):

    def test_a_live_branch_gets_its_pull_back(self):
        self.pulled()
        self.block()
        self.assertIsNone(self.get('FOO').get('pulled_by'))
        change = self.unblock()
        self.assert_pulled('FOO', OTHER)
        self.assertIn('UNBLOCKED FOO - state: pulled, blocked_on: -', change.lines)
        self.assertIn('  pull restored - state: pulled, pulled_by: %s, pulled_at: %s'
                      % (OTHER, TODAY), change.lines)
        self.assertTrue(self.get('FOO').body.rstrip().endswith(
            'Unblocked (%s): pull by %s restored.' % (TODAY, OTHER)))

    def test_the_callers_own_branch_round_trips_to_its_pull(self):
        # block, unblock on the holder's own live branch: the pull is back.
        self.advance(BRANCH)
        self.pulled(holder=BRANCH)
        self.block(on=('live-client',))
        self.unblock()
        self.assert_pulled('FOO', BRANCH)

    def test_an_idle_worktree_is_still_restored(self):
        # The classifier calls the idle tree dead; a holder waiting on the owner is idle by
        # nature, so the restore never asks it.
        self.pulled()
        self.block()
        self.ctx.liveness = lambda b: self.fail('the restore must not ask the idle probe')
        self.unblock()
        self.assert_pulled('FOO', OTHER)

    def test_work_on_top_of_the_stopped_tip_is_still_restored(self):
        self.pulled()
        self.block()
        self.advance(OTHER, start=OTHER)
        self.unblock()
        self.assert_pulled('FOO', OTHER)

    def test_a_dead_branch_opens_the_record_as_before(self):
        self.pulled()
        self.block()
        self.repo.git('branch', '-D', OTHER)
        change = self.unblock()
        self.assert_open('FOO', 'Unblocked (%s): pull by %s not restored: no branch or worktree '
                                'by that name.' % (TODAY, OTHER))
        self.assertIn('UNBLOCKED FOO - state: open, blocked_on: -', change.lines)
        self.assertIn('  pull by %s not restored: no branch or worktree by that name' % OTHER,
                      change.lines)

    def test_a_landed_tip_is_not_restored(self):
        self.pulled()
        self.block()
        tip = self.tip(OTHER)
        self.repo.git('merge', '--no-ff', '-m', 'Merge %s' % OTHER, OTHER)
        change = self.unblock()
        self.assert_open('FOO', 'its stopped tip %s has landed on main' % tip[:10])
        self.assertIn('  pull by %s not restored: its stopped tip %s has landed on main'
                      % (OTHER, tip[:10]), change.lines)

    def test_a_landing_dated_before_the_block_day_is_still_landed(self):
        # The tip decides, not the committer day: a merge dated before the block counts.
        self.pulled()
        self.block()
        with mock.patch.dict(os.environ, {'GIT_COMMITTER_DATE': '2026-09-01T00:00:00+00:00',
                                          'GIT_AUTHOR_DATE': '2026-09-01T00:00:00+00:00'}):
            self.repo.git('merge', '--no-ff', '-m', 'Merge %s' % OTHER, OTHER)
        self.unblock()
        self.assert_open('FOO', 'has landed on main')

    def test_a_branch_reset_past_the_tip_is_not_restored(self):
        self.pulled()
        self.block()
        tip = self.tip(OTHER)
        self.advance(OTHER, what='redo')      # a fresh commit on main: the stopped tip is gone
        self.assertNotEqual(self.tip(OTHER), tip)
        self.unblock()
        self.assert_open('FOO', '%s no longer holds its stopped tip %s' % (OTHER, tip[:10]))

    def test_a_hand_named_branch_that_never_held_the_tip_is_not_restored(self):
        elsewhere = self.advance('tools/elsewhere')
        body = ('Stopped on branch %s at %s, branch kept.\nBlocked on live-client (%s): %s'
                % (OTHER, elsewhere[:10], TODAY, WHY))
        self.seed(priced('FOO', state='blocked', blocked_on='live-client', body=body))
        self.unblock()
        self.assert_open('FOO', '%s no longer holds its stopped tip %s' % (OTHER, elsewhere[:10]))

    def test_a_tip_git_does_not_know_is_not_restored(self):
        body = ('Stopped on branch %s at abcdef1234, branch kept.\nBlocked on live-client (%s): %s'
                % (OTHER, TODAY, WHY))
        self.seed(priced('FOO', state='blocked', blocked_on='live-client', body=body))
        self.unblock()
        self.assert_open('FOO', 'its stopped tip abcdef1234 is gone')

    def test_a_held_record_is_not_restored(self):
        self.pulled()
        self.block()
        self.seed({'FOO': self.get('FOO').text.replace('priority: 2', 'priority: -1')})
        self.unblock()
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('pulled_by')), ('open', None))
        self.assertIn('not restored: the record is held', r.body)

    def test_only_the_last_unblock_restores(self):
        self.pulled()
        self.block(on=('owner', 'live-client'))
        change = self.unblock(on='owner', reason='Q-101 answered: keep the cap')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on'), r.get('pulled_by')),
                         ('blocked', ['live-client'], None))
        self.assertFalse(any('restored' in line for line in change.lines))
        self.unblock(on='live-client', reason='the client is back up now')
        self.assert_pulled('FOO', OTHER)

    def test_a_second_block_of_a_blocked_record_still_restores(self):
        self.pulled()
        self.block()
        self.verb('block', 'FOO', ['live-client'], reason='the client went down too')
        self.unblock(on='owner', reason='Q-101 answered: keep the cap')
        self.unblock(on='live-client', reason='the client is back up now')
        self.assert_pulled('FOO', OTHER)

    def test_unblock_by_the_owner_route_restores(self):
        # The owner's hub route and the PM both reach plan_unblock with role_checked.
        self.pulled()
        self.block()
        self.ctx.branch = 'tools/pm-checkout'
        self.unblock(role_checked=True)
        self.assert_pulled('FOO', OTHER)

    def test_a_scope_collision_with_a_newer_pull_is_not_restored(self):
        self.pulled(scope='area/shared/**')
        self.block()
        self.advance(NEWCOMER)
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=NEWCOMER,
                         scope='area/shared/x.py'))
        change = self.unblock()
        self.assert_open('FOO', 'collides with area/shared/x.py already taken by BAR (in flight '
                                'on %s); a lead may re-pull it (the pull-collision row).'
                         % NEWCOMER)
        self.assertTrue(any('not restored' in line and 'BAR' in line for line in change.lines))

    def test_a_scope_that_folds_into_another_pulls_work_is_not_restored(self):
        self.pulled(scope='area/foo/**')
        self.block()
        path = self.repo.worktree('newcomer', NEWCOMER)
        self.repo.write('area/foo/x.py', 'x = 1\n', root=path)
        self.repo.commit('newcomer work', 'area/foo/x.py', cwd=path)
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=NEWCOMER,
                         scope='area/bar/**'))
        self.unblock()
        self.assert_open('FOO', 'scope folds into %s' % NEWCOMER)
        self.assertIn('a lead may re-pull it', self.get('FOO').body)

    def newcomer_pull(self, start='main'):
        self.advance(NEWCOMER, start=start)
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=NEWCOMER,
                         scope='area/bar/**'))

    def test_a_code_main_that_does_not_resolve_is_not_restored(self):
        # The fold check cannot diff the other pull's branch: "cannot tell" is never "no overlap".
        self.pulled(scope='area/foo/**')
        self.block()
        self.newcomer_pull()
        with mock.patch.object(pmod, '_code_main', return_value=('origin/nosuch', '')):
            self.unblock()
        self.assert_open('FOO', 'cannot tell whether its scope folds into another pull\'s work '
                                '(code main origin/nosuch does not resolve here')
        self.assertIn('a lead may re-pull it', self.get('FOO').body)

    def test_a_branch_with_no_shared_history_is_not_restored(self):
        self.pulled(scope='area/foo/**')
        self.block()
        tree = self.repo.git('rev-parse', 'main^{tree}')
        orphan = self.repo.git('commit-tree', tree, '-m', 'unrelated history')
        self.newcomer_pull(start=orphan)
        self.unblock()
        self.assert_open('FOO', 'shares no history with %s' % NEWCOMER)

    def test_a_later_block_of_the_opened_record_does_not_revive_the_old_pull(self):
        self.pulled()
        self.block()
        tip = self.tip(OTHER)
        self.repo.git('branch', '-D', OTHER)
        self.unblock()
        self.assert_open('FOO')
        self.repo.git('branch', OTHER, tip)   # the name comes back, at the very tip
        self.block(on=('live-client',))
        self.unblock()
        self.assert_open('FOO')

    def test_a_release_ends_the_stopped_pull(self):
        body = ('Stopped on branch %s at %s, branch kept.\n'
                'Blocked on live-client (%s): %s\n\nReleased (%s): given back'
                % (OTHER, self.tip(OTHER)[:10], TODAY, WHY, TODAY))
        self.seed(priced('FOO', state='blocked', blocked_on='live-client', body=body))
        self.unblock()
        self.assert_open('FOO')

    def test_a_blocked_record_with_no_stopped_pull_opens_with_no_body_line(self):
        self.seed(priced('FOO', state='blocked', blocked_on='live-client', body='Some text.'))
        before = self.get('FOO').body
        self.unblock()
        self.assert_open('FOO')
        self.assertEqual(self.get('FOO').body, before)

    def assert_fails_unwritten(self, target, name, value):
        self.pulled()
        self.block()
        before = self.get('FOO').text
        with mock.patch.object(target, name, return_value=value):
            with self.assertRaises(Failure) as cm:
                self.unblock()
        self.assertIn('git broke', str(cm.exception))
        self.assertEqual(self.get('FOO').text, before)

    def test_a_branch_list_git_cannot_give_fails_and_writes_nothing(self):
        self.assert_fails_unwritten(omod, 'live_branches', (None, 'git broke'))

    def test_an_ancestry_git_cannot_tell_fails_and_writes_nothing(self):
        self.assert_fails_unwritten(refs, 'is_ancestor', (None, 'git broke'))

    def test_a_tip_git_cannot_resolve_fails_and_writes_nothing(self):
        self.assert_fails_unwritten(refs, 'resolve', (None, 'git broke'))


class Answer(RestoreCase):
    """The owner's answer filed by the Action Hub clears the owner blocker as unblock does,
    restore included."""

    def answer(self, rid='FOO'):
        return self.verb('answer', rid, 'Q-101', AT, title='Keep the cap?', label='Yes',
                         role_checked=True)

    def test_the_answer_restores_a_live_pull(self):
        self.pulled()
        self.block()
        change = self.answer()
        self.assert_pulled('FOO', OTHER)
        self.assertIn('  owner blocker cleared - state: pulled', change.lines)
        body = self.get('FOO').body
        self.assertIn('## Owner answer (Action Hub, %s)' % AT, body)
        self.assertTrue(body.rstrip().endswith('Unblocked (%s): pull by %s restored.'
                                               % (TODAY, OTHER)))

    def test_the_answer_opens_a_dead_pull(self):
        self.pulled()
        self.block()
        self.repo.git('branch', '-D', OTHER)
        self.answer()
        self.assert_open('FOO')

    def test_an_answer_that_leaves_a_blocker_restores_nothing(self):
        self.pulled()
        self.block(on=('owner', 'live-client'))
        self.answer()
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', ['live-client']))
        self.unblock(reason='the client is back up now')
        self.assert_pulled('FOO', OTHER)

    def test_a_record_opened_by_a_closed_item_keeps_no_stale_pull(self):
        # pull R on B, block on item:X, close X (R opens), block on the owner, answer: R opens,
        # it is not pulled back onto B.
        self.seed(priced('XYZ'))
        self.pulled()
        self.verb('block', 'FOO', ['item:XYZ'], reason='waits for XYZ to land first')
        change = self.verb('close', 'XYZ', 'done and landed', role_checked=True)
        self.assertIn('UNBLOCKED FOO - `item:XYZ` removed -> -; state: open', change.lines)
        self.assert_open('FOO', 'Unblocked (%s): opened by item:XYZ closing; pull by %s not '
                                'restored.' % (TODAY, OTHER))
        self.block()
        self.answer()
        self.assert_open('FOO')

    def end_blocker(self, verb, gone):
        self.seed(priced('XYZ'))
        self.pulled()
        self.verb('block', 'FOO', ['item:XYZ'], reason='waits for XYZ to land first')
        self.verb(verb, 'XYZ', 'not wanted after all', role_checked=True)
        self.assert_open('FOO', 'Unblocked (%s): opened by item:XYZ %s; pull by %s not restored.'
                         % (TODAY, gone, OTHER))
        self.block()
        self.answer()
        self.assert_open('FOO')

    def test_a_record_opened_by_a_retired_item_keeps_no_stale_pull(self):
        self.end_blocker('retire', 'retiring')

    def test_a_record_opened_by_a_deleted_item_keeps_no_stale_pull(self):
        self.end_blocker('delete', 'being deleted')


class BlockedPull(unittest.TestCase):

    def test_the_note_must_be_followed_by_the_blocked_line(self):
        note = 'Stopped on branch kit/x at abcdef1234, branch kept.'
        self.assertEqual(pmod.blocked_pull('%s\nBlocked on owner (2026-10-01): R-1: why' % note),
                         ('kit/x', 'abcdef1234'))
        self.assertIsNone(pmod.blocked_pull('%s\nReleased (2026-10-01): why' % note))
        self.assertIsNone(pmod.blocked_pull(note))
        self.assertIsNone(pmod.blocked_pull('no note'))
        self.assertIsNone(pmod.blocked_pull(
            '%s\nBlocked on owner (2026-10-01): why\n\nUnblocked (2026-10-02): pull by kit/x '
            'restored.' % note))


if __name__ == '__main__':
    unittest.main()
