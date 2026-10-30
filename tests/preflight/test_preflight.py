"""tools/preflight.py and kitlib.preflight: the start-of-task picture. Every case builds its own
repository (tests.gitfixture) and its own kit folder; nothing reads the real repos, fetches from a
real remote, or touches the live machine lock (the lock reader is a stub, or the kit tool on a
scratch store).
"""
import contextlib
import datetime
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.config import ConfigError, load as load_config
from kitlib.git import cmd
from kitlib.config import user
from kitlib.preflight import configline, hooks, lockline, machine, nextblock, report, settings as st
from tests import gitfixture
from tests.home import machine_config
from tools import preflight

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODEMAP = 'python tools/codemap.py -C "D:/src/Repo/.claude/worktrees/y"'    # TheNextBlockLines


def kit(name, root):
    """How the report names the tool `name` of the kit at `root`: every command it prints runs the
    kit's own tools (nextblock.command over the kit's tools folder)."""
    return nextblock.command(root.replace('\\', '/') + '/tools', name)
FREE = {'at': '2026-10-07T12:00:00Z', 'slots': {'cpu': [{'slot': i, 'holder': None} for i in range(4)],
                                                'gpu': [{'slot': i, 'holder': None} for i in range(2)]},
        'runs': [], 'queue': {'entries': [], 'error': None}, 'holds': [], 'holds_error': None}
OVER_BUDGET_TAIL = (
    'DOC BUDGET FAILED: over budget and grew - on the worktree, or once merged into main.\n'
    '  docs/ops/gotchas.md: over budget on the worktree by 400 bytes.\n'
    'Fix: delete closed items, cut entries to keywords. Do not raise the budget to pass.\n')
OK_LINE = 'DOC BUDGET OK (the working tree, 12 .md files)\n'


def free_reader(_kit_root):
    return json.loads(json.dumps(FREE)), None


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


class Fixture(unittest.TestCase):
    """A repository on branch master (one commit, origin/master at it) and an empty kit folder
    (no doc_budget) that tools/preflight.py takes as its kit; the stray check is recorded, not run
    (it has its own tests in tests/git/test_stray.py)."""

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='master')
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.git = self.repo.git
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')
        self.head = self.git('rev-parse', '--short', 'HEAD')
        self.kit = os.path.join(self.repo.base, 'kit')
        os.makedirs(os.path.join(self.kit, 'tools'))
        p = mock.patch.object(preflight, 'KIT', self.kit)
        p.start()
        self.addCleanup(p.stop)
        self.strays = []
        p = mock.patch.object(report.stray, 'check',
                              side_effect=lambda *a, **k: self.strays.append((a, k)) or (0, []))
        p.start()
        self.addCleanup(p.stop)
        # the `config` line reads a scratch home (run_preflight passes it); one that reached for
        # the user's own machine config would fail here instead of reading it
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(self.home)
        real_line = configline.line

        def scratch_only(home=None):
            if home is None:
                raise AssertionError('the config line would read the real home folder')
            return real_line(home)
        p = mock.patch.object(report.configline, 'line', scratch_only)
        p.start()
        self.addCleanup(p.stop)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)

    def config(self, **sections):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump(sections, fh)

    def kit_tool(self, name, text, status, stderr=''):
        """A stub kit tool that prints `text` (and `stderr`) and exits `status`: a real
        subprocess, run exactly as the real one is."""
        with open(os.path.join(self.kit, 'tools', name), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('import sys\nsys.stdout.write(%r)\nsys.stderr.write(%r)\nsys.exit(%d)\n'
                     % (text, stderr, status))

    def run_preflight(self, *argv, reader=free_reader, where=None):
        argv = list(argv) if where is False else ['-C', where or self.root] + list(argv)
        if '--full' not in argv and not getattr(self, 'short', False):
            argv.append('--full')
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                code = preflight.main(argv, lock_reader=reader, home=self.home)
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue() + err.getvalue()

    def line(self, out, start):
        found = [l for l in out.splitlines() if l.startswith(start)]
        self.assertTrue(found, 'no %r line in:\n%s' % (start, out))
        return found[0]


class TheBase(Fixture):
    def test_without_config_or_main_the_base_is_master(self):
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.line(out, 'base '),
                         'base      master @ %s | origin/master @ %s (equal)' % (self.head, self.head))

    def test_a_main_branch_is_taken_as_the_base(self):
        self.git('branch', 'main', 'master')
        out = self.run_preflight()[1]
        self.assertIn('base      main @ %s | origin/main @ ? (DIFFERENT' % self.head, out)

    def test_a_configured_main_branch_wins_over_one_that_exists(self):
        self.git('branch', 'main', 'master')
        self.git('branch', 'trunk', 'master')
        self.config(repo={'main_branch': 'trunk'})
        out = self.run_preflight()[1]
        self.assertIn('base      trunk @ %s | origin/trunk @ ?' % self.head, out)

    def test_an_explicit_base_wins_over_everything(self):
        self.git('branch', 'main', 'master')
        self.config(repo={'main_branch': 'main'})
        out = self.run_preflight('--base', 'master')[1]
        self.assertIn('base      master @ %s | origin/master @ %s (equal)' % (self.head, self.head),
                      out)

    def test_an_explicit_base_may_be_any_revision(self):
        full = self.git('rev-parse', 'HEAD')
        out = self.run_preflight('--base', full)[1]
        self.assertIn('base      %s @ %s | origin/%s @ ?' % (full, self.head, full), out)

    def test_a_base_git_would_read_as_an_option_is_a_usage_error(self):
        code, out = self.run_preflight('--base=-x')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: --base', out)

    def test_the_remote_comes_from_config(self):
        self.git('update-ref', 'refs/remotes/upstream/master', 'HEAD')
        self.git('update-ref', '-d', 'refs/remotes/origin/master')
        self.config(repo={'remote': 'upstream'})
        out = self.run_preflight()[1]
        self.assertIn('base      master @ %s | upstream/master @ %s (equal)' % (self.head, self.head),
                      out)

    def test_fetch_fetches_the_configured_remote_and_says_when_it_failed(self):
        self.config(repo={'remote': 'upstream'})
        with Spawns() as spawns:
            out = self.run_preflight('--fetch')[1]
        self.assertIn(['git', 'fetch', 'upstream', '--quiet'], spawns.argvs)
        self.assertIn('fetch     FAILED - git fetch upstream:', out)    # no such remote here

    def test_no_fetch_without_the_flag(self):
        with Spawns() as spawns:
            self.run_preflight()
        self.assertFalse([a for a in spawns.argvs if 'fetch' in a], spawns.argvs)

    def test_the_base_refs_are_one_git_process(self):
        # The `backlog` line's own ref reads (for-each-ref) are counted in test_default_store.
        with Spawns() as spawns, mock.patch.object(report.defaultstore, 'lines', return_value=[]):
            out = self.run_preflight()[1]
        reads = [a for a in spawns.argvs if '--short' in a or 'show-ref' in a or 'for-each-ref' in a]
        self.assertEqual(len(reads), 2, reads)        # for-each-ref, and HEAD's own rev-parse
        self.assertIn('(equal)', self.line(out, 'base '))

    def test_a_failed_for_each_ref_reads_neither_ref_and_never_equal(self):
        real = cmd.spawner()

        def run(argv, *a, **kw):
            if 'for-each-ref' in argv:
                return 128, b'', b'fatal: simulated'
            return real(argv, *a, **kw)
        with mock.patch.object(cmd, '_run', run):
            self.assertEqual(st.base_refs(self.root, ['master'], 'origin'), {})
            out = self.run_preflight()[1]
        self.assertIn('base      master @ ? | origin/master @ ? (DIFFERENT', out)
        self.assertNotIn('(equal)', out)

    def test_no_base_branch_and_no_remote_never_reads_equal(self):
        self.git('branch', '-m', 'master', 'trunk')
        self.git('update-ref', '-d', 'refs/remotes/origin/master')
        base = self.line(self.run_preflight()[1], 'base ')
        self.assertNotIn('(equal)', base)
        self.assertIn('@ ? |', base)

    def test_a_branch_behind_its_base_is_told_to_merge(self):
        wt = self.repo.worktree('y', 'x/y')
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('base moves', 'b.txt')
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')    # landed: pushed too
        out = self.run_preflight(where=wt)[1]
        self.assertIn('vs base   merge-base %s | behind 1, ahead 0  <- merge the hash the coordinator '
                      'names before landing' % self.head, out)

    def test_a_stale_local_base_is_not_measured_against(self):
        """docs/gate.md "Default base": local master behind origin/master - the branch is
        counted against origin/master, and the line says so."""
        wt = self.repo.worktree('y', 'x/y')
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('base moves', 'b.txt')
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')
        self.git('update-ref', 'refs/heads/master', self.head)            # local master stale
        out = self.run_preflight(where=wt)[1]
        self.assertIn('vs base   merge-base %s | behind 1, ahead 0 (against origin/master)  <- merge '
                      'the hash the coordinator names before landing' % self.head, out)

    def test_an_unpushed_local_base_is_not_measured_against(self):
        """Local master ahead of origin/master: still counted against origin/master."""
        wt = self.repo.worktree('y', 'x/y')
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('not pushed', 'b.txt')
        out = self.run_preflight(where=wt)[1]
        self.assertIn('vs base   merge-base %s | behind 0, ahead 0 (against origin/master)'
                      % self.head, out)


