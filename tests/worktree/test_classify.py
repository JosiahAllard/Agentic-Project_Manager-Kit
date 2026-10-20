"""kitlib.worktree.classify (`worktree_remove.py --classify [--json]`): each class, each liveness
probe, the first-match order, the output and exit codes, and that a run changes nothing on disk.
Throwaway repositories only (tests/gitfixture.py); the machine lock and the process scan are seams,
except one Windows test of the real scan on a temp tree.
"""
import json
import os
import subprocess
import sys
import time
import unittest

from kitlib.records import gate_ok
from kitlib.worktree import classify
from tests import gitfixture
from tests.worktree.test_worktree_remove import TOOL, WINDOWS, RemoveCase

LATER = 3 * 24 * 3600


def free():
    return [], None


def no_procs():
    return ([], 0, 1), None


class ClassifyCase(RemoveCase):

    def run_classify(self, now=None, machine=free, procs=no_procs):
        got = classify.classify(self.repo, now=time.time() + LATER if now is None else now,
                                machine=machine, procs=procs)
        return got.code, '\n'.join(got.lines), got.data

    def row(self, data, path):
        want = os.path.normcase(os.path.normpath(path))
        found = [t for t in data['trees'] if os.path.normcase(t['path']) == want]
        self.assertEqual(len(found), 1, data['trees'])
        return found[0]

    def green(self, path):
        """A gate-OK record for the tree's tip, as a passing --full gate writes it."""
        head = self.git('rev-parse', 'HEAD', cwd=path)
        gate_ok.record_pass(self.repo, head, {}, {'version': 'test'}, 'r', gate_ok.utc(), [],
                            full=True)
        return head

    def outside(self, name, commit):
        root = os.path.join(os.path.realpath(self.fx.base), 'outside')
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, name)
        self.git('worktree', 'add', '-q', '--detach', path, commit)
        return path

    def snapshot(self):
        files = []
        for top in [self.repo] + [os.path.join(os.path.realpath(self.fx.base), 'outside')]:
            for where, dirs, names in os.walk(top):
                for name in names:
                    full = os.path.join(where, name)
                    files.append((full, os.path.getsize(full), os.stat(full).st_mtime_ns))
        return (self.git('worktree', 'list', '--porcelain'),
                self.git('for-each-ref', '--format=%(refname) %(objectname)'),
                sorted(files))


class Classes(ClassifyCase):

    def test_a_landed_clean_tree_is_a(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.row(data, path)['class'], 'A')
        self.assertIn('A landed       1  tip in the base, clean', out)
        self.assertIn('summary   A 1, B 0, C 0, E 0, L 0', out)

    def test_a_finished_tree_with_a_gate_ok_record_is_b(self):
        path = self.workstream('ready', commits=2)
        head = self.green(path)
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 1, out)
        row = self.row(data, path)
        self.assertEqual((row['class'], row['ahead'], row['gate']), ('B', 2, 'green'), row)
        self.assertIn('tip %s' % head[:10], out)
        self.assertIn('next      ', out)
        self.assertIn(' ready --abandon "<why>"   (B,', out)
        self.assertEqual(out.count(' -C '), 1, out)

    def test_no_gate_ok_record_is_c(self):
        path = self.workstream('open')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['gate']), ('C', 'none'), row)
        self.assertIn('gate: none', out)

    def test_a_dirty_tree_is_c_even_with_a_record(self):
        path = self.workstream('wip')
        self.green(path)
        self.fx.write('loose.txt', 'not committed\n', root=path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['dirty']), ('C', 1), row)
        self.assertIn('1 dirty file(s)', out)

    def test_a_landed_dirty_tree_is_c(self):
        path = self.workstream('after')
        self.land('kit/after')
        self.fx.write('loose.txt', 'x\n', root=path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note']), ('C', 'landed, not clean'), row)

    def test_a_fresh_tree_with_nothing_of_its_own_is_c(self):
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note']), ('C', 'nothing of its own'), row)

    def test_an_evidence_name_is_e(self):
        path = self.workstream('agent-1234')
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'E')

    def test_a_detached_tree_outside_the_worktree_folder_is_e(self):
        path = self.outside('scratch', self.git('rev-parse', 'HEAD'))
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['branch']), ('E', None), row)
        self.assertIn('detached', out)

    def test_a_landed_detached_tree_outside_is_a_first(self):
        work = self.workstream('side')
        tip = self.git('rev-parse', 'HEAD', cwd=work)
        self.land('kit/side')
        path = self.outside('red-copy', tip)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'A')


