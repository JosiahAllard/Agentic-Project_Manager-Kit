"""kitlib.gate.reuse: the per-step gate cache (docs/gate.md "Gate reuse"). A step that asks for
reuse and declares its inputs is not run again, in a branch-gate rerun or a landing, when a pass of
the same key (its inputs, argv, cwd, gate config and kit files) is stored; every doubt runs it. A
scratch repository with a bare remote, the scratch lock store; the gate-OK record's kit identity
is pinned so the test does not depend on the kit checkout being clean."""
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land, result, reuse, runner, strict_defaults, verdicts
from kitlib.gate import steps as S
from kitlib.records import gate_ok
from tests.gate import fixtures

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH = 'kit/feature'
OK_CMD = ['{python}', '-c', 'print("ok")']
IDENT = {'commit': 'a' * 40, 'python': os.path.normcase(os.path.abspath(sys.executable)),
         'clean': True}
SRC = {'src/a.py': 'x = 1\n'}
# RED_AWARE reads RED, so RED is declared
UNIT = {'name': 'unit', 'always': True, 'globs': ['src/*', 'RED'], 'cmd': fixtures.RED_AWARE,
        'pass': '^ok$', 'reuse': True}
MERGED = {'name': 'merged', 'always': True, 'globs': ['src/*'], 'cmd': OK_CMD, 'pass': '^ok$',
          'merged_args': ['--merged'], 'reuse': True}
PLAIN = {'name': 'plain', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$'}
STAMP = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ'


class ReuseCase(fixtures.GateRepoCase):
    GATE = {'steps': [UNIT, MERGED, PLAIN]}
    KIT = {'landing': {}}
    ASKING = ('unit', 'merged')

    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        self.ident = dict(IDENT)
        p = mock.patch.object(runner, 'kit_identity', side_effect=lambda: dict(self.ident))
        p.start()
        self.addCleanup(p.stop)
        self.said = []

    def gate(self, wt, **kw):
        """A branch gate of `wt`; self.said holds only its lines."""
        self.said[:] = []
        kw.setdefault('book_kw', self.book_kw)
        kw.setdefault('log_dir', self.logs)
        return runner.branch_gate(wt, say=self.said.append, **kw)

    def gated_branch(self, name='feature', files=None):
        wt, tip = self.branch(name, files or dict(SRC))
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNotNone(out.record, out.lines)
        return wt, tip

    def commit(self, wt, files, msg='more work'):
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        return self.repo.commit(msg, *files, cwd=wt)

    def land(self, branch, tip, expect_main=None, **kw):
        self.said[:] = []
        kw.setdefault('git_env', self.env)
        kw.setdefault('book_kw', self.book_kw)
        kw.setdefault('log_dir', self.logs)
        fixtures.gate_full(self.repo.root, *(tip if isinstance(tip, list) else [tip]))
        lander = land.Lander(self.repo.root, branch, expect_main or self.main, tip,
                             say=self.said.append, **kw)
        return lander, lander.run()

    def lines(self, word=None):
        out = [l for l in self.said if l.startswith(reuse.PREFIX) or l.startswith('REUSED')]
        if word is None:
            return out
        return [l for l in out if l.startswith(reuse.PREFIX + word + ' ')]

    def assert_ran(self, log_dir, *names):
        for n in names:
            self.assertTrue(os.path.exists(os.path.join(log_dir, n + '.log')),
                            '%s did not run: %s' % (n, self.said))

    def assert_not_ran(self, log_dir, *names):
        for n in names:
            self.assertFalse(os.path.exists(os.path.join(log_dir, n + '.log')),
                             '%s ran: %s' % (n, self.said))

    def entries(self):
        folder = reuse.cache_dir(self.repo.root)
        out = []
        if os.path.isdir(folder):
            for d in sorted(os.listdir(folder)):
                p = os.path.join(folder, d)
                if os.path.isdir(p):
                    out += [d + '/' + n for n in sorted(os.listdir(p)) if reuse.ENTRY_RE.match(n)]
        return out

    def assert_none(self, out, log_dir, why, names=None):
        """The gate passed, every asking step ran and says NONE with `why`, and nothing was
        stored."""
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.lines(), [reuse.PREFIX + 'NONE %s: %s' % (n, why)
                                        for n in names or self.ASKING], self.said)
        self.assert_ran(log_dir, *(names or self.ASKING))
        self.assertNotIn('reused', out.verdict)


