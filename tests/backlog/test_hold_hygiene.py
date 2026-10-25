"""Hold hygiene (every block or hold cites the ruling, question or item it waits on; an uncited
hold is void). `check` warns on a `deliberate` or `owner` hold that cites nothing
(kitlib.backlog.check.check_hold_citations, never red); `block` refuses to write one
(kitlib.backlog.record.block_citation_error, called by tools/backlog.py); a hold on an item that
closed or retired is the `item: targets` WARN (kitlib.backlog.store.dangling_items)."""
import unittest

from kitlib.backlog import check as checkmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod

from tests.backlog import test_cli
from tests.backlog.helpers import ADD_FLAGS, parse

DELIBERATE = dict(state='blocked', blocked_on='deliberate')
NOTE = 'Blocked on deliberate (2026-10-10): R-9092: held until the guard rewrite lands'


def snapshot(*recs):
    return smod.Snapshot(list(recs), None, 'test', rmod.Rules())


def result(snap, name):
    return next(r for r in checkmod.check_store(snap) if r.name == name)


def hold_warnings(*recs):
    return result(snapshot(*recs), checkmod.HOLD_CITATIONS).warnings


class HoldCitations(unittest.TestCase):

    def test_an_uncited_deliberate_hold_is_one_warning_and_not_red(self):
        res = result(snapshot(parse('FOO', id='FOO', **DELIBERATE)), checkmod.HOLD_CITATIONS)
        self.assertEqual(len(res.warnings), 1)
        self.assertIn('`blocked_on: deliberate` cites no', res.warnings[0].message)
        self.assertIn('so the hold is void', res.warnings[0].message)
        self.assertEqual((res.subject, res.findings, res.red), (1, [], False))

    def test_the_block_reason_note_cites_the_hold(self):
        self.assertEqual(hold_warnings(parse('FOO', id='FOO', body=NOTE, **DELIBERATE)), [])

    def test_a_question_or_an_item_in_the_note_cites_it(self):
        for cite in ('Q-1027', 'item:EX-OTHER'):
            body = 'Blocked on deliberate (2026-10-10): waits on %s, until it is answered' % cite
            with self.subTest(cite=cite):
                self.assertEqual(hold_warnings(parse('FOO', id='FOO', body=body, **DELIBERATE)), [])

    def test_an_r_q_or_t_id_in_the_reason_note_cites_it(self):
        # `block` takes no --ruling: the reason text carries the citation.
        for cite in ('R-9647', 'Q-1027', 'T-104'):
            body = ('Blocked on deliberate (2026-10-10): %s: until no live session uses a '
                    '*-conductor stem' % cite)
            with self.subTest(cite=cite):
                self.assertEqual(hold_warnings(parse('FOO', id='FOO', body=body, **DELIBERATE)), [])

    def test_a_q_key_cites_it(self):
        self.assertEqual(hold_warnings(parse('FOO', id='FOO', q='Q-1027', **DELIBERATE)), [])

    def test_hold_ruling_resume_and_ref_are_not_citations(self):
        # hold_ruling released a hold and resume unparked one; ref is where the record came from.
        for key, value in (('hold_ruling', 'R-9561'), ('resume', 'R-9561'), ('ref', 'R-9721')):
            with self.subTest(key=key):
                self.assertEqual(len(hold_warnings(parse('FOO', id='FOO', **dict(DELIBERATE,
                                                                               **{key: value})))), 1)

    def test_an_id_elsewhere_in_the_body_is_not_a_citation(self):
        body = 'Filed as a follow-up of the round-9 review (R-9721).'
        self.assertEqual(len(hold_warnings(parse('FOO', id='FOO', body=body, **DELIBERATE))), 1)

    def test_an_owner_hold_is_cited_by_an_id_in_owner_ask(self):
        cited = parse('FOO', id='FOO', state='blocked', blocked_on='owner',
                      owner_ask='Q-1027: which of the two layouts?')
        bare = parse('BAR', id='BAR', state='blocked', blocked_on='owner',
                     owner_ask='which of the two layouts?')
        warned = hold_warnings(cited, bare)
        self.assertEqual([w.path for w in warned], [bare.path])
        self.assertIn('`blocked_on: owner`', warned[0].message)

    def test_other_blockers_are_not_subjects(self):
        res = result(snapshot(parse('FOO', id='FOO', state='blocked', blocked_on='live-client'),
                              parse('BAR', id='BAR')), checkmod.HOLD_CITATIONS)
        self.assertEqual((res.subject, res.warnings, res.red), (0, [], False))

    def test_each_uncited_member_is_its_own_warning(self):
        r = parse('FOO', id='FOO', state='blocked', blocked_on=['owner', 'deliberate'],
                  owner_ask='which of the two layouts?')
        self.assertEqual(len(hold_warnings(r)), 2)

    def test_a_write_is_not_refused_over_a_hold_warning(self):
        # fail_findings is what store.check_change refuses on: warnings stay out of it.
        self.assertEqual(checkmod.fail_findings(snapshot(parse('FOO', id='FOO', **DELIBERATE))), [])


