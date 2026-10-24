"""The ledger rule's wider reach: a folder above a held path (and
a wildcard that can name one), git that rewrites a tree holding the ledger (checkout or switch
that discards changes, reset --hard/--merge/--keep, a stash that is not a read, a clean that is
not a dry run), and a writing git call in a place the guard cannot tell (GIT_DIR, --git-dir,
--work-tree, a computed -C). These bind every marked session but the PM's (a subagent, an
agent's mark, `plain`, an unknown mark): the owner's unmarked session and the PM's pass them
(rules.ledger_wide_binds, the one switch that would widen them to every session)."""
import os
import unittest

from kitlib.guard import context, rules

from . import world as W
from .claudehost import action, decide
from .world import bash, event, ps

OWNER = ({}, False)
SUB = ({}, True)
PM = ({context.ROLE_ENV: 'pm'}, False)
COORDINATOR = ({context.ROLE_ENV: 'coordinator'}, False)
WORKER = ({context.ROLE_ENV: 'worker'}, False)
PLAIN = ({context.ROLE_ENV: 'plain'}, False)
ODD = ({context.ROLE_ENV: 'no-such-role'}, False)
AGENTS = (SUB, COORDINATOR, WORKER, PLAIN, ODD)
FREE = (OWNER, PM)


class Base(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def decide(self, make, session):
        environ, sub = session
        return decide(make(sub), self.w.ctx(environ=environ))

    def refused(self, make, label, sessions=AGENTS, free=FREE):
        """Refused as ledger for each of `sessions`; not this rule's for each of `free`."""
        for session in sessions:
            with self.subTest(label, session=session):
                d = self.decide(make, session)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        self.not_ledger(make, label, sessions=free)

    def not_ledger(self, make, label, sessions=AGENTS + FREE):
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

    def test_the_binding_default_is_agent_sessions(self):
        ev = lambda environ, sub: action(event('Bash', {'command': 'x'}, self.w.wt, sub=sub))
        for (environ, sub), binds in ((SUB, True), (COORDINATOR, True), (WORKER, True), (PLAIN, True),
                                      (ODD, True), (OWNER, False), (PM, False)):
            with self.subTest(environ=environ, sub=sub):
                self.assertIs(bool(rules.ledger_wide_binds(ev(environ, sub), self.w.ctx(environ=environ))), binds)
        self.refused(lambda sub: bash('rm -r rulings', self.pm, sub), 'rm -r rulings')

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
                                 (f, 'git clean -fn --no-dry-run'), (f, 'git stash push -m x -- rules'),
                                 (f, 'git stash push -m x other'), (f, "git stash push -- ':!other'"),
                                 (f, "git clean -fd -- ':!other'")))

    def test_a_writing_git_call_in_a_place_the_guard_cannot_tell(self):
        f = self.folder
        self.rows(self.refused, ((f, 'git --git-dir=x/.git --work-tree=. rm LEDGER.md'),
                                 (f, 'GIT_DIR=x git rm LEDGER.md'), (f, 'git -C "$d" reset --hard'),
                                 (self.w.wt, 'git --work-tree=%s checkout -- .' % W.fwd(f)),
                                 (f, 'git -C "$d" checkout main -- LEDGER.md'), (f, 'git -C "$d" checkout -f main'),
                                 (f, 'git -C "$d" switch --discard-changes main'),
                                 (f, 'git -C "$d" checkout --pathspec-from-file=x'),
                                 (f, 'git -C "$d" checkout .'), (f, 'git -C "$d" checkout HEAD LEDGER.md'),
                                 (f, 'git -C "$d" checkout LEDGER.md'), (f, 'git -C "$d" checkout -p'),
                                 (f, 'git -C "$d" checkout --patch main'), (f, 'git -C "$d" checkout rulings'),
                                 (f, 'git -C "$d" checkout kit/x'), (f, 'git -C "$d" checkout "*"'),
                                 (f, 'git -C "$d" checkout "$b"'), (f, 'git -C "$d" checkout LEDGER.md.')))

    def test_an_unplaced_checkout_of_a_branch_is_not_this_rule(self):
        f = self.folder
        self.rows(self.not_ledger, ((f, 'git -C "$d" checkout main'), (f, 'cd "$x" && git checkout main'),
                                    (f, 'git -C "$d" switch main'),
                                    (f, 'git -C "$d" checkout feature-x'), (f, 'git -C "$d" checkout')))

    def test_a_placed_checkout_of_a_branch_moves_the_tree(self):
        # as switch: a branch or commit, a new branch with a start, an orphan
        f = self.folder
        self.rows(self.refused, ((f, 'git checkout main'), (f, 'git checkout feature-x'), (f, 'git checkout -'),
                                 (f, 'git checkout --detach main'), (f, 'git checkout -m main'),
                                 (f, 'git checkout -b new main'), (f, 'git checkout -bnew main'),
                                 (f, 'git checkout -qb new main'), (f, 'git checkout -B new main'),
                                 (f, 'git checkout --orphan new'), (f, 'git checkout --orphan=new'),
                                 (f, 'git checkout --orph new main'), (f, 'git checkout -p main'),
                                 (f, 'git checkout --conflict merge main')))
        # as switch -c <branch>: a new branch at HEAD keeps the tree; path forms are judged as paths
        self.rows(self.not_ledger, ((f, 'git checkout'), (f, 'git checkout -b new'), (f, 'git checkout -B new'),
                                    (f, 'git checkout -- other'), (f, 'git checkout main -- other'),
                                    (f, 'git checkout main other notes'), (self.w.wt, 'git checkout main'),
                                    (self.w.wt, 'git checkout -b new main')))
        self.rows(lambda make, label: self.refused(make, label, sessions=AGENTS + FREE, free=()),
                  ((f, 'git checkout -- LEDGER.md'), (f, 'git checkout main -- .'), (f, 'git checkout main LEDGER.md')))

    def test_what_still_passes(self):
        f = self.folder
        self.rows(self.not_ledger, ((f, 'git status'), (f, 'git stash list'), (f, 'git clean -n'),
                                    (f, 'git clean -fd other'), (f, 'git --git-dir=x status'),
                                    (f, 'git stash push -m x -- other'), (f, 'git stash push -- other notes'),
                                    (f, 'rm -f *.txt'), (f, 'rm -f rules/drafts/*'), (f, 'echo x >> NOTES.md'),
                                    (self.w.wt, 'git reset --hard'), (self.w.wt, 'git stash')))

    def test_a_computed_subcommand_or_option(self):
        # the shell computes the subcommand, or a word reset, checkout or switch reads as an option
        f = self.folder
        self.rows(self.refused, ((f, 'git $x'), (f, 'git "$x" -f'), (f, 'git reset "$m"'),
                                 (f, 'git reset HEAD "$m"'), (f, 'git -C "$d" reset "$m"'),
                                 (f, 'git -C "$d" switch "$o" main')))
        # a computed branch name, the value of -c, is no option
        self.rows(self.not_ledger, ((f, 'git -C "$d" switch -c "$b" main'), (f, 'git reset -- "$m"')))

    def test_a_computed_subcommand_names_its_paths(self):
        # its positional words are paths for every session, as rm's are
        self.rows(lambda make, label: self.refused(make, label, sessions=AGENTS + FREE, free=()),
                  ((self.folder, 'git $x LEDGER.md'), (self.folder, 'git "$x" -- .')))

    def test_git_that_xargs_runs(self):
        f = self.folder
        every = lambda make, label: self.refused(make, label, sessions=AGENTS + FREE, free=())
        self.rows(every, ((f, 'ls | xargs git rm'), (f, 'ls | xargs git checkout'),
                          (f, 'ls | xargs git restore --staged'), (f, 'ls | xargs git checkout-index')))
        self.rows(self.refused, ((f, 'echo --hard | xargs git reset'), (f, 'ls | xargs git switch'),
                                 (f, 'ls | xargs git clean -fd other'), (f, 'echo x | xargs git'),
                                 (f, 'ls | xargs git -C "$d" checkout main')))

    def test_git_that_moves_the_tree(self):
        f = self.folder
        self.rows(self.refused, ((f, 'git switch main'), (f, 'git switch -c new main'), (f, 'git switch --orphan new'),
                                 (f, 'git merge other'), (f, 'git pull'), (f, 'git apply x.patch'),
                                 (f, 'git apply --index x.patch'), (f, 'git apply --cached -3 x.patch'),
                                 (f, 'git read-tree -u -m HEAD')))
        self.rows(self.not_ledger, ((f, 'git apply --check x.patch'), (f, 'git apply --stat x.patch'),
                                    (f, 'git apply --cached x.patch'), (f, 'git read-tree HEAD'),
                                    (f, 'git switch -c new'), (self.w.wt, 'git switch main'),
                                    (self.w.wt, 'git merge other')))

    def test_the_reason_says_writes_once(self):
        d = self.decide(lambda sub: bash('rm LEDGER.md', self.folder, sub), SUB)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        self.assertIn('`rm LEDGER.md` (LEDGER.md) writes the rulings ledger', d.reason)
        self.assertNotIn('writes LEDGER.md writes', d.reason)

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
        self.rows(self.not_ledger, ((s, 'git clean -fd .'), (s, 'git stash show'), (s, 'git checkout -- .'),
                                    (s, 'git reset --soft HEAD~1')))

    def test_an_unplaced_checkout_of_a_rulebook(self):
        # git reads a word that names no branch as a path: a `.md` name can be a rulebook
        s = self.src
        rows = ((s, 'git -C "$d" checkout policy.md'), (s, 'git -C "$d" checkout POLICY.MD.'),
                (s, 'cd "$x" && git checkout topic.md'))
        self.rows(self.refused, rows)
        self.rows(lambda make, label: self.not_ledger(make, label, sessions=FREE), rows)

    def test_an_unplaced_branch_creation_is_told_how_to_place_it(self):
        d = self.decide(lambda sub: bash('git -C "$d" checkout -b new main', self.src, sub), SUB)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        self.assertIn('Instead: run it with a literal `git -C <worktree>`', d.reason)
        self.assertIn('git switch -c <branch> <start>', d.reason)
        self.assertNotIn('ledger.py', d.instead)

    def branch_refusal(self, line):
        d = self.decide(lambda sub: bash(line, self.src, sub), SUB)
        self.assertIsNotNone(d, line)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        return d

    def test_every_unplaced_branch_creation_is_named_as_one(self):
        # -B, --orphan and its abbreviation: the reason names the branch, not the ledger's tool
        for line in ('git -C "$d" checkout -B new main', 'git -C "$d" checkout --orphan new kit/x',
                     'git -C "$d" checkout --orph new main', 'git -C "$d" checkout -b new main'):
            with self.subTest(line):
                d = self.branch_refusal(line)
                self.assertIn('creates a branch and checks out its start', d.reason)
                self.assertNotIn('only ledger.py writes.', d.reason)
                self.assertNotIn('ledger.py', d.instead)
                self.assertIn('git -C <worktree>', d.instead)

    def test_a_branch_creation_that_discards_is_a_tree_form(self):
        # not the branch path: -f rewrites the tree, so the ledger tool's Instead stands
        d = self.branch_refusal('git -C "$d" checkout -f -b new main')
        self.assertIn('writes a rulebook', d.reason)
        self.assertIn('ledger.py', d.instead)

    def test_a_branch_creation_under_git_dir_is_told_to_drop_it(self):
        d = self.branch_refusal('GIT_DIR=x git checkout -b new main')
        self.assertIn('drop GIT_DIR (a -C does not undo it', d.instead)
        self.assertIn('`git -C <worktree> switch -c <branch> <start>`', d.instead)
        self.assertNotIn('with `git switch -c', d.instead)

    def test_git_that_moves_a_tree_holding_a_split_ledger(self):
        s = self.src
        self.rows(self.refused, ((s, 'git switch main'), (s, 'git apply x.patch'), (s, 'git read-tree -u -m HEAD'),
                                 (s, 'ls | xargs git reset'), (s, 'git checkout main'), (s, 'git checkout -b new main'),
                                 (s, 'git checkout -B new main'), (s, 'git checkout --orphan new')))
        self.rows(self.not_ledger, ((s, 'git checkout -b new'), (s, 'git checkout -- x'),
                                    (s, 'git checkout main -- x')))
        self.rows(lambda make, label: self.refused(make, label, sessions=AGENTS + FREE, free=()),
                  ((s, 'ls | xargs git rm'), (s, 'git $x ../ledger')))


if __name__ == '__main__':
    unittest.main()
