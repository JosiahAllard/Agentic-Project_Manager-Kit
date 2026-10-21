"""The owner gates kitlib/backlog/roles.py's TOP rows already hold (docs/backlog.md
"Owner gates"): a write tools/backlog.py's role check sorted into `set-price-owner`, `set-hold` or
`close-held` needs no `--ruling`, because only the owner or the PM may take those rows. The plan
lifts its own ruling gate only when its caller says the role check ran (`role_checked=True`:
kitlib/backlog/opener.py for the CLI, kitlib/hubsite/recordwrite.py for the owner's routes);
every other caller (a landing's close, the daily run's price) keeps the gate. A ruling given is
still checked and recorded. The gates no TOP row covers keep `--ruling`."""
import unittest

from kitlib.backlog.errors import Refusal, Usage

from tests.backlog.helpers import StoreCase, priced, rec

OWNER_PRICED = dict(priority='2', priority_by='owner', priority_ruling='R-9001')
OWNER_BLOCKED = dict(state='blocked', blocked_on='owner', owner_ask='which one?')
MINUS_ONE = dict(priority='-1', priority_by='-', owner_ask='which one?')
WHY = 'the owner says this should not exist'
REASON = 'the work landed on main'


class TopCase(StoreCase):
    remote = False

    def checked(self, name, *args, **kw):
        return self.verb(name, *args, role_checked=True, **kw)

    def gated(self, write):
        """`write` is refused for want of a ruling: a Refusal naming `--ruling`, not a Usage (the
        store's lint would raise one and pass a bare assertRaises(Refusal))."""
        with self.assertRaises(Refusal) as cm:
            write()
        self.assertNotIsInstance(cm.exception, Usage, cm.exception)
        self.assertIn('--ruling', str(cm.exception))
        return cm.exception


class Close(TopCase):
    """close-held (TOP): an owner-held record closes without a ruling once the role check ran."""

    def test_a_held_record_closes_without_a_ruling_when_the_role_check_ran(self):
        for rid, fields in (('HELD', OWNER_BLOCKED), ('MINUS', MINUS_ONE)):
            self.seed(rec(rid, **fields))
            change = self.checked('close', rid, REASON)
            self.assertIsNone(self.get(rid), rid)
            self.assertIsNone(change.ruling, rid)

    def test_without_the_role_check_a_held_close_still_needs_a_ruling(self):
        # a landing's close (kitlib/backlog/at_landing.py) runs with no role check
        self.seed(rec('HELD', **OWNER_BLOCKED))
        with self.assertRaises(Refusal) as cm:
            self.verb('close', 'HELD', REASON)
        self.assertIn('--ruling', str(cm.exception))
        self.assertIsNotNone(self.get('HELD'))

    def test_a_ruling_given_is_still_checked_and_recorded(self):
        self.seed(rec('HELD', **OWNER_BLOCKED))
        change = self.checked('close', 'HELD', REASON, ruling='R-9002')
        self.assertEqual(change.ruling, 'R-9002')
        self.seed(rec('PRICED', **OWNER_PRICED))
        with self.assertRaises(Usage):
            self.checked('close', 'PRICED', REASON, ruling='R-9002')    # not held


class RetireDelete(TopCase):
    """close-held (TOP) covers a held record; an owner- or PM-priced one keeps its ruling."""

    def test_a_held_record_retires_and_deletes_without_a_ruling(self):
        self.seed(rec('A', **OWNER_BLOCKED), rec('B', **MINUS_ONE))
        self.assertIsNone(self.checked('retire', 'A', WHY).ruling)
        self.assertIsNone(self.checked('delete', 'B', WHY).ruling)
        self.assertIsNone(self.get('A'))
        self.assertIsNone(self.get('B'))

    def test_a_ruling_on_a_held_record_is_still_accepted(self):
        self.seed(rec('A', **OWNER_BLOCKED))
        self.assertEqual(self.checked('delete', 'A', WHY, ruling='R-9002').ruling, 'R-9002')

    def test_an_owner_priced_record_still_needs_a_ruling(self):
        for verb in ('retire', 'delete'):
            self.seed(rec('FOO', **OWNER_PRICED))
            self.gated(lambda: self.checked(verb, 'FOO', WHY))
            self.assertIsNotNone(self.get('FOO'), verb)

    def test_without_the_role_check_a_held_record_still_needs_a_ruling(self):
        for verb in ('retire', 'delete'):
            self.seed(rec('FOO', **OWNER_BLOCKED))
            self.gated(lambda: self.verb(verb, 'FOO', WHY))
            self.assertIsNotNone(self.get('FOO'), verb)


