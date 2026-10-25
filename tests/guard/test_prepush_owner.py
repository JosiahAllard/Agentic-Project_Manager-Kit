"""The owner-only-path rule of githooks/pre-push (kitlib.guard.prepush with kitlib.guard.ownercommits), through git running the real hook."""
import json

from tests import gitfixture
from tests.home import result_drive
from tests.realgit import real_git
from tests.guard import prepush_case as pc
from tests.guard.prepush_case import AGENT, _with_kit_pushed


@real_git('git push runs the real pre-push hook (core.hooksPath)')
class OwnerTest(pc.PrePushCase):
    """A push to main is refused when a commit it adds changes an owner-only path and is not
    authored by the owner ("owner".email of the base config), even with a gate-OK record: a
    branch gate records its own tip, so a gated agent branch could otherwise go straight to main."""

    def setUp(self):
        # The remote's main starts at the .kit.json commit, pushed once in the template without
        # the hook (HookTest's first test pushes that same commit through the hook).
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=_with_kit_pushed,
                                        prepare_key='test_prepush.OwnerTest')
        self.addCleanup(self.repo.cleanup)
        result_drive(self)  # the gate-OK records (kitlib.records.gate_ok) go under it
        self.with_kit = self.repo.prepared
        self.assertEqual(self.remote_main(), self.with_kit)

    def commit_as(self, email, files, message='work'):
        """Commit `files` ({path: text}; None: already written) as `email`: the new HEAD."""
        for rel, text in files.items():
            if text is not None:
                self.repo.write(rel, text)
        self.repo.git('add', '--', *files)
        self.repo.git('-c', 'user.email=%s' % email, 'commit', '-q', '-m', message)
        return self.repo.git('rev-parse', 'HEAD')

    def test_an_agent_commit_to_an_owner_only_path_is_refused_with_a_record(self):
        sha = self.commit_as(AGENT, {'CLAUDE.md': 'agent\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('pre-push: refused, owner-only paths changed by a commit not authored by '
                      'the owner: %s by %s changes CLAUDE.md' % (sha[:10], AGENT), err)
        self.assertEqual(self.remote_main(), self.with_kit)

    def test_the_owners_commit_is_pushed(self):
        sha = self.commit_as(gitfixture.EMAIL, {'CLAUDE.md': 'owner\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), sha)

    def test_an_earlier_agent_commit_under_an_owner_tip_is_refused(self):
        bad = self.commit_as(AGENT, {'.github/workflows/ci.yml': 'x\n'})
        tip = self.commit_as(gitfixture.EMAIL, {'a.txt': 'a\n'})
        self.passed(tip)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('%s by %s changes .github/workflows/ci.yml' % (bad[:10], AGENT), err)

    def test_a_pushed_agent_branch_does_not_hide_its_commits(self):
        self.repo.git('checkout', '-q', '-b', 'kit/x')
        sha = self.commit_as(AGENT, {'CLAUDE.md': 'agent\n'})
        self.assertEqual(self.push('origin', 'kit/x')[0], 0)          # a branch push is not checked
        self.passed(sha)
        code, err = self.push('origin', 'kit/x:main')
        self.assertEqual(code, 1, err)
        self.assertIn('%s by %s changes CLAUDE.md' % (sha[:10], AGENT), err)
        self.assertEqual(self.remote_main(), self.with_kit)

    def test_the_push_cannot_name_its_own_owner(self):
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': True},
                                                 'owner': {'email': AGENT}}))
        sha = self.commit_as(AGENT, {'.kit.json': None})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('%s by %s changes .kit.json' % (sha[:10], AGENT), err)

    def test_an_agent_commit_elsewhere_is_pushed(self):
        sha = self.commit_as(AGENT, {'a.txt': 'a\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), sha)

    def test_an_agent_change_then_revert_of_an_owner_only_path_is_pushed(self):
        edit = self.commit_as(AGENT, {'CLAUDE.md': 'agent\n'})
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit)
        tip = self.repo.git('rev-parse', 'HEAD')
        self.passed(tip)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), tip)

    def test_a_base_without_an_owner_refuses_every_owner_only_change(self):
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': True}}))
        base = self.commit_as(gitfixture.EMAIL, {'.kit.json': None})
        self.passed(base)
        self.assertEqual(self.push('origin', 'main')[0], 0)          # judged by the old base
        sha = self.commit_as(gitfixture.EMAIL, {'CLAUDE.md': 'owner\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('identity is not configured', err)


if __name__ == '__main__':
    unittest.main()
