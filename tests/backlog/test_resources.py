"""Resource capacity (docs/backlog.md "Resources"): `.kit.json` backlog.resources, a record's
`uses:` lines, and the one count inside offer.next_offer that `next`, `pull`, `show`, `check` and
`set` share."""
import json
import unittest

from kitlib.backlog import check as checkmod
from kitlib.backlog import offer as omod
from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Failure, Refusal, Usage
from kitlib.config import schema as schemamod  # noqa: F401  (the key is checked on load below)

from tests import inproc
from tests.backlog import test_cli
from tests.backlog.helpers import (ADD_FLAGS, BRANCH, TODAY, StoreCase, kit_json_ref, messages,
                                   priced, record_text)

HOLDER = 'kit/holder'
OTHER = 'kit/other'
SESSION = 'kit'


def mine(rid, priority='2', **over):
    over.setdefault('session', SESSION)
    return priced(rid, priority, **over)


def held(rid, uses='dev-clone', by=HOLDER, **over):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=by, uses=uses, **over)


class Base(StoreCase):
    remote = False
    resources = {'dev-clone': 1}

    def setUp(self):
        super().setUp()
        self.repo.git('branch', HOLDER)
        self.repo.git('branch', OTHER)
        self.store = smod.Store(self.backend, self.rules, session=SESSION)
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY, cap=5,
                                resources=dict(self.resources))

    def offer(self, cap=5, branch='main', seeds=()):
        snap = self.store.load()
        offers, passed, _holding, _own = pmod.offer_for(snap, self.ctx, branch=branch, cap=cap,
                                                        seeds=seeds)
        return [o.rec.id for o in offers], {p.rec.id: p for p in passed}


