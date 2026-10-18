"""kitlib.gate.land's recovery hint for commits on the remote's main on top of a landed merge
(Lander.on_origin_steps): the full gate it names runs in a worktree, never with -C the main
checkout (AGENTS.md: full gates go in a worktree), and it says how to make one
(tools/new_worktree.py -C <main checkout>).

A green landing into a scratch repository with a bare remote in temp, then an owner commit pushed
on top of its merge; the hint is read from the same Lander's on_origin_steps."""
import os
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land
from kitlib.gate import steps as S
from tests.gate.test_land import BRANCH, LandCase


class OnTopHint(LandCase):
    def on_top_step(self, kit=False):
        """The on_origin_steps line about commits on top of the merge, after a green landing and
        an owner commit pushed on top of it. `kit`: the repository read as the kit's own
        (oncommit.is_kit) while the hint is made, and only then."""
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander = self.lander(BRANCH, self.main, tip)
        out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.repo.write('docs/note.md', 'owner\n')
        on_top = self.repo.commit('owner edit', 'docs/note.md')
        self.repo.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.remote_main(), on_top)
        left = lander.leftovers()
        self.assertEqual(left['pushed'], on_top)
        with mock.patch.object(land.oncommit, 'is_kit', return_value=kit):
            steps = lander.on_origin_steps(left, left['pushed'])
        hits = [what for what, _ in steps if 'on top of the merge' in what]
        self.assertEqual(len(hits), 1, hits)
        return hits[0]

    def assert_worktree_hint(self, what, gate_py):
        root = self.repo.root
        self.assertNotIn('-C "%s" --full' % root, what)           # never the main checkout
        new_wt = os.path.join(S.kit_live(), 'tools', 'new_worktree.py')
        self.assertIn('`python "%s" -C "%s" <area>/<slug>`' % (new_wt, root), what)
        self.assertIn('`python "%s" -C "<worktree>" --full`' % gate_py, what)

    def test_the_full_gate_hint_names_a_worktree_not_the_main_checkout(self):
        what = self.on_top_step()
        self.assert_worktree_hint(what, os.path.join(S.kit_live(), 'tools', 'gate.py'))

    def test_in_the_kit_the_worktree_runs_its_own_gate_py(self):
        # The kit lands itself: the worktree is gated by its own gate.py (docs/gate.md "The
        # caller's checkout"), as full_gate_command names it.
        what = self.on_top_step(kit=True)
        self.assert_worktree_hint(what, os.path.join('<worktree>', 'tools', 'gate.py'))


if __name__ == '__main__':
    unittest.main()
