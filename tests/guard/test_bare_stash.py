"""git-stash-shared (docs/guard.md "Rules"): an agent session's stash in a linked worktree that adds
an untagged entry or takes one it does not name. The sessions it holds follow agent_session, as
kill-by-name does; a main checkout's stash stays git-stash's. The command rows are in
tests/guard/shell_rows.py."""
import unittest

from kitlib.guard import context, rules

from . import world as W
from .claudehost import decide
from .world import bash, ps

RULE = 'git-stash-shared'


class Sessions(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def stash(self, env, sub=False, cmd='git stash', shell=bash):
        return decide(shell(cmd, self.w.wt, sub), self.w.ctx(environ=env))

    def test_subagent_and_default_marks_are_held(self):
        d = self.stash({}, sub=True)
        self.assertEqual((d.verdict, d.rule), ('deny', RULE), d.reason)
        self.assertTrue(d.reason.endswith(rules.tag(RULE, 'subagent')), d.reason)
        for mark in ('coordinator', 'conductor', 'deep-worker', 'consultant', 'pm'):
            with self.subTest(mark):
                d = self.stash({context.ROLE_ENV: mark}, cmd='git stash pop', shell=ps)
                self.assertEqual((d.verdict, d.rule), ('deny', RULE), d and d.reason)
                self.assertTrue(d.reason.endswith(rules.tag(RULE, 'main')), d.reason)

    def test_owner_and_plain_are_not_held(self):
        for env in ({}, {context.ROLE_ENV: 'plain'}):
            with self.subTest(env):
                self.assertIsNone(self.stash(env))
                self.assertIsNone(self.stash(env, cmd='git stash pop'))

    def test_marks_from_the_machine_config(self):
        self.w.user_config({'guard_agent_marks': ['plain']})
        self.assertEqual(self.stash({context.ROLE_ENV: 'plain'}).rule, RULE)
        self.assertIsNone(self.stash({context.ROLE_ENV: 'coordinator'}))

    def test_the_reason_names_the_shared_stack_and_the_gate(self):
        d = self.stash({}, sub=True, cmd='git stash pop')
        self.assertIn('`git stash pop` adds to or takes from the stash stack every worktree', d.reason)
        self.assertIn('docs/gate.md "The stash guard"', d.reason)
        self.assertIn('Instead: git -C "%s" commit -m "WIP: <what>" -- <each path you changed> [kit guard:'
                      % W.fwd(self.w.wt), d.reason)


class MainCheckoutUnchanged(unittest.TestCase):
    """A main checkout's stash is still git-stash's, the owner's session and subagents alike."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_main_checkout_stays_git_stash(self):
        for sub in (False, True):
            with self.subTest(sub=sub):
                d = decide(bash('git stash push -u -m tag-1', self.w.main, sub), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'git-stash'), d.reason)
        d = decide(bash('git stash', self.w.wt, True), self.w.ctx())
        self.assertNotEqual(d.rule, 'git-stash')


if __name__ == '__main__':
    unittest.main()
