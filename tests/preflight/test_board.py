"""`tools/preflight.py --board` and kitlib.preflight.board / state: the coordinator's board. Built on
test_preflight's Fixture (its own repository and an empty kit folder, the stray check recorded, the
lock reader a stub); this file adds a worktree on `feature` and a stub doc_index.py. Nothing reads
the real repos, fetches, or reads the machine lock.
"""
import json
import os
import unittest
from unittest import mock

from kitlib.config import load as load_config
from kitlib.preflight import board, state
from tests.preflight.test_preflight import Fixture

STALE = 'STALE REPORT (HEAD as committed): 0 lines to confirm, rewrite or delete.'


class BoardFixture(Fixture):
    """The preflight fixture plus a worktree on `feature` and a kit doc_index.py that prints its
    argv, then STALE."""

    def setUp(self):
        super().setUp()
        self.wt = self.repo.worktree('wt', 'feature')
        self.doc_index('import sys\nprint("argv " + " ".join(sys.argv[1:]))\nprint(%r)\n' % STALE)

    def doc_index(self, text):
        with open(os.path.join(self.kit, 'tools', 'doc_index.py'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(text)

    def run_board(self, *argv, where=None):
        return self.run_preflight('--board', *argv, where=where)

    def board_part(self, out):
        """The board's lines out of a preflight --board run: from its header on."""
        lines = out.splitlines()
        self.assertIn(board.HEADER, lines, out)
        return lines[lines.index(board.HEADER):]

    def row(self, out, branch='feature'):
        found = [l for l in out.splitlines() if l.startswith(branch + ' |')]
        self.assertTrue(found, 'no %r row in:\n%s' % (branch, out))
        return found[0]

    def column(self, out, name):
        """One named cell of the row, read against the header the board prints. Per column: each
        reading has to be asserted on its own, or one of them is decorative."""
        self.assertIn(board.HEADER, out.splitlines(), out)
        names = [c.strip() for c in board.HEADER.split('|')]
        cells = [c.strip() for c in self.row(out).split('|')]
        self.assertEqual(len(cells), len(names), out)
        return cells[names.index(name)]

    def break_worktree(self):
        """Delete the worktree's .git file: `worktree list` still names it, and every git command
        inside it fails - a half-deleted worktree, for real."""
        os.remove(os.path.join(self.wt, '.git'))


class TheFlag(BoardFixture):
    def test_without_board_there_is_no_board(self):
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertNotIn(board.HEADER, out)
        self.assertNotIn('feature |', out)

    def test_board_follows_the_whole_report(self):
        code, out = self.run_board()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('repo '), out)
        self.assertTrue(any(l.startswith('lock ') for l in lines), out)
        self.assertLess(max(i for i, l in enumerate(lines) if l.startswith('lock ')),
                        lines.index(board.HEADER), out)
        self.assertTrue(self.board_part(out)[-1].startswith('docs: '), out)

    def test_write_state_without_board_is_a_usage_error(self):
        code, out = self.run_preflight('--write-state')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: --write-state needs --board', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.git', 'kit', 'board-state.md')))


