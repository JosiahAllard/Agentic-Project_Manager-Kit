"""kitlib.backlog.roles against the approved design table and the owner's answers."""
import importlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import content
from kitlib.backlog import policy
from kitlib.backlog import record
from kitlib.backlog import roles as R
from kitlib.config import schema
from tests import inproc
from tests.backlog.helpers import ADD_FLAGS, kit_json_ref, parse
from tests.gitfixture import TempRepo, fixture_env, rmtree

BUILDERS = ('weak-worker', 'worker', 'senior-worker', 'opus-worker', 'strong-worker', 'helper')
READ_ONLY = ('analyst', 'test-runner', 'consultant')
ROLES = ('owner', 'pm', 'coordinator', 'deep-worker', 'lander', 'docs-writer') + \
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
    'set-other': ('medium', CPO, {}),       # the set fields no design row names
    'set-widen-collision': ('medium', CPO, {}),
    'pull': ('low', CPO + ('deep-worker',) + BUILDERS, {}),
    'pull-collision': ('medium', CPO, {}),
    'pull-held': ('refused', (), {}),
    'release': ('low', CPO, OWN),
    'block': ('low', CPO, OWN),
    'block-owner': ('medium', CPO, {}),
    'unblock-owner': ('medium', TOP, {}),
    'set-status': ('medium', CPO, {}),
    'close': ('medium', CPO, {'lander': 'via-land'}),
    'close-held': ('high', TOP, {}),
    'retire-priced': ('medium', TOP, {}),
    'retire': ('medium', CPO, {}),
    'delete': ('medium', CPO, {}),
    'clear-review': ('medium', CPO, {}),
    'retarget': ('high', TOP, {}),
    'catch-up': ('medium', CPO, {}),
    'raw-write': ('high', (), {}),
}
VERBS = {'next', 'show', 'list', 'check', 'stale', 'landable', 'find', 'add', 'set', 'block', 
         'unblock',
         'pull', 'release', 'close', 'retire', 'delete', 'clear-review', 'set-status', 'unpark',
         'retarget', 'migrate', 'catch-up'}
# `uncite --apply` takes the retarget row; roles.py's row names it, so the owner and the PM may apply it.
# The flag stays so a roles.py whose row lacks it still fails closed (no role may apply it).
UNCITE_ROW = 'uncite' in R.ACTIONS['retarget'].verbs
if UNCITE_ROW:
    VERBS.add('uncite')


