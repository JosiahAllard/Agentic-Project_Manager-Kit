"""The write verbs end to end through Store.write on a no-remote ref store (the plans do not
depend on the backend; tests/backlog/test_store.py covers the remote path). Ported from
earlier per-repository backlog tests: Add, Set, Block, Pull, Close, Retire and release
cases. Cases for cut features (stale, hold_spent, --owner-said, value shims, small_fix,
the main-checkout refusal) are not ported."""
import unittest

from kitlib.backlog.errors import NotFound, Refusal, Usage

from tests.backlog.helpers import ADD_NONE, BRANCH, TODAY, StoreCase, priced, rec

OWNER_PRICED = dict(priority='2', priority_by='owner', priority_ruling='R-9001')
OWNER_BLOCKED = dict(state='blocked', blocked_on='owner', owner_ask='which one?')


class VerbCase(StoreCase):
    remote = False


class Add(VerbCase):

    def test_add_writes_an_unpriced_open_record(self):
        self.verb('add', 'NEW', 'a new thing to do here', ['link/src/**'], effort='s', **ADD_NONE)
        r = self.get('NEW')
        self.assertEqual((r.get('priority'), r.get('priority_by'), r.get('state'), r.get('area'),
                          r.get('effort')), ('-', '-', 'open', 'link', 's'))
        self.assertIn('## Why this is here', r.body)

    def test_add_refuses_a_taken_id_a_bad_id_and_a_bad_scope(self):
        self.seed(rec('FOO'))
        with self.assertRaises(Refusal):
            self.verb('add', 'FOO', 'again', ['x/**'], **ADD_NONE)
        with self.assertRaises(Usage):
            self.verb('add', 'lower', 'bad id', ['x/**'], **ADD_NONE)
        with self.assertRaises(Usage):
            self.verb('add', 'NEW', 'bad scope', ['*.java'], **ADD_NONE)
        with self.assertRaises(Usage):
            self.verb('add', 'NEW', '   ', ['x/**'], **ADD_NONE)

    def test_re_adding_a_closed_id_needs_reopen(self):
        self.seed(rec('FOO'))
        self.verb('close', 'FOO', 'the work is finished')
        with self.assertRaises(Refusal) as cm:
            self.verb('add', 'FOO', 'it came back', ['x/**'], **ADD_NONE)
        self.assertIn('--reopen', str(cm.exception))
        self.verb('add', 'FOO', 'it came back', ['x/**'], reopen=True, **ADD_NONE)
        self.assertIsNotNone(self.get('FOO'))