class Offer(Base):

    def test_capacity_one_is_passed_over_with_its_reason_and_blocker(self):
        self.seed(held('X'), mine('A', uses='dev-clone'), mine('B'))
        ids, passed = self.offer()
        self.assertEqual(ids, ['B'])
        self.assertEqual(passed['A'].why, 'resource dev-clone at capacity 1, taken by X '
                                          '(in flight on %s)' % HOLDER)
        self.assertEqual(omod.blocker_summary(passed.values()), ['blocks 1: %s (X)' % HOLDER])

    def test_capacity_two_takes_a_second_and_refuses_a_third(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY,
                                resources={'dev-clone': 2})
        self.seed(held('X'), mine('A', uses='dev-clone'), mine('B', uses='dev-clone'))
        ids, passed = self.offer()
        self.assertEqual(ids, ['A'])
        self.assertIn('at capacity 2, taken by X (in flight on %s), A (offered above)' % HOLDER,
                      passed['B'].why)

    def test_a_branchs_own_pulls_take_no_capacity_in_its_own_offer_but_fill_it_for_others(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY,
                                resources={'dev-clone': 2})
        self.seed(held('X', by=HOLDER), held('Y', by=HOLDER), mine('A', uses='dev-clone'))
        ids, passed = self.offer(branch=OTHER)
        self.assertEqual(ids, [])
        self.assertIn('at capacity 2', passed['A'].why)
        ids, _passed = self.offer(branch=HOLDER)
        self.assertEqual(ids, ['A'])

    def test_a_capped_offer_is_not_filled_by_one_resource(self):
        self.seed(mine('A', '1', uses='dev-clone'), mine('B', '1', uses='dev-clone'),
                  mine('C', '1', uses='dev-clone'), mine('D', '2'))
        ids, passed = self.offer(cap=2)
        self.assertEqual(ids, ['A', 'D'])
        self.assertIn('taken by A (offered above)', passed['B'].why)
        self.assertIsNone(passed['B'].blocker)

    def test_another_sessions_record_takes_the_slot_in_rank_order(self):
        self.seed(priced('Z', '1', session='other', uses='dev-clone'),
                  mine('A', '2', uses='dev-clone'))
        ids, passed = self.offer()
        self.assertEqual(ids, [])
        self.assertIn('taken by Z (session other)', passed['A'].why)
        self.assertEqual(passed['A'].blocker, ('session other', 'Z'))

    def test_the_own_branchs_live_pull_takes_no_capacity_in_its_offer(self):
        self.seed(held('X', by=BRANCH), mine('A', uses='dev-clone'))
        self.assertEqual(self.offer(branch=BRANCH)[0], ['A'])
        self.assertEqual(self.offer(branch='main')[0], [])

    def test_a_dead_pull_counts_nothing(self):
        self.seed(held('X', by='kit/gone'), mine('A', uses='dev-clone'))
        self.assertEqual(self.offer()[0], ['A'])

    def test_blocked_and_parked_records_count_nothing(self):
        self.seed(priced('X', state='blocked', blocked_on='deliberate', uses='dev-clone'),
                  priced('Y', state='parked', uses='dev-clone'), mine('A', uses='dev-clone'))
        self.assertEqual(self.offer()[0], ['A'])

    def test_a_seed_is_passed_over_at_a_live_pulls_capacity_like_scope(self):
        self.seed(held('X'), mine('A', '5', uses='dev-clone'))
        ids, passed = self.offer(seeds=('A',))
        self.assertEqual(ids, [])
        self.assertIn('at capacity 1', passed['A'].why)

    def test_a_seed_is_not_passed_over_for_the_rest_of_the_offer(self):
        self.seed(mine('A', '1', uses='dev-clone'), mine('B', '1', uses='dev-clone'))
        snap = self.store.load()
        (offers, _passed), _bad, _ranks = omod.next_offer(
            snap, [], seeds=('A', 'B'), session=SESSION, capacity={'dev-clone': 1})
        self.assertEqual([o.rec.id for o in offers], ['A', 'B'])

    def test_an_undeclared_name_is_passed_over_and_never_crashes(self):
        self.seed(mine('A', uses='nope'), mine('B'))
        ids, passed = self.offer()
        self.assertEqual(ids, ['B'])
        self.assertEqual(passed['A'].why, 'uses nope, which .kit.json does not declare')

    def test_a_live_pull_with_an_undeclared_name_counts_nothing(self):
        self.seed(held('X', uses='nope'), mine('A', uses='dev-clone'))
        self.assertEqual(self.offer()[0], ['A'])

    def test_a_passed_over_record_stays_out_of_the_cap_and_takes_no_capacity(self):
        self.seed(held('X'), mine('A', '1', uses='dev-clone'), mine('B', '2'), mine('C', '3'))
        self.assertEqual(self.offer(cap=2)[0], ['B', 'C'])

    def test_next_all_is_held_to_capacity_too(self):
        self.seed(mine('A', uses='dev-clone'), mine('B', uses='dev-clone'))
        self.assertEqual(self.offer(cap=None)[0], ['A'])


class SeveralResources(Base):
    resources = {'dev-clone': 1, 'gpu': 1}

    def test_one_full_and_one_free_is_passed_over(self):
        self.seed(held('X', uses='gpu'), mine('A', uses=['dev-clone', 'gpu']))
        ids, passed = self.offer()
        self.assertEqual(ids, [])
        self.assertIn('resource gpu at capacity 1', passed['A'].why)

    def test_both_free_is_offered_and_takes_one_unit_of_each(self):
        self.seed(mine('A', '1', uses=['dev-clone', 'gpu']), mine('B', '2', uses='dev-clone'),
                  mine('C', '2', uses='gpu'), mine('D', '3'))
        ids, passed = self.offer()
        self.assertEqual(ids, ['A', 'D'])
        self.assertIn('resource dev-clone', passed['B'].why)
        self.assertIn('resource gpu', passed['C'].why)

    def test_a_name_written_twice_is_one_unit(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY,
                                resources={'dev-clone': 2})
        self.seed(mine('A', '1', uses=['dev-clone', 'dev-clone']),
                  mine('B', '2', uses='dev-clone'))
        self.assertEqual(self.offer()[0], ['A', 'B'])