class TheRows(BoardFixture):
    """A dashboard cannot refuse, so a failed command must read `?` and never a value."""

    def test_the_rows_are_counted_against_the_picked_base(self):
        self.repo.write('b.txt', 'b\n', root=self.wt)
        self.repo.commit('b', 'b.txt', cwd=self.wt)
        out = self.run_board()[1]
        self.assertEqual(self.column(out, 'ahead/behind'), '+1 -0', out)
        self.assertEqual(self.column(out, 'has base tip'), 'yes', out)

    def test_a_branch_behind_its_base_has_no_base_tip(self):
        self.repo.write('m.txt', 'm\n')
        self.repo.commit('m', 'm.txt')
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')    # landed: pushed too
        out = self.run_board()[1]
        self.assertEqual(self.column(out, 'ahead/behind'), '+0 -1', out)
        self.assertEqual(self.column(out, 'has base tip'), 'NO', out)

    def test_a_stale_local_base_is_not_measured_against(self):
        """docs/gate.md "Default base": local master behind origin/master - rows are counted
        against origin/master."""
        self.repo.write('m.txt', 'm\n')
        self.repo.commit('m', 'm.txt')
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')
        self.git('update-ref', 'refs/heads/master', self.head)          # local master stale
        out = self.run_board()[1]
        self.assertIn('board     rows measured against origin/master (local master differs)',
                      out.splitlines())
        self.assertEqual(self.column(out, 'ahead/behind'), '+0 -1', out)
        self.assertEqual(self.column(out, 'has base tip'), 'NO', out)

    def test_an_unpushed_local_base_is_not_measured_against(self):
        """Local master ahead of origin/master: rows still counted against origin/master."""
        self.repo.write('m.txt', 'm\n')
        self.repo.commit('m', 'm.txt')
        out = self.run_board()[1]
        self.assertIn('board     rows measured against origin/master (local master differs)',
                      out.splitlines())
        self.assertEqual(self.column(out, 'ahead/behind'), '+0 -0', out)
        self.assertEqual(self.column(out, 'has base tip'), 'yes', out)

    def test_an_equal_base_prints_no_measure_note(self):
        self.assertNotIn('rows measured against', self.run_board()[1])

    def test_an_explicit_base_is_the_one_rows_are_counted_against(self):
        self.git('branch', 'main', 'master')
        self.repo.write('b.txt', 'b\n', root=self.wt)
        self.repo.commit('b', 'b.txt', cwd=self.wt)
        self.config(repo={'main_branch': 'main'})
        out = self.run_board('--base', 'master')[1]
        self.assertEqual(self.column(out, 'ahead/behind'), '+1 -0', out)

    def test_a_base_git_would_read_as_an_option_is_a_usage_error(self):
        code, out = self.run_board('--base=-x')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: --base', out)

    def test_from_a_worktree_the_board_is_the_same(self):
        def part(where):
            out = self.run_board(where=where)[1]
            age = self.column(out, 'last commit')
            return [l.replace(' | %s | ' % age, ' | <age> | ') for l in self.board_part(out)
                    if not l.startswith('docs:')]
        self.assertEqual(part(self.wt), part(self.root))

    def test_a_healthy_worktree_reports_its_real_readings(self):
        out = self.run_board()[1]
        self.assertEqual(self.column(out, 'tip'), self.head, out)
        self.assertEqual(self.column(out, 'dirty'), '0', out)
        self.assertEqual(self.column(out, 'owner-only touched'), '-', out)
        self.assertNotEqual(self.column(out, 'last commit'), '?', out)

    def test_dirty_tracked_files_are_counted_and_untracked_are_not(self):
        self.repo.write('README.md', 'changed\n', root=self.wt)
        self.repo.write('new.txt', 'new\n', root=self.wt)
        self.assertEqual(self.column(self.run_board()[1], 'dirty'), '1')

    def test_an_owner_only_path_on_the_branch_is_named(self):
        self.repo.write('CLAUDE.md', 'x\n', root=self.wt)
        self.repo.commit('owner file', 'CLAUDE.md', cwd=self.wt)
        out = self.run_board()[1]
        self.assertEqual(self.column(out, 'owner-only touched'), 'CLAUDE.md', out)

    def test_the_owner_only_paths_come_from_config(self):
        self.config(owner={'paths': ['notes/']})
        self.repo.write('notes/a.md', 'x\n', root=self.wt)
        self.repo.write('CLAUDE.md', 'x\n', root=self.wt)
        self.repo.commit('two', 'notes/a.md', 'CLAUDE.md', cwd=self.wt)
        self.assertEqual(self.column(self.run_board()[1], 'owner-only touched'), 'notes/a.md')

    def test_a_worktree_whose_git_status_fails_does_not_report_zero_dirty(self):
        self.break_worktree()
        self.assertEqual(self.column(self.run_board()[1], 'dirty'), '?')

    def test_a_worktree_that_cannot_be_asked_does_not_report_NO_base_tip(self):
        self.break_worktree()
        out = self.run_board()[1]
        self.assertEqual(self.column(out, 'has base tip'), '?', out)
        self.assertEqual(self.column(out, 'ahead/behind'), '+? -?', out)
        self.assertEqual(self.column(out, 'last commit'), '?', out)

    def test_a_failed_owner_only_diff_does_not_look_like_no_owner_only_paths(self):
        self.break_worktree()
        cell = self.column(self.run_board()[1], 'owner-only touched')
        self.assertNotEqual(cell, '-')
        self.assertTrue(cell.startswith('? ('), cell)

    def test_a_half_deleted_worktree_is_not_read_as_the_main_checkout(self):
        """Nested in the main checkout, a worktree without its .git is answered for by the main
        checkout's: dirty there must not show here."""
        self.repo.write('README.md', 'dirty in main\n')
        self.break_worktree()
        out = self.run_board()[1]
        self.assertEqual(self.row(out), 'feature | %s | +? -? | ? | ? | ? | ? (not its own work '
                         'tree: git cannot read it)' % self.head, out)

    def test_a_worktree_outside_the_checkout_whose_git_fails_reads_unknown(self):
        """A worktree beside the repository, every git command failing."""
        outside = os.path.join(self.repo.base, 'outside-wt')
        self.git('worktree', 'add', '-q', outside, '-b', 'beside')
        os.remove(os.path.join(outside, '.git'))
        row = self.row(self.run_board()[1], 'beside')
        self.assertEqual(row, 'beside | %s | +? -? | ? | ? | ? | ? (not its own work tree: git '
                         'cannot read it)' % self.head, row)
        with mock.patch.object(board, '_own_tree', return_value=True):
            cells = [c.strip() for c in self.row(self.run_board()[1], 'beside').split('|')]
        self.assertEqual(cells[2:], ['+? -?', '?', '?', '?', '? (git diff failed)'], cells)

    def test_the_last_worktree_in_the_listing_is_not_dropped(self):
        self.repo.worktree('second', 'feature2')
        out = self.run_board()[1]
        self.assertIn('feature |', out)
        self.assertIn('feature2 |', out)

    def test_only_the_main_checkout_says_so(self):
        self.git('worktree', 'remove', '--force', self.wt)
        self.assertIn('(no worktrees besides the main checkout)', self.run_board()[1].splitlines())

    def test_a_failed_worktree_list_is_unknown_not_no_worktrees(self):
        with mock.patch.object(board.repo, 'worktrees', return_value=(None, 'simulated')):
            out = board.lines(self.root, load_config(self.root), self.kit)
        self.assertIn('(worktrees UNKNOWN - simulated)', out)
        self.assertNotIn('(no worktrees besides the main checkout)', out)


