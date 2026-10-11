"""tools/board.py and kitlib.board: the coordinator's board. Every case builds its own repository
(tests.gitfixture) and its own kit folder (a stub doc_index.py); nothing reads the real repos,
fetches, or reads the machine lock (the lock reader is a stub).

Ported from the PM kit's test_board.py and test_exit_status.py (class Board).
"""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from kitlib.board import report, state
from kitlib.config import load as load_config
from kitlib.preflight import machine
from tests import gitfixture
from tools import board

STALE = 'STALE REPORT (HEAD as committed): 0 lines to confirm, rewrite or delete.'
FREE = {'at': '2026-10-07T12:00:00Z', 'slots': {'cpu': [{'slot': i, 'holder': None} for i in range(4)],
                                                'gpu': [{'slot': i, 'holder': None} for i in range(2)]},
        'runs': [], 'queue': {'entries': [], 'error': None}, 'holds': [], 'holds_error': None}
TASKLIST = ('Image Name                     PID Session Name        Session#    Mem Usage\n'
            '========================= ======== ================ =========== ============\n'
            'Godot_v4.3-stable.exe         1234 Console                    1    200,000 K\n'
            'godot.windows.editor.exe      2345 Console                    1    100,000 K\n'
            'Engine.exe                    3456 Console                    1     10,000 K\n'
            'notepad.exe                   4567 Console                    1      5,000 K\n')
NETSTAT = ('\nActive Connections\n\n  Proto  Local Address          Foreign Address        State           PID\n'
           '  UDP    0.0.0.0:7800           *:*                                    1234\n'
           '  UDP    0.0.0.0:7999           *:*                                    1234\n'
           '  UDP    0.0.0.0:8000           *:*                                    1234\n'
           '  UDP    [::]:7850              *:*                                    2345\n'
           '  UDP    127.0.0.1:7800         *:*                                    3456\n')


def free_reader(_kit):
    return json.loads(json.dumps(FREE)), None


def _origin_master(repo):
    """The template's prepare: origin/master at HEAD; returns HEAD's short hash."""
    repo.git('update-ref', 'refs/remotes/origin/master', 'HEAD')
    return repo.git('rev-parse', '--short', 'HEAD')


