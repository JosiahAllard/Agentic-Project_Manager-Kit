"""`delete ID --why T` (kitlib/backlog/plan.py plan_delete; docs/backlog.md
"Commands"): the record goes with the subject `deletes ID: <why>`, which history reads as it
reads `closes` and `retires` (store._last_closing), so `add` again needs `--reopen`, `show`
names the deleting commit and a `requires: item:` on it is never met. Retire's owner gates; a
live pull refuses, a dead one goes with the record, and `retire` shares that guard."""
import unittest
from unittest import mock

from kitlib.backlog import offer as omod
from kitlib.backlog import plan as pmod
from kitlib.backlog.errors import Failure, NotFound, Refusal, Usage

from tests.backlog.helpers import ADD_NONE, TODAY, StoreCase, priced, rec

OWNER_PRICED = dict(priority='2', priority_by='owner')
OWNER_BLOCKED = dict(state='blocked', blocked_on='owner', owner_ask='which one?')
WHY = 'the owner says this should not exist'


class DeleteCase(StoreCase):
    remote = False


class Delete(DeleteCase):

    def test_delete_removes_the_record_with_a_deletes_subject_and_frees_dependents(self):
        self.seed(priced('FOO'), rec('BAR', state='blocked', blocked_on='item:FOO'))
        change = self.verb('delete', 'FOO', WHY)
        self.assertIsNone(self.get('FOO'))
        self.assertEqual(change.subject, 'deletes FOO: %s' % WHY)
        self.assertEqual(change.lines[0], 'DELETED FOO')
        self.assertEqual((self.get('BAR').get('state'), self.get('BAR').get('blocked_on')),
                         ('open', '-'))
        self.assertRegex(self.store.history('FOO', self.store.load()),
                         r'^[0-9a-f]{7,} deletes FOO: %s$' % WHY)

    def test_re_adding_a_deleted_id_needs_reopen(self):
        self.seed(rec('FOO'))
        self.verb('delete', 'FOO', WHY)
        with self.assertRaises(Refusal) as cm:
            self.verb('add', 'FOO', 'it came back', ['x/**'], **ADD_NONE)
        self.assertIn('--reopen', str(cm.exception))
        self.assertIn('deletes FOO: ', str(cm.exception))
        self.verb('add', 'FOO', 'it came back', ['x/**'], reopen=True, **ADD_NONE)
        self.assertIsNotNone(self.get('FOO'))

    def test_show_names_the_deleting_commit(self):
        self.seed(rec('FOO'))
        self.verb('delete', 'FOO', WHY)
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(self.store.load(), self.ctx, 'FOO')
        self.assertIn('deletes FOO: %s' % WHY, str(cm.exception))

    def test_a_requires_on_a_deleted_record_is_never_met(self):
        self.seed(rec('FOO'), priced('BAR', requires='item:FOO'))
        self.verb('delete', 'FOO', WHY)
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'BAR')
        self.assertIn('retired or deleted, so it will never land', str(cm.exception))

    def test_a_reason_is_required_and_machine_paths_are_refused(self):
        self.seed(rec('FOO'))
        drive = 'C:' + chr(92) + 'Users' + chr(92) + 'nick'     # tests/backlog/test_verbs.py
        for why in ('', 'gone', 'see %s for why' % drive):
            with self.assertRaises(Usage, msg=why):
                self.verb('delete', 'FOO', why)
        with self.assertRaises(Usage) as cm:
            self.verb('delete', 'FOO', 'see %s for why' % drive)
        self.assertIn('machine path', str(cm.exception))
        self.assertIsNotNone(self.get('FOO'))
        with self.assertRaises(NotFound):
            self.verb('delete', 'NOPE', WHY)


class OwnerGates(DeleteCase):
    """Retire's gates: an owner-priced record is the `retire-priced` row and an owner-held one
    `close-held` (kitlib/backlog/roles.py), both top-only, so a plan with no role check refuses
    them; any other record goes without one."""

    def test_an_owner_priced_record_takes_the_role_check(self):
        self.seed(rec('FOO', **OWNER_PRICED), rec('BAR'))
        with self.assertRaises(Refusal) as cm:
            self.verb('delete', 'FOO', WHY)
        self.assertIn('retire-priced', str(cm.exception))
        self.assertIn('no role check ran', str(cm.exception))
        self.assertIsNotNone(self.get('FOO'))
        change = self.verb('delete', 'FOO', WHY, role_checked=True)
        self.assertNotIn('Ruling', change.subject)
        self.verb('delete', 'BAR', WHY)
        self.assertEqual(self.store.load().file_count, 0)

    def test_an_owner_held_record_takes_the_role_check(self):
        self.seed(rec('FOO', **OWNER_BLOCKED))
        with self.assertRaises(Refusal) as cm:
            self.verb('delete', 'FOO', WHY)
        self.assertIn('close-held', str(cm.exception))
        self.verb('delete', 'FOO', WHY, role_checked=True)
        self.assertIsNone(self.get('FOO'))


