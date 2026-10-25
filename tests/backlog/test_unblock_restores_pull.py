"""An unblock that opens a record whose pull a `block` stopped gives the pull back while its
branch is live (kitlib.backlog.plan restore_pull, plan_unblock, plan_answer; docs/backlog.md
"Commands", the block/unblock row). The pull was blocked on the owner, another branch took a file
of its scope meanwhile, and the holder's re-pull was refused as a collision: the restore keeps
the holder's claim instead."""
import unittest

from kitlib.backlog import offer as omod
from kitlib.backlog.errors import Failure
from kitlib.backlog import plan as pmod
from tests.backlog.helpers import BRANCH, TODAY, StoreCase, priced

OTHER = 'tools/other-branch'
WHY = 'waits on the owner before it goes on'
ASK = 'Q-101: keep the cap?'
AT = '2026-10-24T04:40:00-07:00'


class RestoreCase(StoreCase):
    remote = False

    def setUp(self):
        super().setUp()
        self.repo.git('branch', OTHER)
        # The holder's worktree, as the classifier sees it: idle, so anybody may stop its pull.
        self.ctx.liveness = lambda b: (None, self.tip(b))

    def tip(self, branch):
        return self.repo.git('rev-parse', 'refs/heads/%s' % branch)

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

    def assert_open(self, rid):
        r = self.get(rid)
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.all('blocked_on')),
                         ('open', None, ['-']))


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
        # block, unblock on the holder's own live branch: the pull is back, so a pull of it
        # is a pull of a record already in flight.
        self.pulled(holder=BRANCH)
        self.block(on=('live-client',))
        self.unblock()
        self.assert_pulled('FOO', BRANCH)

    def test_an_idle_worktree_is_still_restored(self):
        # The classifier calls the idle tree dead; a holder waiting on the owner is idle by
        # nature, so only the branch's existence counts.
        self.pulled()
        self.block()
        self.ctx.liveness = lambda b: self.fail('the restore must not ask the idle probe')
        self.unblock()
        self.assert_pulled('FOO', OTHER)

    def test_a_dead_branch_opens_the_record_as_before(self):
        self.pulled()
        self.block()
        self.repo.git('branch', '-D', OTHER)
        change = self.unblock()
        self.assert_open('FOO')
        self.assertIn('UNBLOCKED FOO - state: open, blocked_on: -', change.lines)
        self.assertIn('  pull by %s not restored: no branch or worktree by that name' % OTHER,
                      change.lines)
        self.assertIn('Unblocked (%s): pull by %s not restored' % (TODAY, OTHER),
                      self.get('FOO').body)

    def test_a_landed_branch_is_not_restored(self):
        self.repo.git('branch', '-D', OTHER)
        path = self.repo.worktree('other', OTHER)
        self.repo.write('w.txt', 'work\n', root=path)
        self.repo.commit('work', 'w.txt', cwd=path)
        self.pulled()
        self.block()
        self.repo.git('merge', '--no-ff', '-m', 'Merge %s' % OTHER, OTHER)
        change = self.unblock()
        self.assert_open('FOO')
        self.assertTrue(any(line.startswith('  pull by %s not restored: it has landed on main'
                                            % OTHER) for line in change.lines), change.lines)

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

    def test_a_collision_with_a_newer_pull_restores_with_a_warning(self):
        self.pulled(scope='area/shared/**')
        self.block()
        self.repo.git('branch', 'tools/newcomer')
        self.seed(priced('BAR', state='pulled', pulled_at=TODAY, pulled_by='tools/newcomer',
                         scope='area/shared/x.py'))
        change = self.unblock()
        self.assert_pulled('FOO', OTHER)
        self.assertIn('warning: scope collides with `BAR`, in flight on tools/newcomer',
                      change.lines)

    def test_a_later_block_of_the_opened_record_does_not_revive_the_old_pull(self):
        self.pulled()
        self.block()
        self.repo.git('branch', '-D', OTHER)
        self.unblock()
        self.assert_open('FOO')
        self.repo.git('branch', OTHER)        # the name comes back
        self.block(on=('live-client',))
        self.unblock()
        self.assert_open('FOO')

    def test_a_release_ends_the_stopped_pull(self):
        body = ('Stopped on branch %s at abcdef1234, branch kept.\n'
                'Blocked on live-client (%s): %s\n\nReleased (%s): given back'
                % (OTHER, TODAY, WHY, TODAY))
        self.seed(priced('FOO', state='blocked', blocked_on='live-client', body=body))
        self.unblock()
        self.assert_open('FOO')

    def test_a_blocked_record_with_no_stopped_pull_opens_with_no_body_line(self):
        self.seed(priced('FOO', state='blocked', blocked_on='live-client', body='Some text.'))
        before = self.get('FOO').body
        self.unblock()
        self.assert_open('FOO')
        self.assertEqual(self.get('FOO').body, before)

    def test_a_git_that_cannot_say_fails_and_writes_nothing(self):
        self.pulled()
        self.block()
        before = self.get('FOO').text
        real = omod.live_branches
        omod.live_branches = lambda root, remote: (None, 'git broke')
        self.addCleanup(setattr, omod, 'live_branches', real)
        with self.assertRaises(Failure) as cm:
            self.unblock()
        self.assertIn('git broke', str(cm.exception))
        self.assertEqual(self.get('FOO').text, before)


class Answer(RestoreCase):
    """The owner's answer filed by the Action Hub clears the owner blocker as unblock does,
    restore included."""

    def answer(self):
        return self.verb('answer', 'FOO', 'Q-101', AT, title='Keep the cap?', label='Yes',
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


class BlockedPull(unittest.TestCase):

    def test_the_note_must_be_followed_by_the_blocked_line(self):
        note = 'Stopped on branch kit/x at abcdef1234, branch kept.'
        self.assertEqual(pmod.blocked_pull('%s\nBlocked on owner (2026-10-01): R-1: why' % note),
                         ('kit/x', '2026-10-01'))
        self.assertIsNone(pmod.blocked_pull('%s\nReleased (2026-10-01): why' % note))
        self.assertIsNone(pmod.blocked_pull(note))
        self.assertIsNone(pmod.blocked_pull('no note'))
        self.assertIsNone(pmod.blocked_pull(
            '%s\nBlocked on owner (2026-10-01): why\n\nUnblocked (2026-10-02): pull by kit/x '
            'restored.' % note))


if __name__ == '__main__':
    unittest.main()