class Landing(ReuseCase):
    def test_a_step_that_passed_on_the_same_inputs_is_reused(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        logs = lander.gate_logs
        self.assert_not_ran(logs, 'unit')
        self.assert_ran(logs, 'merged', 'plain')
        hit = self.lines('HIT')
        self.assertEqual(len(hit), 1, self.said)
        self.assertRegex(hit[0], '^%sHIT unit: key [0-9a-f]{12}, stored %s$'
                         % (re.escape(reuse.PREFIX), STAMP))
        miss = self.lines('MISS')
        self.assertEqual(len(miss), 1, self.said)        # its merged_args make another key
        self.assertRegex(miss[0], '^%sMISS merged: no result stored for key [0-9a-f]{12}$'
                         % re.escape(reuse.PREFIX))
        self.assertTrue(any(l.startswith('REUSED unit: passed on the same inputs (key ')
                            for l in self.lines()), self.said)
        with open(os.path.join(logs, 'reuse.log'), encoding='utf-8') as f:
            self.assertEqual(f.read().splitlines(), self.lines())
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual({s['name']: s['result'] for s in rec['steps']},
                         {'unit': 'reused', 'merged': 'pass', 'plain': 'pass'})

    def test_main_moving_on_other_paths_still_reuses(self):
        _, tip = self.gated_branch()
        other = self.repo.worktree('other')
        self.repo.write('o.txt', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.txt', cwd=other)
        self.repo.git('merge', '-q', '--ff-only', moved)
        self.repo.git('push', '-q', 'origin', 'main')
        lander, out = self.land(BRANCH, tip, expect_main=moved)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)', self.said)
        self.assert_not_ran(lander.gate_logs, 'unit')

    def test_main_moving_an_input_runs_the_step(self):
        _, tip = self.gated_branch()
        other = self.repo.worktree('other')
        self.repo.write('src/b.py', 'b\n', root=other)
        moved = self.repo.commit('other', 'src/b.py', cwd=other)
        self.repo.git('merge', '-q', '--ff-only', moved)
        self.repo.git('push', '-q', 'origin', 'main')
        lander, out = self.land(BRANCH, tip, expect_main=moved)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertNotIn('reused', out.verdict)
        self.assertTrue(self.lines('MISS')[0].startswith(reuse.PREFIX + 'MISS unit: no result '
                                                         'stored for key '), self.said)
        self.assert_ran(lander.gate_logs, 'unit')

    def test_main_changing_the_gate_config_runs_the_step(self):
        """Main's own change is not in the landing's changed paths, so only the config hash in
        the key sees it: unit's argv, cwd, program and inputs are the same."""
        _, tip = self.gated_branch()
        other = self.repo.worktree('other')
        steps = [UNIT, MERGED, dict(PLAIN, **{'pass': '^o.$'})]
        kit = dict(self.KIT, gate=dict(self.GATE, steps=steps))
        self.repo.write('.kit.json', json.dumps(kit, indent=1) + '\n', root=other)
        moved = self.repo.commit('another gate config', '.kit.json', cwd=other)
        self.repo.git('merge', '-q', '--ff-only', moved)
        self.repo.git('push', '-q', 'origin', 'main')
        lander, out = self.land(BRANCH, tip, expect_main=moved)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertNotIn('reused', out.verdict)
        self.assertTrue(self.lines('MISS')[0].startswith(reuse.PREFIX + 'MISS unit: no result '
                                                         'stored for key '), self.said)
        self.assert_ran(lander.gate_logs, 'unit')

    def test_a_batch_reuses_per_step(self):
        _, tip = self.gated_branch()
        _, tip2 = self.gated_branch('other', {'b.txt': 'y\n'})
        lander, out = self.land([BRANCH, 'kit/other'], [tip, tip2])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)', self.said)
        self.assert_not_ran(lander.gate_logs, 'unit')

    def test_a_landing_stores_what_it_ran(self):
        _, tip = self.branch('feature', dict(SRC))       # no branch gate: nothing stored yet
        self.assertEqual(self.entries(), [])
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual([l[len(reuse.PREFIX):].split(':')[0] for l in self.lines('MISS')],
                         ['MISS unit', 'MISS merged'])
        self.assertEqual(len(self.lines('STORED')), 2, self.said)
        self.assertEqual(len(self.entries()), 2)
        with open(os.path.join(lander.gate_logs, 'reuse.log'), encoding='utf-8') as f:
            self.assertEqual(f.read().splitlines(), self.lines())

    def test_fresh_reads_and_stores_nothing(self):
        _, tip = self.gated_branch()
        before = self.entries()
        lander, out = self.land(BRANCH, tip, fresh=True)
        self.assert_none(out, lander.gate_logs, '--fresh')
        self.assertEqual(self.entries(), before)

    def test_full_reads_and_stores_nothing(self):
        _, tip = self.gated_branch()
        before = self.entries()
        lander, out = self.land(BRANCH, tip, full=True)
        self.assert_none(out, lander.gate_logs, '--full')
        self.assertEqual(self.entries(), before)

    def test_a_later_red_gate_of_the_same_commit_reuses_nothing(self):
        wt, tip = self.gated_branch()
        self.repo.write('RED', 'x\n', root=wt)          # untracked: the same commit gates red
        self.assertEqual(self.gate(wt).code, G.FAILED)
        os.remove(os.path.join(wt, 'RED'))
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, 'a gate of %s on %s ended fail'
                         % (tip[:10], BRANCH))

    def test_a_tip_that_failed_to_land_is_gated_again(self):
        _, tip = self.gated_branch()
        _, first = self.land(BRANCH, tip, merged_gate=lambda l: G.Outcome(
            G.FAILED, ['GATE FAILED: unit (unit: flaky)'], verdict='GATE FAILED'))
        self.assertEqual(first.code, G.FAILED, first.lines)
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, '%s at %s was landed before (fail) and did not '
                         'land: its tree is gated again' % (BRANCH, tip[:10]))

    def test_an_error_while_planning_runs_every_step(self):
        _, tip = self.gated_branch()
        with mock.patch.object(reuse, 'list_files', side_effect=RuntimeError('boom')):
            lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, 'RuntimeError: boom')

    def test_a_failure_to_store_never_fails_the_gate(self):
        _, tip = self.branch('feature', dict(SRC))
        with mock.patch.object(reuse, '_write_entry', side_effect=OSError('disk full')):
            lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.lines('NOT STORED'), [
            reuse.PREFIX + 'NOT STORED unit: OSError: disk full',
            reuse.PREFIX + 'NOT STORED merged: OSError: disk full'], self.said)
        self.assertEqual(self.entries(), [])