class States(DeleteCase):

    def test_a_live_pull_refuses_and_a_dead_one_goes_with_the_record(self):
        self.repo.git('branch', 'tools/live-one')
        self.seed(priced('LIVE', state='pulled', pulled_at=TODAY, pulled_by='tools/live-one'),
                  priced('DEAD', state='pulled', pulled_at=TODAY, pulled_by='tools/gone'))
        with self.assertRaises(Refusal) as cm:
            self.verb('delete', 'LIVE', WHY)
        self.assertIn('release LIVE', str(cm.exception))
        self.assertEqual(self.get('LIVE').get('state'), 'pulled')
        change = self.verb('delete', 'DEAD', WHY)
        self.assertIsNone(self.get('DEAD'))
        self.assertIn('  dead pull on `tools/gone` dropped with the record', change.lines)

    def test_a_failed_branch_probe_fails_and_deletes_nothing(self):
        # Fail closed: a git that cannot say whether the pull is live is a Failure, not a delete.
        self.seed(priced('DEAD', state='pulled', pulled_at=TODAY, pulled_by='tools/gone'))
        with mock.patch.object(omod, 'live_branches', return_value=(None, 'git failed')):
            with self.assertRaises(Failure) as cm:
                self.verb('delete', 'DEAD', WHY)
        self.assertIn('cannot tell whether the pull of `DEAD` by `tools/gone` is live: git failed',
                      str(cm.exception))
        self.assertEqual(self.get('DEAD').get('state'), 'pulled')

    def test_a_parked_record_is_deleted_and_its_kept_branch_left_alone(self):
        self.seed(rec('FOO', state='parked', q='Q-101', parked_at='2026-10-01'))
        change = self.verb('delete', 'FOO', WHY)
        self.assertIsNone(self.get('FOO'))
        self.assertIn('parked on Q-101', '\n'.join(change.lines))

    def test_a_marked_record_can_be_deleted(self):
        self.seed(rec('FOO', review='owner-hub 2026-10-15'))
        self.verb('delete', 'FOO', WHY)
        self.assertIsNone(self.get('FOO'))


class RetireShares(DeleteCase):
    """`retire` runs delete's live-pull guard (plan._refuse_live_pulls): it removes the record
    just as `delete` does, so a branch building it must release it first."""

    def test_a_live_pull_refuses_and_a_dead_one_goes_with_the_record(self):
        self.repo.git('branch', 'tools/live-one')
        self.seed(priced('LIVE', state='pulled', pulled_at=TODAY, pulled_by='tools/live-one'),
                  priced('DEAD', state='pulled', pulled_at=TODAY, pulled_by='tools/gone'))
        with self.assertRaises(Refusal) as cm:
            self.verb('retire', 'LIVE', WHY)
        self.assertIn('`LIVE` is being built on `tools/live-one`; release it first '
                      '(`release LIVE --reason T`, from that branch)', str(cm.exception))
        self.assertEqual(self.get('LIVE').get('state'), 'pulled')
        change = self.verb('retire', 'DEAD', WHY)
        self.assertIsNone(self.get('DEAD'))
        self.assertEqual(change.lines[:2], ['RETIRED DEAD',
                                            '  dead pull on `tools/gone` dropped with the record'])

    def test_a_failed_branch_probe_fails_and_retires_nothing(self):
        self.seed(priced('DEAD', state='pulled', pulled_at=TODAY, pulled_by='tools/gone'))
        with mock.patch.object(omod, 'live_branches', return_value=(None, 'git failed')):
            with self.assertRaises(Failure) as cm:
                self.verb('retire', 'DEAD', WHY)
        self.assertIn('cannot tell whether the pull of `DEAD` by `tools/gone` is live: git failed',
                      str(cm.exception))
        self.assertIsNotNone(self.get('DEAD'))

    def test_an_unpulled_record_never_asks_git(self):
        self.seed(priced('FOO'))
        with mock.patch.object(omod, 'live_branches', return_value=(None, 'git failed')):
            self.verb('retire', 'FOO', WHY)
        self.assertIsNone(self.get('FOO'))


if __name__ == '__main__':
    unittest.main()