class Pull(Base):

    def test_at_capacity_with_no_role_and_a_reason_is_refused(self):
        self.seed(held('X'), mine('A', uses='dev-clone'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'A', reason='the owner wants it today')
        msg = str(cm.exception)
        self.assertIn('resource dev-clone at capacity 1, taken by X', msg)
        self.assertIn('`--reason` alone never overrides it', msg)
        self.assertIn('`pull-collision`', msg)
        self.assertEqual(self.get('A').get('state'), 'open')

    def test_at_capacity_with_a_role_and_no_reason_is_refused_as_a_collision(self):
        self.seed(held('X'), mine('A', uses='dev-clone'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'A', may_collide=True)
        self.assertIn('`pull-collision`', str(cm.exception))
        self.assertIn('resource collides with a live pull', str(cm.exception))
        self.assertEqual(self.get('A').get('state'), 'open')

    def test_at_capacity_with_a_role_and_a_reason_takes_the_row(self):
        self.seed(held('X'), mine('A', uses='dev-clone'))
        change = self.verb('pull', 'A', reason='the owner wants it today', may_collide=True)
        self.assertEqual(self.get('A').get('state'), 'pulled')
        self.assertEqual(change.rows, ['pull-collision'])
        self.assertTrue(any(line.startswith('warning: resource dev-clone')
                            for line in change.lines), change.lines)

    def test_a_pull_with_room_is_plain(self):
        self.seed(mine('A', uses='dev-clone'))
        change = self.verb('pull', 'A')
        self.assertEqual(self.get('A').get('state'), 'pulled')
        self.assertFalse(getattr(change, 'rows', None))

    def test_a_slot_taken_only_by_an_offered_record_needs_only_a_reason(self):
        self.seed(mine('A', '1', uses='dev-clone'), mine('B', '2', uses='dev-clone'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'B')
        self.assertIn("is not in `next`'s offer", str(cm.exception))
        change = self.verb('pull', 'B', reason='needed before the others')
        self.assertFalse(getattr(change, 'rows', None))

    def test_a_dead_pull_takeover_at_capacity_is_the_row(self):
        self.seed(held('X'), held('D', by='kit/gone', session=SESSION))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'D')
        self.assertIn('resource collides with a live pull', str(cm.exception))
        change = self.verb('pull', 'D', may_collide=True)
        self.assertEqual(change.rows, ['pull-collision'])


class SetUses(Base):

    def test_clearing_the_list_frees_the_slot(self):
        self.seed(held('X'), mine('A', uses='dev-clone'))
        self.assertEqual(self.offer()[0], [])
        change = self.verb('set', 'X', [('uses', ['-'])])
        self.assertEqual(self.get('X').all('uses'), [])
        self.assertFalse(getattr(change, 'rows', None))
        self.assertEqual(self.offer()[0], ['A'])

    def test_the_list_is_replaced_whole(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY,
                                resources={'dev-clone': 1, 'gpu': 1})
        self.seed(mine('A', uses=['dev-clone', 'gpu']))
        self.verb('set', 'A', [('uses', ['gpu'])])
        self.assertEqual(self.get('A').all('uses'), ['gpu'])

    def test_a_dash_beside_a_name_and_a_bad_name_are_usage(self):
        self.seed(mine('A'))
        for vals in (['-', 'dev-clone'], ['Dev_Clone']):
            with self.subTest(vals=vals), self.assertRaises(Usage):
                self.verb('set', 'A', [('uses', vals)])

    def test_an_undeclared_name_is_refused(self):
        self.seed(mine('A'))
        with self.assertRaises(Usage) as cm:
            self.verb('set', 'A', [('uses', ['nope'])])
        self.assertIn('declared: dev-clone', str(cm.exception))

    def test_widening_a_pulled_record_into_a_full_resource_is_the_widen_row(self):
        self.seed(held('X'), held('Y', uses=[], by=OTHER))
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'Y', [('uses', ['dev-clone'])])
        self.assertIn('`set-widen-collision`', str(cm.exception))
        self.assertIn('--uses <name>', str(cm.exception))
        self.assertEqual(self.get('Y').all('uses'), [])
        change = self.verb('set', 'Y', [('uses', ['dev-clone'])], may_collide=True)
        self.assertEqual(change.rows, ['set-widen-collision'])

    def test_an_open_record_may_name_a_full_resource(self):
        self.seed(held('X'), mine('A'))
        self.verb('set', 'A', [('uses', ['dev-clone'])])
        self.assertEqual(self.get('A').all('uses'), ['dev-clone'])

    def test_add_writes_the_lines_and_refuses_an_undeclared_name(self):
        args = ('NEW', 'a new record to add', ['area/new/**'])
        kw = dict(found_on='none', why_not_folded='found outside any branch at all')
        with self.assertRaises(Usage):
            self.verb('add', *args, uses=['nope'], **kw)
        self.verb('add', *args, uses=['dev-clone', 'dev-clone'], **kw)
        self.assertEqual(self.get('NEW').all('uses'), ['dev-clone'])


class Load(Base):

    def test_show_line_needs_no_offer(self):
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY,
                                resources={'dev-clone': 1, 'gpu': 2})
        self.seed(held('X'), mine('A', uses=['dev-clone', 'gpu', 'nope']))
        snap = self.store.load()
        self.assertEqual(pmod.uses_shown(snap, self.ctx, snap.get('A')),
                         'uses dev-clone (1/1: X on %s), gpu (0/2), nope (not declared in '
                         '.kit.json)' % HOLDER)
        self.seed(mine('B'))
        self.assertIsNone(pmod.uses_shown(self.store.load(), self.ctx,
                                          self.store.load().get('B')))

    def test_show_says_when_the_capacity_cannot_be_read(self):
        def broken():
            raise Failure('bad file')
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, resources=broken)
        self.seed(mine('A', uses='dev-clone'))
        snap = self.store.load()
        self.assertEqual(pmod.uses_shown(snap, self.ctx, snap.get('A')),
                         'uses dev-clone (capacity not read: bad file)')

    def test_a_store_with_no_uses_line_never_reads_the_key(self):
        def broken():
            raise Failure('must not be read')
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, resources=broken)
        self.seed(mine('A'))
        self.assertEqual(self.offer()[0], ['A'])

    def test_a_callable_is_read_once_and_only_when_asked(self):
        calls = []
        ctx = pmod.Context(self.store, self.repo.root, resources=lambda: calls.append(1) or {'a': 1})
        self.assertEqual(calls, [])
        self.assertEqual((ctx.resources, ctx.resources), ({'a': 1}, {'a': 1}))
        self.assertEqual(calls, [1])
        self.assertEqual(pmod.Context(self.store, self.repo.root).resources, {})