class BranchRerun(ReuseCase):
    def test_a_rerun_after_a_change_outside_the_inputs_reuses(self):
        wt, _ = self.gated_branch()
        head = self.commit(wt, {'notes.txt': 'n\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit, merged)', self.said)
        self.assert_not_ran(out.log_dir, 'unit', 'merged')
        self.assert_ran(out.log_dir, 'plain')
        self.assertEqual(len(self.lines('HIT')), 2, self.said)
        rec, why = gate_ok.verify(self.repo.root, head)
        self.assertIsNone(why)
        self.assertEqual({s['name']: s['result'] for s in rec['steps']},
                         {'unit': 'reused', 'merged': 'reused', 'plain': 'pass'})

    def test_an_input_change_runs(self):
        wt, _ = self.gated_branch()
        self.commit(wt, {'src/a.py': 'x = 2\n'})
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK', self.said)
        self.assertEqual(len(self.lines('MISS')), 2, self.said)
        self.assert_ran(out.log_dir, 'unit', 'merged')

    def test_a_new_untracked_input_runs(self):
        wt, _ = self.gated_branch()
        self.repo.write('src/new.py', 'n\n', root=wt)
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK', self.said)
        self.assert_ran(out.log_dir, 'unit', 'merged')

    def test_an_ignored_file_under_the_globs_is_not_an_input(self):
        """The documented contract: inputs are the files git shows; a reuse step must not read
        an ignored or generated file."""
        files = dict(SRC, **{'.gitignore': '.claude/\nsrc/gen.py\n'})
        wt, _ = self.gated_branch(files=files)
        self.repo.write('src/gen.py', 'generated\n', root=wt)
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit, merged)', self.said)

    def test_full_reads_and_stores_nothing(self):
        wt, _ = self.gated_branch()
        before = self.entries()
        out = self.gate(wt, full=True)
        self.assert_none(out, out.log_dir, '--full')
        self.assertEqual(self.entries(), before)

    def test_only_reads_and_stores_nothing(self):
        wt, _ = self.gated_branch()
        before = self.entries()
        out = self.gate(wt, only=['unit'])
        self.assert_none(out, out.log_dir, '--only', names=('unit',))
        self.assertEqual(self.entries(), before)

    def test_a_kit_file_change_runs(self):
        wt, _ = self.gated_branch()
        other = dict(reuse.kit_identity(), files='0' * 64)
        with mock.patch.object(reuse, 'kit_identity', return_value=other):
            out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK', self.said)
        self.assertEqual(len(self.lines('MISS')), 2, self.said)

    def test_the_kit_commit_is_not_in_the_key(self):
        wt, _ = self.gated_branch()
        self.ident['commit'] = 'b' * 40         # another commit of the same kit files
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit, merged)', self.said)



class Strict(ReuseCase):
    def test_the_strict_paths_are_the_ones_the_code_imports(self):
        self.assertIs(reuse.STRICT_DEFAULTS, strict_defaults.STRICT_DEFAULTS)
        cfg = runner.kconfig.load(self.repo.root)
        paths = [g.replace('**', 'x.py') for g in reuse.STRICT_DEFAULTS]
        self.assertEqual(reuse.strict_hits(cfg, paths + ['src/a.py']), paths)

    def test_the_config_adds_no_strict_path(self):
        """`.kit.json` has no strict-path key (the schema refuses an unknown one), so the config
        is never read for one."""
        paths = [g.replace('**', 'x.py') for g in reuse.STRICT_DEFAULTS]
        self.assertEqual(reuse.strict_hits(None, paths + ['src/a.py']), paths)
        kit = dict(self.KIT, gate=self.GATE, landing={'strict_paths': ['src/*']})
        self.repo.write('.kit.json', json.dumps(kit, indent=1) + '\n')
        with self.assertRaises(runner.ConfigError) as caught:
            runner.kconfig.load(self.repo.root)
        self.assertIn('strict_paths', str(caught.exception))

    def test_a_strict_path_change_reads_and_stores_nothing(self):
        path = reuse.STRICT_DEFAULTS[0].replace('**', 'strict.py')
        why = 'a strict path changes: %s' % path
        wt, tip = self.branch('feature', dict(SRC, **{path: 'x = 1\n'}))
        out = self.gate(wt)
        self.assert_none(out, out.log_dir, why)
        self.commit(wt, {'notes.txt': 'n\n'})
        out = self.gate(wt)
        self.assert_none(out, out.log_dir, why)
        self.assertEqual(self.entries(), [])
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        # a strict branch lands only with a strong non-author verdict, and its merged gate is
        # escalated to --full --fresh (docs/gate.md "Strict paths"): --fresh is the why there
        common, _ = land.repo.common_dir(self.repo.root)
        verdicts.add(common, {'branch': BRANCH, 'tip': tip, 'verdict': 'LAND', 'risk': 'High',
                              'reviewer': 'pm-kit:reviewer', 'reviewer_model': 'opus',
                              'reviewer_id': 'agent-r1', 'author': 'Kit Test / strong-worker',
                              'range': '%s..%s' % (self.main[:10], tip), 'session': 's',
                              'records': []})
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, '--fresh')
        self.assertEqual(self.entries(), [])