class RolesTest(unittest.TestCase):
    def test_every_role_in_the_design_is_a_role(self):
        self.assertEqual(sorted(R.ROLES), sorted(ROLES))
        self.assertEqual((sorted(R.BUILDERS), sorted(R.READ_ONLY), sorted(R.DOCS), R.LANDERS),
                         (sorted(BUILDERS + ('deep-worker',)), sorted(READ_ONLY), ['docs-writer'],
                          ('lander',)))
        self.assertEqual(sorted(R.LEADS), sorted(CPO))
        self.assertEqual(sorted(R.SESSION_ONLY), ['consultant', 'coordinator', 'deep-worker', 'owner', 'pm'])
        self.assertIn('kitlib/backlog/roles.py', schema.OWNER_PATHS)

    def test_the_groups_are_read_from_the_role_files(self):
        """decision 17: every role file's role is in the group its mode (or `group:`) gives, and in
        no other; no name but the owner's is in a group without a role file (an old name is)."""
        roster = {r.name: r for r in content.roster(R.KIT)}
        groups = {'lead': R.LEADS, 'builder': R.BUILDERS, 'docs': R.DOCS, 'read-only': R.READ_ONLY,
                  'lander': R.LANDERS}
        for name, entry in roster.items():
            with self.subTest(role=name):
                self.assertEqual([g for g, names in groups.items() if name in names], [entry.group])
                self.assertEqual(name in R.SESSION_ONLY, entry.run == 'session')
        known = set(roster) | set(content.ALIASES) | {R.OWNER}
        self.assertLessEqual(set(R.ROLES), known)
        self.assertIn(R.PM, roster)
        # test-runner's mode is `test`: read-only for the backlog, as the role policy holds its checkout
        self.assertEqual(roster['test-runner'].mode, 'test')
        self.assertIn('test-runner', R.READ_ONLY)

    def test_a_role_file_adds_a_role_to_its_group(self):
        """One file and nothing else: a new write role is a builder, a docs one a docs role."""
        top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, top, True)
        roles = os.path.join(top, 'content', 'roles')
        os.makedirs(roles)
        head = '---\nname: %s\ndescription: d\ntier: light\nsandbox: read-only\ncapabilities: [read]\n'
        for name, extra in (('maker', 'mode: write\n'), ('scribe', 'mode: write\ngroup: docs\n'),
                            ('boss', 'mode: controller\nrun: session\n'), ('plain', '')):
            with open(os.path.join(roles, name + '.md'), 'w') as f:
                f.write(head % name + extra + '---\nbody\n')
        got = {r.name: (r.group, r.run) for r in content.roster(top)}
        self.assertEqual(got, {'boss': ('lead', 'session'), 'maker': ('builder', None),
                               'plain': (None, None), 'scribe': ('docs', None)})

    def reloaded(self, entries):
        """kitlib.backlog.roles as it reads with `entries` for the kit's roster; the real one after."""
        self.addCleanup(importlib.reload, R)
        with mock.patch.object(content, 'roster', return_value=tuple(entries)):
            return importlib.reload(R)

    def test_no_readable_role_file_lists_the_owner_alone(self):
        """Fails closed: with no role file read, every other role is unlisted (UNLISTED_MAY only)."""
        r = self.reloaded(())
        self.assertEqual(r.ROLES, ('owner',))
        self.assertEqual(r.role_of('pm', False), None)
        self.assertEqual(r.role_of('worker', True), None)
        self.assertTrue(r.allows('retarget', r.role_of('owner', False)))
        self.assertFalse(r.allows('add', r.role_of('worker', True)))

    def test_a_top_role_stays_session_only_whatever_its_file_says(self):
        """pm's file losing `run: session` does not hand a subagent named pm the TOP rows."""
        entries = [e._replace(run=None) if e.name == 'pm' else e for e in content.roster(R.KIT)]
        r = self.reloaded(entries)
        self.assertIn('pm', r.SESSION_ONLY)
        self.assertEqual(r.role_of('pm', True), None)
        self.assertEqual(r.role_of('pm', False), 'pm')

    def test_every_design_row_and_backlog_verb_is_present(self):
        self.assertEqual(set(R.ACTIONS), set(DESIGN))
        self.assertEqual({v for row in R.ACTIONS.values() for v in row.verbs}, VERBS)
        for action, (risk, allowed, when) in DESIGN.items():
            with self.subTest(action):
                row = R.ACTIONS[action]
                self.assertEqual((row.risk, sorted(row.allowed)), (risk, sorted(allowed)))
                self.assertEqual(sorted(row.when), sorted(when.items()))

    @unittest.skipUnless(UNCITE_ROW, "roles.py's retarget row does not name uncite yet (owner approval)")
    def test_uncite_is_the_retarget_rows_and_no_other(self):
        self.assertEqual(R.ACTIONS['retarget'].verbs, ('retarget', 'migrate', 'uncite'))
        self.assertEqual([n for n, row in R.ACTIONS.items() if 'uncite' in row.verbs], ['retarget'])

    def test_no_unknown_role_fact_risk_or_row_is_named(self):
        for action, row in R.ACTIONS.items():
            with self.subTest(action):
                self.assertLessEqual(set(row.allowed) | {r for r, _ in row.when}, set(R.ROLES))
                self.assertLessEqual({f for _, f in row.when}, set(R.FACTS))
                self.assertIn(row.risk, R.RISKS)
                self.assertLessEqual(set(row.over), set(R.ACTIONS) - {action})
        self.assertLessEqual(set(R.SESSION_ONLY), set(R.ROLES))

    def test_builders_never_close_and_only_the_leads_set_other_fields(self):
        for action in ('close', 'close-held', 'delete'):
            for role in R.BUILDERS:
                self.assertFalse(R.allows(action, role, R.FACTS), (action, role))
        for role in R.ROLES:
            self.assertEqual(R.allows('set-other', role, R.FACTS), role in CPO, role)

    def test_a_lead_deletes_and_clears_a_review_and_a_held_record_takes_the_top_gate(self):
        """Delete and clear-review are the leads' (medium, audited); deleting a held record
        takes close-held, the TOP gate that closing and retiring one take."""
        held = R.ACTIONS['close-held']
        self.assertEqual((set(held.verbs), set(held.over)), ({'close', 'retire', 'delete'},) * 2)
        self.assertIn(held.risk, R.AUDITED)
        for action in ('delete', 'clear-review'):
            self.assertIn(R.ACTIONS[action].risk, R.AUDITED, action)
            for role in R.ROLES:
                self.assertEqual(R.allows(action, role, R.FACTS), role in CPO, (action, role))
        for role in R.ROLES:
            self.assertEqual(R.allows('close-held', role, R.FACTS), role in TOP, role)

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
        # the -1 hold is retired: no HOLD value or set-hold row, and -1 is no price band
        self.assertFalse(hasattr(R, 'HOLD'))
        self.assertNotIn(record.RETIRED_PRIORITY, R.PRICE_LOW + R.PRICE_TOP)
        self.assertEqual(sorted(R.PRICE_LOW + R.PRICE_TOP), ['1', '2', '3', '4', '5'])
        for band in R.PRICE_LOW + R.PRICE_TOP:
            self.assertTrue(record.is_band(band), band)
        for field in R.TEXT_FIELDS:
            self.assertTrue(field == 'body' or field in record.FIELDS, field)