class Check(Base):

    def test_an_undeclared_name_is_red_and_a_declared_one_is_green(self):
        self.seed(mine('A', uses='nope'), mine('B', uses='dev-clone'))
        snap = self.store.load()
        res = [r for r in checkmod.check_store(snap, resources=self.ctx.capacity(snap))
               if r.name == checkmod.USES_NAMES]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0].red)
        self.assertEqual(len(res[0].findings), 1)
        self.assertIn('`uses: nope`', res[0].findings[0].message)
        self.assertIn('declared: dev-clone', res[0].findings[0].message)
        self.assertEqual(res[0].subject, 2)

    def test_no_uses_line_adds_no_check(self):
        self.seed(mine('A'))
        snap = self.store.load()
        names = [r.name for r in checkmod.check_store(snap, resources=self.ctx.capacity(snap))]
        self.assertNotIn(checkmod.USES_NAMES, names)

    def test_the_name_grammar_is_a_schema_finding(self):
        self.assertFalse(messages(uses='dev-clone'))
        self.assertTrue(any('not a resource name' in m for m in messages(uses='Dev_Clone')))


class Config(StoreCase):
    """opener.repo_resources: the main checkout's `.kit.json`, whatever worktree asks."""

    def write(self, root, resources):
        self.repo.write('.kit.json', json.dumps({'backlog': {'store': 'ref',
                                                             'resources': resources}}) + '\n',
                        root=root)

    def test_no_key_is_no_resources(self):
        self.assertEqual(opener.repo_resources(self.repo.root), {})

    def test_a_worktree_reads_the_main_checkouts_and_a_note_is_no_resource(self):
        wt = self.repo.worktree('wt')
        self.write(self.repo.root, {'dev-clone': 2, '_why': 'one spare clone'})
        self.write(wt, {'other': 1})
        self.assertEqual(opener.repo_resources(wt), {'dev-clone': 2})

    def test_a_bad_value_or_name_is_refused_when_the_file_loads(self):
        for bad in ({'dev-clone': 0}, {'dev-clone': '1'}, {'Dev_Clone': 1}):
            with self.subTest(bad=bad):
                self.write(self.repo.root, bad)
                with self.assertRaises(Failure) as cm:
                    opener.repo_resources(self.repo.root)
                self.assertIn('cannot read the backlog resources', str(cm.exception))