WHOLE = {'name': 'whole', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$', 'reuse': True}
DIFFY = {'name': 'diffy', 'always': True, 'globs': ['src/*'], 'pass': '^ok$', 'reuse': True,
         'cmd': ['{python}', '-c', 'print("ok")', '{base}']}


class Undeclared(ReuseCase):
    GATE = {'steps': [WHOLE, DIFFY]}

    def test_a_step_with_undeclared_inputs_is_never_cached(self):
        wt, _ = self.branch('feature', dict(SRC))
        expect = [reuse.PREFIX + 'NONE whole: no declared inputs (no "globs" and no '
                  '"content".globs)',
                  reuse.PREFIX + 'NONE diffy: its command names {base}: it reads git history']
        for i in range(2):
            out = self.gate(wt)
            self.assertEqual(out.code, G.OK, out.lines)
            self.assertEqual(self.lines(), expect, self.said)
            self.assert_ran(out.log_dir, 'whole', 'diffy')
            self.commit(wt, {'notes.txt': 'n%d\n' % i})
        self.assertEqual(self.entries(), [])


TYPO = {'name': 'typo', 'always': True, 'globs': ['srcc/*'], 'cmd': OK_CMD, 'pass': '^ok$',
        'reuse': True}
GEN = {'name': 'gen', 'always': True, 'content': {'globs': ['gen/*'], 'regex': 'x'},
       'cmd': OK_CMD, 'pass': '^ok$', 'reuse': True}
NO_INPUT = 'its "globs" and "content".globs match no file git shows in the work tree'


class NoInputs(ReuseCase):
    """Globs that match no file git shows are no declared input: a mistyped glob, or one that
    names only ignored files, would key the step on its command alone."""
    GATE = {'steps': [TYPO, GEN, UNIT]}
    ASKING = ('typo', 'gen')

    def test_a_step_whose_globs_match_no_file_is_never_cached(self):
        files = dict(SRC, **{'.gitignore': '.claude/\ngen/\n'})
        wt, _ = self.branch('feature', files)
        self.repo.write('gen/out.py', 'x = 1\n', root=wt)       # ignored: git does not show it
        none = [reuse.PREFIX + 'NONE %s: %s' % (n, NO_INPUT) for n in self.ASKING]
        for i in range(2):
            out = self.gate(wt)
            self.assertEqual(out.code, G.OK, out.lines)
            self.assertEqual(self.lines()[:2], none, self.said)
            self.assert_ran(out.log_dir, *self.ASKING)
            self.assertEqual([e for e in self.entries() if '"typo"' in self.body(e)
                              or '"gen"' in self.body(e)], [])
            self.commit(wt, {'notes.txt': 'n%d\n' % i})
        # the positive case, in the same gate: unit has inputs and is cached and reused
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)', self.said)
        self.assertEqual(len(self.entries()), 1, self.entries())
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)', self.said)
        self.assertEqual(self.lines()[:2], none, self.said)
        self.assert_ran(lander.gate_logs, *self.ASKING)
        self.assertEqual(len(self.entries()), 1, self.entries())

    def body(self, entry):
        with open(os.path.join(reuse.cache_dir(self.repo.root), entry), encoding='utf-8') as f:
            return f.read()

    def test_step_key_refuses_an_empty_input_set(self):
        cfg = runner.kconfig.load(self.repo.root)
        step = S.Step('typo', [sys.executable, '-c', 'print("ok")'], None, self.repo.root, [],
                      [], (0,), False, False, [], False)
        kit = {'files': '0' * 64}
        with self.assertRaises(reuse.NotCacheable) as caught:
            reuse.step_key(self.repo.root, cfg, TYPO, step, ['src/a.py', 'notes.txt'], kit)
        self.assertEqual(str(caught.exception), NO_INPUT)
        self.repo.write('srcc/a.py', 'x = 1\n')
        key, ins = reuse.step_key(self.repo.root, cfg, TYPO, step, ['srcc/a.py'], kit)
        self.assertRegex(key, '^[0-9a-f]{64}$')
        self.assertEqual(sorted(ins), ['srcc/a.py'])


class CheckStep(unittest.TestCase):
    def test_a_check_step_is_never_cached(self):
        spec = {'name': 'pins', 'check': 'pins', 'globs': ['src/*'], 'reuse': True}
        step = S.Step('pins', None, 'pins', '.', [], [], (0,), False, False, [], False)
        self.assertEqual(reuse.undeclared(spec, step, 'a' * 40), 'a check step')

    def test_setup_is_never_cached(self):
        step = S.Step('setup', ['x'], None, '.', [], [], (0,), False, False, [], False)
        self.assertEqual(reuse.undeclared(None, step, 'a' * 40), 'not a configured step')


SHAKY = {'name': 'shaky', 'always': True, 'globs': ['src/*'], 'pass': '^ok$', 'warn': True,
         'reuse': True, 'cmd': ['{python}', '-c', 'import sys; print("ok"); sys.exit(1)']}


class Warned(ReuseCase):
    GATE = {'steps': [SHAKY]}

    def test_a_warned_step_is_never_reused(self):
        wt, _ = self.gated_branch()
        stored = self.lines('STORED')
        self.assertEqual(len(stored), 1, self.said)
        self.assertRegex(stored[0], '^%sSTORED shaky: fail, key [0-9a-f]{12}$'
                         % re.escape(reuse.PREFIX))
        self.commit(wt, {'notes.txt': 'n\n'})
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK (warned: shaky)', self.said)
        miss = self.lines('MISS')
        self.assertEqual(len(miss), 1, self.said)
        self.assertRegex(miss[0], '^%sMISS shaky: failed or warned on these inputs at %s '
                         r'\(key [0-9a-f]{12}\)$' % (re.escape(reuse.PREFIX), STAMP))
        self.assert_ran(out.log_dir, 'shaky')

    def test_a_kit_edited_while_the_gate_ran_is_not_stored(self):
        wt, _ = self.branch('feature', dict(SRC))
        real = reuse.kit_identity
        seen = []

        def kit():                  # the plan sees one kit, its keep another
            seen.append(1)
            return dict(real(), files='%064d' % len(seen))
        with mock.patch.object(reuse, 'kit_identity', side_effect=kit):
            out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len(seen), 2, self.said)
        self.assertEqual(self.lines('NOT STORED'), [
            reuse.PREFIX + 'NOT STORED shaky: its key changed while the gate ran (the kit, a '
            'program or the config)'], self.said)
        self.assertEqual(self.entries(), [])


# RED is ignored below, so it is not an input: the step fails and then passes on one key
FAILER = {'name': 'unit', 'always': True, 'globs': ['src/*'], 'cmd': fixtures.RED_AWARE,
          'pass': '^ok$', 'reuse': True}