class TheReadings(Fixture):
    def test_a_failed_git_status_is_not_printed_as_zero_dirty_files(self):
        with open(os.path.join(self.root, '.git', 'index'), 'wb') as fh:
            fh.write(b'garbage')
        dirty = self.line(self.run_preflight()[1], 'dirty ')
        self.assertIn('UNKNOWN', dirty)
        self.assertNotIn('0 file(s)', dirty)

    def test_a_working_git_status_counts_and_lists(self):
        self.repo.write('dirty.txt', 'x\n')
        self.repo.write('nd/f.txt', 'x\n')
        out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'dirty '), 'dirty     2 file(s)')   # a new folder is one
        self.assertIn('\n            ?? dirty.txt\n', out)
        self.assertIn('\n            ?? nd/\n', out)

    def test_the_worktrees_are_counted_and_listed(self):
        wt = self.repo.worktree('y', 'x/y')
        out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'worktrees '), 'worktrees 2')
        self.assertIn('            %s  %s [x/y]' % (os.path.normpath(wt).replace('\\', '/'), self.head), out)

    def test_a_failed_worktree_list_is_unknown(self):
        real = cmd.spawner()

        def run(argv, *a, **kw):
            if 'worktree' in argv and 'list' in argv:
                return 128, b'', b'fatal: simulated'
            return real(argv, *a, **kw)
        with mock.patch.object(cmd, '_run', run):
            out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'worktrees '),
                         'worktrees UNKNOWN - git worktree list failed here')

    def test_the_repo_line_is_the_checkout_c_names(self):
        sub = os.path.join(self.root, 'sub')
        os.makedirs(sub)
        out = self.run_preflight(where=sub)[1]
        self.assertEqual(self.line(out, 'repo '), 'repo      %s' % os.path.normpath(self.root).replace('\\', '/'))

    def test_without_c_the_checkout_is_the_current_directory(self):
        os.chdir(self.root)
        code, out = self.run_preflight(where=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.line(out, 'repo '), 'repo      %s' % os.path.normpath(self.root).replace('\\', '/'))

    def test_a_checkout_path_with_a_space_is_printed_whole_in_forward_slashes(self):
        top = 'Z:%smy dir%srepo' % (chr(92), chr(92))
        with mock.patch.object(preflight.repo, 'toplevel', return_value=(top, None)),                 mock.patch.object(preflight.report, 'report', return_value=['repo      ' + top]):
            code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.line(out, 'repo '), 'repo      Z:/my dir/repo')

    def test_no_printed_path_has_a_backslash(self):
        out = self.run_preflight()[1]
        self.assertNotRegex(out, r'[A-Za-z]:\\')

    def test_a_c_with_the_backslashes_eaten_names_the_forward_slash_form(self):
        code, out = self.run_preflight(where='Z:GitHubnowhereatall')
        self.assertEqual(code, 2)
        self.assertIn('Instead: pass forward slashes, as -C Z:/GitHubnowhereatall', out)

    def test_real_c_paths_in_either_slash_are_not_taken_for_mangled(self):
        for where in (self.root, self.root.replace('\\', '/')):
            code, out = self.run_preflight(where=where)
            self.assertEqual(code, 0, out)

    def test_a_directory_in_no_repository_is_a_usage_error(self):
        outside = os.path.join(self.repo.base, 'outside')
        os.makedirs(outside)
        code, out = self.run_preflight(where=outside)
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE: -C', out)


class TheDocsLine(Fixture):
    """The first call of every task must not print a failing doc budget as a verdict."""

    def test_a_failing_doc_budget_is_not_printed_as_a_verdict(self):
        self.kit_tool('doc_budget.py', OVER_BUDGET_TAIL, 1)
        docs = self.line(self.run_preflight()[1], 'docs ')
        self.assertIn('FAILED', docs)
        self.assertIn('exit 1', docs)

    def test_a_passing_doc_budget_still_reads_green(self):
        self.kit_tool('doc_budget.py', OK_LINE, 0)
        docs = self.line(self.run_preflight()[1], 'docs ')
        self.assertNotIn('FAILED', docs)
        self.assertIn('DOC BUDGET OK', docs)

    def test_a_doc_budget_that_exits_0_without_saying_ok_does_not_read_green(self):
        self.kit_tool('doc_budget.py', 'doc_budget: base ref main does not exist here.\n', 0)
        self.assertIn('FAILED', self.line(self.run_preflight()[1], 'docs '))

    def test_a_missing_doc_budget_says_not_checked(self):
        self.assertEqual(self.line(self.run_preflight()[1], 'docs '),
                         'docs      NOT CHECKED - no tools/doc_budget.py in the kit, so nothing '
                         'was measured')

    def test_doc_budget_is_given_the_checkout(self):
        with open(os.path.join(self.kit, 'tools', 'doc_budget.py'), 'w', encoding='utf-8') as fh:
            fh.write('import sys\nprint("DOC BUDGET OK " + " ".join(sys.argv[1:]))\n')
        docs = self.line(self.run_preflight()[1], 'docs ')
        self.assertIn('DOC BUDGET OK -C %s' % os.path.normpath(self.root).replace('\\', '/'), docs)


