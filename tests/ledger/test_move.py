"""`ledger.py split-move` (docs/ledger.md "A new ledger"): kitlib.ledger.move moves STANDING.md
and the `rules` folder from beside the frozen ledger_path into ledger_dir, each whole, refusing a
destination that exists and resuming an interrupted move; the CLI's lines and exit codes; and the
guard lets the PM and a subagent run it and stage the move."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.guard import context
from kitlib.ledger import files, move, rulebook, safepath, standing, write
from kitlib.ledger.errors import Refused
from tests.gitfixture import TempRepo
from tests.guard import world as W
from tests.guard.claudehost import decide
from tests.guard.world import bash
from tests.home import machine_config

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')
BOOK = '# Alpha rules\n\n## Topic\n\n- topic.one: A rule.\n'
DIGEST = '# Standing rulings\n\n## all\n\n- A standing line.\n'


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def text_of(path):
    with open(path, encoding='utf-8', newline='') as f:
        return f.read()


class Layout(object):
    """A PM folder: `rulings/LEDGER.md` (the frozen file) with STANDING.md and rules/ beside it,
    and `ledger_dir` the new `rulings/split` folder, under `root`."""

    def __init__(self, root):
        self.root = root
        self.old = os.path.join(root, 'rulings')
        self.frozen = os.path.join(self.old, 'LEDGER.md')
        self.new = os.path.join(self.old, 'split')
        self.old_rules = os.path.join(self.old, 'rules')
        self.old_digest = os.path.join(self.old, 'STANDING.md')
        self.new_rules = os.path.join(self.new, 'rules')
        self.new_digest = os.path.join(self.new, 'STANDING.md')
        put(self.frozen, '# Ledger\n')
        put(self.old_digest, DIGEST)
        put(os.path.join(self.old_rules, 'alpha.md'), BOOK)
        put(os.path.join(self.old_rules, 'notes.txt'), 'not a book\n')
        self.cfg = {'ledger_path': self.frozen, 'ledger_dir': self.new, 'ledger_repos': ['alpha', 'pm']}


class MoveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.L = Layout(os.path.realpath(tmp.name))

    def states(self, got):
        return [(i.name, i.state) for i in got.items]

    def test_a_dry_run_lists_both_items_and_moves_nothing(self):
        got = move.plan(self.L.cfg)
        self.assertEqual(self.states(got), [('rules', 'move'), ('STANDING.md', 'move')])
        self.assertEqual(got.items[0].count, 2)
        self.assertEqual(got.items[0].src, self.L.old_rules)
        self.assertEqual(got.items[0].dst, self.L.new_rules)
        self.assertTrue(os.path.isfile(self.L.old_digest))
        self.assertFalse(os.path.exists(self.L.new))

    def test_apply_moves_both_unchanged_and_the_split_config_reads_them(self):
        got, moved = move.apply(self.L.cfg)
        self.assertEqual([i.name for i in moved], ['rules', 'STANDING.md'])
        self.assertFalse(os.path.exists(self.L.old_rules))
        self.assertFalse(os.path.exists(self.L.old_digest))
        self.assertEqual(text_of(os.path.join(self.L.new_rules, 'alpha.md')), BOOK)
        self.assertEqual(text_of(os.path.join(self.L.new_rules, 'notes.txt')), 'not a book\n')
        found = files.of_config(self.L.cfg)
        self.assertEqual(list(rulebook.paths(found.rules)), ['alpha'])
        self.assertEqual(standing.read_digest(found.digest)[0], DIGEST)
        self.assertTrue(os.path.isfile(self.L.frozen))           # the frozen file stays

    def test_a_destination_that_exists_is_refused_and_nothing_moves(self):
        put(self.L.new_digest, 'another digest\n')
        for call in (move.plan, move.apply):
            with self.assertRaises(Refused) as cm:
                call(self.L.cfg)
            self.assertIn('%s exists, and %s is still there' % (self.L.new_digest, self.L.old_digest),
                          str(cm.exception))
            self.assertIn('nothing was moved', str(cm.exception))
        self.assertTrue(os.path.isdir(self.L.old_rules))         # the other item did not move either
        self.assertFalse(os.path.exists(self.L.new_rules))
        self.assertEqual(text_of(self.L.new_digest), 'another digest\n')

    def test_an_interrupted_move_resumes(self):
        os.makedirs(self.L.new)
        os.rename(self.L.old_rules, self.L.new_rules)
        self.assertEqual(self.states(move.plan(self.L.cfg)), [('rules', 'done'), ('STANDING.md', 'move')])
        got, moved = move.apply(self.L.cfg)
        self.assertEqual([i.name for i in moved], ['STANDING.md'])
        self.assertEqual(text_of(self.L.new_digest), DIGEST)

    def test_a_rerun_after_the_move_is_done_and_changes_nothing(self):
        move.apply(self.L.cfg)
        got, moved = move.apply(self.L.cfg)
        self.assertEqual(moved, [])
        self.assertEqual(self.states(got), [('rules', 'done'), ('STANDING.md', 'done')])

    def test_nothing_to_move_is_none(self):
        os.remove(self.L.old_digest)
        for name in os.listdir(self.L.old_rules):
            os.remove(os.path.join(self.L.old_rules, name))
        os.rmdir(self.L.old_rules)
        got, moved = move.apply(self.L.cfg)
        self.assertEqual(self.states(got), [('rules', 'none'), ('STANDING.md', 'none')])
        self.assertFalse(os.path.exists(self.L.new))

    def test_it_needs_both_ledger_dir_and_ledger_path(self):
        for drop, word in (('ledger_dir', 'nowhere to move to'), ('ledger_path', 'no folder to move from')):
            cfg = dict(self.L.cfg)
            del cfg[drop]
            with self.assertRaises(Refused) as cm:
                move.plan(cfg)
            self.assertIn(word, str(cm.exception))

    def test_ledger_dir_the_frozen_files_folder_has_nothing_to_move(self):
        cfg = dict(self.L.cfg, ledger_dir=self.L.old.upper())
        got, moved = move.apply(cfg)
        self.assertTrue(got.same)
        self.assertEqual((got.items, moved), ([], []))
        self.assertTrue(os.path.isfile(self.L.old_digest))

    def test_a_path_check_refusal_moves_nothing(self):
        book = os.path.normcase(os.path.join(self.L.old_rules, 'notes.txt'))
        real = safepath.owner_sid

        def owner(p):
            return ('S-1-5-21-1-2-3-9999', None) if os.path.normcase(p) == book else real(p)
        with mock.patch.object(safepath, 'owner_sid', owner):
            with self.assertRaises(Refused) as cm:
                move.apply(self.L.cfg)
        self.assertIn('is owned by S-1-5-21-1-2-3-9999', str(cm.exception))
        self.assertTrue(os.path.isdir(self.L.old_rules))
        self.assertTrue(os.path.isfile(self.L.old_digest))

    def test_a_source_of_the_wrong_kind_is_refused(self):
        os.remove(self.L.old_digest)
        os.makedirs(self.L.old_digest)
        with self.assertRaises(Refused) as cm:
            move.plan(self.L.cfg)
        self.assertIn('is a folder, not a file', str(cm.exception))

    def test_a_rename_that_fails_part_way_says_what_moved_and_a_rerun_finishes(self):
        def rename(src, dst):
            if src == self.L.old_digest:
                raise PermissionError(13, 'in use', src)
            os.rename(src, dst)
        with self.assertRaises(Refused) as cm:
            move.apply(self.L.cfg, rename=rename)
        msg = str(cm.exception)
        self.assertIn('cannot move %s -> %s' % (self.L.old_digest, self.L.new_digest), msg)
        self.assertIn('moved %s -> %s' % (self.L.old_rules, self.L.new_rules), msg)
        self.assertIn('it resumes', msg)
        got, moved = move.apply(self.L.cfg)
        self.assertEqual([i.name for i in moved], ['STANDING.md'])

    def test_a_cross_volume_rename_says_move_it_by_hand(self):
        def rename(src, dst):
            e = OSError(18, 'not the same device')
            e.winerror = 17
            raise e
        with self.assertRaises(Refused) as cm:
            move.apply(self.L.cfg, rename=rename)
        self.assertIn('another volume', str(cm.exception))
        self.assertIn('the owner moves it by hand', str(cm.exception))
        self.assertIn('nothing was moved', str(cm.exception))

    def test_a_ledger_dir_inside_the_old_rules_folder_is_refused_before_anything_is_made(self):
        for inside in (self.L.old_rules, os.path.join(self.L.old_rules, 'split').upper()):
            cfg = dict(self.L.cfg, ledger_dir=inside)
            for call in (move.plan, move.apply):
                with self.assertRaises(Refused) as cm:
                    call(cfg)
                self.assertIn('lies inside', str(cm.exception))
                self.assertIn('nothing was moved', str(cm.exception))
        self.assertEqual(sorted(os.listdir(self.L.old_rules)), ['alpha.md', 'notes.txt'])
        self.assertTrue(os.path.isfile(self.L.old_digest))

    def test_outside_git_no_lock_file_is_made(self):
        self.assertEqual(move.lock_paths(move.plan(self.L.cfg)), [])
        move.apply(self.L.cfg)
        self.assertEqual(sorted(os.listdir(self.L.new_rules)), ['alpha.md', 'notes.txt'])
        self.assertEqual(sorted(os.listdir(self.L.new)), ['STANDING.md', 'rules'])


class GitMoveTest(unittest.TestCase):
    """In a git checkout: the write locks, and the git step's paths."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.L = Layout(self.repo.path('pm'))

    def test_the_locks_are_every_book_and_digest_path_old_and_new_sorted(self):
        got = move.lock_paths(move.plan(self.L.cfg))
        want = [os.path.join(self.L.old_rules, 'alpha.md'), self.L.old_digest,
                os.path.join(self.L.new_rules, 'alpha.md'), self.L.new_digest]
        self.assertEqual(sorted(map(os.path.normcase, got)), sorted(map(os.path.normcase, want)))
        self.assertEqual(got, sorted(got, key=os.path.normcase))

    def test_a_rule_writer_holding_a_book_lock_makes_the_move_wait_then_refuse(self):
        book = os.path.join(self.L.new_rules, 'alpha.md')
        taken, done = threading.Event(), threading.Event()

        def hold():
            with write.locked(book):
                taken.set()
                done.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(done.set)
        self.assertTrue(taken.wait(30))
        with self.assertRaises(Refused) as cm:
            move.apply(self.L.cfg, wait=0.2)
        self.assertIn('being written by another process', str(cm.exception))
        self.assertTrue(os.path.isdir(self.L.old_rules))
        self.assertFalse(os.path.exists(self.L.new))
        done.set()
        t.join()
        move.apply(self.L.cfg)
        self.assertTrue(os.path.isfile(book))

    def steps(self, got):
        steps = move.git_steps(got)
        self.assertEqual(len(steps), 1)
        top, paths = steps[0]
        self.assertTrue(files._same(top, self.repo.root))
        return paths

    def test_the_git_step_names_the_old_and_new_paths(self):
        self.repo.commit('the old layout', 'pm')
        got, _ = move.apply(self.L.cfg)
        self.assertEqual(self.steps(got), ['pm/rulings/rules', 'pm/rulings/split/rules',
                                           'pm/rulings/STANDING.md', 'pm/rulings/split/STANDING.md'])

    def test_the_git_step_leaves_out_old_paths_never_committed(self):
        got, _ = move.apply(self.L.cfg)
        self.assertEqual(self.steps(got), ['pm/rulings/split/rules', 'pm/rulings/split/STANDING.md'])

    def test_the_git_step_leaves_out_an_old_path_whose_removal_is_committed(self):
        self.repo.commit('the old layout', 'pm')
        os.makedirs(self.L.new)
        os.rename(self.L.old_rules, self.L.new_rules)
        self.repo.git('add', '-A', '--', 'pm/rulings/rules', 'pm/rulings/split/rules')
        self.repo.git('commit', '-q', '-m', 'rules moved')
        got, _ = move.apply(self.L.cfg)
        self.assertEqual(self.steps(got), ['pm/rulings/split/rules', 'pm/rulings/STANDING.md',
                                           'pm/rulings/split/STANDING.md'])


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.L = Layout(os.path.join(self.home, 'pm'))
        machine_config(self.home, self.L.cfg, case=self)

    def run_tool(self, *args):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home)
        return subprocess.run([sys.executable, TOOL, 'split-move'] + list(args), capture_output=True,
                              text=True, env=env, timeout=60)

    def test_dry_run_then_apply_then_rerun(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            'move    %s -> %s (2 files)' % (self.L.old_rules, self.L.new_rules),
            'move    %s -> %s' % (self.L.old_digest, self.L.new_digest),
            'dry run: 2 to move; add --apply to move them'])
        self.assertTrue(os.path.isfile(self.L.old_digest))
        r = self.run_tool('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[:2], ['moved   %s -> %s' % (self.L.old_rules, self.L.new_rules),
                                     'moved   %s -> %s' % (self.L.old_digest, self.L.new_digest)])
        self.assertTrue(lines[-1].startswith('then run python "') and lines[-1].endswith('ledger.py" check'),
                        lines[-1])
        self.assertTrue(os.path.isfile(self.L.new_digest))
        r = self.run_tool('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            'done    %s (already in ledger_dir)' % self.L.new_rules,
            'done    %s (already in ledger_dir)' % self.L.new_digest,
            'nothing to move'])

    def test_a_clash_exits_1_and_an_abbreviation_is_a_usage_error(self):
        put(self.L.new_digest, 'x\n')
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn('ledger.py: refused: ', r.stderr)
        self.assertIn('nothing was moved', r.stderr)
        self.assertEqual(self.run_tool('--appl').returncode, 2)
        self.assertTrue(os.path.isdir(self.L.old_rules))


