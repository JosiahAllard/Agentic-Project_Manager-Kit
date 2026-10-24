"""kitlib.git.stray and tools/checkout_guard.py: the main checkout's stray changes and the guard's
exit codes, against real throwaway repositories.

Ported from the PM kit's test_checkout_guard.py (Classify's CLI and hung-git cases,
MainCheckoutUntracked, PrintSafe); its stray-writer attribution tests went with the attribution
(PM ruling, see kitlib/git/stray.py). The fixture is a
main work tree with linked worktrees w and v INSIDE it at .claude/worktrees/, the real shape.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.git import cmd, repo, stray
from tests import gitfixture
from tools import checkout_guard

DOC = 'docs/a.md'
BASE = 'line one\nline two\n'
RECORD = 'docs/backlog/X-ONE.md'


def same(a, b):
    return repo.norm(a) == repo.norm(b)


def has_path(text, path):
    """True when `path` (any case, either slash) is named in `text`."""
    return os.path.normcase(os.path.normpath(path)) in os.path.normcase(text.replace('/', os.sep))


class Spawns:
    """Counts every git child kitlib.git.cmd starts, with its argv."""

    def __enter__(self):
        self.argvs = []
        real = cmd.spawner()

        def counted(argv, *a, **kw):
            self.argvs.append(list(argv))
            return real(argv, *a, **kw)
        self._patch = mock.patch.object(cmd, '_run', counted)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    @property
    def count(self):
        return len(self.argvs)


def _prepare(r):
    """The committed files, built once per process; each test's repository is a copy."""
    r.write('.gitignore', '.claude/worktrees/\n')
    r.write(DOC, BASE)
    r.write(RECORD, 'id: X-ONE\n')
    r.commit('docs', '.gitignore', DOC, RECORD)