class TheLinesAfterTheWorktrees(Fixture):
    def test_the_stray_check_gets_the_checkout_not_the_cwd(self):
        """A repository's PreflightChecksFromItsOwnRoot: an agent shell's cwd is often another tree
        than the one preflight reports on."""
        elsewhere = os.path.join(self.repo.base, 'elsewhere')
        os.makedirs(elsewhere)
        os.chdir(elsewhere)
        self.run_preflight()
        self.assertEqual(self.strays, [((os.path.normpath(self.root),), {})])

    def test_the_stray_lines_are_printed_and_do_not_change_the_exit(self):
        with mock.patch.object(report.stray, 'check', return_value=(1, ['[FAIL] stray x'])):
            code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertIn('\n[FAIL] stray x\n', out)

    def test_the_main_checkout_gets_a_note(self):
        note = self.line(self.run_preflight()[1], 'NOTE: this is the MAIN checkout')
        self.assertIn('%s -C "%s" <area>/<slug>' % (kit('new_worktree', self.kit), self.root.replace('\\', '/')), note)
        self.assertNotIn('python tools/', note)
        self.assertIn('only under .claude/worktrees/<slug>/', note)

    def test_the_note_names_the_configured_folders(self):
        """The worktrees folder is the repository's; the tool is the kit's, whatever tools_dir says."""
        self.config(repo={'tools_dir': 'scripts', 'worktrees_dir': 'trees'})
        note = self.line(self.run_preflight()[1], 'NOTE: ')
        self.assertIn(kit('new_worktree', self.kit), note)
        self.assertNotIn('scripts/', note)
        self.assertIn('only under trees/<slug>/', note)

    def test_a_worktree_gets_no_note(self):
        wt = self.repo.worktree('y', 'x/y')
        self.assertNotIn('NOTE:', self.run_preflight(where=wt)[1])

    def test_the_default_traps_line_names_a_file_the_checkout_has(self):
        os.makedirs(os.path.join(self.root, 'docs', 'ops'))
        with open(os.path.join(self.root, 'docs', 'ops', 'gotchas.md'), 'w', encoding='utf-8') as fh:
            fh.write('# Gotchas\n')
        self.assertEqual(self.line(self.run_preflight()[1], 'traps '), 'traps     ' + st.DEFAULT_TRAPS)

    def test_no_default_traps_line_when_the_checkout_has_no_such_file(self):
        """The kit itself has no docs/ops/gotchas.md: preflight must not send agents to read it."""
        self.assertNotIn('\ntraps ', self.run_preflight()[1])

    def test_the_traps_line_comes_from_config(self):
        self.config(preflight={'traps': 'grep GOTCHA: in the files you change'})
        self.assertEqual(self.line(self.run_preflight()[1], 'traps '),
                         'traps     grep GOTCHA: in the files you change')

    def test_no_engines_or_ports_line_by_default(self):
        with mock.patch.object(machine, 'run', side_effect=AssertionError('ran')) as run:
            out = self.run_preflight()[1]
        self.assertNotIn('engines ', out)
        self.assertNotIn('ports ', out)
        self.assertFalse([c for c in run.call_args_list if c[0][0][0] in ('tasklist', 'netstat')])

    def test_a_bad_config_refuses(self):
        self.config(preflight={'trap': 'x'})
        code, out = self.run_preflight()
        self.assertEqual(code, 2, out)
        self.assertIn('preflight: refused: kit configuration: ', out)
        self.assertIn('trap', out)

    def test_a_bad_repo_section_refuses(self):
        self.config(repo={'remote': 3})
        code, out = self.run_preflight()
        self.assertEqual(code, 2, out)
        self.assertIn('preflight: refused: kit configuration:', out)

    def test_the_index_line_keeps_the_docs_pointers(self):
        index = self.line(self.run_preflight()[1], 'index ')
        self.assertIn('docs/INDEX.md', index)
        self.assertIn('%s -C "%s" --section' % (kit('doc_index', self.kit), self.root.replace('\\', '/')), index)

    def test_the_index_line_names_the_configured_index(self):
        self.config(doc_index={'index': 'handbook/MAP.md'})
        os.makedirs(os.path.join(self.root, 'handbook'))
        with open(os.path.join(self.root, 'handbook', 'MAP.md'), 'w', encoding='utf-8') as fh:
            fh.write('map\n')
        index = self.line(self.run_preflight()[1], 'index ')
        self.assertIn('docs: handbook/MAP.md |', index)
        self.assertNotIn('docs/INDEX.md', index)

    def test_the_index_line_says_opt_in_when_the_index_file_is_absent(self):
        index = self.line(self.run_preflight()[1], 'index ')
        self.assertIn('docs: docs/INDEX.md (none: the doc index is opt-in) |', index)

    def test_the_index_line_has_no_marker_when_the_index_file_exists(self):
        os.makedirs(os.path.join(self.root, 'docs'))
        with open(os.path.join(self.root, 'docs', 'INDEX.md'), 'w', encoding='utf-8') as fh:
            fh.write('index\n')
        index = self.line(self.run_preflight()[1], 'index ')
        self.assertIn('docs: docs/INDEX.md |', index)
        self.assertNotIn('missing', index)

    def test_the_lock_line_is_last(self):
        out = self.run_preflight()[1]
        self.assertTrue(out.splitlines()[-1].startswith('lock      cpu free (0/4)'), out)

    def test_the_config_line_reads_the_home_it_is_given(self):
        """kitlib.preflight.configline over the scratch home: a copy left in the home folder
        beside the PM repository's file is BROKEN with the leftover error."""
        folder = machine_config(self.home, {'ledger_repos': ['alpha']}, case=self)
        os.makedirs(os.path.join(self.home, user.DIR_NAME))
        with open(os.path.join(self.home, user.DIR_NAME, user.FILE_NAME), 'w', encoding='utf-8') as fh:
            json.dump({'ledger_repos': ['beta']}, fh)
        out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'config '), 'config    BROKEN ' + user.locate(self.home).error.replace('\\', '/'))
        self.assertIn(os.path.join(folder, user.FILE_NAME).replace('\\', '/'), self.line(out, 'config '))
        self.assertLess(out.index('\nconfig '), out.index('\nindex '))


class TheLeftoversLine(Fixture):
    """The PM preflight's merged-worktree WARN (kitlib/preflight/leftovers.py): a worktree whose own
    commits are in origin/master is named with the command that removes it."""

    def setUp(self):
        super().setUp()
        self.config(repo={'main_branch': 'master'})

    def landed(self, name, branch):
        wt = self.repo.worktree(name, branch)
        self.repo.write('%s.txt' % name, 'work\n', root=wt)
        self.repo.commit('work', '%s.txt' % name, cwd=wt)
        self.git('merge', '-q', '--no-ff', '-m', 'Merge %s' % branch, branch)
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')
        return wt

    def test_a_landed_worktree_is_a_warn_with_its_remove_command(self):
        self.landed('done', 'x/done')
        out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'leftovers '),
                         'leftovers WARN 1 under .claude/worktrees - drive each to an end:')
        self.assertRegex(out, r"\n            done \[x/done\]  landed \(its own commits are in origin's master\): "
                         r'python "[^"]*/tools/worktree_remove\.py" -C "[^"]+" done\n')

    def test_the_worktree_preflight_runs_in_is_not_listed(self):
        wt = self.landed('done', 'x/done')
        out = self.run_preflight(where=wt)[1]
        self.assertEqual(self.line(out, 'leftovers '), 'leftovers none under .claude/worktrees')

    def test_a_fresh_or_unlanded_worktree_is_not_listed(self):
        self.repo.worktree('fresh', 'x/fresh')
        wt = self.repo.worktree('open', 'x/open')
        self.repo.write('open.txt', 'work\n', root=wt)
        self.repo.commit('work', 'open.txt', cwd=wt)
        out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'leftovers '), 'leftovers none under .claude/worktrees')

    def test_landed_locally_but_not_pushed_is_not_listed(self):
        wt = self.repo.worktree('local', 'x/local')
        self.repo.write('local.txt', 'work\n', root=wt)
        self.repo.commit('work', 'local.txt', cwd=wt)
        self.git('merge', '-q', '--no-ff', '-m', 'Merge', 'x/local')
        out = self.run_preflight()[1]
        self.assertEqual(self.line(out, 'leftovers '), 'leftovers none under .claude/worktrees')

    def test_an_unreadable_base_is_not_checked_and_the_exit_stays_0(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/master')
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.line(out, 'leftovers ').startswith(
            'leftovers WARN merged worktrees not checked: git rev-parse --verify '
            'refs/remotes/origin/master'), out)

    def test_a_failure_inside_the_check_is_a_warn_line(self):
        with mock.patch('kitlib.worktree.remove.worktree_listing', side_effect=OSError('boom')):
            code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.line(out, 'leftovers '), 'leftovers WARN not checked: OSError: boom')


