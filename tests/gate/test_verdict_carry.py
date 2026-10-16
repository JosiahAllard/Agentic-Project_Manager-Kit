"""kitlib.gate.verdicts.carry and `tools/verdict.py carry` (docs/gate.md "Carrying a verdict
forward", owner ruling R-2050): a queued verdict moves to the branch's head only when the head
carries it (kitlib.gate.carry, tested in test_carry.py), recording `carried`; anything else writes
nothing. The entry's `carried` record is checked like every other key. On the scratch repository
of tests/gate/fixtures.py GateRepoCase."""
import contextlib
import datetime
import io
import unittest

from kitlib.gate import carry
from kitlib.gate import verdicts as V
from tests.gate import test_verdict_cli
from tests.gate.test_carry import CarryCase
from tests.gate.test_verdicts import given

BRANCH = 'kit/feature'
NOW = datetime.datetime(2026, 10, 16, 12, 0, 0, tzinfo=datetime.timezone.utc)


class QueueCarryCase(CarryCase):
    def setUp(self):
        CarryCase.setUp(self)
        self.common = self.repo.common_dir()

    def queue(self, tip, **over):
        return V.add(self.common, given(BRANCH, tip, base=self.main[:10], **over))[1]

    def on_disk(self):
        good, bad = V.scan(self.common)
        self.assertEqual(bad, [])
        return good

    def carry(self, check=None):
        return V.carry(self.common, self.repo.root, BRANCH, self.main, now=NOW, checker=check)

    def synced(self):
        """kit/feature reviewed and queued, then main moved and was merged in: (wt, reviewed,
        the queued entry, the merge)."""
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        queued = self.queue(reviewed)
        self.on_main({'m.py': 'm\n'})
        return wt, reviewed, queued, self.sync(wt)


class Carry(QueueCarryCase):
    def test_a_carry_moves_the_entry_to_the_head_and_records_it(self):
        _, reviewed, queued, tip = self.synced()
        path, stored = self.carry()
        self.assertEqual(path, V.entry_path(self.common, BRANCH))
        self.assertEqual(stored['tip'], tip)
        c = stored['carried']
        self.assertEqual({k: c[k] for k in ('from', 'merges', 'main', 'utc')},
                         {'from': reviewed, 'merges': [tip], 'main': self.main,
                          'utc': '2026-10-16T12:00:00Z'})
        self.assertRegex(c['diff'], r'^[0-9a-f]{64}$')
        # the review itself is untouched: who gave it, when it was queued, the range reviewed
        self.assertEqual({k: v for k, v in stored.items() if k not in ('tip', 'carried')},
                         {k: v for k, v in queued.items() if k != 'tip'})
        self.assertEqual(self.on_disk(), [stored])
        self.assertIsNone(V.check(stored))

    def test_a_second_carry_keeps_the_commit_first_reviewed(self):
        wt, reviewed, _, first = self.synced()
        self.carry()
        self.on_main({'n.py': 'n\n'})
        tip = self.sync(wt)
        _, stored = self.carry()
        self.assertEqual((stored['tip'], stored['carried']['from'], stored['carried']['merges']),
                         (tip, reviewed, [first, tip]))

    def test_a_head_that_does_not_carry_the_verdict_writes_nothing(self):
        wt, reviewed, queued, _ = self.synced()
        own = self.more(wt, {'b.py': 'b\n'})
        with self.assertRaises(V.CarryRefused) as cm:
            self.carry()
        self.assertIn('%s at %s does not carry the verdict given at %s' % (
            BRANCH, own[:10], reviewed[:10]), str(cm.exception))
        self.assertIn('not a merge', str(cm.exception))
        self.assertEqual(self.on_disk(), [queued])

    def test_nothing_queued_is_none(self):
        wt, _ = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        self.sync(wt)
        self.assertIsNone(self.carry())
        self.assertEqual(self.on_disk(), [])

    def test_an_entry_marked_failed_is_not_carried(self):
        _, reviewed, _, _ = self.synced()
        V.mark_failed(self.common, BRANCH, 'red gate')
        with self.assertRaises(V.CarryRefused) as cm:
            self.carry()
        self.assertIn('marked failed (red gate)', str(cm.exception))
        self.assertEqual([e['tip'] for e in self.on_disk()], [reviewed])

    def test_an_entry_at_the_head_has_nothing_to_carry(self):
        _, reviewed = self.branch('feature', {'a.py': 'a\n'})
        queued = self.queue(reviewed)
        with self.assertRaises(V.CarryRefused) as cm:
            self.carry()
        self.assertIn('queued at its head %s already: nothing to carry' % reviewed[:10],
                      str(cm.exception))
        self.assertEqual(self.on_disk(), [queued])

    def test_a_re_add_drops_the_carried_record(self):
        _, _, _, tip = self.synced()
        self.carry()
        _, stored = V.add(self.common, given(BRANCH, tip, base=self.main[:10]))
        self.assertNotIn('carried', stored)
        self.assertEqual(self.on_disk(), [stored])

    def test_an_entry_replaced_during_the_check_is_not_written(self):
        _, _, _, tip = self.synced()
        readded = []

        def check(root, reviewed, head, base):
            readded.append(self.queue(tip, reviewer_id='agent-r2', range='%s..%s' % (
                self.main[:10], tip[:10])))
            return carry.check(root, reviewed, head, base)
        with self.assertRaises(V.VerdictError) as cm:
            self.carry(check=check)
        self.assertIn('changed while its carry was checked', str(cm.exception))
        self.assertEqual(self.on_disk(), readded)

    def test_a_branch_that_moves_during_the_check_is_not_written(self):
        wt, _, queued, _ = self.synced()

        def check(root, reviewed, head, base):
            facts = carry.check(root, reviewed, head, base)
            self.more(wt, {'late.py': 'late\n'})
            return facts
        with self.assertRaises(V.VerdictError) as cm:
            self.carry(check=check)
        self.assertIn('moved while its carry was checked', str(cm.exception))
        self.assertEqual(self.on_disk(), [queued])

    def test_facts_for_other_commits_are_refused(self):
        _, reviewed, queued, tip = self.synced()
        good, _ = carry.check(self.repo.root, reviewed, tip, self.main)
        for bad in (dict(good, main=reviewed), dict(good, merges=[reviewed]),
                    dict(good, diff='x')):
            with self.assertRaises(V.CarryRefused):
                self.carry(check=lambda *a, bad=bad: (bad, None))
        self.assertEqual(self.on_disk(), [queued])


