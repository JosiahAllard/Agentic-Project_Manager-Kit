"""kitlib.ledger.vcs: `add --commit/--push` commits the ledger file alone, and `check --base` reads it.

Real git in a throwaway repository (tests/gitfixture.py). Ids are built by rid(n) (tests/scrub)."""
import os
import unittest

from kitlib.ledger import vcs, write
from kitlib.ledger.errors import Refused, Usage
from tests.gitfixture import TempRepo, fixture_env

MODEL = 'Test Model 1'


def rid(n):
    return 'R-%04d' % n


class VcsTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.env = fixture_env()
        self.path = self.repo.path('rulings', 'LEDGER.md')

    def add(self, **kw):
        args = dict(repo='alpha', prov='selected', q='q', a='a', decides='Use this one.', source='s',
                    when='2026-10-06 08:00Z')
        args.update(kw)
        return write.add(self.path, **args)

    def prepare(self, **kw):
        args = dict(branch_ok=False, model=MODEL, env=self.env)
        args.update(kw)
        return vcs.prepare(self.path, **args)

    def test_commit_records_only_the_ledger_with_subject_and_trailer(self):
        self.repo.write('other.txt', 'not mine\n')
        self.repo.git('add', '--', 'other.txt')            # someone else's staged file
        plan = self.prepare()
        got = self.add(decides='Use this one, please.')
        lines = vcs.finish(plan, got.id, None, 'Use this one, please.', push=False)
        self.assertTrue(lines[0].startswith('commit '))
        self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'rulings/LEDGER.md')
        msg = self.repo.git('log', '-1', '--format=%B')
        self.assertTrue(msg.startswith('ledger: %s Use this one, please.' % rid(1)), msg)
        self.assertIn('Co-Authored-By: Claude %s <noreply@anthropic.com>' % MODEL, msg)
        self.assertIn('other.txt', self.repo.git('diff', '--cached', '--name-only'))

    def test_title_overrides_and_long_decides_is_cut(self):
        plan = self.prepare()
        got = self.add(decides='word ' * 40)
        vcs.finish(plan, got.id, None, 'word ' * 40, push=False)
        subject = self.repo.git('log', '-1', '--format=%s')
        self.assertLessEqual(len(subject), len('ledger: %s ' % rid(1)) + vcs.TITLE_LIMIT)
        plan = self.prepare()
        got = self.add()
        vcs.finish(plan, got.id, 'Short title', 'Use this one.', push=False)
        self.assertEqual(self.repo.git('log', '-1', '--format=%s'), 'ledger: %s Short title' % rid(2))

    def test_push_sends_the_branch_to_origin(self):
        plan = self.prepare()
        got = self.add()
        lines = vcs.finish(plan, got.id, None, 'x', push=True)
        self.assertEqual(lines[-1], 'pushed origin/main')
        self.assertEqual(self.repo.git('rev-parse', 'HEAD'),
                         self.repo.git('rev-parse', 'main', cwd=self.repo.remote))

    def test_prepare_refuses_a_dirty_ledger(self):
        self.add()
        self.repo.commit('ledger', 'rulings/LEDGER.md')
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write('stray\n')
        with self.assertRaises(Refused) as cm:
            self.prepare()
        self.assertIn('uncommitted', str(cm.exception))

    def test_prepare_refuses_another_branch_unless_allowed_and_always_detached(self):
        self.repo.git('checkout', '-q', '-b', 'side')
        with self.assertRaises(Refused):
            self.prepare()
        self.prepare(branch_ok=True)
        self.repo.git('checkout', '-q', '--detach')
        with self.assertRaises(Refused):
            self.prepare(branch_ok=True)

    def test_main_branch_comes_from_the_ledger_repos_kit_json(self):
        self.repo.git('checkout', '-q', '-b', 'trunk')
        self.repo.write('.kit.json', '{"repo": {"main_branch": "trunk"}}\n')
        self.repo.commit('kit', '.kit.json')
        self.prepare()

    def test_prepare_needs_a_model_and_a_git_repo(self):
        with self.assertRaises(Usage):
            self.prepare(model='  ', env=dict(self.env, CLAUDE_AGENT_MODEL=''))
        self.prepare(model=None, env=dict(self.env, CLAUDE_AGENT_MODEL=MODEL))
        outside = os.path.join(self.repo.base, 'loose', 'LEDGER.md')
        with self.assertRaises(Refused):
            vcs.prepare(outside, branch_ok=False, model=MODEL, env=self.env)

    def test_a_failed_push_says_the_entry_is_written_and_committed(self):
        self.repo.git('remote', 'set-url', 'origin', os.path.join(self.repo.base, 'missing.git'))
        plan = self.prepare()
        got = self.add()
        with self.assertRaises(Refused) as cm:
            vcs.finish(plan, got.id, None, 'x', push=True)
        self.assertIn('NOT pushed', str(cm.exception))
        self.assertIn(rid(1), self.repo.git('log', '-1', '--format=%s'))

    def test_base_text_reads_the_ledger_at_a_revision(self):
        self.add()
        self.repo.commit('ledger', 'rulings/LEDGER.md')
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('fetch', '-q', 'origin')
        text, why = vcs.base_text(self.path, 'origin/main', env=self.env)
        self.assertIsNone(why)
        self.assertIn('## %s |' % rid(1), text)
        text, why = vcs.base_text(self.path, 'no-such-ref', env=self.env)
        self.assertIsNone(text)
        self.assertTrue(why)

    def test_default_base_is_origin_and_the_main_branch(self):
        self.assertEqual(vcs.default_base(self.path), 'origin/main')


if __name__ == '__main__':
    unittest.main()
