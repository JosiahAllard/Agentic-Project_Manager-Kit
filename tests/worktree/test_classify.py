"""kitlib.worktree.classify (`worktree_remove.py --classify [--json]`): each class, each liveness
probe, the first-match order, the output and exit codes, and that a run changes nothing on disk.
Throwaway repositories only (tests/gitfixture.py); the machine lock and the process scan are seams,
except one Windows test of the real scan on a temp tree.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

import kitlib.platform
from kitlib import kitroot, pmdata
from kitlib.records import gate_ok
from kitlib.worktree import classify, holders, marks, remove, sweep
from tests import gitfixture
from tests.home import result_drive
from tests.realgit import real_git
from tests.worktree.test_worktree_remove import TOOL, WINDOWS, RemoveCase
from tests.platforms import windows_only

LATER = 3 * 24 * 3600
NO_STORE = ('no backlog store is named: this repository\'s .kit.json names no "backlog".store'
            ' (the implicit default is read-only, so no pull is written there) and no PM data'
            ' worktree names one')


def free():
    return [], None


def no_procs():
    return ([], 0, 1), None


class ClassifyCase(RemoveCase):

    def setUp(self):
        super().setUp()
        result_drive(self)          # the gate-OK records (kitlib.records.gate_ok) go under it

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

    def record(self, text, name='T-001.md'):
        """A record committed to the configured store on main and pushed (the base)."""
        full = os.path.join(self.repo, 'docs', 'backlog', name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(text if isinstance(text, bytes) else text.encode('utf-8'))
        self.fx.commit('record', 'docs/backlog/' + name)
        self.fx.git('push', '-q', 'origin', 'main')

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
        nexts = [line for line in out.splitlines() if line.startswith('next      ')]
        self.assertEqual(len(nexts), 2, out)       # --abandon, then --mark-stale
        self.assertEqual([line.count(' -C ') for line in nexts], [1, 1], out)
        self.assertEqual(out.count(' -C '), 2, out)

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

    def test_a_fresh_tree_is_c_when_no_backlog_store_is_named(self):
        """No "backlog".store and no PM data worktree: "no claim" proves nothing, so it stays C."""
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertEqual(row['note'], 'nothing of its own; kept: whether a backlog record claims'
                         ' it is unknown: ' + NO_STORE, row)
        self.assertEqual(row['claims'], 'unknown: ' + NO_STORE)

    def test_an_evidence_name_is_e(self):
        self.configure({'backlog': {'store': 'main'}})
        path = self.workstream('agent-1234')
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'E')

    def test_a_detached_tree_outside_the_worktree_folder_is_e(self):
        path = self.outside('scratch', self.git('rev-parse', 'HEAD'))
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['branch']), ('E', None), row)
        self.assertIn('detached', out)

    def test_a_record_from_a_gate_without_full_is_c_not_full(self):
        path = self.workstream('half')
        head = self.git('rev-parse', 'HEAD', cwd=path)
        gate_ok.record_pass(self.repo, head, {}, {'version': 'test'}, 'r', gate_ok.utc(), [],
                            full=False)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['gate']),
                         ('C', 'not full: the record for %s is from a gate run without --full'
                          % head), row)

    def test_a_scoped_record_is_green_when_the_base_sets_gate_once(self):
        """"landing".gate_once (docs/gate.md "Gate once at landing"): a landing takes a
        default-mode record, so the tree is as finished as with a --full one."""
        self.configure({'landing': {'gate_once': True}})
        path = self.workstream('scoped')
        head = self.git('rev-parse', 'HEAD', cwd=path)
        gate_ok.record_pass(self.repo, head, {}, {'version': 'test'}, 'r', gate_ok.utc(), [],
                            full=False, mode='select')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['gate']), ('B', 'green'), row)

    def test_a_docs_record_is_not_green_under_gate_once(self):
        self.configure({'landing': {'gate_once': True}})
        path = self.workstream('docs')
        head = self.git('rev-parse', 'HEAD', cwd=path)
        gate_ok.record_pass(self.repo, head, {}, {'version': 'test'}, 'r', gate_ok.utc(), [],
                            full=False, mode='docs')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertTrue(row['gate'].startswith('not full: '), row)

    def test_the_backlog_store_is_opened_once_for_every_tree(self):
        self.configure({'backlog': {'store': 'main'}})
        for slug in ('one', 'two', 'three'):
            self.workstream(slug)
        with mock.patch.object(remove.backlog_store, 'open_backend',
                               wraps=remove.backlog_store.open_backend) as opened:
            rc, out, data = self.run_classify()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(data['trees']), 3)

    def test_each_path_is_made_canonical_once_per_run(self):
        for slug in ('one', 'two', 'three'):
            self.workstream(slug)
        elsewhere = os.path.realpath(self.fx.base)
        procs = [(n, 'p.exe', os.path.join(elsewhere, 'p%d' % n)) for n in range(5)]
        calls = []

        def counted(path):
            calls.append(path)
            return holders.canon(path)
        with mock.patch.object(classify, 'canon', counted):
            self.run_classify(procs=lambda: ((procs, 0, 5), None))
        self.assertLessEqual(len(calls), len(procs) + 3 + 2, calls)


class EvidenceKept(ClassifyCase):
    """An evidence name alone proves nothing about the work in a tree: it is E only when the tree
    is clean and, on a branch, every live backlog store can be read and none claims the branch."""

    CONFIG = {'backlog': {'store': 'main'}}

    def test_a_claimed_evidence_named_tree_is_c(self):
        self.record('id: T-001\nstate: pulled\npulled_by: kit/agent-1234\n')
        path = self.workstream('agent-1234')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['claims']), ('C', ['T-001']), row)
        self.assertIn('an evidence name, kept from E: claimed by T-001', row['note'])
        self.assertIn('next      release T-001 or block it (C)', out)

    def test_a_dirty_evidence_named_tree_is_c(self):
        path = self.workstream('agent-5')
        self.fx.write('loose.txt', 'not committed\n', root=path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['dirty']), ('C', 1), row)
        self.assertIn('an evidence name, kept from E: 1 dirty file(s)', row['note'])

    def test_a_dirty_detached_evidence_named_tree_is_c(self):
        path = self.workstream('mutant1')
        self.git('checkout', '-q', '--detach', cwd=path)
        self.fx.write('loose.txt', 'not committed\n', root=path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['branch']), ('C', None), row)

    def test_an_evidence_named_tree_whose_claims_cannot_be_read_is_c(self):
        self.configure({})
        path = self.workstream('agent-6')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn('an evidence name, kept from E: whether a backlog record claims it is'
                      ' unknown: ' + NO_STORE, row['note'])

    def test_a_claim_of_another_branch_leaves_it_e(self):
        self.record('id: T-001\nstate: pulled\npulled_by: kit/other\n')
        path = self.workstream('agent-7')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['claims']), ('E', []), row)

    def test_a_clean_detached_evidence_named_tree_is_e_whatever_the_claims(self):
        self.configure({})
        path = self.workstream('redcheck')
        self.git('checkout', '-q', '--detach', cwd=path)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'E')


@real_git('git reflog: the fake writes no reflog, and A reads it')
@gitfixture.real_fixture('A reads the reflog entry a fixture commit() writes, which only real '
                         'git writes')
class Landed(ClassifyCase):
    """A needs a commit of the branch's own in its reflog (classify.own_commit)."""

    def test_a_landed_clean_tree_is_a(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.row(data, path)['class'], 'A')
        self.assertIn('A landed       1  tip in the base, clean, a commit of its own or empty and'
                      ' unclaimed', out)
        self.assertIn('summary   A 1, B 0, C 0, E 0, L 0', out)

    def test_a_landed_detached_tree_outside_is_a_first(self):
        work = self.workstream('side')
        tip = self.git('rev-parse', 'HEAD', cwd=work)
        self.land('kit/side')
        path = self.outside('red-copy', tip)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'A')

    def fresh_at_landed(self):
        work = self.workstream('side')
        tip = self.git('rev-parse', 'HEAD', cwd=work)
        self.land('kit/side')
        self.git('worktree', 'remove', '--force', work)
        path = os.path.join(self.home, 'fresh')
        self.git('worktree', 'add', '-q', path, '-b', 'kit/fresh', tip)
        return path

    def test_a_fresh_branch_at_a_landed_commit_is_c_when_no_store_is_named(self):
        path = self.fresh_at_landed()
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn('nothing of its own; kept: whether a backlog record claims it is unknown',
                      row['note'])

    def test_a_fresh_branch_at_a_landed_commit_and_no_claim_is_a(self):
        self.configure({'backlog': {'store': 'main'}})
        path = self.fresh_at_landed()
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note'], row['claims']),
                         ('A', 'nothing of its own', []), row)

    def test_a_landed_tree_whose_reflog_is_gone_is_c(self):
        path = self.workstream('done')
        self.land('kit/done')
        os.remove(os.path.join(self.fx.common_dir(), 'logs', 'refs', 'heads', 'kit', 'done'))
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note']),
                         ('C', 'nothing of its own shown: its reflog is missing or expired'), row)