class Fixture(unittest.TestCase):
    """A repository on branch master (one commit, origin/master at it) with a worktree on
    `feature`, and a kit folder whose only tool is a stub doc_index.py that prints its argv."""

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='master', prepare=_origin_master,
                                        prepare_key='test_board.Fixture')
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.git = self.repo.git
        self.head = self.repo.prepared
        self.wt = self.repo.worktree('wt', 'feature')
        self.kit = os.path.join(self.repo.base, 'kit')
        os.makedirs(os.path.join(self.kit, 'tools'))
        self.doc_index('import sys\nprint("argv " + " ".join(sys.argv[1:]))\nprint(%r)\n' % STALE)
        p = mock.patch.object(board, 'KIT', self.kit)
        p.start()
        self.addCleanup(p.stop)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)

    def doc_index(self, text):
        with open(os.path.join(self.kit, 'tools', 'doc_index.py'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(text)

    def config(self, **sections):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump(sections, fh)

    def run_board(self, *argv, where=None, reader=free_reader):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = board.main(['-C', where or self.root] + list(argv), lock_reader=reader)
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def row(self, out, branch='feature'):
        found = [l for l in out.splitlines() if l.startswith(branch + ' |')]
        self.assertTrue(found, 'no %r row in:\n%s' % (branch, out))
        return found[0]

    def column(self, out, name):
        """One named cell of the row, read against the header the board prints. Per column: each
        reading has to be asserted on its own, or one of them is decorative."""
        self.assertIn(report.HEADER, out.splitlines(), out)
        names = [c.strip() for c in report.HEADER.split('|')]
        cells = [c.strip() for c in self.row(out).split('|')]
        self.assertEqual(len(cells), len(names), out)
        return cells[names.index(name)]

    def break_worktree(self):
        """Delete the worktree's .git file: `worktree list` still names it, and every git command
        inside it fails - a half-deleted worktree, for real."""
        os.remove(os.path.join(self.wt, '.git'))


class TheBase(Fixture):
    def test_without_config_or_main_the_base_is_master(self):
        code, out = self.run_board()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0],
                         'master @ %s | origin/master @ %s (equal)' % (self.head, self.head))

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
        against origin/master, and the header says so."""
        self.repo.write('m.txt', 'm\n')
        self.repo.commit('m', 'm.txt')
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')
        self.git('update-ref', 'refs/heads/master', self.head)          # local master stale
        out = self.run_board()[1]
        self.assertTrue(out.splitlines()[0].endswith(
            '(DIFFERENT; rows measured against origin/master)'), out)
        self.assertEqual(self.column(out, 'ahead/behind'), '+0 -1', out)
        self.assertEqual(self.column(out, 'has base tip'), 'NO', out)

    def test_an_unpushed_local_base_is_not_measured_against(self):
        """Local master ahead of origin/master: rows still counted against origin/master."""
        self.repo.write('m.txt', 'm\n')
        self.repo.commit('m', 'm.txt')
        out = self.run_board()[1]
        self.assertTrue(out.splitlines()[0].endswith(
            '(DIFFERENT; rows measured against origin/master)'), out)
        self.assertEqual(self.column(out, 'ahead/behind'), '+0 -0', out)
        self.assertEqual(self.column(out, 'has base tip'), 'yes', out)

    def test_a_main_branch_is_taken_as_the_base(self):
        self.git('branch', 'main', 'master')
        out = self.run_board()[1]
        self.assertEqual(out.splitlines()[0], 'main @ %s | origin/main @ ? (DIFFERENT)' % self.head)

    def test_a_configured_main_branch_wins_over_one_that_exists(self):
        self.git('branch', 'main', 'master')
        self.git('branch', 'trunk', 'master')
        self.config(repo={'main_branch': 'trunk'})
        out = self.run_board()[1]
        self.assertEqual(out.splitlines()[0], 'trunk @ %s | origin/trunk @ ? (DIFFERENT)' % self.head)

    def test_an_explicit_base_wins_over_everything(self):
        self.git('branch', 'main', 'master')
        self.config(repo={'main_branch': 'main'})
        out = self.run_board('--base', 'master')[1]
        self.assertEqual(out.splitlines()[0],
                         'master @ %s | origin/master @ %s (equal)' % (self.head, self.head))

    def test_the_remote_comes_from_config(self):
        self.git('update-ref', 'refs/remotes/upstream/master', 'HEAD')
        self.git('update-ref', '-d', 'refs/remotes/origin/master')
        self.config(repo={'remote': 'upstream'})
        out = self.run_board()[1]
        self.assertEqual(out.splitlines()[0],
                         'master @ %s | upstream/master @ %s (equal)' % (self.head, self.head))

    def test_an_unread_base_never_reads_equal(self):
        out = self.run_board('--base', 'no-such-branch')[1]
        self.assertEqual(out.splitlines()[0],
                         'no-such-branch @ ? | origin/no-such-branch @ ? (DIFFERENT)')

    def test_a_base_git_would_read_as_an_option_is_a_usage_error(self):
        code, out = self.run_board('--base=-x')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: --base', out)

    def test_a_directory_in_no_repository_is_a_usage_error(self):
        outside = os.path.join(self.repo.base, 'outside')
        os.makedirs(outside)
        code, out = self.run_board(where=outside)
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: -C', out)

    def test_from_a_worktree_the_board_is_the_same(self):
        def board(where):
            out = self.run_board(where=where)[1]
            age = self.column(out, 'last commit')
            return [l.replace(' | %s | ' % age, ' | <age> | ') for l in out.splitlines()
                    if not l.startswith('docs:')]
        self.assertEqual(board(self.wt), board(self.root))


class TheRows(Fixture):
    """A dashboard cannot refuse, so a failed command must read `?` and never a value."""

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
        """The PM kit's own case: a worktree beside the repository, every git command failing."""
        outside = os.path.join(self.repo.base, 'outside-wt')
        self.git('worktree', 'add', '-q', outside, '-b', 'beside')
        os.remove(os.path.join(outside, '.git'))
        row = self.row(self.run_board()[1], 'beside')
        self.assertEqual(row, 'beside | %s | +? -? | ? | ? | ? | ? (not its own work tree: git '
                         'cannot read it)' % self.head, row)
        with mock.patch.object(report, '_own_tree', return_value=True):
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
        with mock.patch.object(report.repo, 'worktrees', return_value=(None, 'simulated')):
            out = self.run_board()[1]
        self.assertIn('(worktrees UNKNOWN - simulated)', out.splitlines())
        self.assertNotIn('no worktrees', out)