class Set(VerbCase):

    def test_an_agent_price(self):
        self.seed(rec('FOO'))
        self.verb('set', 'FOO', [('priority', ['3']), ('priority_by', ['agent'])])
        self.assertEqual(self.get('FOO').get('priority'), '3')

    def test_a_pm_price_needs_a_ruling_and_records_it(self):
        self.seed(rec('FOO'))
        with self.assertRaises(Refusal):
            self.verb('set', 'FOO', [('priority', ['2']), ('priority_by', ['pm'])])
        self.verb('set', 'FOO', [('priority', ['2']), ('priority_by', ['pm'])], ruling='R-9001')
        self.assertEqual(self.get('FOO').get('priority_ruling'), 'R-9001')

    def test_an_owner_price_is_not_repriced_on_an_agents_word(self):
        self.seed(rec('FOO', **OWNER_PRICED))
        with self.assertRaises(Refusal):
            self.verb('set', 'FOO', [('priority', ['4'])])

    def test_agent_priority_drops_the_ruling(self):
        self.seed(rec('FOO', priority='2', priority_by='pm', priority_ruling='R-9001'))
        self.verb('set', 'FOO', [('priority_by', ['agent'])], ruling='R-9002')
        self.assertIsNone(self.get('FOO').get('priority_ruling'))

    def test_keys_only_a_verb_writes_are_refused(self):
        self.seed(rec('FOO'))
        for key in ('pulled_by', 'preempt', 'displaced', 'priority_ruling', 'hold_ruling'):
            with self.assertRaises(Refusal, msg=key):
                self.verb('set', 'FOO', [(key, ['x'])])

    def test_set_neither_enters_nor_leaves_pulled(self):
        self.seed(priced('FOO'), priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=BRANCH))
        with self.assertRaises(Refusal):
            self.verb('set', 'FOO', [('state', ['pulled'])])
        with self.assertRaises(Refusal):
            self.verb('set', 'BAR', [('state', ['open'])])

    def test_hold_ruling_re_cites_and_changes_nothing_else(self):
        self.seed(rec('FOO', hold_ruling='R-9001'))
        with self.assertRaises(Usage):
            self.verb('set', 'FOO', [('effort', ['s'])], hold_ruling='R-9002')
        self.verb('set', 'FOO', [], hold_ruling='R-9002')
        self.assertEqual(self.get('FOO').get('hold_ruling'), 'R-9002')
        with self.assertRaises(Refusal):
            self.verb('set', 'FOO', [], hold_ruling='R-9002')

    def test_unknown_and_required_fields(self):
        self.seed(rec('FOO'))
        with self.assertRaises(Usage):
            self.verb('set', 'FOO', [('nope', ['x'])])
        with self.assertRaises(Usage):
            self.verb('set', 'FOO', [], unset=['summary'])
        with self.assertRaises(NotFound):
            self.verb('set', 'NOPE', [('effort', ['s'])])


class RulingShapeOnly(VerbCase):
    """A ruling id is taken on its word: only its shape and the record's own cites are checked."""

    def owner_price(self, ruling, rid='FOO'):
        return self.verb('set', rid, [('priority', ['1']), ('priority_by', ['owner'])],
                         ruling=ruling)

    def test_any_well_shaped_id_prices_a_record(self):
        self.seed(rec('FOO'))
        self.owner_price('R-9999')
        self.assertEqual(self.get('FOO').get('priority_ruling'), 'R-9999')

    def test_a_bad_shape_is_usage_and_one_ruling_is_one_change(self):
        self.seed(rec('FOO', **OWNER_PRICED))
        with self.assertRaises(Usage):
            self.owner_price('R-12')
        with self.assertRaises(Refusal) as cm:
            self.owner_price('R-9001')
        self.assertIn('One ruling, one change', str(cm.exception))
        self.assertEqual(self.get('FOO').get('priority'), '2')

    def test_retiring_an_owner_held_record_takes_a_ruling(self):
        self.seed(rec('FOO', **OWNER_BLOCKED))
        change = self.verb('retire', 'FOO', 'no longer wanted at all', ruling='R-9001')
        self.assertIsNone(self.get('FOO'))
        self.assertEqual(change.ruling, 'R-9001')
        self.assertEqual(change.subject, 'retires FOO: no longer wanted at all')
        self.assertNotIn('Owner said', ' '.join(change.lines))

    def test_any_well_shaped_id_releases_an_owner_hold(self):
        self.seed(rec('FOO', **OWNER_BLOCKED))
        self.verb('unblock', 'FOO', ruling='R-9004')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('hold_ruling'), r.get('owner_ask')),
                         ('open', 'R-9004', None))


