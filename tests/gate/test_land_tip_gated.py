"""kitlib.gate.land: a branch tip lands only with a full-gate OK record for exactly that commit
(docs/gate.md "Landing", the locked checks): a record a `gate.py --full` pass wrote (`full` true).
A default-mode or --only run, a record from before records said whether the gate was --full, a
record for another commit and one git no longer agrees with are each REFUSED before any merge."""
import os
import re

from kitlib import gate as G
from kitlib.gate import runner
from kitlib.records import gate_ok
from tests.gate import test_land

BRANCH = test_land.BRANCH
COMMAND = re.compile(r'python "([^"]+)" -C "([^"]+)" --full')


class TipCase(test_land.LandCase):
    GATE_TIPS = False       # every tip here is gated (or not) by the test itself

    def gate(self, wt, **kw):
        out = runner.branch_gate(wt, book_kw=self.book_kw,
                                 log_dir=os.path.join(self.tmp, 'own'), **kw)
        self.assertEqual(out.code, G.OK, out.lines)
        return out

    def assert_refused(self, out, branch, tip, wt, *says):
        """REFUSED before any merge: main and the remote unmoved, no merge record, and the first
        line names the tip, each of `says` and the command that gates the tip whole."""
        self.assertEqual(out.code, G.REFUSED, out.lines)
        first = out.lines[0]
        self.assertIn('%s tip %s has no full-gate OK record' % (branch, tip[:10]), first)
        for s in says:
            self.assertIn(s, first)
        self.assertIn('nothing was merged', first)
        m = COMMAND.search(first)
        self.assertIsNotNone(m, first)
        self.assertTrue(m.group(1).endswith(os.path.join('tools', 'gate.py')), first)
        self.assertTrue(os.path.samefile(m.group(2), wt), first)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertIsNone(self.lock().read())
        self.assertEqual([r for r in self.records() if r != tip + '.json'], [])


class Refused(TipCase):
    def test_a_tip_with_no_record_is_refused(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assert_refused(out, BRANCH, tip, wt, 'no gate-OK record for %s' % tip)

    def test_a_default_mode_record_does_not_count(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt)
        rec, why = gate_ok.verify(self.repo.root, tip)
        self.assertIsNone(why)                      # a real pass, only not --full
        out = self.lander(BRANCH, self.main, tip).run()
        self.assert_refused(out, BRANCH, tip, wt, 'without --full')

    def test_a_record_from_before_the_full_key_does_not_count(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt, full=True)
        rec, _ = gate_ok.read(self.repo.root, tip)
        del rec['full']                             # a --full pass written by an older kit
        gate_ok.write(self.repo.root, rec)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assert_refused(out, BRANCH, tip, wt, 'does not say whether the gate ran --full')

    def test_a_record_of_the_commit_before_the_tip_does_not_count(self):
        wt, first = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt, full=True)
        self.repo.write('b.py', 'y = 2\n', root=wt)
        tip = self.repo.commit('more', 'b.py', cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('no gate-OK record for %s' % tip, out.lines[0])
        os.remove(gate_ok.record_path(self.repo.root, first))
        self.assert_refused(out, BRANCH, tip, wt)

    def test_a_record_git_no_longer_agrees_with_does_not_count(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt, full=True)
        rec, _ = gate_ok.read(self.repo.root, tip)
        rec['tree'] = '0' * 40
        gate_ok.write(self.repo.root, rec)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assert_refused(out, BRANCH, tip, wt, 'names tree %s' % ('0' * 40))

    def test_a_batch_with_one_ungated_tip_lands_nothing(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt, full=True)
        wt2, tip2 = self.branch('other', {'o.py': 'o = 1\n'})
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip, tip2]).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('kit/other tip %s has no full-gate OK record' % tip2[:10], out.lines[0])
        self.assertTrue(os.path.samefile(COMMAND.search(out.lines[0]).group(2), wt2))
        os.remove(gate_ok.record_path(self.repo.root, tip))
        self.assert_refused(out, 'kit/other', tip2, wt2)


class Lands(TipCase):
    def test_a_full_gate_record_of_the_tip_lands(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt, full=True)
        self.assertIs(gate_ok.read(self.repo.root, tip)[0]['full'], True, out.lines)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertIs(rec['full'], False)           # the merged gate ran in its default mode

    def test_a_full_landing_records_its_merge_as_full(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt, full=True)
        out = self.lander(BRANCH, self.main, tip, full=True).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIs(gate_ok.read(self.repo.root, out.merge)[0]['full'], True)


class BranchGateRecord(TipCase):
    def test_full_says_whether_the_branch_gate_ran_full(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt)
        self.assertIs(gate_ok.read(self.repo.root, tip)[0]['full'], False)
        self.gate(wt, full=True)
        self.assertIs(gate_ok.read(self.repo.root, tip)[0]['full'], True)
        out = runner.branch_gate(wt, only=['unit'], book_kw=self.book_kw,
                                 log_dir=os.path.join(self.tmp, 'only'))
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.facts['record'])      # --only writes no record at all
        self.assertIs(gate_ok.read(self.repo.root, tip)[0]['full'], True)