class Failed(ReuseCase):
    GATE = {'steps': [FAILER]}

    def test_a_failed_run_blocks_a_later_pass_on_the_same_key(self):
        """FAIL is stored as fail by the runner, and the fail outlives a later pass."""
        wt, _ = self.branch('feature', dict(SRC, **{'.gitignore': '.claude/\nRED\n'}))
        self.repo.write('RED', 'x\n', root=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        stored = self.lines('STORED')
        self.assertEqual(len(stored), 1, self.said)
        found = re.match('^%sSTORED unit: fail, key ([0-9a-f]{12})$' % re.escape(reuse.PREFIX),
                         stored[0])
        self.assertIsNotNone(found, self.said)
        key = found.group(1)
        os.remove(os.path.join(wt, 'RED'))
        blocked = '^%sMISS unit: failed or warned on these inputs at %s %s$' % (
            re.escape(reuse.PREFIX), STAMP, re.escape('(key %s)' % key))
        for i in range(2):                  # the pass of the first rerun never serves the second
            out = self.gate(wt)
            self.assertEqual(out.verdict, 'GATE OK', self.said)
            self.assertEqual(len(self.lines('MISS')), 1, self.said)
            self.assertRegex(self.lines('MISS')[0], blocked)
            self.assert_ran(out.log_dir, 'unit')
            self.assertEqual(self.lines('STORED'),
                             [reuse.PREFIX + 'STORED unit: pass, key %s' % key], self.said)
            self.commit(wt, {'notes.txt': 'n%d\n' % i})
        self.assertEqual(sorted(e.split('.')[-2] for e in self.entries()),
                         ['fail', 'pass', 'pass'])
        self.assertEqual(len({e.split('/')[0] for e in self.entries()}), 1, self.entries())


SCRIPTED = {'name': 'scripted', 'always': True, 'globs': ['src/*'], 'pass': '^ok$', 'reuse': True,
            'cmd': ['{python}', 'scripts/check.py']}


class Scripted(ReuseCase):
    GATE = {'steps': [SCRIPTED]}

    def test_a_change_to_a_file_the_command_names_runs_the_step(self):
        """scripts/check.py is not under the step's globs: only the hash of the argv file in the
        key sees it change."""
        wt, _ = self.gated_branch(files=dict(SRC, **{'scripts/check.py': 'print("ok")\n'}))
        self.commit(wt, {'notes.txt': 'n\n'})
        out = self.gate(wt)                 # the positive case: nothing the key names changed
        self.assertEqual(out.verdict, 'GATE OK (reused: scripted)', self.said)
        self.commit(wt, {'scripts/check.py': 'print("ok")  # another check\n'})
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK', self.said)
        self.assertEqual(len(self.lines('MISS')), 1, self.said)
        self.assertTrue(self.lines('MISS')[0].startswith(
            reuse.PREFIX + 'MISS scripted: no result stored for key '), self.said)
        self.assert_ran(out.log_dir, 'scripted')


WRITER = {'name': 'writer', 'always': True, 'globs': ['src/*'], 'pass': '^ok$', 'reuse': True,
          'cmd': ['{python}', '-c', 'open("src/a.py", "a").write("#\\n"); print("ok")']}


class Writer(ReuseCase):
    GATE = {'steps': [WRITER]}

    def test_a_step_that_changes_its_inputs_is_not_stored(self):
        wt, _ = self.branch('feature', dict(SRC))
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.lines('NOT STORED'), [
            reuse.PREFIX + 'NOT STORED writer: its inputs changed while the gate ran'], self.said)
        self.assertEqual(self.entries(), [])


class NeverReused(ReuseCase):
    KIT = {'landing': {'reuse_never': ['docs/*']}}

    def test_a_reuse_never_path_reads_and_stores_nothing(self):
        wt, tip = self.branch('feature', dict(SRC, **{'docs/x.md': 'x\n'}))
        out = self.gate(wt)
        self.assert_none(out, out.log_dir, 'the change touches docs/x.md')
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, 'the change touches docs/x.md')
        self.assertEqual(self.entries(), [])


class MaxAgeZero(ReuseCase):
    KIT = {'landing': {'reuse_max_age_h': 0}}

    def test_max_age_zero_reads_and_stores_nothing(self):
        wt, tip = self.branch('feature', dict(SRC))
        out = self.gate(wt)
        self.assert_none(out, out.log_dir, '"landing".reuse_max_age_h is 0')
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, '"landing".reuse_max_age_h is 0')
        self.assertEqual(self.entries(), [])


class Groups(ReuseCase):
    GATE = {'setup': OK_CMD,
            'steps': [UNIT,
                      {'name': 'build', 'always': True, 'globs': ['src/*'], 'cmd': OK_CMD,
                       'pass': '^ok$', 'reuse': 'pair'},
                      {'name': 'test', 'always': True, 'globs': ['src/*'], 'cmd': OK_CMD,
                       'pass': '^ok$', 'reuse': 'pair', 'merged_args': ['--merged']}]}

    def test_a_group_is_reused_whole_or_not_at_all(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assertIn(reuse.PREFIX + 'MISS build: test of its group pair runs', self.lines())
        self.assert_ran(lander.gate_logs, 'setup', 'build', 'test')


class AllReused(ReuseCase):
    GATE = {'setup': OK_CMD, 'steps': [UNIT]}

    def test_a_rerun_with_every_step_reused_books_nothing(self):
        wt, _ = self.gated_branch()
        head = self.commit(wt, {'notes.txt': 'n\n'})
        with mock.patch.object(runner.lockrun, 'book', side_effect=AssertionError('booked')):
            out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assertEqual(sorted(os.listdir(out.log_dir)), ['reuse.log'])
        rec, why = gate_ok.verify(self.repo.root, head)
        self.assertIsNone(why)
        self.assertEqual([(s['name'], s['result']) for s in rec['steps']], [('unit', 'reused')])

    def test_nothing_to_run_books_only_the_landing_turn_and_runs_no_setup(self):
        # the landing books its one turn before the merge (docs/gate.md "The landing queue"); the
        # merged gate, with every step reused, books nothing more
        _, tip = self.gated_branch()
        del self.book_calls[:]
        lander, out = self.land(BRANCH, tip)
        self.assertEqual([c.get('priority') for c in self.book_calls], ['land'], self.book_calls)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assertEqual(sorted(os.listdir(lander.gate_logs)), ['reuse.log'])
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual([(s['name'], s['result']) for s in rec['steps']], [('unit', 'reused')])


class NoneAsked(ReuseCase):
    GATE = fixtures.GATE

    def test_no_step_asking_for_reuse_says_nothing(self):
        _, tip = self.gated_branch()
        self.assertEqual(self.lines(), [])
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.lines(), [])
        self.assertFalse(os.path.exists(os.path.join(lander.gate_logs, 'reuse.log')))
        self.assert_ran(lander.gate_logs, 'unit')
        self.assertEqual(self.entries(), [])


