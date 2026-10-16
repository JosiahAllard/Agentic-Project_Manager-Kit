"""kitlib.gate.land: a branch tip lands only with a full-gate OK record for exactly that commit
(docs/gate.md "Landing", the locked checks): a record a `gate.py --full` pass wrote (`full` true).
A default-mode or --only run, a record from before records said whether the gate was --full, a
record for another commit and one git no longer agrees with are each REFUSED before any merge.
The refusal says what gates the tip whole (merge the base first, in the kit, when the tip lacks
it); a base gate with no step that is not manual is refused first; the record's gate_config is not
compared."""
import json
import os
import re

from kitlib import gate as G
from kitlib.gate import runner
from kitlib.gate import steps as S
from kitlib.records import gate_ok
from tests.gate import fixtures, test_land

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


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


GATE_WHOLE = 'gate that tip whole first: '
MERGE_FIRST = 'merge origin/main (%s) into %s first'


class GateCommand(TipCase):
    """The refusal's command names the worktree's own tools/gate.py when the repository being
    landed is the kit (its main checkout has the kit's layout, kitlib.content.oncommit.is_kit),
    wherever the running kit came from; any other repository is gated by the running kit's. In
    the kit, a tip that does not contain the base is told to merge it in first: the branch's own
    gate.py may be from before records said `full` (Lander.full_gate_advice)."""

    def kit_layout(self):
        self.repo.write('content/roles/worker.md', 'role\n')
        self.repo.write('tools/render_content.py', '')
        self.main = self.repo.commit('kit layout', 'content/roles/worker.md', 'tools/render_content.py')
        self.repo.git('push', '-q', 'origin', 'main')

    def main_moves(self):
        self.repo.write('later.txt', 'later\n')
        self.main = self.repo.commit('main moves on', 'later.txt')
        self.repo.git('push', '-q', 'origin', 'main')

    def refusal(self, behind=False):
        """The refused landing of an ungated branch (`behind`: main moves on after it branched):
        (gate.py it names, the worktree, the first line)."""
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        if behind:
            self.main_moves()
        out = self.lander(BRANCH, self.main, tip).run()
        self.assert_refused(out, BRANCH, tip, wt)
        return COMMAND.search(out.lines[0]).group(1), wt, out.lines[0]

    def test_another_repository_is_gated_by_the_running_kit(self):
        gate_py, _, first = self.refusal()
        self.assertTrue(same_path(gate_py, os.path.join(S.kit_live(), 'tools', 'gate.py')), gate_py)
        self.assertIn(GATE_WHOLE, first)

    def test_another_repository_behind_its_base_is_not_told_to_merge(self):
        # the running kit's gate.py writes `full` whatever the branch holds
        gate_py, _, first = self.refusal(behind=True)
        self.assertTrue(same_path(gate_py, os.path.join(S.kit_live(), 'tools', 'gate.py')), gate_py)
        self.assertIn(GATE_WHOLE, first)
        self.assertNotIn('merge origin/main', first)

    def test_the_kit_is_gated_by_the_worktrees_own_gate(self):
        self.kit_layout()
        gate_py, wt, first = self.refusal()
        self.assertTrue(same_path(gate_py, os.path.join(wt, 'tools', 'gate.py')), gate_py)
        self.assertIn(GATE_WHOLE, first)            # the tip holds the base: no merge to make
        self.assertNotIn('merge origin/main', first)

    def test_a_kit_branch_behind_its_base_is_told_to_merge_it_first(self):
        self.kit_layout()
        gate_py, wt, first = self.refusal(behind=True)
        self.assertTrue(same_path(gate_py, os.path.join(wt, 'tools', 'gate.py')), gate_py)
        self.assertIn(MERGE_FIRST % (self.main[:10], BRANCH), first)
        self.assertIn('then gate the merge whole and land that tip: python', first)
        self.assertNotIn(GATE_WHOLE, first)


class ManualOnly(TipCase):
    """A base whose "gate" section has no step that is not manual is refused before any tip is
    read (Lander.manual_only): neither `gate.py --full` nor the merged gate would select a step,
    so the tip check's command could never be met. One runnable step among manual ones lands."""

    def set_gate(self, gate):
        self.repo.write('.kit.json', json.dumps(dict(test_land.LandCase.KIT, gate=gate), indent=1))
        self.main = self.repo.commit('gate config', '.kit.json')
        self.repo.git('push', '-q', 'origin', 'main')

    def assert_manual_refusal(self, gate):
        self.set_gate(gate)
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate_tips(tip)                         # a full record: not what refuses it
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        first = out.lines[0]
        self.assertIn('the "gate" section of .kit.json at %s has no step that is not manual'
                      % self.main[:10], first)
        self.assertIn('nothing was merged', first)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertIsNone(self.lock().read())

    def test_a_gate_of_manual_steps_only_is_refused(self):
        self.assert_manual_refusal({'steps': [
            {'name': 'unit', 'manual': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'}]})

    def test_a_gate_with_no_step_is_refused(self):
        self.assert_manual_refusal({'steps': []})

    def test_one_step_that_is_not_manual_lands(self):
        self.set_gate({'steps': [
            {'name': 'slow', 'manual': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'},
            dict(fixtures.GATE['steps'][0])]})
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.gate(wt, full=True)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)


class TipConfig(TipCase):
    """The tip record's `gate_config` is not compared with any config (Lander.tip_gate_check):
    a branch that changes its own gate is gated whole by it and lands; the merged gate runs the
    base's. .kit.json is owner-only, so the base names an owner and the owner commits the change
    (tests/gate/test_land_owner.py)."""
    OWNER = 'owner@example.invalid'
    KIT = dict(test_land.LandCase.KIT, owner={'email': OWNER})

    def test_a_branch_that_changes_its_gate_lands_on_its_own_full_record(self):
        gate = {'steps': [dict(fixtures.GATE['steps'][0]),
                          dict(fixtures.GATE['steps'][0], name='lint')]}
        wt = self.repo.worktree('feature')
        self.repo.write('.kit.json', json.dumps(dict(self.KIT, gate=gate), indent=1), root=wt)
        self.repo.git('add', '.kit.json', cwd=wt)
        self.repo.git('-c', 'user.email=%s' % self.OWNER, 'commit', '-q', '-m', 'gate', cwd=wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        self.gate(wt, full=True)
        rec, why = gate_ok.verify_full(self.repo.root, tip)
        self.assertIsNone(why)
        self.assertNotEqual(rec['gate_config'],
                            gate_ok.config_hash(runner.gate_config_at(self.repo.root, self.main)))
        self.assertEqual([s['name'] for s in rec['steps']], ['unit', 'lint'])
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)


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