class ClosedItemHold(unittest.TestCase):

    def test_a_hold_on_an_item_with_no_record_is_the_item_targets_warning(self):
        # A closed, retired or deleted item has no record (record.py: there is no `closed`).
        snap = snapshot(parse('FOO', id='FOO', state='blocked', blocked_on='item:GONE'),
                        parse('BAR', id='BAR', state='blocked', blocked_on='item:FOO'))
        res = result(snap, rmod.ITEM_TARGETS)
        self.assertEqual([w.path for w in res.warnings], ['backlog/FOO.md'])
        self.assertIn('`item:GONE` names no live record', res.warnings[0].message)
        self.assertFalse(res.red)


class BlockCitationError(unittest.TestCase):

    def test_deliberate_or_owner_without_an_id_is_refused(self):
        for members in (['deliberate'], ['owner'], ['live-client', 'deliberate']):
            with self.subTest(members=members):
                err = rmod.block_citation_error(members, 'waiting for the next pass', 'which?')
                self.assertIn('cites nothing it waits on', err)
                self.assertIn('an uncited hold is void', err)
        self.assertIn('--on deliberate', rmod.block_citation_error(['deliberate']))

    def test_an_r_q_or_item_id_in_the_reason_or_the_ask_passes(self):
        for reason, ask in (('R-9092: until the rewrite lands', None),
                            ('R-9647: until no live session uses a *-conductor stem', None),
                            ('until Q-1027 is answered', None),
                            ('until T-104 is done', None),
                            ('A-025: until the owner decides the approval', None),
                            ('until item:EX-OTHER closes', None),
                            (None, 'Q-1027: which of the two?')):
            with self.subTest(reason=reason, ask=ask):
                self.assertIsNone(rmod.block_citation_error(['deliberate', 'owner'], reason, ask))

    def test_other_members_need_no_id(self):
        for m in ('live-client', 'item:BAR', 'tool:x', 'upstream:y', 'phase:L0'):
            with self.subTest(member=m):
                self.assertIsNone(rmod.block_citation_error([m]))

    def test_a_malformed_id_is_not_a_citation(self):
        for reason in ('per R-12 for now', 'per aR-9092 for now', 'per Q-12 for now', 'per A-12 for now',
                       'per aA-025 for now'):
            with self.subTest(reason=reason):
                self.assertIsNotNone(rmod.block_citation_error(['deliberate'], reason))


class BlockCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_an_uncited_deliberate_block_exits_2_and_writes_nothing(self):
        self.ok('add', 'FOO', '--summary', 'a thing to hold for now', '--scope', 'a/foo.py',
                *ADD_FLAGS)
        before = self.repo.git('rev-parse', smod.STORE_REF)
        rc, out = self.run_tool('block', 'FOO', '--on', 'deliberate', '--reason',
                                'waiting for the next pass over the guard')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: `block --on deliberate` cites nothing it waits on', out)
        self.assertEqual(self.repo.git('rev-parse', smod.STORE_REF), before)
        self.ok('block', 'FOO', '--on', 'deliberate', '--reason',
                'R-9647: until no live session uses a *-conductor stem')
        out = self.ok('check')
        self.assertIn('hold citations   1 ok, 0 FAILED', out)
        self.assertNotIn('WARN', out)


if __name__ == '__main__':
    unittest.main()