class Live(ClassifyCase):

    def test_recent_activity_is_l_and_exit_zero(self):
        path = self.workstream('busy')
        rc, out, data = self.run_classify(now=time.time())
        self.assertEqual(rc, 0, out)
        row = self.row(data, path)
        self.assertEqual(row['class'], 'L')
        self.assertIn('under the 24 h dead bar', row['reason'])
        self.assertIn('summary   A 0, B 0, C 0, E 0, L 1', out)
        self.assertNotIn('next      ', out)

    def test_a_machine_lock_record_naming_it_is_l(self):
        path = self.workstream('gpu')
        self.land('kit/gpu')

        def machine():
            return [{'repo': 'r', 'slug': 'x', 'id': 7,
                     'note': 'training in .claude/worktrees/gpu'}], None
        rc, out, data = self.run_classify(machine=machine)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.row(data, path)['reason'].startswith('machine lock: r/x run 7'))

    def test_the_landing_lock_naming_its_branch_is_l(self):
        path = self.workstream('landing')
        other = self.workstream('other')
        with open(os.path.join(self.fx.common_dir(), 'kit-landing.lock'), 'w') as f:
            json.dump({'lane': 'full', 'branches': ['kit/landing'], 'token': 't'}, f)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['reason'], 'the landing lock names kit/landing')
        self.assertEqual(self.row(data, other)['class'], 'C')

    def test_an_unreadable_landing_lock_makes_every_tree_l(self):
        path = self.workstream('done')
        self.land('kit/done')
        with open(os.path.join(self.fx.common_dir(), 'kit-landing.lock'), 'w') as f:
            f.write('not a record')
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 0, out)
        self.assertIn('note      the landing lock cannot be read', out)
        self.assertTrue(self.row(data, path)['reason'].startswith('unreadable: the landing lock'))

    def test_an_unreadable_machine_lock_makes_every_tree_l(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out, data = self.run_classify(machine=lambda: (None, 'tool missing'))
        self.assertEqual(rc, 0, out)
        self.assertIn('the machine lock cannot be read (tool missing): every tree is L', out)
        self.assertEqual(self.row(data, path)['class'], 'L')

    def test_a_failed_process_scan_makes_every_tree_l(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out, data = self.run_classify(procs=lambda: (None, 'no list'))
        self.assertEqual(rc, 0, out)
        self.assertIn('the process scan failed (no list)', out)
        self.assertEqual(self.row(data, path)['class'], 'L')
        self.assertNotIn('processes', data)

    def test_a_process_with_its_cwd_inside_is_l(self):
        path = self.workstream('held')
        self.land('kit/held')
        inner = os.path.join(path, 'sub')
        os.makedirs(inner)
        rc, out, data = self.run_classify(
            procs=lambda: (([(4242, 'python.exe', inner)], 3, 10), None))
        self.assertEqual(rc, 0, out)
        self.assertIn('process pid 4242 python.exe', self.row(data, path)['reason'])
        self.assertIn('processes 10 scanned, 3 unreadable', out)
        self.assertEqual(data['processes'], {'scanned': 10, 'unreadable': 3})

    def test_a_locked_tree_is_l(self):
        path = self.workstream('kept')
        self.land('kit/kept')
        self.git('worktree', 'lock', '--reason', 'owner keeps it', path)
        rc, out, data = self.run_classify()
        self.assertIn('locked with `git worktree lock`: owner keeps it',
                      self.row(data, path)['reason'])

    def test_an_exempt_tree_is_l(self):
        self.configure({'worktree_remove': {'exempt': {'daily': 'kept by ruling X'}}})
        path = self.workstream('daily')
        self.land('kit/daily')
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['reason'], 'exempt (kept by ruling X)')

    def test_the_landing_tree_is_l(self):
        path = self.workstream('_landing')
        rc, out, data = self.run_classify()
        self.assertIn('the landing tree', self.row(data, path)['reason'])

    def test_l_comes_before_every_other_class(self):
        # Landed and clean (A) and an evidence name (E), but held: L wins.
        path = self.workstream('agent-9')
        self.land('kit/agent-9')
        rc, out, data = self.run_classify(
            procs=lambda: (([(1, 'x.exe', path)], 0, 1), None))
        self.assertEqual(self.row(data, path)['class'], 'L')

    @unittest.skipUnless(WINDOWS, 'the process scan is Windows only')
    def test_the_real_process_scan_finds_this_process_in_a_tree(self):
        path = self.workstream('here')
        self.land('kit/here')
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(path)
        rc, out, data = self.run_classify(procs=classify.process_cwds)
        self.assertIn('process pid %d ' % os.getpid(), self.row(data, path)['reason'])


class ReadOnly(ClassifyCase):

    def test_a_run_changes_nothing_on_disk(self):
        self.workstream('done')
        self.land('kit/done')
        ready = self.workstream('ready')
        self.green(ready)
        dirty = self.workstream('wip')
        self.fx.write('loose.txt', 'x\n', root=dirty)
        self.workstream('agent-1')
        self.outside('scratch', self.git('rev-parse', 'HEAD'))
        before = self.snapshot()
        rc, out, data = self.run_classify()
        self.assertEqual(data['counts'], {'A': 1, 'B': 1, 'C': 1, 'E': 2, 'L': 0}, out)
        self.assertEqual(self.snapshot(), before)


class Tool(ClassifyCase):

    def cli(self, *args):
        done = subprocess.run([sys.executable, TOOL] + list(args), cwd=self.repo,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env(), timeout=300)
        return done.returncode, done.stdout or '', done.stderr or ''

    def test_classify_prints_the_classes(self):
        self.workstream('open')
        rc, out, err = self.cli('-C', self.repo, '--classify')
        self.assertIn('classify  1 worktree(s) besides the main checkout', out + err)
        self.assertIn('summary   ', out)
        self.assertIn(rc, (0, 1), out + err)

    def test_json_prints_the_data(self):
        path = self.workstream('open')
        rc, out, err = self.cli('--classify', '--json')
        data = json.loads(out)
        self.assertEqual(sorted(data['counts']), list(classify.CLASSES))
        self.assertEqual([os.path.normcase(t['path']) for t in data['trees']],
                         [os.path.normcase(os.path.normpath(path))])

    def test_apply_is_refused(self):
        rc, out, err = self.cli('--classify', '--apply')
        self.assertEqual(rc, 2, out + err)
        self.assertIn('it is read-only and has no --apply', out + err)

    def test_json_alone_is_refused(self):
        rc, out, err = self.cli('--json')
        self.assertEqual(rc, 2, out + err)


if __name__ == '__main__':
    unittest.main()
