"""The ledger rule's wider reach (KIT-LEDGER-GUARD-WRITE-GAPS): a folder above a held path (and
a wildcard that can name one), git that rewrites a tree holding the ledger (checkout or switch
that discards changes, reset --hard/--merge/--keep, a stash that is not a read, a clean that is
not a dry run), and a writing git call in a place the guard cannot tell (GIT_DIR, --git-dir,
--work-tree, a computed -C). These bind every session, the owner's and the PM's included
(rules.ledger_wide_binds, the one place the owner's answer may narrow them)."""
import os
import unittest

from kitlib.guard import context, rules

from . import world as W
from .claudehost import decide
from .world import bash, event, ps

OWNER = ({}, False)
SUB = ({}, True)
PM = ({context.ROLE_ENV: 'pm'}, False)
EVERY = (OWNER, SUB, PM)


class Base(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def decide(self, make, session):
        environ, sub = session
        return decide(make(sub), self.w.ctx(environ=environ))

    def refused(self, make, label, sessions=EVERY):
        for session in sessions:
            with self.subTest(label, session=session):
                d = self.decide(make, session)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)

    def not_ledger(self, make, label, sessions=EVERY):
        for session in sessions:
            with self.subTest(label, session=session):
                d = self.decide(make, session)
                self.assertTrue(d is None or d.rule != 'ledger', d and d.reason)

    def rows(self, check, rows):
        for cwd, line in rows:
            shell = ps if line.startswith('PS ') else bash
            text = line[3:] if line.startswith('PS ') else line
            check(lambda sub, s=shell, t=text, c=cwd: s(t, c, sub), line)


class OutsideGit(Base):
    """The ledger in base/pm/rulings, a folder outside git."""

    def setUp(self):
        super().setUp()
        self.folder = os.path.dirname(self.w.ledger)
        self.pm = os.path.dirname(self.folder)
        os.makedirs(os.path.join(self.folder, 'rules'))
        self.w.user_config({'ledger_path': self.w.ledger})

    def test_the_binding_default_is_every_session(self):
        self.assertTrue(rules.ledger_wide_binds(event('Bash', {'command': 'x'}, self.w.wt), self.w.ctx()))
        self.refused(lambda sub: bash('rm -r rulings', self.pm, sub), 'owner', sessions=(OWNER,))

    def test_a_folder_above_the_ledger(self):
        self.rows(self.refused, ((self.pm, 'rm -r rulings'), (self.pm, 'mv rulings old'),
                                 (self.folder, 'rm -rf ../rulings'), (self.pm, 'PS Remove-Item -Recurse -Force rulings'),
                                 (self.pm, 'cmd /c rd /s /q rulings'), (self.w.base, 'rm -r pm')))

    def test_a_wildcard_that_can_name_a_folder_above_it(self):
        self.rows(self.refused, ((self.pm, 'rm -r r*'), (self.w.base, 'rm -r p*'), (self.pm, 'rm -r r\\<')))

    def test_git_that_rewrites_the_tree(self):
        f = self.folder
        self.rows(self.refused, ((f, 'git checkout -f'), (f, 'git checkout --force main'), (f, 'git checkout -f main'),
                                 (f, 'git reset --hard'), (f, 'git reset --hard HEAD~1'), (f, 'git reset --merge'),
                                 (f, 'git reset --k HEAD~1'), (f, 'git stash'), (f, 'git stash -u'),
                                 (f, 'git stash pop'), (f, 'git clean -fdx'), (f, 'git clean -fd rules'),
                                 (f, 'git clean -fn --no-dry-run')))

    def test_a_writing_git_call_in_a_place_the_guard_cannot_tell(self):
        f = self.folder
        self.rows(self.refused, ((f, 'git --git-dir=x/.git --work-tree=. rm LEDGER.md'),
                                 (f, 'GIT_DIR=x git rm LEDGER.md'), (f, 'git -C "$d" reset --hard'),
                                 (self.w.wt, 'git --work-tree=%s checkout -- .' % W.fwd(f))))

    def test_what_still_passes(self):
        f = self.folder
        self.rows(self.not_ledger, ((f, 'git status'), (f, 'git stash list'), (f, 'git clean -n'),
                                    (f, 'git clean -fd other'), (f, 'git --git-dir=x status'),
                                    (f, 'rm -f *.txt'), (f, 'rm -f rules/drafts/*'), (f, 'echo x >> NOTES.md'),
                                    (self.w.wt, 'git reset --hard'), (self.w.wt, 'git stash')))

    def test_the_pm_tool_run_in_the_folder_is_a_stand_in(self):
        tool = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'ledger.py'))
        self.not_ledger(lambda sub: bash('python "%s" conflicts --repo kit --topic t --key k --text r' % tool,
                                         self.folder, sub), 'ledger.py', sessions=(PM,))


class SplitLedger(Base):
    """A split ledger at wt/ledger, git run from wt/src."""

    def setUp(self):
        super().setUp()
        split = os.path.join(self.w.wt, 'ledger')
        os.makedirs(os.path.join(split, 'rules'))
        self.src = os.path.join(self.w.wt, 'src')
        os.makedirs(self.src)
        self.w.user_config({'ledger_dir': split, 'ledger_repos': ['kit']})

    def test_git_that_rewrites_the_tree(self):
        s = self.src
        self.rows(self.refused, ((s, 'git checkout -f'), (s, 'git switch -f main'),
                                 (s, 'git switch --discard-changes main'), (s, 'git reset --hard'),
                                 (s, 'git reset --keep HEAD~1'), (s, 'git stash push -m x'), (s, 'git clean -fdx'),
                                 (s, 'git clean -fd ../ledger')))

    def test_what_still_passes(self):
        s = self.src
        self.rows(self.not_ledger, ((s, 'git clean -fd .'), (s, 'git stash show'), (s, 'git checkout main'),
                                    (s, 'git reset --soft HEAD~1')))


if __name__ == '__main__':
    unittest.main()
