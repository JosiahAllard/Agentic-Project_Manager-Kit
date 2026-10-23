"""The owner gates kitlib/backlog/roles.py's TOP rows hold (docs/backlog.md "Owner gates"): a
write tools/backlog.py's role check sorted into `set-price-owner`, `set-hold`, `unblock-owner`,
`close-held` or `retire-priced` is the owner's and the PM's, and the role check decides it; no
verb takes a cited id. The plan writes such a move only when its caller says the role check ran
(`role_checked=True`: kitlib/backlog/opener.py for the CLI, kitlib/hubsite/recordwrite.py for
the owner's routes); every other caller (a landing's close, a library caller) is refused, naming
the row. A record that carries an old `priority_ruling` loses it when its price moves; an old
`hold_ruling` is kept, never written."""
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

    def unchecked(self, write, row):
        """`write`, planned with no role check, is refused naming the TOP `row`: a Refusal, not a
        Usage (the store's lint would raise one and pass a bare assertRaises(Refusal))."""
        with self.assertRaises(Refusal) as cm:
            write()
        self.assertNotIsInstance(cm.exception, Usage, cm.exception)
        msg = str(cm.exception)
        self.assertIn('`%s`' % row, msg)
        self.assertIn('no role check ran', msg)
        self.assertNotIn('--ruling', msg)
        return cm.exception


class Close(TopCase):
    """close-held (TOP): an owner-held record closes once the role check ran."""

    def test_a_held_record_closes_when_the_role_check_ran(self):
        for rid, fields in (('HELD', OWNER_BLOCKED), ('MINUS', MINUS_ONE)):
            self.seed(rec(rid, **fields))
            change = self.checked('close', rid, REASON)
            self.assertIsNone(self.get(rid), rid)
            self.assertIsNone(change.ruling, rid)

    def test_without_the_role_check_a_held_close_is_refused(self):
        # a landing's close (kitlib/backlog/at_landing.py) runs with no role check
        self.seed(rec('HELD', **OWNER_BLOCKED))
        self.unchecked(lambda: self.verb('close', 'HELD', REASON), 'close-held')
        self.assertIsNotNone(self.get('HELD'))

    def test_without_the_role_check_an_unheld_close_is_written(self):
        self.seed(rec('PRICED', **OWNER_PRICED))
        self.verb('close', 'PRICED', REASON)
        self.assertIsNone(self.get('PRICED'))


class RetireDelete(TopCase):
    """close-held (TOP) covers a held record, retire-priced (TOP) an owner- or PM-priced one."""

    def test_a_held_record_retires_and_deletes_when_the_role_check_ran(self):
        self.seed(rec('A', **OWNER_BLOCKED), rec('B', **MINUS_ONE))
        self.assertIsNone(self.checked('retire', 'A', WHY).ruling)
        self.assertIsNone(self.checked('delete', 'B', WHY).ruling)
        self.assertIsNone(self.get('A'))
        self.assertIsNone(self.get('B'))

    def test_an_owner_priced_record_retires_and_deletes_when_the_role_check_ran(self):
        for verb in ('retire', 'delete'):
            self.seed(rec('FOO', **OWNER_PRICED))
            self.checked(verb, 'FOO', WHY)
            self.assertIsNone(self.get('FOO'), verb)

    def test_without_the_role_check_an_owner_priced_record_is_refused(self):
        for verb in ('retire', 'delete'):
            self.seed(rec('FOO', **OWNER_PRICED))
            self.unchecked(lambda: self.verb(verb, 'FOO', WHY), 'retire-priced')
            self.assertIsNotNone(self.get('FOO'), verb)

    def test_without_the_role_check_a_held_record_is_refused(self):
        for verb in ('retire', 'delete'):
            self.seed(rec('FOO', **OWNER_BLOCKED))
            self.unchecked(lambda: self.verb(verb, 'FOO', WHY), 'close-held')
            self.assertIsNotNone(self.get('FOO'), verb)


