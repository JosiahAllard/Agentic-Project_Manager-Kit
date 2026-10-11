"""kitlib.gate.land when the owner commits on top of the gated merge in the main checkout and pushes
while the merged gate runs (KIT-GATE-UNSTICK; docs/gate.md "Landing", steps 7 and 8).

Green, the merge on <remote>/<main> with commits on top, main == the tracking ref, a clean main
checkout with HEAD on main: the landing is finished (records closed, lock released, cleanup), exit
0 when the commits on top select no gate step, exit LANDED_UNGATED when they do. Anything else
still ends STUCK, and a STUCK report whose merge is on the remote never says reset.

The merged gate is injected: it plays the owner, committing in the main checkout and pushing.
Scratch repositories with a bare remote in temp and the scratch lock store only."""
import unittest

from kitlib import gate as G
from kitlib.gate import result
from kitlib.records import gate_ok
from tests.gate import fixtures
from tests.gate.test_land import BRANCH
from tests.gate.test_land_store import StoreLandCase, pulled
from tests.gate.test_stuck import _Stuck, commands, joined

GREEN = G.Outcome(G.OK, ['GATE OK'], verdict='GATE OK', results=[])
RED = G.Outcome(G.FAILED, ['GATE FAILED: unit (unit: exit 1)'], verdict='GATE FAILED: unit',
                results=[])
# `unit` runs only for python files, so a commit on top that changes a doc selects no step.
GLOBBED = {'steps': [{'name': 'unit', 'globs': ['*.py'], 'cmd': fixtures.RED_AWARE,
                      'pass': '^ok$'}]}


class _OnTop(_Stuck):
    """Landings whose lock holder is a dead process (so a printed break command can break it)."""
    GATE = GLOBBED

    def owner(self, files, push=True, after=None, verdict=GREEN):
        """A merged gate that commits `files` in the main checkout (the owner), pushes main when
        `push`, then runs `after()`, and returns `verdict`. Its commit goes to self.extra."""
        def gate(lander):
            for rel, text in files.items():
                self.repo.write(rel, text)
            self.extra = self.repo.commit('owner edit', *files)
            if push:
                self.repo.git('push', '-q', 'origin', 'main')
            if after is not None:
                after()
            return verdict
        return gate

    def land(self, gate, branches=None):
        if branches is None:
            _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
            return self.lander(BRANCH, self.main, tip, merged_gate=gate).run()
        tips = [self.branch(b, {'%s.py' % b: 'x = 1\n'})[1] for b in branches]
        return self.lander(['kit/%s' % b for b in branches], self.main, tips,
                           merged_gate=gate).run()

    def landed_line(self, out):
        lines = [l for l in out.lines if l.startswith('LANDED ')]
        self.assertEqual(len(lines), 1, out.lines)
        return lines[0]

    def assert_finished(self, out):
        """Landed at the merge, with the owner's commit on top on main, the remote and the
        tracking ref, and the lock released."""
        self.assertFalse([l for l in out.lines if l.startswith('STUCK')], out.lines)
        line = self.landed_line(out)
        self.assertTrue(line.startswith('LANDED kit/feature on main at %s' % out.merge), line)
        self.assertIn(self.extra[:10], line)
        self.assertIn('owner edit', line)
        self.assertEqual(self.repo.git('rev-parse', self.extra + '^1'), out.merge)
        self.assertEqual(self.main_now(), self.extra)
        self.assertEqual(self.remote_main(), self.extra)
        self.assertEqual(self.repo.git('rev-parse', 'refs/remotes/origin/main'), self.extra)
        self.assertIsNone(self.lock().read())
        return line

    def assert_no_reset(self, out):
        """A STUCK report whose merge is on the remote: nothing in it says reset, since a reset
        would put main behind a remote that already holds the merge (the hub-validate landing
        printed `reset --keep <base>`). The lock is left at phase stuck."""
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertNotIn('reset', joined(out).lower(), out.lines)
        self.assertEqual(self.lock().read()['phase'], 'stuck')

    def run_printed(self, out):
        """Runs every printed command; the last is the break command, and it breaks the lock."""
        printed = None
        for c in commands(out):
            printed = self.sh(c)
        self.assert_broken(printed)


