"""The guard in a repository with no `.kit.json` (docs/kit-json.md "With no file"): a delete or a
forced push of the main branch is refused, the main branch read from the git files; a plain push
is allowed; the owner email comes from the machine config."""
import os
import unittest

from . import world as W
from .claudehost import decide
from .world import bash


class NoKitJson(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        os.remove(os.path.join(self.w.main, '.kit.json'))
        git = os.path.join(self.w.main, '.git')
        for d in ('objects', 'refs/heads', 'refs/remotes/origin'):
            os.makedirs(os.path.join(git, *d.split('/')), exist_ok=True)
        with open(os.path.join(git, 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/main\n')

    def head(self, branch):
        with open(os.path.join(self.w.main, '.git', 'refs', 'remotes', 'origin', 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/remotes/origin/%s\n' % branch)

    def decide(self, text, sub=False, cwd=None):
        return decide(bash(text, cwd or self.w.wt, sub=sub), self.w.ctx())

    def test_a_forced_push_or_delete_of_main_is_refused(self):
        for cmd in ('git push --force origin main', 'git push -f origin kit/wt:main', 'git push origin +kit/wt:main',
                    'git push --force-with-lease origin main', 'git push --mirror origin',
                    'git push origin :main', 'git push --delete origin main'):
            for sub in (False, True):
                with self.subTest(cmd=cmd, sub=sub):
                    d = self.decide(cmd, sub)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', 'git-push-main'), d.reason)
                    self.assertIn('to origin/main', d.reason)

    def test_the_refusal_names_a_push_without_force(self):
        d = self.decide('git push --force origin kit/wt:main')
        self.assertIn('main is never force-pushed', d.reason)
        self.assertIn('Instead: git -C "%s" push origin kit/wt:main' % W.fwd(self.w.wt), d.reason)

    def test_a_plain_push_is_allowed(self):
        for cmd in ('git push origin main', 'git push origin kit/wt', 'git push --force origin kit/wt',
                    'git push --no-verify origin main', 'git push $REMOTE main'):
            with self.subTest(cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_a_refspec_that_may_force_and_is_not_literal_is_refused(self):
        # a computed refspec with a force or delete flag, a literal `+` or `:`, or a computed
        # remote with --no-verify that skips the pre-push hook that would judge it
        for cmd in ('git push --force origin $BRANCH', 'git push origin "+$BRANCH"', 'git push origin ":$B"',
                    'git push --delete origin $B', 'git push --no-verify $REMOTE main'):
            with self.subTest(cmd):
                d = self.decide(cmd)
                self.assertEqual(d and d.rule, 'git-push-main', d and d.reason)
                self.assertIn('not literal', d.reason)

    def test_a_computed_refspec_without_force_is_unplaced(self):
        # KIT-GUARD-CHEAP item 4: a warning by default, a refusal when the owner turns it on
        for cmd in ('git push origin $BRANCH', 'git push origin "HEAD:$B"'):
            with self.subTest(cmd):
                self.assertIsNone(self.decide(cmd), cmd)
                W.deny_softened()
                try:
                    d = self.decide(cmd)
                finally:
                    W.allow_unplaced()
                self.assertEqual(d and d.rule, 'unplaced', d and d.reason)

    def test_the_main_branch_is_origin_head(self):
        self.head('trunk')
        d = self.decide('git push --force origin trunk')
        self.assertEqual(d and d.rule, 'git-push-main', d and d.reason)
        self.assertIn('to origin/trunk', d.reason)
        self.assertIsNone(self.decide('git push --force origin kit/wt'))

    def test_a_repointed_origin_head_never_frees_main(self):
        # <remote>/HEAD is local (`git remote set-head origin x`): main stays guarded with it
        self.head('kit/wt')
        for cmd in ('git push --force origin HEAD:main', 'git push origin :main', 'git push --force origin kit/wt'):
            with self.subTest(cmd):
                d = self.decide(cmd, sub=True)
                self.assertEqual(d and d.rule, 'git-push-main', d and d.reason)

    def test_a_head_naming_an_option_is_not_read(self):
        self.head('-f')
        d = self.decide('git push --force origin main')
        self.assertEqual(d and d.rule, 'git-push-main', d and d.reason)
        self.assertIn('to origin/main', d.reason)

    def test_a_file_without_push_block_still_refuses_a_forced_push(self):
        with open(os.path.join(self.w.main, '.kit.json'), 'w', encoding='utf-8') as f:
            f.write('{"repo": {"name": "demo"}}')
        d = self.decide('git push --force origin main')
        self.assertEqual(d and d.rule, 'git-push-main', d and d.reason)

    def test_a_broken_file_still_refuses_every_push(self):
        with open(os.path.join(self.w.main, '.kit.json'), 'w', encoding='utf-8') as f:
            f.write('{')
        d = self.decide('git push origin kit/wt')
        self.assertEqual(d and d.rule, 'git-push-main', d and d.reason)
        self.assertIn('does not load', d.reason)

    def test_the_machine_owner_email_guards_the_owner_identity(self):
        self.assertIsNone(self.decide('git commit --author="O <owner@example.com>" -m x', sub=True))
        self.w.user_config({'owner_email': 'owner@example.com'})
        d = self.decide('git commit --author="O <owner@example.com>" -m x', sub=True)
        self.assertEqual(d and d.rule, 'owner-identity', d and d.reason)


if __name__ == '__main__':
    unittest.main()