class TheNextBlock(Fixture):
    """The `next` block prints the kit commands with
    this session's values, so agents run the kit tools instead of improvising."""

    def worktree(self, base_ahead):
        """A linked worktree on branch x/y with one dirty file; master and origin/master
        `base_ahead` commits on."""
        wt = self.repo.worktree('y', 'x/y')
        self.repo.write('README.md', 'changed\n', root=wt)
        for i in range(base_ahead):
            self.repo.write('b%d.txt' % i, 'b\n')
            self.repo.commit('base %d' % i, 'b%d.txt' % i)
        self.git('update-ref', 'refs/remotes/origin/master', 'HEAD')    # landed: pushed too
        return wt

    def block(self, out):
        lines = out.splitlines()
        start = [i for i, l in enumerate(lines) if l.startswith('next ')]
        self.assertEqual(len(start), 1, out)
        block = [lines[start[0]][10:]]
        for l in lines[start[0] + 1:]:
            if not l.startswith(' ' * 10):
                break
            block.append(l[10:])
        return block

    def test_a_worktree_behind_its_base_gets_commit_review_codemap_and_sync(self):
        wt = self.worktree(base_ahead=1)
        code, out = self.run_preflight(where=wt)
        self.assertEqual(code, 0, out)
        block = self.block(out)
        fwd = wt.replace('\\', '/')
        self.assertEqual([l for l in block if 'commit.py' in l],
                         ['%s -C "%s" -m "<subject>" --model "<your model>" '
                          'README.md' % (kit('commit', self.kit), fwd)], block)
        self.assertIn('%s -C "%s" x/y' % (kit('review_pack', self.kit), fwd), block)
        self.assertIn('%s -C "%s" --file README.md' % (kit('codemap', self.kit), fwd), block)
        # the fixture's main branch is master, and with no .kit.json the kit reads it from the
        # repository (docs/kit-json.md "With no file"), so no --base is needed
        self.assertIn('%s -C "%s" <hash the coordinator names> --model '
                      '"<your model>"' % (kit('sync_base', self.kit), fwd), block)
        self.assertTrue([l for l in block if 'kitlib.git.checkout.write_text' in l and 'docs/worktree-paths.md' in l],
                        block)
        self.assertFalse([l for l in block if 'new_worktree.py' in l], block)

    def test_a_worktree_level_with_its_base_gets_no_sync_line(self):
        wt = self.worktree(base_ahead=0)
        block = self.block(self.run_preflight(where=wt)[1])
        self.assertIn('%s -C "%s" x/y' % (kit('review_pack', self.kit), wt.replace('\\', '/')), block)
        self.assertFalse([l for l in block if 'sync_base.py' in l], block)

    def test_the_main_checkout_gets_only_new_worktree(self):
        self.assertEqual(self.block(self.run_preflight()[1]),
                         ['%s -C "%s" <area>/<slug>' % (kit('new_worktree', self.kit), self.root.replace('\\', '/'))])

    def test_the_block_adds_no_git_process(self):
        wt = self.worktree(base_ahead=1)
        with mock.patch.object(nextblock, 'lines', lambda *a, **k: []):
            with Spawns() as without:
                self.run_preflight(where=wt)
        with Spawns() as spawns:
            self.run_preflight(where=wt)
        self.assertEqual(len(spawns.argvs), len(without.argvs), spawns.argvs)

    def test_real_git_a_spaced_file_is_quoted_and_a_new_folder_gives_paths(self):
        wt = self.worktree(base_ahead=0)
        self.repo.write('s p.txt', 'x\n', root=wt)
        block = self.block(self.run_preflight(where=wt)[1])
        commit = [l for l in block if 'commit.py' in l]
        self.assertEqual(len(commit), 1, block)
        self.assertTrue(commit[0].endswith('" README.md "s p.txt"'), commit)
        self.assertIn('%s -C "%s" --file README.md' % (kit('codemap', self.kit), wt.replace('\\', '/')), block)
        self.repo.write('nd/f.txt', 'x\n', root=wt)
        block = self.block(self.run_preflight(where=wt)[1])
        self.assertTrue([l for l in block if 'commit.py' in l][0].endswith(' <paths>'), block)

    def test_real_git_a_non_ascii_name_is_given_as_typed(self):
        wt = self.worktree(base_ahead=0)
        self.repo.write('caf\u00e9.txt', 'x\n', root=wt)
        block = self.block(self.run_preflight(where=wt)[1])
        self.assertTrue([l for l in block if 'commit.py' in l][0].endswith(' caf\u00e9.txt'), block)


class TheNextBlockLines(unittest.TestCase):
    """nextblock.lines on its own: the readings handed in, the lines made."""
    ROOT = '\\'.join(('D:', 'src', 'Repo', '.claude', 'worktrees', 'y'))
    WT = '"D:/src/Repo/.claude/worktrees/y"'

    def block(self, dirty=(), branch='x/y', base='main', behind='0', in_main=False):
        lines = nextblock.lines(self.ROOT, 'tools', branch, base, 'main', behind, list(dirty),
                                in_main)
        self.assertTrue(lines[0].startswith('next      '), lines)
        self.assertTrue(all(l.startswith(' ' * 10) for l in lines[1:]), lines)
        return [l[10:] for l in lines]

    def commit(self, block):
        [line] = [l for l in block if 'commit.py' in l]
        return line

    def test_the_worktree_is_always_quoted_with_forward_slashes(self):
        block = self.block([(' M', 'a.txt')], behind='2')
        self.assertTrue(self.commit(block).startswith('python tools/commit.py -C %s -m' % self.WT),
                        block)
        self.assertIn('python tools/sync_base.py -C %s <hash the coordinator names> --model '
                      '"<your model>"' % self.WT, block)

    def test_a_path_with_a_space_is_quoted_on_both_lines(self):
        block = self.block([('??', 'a b.txt'), (' M', 'c.txt')])
        self.assertTrue(self.commit(block).endswith('"<your model>" "a b.txt" c.txt'), block)
        self.assertIn(CODEMAP + ' --file "a b.txt"', block)

    def test_a_name_no_shell_word_can_carry_gives_paths(self):
        block = self.block([('??', 'a"b.txt'), (' M', 'c.txt')])
        self.assertTrue(self.commit(block).endswith(' <paths>'), block)
        self.assertIn(CODEMAP + ' --file c.txt', block)

    def test_more_than_12_paths_are_capped(self):
        names = ['f%02d.txt' % i for i in range(14)]
        block = self.block([('??', n) for n in names])
        self.assertTrue(self.commit(block).endswith(' %s <+2 more>' % ' '.join(names[:12])), block)

    def test_an_untracked_folder_gives_paths(self):
        block = self.block([('??', 'newdir/')])
        self.assertTrue(self.commit(block).endswith(' <paths>'), block)
        self.assertIn(CODEMAP, block)
        block = self.block([(' M', 'a.txt'), ('??', 'newdir/')])
        self.assertTrue(self.commit(block).endswith(' <paths>'), block)
        self.assertIn(CODEMAP + ' --file a.txt', block)

    def test_a_rename_gives_paths_and_the_new_name_to_codemap(self):
        block = self.block([('R ', 'new.txt')])          # refs.status gives a rename's new path
        self.assertTrue(self.commit(block).endswith(' <paths>'), block)
        self.assertIn(CODEMAP + ' --file new.txt', block)
        block = self.block([('R ', 'n e.txt')])
        self.assertIn(CODEMAP + ' --file "n e.txt"', block)

    def test_a_copy_gives_paths(self):
        self.assertTrue(self.commit(self.block([('C ', 'b.txt')])).endswith(' <paths>'))

    def test_a_clean_tree_prints_paths_and_a_bare_codemap(self):
        block = self.block([])
        self.assertTrue(self.commit(block).endswith('"<your model>" <paths>'), block)
        self.assertIn(CODEMAP, block)

    def test_the_base_flag_only_when_the_base_is_not_the_main_branch(self):
        [sync] = [l for l in self.block(behind='1', base='main') if 'sync_base.py' in l]
        self.assertNotIn('--base', sync)
        [sync] = [l for l in self.block(behind='1', base='master') if 'sync_base.py' in l]
        self.assertTrue(sync.endswith(' --base master'), sync)

    def test_an_unknown_behind_count_prints_no_sync_line(self):
        self.assertFalse([l for l in self.block(behind='?') if 'sync_base.py' in l])
        self.assertFalse([l for l in self.block(behind=None) if 'sync_base.py' in l])

    def test_a_detached_head_prints_a_branch_placeholder(self):
        self.assertIn('python tools/review_pack.py -C %s <branch>' % self.WT, self.block(branch='HEAD'))

    def test_a_kit_folder_with_a_space_is_quoted(self):
        lines = nextblock.lines(self.ROOT, 'C:/my kits/pm-kit/tools', 'x/y', 'main', 'main', '0', [],
                                False)
        self.assertTrue(lines[0].startswith('next      python "C:/my kits/pm-kit/tools/commit.py" -C '),
                        lines)

    def test_shell_word(self):
        self.assertEqual(nextblock.shell_word('a/b-c_d.txt'), 'a/b-c_d.txt')
        self.assertEqual(nextblock.shell_word('a (1).txt'), '"a (1).txt"')
        for bad in ('', 'a$b', 'a`b', 'a\\b', 'a!b', 'a"b'):
            self.assertIsNone(nextblock.shell_word(bad), bad)


