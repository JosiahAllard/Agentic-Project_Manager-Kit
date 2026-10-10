"""githooks/pre-commit and kitlib.ledger.precommit: inert unless .kit.json ledger.pre_commit_check
opts in; opted in, `tools/ledger.py check --base HEAD` failing refuses the commit
(docs/ledger.md "Pre-commit check"). Real git in a throwaway repository (tests/gitfixture.py) with
core.hooksPath set to the kit's githooks for the one commit. Ids are built by rid(n) (tests/scrub)."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from kitlib.ledger import precommit
from tests import gitfixture
from tests.realgit import real_git

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(KIT_ROOT, 'githooks')
SH = shutil.which('sh')
FAKE_PYTHON = '#!/bin/sh\necho ran > "$KIT_TEST_MARK"\n'


def rid(n):
    return 'R-%04d' % n


ENTRY = ('## %s | 2026-10-06 08:00Z | all | selected\nQ: q\nA: a\nDecides: %s\nSource: s\n'
         'Supersedes: -\n\n')


@real_git('hooks: the fake runs no hooks')
class PreCommitTest(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.ledger = self.repo.write('rulings/LEDGER.md', '# Rulings ledger\n\n' + ENTRY % (rid(1), 'Keep.'))
        self.repo.commit('ledger', 'rulings/LEDGER.md')
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(os.path.join(self.home, '.claude-pm-kit'))
        with open(os.path.join(self.home, '.claude-pm-kit', 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': self.ledger}, f)

    def opt_in(self, value=True):
        self.repo.write('.kit.json', json.dumps({'ledger': {'pre_commit_check': value}}))
        self.repo.commit('kit', '.kit.json')

    def rewrite_entry(self):
        with open(self.ledger, 'w', encoding='utf-8', newline='\n') as f:
            f.write('# Rulings ledger\n\n' + ENTRY % (rid(1), 'Changed.'))

    def commit_other(self):
        """Commit an unrelated file with the kit's hooks: (exit code, stderr)."""
        self.repo.write('other.txt', 'x\n')
        self.repo.git('add', '--', 'other.txt')
        env = gitfixture.fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        r = subprocess.run(['git', '-c', 'core.hooksPath=%s' % HOOKS.replace(os.sep, '/'), 'commit', '-q',
                            '-m', 'other', '--', 'other.txt'], cwd=self.repo.root, capture_output=True,
                           text=True, encoding='utf-8', errors='replace', env=env, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_not_opted_in_the_hook_lets_a_broken_ledger_through(self):
        self.opt_in(False)
        self.assertEqual(precommit.enabled(self.repo.root), (False, None))
        self.rewrite_entry()
        rc, out = self.commit_other()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.repo.git('log', '-1', '--format=%s'), 'other')

    def test_opted_in_a_rewritten_entry_refuses_the_commit(self):
        self.opt_in()
        self.assertEqual(precommit.enabled(self.repo.root), (True, None))
        rc, out = self.commit_other()
        self.assertEqual(rc, 0, out)
        self.rewrite_entry()
        rc, out = self.commit_other()
        self.assertNotEqual(rc, 0, out)
        self.assertIn('FAIL', out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.repo.git('log', '-1', '--format=%s'), 'other')
        self.assertEqual(precommit.command(self.repo.root)[2:5], ['check', '--base', 'HEAD'])

    def test_a_kit_json_the_kit_cannot_load_fails_open_with_a_note(self):
        self.repo.write('.kit.json', json.dumps({'ledger': {'no_such_key': True}}))
        on, why = precommit.enabled(self.repo.root)
        self.assertFalse(on)
        self.assertIn('no_such_key', why)
        code, message = precommit.main(self.repo.root)
        self.assertEqual(code, 0)
        self.assertIn('ledger check skipped', message)

    def test_no_kit_json_is_no_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(precommit.main(tmp), (0, None))

    def test_opted_in_a_kit_json_the_kit_cannot_load_refuses_the_commit(self):
        self.repo.write('.kit.json', json.dumps({'ledger': {'pre_commit_check': 'yes'}}))
        code, message = precommit.main(self.repo.root)
        self.assertEqual(code, 1)
        self.assertIn('cannot be loaded', message)
        rc, out = self.commit_other()
        self.assertNotEqual(rc, 0, out)
        self.assertIn('commit refused', out)
        self.assertEqual(self.repo.git('log', '-1', '--format=%s'), 'ledger')

    def test_any_load_failure_is_handled_like_a_config_error(self):
        with mock.patch.object(precommit.config, 'load', side_effect=ValueError('boom')):
            self.assertEqual(precommit.enabled(self.repo.root), (False, 'boom'))
            self.assertEqual(precommit.main(self.repo.root)[0], 0)
            self.repo.write('.kit.json', json.dumps({'ledger': {'pre_commit_check': True}}))
            code, message = precommit.main(self.repo.root)
        self.assertEqual(code, 1)
        self.assertIn('boom', message)


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