class Adopts(_OnTop):
    def test_a_doc_commit_pushed_on_top_lands_with_exit_0(self):           # T1
        out = self.land(self.owner({'docs/note.md': 'owner\n'}))
        self.assertEqual(out.code, G.OK, out.lines)
        line = self.assert_finished(out)
        self.assertIn('1 commit(s) on top', line)
        self.assertFalse([l for l in out.lines if l.startswith('WARNING')], out.lines)
        rec, why = gate_ok.verify(self.repo.root, out.merge)       # the gated merge's record
        self.assertIsNone(why)
        self.assertEqual(rec['base'], self.main)
        got, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNone(why)
        self.assertEqual((got['verdict'], got['code']), ('pass', G.OK))
        entry = out.cleanup[BRANCH]                                # the branch cleanup ran
        self.assertEqual((entry['worktree'], entry['local_branch']), ('removed', 'deleted'),
                         out.lines)
        self.assertEqual(got['cleanup'], entry)

    def test_a_gated_path_pushed_on_top_lands_with_its_own_exit_code(self):  # T2
        out = self.land(self.owner({'tools/x.py': 'y = 2\n'}))
        self.assertEqual(out.code, G.LANDED_UNGATED, out.lines)
        self.assertNotEqual(G.LANDED_UNGATED, G.OK)
        self.assert_finished(out)
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        for part in ('not gated whole', self.extra[:10], 'tools/x.py', 'unit',
                     'gate.py" -C "%s" --full' % self.repo.root):
            self.assertIn(part, warn[0])
        self.assertEqual(self.records(), [])                       # no gate-OK record written
        got, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNone(why)
        self.assertEqual((got['verdict'], got['code']), ('landed-ungated', G.LANDED_UNGATED))

    def test_a_batch_with_a_commit_on_top_lands_each_branch(self):          # T7
        out = self.land(self.owner({'docs/note.md': 'owner\n'}), branches=['one', 'two'])
        self.assertEqual(out.code, G.OK, out.lines)
        line = self.landed_line(out)
        self.assertTrue(line.startswith('LANDED kit/one, kit/two on main at %s' % out.merge), line)
        self.assertIn(self.extra[:10], line)
        self.assertEqual(self.remote_main(), self.extra)
        self.assertIsNone(self.lock().read())
        for b in ('kit/one', 'kit/two'):
            got, why = result.read(self.repo.root, b, 'land')
            self.assertIsNone(why)
            self.assertEqual(got['verdict'], 'pass', b)


class AlwaysStep(_OnTop):
    """The kit's own gate: one step with `always`, so any commit on top is one no gate saw."""
    GATE = fixtures.GATE

    def test_an_always_step_counts_any_commit_on_top_as_ungated(self):
        out = self.land(self.owner({'docs/note.md': 'owner\n'}))
        self.assertEqual(out.code, G.LANDED_UNGATED, out.lines)
        self.assert_finished(out)
        self.assertIn('unit', ' '.join(l for l in out.lines if l.startswith('WARNING')))