class Price(TopCase):
    """set-price-owner, set-hold and unblock-owner (TOP): a move they cover is written once the
    role check ran."""

    def test_an_owner_priced_record_is_repriced(self):
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
        self.assertIn('priority_ruling R-9001 dropped: it named the old price', out)

    def test_priority_one_by_the_owner_is_written(self):
        self.seed(priced('FOO', priority='3', priority_by='agent'))
        self.checked('set', 'FOO', [('priority', ['1']), ('priority_by', ['owner'])])
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_by')), ('1', 'owner'))
        self.assertIsNone(foo.get('priority_ruling'))

    def test_a_minus_one_record_is_released_and_priced(self):
        self.seed(rec('A', **MINUS_ONE), rec('B', **MINUS_ONE))
        self.checked('set', 'A', [('priority', ['-'])], ('owner_ask',))
        self.assertEqual(self.get('A').get('priority'), '-')
        self.assertIsNone(self.get('A').get('hold_ruling'))
        self.checked('set', 'B', [('priority', ['2']), ('priority_by', ['owner'])],
                     ('owner_ask',))
        self.assertEqual(self.get('B').get('priority'), '2')

    def test_without_the_role_check_the_price_gates_refuse(self):
        self.seed(rec('FOO', **OWNER_PRICED), rec('M', **MINUS_ONE))
        self.unchecked(lambda: self.verb('set', 'FOO', [('priority', ['3'])]), 'set-price-owner')
        self.unchecked(lambda: self.verb('set', 'M', [('priority', ['-'])], ('owner_ask',)),
                       'set-hold')
        self.assertEqual(self.get('FOO').get('priority'), '2')
        self.assertEqual(self.get('M').get('priority'), '-1')

    def test_a_pm_price_is_written_with_no_ruling_beside_it(self):
        # record.py no longer asks a `priority_by: pm` for a `priority_ruling`
        self.seed(rec('FOO', **OWNER_PRICED))
        self.checked('set', 'FOO', [('priority', ['3']), ('priority_by', ['pm'])])
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_by')), ('3', 'pm'))
        self.assertIsNone(foo.get('priority_ruling'))

    def test_a_reprice_that_keeps_a_records_pm_price_drops_its_old_ruling(self):
        self.seed(rec('FOO', priority='2', priority_by='pm', priority_ruling='R-9001'))
        self.checked('set', 'FOO', [('priority', ['3'])])
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_by')), ('3', 'pm'))
        self.assertIsNone(foo.get('priority_ruling'))

    def test_an_owner_priced_owner_blocked_record_is_repriced(self):
        self.seed(rec('BLK', **dict(OWNER_PRICED, **OWNER_BLOCKED)))
        change = self.checked('set', 'BLK', [('priority', ['4']), ('priority_by', ['owner'])])
        blk = self.get('BLK')
        self.assertEqual((blk.get('priority'), blk.get('priority_by')), ('4', 'owner'))
        self.assertEqual(blk.get('blocked_on'), 'owner')
        self.assertIsNone(change.ruling)

    def test_a_minus_one_release_keeps_an_old_hold_ruling_unwritten(self):
        self.seed(rec('FOO', hold_ruling='R-9005', **MINUS_ONE))
        change = self.checked('set', 'FOO', [('priority', ['-'])], ('owner_ask',))
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('hold_ruling')), ('-', 'R-9005'))
        self.assertNotIn('hold_ruling', '\n'.join(change.lines))

    def test_a_reprice_of_an_agent_priced_owner_blocked_record_is_set_price_owner(self):
        self.seed(priced('BLK', priority_by='agent', state='blocked', blocked_on='owner',
                         owner_ask='which one?'))
        self.unchecked(lambda: self.verb('set', 'BLK', [('priority', ['3'])]), 'set-price-owner')
        self.assertEqual(self.get('BLK').get('priority'), '2')
        # owner-held, so the price moves only with `--priority-by owner|pm`
        self.checked('set', 'BLK', [('priority', ['3']), ('priority_by', ['owner'])])
        self.assertEqual(self.get('BLK').get('priority'), '3')

    def test_a_priority_by_change_of_an_owners_price_is_set_price_owner(self):
        # no price move, but it changes the owner's price; `agent` carries no ruling, so an old
        # one goes
        self.seed(rec('FOO', **OWNER_PRICED))
        self.unchecked(lambda: self.verb('set', 'FOO', [('priority_by', ['agent'])]),
                       'set-price-owner')
        self.assertEqual(self.get('FOO').get('priority_by'), 'owner')
        change = self.checked('set', 'FOO', [('priority_by', ['agent'])])
        self.assertIsNone(self.get('FOO').get('priority_ruling'))
        self.assertIn('`priority_by: agent` carries no ruling', '\n'.join(change.lines))

    def test_clearing_the_owner_from_blocked_on_through_set_is_unblock_owner(self):
        self.seed(priced('BLK', priority_by='agent', state='blocked', blocked_on='owner',
                         owner_ask='which one?'))
        clear = ([('blocked_on', ['-']), ('state', ['open'])], ('owner_ask',))
        self.unchecked(lambda: self.verb('set', 'BLK', *clear), 'unblock-owner')
        self.assertEqual(self.get('BLK').get('blocked_on'), 'owner')
        self.checked('set', 'BLK', *clear)
        self.assertEqual(self.get('BLK').get('state'), 'open')


class Pull(TopCase):
    """pull-held refuses every role, so the plan refuses a -1 pull whatever the caller may take."""

    def test_a_minus_one_record_is_never_pulled(self):
        self.seed(rec('FOO', **MINUS_ONE))
        for may_collide in (False, True):
            with self.assertRaises(Refusal) as cm:
                self.verb('pull', 'FOO', 'the owner said go', may_collide)
            self.assertIn('set FOO --priority', str(cm.exception))
            self.assertEqual(self.get('FOO').get('priority'), '-1')


if __name__ == '__main__':
    unittest.main()