class ThroughTheTool(unittest.TestCase):
    """tools/backlog.py on a ref store: the capacity is the config's alone."""
    setUp_cli = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def setUp(self):
        self.setUp_cli()
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap, {'AAA': record_text(id='AAA', priority='1', priority_by='agent',
                                                  scope='area/a/**', uses='dev-clone'),
                               'BBB': record_text(id='BBB', priority='2', priority_by='agent',
                                                  scope='area/b/**', uses='dev-clone')}, 'seed\n')
        self.resources({'dev-clone': 1})

    def resources(self, value):
        kit_json_ref(self.repo, {'backlog': {'resources': value}})

    def test_raising_a_capacity_in_config_alone_takes_effect(self):
        out = self.ok('next')
        self.assertIn('NEXT (1 offered)', out)
        self.assertIn('passed over BBB (rank 2): resource dev-clone at capacity 1, taken by '
                      'AAA (offered above)', out)
        self.resources({'dev-clone': 2})
        self.assertIn('NEXT (2 offered)', self.ok('next'))

    def test_show_check_and_add(self):
        self.assertIn('uses dev-clone (0/1)', self.ok('show', 'AAA'))
        self.assertIn('uses names', self.ok('check'))
        rc, out = self.run_tool('add', 'NEW', '--summary', 'a new record to add', '--scope',
                                'area/new/**', '--uses', 'dev-clone', *ADD_FLAGS)
        self.assertEqual(rc, 0, out)
        self.assertIn('uses:', out)

    def test_set_clears_the_list_by_dash_and_by_unset(self):
        for clear in (('--uses', '-'), ('--unset', 'uses')):
            with self.subTest(clear=clear):
                self.ok('set', 'AAA', '--uses', 'dev-clone')
                self.ok('set', 'AAA', *clear)
                self.assertNotIn('uses dev-clone', self.ok('show', 'AAA'))

    def test_set_refuses_an_undeclared_name_with_nothing_written(self):
        rc, out = self.run_tool('set', 'AAA', '--uses', 'nope')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('declared: dev-clone', out)

    def test_check_is_red_once_the_name_is_gone(self):
        self.resources({'gpu': 1})
        rc, out, err = inproc.run_main(test_cli.BACKLOG.main, ['-C', self.repo.root, 'check'],
                                       self.env, self.repo.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn('`uses: dev-clone` is not in .kit.json', out)


if __name__ == '__main__':
    unittest.main()