def _rec(**over):
    return parse(**over)


def _price(priority, by=None):
    """policy.sort's `given` for `set ID --priority <priority> [--priority-by <by>]`."""
    changes = [('priority', [priority])] + ([('priority_by', [by])] if by is not None else [])
    return policy.given_of_set(changes)


UNPRICED = dict(priority='-', priority_by='-')
OWNER_BLOCKED = dict(state='blocked', blocked_on='owner', owner_ask='which one?')


class CoordinatorPrice(unittest.TestCase):
    """The owner's rule (KIT-COORD-FOLLOWUP-PRICE-MISMATCH): a coordinator, like the PM, sets a
    first price of 2-5 with `priority_by: agent` on an unpriced record; a price of 1, a
    reprice of an owner's or the PM's price, a `priority_by` of owner or pm, and a price on a
    record blocked on the owner stay the owner's and the PM's. The rows are sorted as
    tools/backlog.py's check sorts them (policy.sort over the record it reads) and judged by
    roles.allows."""

    def refusal(self, role, given, rec):
        return policy.refusal(role, policy.sort('set', given, rec))

    def test_a_coordinator_sets_a_first_agent_price_of_2_to_5_on_an_unpriced_record(self):
        for band in R.PRICE_LOW:
            with self.subTest(band=band):
                rows = policy.sort('set', _price(band, 'agent'), _rec(**UNPRICED))
                self.assertEqual(rows, ['set-price', 'set-other'])
                self.assertIsNone(policy.refusal('coordinator', rows))

    def test_a_coordinator_may_not_set_a_price_of_1(self):
        text = self.refusal('coordinator', _price('1', 'agent'), _rec(**UNPRICED))
        self.assertIn('`set-price-owner` (risk medium) is for owner, pm', text)

    def test_a_coordinator_may_not_reprice_an_owner_or_pm_price(self):
        """The three logged refusals (docs/refusals.md in the PM repo, 2026-10-24 and -25) were
        this case: `--priority N --priority-by agent` on a record the PM had already priced."""
        for by in TOP:
            for band in R.PRICE_LOW:
                with self.subTest(by=by, band=band):
                    rec = _rec(priority='3', priority_by=by)
                    for given in (_price(band, 'agent'), _price(band), _price('-')):
                        text = self.refusal('coordinator', given, rec)
                        self.assertIn('`set-price-owner` (risk medium)', text)

    def test_a_coordinator_may_not_price_as_the_owner_or_the_pm(self):
        for by in TOP:
            with self.subTest(by=by):
                text = self.refusal('coordinator', _price('3', by), _rec(**UNPRICED))
                self.assertIn('`set-price-owner` (risk medium)', text)

    def test_a_coordinator_may_not_price_a_record_blocked_on_the_owner(self):
        for rec in (_rec(**dict(UNPRICED, **OWNER_BLOCKED)),
                    _rec(priority='3', priority_by='agent', **OWNER_BLOCKED)):
            with self.subTest(priority=rec.get('priority')):
                text = self.refusal('coordinator', _price('3', 'agent'), rec)
                self.assertIn('`set-price-owner` (risk medium)', text)

    def test_a_coordinator_reprices_an_agent_price(self):
        """An agent price is set-price, the leads' row, whoever set it: `priority_by: agent`
        does not say which session priced, so a coordinator may move it within 2-5 (with or
        without `--priority-by agent`) or clear it, but never to 1."""
        rec = _rec(priority='3', priority_by='agent')
        for given in (_price('4', 'agent'), _price('5'), _price('-')):
            self.assertIsNone(self.refusal('coordinator', given, rec), given)
        self.assertIn('`set-price-owner`', self.refusal('coordinator', _price('1', 'agent'), rec))

    def test_the_pm_and_the_owner_keep_every_price_and_builders_take_none(self):
        recs = (_rec(**UNPRICED), _rec(priority='3', priority_by='agent'),
                _rec(priority='3', priority_by='pm'), _rec(priority='3', priority_by='owner'),
                _rec(**dict(UNPRICED, **OWNER_BLOCKED)))
        for rec in recs:
            # a bare `--priority 4` on an agent price is set-price alone, no set-other beside it
            for given in (_price('1', 'pm'), _price('3', 'owner'), _price('3', 'agent'),
                          _price('4'), _price('-')):
                for role in R.TOP:
                    self.assertIsNone(self.refusal(role, given, rec), (role, given))
                for role in R.BUILDERS + R.DOCS + R.READ_ONLY + R.LANDERS:
                    self.assertIsNotNone(self.refusal(role, given, rec), (role, given))
                    self.assertIsNotNone(policy.refusal(role, policy.sort('set', given, rec),
                                                        R.FACTS), (role, given))


