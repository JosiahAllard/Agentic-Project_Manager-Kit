"""The render step of the kit's pre-commit hook (kitlib.githooks.render; docs/content.md "Render on
commit"): in a kit checkout, a commit that stages a render source gets the rendered files made
current and staged into the same commit; anything else starts no process. The shell front end
(githooks/pre-commit) starts python for it only in a kit checkout with a render source staged."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.content import oncommit
from kitlib.githooks import precommit, render
from tests import gitfixture
from tests.commit.test_commit import RENDER_STUB
from tests.realgit import real_git

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(KIT_ROOT, 'githooks')
SH = shutil.which('sh')
GIT = shutil.which('git')
FAKE_PYTHON = '#!/bin/sh\necho ran > "$KIT_TEST_MARK"\n'


class FakeCommit:
    def __init__(self, root, staged):
        self.root, self._staged = root, staged

    def staged(self):
        return self._staged


class Dispatch(unittest.TestCase):
    def test_the_render_step_runs_before_the_ledger_check(self):
        self.assertEqual(precommit.CHECKS[0], render.check)

    def test_no_source_staged_starts_nothing(self):
        with mock.patch.object(oncommit, 'render') as run:
            self.assertEqual(render.check(FakeCommit(KIT_ROOT, ['docs/a.md', 'kitlib/x.py'])), (0, None))
        run.assert_not_called()

    def test_a_repository_without_the_kit_layout_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(oncommit, 'render') as run:
            self.assertEqual(render.check(FakeCommit(tmp, ['content/roles/x.md', 'kit.json'])), (0, None))
        run.assert_not_called()

    def test_the_sources(self):
        self.assertTrue(oncommit.wants_render(['a.py', 'content/skills/x/SKILL.md']))
        self.assertTrue(oncommit.wants_render(['kit.json']))
        self.assertTrue(oncommit.wants_render(['hosts.json']))
        self.assertFalse(oncommit.wants_render(['docs/content.md', 'hosts/claude/shell/agents/pm.md',
                                                'contents/x', 'tools/kit.json']))


@real_git('the render step stages into the index git gives the hook')
class Staging(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('content/roles/x.md', 'one\n')
        self.repo.write('out/old.txt', 'stale\n')
        self.repo.commit('kit layout', 'content/roles/x.md', 'out/old.txt')
        self.repo.write('tools/render_content.py', RENDER_STUB)

    def staged(self):
        return sorted(self.repo.git('diff', '--cached', '--name-status', '--no-renames').splitlines())

    def test_a_staged_role_edit_stages_what_the_render_changed(self):
        self.repo.write('content/roles/x.md', 'two\n')
        self.repo.git('add', '--', 'content/roles/x.md')
        code, message = render.check(precommit.Commit(self.repo.root))
        self.assertEqual(code, 0, message)
        self.assertEqual(message, 'pre-commit: rendered and staged: out/old.txt, out/role.txt')
        self.assertEqual(self.staged(), ['A\tout/role.txt', 'D\tout/old.txt', 'M\tcontent/roles/x.md'])

    def test_a_failed_render_refuses_naming_the_command(self):
        self.repo.write('tools/render_content.py', 'import sys\nsys.exit(2)\n')
        self.repo.write('content/roles/x.md', 'two\n')
        self.repo.git('add', '--', 'content/roles/x.md')
        code, message = render.check(precommit.Commit(self.repo.root))
        self.assertEqual(code, 1)
        self.assertIn('tools/render_content.py exited 2', message)
        self.assertIn('render_content.py -C ', message)
        self.assertIn('commit refused', message)

    @unittest.skipUnless(SH and shutil.which('python'), 'no sh or python on PATH')
    def test_a_plain_git_commit_through_the_hook_carries_the_rendered_files(self):
        self.repo.git('config', 'core.hooksPath', HOOKS.replace(os.sep, '/'))
        self.repo.write('content/roles/x.md', 'two\n')
        self.repo.git('add', '--', 'content/roles/x.md')
        self.repo.git('commit', '-q', '-m', 'edit x')
        shown = sorted(self.repo.git('show', '--name-status', '--format=', 'HEAD').splitlines())
        self.assertEqual(shown, ['A\tout/role.txt', 'D\tout/old.txt', 'M\tcontent/roles/x.md'])


@unittest.skipUnless(SH and GIT, 'no sh or git on PATH')
class FrontEnd(unittest.TestCase):
    """githooks/pre-commit starts python for the render only in a kit checkout with a render source
    staged. A fake `python` first on PATH writes a marker when it is started."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('content/roles/x.md', 'one\n')
        self.repo.write('tools/render_content.py', 'pass\n')
        self.fake = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, self.fake)
        path = os.path.join(self.fake, 'python')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(FAKE_PYTHON)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.mark = os.path.join(self.fake, 'mark.txt')

    def hook(self, path):
        env = gitfixture.fixture_env({}) if hasattr(gitfixture, 'fixture_env') else dict(os.environ)
        env = {k: v for k, v in env.items() if k.upper() != 'PATH'}
        env['PATH'] = path
        env['KIT_TEST_MARK'] = self.mark
        r = subprocess.run([SH, os.path.join(HOOKS, 'pre-commit').replace(os.sep, '/')], cwd=self.repo.root,
                           env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=60)
        return r.returncode, r.stdout + r.stderr, os.path.exists(self.mark)

    def tools(self, fake=True):
        dirs = [os.path.dirname(SH), os.path.dirname(GIT)]
        return os.pathsep.join(([self.fake] if fake else []) + dirs)

    def test_a_staged_source_starts_python(self):
        self.repo.git('add', '--', 'content/roles/x.md')
        rc, out, ran = self.hook(self.tools())
        self.assertEqual(rc, 0, out)
        self.assertTrue(ran, out)

    def test_another_staged_file_starts_no_python(self):
        self.repo.write('a.txt', 'a\n')
        self.repo.git('add', '--', 'a.txt')
        self.assertEqual(self.hook(self.tools()), (0, '', False))

    def test_a_repository_without_the_render_tool_starts_no_python(self):
        os.remove(os.path.join(self.repo.root, 'tools', 'render_content.py'))
        self.repo.git('add', '--', 'content/roles/x.md')
        self.assertEqual(self.hook(self.tools()), (0, '', False))

    def test_no_python_with_a_source_staged_refuses_with_the_command(self):
        if any(shutil.which('python', path=d) for d in (os.path.dirname(SH), os.path.dirname(GIT))):
            self.skipTest('python sits beside sh or git')
        self.repo.git('add', '--', 'content/roles/x.md')
        rc, out, ran = self.hook(self.tools(fake=False))
        self.assertEqual(rc, 1, out)
        self.assertIn('python tools/render_content.py --write', out)
        self.assertIn('commit refused', out)


if __name__ == '__main__':
    unittest.main()