class NeverList(ReuseCase):
    """The config, a section file and the hook module are never reused across, whatever
    "landing".reuse_never says. Owner-only paths are narrowed so the branch may change them."""
    KIT = {'landing': {}, 'owner': {'paths': ['CLAUDE.md']}, 'hook': {'module': 'gate_hook'}}

    def setUp(self):
        ReuseCase.setUp(self)
        kit = dict(self.KIT, gate=self.GATE, forbidden='cfg/forbidden.json')
        self.repo.write('.kit.json', json.dumps(kit, indent=1) + '\n')
        self.repo.write('cfg/forbidden.json', '{}\n')
        self.main = self.repo.commit('forbidden in a section file', '.kit.json',
                                     'cfg/forbidden.json')
        self.repo.git('push', '-q', 'origin', 'main')

    def check(self, files, path):
        wt, tip = self.branch('feature', dict(SRC, **files))
        out = self.gate(wt)
        self.assert_none(out, out.log_dir, 'the change touches %s' % path)
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, 'the change touches %s' % path)
        self.assertEqual(self.entries(), [])

    def test_the_kit_json_is_never_reused_across(self):
        kit = dict(self.KIT, gate=self.GATE, forbidden='cfg/forbidden.json',
                   repo={'name': 'renamed'})                   # the gate config is unchanged
        self.check({'.kit.json': json.dumps(kit, indent=1) + '\n'}, '.kit.json')

    def test_a_section_file_is_never_reused_across(self):
        self.check({'cfg/forbidden.json': '{"x": 1}\n'}, 'cfg/forbidden.json')

    def test_the_hook_module_is_never_reused_across(self):
        self.check({'tools/gate_hook.py': 'GATE_CHECKS = {}\n'}, 'tools/gate_hook.py')


KEY = 'ab' * 32
NOW = datetime.datetime(2026, 10, 11, 12, tzinfo=datetime.timezone.utc)


def hours(n):
    return NOW - datetime.timedelta(hours=n)


