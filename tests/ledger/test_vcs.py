"""kitlib.ledger.vcs: `add --commit/--push` commits the ledger file alone, and `check --base` reads it.

Real git in a throwaway repository (tests/gitfixture.py). Ids are built by rid(n) (tests/scrub)."""
import json
import os
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.ledger import vcs, write
from kitlib.ledger.errors import Refused, Usage
from tests.gitfixture import TempRepo, fixture_env
from tests.ledger.test_writer import race, rid

MODEL = 'Test Model 1'


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

    def test_codex_ledger_commit_uses_shared_provider_format(self):
        plan = self.prepare(model='gpt-6.1-sol', provider='codex')
        got = self.add()
        vcs.finish(plan, got.id, None, 'Use this one.')
        self.assertIn('Codex gpt-6.1-sol <noreply@openai.com>', self.repo.git('log', '-1', '--format=%B'))

    def test_codex_identity_is_validated_before_the_ledger_is_written(self):
        with self.assertRaises(Usage):
            self.prepare(model=None, provider='codex')
        self.assertFalse(os.path.exists(self.path))

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

    def use_private_remote(self):
        """The bare remote is named `private`; `origin` points nowhere; repo.remote says `private`."""
        self.repo.git('remote', 'rename', 'origin', 'private')
        self.repo.git('remote', 'add', 'origin', os.path.join(self.repo.base, 'missing.git'))
        self.repo.write('.kit.json', '{"repo": {"remote": "private"}}\n')
        self.repo.commit('kit', '.kit.json')

    def test_push_goes_to_repo_remote_not_origin(self):
        self.use_private_remote()
        plan = self.prepare()
        got = self.add()
        lines = vcs.finish(plan, got.id, None, 'x', push=True)
        self.assertEqual(lines[-1], 'pushed private/main')
        self.assertEqual(self.repo.git('rev-parse', 'HEAD'),
                         self.repo.git('rev-parse', 'main', cwd=self.repo.remote))

    def test_default_base_follows_repo_remote(self):
        self.use_private_remote()
        self.assertEqual(vcs.default_base(self.path), 'private/main')

    def seed_rulings(self):
        """A committed file in rulings/ before the ledger's first commit: the folder is tracked,
        so `git status -- rulings/LEDGER.md` beside an untracked lock file is a plain question
        (the fake git answers no status pathspec inside an untracked folder)."""
        self.repo.write('rulings/README.md', 'rulings\n')
        self.repo.commit('rulings folder', 'rulings/README.md')

    def test_the_untracked_lock_file_is_neither_dirt_to_prepare_nor_committed(self):
        self.seed_rulings()
        lock = os.path.join(os.path.dirname(self.path), '.LEDGER.md.lock')
        for n in (1, 2):
            with write.locked(self.path):
                plan = self.prepare()               # the lock file is there: not a refusal
                self.assertTrue(os.path.isfile(lock))
                got = self.add(decides='Entry %d.' % n)
                vcs.finish(plan, got.id, None, 'x', push=False)
            self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'rulings/LEDGER.md')
        self.assertEqual(self.repo.git('ls-files', '--', 'rulings').splitlines(),
                         ['rulings/LEDGER.md', 'rulings/README.md'])
        self.assertEqual(self.repo.git('status', '--porcelain', '--', 'rulings'), '?? rulings/.LEDGER.md.lock')

    def test_two_processes_adding_and_committing_at_once_give_one_commit_per_entry(self):
        self.one_commit_per_entry('commit', self.env)

    def test_two_cli_adds_with_commit_at_once_give_one_commit_per_entry(self):
        home = os.path.join(self.repo.base, 'home')
        os.makedirs(kuser.config_dir(home))
        with open(kuser.config_file(home), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': self.path, 'ledger_repos': ['alpha']}, f)
        self.one_commit_per_entry('cli', dict(self.env, USERPROFILE=home, HOME=home))

    def one_commit_per_entry(self, mode, env):
        self.seed_rulings()
        before = int(self.repo.git('rev-list', '--count', 'HEAD'))
        got = race(self, self.repo.base, mode, self.path, ['first', 'second'], env=env)
        for rc, out, err in got:
            self.assertEqual(rc, 0, err)
        ids = sorted(out.splitlines()[0] for _, out, _ in got)
        self.assertEqual(ids, [rid(1), rid(2)])
        self.assertEqual(int(self.repo.git('rev-list', '--count', 'HEAD')), before + 2)
        for n, sha in enumerate(reversed(self.repo.git('rev-list', '-2', 'HEAD').split()), 1):
            subject = self.repo.git('log', '-1', '--format=%s', sha)
            self.assertTrue(subject.startswith('ledger: %s ' % rid(n)), subject)
            added = [line for line in self.repo.git('show', '--format=', sha).splitlines()
                     if line.startswith('+## ')]
            self.assertEqual(len(added), 1, added)          # its own entry and no other
            self.assertTrue(added[0].startswith('+## %s |' % rid(n)), added)
        self.assertEqual(self.repo.git('status', '--porcelain', '--', 'rulings/LEDGER.md'), '')

    def test_finish_commits_under_the_lock_and_pushes_after_it_is_given_up(self):
        plan = self.prepare()
        got = self.add()
        seen = []
        real = vcs.cmd.run

        def spy(args, *a, **kw):
            if args[:1] in (['commit'], ['push']):
                seen.append((args[0], write.held(self.path)))
            return real(args, *a, **kw)
        with mock.patch.object(vcs.cmd, 'run', spy):
            lines = vcs.finish(plan, got.id, None, 'x', push=True)
        self.assertEqual(lines[-1], 'pushed origin/main')
        self.assertEqual(seen, [('commit', True), ('push', False)])


if __name__ == '__main__':
    unittest.main()