class TheDocsLine(BoardFixture):
    def test_the_docs_line_is_the_stale_reports_last_line_for_this_checkout(self):
        self.doc_index('import sys\nprint(" ".join(sys.argv[1:]))\n')
        out = self.board_part(self.run_board(where=self.wt)[1])
        self.assertIn('docs: --stale -C %s' % os.path.normpath(self.wt).replace('\\', '/'), out)
        self.doc_index('print("first")\nprint(%r)\n' % STALE)
        self.assertIn('docs: ' + STALE, self.board_part(self.run_board()[1]))

    def test_a_failing_stale_report_says_so(self):
        self.doc_index('import sys\nprint("boom")\nsys.exit(3)\n')
        self.assertIn('docs: doc_index --stale FAILED (exit 3) boom',
                      self.board_part(self.run_board()[1]))

    def test_a_kit_without_doc_index_says_not_checked(self):
        os.remove(os.path.join(self.kit, 'tools', 'doc_index.py'))
        self.assertIn('docs: NOT CHECKED - no tools/doc_index.py in the kit',
                      self.board_part(self.run_board()[1]))


class TheStateFile(BoardFixture):
    BLOCK = '# state\n\n<!-- board:begin -->\nold\n<!-- board:end -->\n\n## Next\nkeep me\n'

    def state(self):
        return os.path.join(self.root, '.git', 'kit', 'board-state.md')

    def read_state(self):
        with open(self.state(), encoding='utf-8') as fh:
            return fh.read()

    def test_the_default_is_under_the_git_dir_and_leaves_the_tree_clean(self):
        code, out = self.run_board('--write-state')
        self.assertEqual(code, 0, out)
        body = self.read_state()
        self.assertIn('feature |', body)
        self.assertIn(board.HEADER, body)
        self.assertIn('## Landing queue', body)
        self.assertIn(('state written: %s' % os.path.normpath(self.state()).replace('\\', '/')).lower(), out.lower())
        for folder in ('ops', 'docs'):
            self.assertFalse(os.path.exists(os.path.join(self.root, folder)))
        self.assertNotIn('state', self.git('status', '--porcelain'))

    def test_the_state_keeps_the_base_and_the_lock_lines(self):
        self.run_board('--write-state')
        body = self.read_state().splitlines()
        self.assertIn('base      master @ %s | origin/master @ %s (equal)' % (self.head, self.head),
                      body)
        self.assertTrue(any(l.startswith('lock      ') for l in body), body)
        self.assertLess(body.index(board.HEADER), max(
            i for i, l in enumerate(body) if l.startswith('lock ')))

    def test_without_the_flag_nothing_is_written(self):
        self.run_board()
        self.assertFalse(os.path.exists(self.state()))

    def test_write_state_fills_only_the_marked_block(self):
        os.makedirs(os.path.dirname(self.state()))
        with open(self.state(), 'w', encoding='utf-8') as fh:
            fh.write(self.BLOCK)
        self.run_board('--write-state')
        body = self.read_state()
        self.assertTrue(body.startswith('# state\n\n<!-- board:begin -->\n'), body)
        self.assertTrue(body.endswith('<!-- board:end -->\n\n## Next\nkeep me\n'), body)
        self.assertNotIn('\nold\n', body)
        self.assertIn('feature |', body)

    def test_a_configured_state_file_under_the_git_dir_is_used(self):
        self.config(board={'state_file': '.git/notes/board.md'})
        code, out = self.run_board('--write-state')
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.root, '.git', 'notes', 'board.md'), encoding='utf-8') as fh:
            self.assertIn('feature |', fh.read())
        self.assertFalse(os.path.exists(self.state()))

    def test_a_configured_state_file_in_the_work_tree_refuses(self):
        self.config(board={'state_file': 'notes/board.md'})
        code, out = self.run_board('--write-state')
        self.assertEqual(code, 2, out)
        self.assertIn('preflight: refused: "board".state_file notes/board.md is inside the work tree',
                      out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'notes')))

    def test_the_state_goes_to_the_main_checkout_from_a_worktree(self):
        self.run_board('--write-state', where=self.wt)
        self.assertIn('feature |', self.read_state())
        self.assertFalse(os.path.exists(os.path.join(self.wt, 'docs')))

    def test_fill(self):
        self.assertEqual(state.fill('top\n', 'S', 'T'),
                         '<!-- board:begin -->\nS\nT\n<!-- board:end -->\n\ntop\n')
        body = '%s\nx\n' % state.END       # an end marker with no begin before it
        self.assertTrue(state.fill(body, 'S', 'T').startswith(state.BEGIN + '\nS\nT\n' + state.END))


class TheConfig(BoardFixture):
    def refused(self, **sections):
        self.config(**sections)
        code, out = self.run_board()
        self.assertEqual(code, 2, out)
        self.assertIn('preflight: refused: kit configuration', out)
        return out

    def test_an_unknown_board_key_refuses(self):
        self.assertIn('bogus', self.refused(board={'bogus': 1}))

    def test_an_absolute_state_file_refuses(self):
        self.assertIn('state_file',
                      self.refused(board={'state_file': os.path.join(self.repo.base, 'x.md')}))

    def test_a_state_file_climbing_out_refuses(self):
        self.assertIn('state_file', self.refused(board={'state_file': '../x.md'}))
        self.assertIn('state_file', self.refused(board={'state_file': 'a\\..\\..\\x.md'}))

    def test_an_empty_state_file_refuses(self):
        self.assertIn('state_file', self.refused(board={'state_file': ' '}))

    def test_the_state_file_default_is_none(self):
        self.assertIsNone(board.state_file(load_config(self.root)))


if __name__ == '__main__':
    unittest.main()