@real_git('git reflog: the fake writes no reflog, and an empty tree is A only when it is read')
@gitfixture.real_fixture('an empty tree is A only when its branch reflog, which only real git '
                         'writes, shows nothing of its own')
class EmptyTrees(ClassifyCase):
    """A clean tree with its tip in the base and nothing of its own is A only when every live
    backlog store can be read and no record claims its branch, and its reflogs lose nothing."""

    CONFIG = {'backlog': {'store': 'main'}}

    def pm_worktree(self, store):
        """The PM data worktree (kitlib.pmdata), outside the worktree folder, its `.kit.json`
        naming `store` uncommitted (the tree is dirty, so C itself)."""
        pmdata.write(self.fx.common_dir(), 'pmdata', 'private', self.git('rev-parse', 'HEAD'))
        path = os.path.join(os.path.realpath(self.fx.base), 'pm')
        self.git('worktree', 'add', '-q', '-b', 'pmdata', path, 'HEAD')
        self.fx.write('.kit.json', json.dumps({'backlog': {'store': store}}) + '\n', root=path)
        return path

    def test_an_empty_unclaimed_tree_is_a(self):
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note'], row['claims']),
                         ('A', 'nothing of its own', []), row)
        self.assertNotIn('next ', out)

    def test_a_claim_in_the_configured_store_keeps_it_c(self):
        self.record('id: T-001\nstate: pulled\npulled_by: kit/fresh\n')
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note'], row['claims']),
                         ('C', 'nothing of its own; kept: claimed by T-001', ['T-001']), row)
        # A main store takes no write in the main checkout (MainBackend.publish): no -C line.
        self.assertNotIn('%s release' % kitroot.command('backlog', self.repo), out)
        self.assertIn('next      release T-001 or block it (C): its store is in the main checkout,'
                      ' which takes no backlog write; run `backlog.py release T-001 --reason'
                      ' "<why>"` (or `block T-001 --on <M> --reason "<why>"` to keep the branch)'
                      ' from a worktree whose branch holds the record', out)
        self.assertLess(out.index('release T-001'), out.index('fresh --abandon'))

    def test_a_claim_is_matched_without_regard_to_case(self):
        self.record('id: T-001\nstate: pulled\npulled_by: Kit/Fresh\n')
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['claims']), ('C', ['T-001']), row)
        self.assertIn('kept: claimed by T-001', row['note'])

    def test_both_stores_named_a_claim_in_the_pm_store_alone_keeps_it_c(self):
        self.record('id: T-001\nstate: pulled\npulled_by: kit/other\n')
        pm = self.pm_worktree('main')
        self.fx.write('docs/backlog/T-002.md', 'id: T-002\nstate: pulled\npulled_by: kit/fresh\n',
                      root=pm)
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['claims']), ('C', ['T-002']), row)
        self.assertIn('%s release T-002' % kitroot.command('backlog', pm), out)
        self.assertNotIn('T-001', out)

    def test_both_stores_named_a_claim_in_the_main_store_alone_keeps_it_c(self):
        pm = self.pm_worktree('main')             # made first: its store never holds T-001
        self.fx.write('docs/backlog/T-002.md', 'id: T-002\nstate: pulled\npulled_by: kit/other\n',
                      root=pm)
        self.record('id: T-001\nstate: pulled\npulled_by: kit/fresh\n')
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['claims']), ('C', ['T-001']), row)
        self.assertIn('next      release T-001 or block it (C): its store is in the main', out)
        self.assertNotIn('%s release' % kitroot.command('backlog', pm), out)
        self.assertNotIn('T-002', out)

    def test_an_unreadable_record_in_the_pm_data_store_keeps_it_c(self):
        self.configure({})
        pm = self.pm_worktree('main')
        os.makedirs(os.path.join(pm, 'docs', 'backlog'), exist_ok=True)
        with open(os.path.join(pm, 'docs', 'backlog', 'T-009.md'), 'wb') as f:
            f.write(b'id: T-009\nstate: pulled\ntitle: \xff\xfe\n')
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn('kept: whether a backlog record claims it is unknown: unreadable backlog'
                      ' record(s)', row['note'])
        self.assertIn('T-009', row['note'])

    def test_a_pm_data_kit_json_that_fails_to_parse_keeps_it_c(self):
        self.configure({})
        pm = self.pm_worktree('main')
        self.fx.write('.kit.json', '{not json\n', root=pm)
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn("kept: whether a backlog record claims it is unknown: the PM data"
                      " worktree's backlog store cannot be read (the .kit.json of the PM data"
                      " worktree", row['note'])

    def test_a_pm_data_branch_with_no_worktree_keeps_it_c(self):
        self.configure({})
        pmdata.write(self.fx.common_dir(), 'pmdata', 'private', self.git('rev-parse', 'HEAD'))
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn("kept: whether a backlog record claims it is unknown: the PM data"
                      " worktree's backlog store cannot be read", row['note'])

    def test_an_unreadable_store_keeps_it_c(self):
        self.record(b'id: T-009\nstate: pulled\npulled_by: kit/x\ntitle: \xff\xfe\n', 'T-009.md')
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn('kept: whether a backlog record claims it is unknown: unreadable backlog'
                      ' record(s)', row['note'])
        self.assertIn('T-009', row['note'])

    def test_an_unsaved_commit_in_its_head_reflog_keeps_it_c(self):
        path = self.workstream('fresh', commits=0)
        self.git('checkout', '-q', '--detach', cwd=path)
        self.fx.write('lost.txt', 'lost\n', root=path)
        self.fx.commit('lost', 'lost.txt', cwd=path)
        self.git('checkout', '-q', 'kit/fresh', cwd=path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['note']), (
            'C', 'nothing of its own; kept: its reflogs hold 1 commit(s) no ref and not its tip'
            ' reaches'), row)

    def test_a_claim_in_the_pm_data_worktrees_store_keeps_it_c(self):
        self.configure({})
        pm = self.pm_worktree('main')
        self.fx.write('docs/backlog/T-002.md', 'id: T-002\nstate: pulled\npulled_by: kit/fresh\n',
                      root=pm)
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['claims']), ('C', ['T-002']), row)
        self.assertIn('kept: claimed by T-002', row['note'])
        self.assertIn('-C "%s" release T-002' % pm.replace('\\', '/'), out.replace('\\', '/'))

    def test_the_pm_data_worktrees_store_with_no_claim_makes_it_a(self):
        self.configure({})
        self.pm_worktree('main')
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'A')

    def test_the_pm_data_worktree_itself_is_never_empty_a(self):
        self.configure({'backlog': {'store': 'kit/backlog'}})
        pm = os.path.join(os.path.realpath(self.fx.base), 'pm')
        pmdata.write(self.fx.common_dir(), 'pmdata', 'private', self.git('rev-parse', 'HEAD'))
        self.git('worktree', 'add', '-q', '-b', 'pmdata', pm, 'HEAD')
        rc, out, data = self.run_classify()
        row = self.row(data, pm)
        self.assertEqual((row['class'], row['note']),
                         ('C', 'nothing of its own; kept: the PM data worktree'), row)


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

    def test_a_missing_folder_is_o_and_exit_one(self):
        path = self.workstream('gone')
        shutil.rmtree(path)
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 1, out)
        row = self.row(data, path)
        self.assertEqual((row['class'], row['reason'], row['unsaved'], row['note']),
                         ('O', 'its folder is missing (an orphan: `--check` reports it)', 0,
                          'nothing only its registration keeps'))
        self.assertIn('summary   A 0, B 0, C 0, E 0, L 0, O 1', out)

    def test_a_locked_missing_folder_is_l(self):
        path = self.workstream('gone')
        self.git('worktree', 'lock', '--reason', 'on a drive', path)
        shutil.rmtree(path)
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.row(data, path)['class'], 'L')

    def test_no_head_is_l(self):
        path = self.workstream('nohead')
        with open(os.path.join(self.fx.common_dir(), 'worktrees', 'nohead', 'HEAD'), 'w',
                  newline='\n') as f:
            f.write('ref: refs/heads/kit/missing\n')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['reason']),
                         ('L', 'unreadable: git reported no HEAD for it'), row)

    def test_a_status_that_cannot_be_read_is_l(self):
        path = self.workstream('badindex')
        with open(os.path.join(self.fx.common_dir(), 'worktrees', 'badindex', 'index'),
                  'wb') as f:
            f.write(b'not an index at all')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'L', row)
        self.assertTrue(row['reason'].startswith('unreadable: its status ('), row)

    def test_activity_that_cannot_be_read_is_l(self):
        path = self.workstream('denied')
        with mock.patch.object(sweep, 'last_activity', return_value=(None, 'access denied')):
            rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['reason'],
                         'unreadable: its last activity (access denied)')

    @windows_only('held_folder opens folders for delete: Windows only')
    def test_an_unread_process_and_no_held_folder_leaves_the_class(self):
        path = self.workstream('free')
        rc, out, data = self.run_classify(procs=lambda: (([], 1, 1), None))
        self.assertEqual(self.row(data, path)['class'], 'C')

    @windows_only('held_folder opens folders for delete: Windows only')
    def test_an_unread_process_and_a_held_folder_is_l(self):
        path = self.workstream('stood')
        inner = os.path.join(path, 'sub')
        os.makedirs(inner)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                 cwd=inner)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.time() + 10
        while holders.held_folder(path) is None and time.time() < deadline:
            time.sleep(0.1)
        rc, out, data = self.run_classify(procs=lambda: (([], 1, 1), None))
        row = self.row(data, path)
        self.assertEqual(row['class'], 'L', row)
        self.assertEqual(row['reason'],
                         'unreadable: 1 process(es) could not be read and %s cannot be opened '
                         'for delete (Windows error 32): a process we could not read may hold it'
                         % inner)

    @windows_only('held_folder opens folders for delete: Windows only')
    def test_an_unread_process_and_a_tree_past_walk_max_is_l(self):
        path = self.workstream('big')
        with mock.patch.object(holders, 'WALK_MAX', 1):
            rc, out, data = self.run_classify(procs=lambda: (([], 2, 3), None))
        reason = self.row(data, path)['reason']
        self.assertTrue(reason.startswith('unreadable: 2 process(es) could not be read and %s '
                                          'has more than' % path), reason)
        self.assertTrue(reason.endswith('a process we could not read may hold it'), reason)

    def test_an_unread_process_off_windows_is_l_and_says_not_probed(self):
        path = self.workstream('offwin')
        with mock.patch.object(holders, 'WINDOWS', False), \
                mock.patch.object(kitlib.platform, 'NAME', 'unsupported'):
            rc, out, data = self.run_classify(procs=lambda: (([], 1, 1), None))
        row = self.row(data, path)
        self.assertEqual(row['class'], 'L', row)
        self.assertEqual(row['reason'],
                         'unreadable: 1 process(es) could not be read and %s was not probed: the '
                         'open for delete is made on Windows only: a process we could not read '
                         'may hold it' % path)

    def test_an_unread_process_on_linux_is_l_and_says_why(self):
        path = self.workstream('onlinux')
        with mock.patch.object(holders, 'WINDOWS', False), \
                mock.patch.object(kitlib.platform, 'NAME', 'linux'):
            rc, out, data = self.run_classify(procs=lambda: (([], 1, 1), None))
        row = self.row(data, path)
        self.assertEqual(row['class'], 'L', row)
        self.assertEqual(row['reason'],
                         'unreadable: 1 process(es) could not be read and %s was not probed: '
                         'Linux has no open that shows which folders a process holds: a process '
                         'we could not read may hold it' % path)

    @windows_only('the process scan is Windows only')
    def test_the_real_process_scan_finds_this_process_in_a_tree(self):
        path = self.workstream('here')
        self.land('kit/here')
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(path)
        rc, out, data = self.run_classify(procs=classify.process_cwds)
        self.assertIn('process pid %d ' % os.getpid(), self.row(data, path)['reason'])