class TheCarriedRecord(QueueCarryCase):
    def carried_entry(self):
        _, reviewed, _, tip = self.synced()
        _, stored = self.carry()
        return reviewed, tip, stored

    def test_check_takes_a_well_formed_carried_record_only(self):
        reviewed, tip, stored = self.carried_entry()
        c = stored['carried']
        for over, why in (({'from': tip}, 'carried.from is the commit reviewed'),
                          ({'from': 'abc'}, 'carried.from is a full commit hash'),
                          ({'main': None}, 'carried.main is a full commit hash'),
                          ({'merges': []}, 'carried.merges is the main merges'),
                          ({'merges': [reviewed]}, 'carried.merges is the main merges'),
                          ({'merges': [tip[:10]]}, 'carried.merges is the main merges'),
                          ({'diff': 'x' * 64}, 'carried.diff is a sha256'),
                          ({'utc': '2026-10-16'}, 'carried.utc is YYYY-MM-DDTHH:MM:SSZ')):
            with self.subTest(over=over):
                self.assertIn(why, V.check(dict(stored, carried=dict(c, **over))))
        self.assertIn('carried is an object of', V.check(dict(stored, carried=dict(c, more=1))))
        self.assertIn('carried is an object of', V.check(dict(stored, carried=[reviewed])))

    def test_the_range_ends_at_the_commit_reviewed(self):
        _, tip, stored = self.carried_entry()
        why = V.check(dict(stored, range='%s..%s' % (self.main[:10], tip[:10])))
        self.assertIn('the reviewed commit the verdict was carried from', why)

    def test_the_coordinator_never_states_a_carried_record(self):
        _, tip, stored = self.carried_entry()
        given_entry = {k: stored[k] for k in V.GIVEN_KEYS}
        self.assertIn('may not set: carried',
                      V.check(dict(given_entry, carried=stored['carried']), stored=False))
        with self.assertRaises(V.BadEntry):
            V.add(self.common, dict(given_entry, carried=stored['carried']))


class CarryCli(QueueCarryCase):
    def setUp(self):
        QueueCarryCase.setUp(self)
        self.tool = test_verdict_cli.load_tool()

    def run_tool(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.tool.main(list(args) + ['-C', self.repo.root])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_carry_prints_carried_and_list_names_the_commit_reviewed(self):
        _, reviewed, _, tip = self.synced()
        code, out, err = self.run_tool('carry', '--branch', BRANCH)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(out.strip(), "CARRIED %s %s from reviewed %s: 1 main merge, no conflict, "
                                      "the branch's own diff unchanged against origin/main %s; "
                                      "LAND by reviewer/opus (agent-r1)"
                         % (BRANCH, tip, reviewed, self.main[:10]))
        code, out, _ = self.run_tool('list')
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r'^QUEUED %s %s .* carried_from=%s$' % (BRANCH, tip, reviewed))

    def test_a_refused_carry_exits_1_and_writes_nothing(self):
        wt, _, queued, _ = self.synced()
        self.more(wt, {'b.py': 'b\n'})
        code, out, _ = self.run_tool('carry', '--branch', BRANCH)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith('NOT CARRIED: '), out)
        self.assertIn('review and queue the head instead', out)
        self.assertEqual(self.on_disk(), [queued])

    def test_nothing_queued_exits_1(self):
        code, out, _ = self.run_tool('carry', '--branch', BRANCH)
        self.assertEqual((code, out.strip()), (1, 'NOT QUEUED: %s' % BRANCH))

    def test_no_fetched_main_exits_1(self):
        _, _, queued, _ = self.synced()
        self.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        code, out, _ = self.run_tool('carry', '--branch', BRANCH)
        self.assertEqual(code, 1)
        self.assertIn('NOT CARRIED: main as fetched (origin/main) cannot be read', out)
        self.assertIn('fetch first', out)
        self.assertEqual(self.on_disk(), [queued])

    def test_carry_names_its_branch(self):
        code, _, err = self.run_tool('carry')
        self.assertEqual(code, 2)
        self.assertIn('USAGE:', err)


if __name__ == '__main__':
    unittest.main()
