"""githooks/pre-commit and kitlib.ledger.precommit: inert unless .kit.json ledger.pre_commit_check
opts in; opted in, a commit that changes the configured ledger file (or the standing digest beside
it) runs `tools/ledger.py check --base HEAD`, and a failure refuses the commit; any other commit
passes without the check (docs/ledger.md "Pre-commit check"). Real git in a throwaway repository
(tests/gitfixture.py) with core.hooksPath set to the kit's githooks for the one commit. Ids are
built by rid(n) (tests/scrub)."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from kitlib.githooks import precommit as dispatch
from kitlib.ledger import precommit
from tests import gitfixture
from tests.realgit import real_git
from tests.home import machine_config

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(KIT_ROOT, 'githooks')
SH = shutil.which('sh')
FAKE_PYTHON = '#!/bin/sh\necho ran > "$KIT_TEST_MARK"\n'
CHECK_RAN = 'ledger: '          # the summary line tools/ledger.py check always prints


def rid(n):
    return 'R-%04d' % n


ENTRY = ('## %s | 2026-10-06 08:00Z | all | selected\nQ: q\nA: a\nDecides: %s\nSource: s\n'
         'Supersedes: -\n\n')
HEADER = '# Rulings ledger\n\n'


class FakeCommit:
    def __init__(self, root, staged=()):
        self.root = root
        self._staged = tuple(staged)

    def staged(self):
        return self._staged


@real_git('hooks: the fake runs no hooks')
class PreCommitTest(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.ledger = self.repo.write('rulings/LEDGER.md', HEADER + ENTRY % (rid(1), 'Keep.'))
        self.repo.commit('ledger', 'rulings/LEDGER.md')
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(self.home)
        self.machine_config({'ledger_path': self.ledger})

    def machine_config(self, obj):
        machine_config(self.home, obj, case=self)

    def opt_in(self, value=True):
        self.repo.write('.kit.json', json.dumps({'ledger': {'pre_commit_check': value}}))
        self.repo.commit('kit', '.kit.json')

    def write_ledger(self, *entries):
        with open(self.ledger, 'w', encoding='utf-8', newline='\n') as f:
            f.write(HEADER + ''.join(entries))

    def rewrite_entry(self):
        self.write_ledger(ENTRY % (rid(1), 'Changed.'))

    def append_entry(self):
        self.write_ledger(ENTRY % (rid(1), 'Keep.'), ENTRY % (rid(2), 'More.'))

    def hook_commit(self, *args):
        """`git commit <args>` with the kit's hooks: (exit code, stdout and stderr)."""
        env = gitfixture.fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        r = subprocess.run(['git', '-c', 'core.hooksPath=%s' % HOOKS.replace(os.sep, '/'), 'commit', '-q']
                           + list(args), cwd=self.repo.root, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', env=env, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def commit_other(self):
        """Commit an unrelated file with the kit's hooks."""
        self.repo.write('other.txt', 'x\n')
        self.repo.git('add', '--', 'other.txt')
        return self.hook_commit('-m', 'other', '--', 'other.txt')

    def last(self):
        return self.repo.git('log', '-1', '--format=%s')

    def test_not_opted_in_the_hook_lets_a_broken_ledger_through(self):
        self.opt_in(False)
        self.assertEqual(precommit.enabled(self.repo.root), (False, None))
        self.rewrite_entry()
        rc, out = self.hook_commit('-m', 'bad', '--', 'rulings/LEDGER.md')
        self.assertEqual(rc, 0, out)
        self.assertNotIn(CHECK_RAN, out)
        self.assertEqual(self.last(), 'bad')

    def test_opted_in_a_staged_rewritten_entry_refuses_the_commit(self):
        self.opt_in()
        self.assertEqual(precommit.enabled(self.repo.root), (True, None))
        self.rewrite_entry()
        self.repo.git('add', '--', 'rulings/LEDGER.md')
        rc, out = self.hook_commit('-m', 'bad')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('FAIL', out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.last(), 'kit')
        self.assertEqual(precommit.command(self.repo.root)[2:5], ['check', '--base', 'HEAD'])

    def test_opted_in_commit_of_the_ledger_by_path_is_checked(self):
        self.opt_in()
        self.rewrite_entry()
        rc, out = self.hook_commit('-m', 'bad', '--', 'rulings/LEDGER.md')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.last(), 'kit')

    def test_opted_in_commit_all_is_checked(self):
        self.opt_in()
        self.rewrite_entry()
        rc, out = self.hook_commit('-a', '-m', 'bad')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.last(), 'kit')

    def test_opted_in_an_appended_entry_is_checked_and_commits(self):
        self.opt_in()
        self.append_entry()
        rc, out = self.hook_commit('-m', 'append', '--', 'rulings/LEDGER.md')
        self.assertEqual(rc, 0, out)
        self.assertIn('ledger: 2 entries, OK', out)
        self.assertEqual(self.last(), 'append')

    def test_opted_in_a_commit_not_touching_the_ledger_skips_the_check(self):
        self.opt_in()
        self.rewrite_entry()                          # broken, but not in this commit
        rc, out = self.commit_other()
        self.assertEqual(rc, 0, out)
        self.assertNotIn(CHECK_RAN, out)
        self.assertEqual(self.last(), 'other')

    def test_opted_in_a_staged_bad_ledger_with_a_good_working_file_is_refused(self):
        """The check reads the working file: staged and working copies must agree, or a bad
        staged ledger would be judged by a good working one."""
        self.opt_in()
        self.rewrite_entry()
        self.repo.git('add', '--', 'rulings/LEDGER.md')
        self.write_ledger(ENTRY % (rid(1), 'Keep.'))
        rc, out = self.hook_commit('-m', 'bad')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('unstaged changes', out)
        self.assertIn('commit refused', out)
        self.assertNotIn(CHECK_RAN, out)
        self.assertEqual(self.last(), 'kit')

    def test_opted_in_a_staged_standing_digest_is_checked(self):
        self.opt_in()
        self.repo.write('rulings/STANDING.md', '# Standing\n\n- see %s\n' % rid(9))
        self.repo.git('add', '--', 'rulings/STANDING.md')
        rc, out = self.hook_commit('-m', 'digest')
        self.assertNotEqual(rc, 0, out)
        self.assertIn(rid(9), out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.last(), 'kit')

    def test_opted_in_a_staged_digest_with_an_unstaged_ledger_change_is_refused(self):
        """The check reads the working ledger too, so it must be what the commit holds."""
        self.opt_in()
        self.repo.write('rulings/STANDING.md', '# Standing\n\n- see %s\n' % rid(1))
        self.repo.git('add', '--', 'rulings/STANDING.md')
        self.append_entry()                           # working ledger only, not staged
        rc, out = self.hook_commit('-m', 'digest')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('unstaged changes', out)
        self.assertIn('rulings/LEDGER.md', out)
        self.assertIn('commit refused', out)
        self.assertNotIn(CHECK_RAN, out)
        self.assertEqual(self.last(), 'kit')

    def test_opted_in_a_staged_ledger_with_an_unstaged_digest_change_is_refused(self):
        self.opt_in()
        self.repo.write('rulings/STANDING.md', '# Standing\n\n- see %s\n' % rid(1))
        self.repo.commit('digest', 'rulings/STANDING.md')
        self.append_entry()
        self.repo.git('add', '--', 'rulings/LEDGER.md')
        self.repo.write('rulings/STANDING.md', '# Standing\n\n- see %s\n' % rid(2))
        rc, out = self.hook_commit('-m', 'append')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('unstaged changes', out)
        self.assertIn('rulings/STANDING.md', out)
        self.assertIn('commit refused', out)
        self.assertNotIn(CHECK_RAN, out)
        self.assertEqual(self.last(), 'digest')

    def test_opted_in_no_ledger_path_refuses(self):
        self.opt_in()
        self.machine_config({})
        rc, out = self.commit_other()
        self.assertNotEqual(rc, 0, out)
        self.assertIn('no ledger_path', out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.last(), 'kit')

    def test_a_kit_json_the_kit_cannot_load_fails_open_with_a_note(self):
        self.repo.write('.kit.json', json.dumps({'ledger': {'no_such_key': True}}))
        on, why = precommit.enabled(self.repo.root)
        self.assertFalse(on)
        self.assertIn('no_such_key', why)
        code, message = precommit.check(FakeCommit(self.repo.root), self.home)
        self.assertEqual(code, 0)
        self.assertIn('ledger check skipped', message)

    def test_opted_in_a_kit_json_the_kit_cannot_load_refuses_the_commit(self):
        self.repo.write('.kit.json', json.dumps({'ledger': {'pre_commit_check': 'yes'}}))
        code, message = precommit.check(FakeCommit(self.repo.root), self.home)
        self.assertEqual(code, 1)
        self.assertIn('cannot be loaded', message)
        rc, out = self.commit_other()
        self.assertNotEqual(rc, 0, out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.last(), 'ledger')

    def test_any_load_failure_is_handled_like_a_config_error(self):
        with mock.patch.object(precommit.config, 'load', side_effect=ValueError('boom')):
            self.assertEqual(precommit.enabled(self.repo.root), (False, 'boom'))
            self.assertEqual(precommit.check(FakeCommit(self.repo.root), self.home)[0], 0)
            self.repo.write('.kit.json', json.dumps({'ledger': {'pre_commit_check': True}}))
            code, message = precommit.check(FakeCommit(self.repo.root), self.home)
        self.assertEqual(code, 1)
        self.assertIn('boom', message)

    def test_a_check_that_cannot_start_refuses(self):
        self.opt_in()
        self.append_entry()
        self.repo.git('add', '--', 'rulings/LEDGER.md')
        with mock.patch.object(precommit.subprocess, 'call', side_effect=OSError('no python')):
            code, message = dispatch.main(self.repo.root, checks=(
                lambda c: precommit.check(c, self.home),))
        self.assertEqual(code, 1)
        self.assertIn('no python', message)
        self.assertIn('commit refused', message)