class Fixture(unittest.TestCase):
    """main/ holds a committed .gitignore (.claude/worktrees/), docs/a.md and a backlog record;
    w and v are linked worktrees of it."""

    def setUp(self):
        self.r = gitfixture.TempRepo(prepare=_prepare, prepare_key=('test_stray.Fixture', DOC, BASE,
                                                                     RECORD))
        self.addCleanup(self.r.cleanup)
        self.main = self.r.root
        self.w = self.r.worktree('w', 'area/w')
        self.v = self.r.worktree('v', 'area/v')
        self.outside = tempfile.mkdtemp(prefix='kit-stray-outside-')
        self.addCleanup(gitfixture.rmtree, self.outside)

    def put(self, tree, rel, text):
        return self.r.write(rel, text, root=tree)

    def stray(self, text, name=DOC):
        self.put(self.main, name, text)

    def check(self, path, **kw):
        code, lines = stray.check(path, **kw)
        return code, '\n'.join(lines)

    def append(self, rel, text, tree=None):
        with open(os.path.join(tree or self.main, rel), 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(text)


class Changes(Fixture):
    """stray.changes: found from either tree; ignored files are not strays; failure is an error."""

    def test_an_untracked_file_in_main_is_found_from_main(self):
        self.stray('x', name='oops.txt')
        self.assertEqual(stray.changes(self.main), ([('??', 'oops.txt')], None))

    def test_an_untracked_file_in_main_is_found_from_a_worktree(self):
        # The defect: the shell stands in the linked worktree, the file landed in main.
        self.stray('x', name='oops.txt')
        self.assertEqual(stray.changes(self.w), ([('??', 'oops.txt')], None))

    def test_a_file_in_a_new_folder_is_named_as_the_file(self):
        self.stray('x', name='src/Approach.java')
        self.assertEqual(stray.changes(self.w), ([('??', 'src/Approach.java')], None))

    def test_an_ignored_file_or_folder_is_not_found(self):
        self.append('.gitignore', 'ignored.txt\nbuild/\n')
        self.r.commit('ignore', '.gitignore')
        self.stray('x', name='ignored.txt')
        self.stray('x', name='build/out.class')
        self.assertEqual(stray.changes(self.w), ([], None))

    def test_a_clean_main_checkout_finds_nothing(self):
        self.assertEqual(stray.changes(self.w), ([], None))

    def test_outside_any_repository_is_an_error_not_a_silent_clean(self):
        entries, error = stray.changes(self.outside)
        self.assertEqual(entries, [])
        self.assertIn('could not find the main checkout', error)

    def test_it_classifies_its_path_once(self):
        self.stray(BASE + 'x\n')
        with Spawns() as spawns:
            stray.changes(self.w)
        self.assertEqual(len([a for a in spawns.argvs if 'rev-parse' in a]), 1, spawns.argvs)


class Check(Fixture):
    """stray.check: [FAIL] (1) naming each stray, 0 when clean, WARNING when it could not tell."""

    RESTORE_POINTER = 'python <kit>/tools/stray_restore.py -C <main checkout> (a dry run; --apply '
    REMOVE_ADVICE = 'else remove it (owner if a delete is refused)'
    STRAY_ADVICE = 'a write went to the main checkout; first ' + RESTORE_POINTER

    def test_the_fail_line_points_at_the_safe_restore_before_a_delete(self):
        # KIT-STRAY-FAIL-RESTORE-POINTER: tools/stray_restore.py discards only what a branch tip
        # holds, so the FAIL line names it, and its real form, ahead of a plain delete.
        self.stray('x', name='oops.txt')
        code, out = self.check(self.w)
        self.assertEqual(code, 1, out)
        self.assertIn(self.RESTORE_POINTER, out)
        self.assertLess(out.index(self.RESTORE_POINTER), out.index(self.REMOVE_ADVICE), out)

    def test_a_stray_untracked_file_fails_and_names_the_path(self):
        self.stray('x', name='src/Approach.java')
        code, out = self.check(self.w)
        self.assertEqual(code, 1, out)
        self.assertIn('[FAIL]', out)
        self.assertIn('src/Approach.java (untracked)', out)
        self.assertIn(self.STRAY_ADVICE, out)
        self.assertIn(self.REMOVE_ADVICE, out)

    def test_a_modified_tracked_file_in_main_fails_from_a_worktree(self):
        self.append('.gitignore', '# stray edit\n')
        code, out = self.check(self.w)
        self.assertEqual(code, 1, out)
        self.assertIn('.gitignore (uncommitted', out)

    def test_a_deleted_tracked_file_in_main_fails_from_a_worktree(self):
        os.remove(os.path.join(self.main, RECORD))
        code, out = self.check(self.w)
        self.assertEqual(code, 1, out)
        self.assertIn(RECORD, out)

    def test_a_clean_main_checkout_passes(self):
        code, out = self.check(self.w)
        self.assertEqual((code, out), (0, stray.CLEAN))

    def test_an_ignored_state_note_passes(self):
        self.append('.gitignore', 'docs/ops/state.local.md\n')
        self.r.commit('ignore the state note', '.gitignore')
        self.stray('state\n', name='docs/ops/state.local.md')
        code, out = self.check(self.w)
        self.assertEqual(code, 0, out)
        self.assertNotIn('state.local.md', out)

    def test_a_landed_merge_commit_in_main_passes_from_either_tree(self):
        self.put(self.w, 'feature.txt', 'x\n')
        self.r.commit('feature', 'feature.txt', cwd=self.w)
        self.r.git('merge', '-q', '--no-ff', 'area/w', '-m', 'Merge area/w')
        for where in (self.main, self.w):
            code, out = self.check(where)
            self.assertEqual(code, 0, out)

    def conflicted_merge_in_main(self):
        self.append(RECORD, 'branch side\n', tree=self.w)
        self.r.commit('branch side', RECORD, cwd=self.w)
        self.append(RECORD, 'main side\n')
        self.r.commit('main side', RECORD)
        out = self.r.git('merge', '--no-ff', 'area/w', check=False)
        self.assertTrue(os.path.isfile(os.path.join(self.main, '.git', 'MERGE_HEAD')), out)

    def test_a_merge_in_progress_in_main_fails_even_under_the_flag(self):
        # The lander merges in the _landing worktree, so the flag exempts no tracked change.
        self.conflicted_merge_in_main()
        code, out = self.check(self.main, allow_tracked=True)
        self.assertEqual(code, 1, out)
        self.assertIn(RECORD, out)

    def test_merge_head_in_main_does_not_hide_a_tracked_change_from_a_worktree(self):
        self.conflicted_merge_in_main()
        code, out = self.check(self.w)
        self.assertEqual(code, 1, out)
        self.assertIn(RECORD, out)

    def test_merge_head_in_main_does_not_hide_a_tracked_change_from_main_unflagged(self):
        self.conflicted_merge_in_main()
        self.assertEqual(self.check(self.main)[0], 1)

    def test_a_tracked_change_in_main_fails_from_main_without_the_flag(self):
        self.append('.gitignore', '# stray edit\n')
        code, out = self.check(self.main)
        self.assertEqual(code, 1, out)
        self.assertIn('.gitignore', out)

    def test_the_flag_does_not_exempt_a_tracked_change_from_main(self):
        self.append('.gitignore', '# stray edit\n')
        code, out = self.check(self.main, allow_tracked=True)
        self.assertEqual(code, 1, out)
        self.assertIn('.gitignore', out)

    def test_the_flag_does_not_exempt_a_tracked_change_from_a_worktree(self):
        self.append('.gitignore', '# stray edit\n')
        self.assertEqual(self.check(self.w, allow_tracked=True)[0], 1)

    def test_the_flag_never_exempts_an_untracked_stray(self):
        self.stray('x', name='oops.txt')
        code, out = self.check(self.main, allow_tracked=True)
        self.assertEqual(code, 1, out)
        self.assertIn('oops.txt', out)

    def test_an_untracked_stray_still_fails_during_a_merge_in_main(self):
        self.put(self.w, 'feature.txt', 'x\n')
        self.r.commit('feature', 'feature.txt', cwd=self.w)
        self.r.git('merge', '-q', '--no-ff', '--no-commit', 'area/w')
        self.stray('x', name='oops.txt')
        code, out = self.check(self.main)
        self.assertEqual(code, 1, out)
        self.assertIn('oops.txt', out)

    def test_every_stray_file_is_named(self):
        self.stray('1', name='one.txt')
        self.stray('2', name='two file.txt')
        code, out = self.check(self.main)
        self.assertEqual(code, 1)
        self.assertIn('one.txt', out)
        self.assertIn('two file.txt', out)

    def test_each_stray_is_one_fail_line_with_nothing_under_it(self):
        # No `likely writer` evidence line: the attribution was dropped. A worktree holding the
        # same content must not make check() name it.
        self.put(self.v, DOC, BASE + 'added in v\n')
        self.stray(BASE + 'added in v\n')
        self.stray('x', name='oops.txt')
        code, lines = stray.check(self.w)
        self.assertEqual(code, 1, lines)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(l.startswith('[FAIL] stray in the main checkout: ') for l in lines), lines)
        self.assertFalse(any(has_path(l, self.v) for l in lines), lines)

    def test_outside_any_repository_warns_rather_than_fails(self):
        code, out = self.check(self.outside)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('WARNING: could not check'), out)

    def test_under_the_flag_a_git_error_fails_rather_than_warns(self):
        code, out = self.check(self.outside, allow_tracked=True)
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith('[FAIL] could not check'), out)

    def test_a_hung_git_warns_rather_than_hangs_or_fails(self):
        timeout = subprocess.TimeoutExpired(cmd=['git'], timeout=cmd.GIT_TIMEOUT)
        with mock.patch.object(cmd, '_run', side_effect=timeout):
            code, out = self.check(self.w)
        self.assertEqual(code, 0)
        self.assertIn('WARNING:', out)
        self.assertIn('timed out', out)