class BlockUnblock(VerbCase):

    def test_block_replaces_blocked_on_and_unblock_opens(self):
        self.seed(rec('FOO'), rec('BAR'))
        self.verb('block', 'FOO', ['item:BAR', 'live-client'])
        self.assertEqual(self.get('FOO').all('blocked_on'), ['item:BAR', 'live-client'])
        self.verb('block', 'FOO', ['live-client'])
        self.assertEqual(self.get('FOO').all('blocked_on'), ['live-client'])
        self.verb('unblock', 'FOO')
        self.assertEqual((self.get('FOO').get('state'), self.get('FOO').get('blocked_on')),
                         ('open', '-'))

    def test_block_refusals(self):
        self.seed(rec('FOO'), priced('BAR', state='pulled', pulled_at=TODAY, pulled_by=BRANCH))
        for members, exc in ((['item:GONE'], Usage), (['item:FOO'], Usage), (['-'], Usage),
                             (['owner'], Usage), (['whenever'], Usage)):
            with self.assertRaises(exc, msg=members):
                self.verb('block', 'FOO', members)
        with self.assertRaises(Refusal):
            self.verb('block', 'BAR', ['live-client'])

    def test_the_owners_hold_is_released_only_with_a_ruling(self):
        self.seed(rec('FOO', **OWNER_BLOCKED))
        with self.assertRaises(Refusal):
            self.verb('unblock', 'FOO')
        with self.assertRaises(Refusal):
            self.verb('block', 'FOO', ['live-client'])
        self.verb('block', 'FOO', ['live-client'], ruling='R-9001')
        r = self.get('FOO')
        self.assertEqual((r.all('blocked_on'), r.get('hold_ruling')), (['live-client'], 'R-9001'))


class PullRelease(VerbCase):

    def test_an_offered_pull_records_the_branch(self):
        self.seed(priced('FOO'))
        change = self.verb('pull', 'FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.get('pulled_at')),
                         ('pulled', BRANCH, TODAY))
        self.assertIsNone(r.get('preempt'))
        self.assertEqual(change.subject, 'backlog: pull FOO onto %s' % BRANCH)

    def test_an_out_of_order_pull_needs_a_reason_and_records_the_displaced(self):
        self.seed(priced('FOO', priority='1'), *[priced('R%d' % i, priority='1') for i in range(5)],
                  priced('LOW', priority='5'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'LOW')
        self.assertIn('cap', str(cm.exception))
        self.verb('pull', 'LOW', reason='the owner wants it today')
        r = self.get('LOW')
        self.assertEqual(r.get('preempt'), 'the owner wants it today')
        self.assertEqual(len(r.all('displaced')), 5)
        self.assertEqual(r.get('preempt_by'), BRANCH)

    def test_pull_refusals(self):
        self.seed(rec('UNP'), priced('BLK', state='blocked', blocked_on='live-client'),
                  rec('MIN', priority='-1', owner_ask='which?'),
                  priced('REQ', requires='item:UNP'))
        for rid in ('UNP', 'BLK', 'MIN', 'REQ'):
            with self.assertRaises(Refusal, msg=rid):
                self.verb('pull', rid)
        self.verb('pull', 'MIN', reason='the owner said go', ruling='R-9001')
        r = self.get('MIN')
        self.assertEqual((r.get('priority'), r.get('hold_ruling'), r.get('owner_ask')),
                         ('-', 'R-9001', None))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'MIN', reason='twice is once too many')
        self.assertIn('already in flight', str(cm.exception))

    def test_release_own_pull_clears_the_pull_and_the_preempt(self):
        self.seed(priced('FOO', priority='5'), priced('BAR', priority='1'))
        self.verb('pull', 'FOO', reason='needed before the others')
        self.verb('release', 'FOO', 'handing it back unfinished')
        r = self.get('FOO')
        self.assertEqual(r.get('state'), 'open')
        for key in ('pulled_by', 'pulled_at', 'preempt', 'preempt_by', 'preempt_at', 'displaced'):
            self.assertIsNone(r.get(key), key)

    def test_release_of_another_branch_only_when_dead(self):
        self.repo.git('branch', 'tools/live-one')
        self.seed(priced('LIVE', state='pulled', pulled_at=TODAY, pulled_by='tools/live-one'),
                  priced('DEAD', state='pulled', pulled_at=TODAY, pulled_by='tools/gone'))
        with self.assertRaises(Refusal):
            self.verb('release', 'LIVE', 'taking it over from them')
        change = self.verb('release', 'DEAD', 'its branch is gone for good')
        self.assertIn('(dead)', '\n'.join(change.lines))
        with self.assertRaises(Usage):
            self.verb('release', 'DEAD', 'short')