class CoordinatorPriceCli(unittest.TestCase):
    """The same rule through tools/backlog.py, run with a coordinator's session mark."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True, prepare=kit_json_ref,
                             prepare_key='test_cli.Cli.kit_json_ref')
        self.addCleanup(self.repo.cleanup)
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        self.env = fixture_env({'USERPROFILE': home, 'HOME': home})
        self.env.pop('PM_KIT_ROLE', None)

    def run_tool(self, mark, *args):
        env = dict(self.env)
        if mark is not None:
            env['PM_KIT_ROLE'] = mark
        return inproc.run_main(_backlog().main, ['-C', self.repo.root] + list(args), env,
                               self.repo.root)

    def ok(self, mark, *args):
        rc, out, err = self.run_tool(mark, *args)
        self.assertEqual(rc, 0, out + err)
        return out + err

    def add(self, rid):
        self.ok(None, 'add', rid, '--summary', 'a thing to do', '--scope', 'tools/%s.py' % rid.lower(),
                *ADD_FLAGS)

    def test_a_coordinator_prices_an_unpriced_record_once_and_never_over_the_pm(self):
        self.add('FOO')
        self.add('BAR')
        self.ok('coordinator', 'set', 'FOO', '--priority', '3', '--priority-by', 'agent')
        out = self.ok(None, 'show', 'FOO')
        self.assertIn('priority: 3\n', out)
        self.assertIn('priority_by: agent\n', out)
        self.ok(None, 'set', 'BAR', '--priority', '2', '--priority-by', 'pm')
        rc, out, err = self.run_tool('coordinator', 'set', 'BAR', '--priority', '4',
                                     '--priority-by', 'agent')
        self.assertEqual(rc, 2, out + err)
        self.assertIn('REFUSED: the backlog action `set-price-owner` (risk medium) is for owner, pm; '
                      "this session's role is coordinator", out + err)
        self.assertIn('priority: 2\n', self.ok(None, 'show', 'BAR'))


def _backlog():
    return inproc.load_tool('backlog')


if __name__ == '__main__':
    unittest.main()