class NarrowConsole:
    """A legacy-codepage console: print() to it raises UnicodeEncodeError outside cp1252."""
    encoding = 'cp1252'

    def __init__(self):
        self.written = []

    def write(self, text):
        text.encode(self.encoding)
        self.written.append(text)

    def flush(self):
        pass


class TheCommandLine(Fixture):
    """tools/checkout_guard.py: its arguments and exit codes."""

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checkout_guard.main(list(argv))
        return code, out.getvalue()

    def test_exit_1_on_main_and_0_on_linked_or_none(self):
        code, out = self.run_cli(os.path.join(self.main, 'docs', 'backlog'))
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith('main '), out)
        code, out = self.run_cli(self.w)
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith('linked '), out)
        self.assertEqual(self.run_cli(self.outside)[0], 0)

    def test_exit_2_when_git_cannot_tell(self):
        code, out = self.run_cli(os.path.join(self.main, '.git'))
        self.assertEqual(code, 2, out)
        self.assertTrue(out.startswith('unknown '), out)

    def test_a_usage_error_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                checkout_guard.main(['--no-such-flag'])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('USAGE:', err.getvalue())

    def test_allow_tracked_alone_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                checkout_guard.main(['--allow-tracked'])
        self.assertEqual(cm.exception.code, 2)

    def test_main_stray_passes_the_flag_through(self):
        self.assertEqual(self.run_cli('--main-stray', self.outside)[0], 0)
        code, out = self.run_cli('--main-stray', '--allow-tracked', self.outside)
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith('[FAIL] could not check'), out)

    def test_main_stray_defaults_to_the_current_directory(self):
        self.stray('x', name='oops.txt')
        old = os.getcwd()
        os.chdir(self.w)
        self.addCleanup(os.chdir, old)
        code, out = self.run_cli('--main-stray')
        self.assertEqual(code, 1, out)
        self.assertIn('oops.txt', out)

    def test_an_ordinary_line_prints_unchanged(self):
        stream = NarrowConsole()
        with mock.patch.object(sys, 'stdout', stream):
            checkout_guard.print_safe(stray.CLEAN)
        self.assertEqual(''.join(stream.written), stray.CLEAN + '\n')

    def test_an_unencodable_character_does_not_raise_and_still_prints(self):
        stream = NarrowConsole()
        with mock.patch.object(sys, 'stdout', stream):
            checkout_guard.print_safe('[FAIL] stray in the main checkout: 日本.txt')
        text = ''.join(stream.written)
        self.assertIn('[FAIL]', text)
        self.assertIn('.txt', text)

    def test_main_stray_does_not_raise_through_a_narrow_console(self):
        self.stray('x', name='日本.txt')
        stream = NarrowConsole()
        with mock.patch.object(sys, 'stdout', stream):
            code = checkout_guard.main(['--main-stray', self.w])
        self.assertEqual(code, 1)
        self.assertTrue(any('[FAIL]' in w for w in stream.written), stream.written)


if __name__ == '__main__':
    unittest.main()
