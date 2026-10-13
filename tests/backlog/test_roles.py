"""kitlib.backlog.roles against the approved design table and the owner's answers."""
import unittest

from kitlib.backlog import record
from kitlib.backlog import roles as R
from kitlib.config import schema

BUILDERS = ('worker', 'senior-worker', 'opus-worker', 'strong-worker', 'tech-artist', 'asset-smith', 'sound-smith')
READ_ONLY = ('reviewer', 'test-runner', 'researcher', 'consultant')
ROLES = ('owner', 'pm', 'coordinator', 'deep-worker', 'lander', 'records', 'spec-writer') + \
    BUILDERS + READ_ONLY
CPO = ('coordinator', 'pm', 'owner')
TOP = ('owner', 'pm')
OWN = dict.fromkeys(BUILDERS + ('deep-worker',), 'own-pull')

# action: (risk, roles always allowed, {role: the fact it needs}); one entry per design row
DESIGN = {
    'read': ('none', ROLES, {}),
    'add': ('low', tuple(r for r in ROLES if r not in READ_ONLY), {}),
    'set-text': ('low', CPO, OWN),
    'set-price': ('low', CPO, {}),
    'set-price-owner': ('medium', TOP, {}),
    'set-hold': ('high', TOP, {}),
    'set-other': ('medium', CPO, {}),       # the set fields no design row names
    'pull': ('low', CPO + ('deep-worker',) + BUILDERS, {}),
    'pull-collision': ('medium', CPO, {}),
    'pull-held': ('refused', (), {}),
    'release': ('low', CPO, OWN),
    'block': ('low', CPO, OWN),
    'block-owner': ('medium', CPO, {}),
    'set-status': ('medium', CPO, {}),
    'close': ('medium', CPO, {'lander': 'via-land'}),
    'close-held': ('high', TOP, {}),
    'retire': ('medium', CPO, {}),
    'retarget': ('high', TOP, {}),
    'raw-write': ('high', (), {}),
}
VERBS = {'next', 'show', 'list', 'check', 'stale', 'landable', 'add', 'set', 'block', 'unblock',
         'pull', 'release', 'close', 'retire', 'set-status', 'unpark', 'retarget', 'migrate'}


class RolesTest(unittest.TestCase):
    def test_every_role_in_the_design_is_a_role(self):
        self.assertEqual(sorted(R.ROLES), sorted(ROLES))
        self.assertEqual((R.BUILDERS, R.READ_ONLY, R.DOCS),
                         (BUILDERS, READ_ONLY, ('records', 'spec-writer')))
        self.assertIn('kitlib/backlog/roles.py', schema.OWNER_PATHS)

    def test_every_design_row_and_backlog_verb_is_present(self):
        self.assertEqual(set(R.ACTIONS), set(DESIGN))
        self.assertEqual({v for row in R.ACTIONS.values() for v in row.verbs}, VERBS)
        for action, (risk, allowed, when) in DESIGN.items():
            with self.subTest(action):
                row = R.ACTIONS[action]
                self.assertEqual((row.risk, sorted(row.allowed)), (risk, sorted(allowed)))
                self.assertEqual(sorted(row.when), sorted(when.items()))

    def test_no_unknown_role_fact_risk_or_row_is_named(self):
        for action, row in R.ACTIONS.items():
            with self.subTest(action):
                self.assertLessEqual(set(row.allowed) | {r for r, _ in row.when}, set(R.ROLES))
                self.assertLessEqual({f for _, f in row.when}, set(R.FACTS))
                self.assertIn(row.risk, R.RISKS)
                self.assertLessEqual(set(row.over), set(R.ACTIONS) - {action})
        self.assertLessEqual(set(R.SESSION_ONLY), set(R.ROLES))

    def test_builders_never_close_and_only_the_leads_set_other_fields(self):
        for action in ('close', 'close-held'):
            for role in R.BUILDERS:
                self.assertFalse(R.allows(action, role, R.FACTS), (action, role))
        for role in R.ROLES:
            self.assertEqual(R.allows('set-other', role, R.FACTS), role in CPO, role)

    def test_the_owner_takes_every_action_but_a_held_pull_or_a_raw_write(self):
        for action in R.ACTIONS:
            self.assertEqual(R.allows(action, 'owner'), action not in ('pull-held', 'raw-write'))

    def test_nobody_pulls_a_held_record_or_writes_the_store_raw_not_even_the_owner(self):
        self.assertEqual(set(R.ACTIONS['pull-held'].over), {'pull', 'pull-collision'})
        self.assertEqual(R.ACTIONS['raw-write'].verbs, ())    # no backlog.py verb is a raw write
        for role in R.ROLES + (None,):
            self.assertFalse(R.allows('pull-held', role, R.FACTS), role)
            self.assertFalse(R.allows('raw-write', role, R.FACTS), role)

    def test_a_when_grant_needs_its_fact_and_the_deep_worker_has_the_builders_grants(self):
        for action in ('set-text', 'release', 'block'):
            for role in ('worker', 'deep-worker'):
                self.assertFalse(R.allows(action, role), (action, role))
                self.assertTrue(R.allows(action, role, (R.OWN_PULL,)), (action, role))
        self.assertFalse(R.allows('close', 'lander', (R.OWN_PULL,)))
        self.assertTrue(R.allows('close', 'lander', (R.VIA_LAND,)))

    def test_a_session_role_needs_a_mark_and_an_unlisted_role_only_reads(self):
        self.assertEqual(R.role_of('pm', subagent=False), 'pm')
        self.assertIsNone(R.role_of('conductor', subagent=False))     # the old name is no role
        self.assertEqual(R.role_of('lander', subagent=True), 'lander')
        for name in ('owner', 'pm', 'coordinator', 'deep-worker', 'consultant', 'conductor'):
            self.assertIsNone(R.role_of(name, subagent=True), name)
        for name in ('general-purpose', 'plain', 'marked-owner', '', None):
            self.assertIsNone(R.role_of(name, subagent=False), name)
        self.assertEqual(R.UNLISTED_MAY, (R.NONE,))     # an unlisted role only reads
        for action, row in R.ACTIONS.items():
            self.assertEqual(R.allows(action, None), row.risk == R.NONE, action)

    def test_the_set_data_matches_the_record_schema(self):
        self.assertEqual(R.HOLD, record.OWNER_PRIORITY)
        self.assertEqual(sorted(R.PRICE_LOW + R.PRICE_TOP), ['1', '2', '3', '4', '5'])
        for band in R.PRICE_LOW + R.PRICE_TOP:
            self.assertTrue(record.is_band(band), band)
        for field in R.TEXT_FIELDS:
            self.assertTrue(field == 'body' or field in record.FIELDS, field)


if __name__ == '__main__':
    unittest.main()
