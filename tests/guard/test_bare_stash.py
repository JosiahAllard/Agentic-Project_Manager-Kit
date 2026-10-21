"""git-stash-shared (docs/guard.md "Rules"): an agent session's stash in a linked worktree that adds
an untagged entry or takes one it does not name. The sessions it holds follow agent_session, as
kill-by-name does; a main checkout's stash stays git-stash's. The command rows are in
tests/guard/shell_rows.py."""
import unittest

from kitlib.guard import commands as cm, context, rules, shell as sh

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
        wt = W.fwd(self.w.wt)
        # a WIP commit of a path git does not know fails, so the alternative stages first (a new file too)
        self.assertIn('stage each path you changed, a new file too, then commit them with git -C "%s" commit -m '
                      '"WIP: <what>" -- <each path you changed>, each in its own call.' % wt, d.reason)
        self.assertIn('Instead: git -C "%s" add -- <each path you changed> [kit guard:' % wt, d.reason)


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


class VerbIsTheFirstWord(unittest.TestCase):
    """git reads a stash verb only as the first word after `stash` (git 2.55, observed: `stash --
    show` and `stash -- list` push the path, `stash -q list` exits 128), so rules.git_reads, which
    the write jail asks, counts only `list` or `show` there as a read (rules.stash_reads)."""

    @staticmethod
    def call(*words):
        return cm.GitCall(None, 'stash', [sh.Word(t, False, 0, 0) for t in words], None, False)

    def test_an_option_or_dashes_first_is_no_read(self):
        for words in (('--', 'show'), ('--', 'list'), ('-q', 'list'), ('--quiet', 'show'), ('-q', '--', 'show'),
                      ('-m', 'x', 'list'), ()):
            for strict in (False, True):
                with self.subTest(words=words, strict=strict):
                    self.assertFalse(rules.git_reads(self.call(*words), strict))

    def test_list_and_show_first_read(self):
        for words in (('list',), ('show',), ('show', '-p'), ('list', '--oneline'), ('show', 'stash@{1}')):
            for strict in (False, True):
                with self.subTest(words=words, strict=strict):
                    self.assertTrue(rules.git_reads(self.call(*words), strict))


if __name__ == '__main__':
    unittest.main()
