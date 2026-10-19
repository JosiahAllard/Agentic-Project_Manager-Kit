"""Second-review fixes to the PM data routes (docs/guard.md "Rules" and "Fallback"):

  - `& (<expr>)` is refused as unplaced for an agent session in any repository (a warning by
    default): a computed program skips the rules that need a literal one (heavy,
    the write jail, the git rules); only the owner and `plain` read it as any other line;
  - git-remote-rewrite counts a directory the text cannot tell only when the machine config
    names pm_data or the command's own directory has a record (rules._unknown_dir_counts).

HOME set by arithmetic or select, and an alias from config on a --config-env call, are no
longer judged: the guard catches honest mistakes, not evasion (R-2271). Their allowed forms
stay pinned."""
import os
import unittest

from kitlib import guard, pmdata

from . import claudehost
from . import world as W
from .test_pmdata_gaps import PLAIN, PM, ROOT, Base
from .world import ps


class CallSubexpressionAgents(Base):
    """Finding: `& (...)` read clean in every session, so in a repository without a PM data
    record an agent's computed program skipped heavy, the write jail and the git rules."""

    def setUp(self):
        super().setUp()
        os.remove(self.record)                  # no PM data anywhere: only the agent check is left

    def lines(self):
        main = W.fwd(self.w.main)
        return ('& (Get-Command git) -C "%s" reset --hard' % main,
                'cd $env:TEMP; & (Get-Command git) -C "%s" push --no-verify origin pmdata' % main,
                '& (Join-Path $r x.exe) a',
                "& (Join-Path $r x.exe) a; echo 'x")      # unclean for another reason too

    def test_an_agent_session_is_denied(self):
        W.deny_unplaced()                       # unplaced is warn by default (test_by_default_...)
        self.addCleanup(W.allow_unplaced)
        for text in self.lines():
            for label, sub, env in self.sessions():
                with self.subTest(text=text, session=label):
                    d = self.decide(text, sub=sub, environ=env, shell=ps)
                    self.assert_denied(d, 'unplaced')
                    self.assertIn('Name the program literally', d.reason)

    def test_by_default_an_agent_session_is_warned(self):
        for text in self.lines():
            for label, sub, env in self.sessions():
                with self.subTest(text=text, session=label):
                    d = guard.decide(claudehost.action(ps(text, self.w.wt, sub=sub)), self.ctx(env))
                    self.assertEqual((d.verdict, d.rule), ('allow', 'warn'), d.reason)
                    self.assertIn('Name the program literally', d.reason)

    def test_the_owner_and_plain_pass(self):
        for text in self.lines()[:3]:
            with self.subTest(text=text, session='owner'):
                self.assertIsNone(self.decide(text, shell=ps))
            with self.subTest(text=text, session='plain'):
                self.assertIsNone(self.decide(text, environ=PLAIN, shell=ps))
        self.assertIsNone(self.decide('& (Join-Path $r x.exe) a', cwd=self.w.main, shell=ps))

    def test_a_pm_data_repository_still_names_pm_data(self):
        pmdata.write(os.path.join(self.w.main, '.git'), 'pmdata', 'private', ROOT)
        for label, sub, env in self.sessions():
            with self.subTest(label):
                self.assert_denied(self.decide('& (Get-Command git) push', sub=sub, environ=env, shell=ps),
                                   'git-push-pmdata')


class HomeByArithmetic(Base):
    """HOME is counted only when the line assigns it literally (commands.line_env_names): let,
    arithmetic and select are not read (R-2271), and a push after them is judged as any push."""

    def test_reads_and_other_names_pass(self):
        for text in ('let x=1; git push private pmdata',
                     ': $((x+1)); git push private pmdata',
                     'echo $((HOME)); git push private pmdata',
                     '(( HOME == 1 )); git push private pmdata',
                     "echo '$((HOME=1))'; git push private pmdata",
                     'select x in a; do git push private pmdata; done',
                     'for ((i=$n; i<2; i++)); do git push private pmdata; done'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))


class RemoteRewriteUnknownDirectory(Base):
    """Finding: git-remote-rewrite counted a directory the text cannot tell as a recorded
    repository (pmdata_recorded(None)), so `git -C $D remote add` was denied for every session
    but the owner's, on machines whose config names no pm_data too."""

    TEXTS = ('git -C $D remote add x https://h/x', 'git -C $D config remote.x.url https://h/x')

    def test_no_machine_pm_data_and_no_record_passes(self):
        os.remove(self.record)
        # a session the write jail does not hold: for a jailed one write-jail denies git -C $D first
        for text in self.TEXTS:
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PLAIN, cwd=self.w.plain))

    def test_a_record_where_it_runs_or_machine_pm_data_denies(self):
        for text in self.TEXTS:
            with self.subTest(text=text, where='record'):
                self.assert_denied(self.decide(text, environ=PLAIN), 'git-remote-rewrite')
                self.assertIsNone(self.decide(text))
        os.remove(self.record)
        self.machine_pm_data()
        for text in self.TEXTS:
            with self.subTest(text=text, where='machine'):
                self.assert_denied(self.decide(text, environ=PLAIN, cwd=self.w.plain), 'git-remote-rewrite')


if __name__ == '__main__':
    unittest.main()