class TheLockAndDocsLines(Fixture):
    def test_the_lock_line_is_the_kit_lock_summary(self):
        snap = json.loads(json.dumps(FREE))
        snap['runs'] = [{'id': 'r-g', 'state': 'held', 'repo': 'c', 'slug': 'img',
                         'kind': 'local-ai', 'since': '2026-10-07T11:58:00Z',
                         'held_since': '2026-10-07T11:58:00Z', 'background': False,
                         'overrun': None}]
        snap['slots']['gpu'][0]['holder'] = snap['slots']['gpu'][1]['holder'] = 'r-g'
        out = self.run_board(reader=lambda _k: (snap, None))[1]
        self.assertIn('machine: lock cpu free (0/4) | gpu 2/2: slot0-1 c/img (local-ai, run r-g, '
                      '2 min) | waiting 0', out.splitlines())

    def test_an_unreadable_lock_is_unknown(self):
        out = self.run_board(reader=lambda _k: (None, 'simulated'))[1]
        self.assertIn('machine: lock unknown (simulated)', out.splitlines())

    def test_a_kit_without_the_lock_tool_is_unknown(self):
        out = self.run_board(reader=report.lockline.read_snapshot)[1]
        self.assertIn('machine: lock unknown (no tools/machine_lock.py in the kit)',
                      out.splitlines())

    def test_the_docs_line_is_the_stale_reports_last_line_for_this_checkout(self):
        self.doc_index('import sys\nprint(" ".join(sys.argv[1:]))\n')
        out = self.run_board(where=self.wt)[1]
        self.assertIn('docs: --stale -C %s' % os.path.normpath(self.wt), out.splitlines())
        self.doc_index('print("first")\nprint(%r)\n' % STALE)
        self.assertIn('docs: ' + STALE, self.run_board()[1].splitlines())

    def test_a_failing_stale_report_says_so(self):
        self.doc_index('import sys\nprint("boom")\nsys.exit(3)\n')
        self.assertIn('docs: doc_index --stale FAILED (exit 3) boom', self.run_board()[1].splitlines())

    def test_a_kit_without_doc_index_says_not_checked(self):
        os.remove(os.path.join(self.kit, 'tools', 'doc_index.py'))
        self.assertIn('docs: NOT CHECKED - no tools/doc_index.py in the kit',
                      self.run_board()[1].splitlines())


class TheMachineLine(Fixture):
    def fake_run(self, tasklist=(0, TASKLIST, ''), netstat=(0, NETSTAT, '')):
        real = machine.run

        def run(argv, **kw):
            if argv[0] == 'tasklist':
                return tasklist
            if argv[0] == 'netstat':
                return netstat
            return real(argv, **kw)
        return mock.patch.object(machine, 'run', run)

    def line(self, engines=(), udp_ports=None):
        return machine.board_line({'engines': list(engines), 'udp_ports': udp_ports})

    def test_nothing_configured_prints_no_engines_or_ports(self):
        with self.fake_run():
            out = self.run_board()[1]
        self.assertNotIn('engines', out)
        self.assertNotIn('ports', out)
        self.assertIsNone(self.line())

    def test_configured_engines_and_ports_are_counted_on_one_line(self):
        self.config(preflight={'engines': ['Godot', 'engine.exe'], 'udp_ports': [7800, 7999]})
        with self.fake_run():
            code, out = self.run_board()
        self.assertEqual(code, 0, out)
        self.assertIn('machine: engines running 3 | ports 7800-7999 in use [7800, 7850, 7999]',
                      out.splitlines())

    def test_engines_alone(self):
        with self.fake_run():
            self.assertEqual(self.line(engines=['notepad']), 'machine: engines running 1')

    def test_no_port_in_range_reads_none(self):
        with self.fake_run():
            self.assertEqual(self.line(udp_ports=[9000, 9001]),
                             'machine: ports 9000-9001 in use none')

    def test_a_failed_tasklist_is_unknown_not_zero(self):
        with self.fake_run(tasklist=(1, '', '')):
            self.assertEqual(self.line(engines=['godot']),
                             'machine: engines UNKNOWN (tasklist failed)')

    def test_a_failed_netstat_is_unknown_not_none(self):
        with self.fake_run(netstat=(1, '', '')):
            self.assertEqual(self.line(udp_ports=[7800, 7999]),
                             'machine: ports 7800-7999 UNKNOWN (netstat failed)')


class TheStateFile(Fixture):
    BLOCK = '# state\n\n<!-- board:begin -->\nold\n<!-- board:end -->\n\n## Next\nkeep me\n'

    def read(self, *parts):
        with open(os.path.join(self.root, *parts), encoding='utf-8') as fh:
            return fh.read()

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
        self.assertIn('## Landing queue', body)
        self.assertIn(('state written: %s' % os.path.normpath(self.state())).lower(), out.lower())
        for folder in ('ops', 'docs'):
            self.assertFalse(os.path.exists(os.path.join(self.root, folder)))
        self.assertNotIn('state', self.git('status', '--porcelain'))

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
        self.assertIn('board: refused: "board".state_file notes/board.md is inside the work tree', out)
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


class TheConfig(Fixture):
    def refused(self, **sections):
        self.config(**sections)
        code, out = self.run_board()
        self.assertEqual(code, 2, out)
        self.assertIn('board: refused: kit configuration', out)
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

    def test_a_bad_preflight_machine_key_refuses_the_board_too(self):
        self.assertIn('udp_ports', self.refused(preflight={'udp_ports': [9, 1]}))

    def test_the_state_file_default_is_none(self):
        self.assertIsNone(report.state_file(load_config(self.root)))


if __name__ == '__main__':
    unittest.main()