class StillStuck(_OnTop):
    def test_a_commit_on_top_not_yet_pushed_is_stuck_and_not_reset(self):  # T3
        def local():
            self.repo.write('docs/local.md', 'local\n')
            self.local = self.repo.commit('owner local', 'docs/local.md')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=local))
        self.assert_no_reset(out)
        self.assertEqual(self.main_now(), self.local)               # nothing moved
        self.assertEqual(self.remote_main(), self.extra)
        self.assertIn(self.local[:10], joined(out))
        self.repo.git('push', '-q', 'origin', 'main')               # the owner settles it
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.local)

    def test_an_untracked_file_is_stuck_and_not_reset(self):               # T4
        def litter():
            self.repo.write('scratch.txt', 'left\n')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=litter))
        self.assert_no_reset(out)
        self.assertIn('scratch.txt', joined(out))
        self.assertEqual(self.main_now(), self.extra)
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.extra)
        self.assertEqual(self.remote_main(), self.extra)

    def test_the_merge_pushed_alone_with_an_untracked_file_is_not_reset(self):
        # origin == the merge exactly: the undo found main one merge ahead with no tracked change,
        # and reset it behind the remote.
        def gate(lander):
            self.repo.git('push', '-q', 'origin', 'main')
            self.repo.write('scratch.txt', 'left\n')
            return GREEN
        out = self.land(gate)
        self.assert_no_reset(out)
        self.assertEqual(self.main_now(), out.merge)
        self.assertEqual(self.remote_main(), out.merge)
        self.run_printed(out)
        self.assertEqual(self.main_now(), out.merge)

    def test_head_switched_after_the_push_is_stuck_and_not_reset(self):    # T5
        def switch():
            self.repo.git('switch', '-q', '-c', 'elsewhere')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=switch))
        self.assert_no_reset(out)
        self.assertIn('switch back', joined(out))
        self.run_printed(out)
        self.assertEqual(self.head_branch(), 'main')
        self.assertEqual(self.main_now(), self.extra)

    def test_a_red_gate_whose_merge_the_owner_pushed_is_stuck_and_not_reset(self):  # T6
        def gate(lander):
            self.repo.git('push', '-q', 'origin', 'main')
            return RED
        out = self.land(gate)
        self.assert_no_reset(out)
        self.assertIn('without a passing', joined(out))
        self.assertIn('revert -m 1 %s' % out.merge, joined(out))
        self.assertEqual(self.main_now(), out.merge)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.records(), [])
        self.run_printed(out)
        self.assertEqual(self.main_now(), out.merge)

    def test_a_red_gate_under_a_commit_pushed_on_top_is_stuck_and_not_reset(self):
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, verdict=RED))
        self.assert_no_reset(out)
        self.assertIn('without a passing', joined(out))
        self.assertEqual(self.remote_main(), self.extra)
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.extra)
        self.assertEqual(self.remote_main(), self.extra)

    def test_stuck_with_merge_on_origin_prints_no_reset(self):
        # The hub-validate shape: green, the owner's commit pushed on top of the merge, so the
        # remote descends from it; an uncommitted edit keeps the landing from being finished, and
        # the report goes through clear_steps. At base it printed `reset --keep <base>`.
        def edit():
            self.repo.write('README.md', 'edited, not committed\n')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=edit))
        self.assert_no_reset(out)
        self.assertTrue(out.lines[0].startswith('STUCK: landing kit/feature'), out.lines)
        self.assertIn('README.md', joined(out))
        self.assertEqual(self.remote_main(), self.extra)
        self.assertEqual(self.repo.git('rev-parse', self.extra + '^1'), out.merge)
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.extra)               # nothing reset
        self.assertEqual(self.remote_main(), self.extra)
        with open(self.repo.path('README.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'edited, not committed\n')


class ClosesRecords(StoreLandCase):
    GATE = GLOBBED

    def test_the_pulled_record_is_closed_when_the_merge_is_adopted(self):  # T8
        self.seed(pulled('FOO'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def gate(lander):
            self.repo.write('docs/note.md', 'owner\n')
            self.repo.commit('owner edit', 'docs/note.md')
            self.repo.git('push', '-q', 'origin', 'main')
            return GREEN
        out = self.lander(BRANCH, self.main, tip, merged_gate=gate).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), [])
        self.assertIn('closes FOO: landed %s' % out.merge, self.subjects())
        self.assertIsNone(self.lock().read())


if __name__ == '__main__':
    unittest.main()