class Price(TopCase):
    """set-price-owner and set-hold (TOP): a price move they cover needs no ruling."""

    def test_an_owner_priced_record_is_repriced_without_a_ruling(self):
        self.seed(rec('FOO', **OWNER_PRICED))
        change = self.checked('set', 'FOO', [('priority', ['3'])])
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_by')), ('3', 'owner'))
        self.assertIsNone(change.ruling)

    def test_the_stale_priority_ruling_is_dropped_and_said(self):
        self.seed(rec('FOO', **OWNER_PRICED))
        change = self.checked('set', 'FOO', [('priority', ['3'])])
        self.assertIsNone(self.get('FOO').get('priority_ruling'))
        out = '\n'.join(change.lines)
        self.assertIn('priority_ruling: R-9001 -> (absent)', out)
        self.assertIn('priority_ruling R-9001 dropped', out)
        self.assertIn('without a ruling', out)

    def test_a_ruling_given_replaces_the_priority_ruling(self):
        self.seed(rec('FOO', **OWNER_PRICED))
        change = self.checked('set', 'FOO', [('priority', ['3'])], (), 'R-9002')
        self.assertEqual(self.get('FOO').get('priority_ruling'), 'R-9002')
        self.assertEqual(change.ruling, 'R-9002')
        self.assertNotIn('dropped', '\n'.join(change.lines))

    def test_priority_one_by_the_owner_needs_no_ruling(self):
        self.seed(priced('FOO', priority='3', priority_by='agent'))
        self.checked('set', 'FOO', [('priority', ['1']), ('priority_by', ['owner'])])
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_by')), ('1', 'owner'))
        self.assertIsNone(foo.get('priority_ruling'))

    def test_a_minus_one_record_is_released_and_priced_without_a_ruling(self):
        self.seed(rec('A', **MINUS_ONE), rec('B', **MINUS_ONE))
        self.checked('set', 'A', [('priority', ['-'])], ('owner_ask',))
        self.assertEqual(self.get('A').get('priority'), '-')
        self.assertIsNone(self.get('A').get('hold_ruling'))
        self.checked('set', 'B', [('priority', ['2']), ('priority_by', ['owner'])],
                     ('owner_ask',))
        self.assertEqual(self.get('B').get('priority'), '2')

    def test_without_the_role_check_the_price_gates_stay(self):
        self.seed(rec('FOO', **OWNER_PRICED), rec('M', **MINUS_ONE))
        self.gated(lambda: self.verb('set', 'FOO', [('priority', ['3'])]))
        self.gated(lambda: self.verb('set', 'M', [('priority', ['-'])], ('owner_ask',)))
        self.assertEqual(self.get('FOO').get('priority'), '2')
        self.assertEqual(self.get('M').get('priority'), '-1')

    def test_a_pm_price_still_needs_its_ruling(self):
        # record.py's lint: `priority_by: pm` is written only beside its `priority_ruling`
        self.seed(rec('FOO', **OWNER_PRICED))
        self.gated(lambda: self.checked('set', 'FOO', [('priority', ['3']),
                                                       ('priority_by', ['pm'])]))

    def test_a_reprice_that_keeps_a_records_pm_price_still_needs_its_ruling(self):
        # no `--priority-by`: the record's own `priority_by: pm` stays, so its ruling must too
        self.seed(rec('FOO', priority='2', priority_by='pm', priority_ruling='R-9001'))
        exc = self.gated(lambda: self.checked('set', 'FOO', [('priority', ['3'])]))
        self.assertIn('priority_by: pm', str(exc))
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_ruling')), ('2', 'R-9001'))
        self.checked('set', 'FOO', [('priority', ['3'])], (), 'R-9002')
        self.assertEqual(self.get('FOO').get('priority_ruling'), 'R-9002')

    def test_an_owner_priced_owner_blocked_record_is_repriced_without_a_ruling(self):
        # set-price-owner (TOP): only the agent-priced owner-blocked case keeps the ruling
        self.seed(rec('BLK', **dict(OWNER_PRICED, **OWNER_BLOCKED)))
        change = self.checked('set', 'BLK', [('priority', ['4']), ('priority_by', ['owner'])])
        blk = self.get('BLK')
        self.assertEqual((blk.get('priority'), blk.get('priority_by')), ('4', 'owner'))
        self.assertEqual(blk.get('blocked_on'), 'owner')
        self.assertIsNone(change.ruling)

    def test_a_minus_one_release_beside_a_live_hold_ruling_still_names_it(self):
        self.seed(rec('FOO', hold_ruling='R-9005', **MINUS_ONE))
        exc = self.gated(lambda: self.checked('set', 'FOO', [('priority', ['-'])],
                                              ('owner_ask',)))
        self.assertIn('R-9005', str(exc))

    def test_gaps_no_top_row_covers_keep_the_ruling(self):
        # an owner-blocked record priced by an agent: `set-price`, open to coordinators
        self.seed(priced('BLK', priority_by='agent', state='blocked', blocked_on='owner',
                         owner_ask='which one?'),
                  rec('FOO', **OWNER_PRICED))
        self.gated(lambda: self.checked('set', 'BLK', [('priority', ['3'])]))
        # a priority_by change with no price move: `set-other`
        self.gated(lambda: self.checked('set', 'FOO', [('priority_by', ['agent'])]))
        # clearing the owner from blocked_on through set
        self.gated(lambda: self.checked('set', 'BLK', [('blocked_on', ['-']), ('state', ['open'])],
                                        ('owner_ask',)))
        self.assertEqual(self.get('BLK').get('priority'), '2')


class Pull(TopCase):
    """pull-held refuses every role, so the plan refuses a -1 pull whatever ruling it is given."""

    def test_a_minus_one_record_is_never_pulled(self):
        self.seed(rec('FOO', **MINUS_ONE))
        for ruling in (None, 'R-9002'):
            with self.assertRaises(Refusal) as cm:
                self.verb('pull', 'FOO', 'the owner said go', ruling)
            self.assertIn('set FOO --priority', str(cm.exception))
            self.assertEqual(self.get('FOO').get('priority'), '-1')


if __name__ == '__main__':
    unittest.main()