class CliGitTest(unittest.TestCase):
    """The layout in a git checkout: the printed git step, and a rerun that still prints it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.L = Layout(self.repo.path('pm'))
        self.repo.commit('the old layout', 'pm')
        machine_config(self.home, self.L.cfg, case=self)

    run_tool = CliTest.run_tool

    def step(self, *paths):
        return 'next: ' + subprocess.list2cmdline(['git', '-C', self.repo.root, 'add', '-A', '--'] + list(paths))

    def test_a_rerun_after_a_lost_apply_still_prints_the_git_step(self):
        everything = ['pm/rulings/rules', 'pm/rulings/split/rules', 'pm/rulings/STANDING.md',
                      'pm/rulings/split/STANDING.md']
        r = self.run_tool('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[2], self.step(*everything))
        for args in (('--apply',), ()):               # its output lost: the rerun says it again
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = r.stdout.splitlines()
            self.assertEqual(lines[:4], [
                'done    %s (already in ledger_dir)' % self.L.new_rules,
                'done    %s (already in ledger_dir)' % self.L.new_digest,
                'nothing to move', self.step(*everything)])
            self.assertTrue(lines[4].startswith('then commit those paths, then run python "'), lines)
        self.repo.git('add', '-A', '--', *everything)
        self.repo.git('commit', '-q', '-m', 'the move')
        r = self.run_tool()
        self.assertEqual(r.stdout.splitlines()[3],
                         self.step('pm/rulings/split/rules', 'pm/rulings/split/STANDING.md'))


class GuardTest(unittest.TestCase):
    """The PM and a subagent may run the verb and stage its move; a hand move is refused."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.folder = os.path.dirname(self.w.ledger)
        self.split = os.path.join(self.folder, 'split')
        os.makedirs(os.path.join(self.folder, 'rules'))
        self.w.user_config({'ledger_path': self.w.ledger, 'ledger_dir': self.split,
                            'ledger_repos': ['kit', 'pm']})
        self.verb = 'python "%s/tools/ledger.py" split-move --apply' % W.fwd(self.w.kit_dir)
        self.stage = ('git -C "%s" add -A -- rulings/rules rulings/split/rules rulings/STANDING.md '
                      'rulings/split/STANDING.md' % W.fwd(os.path.dirname(self.folder)))

    def decision(self, text, sub, cwd=None):
        return decide(bash(text, cwd or self.folder, sub=sub), self.w.ctx())

    def test_the_verb_passes_for_a_subagent_and_the_pm_and_the_pm_may_stage_it(self):
        d = self.decision(self.verb, True)
        self.assertIsNone(d, d and d.reason)
        self.w.environ = {context.ROLE_ENV: 'pm'}
        pm = os.path.dirname(self.folder)         # the PM's session runs in the PM repository
        for text in (self.verb, self.stage):
            d = self.decision(text, False, pm)
            self.assertIsNone(d, '%s: %s' % (text, d and d.reason))

    def test_a_hand_move_into_ledger_dir_is_refused(self):
        d = self.decision('mv rules split/rules', True)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)


if __name__ == '__main__':
    unittest.main()