class CloseRetire(VerbCase):

    def test_close_deletes_and_unblocks_dependents(self):
        self.seed(priced('FOO'), rec('BAR', state='blocked', blocked_on='item:FOO'),
                  rec('BAZ', state='blocked', blocked_on=['item:FOO', 'live-client']))
        change = self.verb('close', 'FOO', 'the work is finished')
        self.assertIsNone(self.get('FOO'))
        self.assertEqual((self.get('BAR').get('state'), self.get('BAR').get('blocked_on')),
                         ('open', '-'))
        self.assertEqual(self.get('BAZ').all('blocked_on'), ['live-client'])
        self.assertEqual(change.subject, 'closes FOO: the work is finished')
        self.assertRegex(self.store.history('FOO', self.store.load()),
                         r'^[0-9a-f]{7,} closes FOO: the work is finished$')

    def test_close_of_an_owner_held_record_needs_a_ruling(self):
        self.seed(rec('FOO', **OWNER_BLOCKED))
        with self.assertRaises(Refusal):
            self.verb('close', 'FOO', 'the work is finished')
        self.verb('close', 'FOO', 'the work is finished', ruling='R-9001')
        self.assertIsNone(self.get('FOO'))

    def test_retire_of_an_owner_priced_record_needs_a_ruling(self):
        self.seed(rec('FOO', **OWNER_PRICED), rec('BAR'))
        with self.assertRaises(Refusal):
            self.verb('retire', 'FOO', 'no longer wanted at all')
        with self.assertRaises(Usage):
            self.verb('retire', 'BAR', 'no longer wanted at all', ruling='R-9002')
        self.verb('retire', 'FOO', 'no longer wanted at all', ruling='R-9002')
        self.verb('retire', 'BAR', 'no longer wanted at all')
        self.assertEqual(self.store.load().file_count, 0)

    def test_a_reason_is_required(self):
        self.seed(rec('FOO'))
        with self.assertRaises(Usage):
            self.verb('close', 'FOO', 'done')
        with self.assertRaises(Usage):
            self.verb('retire', 'FOO', '')


class MachinePaths(VerbCase):
    """Free text that carries a machine path is refused (Usage) before any write, in every verb."""
    DRIVES = ('C:' + chr(92) + 'Users' + chr(92) + 'nick', 'C:' + '/Users/nick')

    def refused(self, name, *args, **kw):
        before = self.remote_tip()
        with self.assertRaises(Usage) as ctx:
            self.verb(name, *args, **kw)
        self.assertIn('machine path', str(ctx.exception))
        self.assertEqual(self.remote_tip(), before)

    def test_each_free_text_field_refuses_a_drive_path(self):
        self.seed(priced('FOO'), rec('BAR'), rec('BAZ', **OWNER_BLOCKED))
        for d in self.DRIVES:
            text = 'see %s for the details' % d
            with self.subTest(drive=d):
                self.refused('add', 'NEW', text, ['link/src/**'], **ADD_NONE)
                self.refused('set', 'FOO', [('summary', [text])])
                self.refused('block', 'BAR', ['owner'], owner_ask=text)
                self.refused('pull', 'FOO', reason=text)
                self.refused('release', 'FOO', text)
                self.refused('close', 'FOO', text)
                self.refused('retire', 'BAR', text)

    def test_set_body_refuses_a_drive_path(self):
        self.seed(rec('FOO'))
        for d in self.DRIVES:
            with self.subTest(drive=d):
                self.refused('set', 'FOO', [('summary', ['a clean replacement summary'])],
                             body='notes at %s here\n' % d)

    def test_a_refused_close_leaves_the_record_and_clean_text_still_closes(self):
        self.seed(priced('FOO'))
        self.refused('close', 'FOO', 'done, see %s' % self.DRIVES[0])
        self.assertIsNotNone(self.get('FOO'))
        self.verb('close', 'FOO', 'the work is finished')
        self.assertIsNone(self.get('FOO'))


if __name__ == '__main__':
    unittest.main()