@real_git('git reflog: the fake writes no reflog, and A reads it')
@gitfixture.real_fixture('A reads the reflog entry a fixture commit() writes, which only real '
                         'git writes')
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
        self.configure({'backlog': {'store': 'main'}})
        before = self.snapshot()
        rc, out, data = self.run_classify()
        self.assertEqual(data['counts'], {'A': 1, 'B': 1, 'C': 1, 'E': 2, 'L': 0, 'O': 0}, out)
        self.assertEqual(self.snapshot(), before)


@real_git('git reflog: the fake writes no reflog, and an orphan is read from its admin folder')
class Orphans(ClassifyCase):
    """O: a registered tree whose folder is gone. Its admin folder (`git worktree remove` deletes
    it) may keep a commit no ref reaches; then it is left for the owner."""

    def detached_orphan(self, slug):
        """A detached tree that made a commit, checked out main again and lost its folder: only
        its HEAD reflog keeps that commit. (its path, the commit)"""
        path = os.path.join(self.home, slug)
        self.git('worktree', 'add', '-q', '--detach', path, 'main')
        path = os.path.realpath(path)
        self.fx.write('left.txt', 'left\n', root=path)
        left = self.fx.commit('left behind', 'left.txt', cwd=path)
        self.git('checkout', '-q', '--detach', 'main', cwd=path)
        shutil.rmtree(path)
        return path, left

    def test_an_orphan_whose_reflog_keeps_a_commit_is_left_for_the_owner(self):
        path, left = self.detached_orphan('gone')
        rc, out, data = self.run_classify()
        self.assertEqual(rc, 1, out)
        row = self.row(data, path)
        self.assertEqual((row['class'], row['unsaved']), ('O', 1), out)
        self.assertEqual(row['note'], '1 commit(s) only its registration keeps, which no ref'
                                      ' reaches: left for the owner')

    def test_a_per_worktree_ref_keeps_a_commit_too(self):
        path = os.path.join(self.home, 'bisect')
        self.git('worktree', 'add', '-q', '--detach', path, 'main')
        path = os.path.realpath(path)
        self.fx.write('b.txt', 'b\n', root=path)
        kept = self.fx.commit('bisect only', 'b.txt', cwd=path)
        self.git('update-ref', 'refs/bisect/bad', kept, cwd=path)
        self.git('checkout', '-q', '--detach', 'main', cwd=path)
        self.git('reflog', 'expire', '--expire=now', '--all', cwd=path)
        shutil.rmtree(path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['unsaved']), ('O', 1), out)

    def test_a_reflog_a_ref_reaches_keeps_nothing(self):
        path = self.workstream('gone')
        shutil.rmtree(path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['unsaved'], row['note']),
                         ('O', 0, 'nothing only its registration keeps'), out)
        self.assertIn('orphan', out)

    def test_an_unreadable_admin_folder_is_left_for_the_owner(self):
        path = self.workstream('gone')
        shutil.rmtree(path)
        with open(os.path.join(self.fx.common_dir(), 'worktrees', 'gone', 'ORIG_HEAD'), 'w',
                  newline='\n') as f:
            f.write('not an id\n')
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual((row['class'], row['unsaved']), ('O', None), out)
        self.assertIn('is unknown (', row['note'])
        self.assertIn('has a line this verb cannot read', row['note'])


