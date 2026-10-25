"""plan_set takes no ruling id (KIT-BACKLOG-UNCITE-REST-3): an owner gate is a role row
(kitlib/backlog/roles.py) the caller's role check decides, never a cited id. The old sixth
positional slot is kept only for kitlib/hubsite/recordwrite.py, which passes None there; any other
value is refused and nothing is written. A priority-1/pm price is written only once the role check
ran, and then with no `priority_ruling` and no `Ruling:` line in the store commit. The ruling id is
made up and built at run time, so no ruling id is written here."""
import unittest

from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal, Usage

from tests.backlog.helpers import StoreCase, rec

RULING = 'R-' + '9001'        # made up; never a real ruling
PM_ONE = [('priority', ['1']), ('priority_by', ['pm'])]


class NoRuling(StoreCase):
    remote = False

    def store_message(self):
        return self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)

    def test_a_value_in_the_old_ruling_slot_is_refused_and_nothing_is_written(self):
        self.seed(rec('FOO'))
        before = self.get('FOO').text
        with self.assertRaises(Usage) as cm:
            self.verb('set', 'FOO', PM_ONE, (), RULING)
        self.assertNotIn(RULING, str(cm.exception))
        self.assertEqual(self.get('FOO').text, before)
        self.assertIsNone(self.get('FOO').get('priority_ruling'))

    def test_a_ruling_keyword_is_no_parameter(self):
        self.seed(rec('FOO'))
        with self.assertRaises(TypeError):
            self.verb('set', 'FOO', PM_ONE, ruling=RULING)
        self.assertEqual(self.get('FOO').get('priority'), '-')

    def test_without_the_role_check_a_pm_price_of_one_is_refused(self):
        self.seed(rec('FOO'))
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'FOO', PM_ONE)
        self.assertNotIsInstance(cm.exception, Usage)
        self.assertIn('no role check ran', str(cm.exception))
        self.assertEqual(self.get('FOO').get('priority'), '-')

    def test_role_checked_a_pm_price_of_one_is_written_with_no_ruling(self):
        self.seed(rec('FOO'))
        change = self.verb('set', 'FOO', PM_ONE, (), None, role_checked=True)
        foo = self.get('FOO')
        self.assertEqual((foo.get('priority'), foo.get('priority_by')), ('1', 'pm'))
        self.assertIsNone(foo.get('priority_ruling'))
        self.assertIsNone(change.ruling)
        self.assertEqual(change.subject, 'backlog: set FOO')
        self.assertNotIn('Ruling:', self.store_message())

    def test_the_hub_owner_route_shape_still_passes_none_in_the_slot(self):
        # kitlib/hubsite/recordwrite.py: plan_set(s, ctx, rid, changes, unset, None, body, ...)
        self.seed(rec('FOO'))
        body = 'a new body\n'
        change = self.verb('set', 'FOO', [('summary', ['renamed'])], [], None, body,
                                role_checked=True, may_collide=True)
        foo = self.get('FOO')
        self.assertEqual(foo.get('summary'), 'renamed')
        self.assertEqual(foo.body.strip(), body.strip())
        self.assertIsNone(change.ruling)


if __name__ == '__main__':
    unittest.main()
