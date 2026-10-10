"""kitlib.gate.runner.branch_gate: the branch gate under book() on the scratch lock store, and the
rule that the gate-OK record is written only for the commit whose clean tree passed."""
import os
import sys
import unittest
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.gate import runner
from kitlib.records import gate_ok
from tests import gitfixture
from tests.gate import fixtures


class BranchGate(fixtures.GateRepoCase):
    def gate(self, wt, **kw):
        kw.setdefault('log_dir', self.logs)
        return runner.branch_gate(wt, book_kw=self.book_kw, **kw)

    def gate_config(self, wt):
        return runner.gate_config(kconfig.load(wt))

    def test_pass_writes_the_record_for_exactly_that_commit(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.head, tip)
        self.assertTrue(out.verdict.startswith('GATE OK'), out.verdict)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        self.assertTrue(os.path.isfile(out.record))
        rec, why = gate_ok.verify(wt, tip, self.gate_config(wt))
        self.assertIsNone(why)
        self.assertEqual(rec['base'], self.main)
        self.assertEqual([s['name'] for s in rec['steps']], ['unit'])
        # the record lives in the common dir: every checkout of the repo sees it
        self.assertEqual(gate_ok.verify(self.repo.root, tip)[1], None)
        # a later commit is not covered
        self.repo.write('b.py', 'y = 2\n', root=wt)
        later = self.repo.commit('later', 'b.py', cwd=wt)
        self.assertIsNotNone(gate_ok.verify(wt, later)[1])
        # the run went through the scratch lock store
        self.assertTrue(any(d.get('kind') == 'engine-test' for d in self.store.history_records(1)))

    def test_the_lock_run_is_a_gate_run(self):
        """The gate books as a gate (book(gate=True)), so the lock stops the build daemons it
        leaves (kitlib.lock.site, machine_lock.daemons)."""
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        self.assertEqual(self.gate(wt).code, G.OK)
        ended = self.store.path('ended')
        recs = [self.store.read('ended/' + n) for n in os.listdir(ended)]
        self.assertEqual([r.get('gate') for r in recs], [True])

    def test_red_writes_no_record(self):
        wt, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.verdict.startswith('GATE FAILED: unit'), out.verdict)
        self.assertIsNone(out.record)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))

    def test_dirty_tracked_file_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.repo.write('a.py', 'x = 2\n', root=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.record)
        self.assertIn('uncommitted', out.no_record)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))

    def test_untracked_file_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.repo.write('new.py', 'z = 1\n', root=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.record)
        self.assertIn('new.py', out.changed)          # gated, so selected, but not recorded
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))

    def test_only_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt, only=['unit'])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.record)
        self.assertIn('--only', out.no_record)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))

    def test_head_moved_during_the_gate_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        # a step that commits: HEAD moves while the gate runs (the step gets the fixture's git env).
        # The step is a python that runs git: the gate starts steps in this process, where the
        # unit tier allows no git (tests/__init__.py).
        step = {'name': 'mover', 'always': True,
                'cmd': [sys.executable, '-c', 'import subprocess; subprocess.check_call('
                        '["git", "commit", "--allow-empty", "-m", "moved"])']}
        self.repo.write('.kit.json', '{"gate": {"steps": [%s]}}\n' % (
            __import__('json').dumps(step)), root=wt)
        tip = self.repo.commit('mover gate', '.kit.json', cwd=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.record)
        self.assertIn('HEAD moved', out.no_record)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))
        moved = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        self.assertNotEqual(moved, tip)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, moved)))

    def test_plan_books_and_runs_nothing(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt, plan=True)
        self.assertEqual(out.code, G.OK)
        self.assertEqual(out.chosen, ['unit'])
        self.assertTrue(any(line.startswith('RUN   unit') for line in out.lines), out.lines)
        self.assertEqual(self.store.history_records(1), [])
        self.assertFalse(os.path.exists(self.logs))

    def test_unknown_only_and_bad_base_are_usage(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        self.assertEqual(self.gate(wt, only=['nosuch']).code, G.USAGE)
        self.assertEqual(self.gate(wt, base='no-such-ref').code, G.USAGE)

    def test_unported_setting_is_refused(self):
        wt = self.repo.worktree('feature')
        self.repo.write('.kit.json', '{"gate": {"trailers": true, "steps": []}}\n', root=wt)
        self.repo.commit('trailers', '.kit.json', cwd=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.REFUSED)
        self.assertIn('trailers', out.lines[0])

    def test_worktree_guard_refuses_the_main_checkout(self):
        self.repo.write('.kit.json', '{"gate": {"worktree_guard": true, "steps": []}}\n')
        self.repo.commit('guard', '.kit.json')
        self.assertEqual(self.gate(self.repo.root).code, G.REFUSED)

    def test_no_step_selected_is_refused_and_writes_no_record(self):
        wt = self.repo.worktree('feature')
        self.repo.write('.kit.json', '{"gate": {"steps": [{"name": "src", "globs": ["src/*"], '
                        '"cmd": ["{python}", "-c", "print(1)"]}]}}\n', root=wt)
        self.repo.write('a.py', 'x = 1\n', root=wt)
        tip = self.repo.commit('nothing covered', '.kit.json', 'a.py', cwd=wt)
        self.assertEqual(self.gate(wt, plan=True).code, G.OK)       # a plan may show nothing
        out = self.gate(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: no gate step selected'), out.lines)
        self.assertEqual(out.no_record, 'no gate step selected')
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))
        self.assertEqual(self.store.history_records(1), [])          # nothing booked or run
        self.assertFalse(os.path.exists(self.logs))

    def test_check_step_runs_the_hook_module(self):
        wt = self.repo.worktree('feature')
        self.repo.write('tools/kit_hooks.py', (
            'def ok(ctx):\n'
            '    ctx["log"].write("checked %s\\n" % ",".join(ctx["changed"]))\n'
            '    return ctx["run"]([__import__("sys").executable, "-c", "print(1)"])\n'
            'GATE_CHECKS = {"ok": ok}\n'), root=wt)
        self.repo.write('.kit.json', '{"hook": {"module": "kit_hooks"}, "gate": {"steps": '
                        '[{"name": "c", "always": true, "check": "ok"}]}}\n', root=wt)
        tip = self.repo.commit('check', 'tools/kit_hooks.py', '.kit.json', cwd=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        with open(out.results[0]['log'], encoding='utf-8') as f:
            self.assertIn('checked .kit.json,tools/kit_hooks.py', f.read())

    def test_loading_the_hook_module_writes_no_bytecode_into_the_tree(self):
        """In a terminal without PYTHONDONTWRITEBYTECODE, importing the hook module (and a sibling
        it imports) would leave tools/__pycache__ in the work tree: an untracked change, so the
        gate would write no record. The fixture has no .gitignore, as a client repo may not."""
        wt = self.repo.worktree('feature')
        self.repo.write('tools/hook_sibling.py', 'WORD = "sib"\n', root=wt)
        self.repo.write('tools/kit_hooks.py', (
            'import os, sys\n'
            'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n'
            'import hook_sibling\n'
            'sys.path.pop(0)\n'
            'def ok(ctx):\n'
            '    return ctx["run"]([sys.executable, "-c", "print(1)"])\n'
            'GATE_CHECKS = {"ok": ok}\n'), root=wt)
        self.addCleanup(sys.modules.pop, 'hook_sibling', None)
        self.repo.write('.kit.json', '{"hook": {"module": "kit_hooks"}, "gate": {"steps": '
                        '[{"name": "c", "always": true, "check": "ok"}]}}\n', root=wt)
        tip = self.repo.commit('check', 'tools/kit_hooks.py', 'tools/hook_sibling.py',
                               '.kit.json', cwd=wt)
        with mock.patch.object(sys, 'dont_write_bytecode', False):
            out = self.gate(wt)
            self.assertFalse(sys.dont_write_bytecode)          # put back as it was
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.no_record, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        self.assertFalse(os.path.exists(os.path.join(wt, 'tools', '__pycache__')),
                         os.listdir(os.path.join(wt, 'tools')))

    def test_a_cmd_step_python_writes_no_bytecode_into_the_tree(self):
        """A step's child Python does not inherit sys.dont_write_bytecode: the gate gives it
        PYTHONDONTWRITEBYTECODE=1, even when the environment it was started from has none, so a
        script importing a sibling leaves no tools/__pycache__ (the fixture has no .gitignore)."""
        wt = self.repo.worktree('feature')
        self.repo.write('tools/step_sibling.py', 'WORD = "sib"\n', root=wt)
        self.repo.write('tools/step_main.py', 'import step_sibling\nprint("step", step_sibling.WORD)\n',
                        root=wt)
        self.repo.write('.kit.json', '{"gate": {"steps": [{"name": "s", "always": true, '
                        '"cmd": ["{python}", "tools/step_main.py"], "pass": "^step sib"}]}}\n',
                        root=wt)
        tip = self.repo.commit('cmd step', 'tools/step_main.py', 'tools/step_sibling.py',
                               '.kit.json', cwd=wt)
        self.book_kw['env'].pop('PYTHONDONTWRITEBYTECODE', None)
        with mock.patch.dict(os.environ):
            os.environ.pop('PYTHONDONTWRITEBYTECODE', None)
            out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        with open(out.results[0]['log'], encoding='utf-8') as f:
            self.assertIn('step sib', f.read())              # the script and its import ran
        self.assertIsNone(out.no_record, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        self.assertFalse(os.path.exists(os.path.join(wt, 'tools', '__pycache__')),
                         os.listdir(os.path.join(wt, 'tools')))
        self.assertNotIn('PYTHONDONTWRITEBYTECODE', self.book_kw['env'])   # merged, not mutated

    def test_a_check_that_imports_a_sibling_lazily_writes_no_bytecode_into_the_tree(self):
        """The check runs after the module loaded: its own import of a sibling is guarded too."""
        wt = self.repo.worktree('feature')
        self.repo.write('tools/lazy_sibling.py', 'WORD = "lazy"\n', root=wt)
        self.repo.write('tools/kit_hooks.py', (
            'import os, sys\n'
            'def ok(ctx):\n'
            '    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n'
            '    try:\n'
            '        import lazy_sibling\n'
            '    finally:\n'
            '        sys.path.pop(0)\n'
            '    ctx["log"].write("sibling %s\\n" % lazy_sibling.WORD)\n'
            '    return 0\n'
            'GATE_CHECKS = {"ok": ok}\n'), root=wt)
        self.addCleanup(sys.modules.pop, 'lazy_sibling', None)
        self.repo.write('.kit.json', '{"hook": {"module": "kit_hooks"}, "gate": {"steps": '
                        '[{"name": "c", "always": true, "check": "ok"}]}}\n', root=wt)
        tip = self.repo.commit('lazy check', 'tools/kit_hooks.py', 'tools/lazy_sibling.py',
                               '.kit.json', cwd=wt)
        with mock.patch.object(sys, 'dont_write_bytecode', False):
            out = self.gate(wt)
            self.assertFalse(sys.dont_write_bytecode)          # put back as it was
        self.assertEqual(out.code, G.OK, out.lines)
        with open(out.results[0]['log'], encoding='utf-8') as f:
            self.assertIn('sibling lazy', f.read())          # the lazy import did run
        self.assertIsNone(out.no_record, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        self.assertFalse(os.path.exists(os.path.join(wt, 'tools', '__pycache__')),
                         os.listdir(os.path.join(wt, 'tools')))


def mode_step(word):
    return ['{python}', '-c', 'print("mode %s")' % word]


class FullCmd(fixtures.GateRepoCase):
    """A step's full_cmd is what a full run (gate.py --full, land --full) runs; a gate without
    --full still runs the step's cmd, which a landing gates with."""
    GATE = {'steps': [{'name': 'suite', 'always': True, 'cmd': mode_step('select'),
                       'full_cmd': mode_step('full'), 'pass': '^mode '}]}

    def ran(self, out):
        self.assertEqual(out.code, G.OK, out.lines)
        with open(out.results[0]['log'], encoding='utf-8') as f:
            return f.read().strip()

    def test_the_branch_gate_runs_cmd_and_full_runs_full_cmd(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})

        def gate(**kw):
            return runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs, **kw)
        self.assertEqual(self.ran(gate()), 'mode select')
        self.assertEqual(self.ran(gate(full=True)), 'mode full')

    def test_the_merged_tree_gate_runs_cmd_and_full_runs_full_cmd(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        cfg = kconfig.load(wt)

        def gate(**kw):
            return runner.merged_gate(wt, cfg, self.main, log_dir=self.logs,
                                      book_kw=self.book_kw, **kw)
        self.assertEqual(self.ran(gate()), 'mode select')
        self.assertEqual(self.ran(gate(full=True)), 'mode full')


class DefaultBaseNote(fixtures.GateRepoCase):
    """With no --base the gate measures against origin/main and prints the line naming a local
    main that differs; an explicit --base prints none."""

    def gated(self, **kw):
        stale, fresh = gitfixture.stale_local_main(self.repo)
        wt = self.repo.worktree('feature')
        lines = []
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs, say=lines.append, **kw)
        return stale, fresh, out, lines

    def test_a_stale_local_main_is_measured_against_origin_main_and_the_note_is_printed(self):
        stale, fresh, out, lines = self.gated()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('base: local main %s is 1 behind origin/main %s; measuring against '
                      'refs/remotes/origin/main' % (stale[:7], fresh[:7]), lines)

    def test_an_explicit_base_prints_no_note(self):
        _stale, _fresh, out, lines = self.gated(base='main')
        self.assertFalse([l for l in lines if 'measuring against' in l], lines)


if __name__ == '__main__':
    unittest.main()
