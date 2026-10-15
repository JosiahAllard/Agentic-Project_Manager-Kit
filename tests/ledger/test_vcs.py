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
        so `git status -- rulings/LEDGER.md` is a plain question (the fake git answers no status
        pathspec inside an untracked folder)."""
        self.repo.write('rulings/README.md', 'rulings\n')
        self.repo.commit('rulings folder', 'rulings/README.md')

    def status(self):
        return self.repo.git('status', '--porcelain', '--untracked-files=all', '--ignored')

    def test_an_add_or_retire_leaves_no_untracked_file_in_the_ledgers_checkout(self):
        self.seed_rulings()
        before = self.status()
        for n in (1, 2):
            with write.locked(self.path):
                plan = self.prepare()
                got = self.add(decides='Entry %d.' % n)
                vcs.finish(plan, got.id, None, 'x', push=False)
            self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'rulings/LEDGER.md')
            self.assertEqual(self.status(), before)
        write.retire(self.path, rid(1), 'spent', 'why')
        new = [line.strip() for line in self.status().splitlines() if line not in before.splitlines()]
        self.assertEqual(new, ['M rulings/LEDGER.md'])
        self.assertEqual(self.repo.git('ls-files', '--', 'rulings').splitlines(),
                         ['rulings/LEDGER.md', 'rulings/README.md'])

    def test_the_lock_is_in_the_git_common_dir_one_per_file_under_any_spelling(self):
        common = os.path.normcase(os.path.realpath(self.repo.common_dir()))
        lock = write.lock_path(self.path)
        self.assertEqual(os.path.normcase(os.path.dirname(lock)), common)
        self.assertTrue(os.path.basename(lock).startswith('kit-ledger-ledger.md-'), lock)
        self.assertEqual(write.lock_path(self.path.replace('\\', '/').upper()), lock)
        other = write.lock_path(self.repo.path('rulings', 'other.md'))
        self.assertNotEqual(other, lock)
        self.add()
        self.assertTrue(os.path.isfile(lock))

    def test_a_ledger_in_a_linked_worktree_locks_in_the_shared_common_dir(self):
        self.seed_rulings()
        wt = self.repo.worktree('wt')
        lock = write.lock_path(os.path.join(wt, 'rulings', 'LEDGER.md'))
        common = os.path.normcase(os.path.realpath(self.repo.common_dir()))
        self.assertEqual(os.path.normcase(os.path.dirname(lock)), common)
        self.assertNotEqual(lock, write.lock_path(self.path))      # another file, another lock

    def test_a_git_that_cannot_say_where_the_checkout_is_refuses_and_writes_nothing(self):
        bad = write.gitrepo.Place(write.gitrepo.UNKNOWN, None, 'git did not answer: timed out', None, None)
        with mock.patch.object(write.gitrepo, 'classify', return_value=bad):
            with self.assertRaises(Refused) as cm:
                self.add()
        self.assertIn('nothing was written', str(cm.exception))
        self.assertIn('timed out', str(cm.exception))
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))

    def test_a_missing_ledger_folder_names_the_same_lock_before_and_after_it_is_made(self):
        folder = os.path.dirname(self.path)
        self.assertFalse(os.path.exists(folder))
        lock = write.lock_path(self.path)
        with write.locked(self.path):                   # taken while the folder is still missing
            self.assertTrue(write.held(self.path))
            self.assertFalse(os.path.exists(folder))    # the lock is in the common dir, not there
            self.assertEqual(self.add().id, rid(1))     # the write makes the folder, under the lock
            self.assertTrue(os.path.isdir(folder))
            self.assertEqual(write.lock_path(self.path), lock)
            self.assertTrue(write.held(self.path))      # one file identity before and after
        self.assertFalse(write.held(self.path))

    def test_held_is_false_when_not_held_when_the_lock_file_is_missing_and_when_git_cannot_say(self):
        self.assertFalse(os.path.exists(write.lock_path(self.path)))
        self.assertFalse(write.held(self.path))         # no lock file yet
        self.add()                                      # makes the lock file and gives the lock up
        self.assertTrue(os.path.isfile(write.lock_path(self.path)))
        self.assertFalse(write.held(self.path))         # the file exists, nobody holds it
        bad = write.gitrepo.Place(write.gitrepo.UNKNOWN, None, 'git did not answer: timed out', None, None)
        with write.locked(self.path):
            with mock.patch.object(write.gitrepo, 'classify', return_value=bad):
                self.assertFalse(write.held(self.path))  # nowhere to look: never held

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