class TheReportHooks(Fixture):
    def setUp(self):
        super().setUp()
        self.tools = os.path.join(self.root, 'tools')
        os.makedirs(self.tools)
        path = list(sys.path)
        self.addCleanup(setattr, sys, 'path', path)

    def module(self, name, text, folder=None):
        with open(os.path.join(folder or self.tools, name + '.py'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(text)

    def test_a_hook_prints_its_lines(self):
        self.module('daily_stub', 'import daily_sibling\n'
                                  'def summary_lines():\n'
                                  '    return ["daily     OK " + daily_sibling.WORD, "  more"]\n')
        self.module('daily_sibling', 'WORD = "stub"\n')
        self.config(preflight={'report_hooks': ['daily_stub:summary_lines']})
        self.assertIn('\ndaily     OK stub\n  more\n', self.run_preflight()[1])

    def test_the_hook_is_read_from_the_configured_tools_dir(self):
        os.makedirs(os.path.join(self.root, 'scripts'))
        self.module('h', 'def lines():\n    return ["from scripts"]\n',
                    os.path.join(self.root, 'scripts'))
        self.config(repo={'tools_dir': 'scripts'}, preflight={'report_hooks': ['h:lines']})
        self.assertIn('\nfrom scripts\n', self.run_preflight()[1])

    def test_a_missing_hook_module_is_one_not_checked_line(self):
        self.config(preflight={'report_hooks': ['nope:lines']})
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertIn('NOT CHECKED - nope:lines: FileNotFoundError', self.line(out, 'hook '))
        self.assertIn('\nindex ', out)                # and preflight went on

    def test_a_raising_hook_is_one_not_checked_line(self):
        self.module('bad', 'def lines():\n    raise ValueError("simulated")\n')
        self.config(preflight={'report_hooks': ['bad:lines']})
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertIn('hook      NOT CHECKED - bad:lines: ValueError: simulated', out)

    def test_a_missing_function_is_one_not_checked_line(self):
        self.module('empty', 'X = 1\n')
        self.config(preflight={'report_hooks': ['empty:lines']})
        self.assertIn('hook      NOT CHECKED - empty:lines: AttributeError', self.run_preflight()[1])

    def test_a_hook_that_fails_to_import_is_one_not_checked_line(self):
        self.module('broken', 'import no_such_module_here\n')
        self.assertEqual(hooks.lines(self.tools, ['broken:lines']),
                         ['hook      NOT CHECKED - broken:lines: ModuleNotFoundError: No module '
                          "named 'no_such_module_here'"])

    def test_a_hook_gets_its_own_sibling_not_the_kits_tool_of_that_name(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'tools'))
        self.addCleanup(sys.modules.pop, 'pieces', None)
        import pieces as kit_pieces              # the kit's tools/pieces.py, imported before
        self.assertFalse(hasattr(kit_pieces, 'table'))
        self.module('pieces', 'def table():\n    return ["repo table"]\n')
        self.module('shadow', 'import pieces\n'
                              'def lines():\n    return pieces.table()\n')
        before = list(sys.path)
        self.assertEqual(hooks.lines(self.tools, ['shadow:lines']), ['repo table'])
        self.assertIs(sys.modules['pieces'], kit_pieces)     # kit code still has the kit's
        self.assertEqual(sys.path, before)
        del sys.modules['pieces']                            # and with none imported first
        self.assertEqual(hooks.lines(self.tools, ['shadow:lines']), ['repo table'])
        self.assertNotIn('pieces', sys.modules)

    def test_a_hook_writes_no_bytecode_into_the_repo(self):
        """A terminal without PYTHONDONTWRITEBYTECODE would leave tools/__pycache__ (the hook's
        and its sibling's .pyc) as untracked files in the repo's tree."""
        self.module('pyc_sibling', 'WORD = "sib"\n')
        self.module('pyc_hook', 'import pyc_sibling\n'
                                'def lines():\n    return [pyc_sibling.WORD]\n')
        with mock.patch.object(sys, 'dont_write_bytecode', False):
            self.assertEqual(hooks.lines(self.tools, ['pyc_hook:lines']), ['sib'])
            self.assertFalse(sys.dont_write_bytecode)          # put back as it was
        self.assertFalse(os.path.exists(os.path.join(self.tools, '__pycache__')),
                         os.listdir(self.tools))


class TheReportHookScope(Fixture):
    """What _repo_first displaces for a hook and what it drops afterwards: only copies from
    outside the tools dir are displaced, only modules from the tools dir are dropped."""

    module = TheReportHooks.module

    def setUp(self):
        super().setUp()
        self.tools = os.path.join(self.root, 'tools')
        os.makedirs(self.tools)
        path = list(sys.path)
        self.addCleanup(setattr, sys, 'path', path)
        self.side = os.path.join(self.root, 'kit_side')     # stands in for the kit's tools dir
        os.makedirs(self.side)
        sys.path.insert(0, self.side)

    def fresh(self, name):
        """`name` not loaded for the test, and put back as it was after it."""
        saved = sys.modules.pop(name, None)
        self.addCleanup(lambda: sys.modules.__setitem__(name, saved) if saved is not None
                        else sys.modules.pop(name, None))

    def outside(self, name, word='kit', package=False):
        """A module (or package with a `sub` module) of `name` loaded from outside the tools dir."""
        for key in [k for k in sys.modules if k == name or k.startswith(name + '.')]:
            del sys.modules[key]
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == name or k.startswith(name + '.')])
        if package:
            os.makedirs(os.path.join(self.side, name))
            self.module('__init__', '', os.path.join(self.side, name))
            self.module('sub', 'WORD = %r\n' % word, os.path.join(self.side, name))
            __import__(name + '.sub')
            return sys.modules[name], sys.modules[name + '.sub']
        self.module(name, 'WORD = %r\n' % word, self.side)
        importlib.invalidate_caches()         # a second file in the same mtime tick
        return __import__(name)

    def test_a_builtin_of_a_sibling_name_is_not_displaced(self):
        import time
        self.assertIn('time', sys.builtin_module_names)
        self.module('time', 'X = 1\n')
        self.module('builtin_hook', 'import sys\nHAD = sys.modules.get("time")\n'
                                    'def lines():\n    return [id(HAD)]\n')
        self.assertEqual(hooks.lines(self.tools, ['builtin_hook:lines']), [str(id(time))])
        self.assertIs(sys.modules['time'], time)

    def test_a_loaded_module_from_the_tools_dir_is_not_displaced(self):
        mine = self.outside('hs_mine')
        mine.__file__ = os.path.join(self.tools, 'hs_mine.py')       # as if loaded from there
        self.module('hs_mine', 'WORD = "reloaded"\n')
        self.module('mine_hook', 'import hs_mine\ndef lines():\n    return [hs_mine.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['mine_hook:lines']), ['kit'])

    def test_a_module_the_hook_imports_from_elsewhere_stays_loaded(self):
        self.fresh('colorsys')
        self.module('std_hook', 'import colorsys\ndef lines():\n    return ["ok"]\n')
        self.assertEqual(hooks.lines(self.tools, ['std_hook:lines']), ['ok'])
        self.assertIn('colorsys', sys.modules)          # stdlib the hook pulled in is kept

    def test_a_displaced_packages_submodules_go_too(self):
        kit_pkg, kit_sub = self.outside('hs_pkg', package=True)
        os.makedirs(os.path.join(self.tools, 'hs_pkg'))
        self.module('__init__', '', os.path.join(self.tools, 'hs_pkg'))
        self.module('sub', 'WORD = "repo"\n', os.path.join(self.tools, 'hs_pkg'))
        self.module('pkg_hook', 'import hs_pkg.sub\ndef lines():\n    return [hs_pkg.sub.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['pkg_hook:lines']), ['repo'])
        self.assertIs(sys.modules['hs_pkg'], kit_pkg)
        self.assertIs(sys.modules['hs_pkg.sub'], kit_sub)

    def test_a_pyw_sibling_displaces_the_kits_module(self):
        kit = self.outside('hs_pyw')
        with open(os.path.join(self.tools, 'hs_pyw.pyw'), 'w', encoding='utf-8') as fh:
            fh.write('WORD = "repo"\n')
        self.module('pyw_hook', 'import hs_pyw\ndef lines():\n    return [hs_pyw.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['pyw_hook:lines']), ['repo'])
        self.assertIs(sys.modules['hs_pyw'], kit)

    def test_a_pyd_sibling_displaces_the_kits_module(self):
        """A tagged `x.cp314-win_amd64.pyd` and a plain `x.pyd` both name a sibling."""
        kit, plain = self.outside('hs_pyd'), self.outside('hs_pyd_plain')
        for entry in ('hs_pyd.cp314-win_amd64.pyd', 'hs_pyd_plain.pyd'):
            with open(os.path.join(self.tools, entry), 'wb') as fh:
                fh.write(b'')
        self.module('pyd_hook', 'import sys\n'
                                'HAD = ["hs_pyd" in sys.modules, "hs_pyd_plain" in sys.modules]\n'
                                'def lines():\n    return HAD\n')
        self.assertEqual(hooks.lines(self.tools, ['pyd_hook:lines']), ['False', 'False'])
        self.assertIs(sys.modules['hs_pyd'], kit)
        self.assertIs(sys.modules['hs_pyd_plain'], plain)

    def test_a_module_from_elsewhere_bound_to_a_repo_stdlib_name_is_dropped(self):
        """tools/json.py shadows the stdlib json; pdb, first imported during the hook, does
        `import json` and gets the repo's copy. It must not stay loaded bound to it."""
        import json as std_json
        self.fresh('pdb')
        self.module('json', 'WORD = "repo"\n')
        self.module('json_hook', 'import pdb\ndef lines():\n    return [pdb.json.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['json_hook:lines']), ['repo'])
        self.assertIs(sys.modules['json'], std_json)
        bound = [k for k, m in list(sys.modules.items())
                 if getattr(getattr(m, '__dict__', {}).get('json'), 'WORD', None) == 'repo']
        self.assertEqual(bound, [])

    def test_a_bytes_file_in_sys_modules_does_not_stop_the_restore(self):
        kit = self.outside('hs_bfile')
        self.addCleanup(sys.modules.pop, 'hs_bytes', None)
        self.module('hs_bfile', 'WORD = "repo"\n')
        self.module('bytes_hook', 'import sys, types, hs_bfile\n'
                                  'm = types.ModuleType("hs_bytes")\n'
                                  'm.__file__ = b"Z:\\\\elsewhere\\\\hs_bytes.py"\n'
                                  'sys.modules["hs_bytes"] = m\n'
                                  'def lines():\n    return [hs_bfile.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['bytes_hook:lines']), ['repo'])
        self.assertIs(sys.modules['hs_bfile'], kit)

    def test_a_namespace_package_on_both_sides_is_restored(self):
        for key in ('hs_ns', 'hs_ns.other', 'hs_ns.mod'):
            self.addCleanup(sys.modules.pop, key, None)
        os.makedirs(os.path.join(self.side, 'hs_ns'))
        self.module('other', 'WORD = "kit"\n', os.path.join(self.side, 'hs_ns'))
        __import__('hs_ns.other')
        kit_ns, kit_other = sys.modules['hs_ns'], sys.modules['hs_ns.other']
        self.assertIsNone(getattr(kit_ns, '__file__', None))          # a namespace package
        os.makedirs(os.path.join(self.tools, 'hs_ns'))
        self.module('mod', 'WORD = "repo"\n', os.path.join(self.tools, 'hs_ns'))
        self.module('ns_hook', 'import hs_ns.mod\ndef lines():\n    return [hs_ns.mod.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['ns_hook:lines']), ['repo'])
        self.assertIs(sys.modules['hs_ns'], kit_ns)
        self.assertIs(sys.modules['hs_ns.other'], kit_other)
        self.assertNotIn('hs_ns.mod', sys.modules)

    def test_a_namespace_package_only_in_tools_is_dropped(self):
        for key in ('hs_ns_only', 'hs_ns_only.mod'):
            self.addCleanup(sys.modules.pop, key, None)
        os.makedirs(os.path.join(self.tools, 'hs_ns_only'))
        self.module('mod', 'WORD = "repo"\n', os.path.join(self.tools, 'hs_ns_only'))
        self.module('ns_only_hook', 'import hs_ns_only.mod\n'
                                    'def lines():\n    return [hs_ns_only.mod.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['ns_only_hook:lines']), ['repo'])
        self.assertNotIn('hs_ns_only', sys.modules)
        self.assertNotIn('hs_ns_only.mod', sys.modules)

    def test_with_nothing_shadowed_only_the_tools_dir_modules_go(self):
        """The narrow rule: no kit copy set aside, no stdlib name in the tools dir, so the
        stdlib module the hook pulled in stays and the repo's module goes."""
        self.fresh('colorsys')
        self.addCleanup(sys.modules.pop, 'hs_narrow', None)
        self.module('hs_narrow', 'WORD = "repo"\n')
        self.module('narrow_hook', 'import colorsys, hs_narrow\n'
                                   'def lines():\n    return [hs_narrow.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['narrow_hook:lines']), ['repo'])
        self.assertIn('colorsys', sys.modules)
        self.assertNotIn('hs_narrow', sys.modules)

    def test_a_repo_module_with_a_stdlib_name_not_loaded_before_drops_everything_new(self):
        """tools/colorsys.py with the stdlib colorsys not loaded: nothing is set aside, yet the
        repo's colorsys has a stdlib name, so a module from elsewhere imported meanwhile goes."""
        self.fresh('colorsys')
        self.addCleanup(sys.modules.pop, 'hs_elsewhere', None)
        self.module('hs_elsewhere', 'WORD = "kit"\n', self.side)        # not imported before
        importlib.invalidate_caches()
        self.module('colorsys', 'WORD = "repo"\n')
        self.module('stdname_hook', 'import colorsys, hs_elsewhere\n'
                                    'def lines():\n    return [colorsys.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['stdname_hook:lines']), ['repo'])
        self.assertNotIn('hs_elsewhere', sys.modules)
        self.assertNotIn('colorsys', sys.modules)

    def test_a_raise_while_dropping_still_restores_path_and_modules(self):
        kit = self.outside('hs_fin')
        self.module('hs_fin', 'WORD = "repo"\n')
        self.module('fin_hook', 'import hs_fin\ndef lines():\n    return [hs_fin.WORD]\n')
        real = hooks._from_tools

        def from_tools(module, root):          # raises only in the drop step, where the
            if sys.path and sys.path[0] == self.tools:      # tools dir is still first
                raise RuntimeError('simulated')
            return real(module, root)
        before = list(sys.path)
        with mock.patch.object(hooks, '_from_tools', from_tools):
            got = hooks.lines(self.tools, ['fin_hook:lines'])
        self.assertEqual(got, ['repo', 'hook      NOT CHECKED - fin_hook:lines: RuntimeError: '
                                       'simulated'])
        self.assertEqual(sys.path, before)
        self.assertIs(sys.modules['hs_fin'], kit)

    def test_a_none_entry_the_hook_adds_is_dropped_when_everything_new_goes(self):
        """`sys.modules[name] = None` (an import blocker) is new too: with a kit copy set aside,
        everything new goes, that entry included."""
        kit = self.outside('hs_nonekit')
        self.addCleanup(sys.modules.pop, 'hs_none', None)
        self.module('hs_nonekit', 'WORD = "repo"\n')
        self.module('none_hook', 'import sys, hs_nonekit\nsys.modules["hs_none"] = None\n'
                                 'def lines():\n    return [hs_nonekit.WORD]\n')
        self.assertEqual(hooks.lines(self.tools, ['none_hook:lines']), ['repo'])
        self.assertIs(sys.modules['hs_nonekit'], kit)
        self.assertNotIn('hs_none', sys.modules)

    def restored_after(self, raising_text, call=None):
        """A hook that loads a repo sibling over a kit copy, pulls in a stdlib module, then runs
        `raising_text`; afterwards sys.path and sys.modules are exactly as before: the kit copy
        is back, and since a displaced name was loaded from the tools dir, the stdlib module the
        hook pulled in is dropped with the repo's modules."""
        kit = self.outside('hs_both')
        self.fresh('colorsys')
        self.module('hs_both', 'WORD = "repo"\n')
        self.module('hs_only', 'WORD = "repo"\n')
        self.module('r_hook', 'import colorsys, hs_both, hs_only\n' + raising_text)
        before = list(sys.path)
        modules = dict(sys.modules)
        got = (call or (lambda: hooks.lines(self.tools, ['r_hook:lines'])))()
        self.assertEqual(sys.path, before)
        self.assertIs(sys.modules['hs_both'], kit)
        self.assertEqual(set(sys.modules), set(modules))
        self.assertFalse([k for k in modules if sys.modules[k] is not modules[k]])
        return got

    def test_state_is_restored_after_a_hook_that_raises_at_import(self):
        [line] = self.restored_after('raise ValueError("at import")\n')
        self.assertEqual(line, 'hook      NOT CHECKED - r_hook:lines: ValueError: at import')

    def test_state_is_restored_after_a_hook_that_raises_at_call(self):
        [line] = self.restored_after('def lines():\n    raise ValueError("at call")\n')
        self.assertEqual(line, 'hook      NOT CHECKED - r_hook:lines: ValueError: at call')

    def test_state_is_restored_after_a_keyboard_interrupt_which_is_not_caught(self):
        def call():
            with self.assertRaises(KeyboardInterrupt):
                hooks.lines(self.tools, ['r_hook:lines'])
        self.restored_after('def lines():\n    raise KeyboardInterrupt\n', call)

    def test_a_system_exit_is_one_not_checked_line(self):
        self.module('exits', 'raise SystemExit(3)\n')
        self.module('argv_sib', 'import argparse\n'
                                'argparse.ArgumentParser().parse_args(["--no-such-flag"])\n')
        self.module('argv_hook', 'import argv_sib\ndef lines():\n    return []\n')
        self.module('fine', 'def lines():\n    return ["still ran"]\n')
        with contextlib.redirect_stderr(io.StringIO()):
            got = hooks.lines(self.tools, ['exits:lines', 'argv_hook:lines', 'fine:lines'])
        self.assertEqual(got, ['hook      NOT CHECKED - exits:lines: SystemExit: 3',
                               'hook      NOT CHECKED - argv_hook:lines: SystemExit: 2',
                               'still ran'])


TASKLIST = ('\nImage Name                     PID Session Name\n'
            '========================= ======== ================\n'
            'Godot_v4.3-stable.exe         4242 Console\n'
            'engine.exe                    4343 Console\n'
            'notepad.exe                   4444 Console\n')
NETSTAT = ('\nActive Connections\n\n  Proto  Local Address          Foreign Address\n'
           '  UDP    0.0.0.0:41001          *:*\n'
           '  UDP    [::]:41199             *:*\n'
           '  UDP    0.0.0.0:8000           *:*\n'
           '  UDP    0.0.0.0:53             *:*\n')


class TheEngineAndPortLines(unittest.TestCase):
    """A Godot repository's (and a mixed one's Godot-only) engines and ports lines, driven by config."""

    def readings(self, cfg, answers):
        s = dict({'engines': [], 'udp_ports': None}, **cfg)
        with mock.patch.object(machine, 'run', lambda argv, **kw: answers[argv[0]]):
            return machine.preflight_lines(s)

    def test_engines_are_counted_by_prefix_case_blind(self):
        [line] = self.readings({'engines': ['godot', 'engine.exe']}, {'tasklist': (0, TASKLIST, '')})
        self.assertTrue(line.startswith('engines   2 running (Godot_v4.3-stable.exe, engine.exe)'),
                        line)
        self.assertIn('not yours? do not kill it', line)

    def test_no_engine_running_says_0(self):
        [line] = self.readings({'engines': ['godot']}, {'tasklist': (0, 'notepad.exe 1 Console\n', '')})
        self.assertEqual(line, 'engines   0 running')

    def test_a_failed_tasklist_is_unknown_not_0(self):
        [line] = self.readings({'engines': ['godot']}, {'tasklist': (1, '', 'not found')})
        self.assertEqual(line, 'engines   UNKNOWN - tasklist failed: not found')

    def test_ports_in_the_range_are_listed(self):
        [line] = self.readings({'udp_ports': [41000, 41199]}, {'netstat': (0, NETSTAT, '')})
        self.assertEqual(line, 'ports     41000-41199 in use: [41001, 41199]')

    def test_no_port_in_use_says_none(self):
        [line] = self.readings({'udp_ports': [9000, 9001]}, {'netstat': (0, NETSTAT, '')})
        self.assertEqual(line, 'ports     9000-9001 in use: none')

    def test_a_failed_netstat_is_unknown(self):
        [line] = self.readings({'udp_ports': [41000, 41199]}, {'netstat': (1, '', '')})
        self.assertEqual(line, 'ports     UNKNOWN - netstat failed: exit 1')

    def test_a_command_that_cannot_start_is_a_failed_reading(self):
        with mock.patch.object(machine.subprocess, 'run', side_effect=OSError('no such program')):
            self.assertEqual(machine.run(['tasklist']), (1, '', 'no such program'))


class TheConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='preflight_cfg_')
        self.addCleanup(gitfixture.rmtree, self.tmp)

    def settings(self, section):
        if section is not None:
            with open(os.path.join(self.tmp, '.kit.json'), 'w', encoding='utf-8') as fh:
                json.dump({'preflight': section}, fh)
        return st.settings(load_config(self.tmp))

    def test_no_config_is_the_kit_defaults(self):
        self.assertEqual(self.settings(None), {'traps': st.DEFAULT_TRAPS, 'report_hooks': [],
                                               'engines': [], 'udp_ports': None})

    def test_every_key_set(self):
        got = self.settings({'traps': ' t ', 'report_hooks': ['daily_full:summary_lines'],
                             'engines': ['Godot', 'engine.exe'], 'udp_ports': [41000, 41199],
                             '_note': 'notes are allowed'})
        self.assertEqual(got, {'traps': 't', 'report_hooks': ['daily_full:summary_lines'],
                               'engines': ['godot', 'engine.exe'], 'udp_ports': [41000, 41199]})

    def test_what_it_cannot_use_refuses(self):
        for bad, words in (({'traps': ''}, 'traps'),
                           ({'traps': 3}, 'traps'),
                           ({'report_hooks': 'daily_full:summary_lines'}, 'report_hooks'),
                           ({'report_hooks': ['daily_full']}, 'report_hooks'),
                           ({'report_hooks': ['a:b:c']}, 'report_hooks'),
                           ({'report_hooks': ['tools/x:y']}, 'report_hooks'),
                           ({'report_hooks': ['a.b:c']}, 'report_hooks'),
                           ({'engines': 'godot'}, 'engines'),
                           ({'engines': ['']}, 'engines'),
                           ({'udp_ports': [41199, 41000]}, 'udp_ports'),
                           ({'udp_ports': [41000]}, 'udp_ports'),
                           ({'udp_ports': [0, 70000]}, 'udp_ports'),
                           ({'udp_ports': [True, 9]}, 'udp_ports'),
                           ({'udp_port': [1, 2]}, 'udp_port')):
            with self.assertRaises(ConfigError, msg=bad) as cm:
                self.settings(bad)
            self.assertIn(words, str(cm.exception), bad)


def snap(**over):
    s = json.loads(json.dumps(FREE))
    s.update(over)
    return s


def run_row(rid, repo, slug, kind, since, **extra):
    return dict({'id': rid, 'state': 'held', 'repo': repo, 'slug': slug, 'kind': kind,
                 'since': since, 'held_since': since, 'background': False, 'overrun': None}, **extra)


class TheLockLine(unittest.TestCase):
    """The lock line from the kit lock's own snapshot (tools/machine_lock.py status --json)."""

    def test_a_free_lock(self):
        self.assertEqual(lockline.summary(snap()), 'cpu free (0/4) | gpu free (0/2) | waiting 0')

    def test_holders_are_named_with_their_slots_kind_run_and_minutes(self):
        s = snap(runs=[run_row('r-h', 'gb', 'tests', 'engine-test', '2026-10-07T11:56:00Z'),
                       run_row('r-b', 'rt', 'box', 'build', '2026-10-07T11:59:00Z'),
                       run_row('r-m', 'pm', 'bench', 'measure', '2026-10-07T11:50:00Z',
                               overrun={'s': 125})])
        for i, holder in enumerate(('r-h', 'r-h', 'r-b', None)):
            s['slots']['cpu'][i]['holder'] = holder
        for i in range(2):
            s['slots']['gpu'][i]['holder'] = 'r-m'
        line = lockline.summary(s)
        self.assertIn('cpu 3/4: slot0-1 gb/tests (engine-test, run r-h, 4 min), '
                      'slot2 rt/box (build, run r-b, 1 min)', line)
        self.assertIn('gpu 2/2: slot0-1 pm/bench (measure, run r-m, 10 min, OVERRUN +2 min)', line)

    def test_a_background_run_is_named(self):
        s = snap(runs=[run_row('r-bg', 'rt', 'arm-a', None, '2026-10-07T11:00:00Z', state='bg',
                               background=True)])
        s['slots']['gpu'][0]['holder'] = 'r-bg'
        self.assertIn('gpu 1/2: slot0 rt/arm-a (?, run r-bg, 60 min, background)',
                      lockline.summary(s))

    def test_the_queue_is_shown_in_order_with_needs_and_start(self):
        now = datetime.datetime(2026, 10, 7, 12, 0, tzinfo=datetime.timezone.utc)
        today = now + datetime.timedelta(minutes=5)
        later = now + datetime.timedelta(days=3)

        def row(position, slug, kind, cpu, gpu, est, **extra):
            return dict({'id': 'r-' + slug, 'position': position, 'repo': 'pm', 'slug': slug,
                         'kind': kind, 'needs': {'cpu': cpu, 'gpu': gpu}, 'since': 'x',
                         'est_start': est.strftime(lockline.ISO) if est else None,
                         'backfill': False, 'parked': False}, **extra)
        s = snap(queue={'entries': [row(1, 'gb', 'graph-build', 6, 0, today),
                                    row(2, 'ai', 'local-ai', 1, 1, later, backfill=True),
                                    row(3, 'gone', 'build', 2, 0, None, parked=True)],
                        'error': None})
        line = lockline.summary(s)
        if today.astimezone().date() != now.astimezone().date():
            self.skipTest('the local day turns between the two fixture times')
        self.assertIn('waiting 3: #1 pm/gb (graph-build, 6 cores, starts %s), '
                      '#2 pm/ai (local-ai, 1 core + 1 gpu, starts %s, backfill), '
                      '#3 pm/gone (build, 2 cores, start not planned, parked)'
                      % (today.astimezone().strftime('%H:%M'),
                         later.astimezone().strftime('%Y-%m-%d %H:%M')), line)

    def test_an_unreadable_queue_is_said_never_waiting_0(self):
        line = lockline.summary(snap(queue={'entries': None, 'error': 'queue.json: bad JSON'}))
        self.assertIn('queue unreadable: queue.json: bad JSON', line)
        self.assertNotIn('waiting', line)

    def test_holds_live_and_next_are_named(self):
        holds = [{'id': 'h-1', 'owner': 'owner', 'scope': 'half', 'live': True,
                  'start_local': '09:00', 'end_local': '18:00', 'note': 'gaming'},
                 {'id': 'h-2', 'owner': 'agent', 'scope': 'whole', 'live': False,
                  'start_local': '2026-10-08 09:00', 'end_local': '2026-10-08 10:00'}]
        line = lockline.summary(snap(holds=holds))
        self.assertIn('waiting 0 | hold half until 18:00: gaming (owner) '
                      '| hold next: whole from 2026-10-08 09:00 (agent)', line)
        line = lockline.summary(snap(holds=[], holds_error='holds.json: denied'))
        self.assertIn('holds unreadable: holds.json: denied', line)

    def test_a_snapshot_of_another_shape_is_unknown_never_free(self):
        for bad in (None, [], {'lock': {'held': [], 'waiting': []}},
                    snap(slots={'cpu': []}), snap(runs=None), snap(queue=[]),
                    snap(queue={'entries': {'not': 'a list'}}),
                    snap(queue={'entries': ['x']}), snap(holds={'x': 1})):
            line = lockline.summary(bad)
            self.assertTrue(line.startswith('unknown ('), (bad, line))
            self.assertNotIn('free', line)

    def tool(self, kit, text, code=0):
        os.makedirs(os.path.join(kit, 'tools'), exist_ok=True)
        with open(os.path.join(kit, 'tools', 'machine_lock.py'), 'w', encoding='utf-8') as fh:
            fh.write('import sys\nsys.stdout.write(%r)\nsys.exit(%d)\n' % (text, code))

    def test_a_reading_that_fails_is_unknown_never_free(self):
        with tempfile.TemporaryDirectory() as kit:
            self.assertEqual(lockline.line_for(kit), 'unknown (no tools/machine_lock.py in the kit)')
            for text, code in ((json.dumps(FREE), 1), ('not json', 0),
                               (json.dumps({'lock': {'held': []}}), 0)):
                self.tool(kit, text, code)
                line = lockline.line_for(kit)
                self.assertTrue(line.startswith('unknown ('), (text, code, line))
                self.assertNotIn('free', line)
            self.tool(kit, json.dumps(FREE))
            self.assertEqual(lockline.line_for(kit), 'cpu free (0/4) | gpu free (0/2) | waiting 0')

    def test_the_real_kit_tool_on_a_scratch_store_reads_as_free(self):
        """The shape this module reads is the one the kit tool prints (--test-root: a scratch
        store, never the live lock)."""
        with tempfile.TemporaryDirectory() as root:
            r = subprocess.run([sys.executable, '-B', os.path.join(KIT, 'tools', 'machine_lock.py'),
                                'status', '--test-root', root, '--json'], capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            line = lockline.summary(json.loads(r.stdout))
        self.assertRegex(line, r'^cpu free \(0/\d+\) \| gpu free \(0/\d+\) \| waiting 0$')


if __name__ == '__main__':
    unittest.main()


class TheShortReport(unittest.TestCase):
    LINES = ['repo      /r', 'branch    kit/x @ abc1234', 'base      main @ abc1234 | origin/main @ abc1234 (equal)',
             'dirty     0 file(s)', 'worktrees 2', '            /r  abc [main]', 'leftovers none under w',
             'docs      DOC BUDGET OK (HEAD)', 'main checkout clean: no stray untracked or uncommitted files',
             'daily     GREEN abc', 'config    /c', 'index     docs: docs/INDEX.md', 'next      python x',
             '          python y', 'lock      cpu free (0/12)']

    def test_fine_readings_are_left_out_and_the_kit_folder_is_named(self):
        out = report.brief(self.LINES, 'Z:' + chr(92) + 'kit')
        self.assertEqual([l.split()[0] for l in out[:3]], ['repo', 'branch', 'base'])
        self.assertEqual([l for l in out if l.startswith(('worktrees', 'dirty', 'docs', 'lock', 'next', '            '))], [])
        self.assertIn('kit       Z:/kit', out)
        self.assertLess(len(out), len(self.LINES))

    def test_a_leftovers_list_is_kept_and_leftovers_none_is_dropped(self):
        head = self.LINES[:3]
        kept = report.brief(head + ['leftovers 1 under w', '            a [x]  landed'], '/k')
        self.assertIn('leftovers 1 under w', kept)
        self.assertIn('            a [x]  landed', kept)
        self.assertFalse(any(l.startswith('leftovers') for l in report.brief(head + ['leftovers none under w'], '/k')))

    def test_a_problem_line_and_its_continuation_are_kept(self):
        lines = self.LINES[:3] + ['docs      DOC BUDGET FAILED x', 'dirty     2 file(s)', '            M a', '            M b',
                                  'daily     RED abc', 'config    BROKEN - x', 'worktrees 1', '            /r  abc [main]']
        out = report.brief(lines, '/k')
        for kept in ('docs      DOC BUDGET FAILED x', 'dirty     2 file(s)', '            M a', 'daily     RED abc',
                     'config    BROKEN - x'):
            self.assertIn(kept, out)
        self.assertNotIn('worktrees 1', out)
        self.assertNotIn('            /r  abc [main]', out)


class TheDefaultIsShort(Fixture):
    def test_default_omits_what_full_prints_and_exit_code_is_the_same(self):
        self.short = True
        code, short = self.run_preflight()
        self.short = False
        code_full, full = self.run_preflight('--full')
        self.assertEqual(code, code_full)
        self.assertFalse(any(l.startswith('worktrees') for l in short.splitlines()))
        self.assertTrue(any(l.startswith('worktrees') for l in full.splitlines()))
        self.assertIn('kit       ', short)
        self.assertLess(len(short), len(full))