class Store(unittest.TestCase):
    """The cache folder alone: entries, the hit rule, pruning."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def put(self, res, when, key=KEY):
        return reuse.store_entry(self.folder, key, 'unit', res, now=when)

    def test_a_pass_within_the_age_hits(self):
        self.put('pass', hours(1))
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(why)
        self.assertEqual(hit['stored_utc'], gate_ok.utc(hours(1)))

    def test_nothing_stored_misses(self):
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(hit)
        self.assertEqual(why, 'no result stored for key %s' % KEY[:12])

    def test_a_pass_older_than_the_limit_misses(self):
        self.put('pass', hours(30))
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(hit)
        self.assertEqual(why, 'no pass within "landing".reuse_max_age_h (24) for key %s'
                         % KEY[:12])

    def test_a_pass_from_the_future_misses(self):
        self.put('pass', NOW + datetime.timedelta(hours=1))
        self.assertIsNone(reuse.lookup(self.folder, KEY, 24, NOW)[0])

    def test_a_fail_in_the_window_is_sticky(self):
        self.put('fail', hours(2))
        self.put('pass', hours(1))
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(hit)
        self.assertEqual(why, 'failed or warned on these inputs at %s (key %s)'
                         % (gate_ok.utc(hours(2)), KEY[:12]))

    def test_a_fail_from_the_future_still_counts(self):
        self.put('fail', NOW + datetime.timedelta(hours=1))
        self.put('pass', hours(1))
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(hit)
        self.assertTrue(why.startswith('failed or warned on these inputs at '), why)

    def test_a_fail_before_the_window_is_forgotten(self):
        self.put('fail', hours(30))
        self.put('pass', hours(1))
        self.assertIsNotNone(reuse.lookup(self.folder, KEY, 24, NOW)[0])

    def test_an_unreadable_entry_misses(self):
        path = self.put('pass', hours(1))
        self.put('pass', hours(2))
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"result": "pa')
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(hit)
        self.assertIn('cannot be read', why)

    def test_an_entry_naming_another_key_misses(self):
        path = self.put('pass', hours(1), key='cd' * 32)
        os.makedirs(os.path.join(self.folder, KEY))
        os.replace(path, os.path.join(self.folder, KEY, os.path.basename(path)))
        hit, why = reuse.lookup(self.folder, KEY, 24, NOW)
        self.assertIsNone(hit)
        self.assertIn('cannot be read', why)

    def test_a_foreign_file_is_ignored(self):
        self.put('pass', hours(1))
        with open(os.path.join(self.folder, KEY, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('x')
        self.assertIsNotNone(reuse.lookup(self.folder, KEY, 24, NOW)[0])

    def test_prune_removes_old_entries_and_leaves_foreign_files(self):
        old = self.put('pass', hours(reuse.PRUNE_H + 1))
        new = self.put('pass', hours(1))
        gone_dir = os.path.dirname(self.put('pass', hours(reuse.PRUNE_H + 2), key='cd' * 32))
        foreign = os.path.join(self.folder, KEY, 'notes.txt')
        top = os.path.join(self.folder, 'README')
        for p in (foreign, top):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x')
        self.assertEqual(reuse.prune(self.folder, NOW), [])
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(gone_dir))         # emptied: removed
        for p in (new, foreign, top):
            self.assertTrue(os.path.exists(p), p)

    def temp(self, age_h, key=KEY, name='.tmp-4242-0a1b2c3d'):
        """A temp file as _write_entry names it, left by a crash before its rename, last
        written `age_h` hours before NOW."""
        os.makedirs(os.path.join(self.folder, key), exist_ok=True)
        path = os.path.join(self.folder, key, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"result": "pa')
        when = hours(age_h).timestamp()
        os.utime(path, (when, when))
        return path

    def test_the_temp_name_prune_knows_is_the_one_written(self):
        seen = []
        real = os.replace

        def replace(src, dst):
            seen.append(os.path.basename(src))
            return real(src, dst)
        with mock.patch.object(reuse.os, 'replace', replace):
            self.put('pass', hours(1))
        self.assertEqual(len(seen), 1, seen)
        self.assertRegex(seen[0], reuse.TMP_RE)

    def test_prune_removes_a_stale_temp_file_and_its_emptied_folder(self):
        alone = self.temp(reuse.PRUNE_H + 1, key='cd' * 32)
        beside = self.temp(reuse.PRUNE_H + 1)
        new = self.put('pass', hours(1))
        self.assertEqual(reuse.prune(self.folder, NOW), [])
        self.assertFalse(os.path.exists(alone))
        self.assertFalse(os.path.exists(os.path.dirname(alone)))    # nothing else kept it
        self.assertFalse(os.path.exists(beside))
        self.assertTrue(os.path.exists(new))

    def test_prune_keeps_a_fresh_temp_file(self):
        """Another gate may be between its write and its rename."""
        fresh = self.temp(1, key='cd' * 32)
        edge = self.temp(reuse.PRUNE_H - 1)
        self.assertEqual(reuse.prune(self.folder, NOW), [])
        for p in (fresh, edge):
            self.assertTrue(os.path.exists(p), p)

    def test_prune_leaves_a_name_that_only_looks_like_a_temp_file(self):
        names = ('.tmp-4242-0a1b2c3d.json', 'x.tmp-4242-0a1b2c3d', '.tmp-notes')
        kept = [self.temp(reuse.PRUNE_H + 1, name=n) for n in names]
        kept.append(os.path.join(self.folder, KEY, '.tmp-1-0a1b2c3d'))
        os.mkdir(kept[-1])                                  # a folder, not a file
        when = hours(reuse.PRUNE_H + 1).timestamp()
        os.utime(kept[-1], (when, when))
        self.assertEqual(reuse.prune(self.folder, NOW), [])
        for p in kept:
            self.assertTrue(os.path.exists(p), p)

    def test_prune_never_follows_a_junction(self):
        import _winapi
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        old = os.path.join(outside, os.path.basename(self.put('pass', hours(reuse.PRUNE_H + 1),
                                                                 key='ef' * 32)))
        os.replace(os.path.join(self.folder, 'ef' * 32, os.path.basename(old)), old)
        os.rmdir(os.path.join(self.folder, 'ef' * 32))
        link = os.path.join(self.folder, 'ef' * 32)
        _winapi.CreateJunction(outside, link)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        self.assertEqual(reuse.prune(self.folder, NOW), [])
        self.assertTrue(os.path.exists(old))

    def test_a_failed_prune_says_one_line(self):
        self.put('pass', hours(reuse.PRUNE_H + 1))
        self.put('pass', hours(reuse.PRUNE_H + 2))
        with mock.patch.object(reuse.os, 'remove', side_effect=PermissionError('in use')):
            lines = reuse.prune(self.folder, NOW)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(reuse.PREFIX + 'prune: '), lines)


REAL_IDENTITY = runner.kit_identity


class Identity(unittest.TestCase):
    def test_the_cache_identity_is_the_kit_files_and_the_interpreter(self):
        here = reuse.kit_identity()
        self.assertEqual(sorted(here), ['files', 'python', 'version'])
        self.assertRegex(here['files'], '^[0-9a-f]{64}$')
        self.assertEqual(here['python'], os.path.normcase(os.path.abspath(sys.executable)))
        other = os.path.join(os.path.dirname(sys.executable), 'other-python.exe')
        with mock.patch.object(sys, 'executable', other):
            there = reuse.kit_identity()
        self.assertEqual(there['files'], here['files'])
        self.assertNotEqual(there, here)

    def test_kit_identity_names_the_running_interpreter(self):
        here = REAL_IDENTITY()
        self.assertEqual(here['python'], os.path.normcase(os.path.abspath(sys.executable)))
        other = os.path.join(os.path.dirname(sys.executable), 'other-python.exe')
        with mock.patch.object(sys, 'executable', other):
            there = REAL_IDENTITY()
        self.assertEqual(there['python'], os.path.normcase(os.path.abspath(other)))
        self.assertEqual(there['commit'], here['commit'])
        self.assertNotEqual(there, here)


class KitFiles(unittest.TestCase):
    """kit_files_hash walks the files of a kit's kitlib/ and tools/: a temp kit, and
    reuse.kit_identity as it is (never a mock of it)."""
    FILES = {'kitlib/gate/a.py': 'a = 1\n', 'tools/t.py': 't = 1\n', 'docs/x.md': 'x\n'}

    def setUp(self):
        self.kit = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.kit, True)
        for rel, text in self.FILES.items():
            self.write(rel, text)
        p = mock.patch.object(S, 'KIT_ROOT', self.kit)
        p.start()
        self.addCleanup(p.stop)

    def write(self, rel, text):
        path = os.path.join(self.kit, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return path

    def files(self):
        return reuse.kit_identity()['files']

    def test_a_changed_file_under_kitlib_or_tools_changes_the_hash(self):
        first = self.files()
        self.assertRegex(first, '^[0-9a-f]{64}$')
        self.assertEqual(self.files(), first)
        for rel in ('kitlib/gate/a.py', 'tools/t.py'):
            with self.subTest(rel):
                self.write(rel, 'other = 2\n')
                self.assertNotEqual(self.files(), first)
                self.write(rel, self.FILES[rel])
                self.assertEqual(self.files(), first)

    def test_a_new_or_a_renamed_file_changes_the_hash(self):
        first = self.files()
        for rel in ('kitlib/new.py', 'tools/deep/new.py'):
            with self.subTest(rel):
                new = self.write(rel, '')
                self.assertNotEqual(self.files(), first)
                os.remove(new)
                self.assertEqual(self.files(), first)
        os.replace(os.path.join(self.kit, 'tools', 't.py'), os.path.join(self.kit, 'tools', 'u.py'))
        self.assertNotEqual(self.files(), first)        # the path is hashed, not only the bytes

    def test_bytecode_and_files_outside_the_two_folders_do_not(self):
        first = self.files()
        self.write('kitlib/gate/__pycache__/a.cpython-313.pyc', 'x')
        self.write('tools/t.pyc', 'x')
        self.write('docs/x.md', 'another\n')
        self.write('README.md', 'r\n')
        self.assertEqual(self.files(), first)

    def test_a_missing_folder_raises(self):
        shutil.rmtree(os.path.join(self.kit, 'tools'))
        with self.assertRaises(OSError):
            reuse.kit_identity()


class Key(ReuseCase):
    """step_key alone: each part of its material changes the key, and nothing else does. Each
    test changes one part only."""
    ARGV = [sys.executable, '-c', 'print("ok")']

    def setUp(self):
        ReuseCase.setUp(self)
        self.root = self.repo.root
        for rel, text in (('src/a.py', 'x = 1\n'), ('sub/keep.txt', 'k\n'),
                          ('scripts/check.py', 'print("ok")\n')):
            self.repo.write(rel, text)
        self.cfg = runner.kconfig.load(self.root)
        self.kit = {'files': '0' * 64, 'python': 'p', 'version': 'v'}
        self.files = reuse.list_files(self.root)

    def step(self, argv=None, cwd=None, name='unit'):
        return S.Step(name, argv or self.ARGV, None, cwd or self.root, [], [], (0,), False,
                      False, [], False)

    def key(self, step=None, cfg=None, kit=None):
        key, ins = reuse.step_key(self.root, cfg or self.cfg, UNIT, step or self.step(),
                                  self.files, kit or self.kit)
        self.assertEqual(sorted(ins), ['src/a.py'])
        return key

    def test_the_same_material_makes_the_same_key(self):
        self.assertRegex(self.key(), '^[0-9a-f]{64}$')
        self.assertEqual(self.key(), self.key())
        self.repo.write('sub/keep.txt', 'not an input\n')
        self.assertEqual(self.key(), self.key(self.step()))

    def test_the_cwd_is_in_the_key(self):
        there = self.step(cwd=os.path.join(self.root, 'sub'))
        self.assertEqual(reuse.tools_of(there, []), reuse.tools_of(self.step(), []))
        self.assertNotEqual(self.key(there), self.key())

    def test_the_program_is_in_the_key(self):
        first = self.key()
        other = os.path.join(self.root, 'scripts', 'check.py')      # any other file's bytes
        with mock.patch.object(reuse.shutil, 'which', return_value=other) as which:
            second = self.key()
        which.assert_called_once_with(sys.executable)
        self.assertNotEqual(second, first)

    def test_a_file_the_command_names_is_in_the_key(self):
        step = self.step(argv=[sys.executable, 'scripts/check.py'])
        first = self.key(step)
        self.repo.write('scripts/check.py', 'print("ok")  # another check\n')
        self.assertNotEqual(self.key(step), first)
        self.repo.write('scripts/check.py', 'print("ok")\n')
        self.assertEqual(self.key(step), first)

    def test_the_gate_config_is_in_the_key(self):
        first = self.key()
        steps = [UNIT, MERGED, dict(PLAIN, **{'pass': '^o.$'})]
        kit = dict(self.KIT, gate=dict(self.GATE, steps=steps))
        self.repo.write('.kit.json', json.dumps(kit, indent=1) + '\n')
        self.assertNotEqual(self.key(cfg=runner.kconfig.load(self.root)), first)

    def test_the_step_name_and_the_kit_are_in_the_key(self):
        first = self.key()
        self.assertNotEqual(self.key(self.step(name='other')), first)
        self.assertNotEqual(self.key(kit=dict(self.kit, files='1' * 64)), first)
        self.assertNotEqual(self.key(kit=dict(self.kit, python='q')), first)


class PriorRecords(unittest.TestCase):
    def test_an_unreadable_folder_is_none(self):
        with mock.patch.object(result, 'result_dir', side_effect=result.ResultError('x')):
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))

    def test_one_unreadable_record_is_none(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        mine = {'kind': 'gate', 'branch': BRANCH, 'tip': 'a' * 40, 'verdict': 'pass'}
        other = dict(mine, branch='kit/other', tip='b' * 40)
        for name, rec in (('mine.gate.json', mine), ('other.gate.json', other)):
            with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                json.dump(rec, f)
        with mock.patch.object(result, 'result_dir', return_value=folder):
            self.assertEqual(reuse.prior_records('.', ['a' * 40]), [mine])
            with open(os.path.join(folder, 'red.gate.json'), 'w', encoding='utf-8') as f:
                f.write('{"kind": "gate", "verdict": "fa')          # cut short: unreadable
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))


def load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Entry(unittest.TestCase):
    """--fresh reaches the landing from tools/land.py and from gate.py land."""

    def landed(self, tool, argv):
        seen = {}

        def fake(root, branch, expect_main, expect_tip, **kw):
            seen.update(kw)
            return G.Outcome(G.OK, ['LANDED fake'])
        with mock.patch.object(land, 'land', fake), mock.patch('sys.stdout'):
            self.assertEqual(tool.main(argv), G.OK)
        return seen

    def test_fresh_reaches_the_landing(self):
        tip = [BRANCH, '--expect-main', 'a' * 40, '--expect-tip', 'b' * 40]
        land_py = load_tool(os.path.join(KIT_ROOT, 'tools', 'land.py'), 'kit_land_tool_reuse')
        self.assertIs(self.landed(land_py, tip + ['--fresh'])['fresh'], True)
        self.assertIs(self.landed(land_py, tip)['fresh'], False)
        gate_py = load_tool(os.path.join(KIT_ROOT, 'tools', 'gate.py'), 'kit_gate_tool_reuse')
        self.assertIs(self.landed(gate_py, ['land'] + tip + ['--fresh'])['fresh'], True)
        self.assertIs(self.landed(gate_py, ['land'] + tip)['fresh'], False)

    def test_fresh_reaches_the_lander(self):
        with mock.patch.object(land.Lander, 'run', lambda self: self.fresh):
            self.assertIs(land.land('.', BRANCH, 'a' * 40, 'b' * 40, fresh=True), True)
            self.assertIs(land.land('.', BRANCH, 'a' * 40, 'b' * 40), False)

    def test_sweep_landed_refuses_fresh(self):
        r = subprocess.run([sys.executable, os.path.join(KIT_ROOT, 'tools', 'land.py'),
                            '--sweep-landed', '--fresh'], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL, env=fixtures.test_env())
        self.assertEqual(r.returncode, G.USAGE, (r.stdout, r.stderr))


if __name__ == '__main__':
    unittest.main()