class CommandTest(unittest.TestCase):
    """The check the hook runs names no repository: `check` has no `--root`."""

    def test_the_command_is_check_against_head_and_nothing_more(self):
        got = precommit.command(os.path.join('some', 'root'))
        self.assertEqual(got[1:], [precommit.LEDGER_TOOL, 'check', '--base', 'HEAD'])
        self.assertNotIn('--root', got)


class TargetsTest(unittest.TestCase):
    """Which staged paths are the ledger: no git, no hook."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, 'repo')
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        os.makedirs(self.root)
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'ledger': {'pre_commit_check': True}}, f)

    def ledger_at(self, path):
        machine_config(self.home, {'ledger_path': path}, case=self)

    def test_the_ledger_and_its_digest_are_targets(self):
        ledger = os.path.join(self.root, 'rulings', 'LEDGER.md')
        self.ledger_at(ledger)
        paths = precommit.targets(self.home)
        self.assertEqual(paths[:2], (ledger, os.path.join(self.root, 'rulings', 'STANDING.md')))
        self.assertEqual(len(paths), 2 + 5)        # and the five rulebooks
        staged = ['a.txt', 'rulings/LEDGER.md', 'rulings/STANDING.md', 'rulings/other.md']
        self.assertEqual(precommit.staged_targets(self.root, staged, paths),
                         ['rulings/LEDGER.md', 'rulings/STANDING.md'])

    @unittest.skipUnless(os.name == 'nt', 'case-insensitive paths are a Windows matter')
    def test_case_and_separators_do_not_hide_the_ledger(self):
        ledger = os.path.join(self.root, 'rulings', 'LEDGER.md')
        self.assertEqual(precommit.staged_targets(self.root, ['Rulings/ledger.MD'], (ledger,)),
                         ['Rulings/ledger.MD'])

    def test_both_targets_under_the_root_are_pathspecs(self):
        ledger = os.path.join(self.root, 'rulings', 'LEDGER.md')
        paths = (ledger, os.path.join(self.root, 'rulings', 'STANDING.md'))
        self.assertEqual(precommit.under_root(self.root, paths),
                         ['rulings/LEDGER.md', 'rulings/STANDING.md'])
        self.assertEqual(precommit.under_root(self.root, (os.path.join(self.tmp, 'LEDGER.md'),)), [])

    def test_realpath_is_tried_only_for_a_target_file_name(self):
        ledger = os.path.join(self.root, 'rulings', 'LEDGER.md')
        real = os.path.realpath
        calls = []

        def spy(p):
            calls.append(p)
            return real(p)
        with mock.patch.object(precommit.os.path, 'realpath', side_effect=spy):
            paths = (ledger,)
            before = len(calls)
            self.assertEqual(precommit.staged_targets(self.root, ['a.txt', 'b/c.md', 'rulings/LEDGER.md'],
                                                      paths), ['rulings/LEDGER.md'])
            self.assertEqual(len(calls) - before, 1)      # the targets, once; no staged path resolved
            calls.clear()
            self.assertEqual(precommit.staged_targets(self.root, ['other/LEDGER.md'], paths), [])
            self.assertEqual(len(calls), 2)               # the target, and the one same-named path

    def test_a_ledger_outside_the_repository_is_never_staged_here(self):
        self.ledger_at(os.path.join(self.tmp, 'elsewhere', 'LEDGER.md'))
        commit = FakeCommit(self.root, ['LEDGER.md', 'rulings/LEDGER.md'])
        with mock.patch.object(precommit.subprocess, 'call') as run:
            self.assertEqual(precommit.check(commit, self.home), (0, None))
        run.assert_not_called()

    def test_not_staged_runs_no_check_and_no_further_git(self):
        self.ledger_at(os.path.join(self.root, 'LEDGER.md'))
        with mock.patch.object(precommit.subprocess, 'call') as run, \
                mock.patch.object(precommit, 'unstaged') as diff:
            self.assertEqual(precommit.check(FakeCommit(self.root, ['a.txt']), self.home), (0, None))
        run.assert_not_called()
        diff.assert_not_called()

    def test_not_opted_in_reads_neither_machine_config_nor_staged_files(self):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'ledger': {'pre_commit_check': False}}, f)
        commit = mock.Mock(root=self.root)
        with mock.patch.object(precommit, 'targets') as targets:
            self.assertEqual(precommit.check(commit, self.home), (0, None))
        targets.assert_not_called()
        commit.staged.assert_not_called()

    def test_no_kit_json_is_no_check(self):
        os.remove(os.path.join(self.root, '.kit.json'))
        self.assertEqual(precommit.check(mock.Mock(root=self.root), self.home), (0, None))

    def test_an_unreadable_machine_config_refuses(self):
        machine_config(self.home, '{', case=self)
        code, message = precommit.check(FakeCommit(self.root, ['a.txt']), self.home)
        self.assertEqual(code, 1)
        self.assertIn('commit refused', message)


@unittest.skipUnless(SH, 'no sh on PATH')
class FrontEndTest(unittest.TestCase):
    """githooks/pre-commit's shell front end starts python only when .kit.json names
    pre_commit_check. A fake `python` first on PATH writes a marker when it is started."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, 'repo')
        self.fake = os.path.join(self.tmp, 'bin')
        os.makedirs(self.root)
        os.makedirs(self.fake)
        path = os.path.join(self.fake, 'python')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(FAKE_PYTHON)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.mark = os.path.join(self.tmp, 'mark.txt')

    def kit_json(self, text):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as f:
            f.write(text)

    def hook(self, path):
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PATH'}
        env['PATH'] = path
        env['KIT_TEST_MARK'] = self.mark
        r = subprocess.run([SH, os.path.join(HOOKS, 'pre-commit').replace(os.sep, '/')], cwd=self.root,
                           env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=60)
        return r.returncode, r.stdout + r.stderr, os.path.exists(self.mark)

    def with_fake(self):
        return self.fake + os.pathsep + os.path.dirname(SH)

    def test_no_kit_json_starts_no_python(self):
        self.assertEqual(self.hook(self.with_fake()), (0, '', False))

    def test_a_kit_json_not_naming_the_key_starts_no_python(self):
        self.kit_json(json.dumps({'repo': {'main_branch': 'main'}}, indent=1))
        self.assertEqual(self.hook(self.with_fake()), (0, '', False))

    def test_a_kit_json_naming_the_key_starts_python(self):
        self.kit_json(json.dumps({'ledger': {'pre_commit_check': False}}, indent=1))
        rc, out, ran = self.hook(self.with_fake())
        self.assertEqual(rc, 0, out)
        self.assertTrue(ran, out)

    def test_the_key_on_a_last_line_with_no_newline_is_read(self):
        self.kit_json('{"ledger": {"pre_commit_check": false}}')
        self.assertTrue(self.hook(self.with_fake())[2])

    def test_no_python_on_path_is_a_skip_with_a_note(self):
        bare = os.path.dirname(SH)
        if shutil.which('python', path=bare):
            self.skipTest('python sits beside sh')
        self.kit_json('{"ledger": {"pre_commit_check": true}}')
        rc, out, ran = self.hook(bare)
        self.assertEqual(rc, 0, out)
        self.assertFalse(ran)
        self.assertIn('no python on PATH', out)


if __name__ == '__main__':
    unittest.main()
