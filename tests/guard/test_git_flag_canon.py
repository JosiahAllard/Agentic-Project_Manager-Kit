"""git option spellings the guard reads as git does: a one-letter
abbreviation of a long option (`--h` is `--hard` to git when no other reset option starts with
h), and clean's dry run read in order (`-n --no-dry-run` deletes: the last one wins)."""
import os
import unittest

from kitlib.guard import context, delegfile

from . import world as W
from .claudehost import decide
from .world import bash

OWNER = ({}, False)
SUB = ({}, True)
PM = ({context.ROLE_ENV: 'pm'}, False)


class Base(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.pointer = None

    def decide(self, text, cwd, session):
        environ, sub = session
        kw = {'pointer': (lambda: self.pointer)} if self.pointer else {}
        return decide(bash(text, cwd, sub), self.w.ctx(environ=environ, **kw))

    def assert_rule(self, text, cwd, rule, sessions=(OWNER, SUB)):
        for session in sessions:
            with self.subTest(text, session=session):
                d = self.decide(text, cwd, session)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def assert_not_rule(self, text, cwd, rule, sessions=(OWNER, SUB)):
        for session in sessions:
            with self.subTest(text, session=session):
                d = self.decide(text, cwd, session)
                self.assertTrue(d is None or d.rule != rule, d and d.reason)


class OneLetterLongs(Base):

    def test_reset_one_letter_hard_keep_merge_in_main(self):
        # git reads `--h` as `--hard`, `--k` as `--keep` (no other reset option starts with h or k)
        self.assert_rule('git reset --h', self.w.main, 'git-reset-hard', sessions=(OWNER,))
        self.assert_rule('git reset --h origin/main', self.w.main, 'git-reset-hard')

    def test_branch_one_letter_delete_with_force(self):
        self.assert_rule('git branch --d -f kit/old', self.w.wt, 'git-branch-force-delete')
        self.assert_rule('git branch --d --forc kit/old', self.w.wt, 'git-branch-force-delete')

    def test_an_ambiguous_one_letter_stays_as_written(self):
        # `--s` is staged or source to restore: git rejects it; the guard keeps it unknown, so
        # restore is not read as index-only (fail closed)
        self.assert_rule('git restore --s a.py', self.w.main, 'git-restore', sessions=(OWNER,))


class CleanDryRunInOrder(Base):

    def test_no_dry_run_after_n_deletes_in_main(self):
        for text in ('git clean -fn --no-dry-run', 'git clean -n -f --no-d', 'git clean --dry-run -f --no-dry'):
            self.assert_rule(text, self.w.main, 'git-clean')

    def test_a_dry_run_last_stays_open(self):
        for text in ('git clean -fn', 'git clean --no-dry-run -n -f', 'git clean --dry -d', 'git clean -f --dry-r'):
            self.assert_not_rule(text, self.w.main, 'git-clean')

    def test_clean_one_letter_d_stays_refused(self):
        # git reads `--d` as `--dry-run` (clean's -d has no long form), so this deletes nothing; the
        # guard leaves a one-letter dry run unread and refuses it (fail closed)
        self.assert_rule('git clean --d -f', self.w.main, 'git-clean')


class PmRepositoryCopy(Base):
    """delegfile.git and git_config with the PM repository pointer on base/pmdata."""

    def setUp(self):
        super().setUp()
        self.pointer = self.w.data
        copy = os.path.join(self.w.data, '.pm-kit', 'owner-delegations.json')
        os.makedirs(os.path.dirname(copy))
        with open(copy, 'w', encoding='utf-8') as fh:
            fh.write('{"schema": 1, "operations": {}}\n')

    def test_checkout_index_one_letter_all(self):
        self.assert_rule('git checkout-index --a', self.w.data, delegfile.RULE, sessions=(SUB, PM))
        self.assert_rule('git checkout-index --al -f', self.w.data, delegfile.RULE, sessions=(SUB, PM))

    def test_clean_no_dry_run_after_n(self):
        for text in ('git clean -fn --no-dry-run', 'git clean -n -f --no-d'):
            self.assert_rule(text, self.w.data, delegfile.RULE, sessions=(SUB, PM))

    def test_clean_no_dry_run_after_n_in_the_config_folder(self):
        # a path in .pm-kit that is not the list: delegfile.git leaves it, git_config holds the folder
        self.assert_rule('git clean -fn --no-dry-run .pm-kit/config.json', self.w.data, 'machine-config',
                         sessions=(SUB, PM))
        self.assert_not_rule('git clean -fn .pm-kit/config.json', self.w.data, 'machine-config', sessions=(SUB, PM))

    def test_clean_dry_run_stays_open(self):
        for text in ('git clean -fn', 'git clean -fn .pm-kit', 'git clean --dry-run -f'):
            self.assert_not_rule(text, self.w.data, delegfile.RULE, sessions=(SUB, PM))


if __name__ == '__main__':
    unittest.main()