class BranchLiveness(ClassifyCase):

    def liveness(self, branch, now):
        return classify.branch_liveness(self.repo, branch, now=now, machine=free,
                                        procs=no_procs)

    def test_a_dead_tree_gives_its_tip(self):
        path = self.workstream('quiet')
        head = self.git('rev-parse', 'HEAD', cwd=path)
        self.assertEqual(self.liveness('kit/quiet', time.time() + LATER), (None, head))

    def test_a_live_tree_gives_why_and_where(self):
        path = self.workstream('busy')
        why, tip = self.liveness('kit/busy', time.time())
        self.assertIsNone(tip)
        self.assertTrue(why.startswith('active '), why)
        self.assertTrue(why.endswith('under the 24 h dead bar (%s)' % os.path.normpath(path)),
                        why)

    def test_a_branch_no_tree_is_on_is_live(self):
        self.git('branch', 'kit/treeless')
        self.assertEqual(self.liveness('kit/treeless', time.time() + LATER),
                         ('no worktree here is on kit/treeless, so whether its work goes on '
                          'cannot be judged', None))


class StaleMarkShown(ClassifyCase):

    def write_marks(self, *records, text=None):
        with open(os.path.join(self.fx.common_dir(), marks.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(text if text is not None else json.dumps({'version': 1,
                                                              'marks': list(records)}))

    def record(self, slug, path, at=None):
        return {'branch': 'kit/' + slug, 'tip': self.git('rev-parse', 'kit/' + slug),
                'path': path, 'why': 'superseded', 'by': 'c1 (coordinator)',
                'at': marks.stamp(time.time() if at is None else at)}

    def test_an_unmarked_tree_is_offered_a_mark(self):
        path = self.workstream('open')
        rc, out, data = self.run_classify()
        self.assertNotIn('mark', self.row(data, path))
        self.assertIn(' kit/open --mark-stale "<why>"   (C, so the nightly sweep abandons it', out)
        self.assertNotIn('--unmark-stale', out)
        self.assertNotIn('marks_error', data)

    def test_a_mark_that_holds_is_shown_with_its_clear_line(self):
        path = self.workstream('open')
        self.write_marks(self.record('open', path))
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIsNone(row['mark']['void'], row)
        self.assertEqual((row['mark']['why'], row['mark']['by']), ('superseded', 'c1 (coordinator)'))
        self.assertIn('mark: stale since ', out)
        self.assertIn(' kit/open --unmark-stale   (C, marked stale', out)
        self.assertNotIn('--mark-stale', out)

    def test_a_void_mark_is_shown_void_and_a_new_mark_offered(self):
        path = self.workstream('open')
        mark = self.record('open', path)
        self.write_marks(mark)
        self.fx.write('more.txt', 'more\n', root=path)
        self.fx.commit('more', 'more.txt', cwd=path)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertIn('the tip moved from %s' % mark['tip'][:10], row['mark']['void'])
        self.assertIn('mark: void (the tip moved from ', out)
        self.assertIn(' kit/open --mark-stale "<why>"', out)
        self.assertNotIn('--unmark-stale', out)

    def test_activity_after_the_mark_shows_it_void(self):
        path = self.workstream('open')
        self.write_marks(self.record('open', path, at=time.time() - 3600))
        rc, out, data = self.run_classify()
        self.assertIn('changed at ', self.row(data, path)['mark']['void'])

    def test_an_unreadable_marks_file_is_a_note_and_offers_nothing(self):
        path = self.workstream('open')
        self.write_marks(text='not json')
        rc, out, data = self.run_classify()
        self.assertIn('note      the stale marks cannot be read (', out)
        self.assertIn('is not valid JSON', data['marks_error'])
        self.assertNotIn('mark', self.row(data, path))
        self.assertNotIn('--mark-stale', out)
        self.assertNotIn('--unmark-stale', out)

    def assert_no_mark_offered(self, out, why):
        self.assertNotIn('--mark-stale "<why>"', out)
        self.assertIn('takes no --mark-stale (C): %s' % why, out)

    def test_a_dirty_tree_is_told_why_it_takes_no_mark(self):
        path = self.workstream('wip')
        self.fx.write('loose.txt', 'not committed\n', root=path)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['class'], 'C')
        self.assert_no_mark_offered(out, '1 dirty file(s): it marks a clean tree only')
        got = marks.mark(self.repo, 'kit/wip', 'stale', liveness=lambda m, b: (None, 'tip'))
        self.assertEqual(got.code, remove.REFUSED, got.lines)    # the note agrees with the tool

    def test_a_landed_dirty_tree_takes_no_mark(self):
        path = self.workstream('after')
        self.land('kit/after')
        self.fx.write('loose.txt', 'x\n', root=path)
        rc, out, data = self.run_classify()
        self.assertEqual(self.row(data, path)['note'], 'landed, not clean')
        self.assert_no_mark_offered(out, '1 dirty file(s)')

    def test_a_tree_with_nothing_of_its_own_takes_no_mark(self):
        path = self.workstream('fresh', commits=0)
        rc, out, data = self.run_classify()
        row = self.row(data, path)
        self.assertEqual(row['class'], 'C', row)
        self.assertIn('nothing of its own', row['note'])
        self.assert_no_mark_offered(out, 'its tip is in the base: landed work needs no mark')

    def test_a_tree_outside_the_worktree_folder_is_offered_no_mark(self):
        path = self.outside('away', 'main')
        self.git('checkout', '-q', '-b', 'kit/away', cwd=path)
        self.fx.write('a.txt', 'a\n', root=path)
        self.fx.commit('a', 'a.txt', cwd=path)
        rc, out, data = self.run_classify()
        self.assertIn(self.row(data, path)['class'], ('B', 'C'))
        self.assertNotIn('--mark-stale', out)


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

    def test_json_alone_is_refused(self):
        rc, out, err = self.cli('--json')
        self.assertEqual(rc, 2, out + err)


if __name__ == '__main__':
    unittest.main()
