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
        fixtures.queue_review(self.repo.root, branch, tip)
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
    def test_an_input_change_runs(self):
        wt, _ = self.gated_branch()
        self.commit(wt, {'src/a.py': 'x = 2\n'})
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK', self.said)
        self.assertEqual(len(self.lines('MISS')), 2, self.said)
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

    def test_a_different_change_list_misses_on_the_same_inputs(self):
        """A step such as `kit_tests.py --select` picks its tests by the change without naming
        {base}: another change list on the same inputs is another key."""
        wt, _ = self.gated_branch()
        self.commit(wt, {'notes.txt': 'not an input\n'})
        out = self.gate(wt)
        self.assertEqual(out.verdict, 'GATE OK', self.said)
        self.assertEqual(len(self.lines('MISS')), 2, self.said)
        self.assert_ran(out.log_dir, 'unit', 'merged')
        again = self.gate(wt)                       # the same change again: reused
        self.assertEqual(again.verdict, 'GATE OK (reused: unit, merged)', self.said)



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
        out = self.gate(wt)         # the same key: the change list is part of it (step_key)
        self.assertEqual(out.verdict, 'GATE OK (warned: shaky)', self.said)
        miss = self.lines('MISS')
        self.assertEqual(len(miss), 1, self.said)
        self.assertRegex(miss[0], '^%sMISS shaky: failed or warned on these inputs at %s '
                         r'\(key [0-9a-f]{12}\)$' % (re.escape(reuse.PREFIX), STAMP))
        self.assert_ran(out.log_dir, 'shaky')


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


BASEY = {'name': 'basey', 'always': True, 'globs': ['src/*'], 'pass': '^ok$', 'reuse': True,
         'cmd': ['{python}', '-c', 'print("ok")', '{base}']}
NOT_USED = 'SAME TREE %s: the --full record of ' + BRANCH + ' at %s is not used: %s'


class SameTree(ReuseCase):
    """A landing whose merge holds the very tree its tip passed gate.py --full on takes the steps
    the cache does not serve from the tip's gate-OK record (docs/gate.md "Gate reuse", "The same
    tree"): only a one-branch, non-strict landing, and only the record of the same base, gate
    config and kit, within "landing".reuse_max_age_h."""
    GATE = {'steps': [UNIT, BASEY, WHOLE, MERGED, PLAIN]}
    ASKING = ('unit', 'basey', 'whole', 'merged')

    def setUp(self):
        ReuseCase.setUp(self)
        # the same tree alone: its records carry no input keys (the input tree: class InputTree)
        p = mock.patch.object(reuse, 'record_keys', return_value=(None, None))
        p.start()
        self.addCleanup(p.stop)

    def full_tip(self):
        wt, tip = self.branch('feature', dict(SRC))
        out = self.gate(wt, full=True)
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = gate_ok.verify_full(self.repo.root, tip)
        self.assertIsNone(why)
        self.assertEqual(gate_ok.mode_of(rec), 'full')
        return wt, tip

    def tree(self, rev):
        return self.repo.git('rev-parse', rev + '^{tree}')

    def retouch(self, tip, **changes):
        rec = gate_ok.read(self.repo.root, tip)[0]
        rec.update(changes)
        gate_ok.write(self.repo.root, rec)
        return rec

    def same_lines(self):
        return [l for l in self.lines() if l.startswith(reuse.PREFIX + 'SAME TREE ')]

    def assert_unused(self, out, lander, tip, why, reused=None):
        """The trees match, the record serves nothing for `why`, and the steps the record would
        have served ran."""
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.same_lines(), [reuse.PREFIX + NOT_USED % (
            self.tree(tip)[:10], tip[:10], why)], self.said)
        self.assertEqual(out.verdict, 'GATE OK' + (' (reused: %s)' % reused if reused else ''))
        self.assert_ran(lander.gate_logs, *[n for n in ('basey', 'whole') if n != reused])

    def test_a_same_tree_landing_takes_steps_from_the_tip_full_record(self):
        _, tip = self.full_tip()
        ended = gate_ok.read(self.repo.root, tip)[0]['ended_utc']
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.tree(out.merge), self.tree(tip))
        self.assertEqual(out.verdict, 'GATE OK (reused: unit, basey, whole)')
        self.assert_not_ran(lander.gate_logs, 'unit', 'basey', 'whole')
        self.assert_ran(lander.gate_logs, 'merged', 'plain')         # merged_args; no reuse
        tree = self.tree(tip)[:10]
        self.assertEqual(self.same_lines(), [
            reuse.PREFIX + 'SAME TREE %s: %s at %s passed gate.py --full on this tree (record '
            'ended %s)' % (tree, BRANCH, tip[:10], ended)], self.said)
        for n in ('unit', 'basey', 'whole'):
            self.assertIn(reuse.PREFIX + 'HIT %s: the --full record of %s (same tree %s, ended %s)'
                          % (n, tip[:10], tree, ended), self.lines())
            self.assertIn('REUSED %s: passed in the --full gate of %s on the same tree (ended %s)'
                          % (n, tip[:10], ended), self.lines())
        # nothing the record served is stored: the relaxation never reaches the cache
        stored = self.lines('STORED')
        self.assertEqual(len(stored), 1, self.said)
        self.assertTrue(stored[0].startswith(reuse.PREFIX + 'STORED merged: pass, key '), stored)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual({s['name']: s['result'] for s in rec['steps']},
                         {'unit': 'reused', 'basey': 'reused', 'whole': 'reused',
                          'merged': 'pass', 'plain': 'pass'})
        self.assertIs(rec['full'], False)
        # the same tree as the merge of the tip onto another base is not the same landing
        cfg = runner.kconfig.load(self.repo.root)
        self.assertEqual(reuse.same_tree(self.repo.root, cfg, BRANCH, tip, 'd' * 40, out.merge),
                         (None, NOT_USED % (tree, tip[:10], 'the merge %s is not the merge of %s '
                                            'onto dddddddddd' % (out.merge[:10], tip[:10]))))

    def test_main_moved_runs_every_step_and_relaxes_nothing(self):
        _, tip = self.full_tip()
        other = self.repo.worktree('other')
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertNotEqual(self.tree(out.merge), self.tree(tip))
        self.assertEqual(out.verdict, 'GATE OK')
        self.assertEqual(self.same_lines(), [], self.said)
        self.assertIn(reuse.PREFIX + 'NONE basey: its command names {base}: it reads git history',
                      self.lines())
        self.assertIn(reuse.PREFIX + 'NONE whole: no declared inputs (no "globs" and no '
                      '"content".globs)', self.lines())
        self.assert_ran(lander.gate_logs, 'unit', 'basey', 'whole', 'merged', 'plain')
        cfg = runner.kconfig.load(self.repo.root)
        self.assertEqual(reuse.same_tree(self.repo.root, cfg, BRANCH, tip, moved, out.merge),
                         (None, None))

    def test_the_relaxation_is_for_the_same_tree_only(self):
        cfg = runner.kconfig.load(self.repo.root)
        step = S.Step('basey', [sys.executable, '-c', 'print("ok")', 'b' * 40], None,
                      self.repo.root, [], [], (0,), False, False, [], False)
        self.assertEqual(reuse.undeclared(BASEY, step, 'b' * 40),
                         'its command names {base}: it reads git history')
        self.assertIsNone(reuse.undeclared(BASEY, step, 'b' * 40, same_tree=True))
        self.assertEqual(reuse.undeclared(WHOLE, step, None),
                         'no declared inputs (no "globs" and no "content".globs)')
        self.assertIsNone(reuse.undeclared(WHOLE, step, None, same_tree=True))
        self.assertIsNone(reuse._from_record(None, BASEY, step))         # no same tree: nothing
        with self.assertRaises(reuse.NoInputs):
            reuse.step_key(self.repo.root, cfg, WHOLE, step, ['src/a.py'], {'files': '0' * 64})
        check = S.Step('pins', None, 'pins', '.', [], [], (0,), False, False, [], False)
        same = reuse.SameTree({'steps': [{'name': 'pins', 'result': 'pass'}],
                               'ended_utc': '2026-10-11T12:00:00Z'}, 'a' * 40, 'c' * 40)
        self.assertEqual(reuse.undeclared({'name': 'pins', 'check': 'pins', 'reuse': True}, check,
                                          None, same_tree=True), 'a check step')
        self.assertIsNone(reuse._from_record(same, {'name': 'pins', 'check': 'pins',
                                                    'reuse': True}, check))

    def test_a_kit_change_since_the_tip_gate_serves_nothing(self):
        _, tip = self.full_tip()
        self.ident['commit'] = 'b' * 40
        lander, out = self.land(BRANCH, tip)
        self.assert_unused(out, lander, tip, 'the record was made by another kit (aaaaaaaaaa)')
        self.assert_ran(lander.gate_logs, 'unit')

    def unclean(self, record, landing):
        """The tip gated --full by a kit whose clean is `record`, landed by one whose clean is
        `landing`: the record serves nothing and every step runs."""
        self.ident['clean'] = record
        _, tip = self.full_tip()
        self.assertIs(gate_ok.read(self.repo.root, tip)[0]['kit']['clean'], record)
        self.ident['clean'] = landing
        lander, out = self.land(BRANCH, tip)
        self.assert_unused(out, lander, tip, "the record's kit and the landing's kit are the same "
                           'commit but differ in uncommitted state (record clean=%s, landing '
                           'clean=%s)' % (record, landing))
        self.assert_ran(lander.gate_logs, 'unit')

    def test_a_kit_with_uncommitted_files_serves_nothing(self):
        self.unclean(False, False)

    def test_a_kit_git_cannot_read_serves_nothing(self):
        self.unclean(None, None)

    def test_a_record_of_an_unclean_kit_serves_a_clean_one_nothing(self):
        self.unclean(False, True)

    def test_a_record_of_a_clean_kit_serves_an_unclean_one_nothing(self):
        self.unclean(True, None)

    def test_an_old_record_serves_nothing(self):
        _, tip = self.full_tip()
        old = gate_ok.utc(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25))
        self.retouch(tip, ended_utc=old)
        lander, out = self.land(BRANCH, tip)
        self.assert_unused(out, lander, tip, 'the record ended %s, not within "landing".'
                           'reuse_max_age_h (24)' % old)

    def test_a_record_of_another_base_serves_nothing(self):
        _, tip = self.full_tip()
        self.retouch(tip, base='c' * 40)
        lander, out = self.land(BRANCH, tip)
        self.assert_unused(out, lander, tip, 'the record gated against cccccccccc, not %s'
                           % self.main[:10])

    def test_a_record_of_another_gate_config_serves_nothing(self):
        _, tip = self.full_tip()
        self.retouch(tip, gate_config='0' * 64)
        lander, out = self.land(BRANCH, tip)
        self.assert_unused(out, lander, tip, 'the record was made with another gate config')

    def test_a_select_record_serves_nothing(self):
        # the landing fixture marks the select record full; its mode still says select
        wt, tip = self.branch('feature', dict(SRC))
        self.assertEqual(self.gate(wt).code, G.OK)
        lander, out = self.land(BRANCH, tip)
        self.assert_unused(out, lander, tip, 'the record is not a --full record (mode None)',
                           reused='unit')                            # the cache's hit stays

    def test_a_step_the_record_warned_runs(self):
        _, tip = self.full_tip()
        rec = gate_ok.read(self.repo.root, tip)[0]
        self.retouch(tip, steps=[dict(s, result='warn') if s['name'] == 'unit' else s
                                 for s in rec['steps']])
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.verdict, 'GATE OK (reused: basey, whole)')
        self.assert_ran(lander.gate_logs, 'unit')
        self.assertEqual(len(self.lines('MISS')), 2, self.said)      # unit and merged

    def test_an_error_while_reading_the_record_runs_every_step(self):
        _, tip = self.gated_branch()            # the cache holds unit: the error drops it too
        with mock.patch.object(reuse, 'same_tree', side_effect=RuntimeError('boom')):
            lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander.gate_logs, 'RuntimeError: boom')

    def test_a_batch_reads_no_record(self):
        _, tip = self.full_tip()
        real = reuse.planner
        with mock.patch.object(reuse, 'same_tree', wraps=reuse.same_tree) as read, \
                mock.patch.object(reuse, 'planner',
                                  side_effect=lambda *a, **k: real(*a, **dict(k, batch=2))):
            lander, out = self.land(BRANCH, tip)
        read.assert_not_called()
        self.assertEqual(out.verdict, 'GATE OK')
        self.assert_ran(lander.gate_logs, 'unit', 'basey', 'whole')

    def test_a_strict_tip_reads_no_record(self):
        path = reuse.STRICT_DEFAULTS[0].replace('**', 'strict.py')
        wt, tip = self.branch('feature', dict(SRC, **{path: 'x = 1\n'}))
        self.assertEqual(self.gate(wt, full=True).code, G.OK)
        self.assertIsNotNone(gate_ok.verify_full(self.repo.root, tip)[0])
        common, _ = land.repo.common_dir(self.repo.root)
        verdicts.add(common, {'branch': BRANCH, 'tip': tip, 'verdict': 'LAND', 'risk': 'High',
                              'reviewer': 'pm-kit:reviewer', 'reviewer_model': 'opus',
                              'reviewer_id': 'agent-r1', 'author': 'Kit Test / strong-worker',
                              'range': '%s..%s' % (self.main[:10], tip), 'session': 's',
                              'records': []})
        with mock.patch.object(reuse, 'same_tree', wraps=reuse.same_tree) as read:
            lander, out = self.land(BRANCH, tip)
        read.assert_not_called()
        self.assert_none(out, lander.gate_logs, '--fresh')


# full_cmd differs from cmd: a --full record ran another argv than a landing runs
FULLY = {'name': 'fully', 'always': True, 'globs': ['src/*'], 'pass': '^ok$', 'reuse': True,
         'cmd': OK_CMD, 'full_cmd': ['{python}', '-c', 'print("ok")', '--all']}
# a group whose other member declares no input: the input tree serves gu, never gw
GU = {'name': 'gu', 'always': True, 'globs': ['src/*'], 'cmd': OK_CMD, 'pass': '^ok$',
      'reuse': 'pair'}
GW = {'name': 'gw', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$', 'reuse': 'pair'}


class InputTree(ReuseCase):
    """A one-branch landing whose main moved since the tip's gate takes a step from the tip's
    gate-OK record when every part of the step's input key is the one the record holds: its argv
    (the base written <base>), tools, its inputs at the tip and the merge and at both bases, the
    changed paths and the kit (docs/gate.md "Gate reuse", "The input tree"). A step that declares
    no input, a batch, a docs-only record, a result that was not a pass, or a record without input
    keys serves nothing."""
    GATE = {'steps': [UNIT, BASEY, WHOLE, FULLY, GU, GW, MERGED, PLAIN]}
    KIT = {'landing': {'shared_paths': ['shared/*']}}       # a default-mode record lands
    ASKING = ('unit', 'basey', 'whole', 'fully', 'gu', 'gw', 'merged')

    def full_tip(self):
        wt, tip = self.branch('feature', dict(SRC))
        out = self.gate(wt, full=True)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(gate_ok.mode_of(gate_ok.verify_full(self.repo.root, tip)[0]), 'full')
        return wt, tip

    def move_main(self, files):
        other = self.repo.worktree('other')
        for rel, text in files.items():
            self.repo.write(rel, text, root=other)
        moved = self.repo.commit('main moves', *files, cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        return moved

    def retouch(self, tip, step, **parts):
        rec = gate_ok.read(self.repo.root, tip)[0]
        rec['input_keys']['steps'][step].update(parts)
        gate_ok.write(self.repo.root, rec)
        return rec

    def land_as_is(self, tip):
        """A landing of `tip` with the record its branch gate wrote (never marked --full)."""
        self.said[:] = []
        fixtures.queue_review(self.repo.root, BRANCH, tip)
        lander = land.Lander(self.repo.root, BRANCH, None, tip, say=self.said.append,
                             git_env=self.env, book_kw=self.book_kw, log_dir=self.logs)
        return lander, lander.run()

    def tree_lines(self):
        return [l for l in self.lines() if l.startswith(reuse.PREFIX + 'INPUT TREE:')]

    def line_of(self, word, name):
        found = [l for l in self.lines(word) if l.startswith(reuse.PREFIX + '%s %s: ' % (word, name))]
        self.assertEqual(len(found), 1, self.said)
        return found[0]

    def assert_served(self, tip, mode, names):
        rec = gate_ok.read(self.repo.root, tip)[0]
        ended = rec['ended_utc']
        self.assertEqual(self.tree_lines(), [
            reuse.PREFIX + 'INPUT TREE: the %s record of %s at %s (base %s, ended %s) serves a '
            'step whose key parts all match' % (mode, BRANCH, tip[:10], rec['base'][:10], ended)],
            self.said)
        for n in names:
            self.assertIn(reuse.PREFIX + 'HIT %s: the input tree of the %s record of %s (ended %s)'
                          % (n, mode, tip[:10], ended), self.lines())
            self.assertIn('REUSED %s: passed in the %s gate of %s on the same inputs, base side '
                          'and change (ended %s)' % (n, mode, tip[:10], ended), self.lines())

    def test_the_record_holds_a_key_for_each_step_it_can_serve(self):
        _, tip = self.full_tip()
        keys = gate_ok.read(self.repo.root, tip)[0]['input_keys']
        self.assertEqual(keys['schema'], reuse.INPUT_SCHEMA)
        # whole and gw declare no input, merged has merged_args, plain asks for no reuse
        self.assertEqual(sorted(keys['steps']), ['basey', 'fully', 'gu', 'unit'])
        for parts in keys['steps'].values():
            self.assertEqual(sorted(parts), sorted(reuse.INPUT_PARTS))
            for digest in parts.values():
                self.assertRegex(digest, '^[0-9a-f]{64}$')

    def test_main_moved_on_no_input_takes_steps_from_the_tip_record(self):
        _, tip = self.full_tip()
        moved = self.move_main({'o.py': 'o\n'})
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(lander.expect_main, moved)
        self.assertEqual(lander.tip_records[tip]['commit'], tip)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit, basey)')
        self.assert_served(tip, 'full', ('unit', 'basey'))
        self.assert_not_ran(lander.gate_logs, 'unit', 'basey')
        self.assert_ran(lander.gate_logs, 'whole', 'fully', 'gu', 'gw', 'merged', 'plain')
        # the --full record ran fully's full_cmd: a landing runs its cmd
        self.assertTrue(self.line_of('MISS', 'fully').endswith('; input tree: argv differs'),
                        self.said)
        self.assertEqual(self.line_of('NONE', 'whole'), reuse.PREFIX + 'NONE whole: no declared '
                         'inputs (no "globs" and no "content".globs)')
        # nothing the record served is stored
        self.assertFalse([l for l in self.lines('STORED') if ' unit:' in l or ' basey:' in l],
                         self.said)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual({s['name']: s['result'] for s in rec['steps']}['basey'], 'reused')

    def test_a_group_with_a_member_the_record_cannot_serve_runs_whole(self):
        _, tip = self.full_tip()
        self.move_main({'o.py': 'o\n'})
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.line_of('MISS', 'gu'),
                         reuse.PREFIX + 'MISS gu: gw of its group pair runs')
        self.assert_ran(lander.gate_logs, 'gu', 'gw')

    def test_main_adding_an_input_runs_the_steps(self):
        _, tip = self.full_tip()
        self.move_main({'src/b.py': 'b = 1\n'})
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK')
        self.assert_ran(lander.gate_logs, 'unit', 'basey')
        self.assertTrue(self.line_of('NONE', 'basey').endswith(
            '; input tree: head_inputs, base_inputs differ'), self.said)
        self.assertTrue(self.line_of('MISS', 'unit').endswith(
            '; input tree: head_inputs, base_inputs differ'), self.said)

    def test_each_part_that_differs_is_named(self):
        _, tip = self.full_tip()
        self.move_main({'o.py': 'o\n'})
        self.retouch(tip, 'unit', head_inputs='0' * 64)
        self.retouch(tip, 'basey', base_inputs='0' * 64, kit='1' * 64)
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.verdict, 'GATE OK')
        self.assert_ran(lander.gate_logs, 'unit', 'basey')
        self.assertTrue(self.line_of('MISS', 'unit').endswith('; input tree: head_inputs differs'),
                        self.said)
        self.assertTrue(self.line_of('NONE', 'basey').endswith(
            '; input tree: base_inputs, kit differ'), self.said)

    def test_a_step_the_record_did_not_pass_runs(self):
        _, tip = self.full_tip()
        self.move_main({'o.py': 'o\n'})
        rec = gate_ok.read(self.repo.root, tip)[0]
        rec['steps'] = [dict(s, result='reused') if s['name'] == 'basey' else s
                        for s in rec['steps']]
        gate_ok.write(self.repo.root, rec)
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assert_ran(lander.gate_logs, 'basey')
        self.assertTrue(self.line_of('NONE', 'basey').endswith(
            '; input tree: the record says reused'), self.said)

    def test_a_record_without_input_keys_serves_nothing(self):
        _, tip = self.full_tip()
        rec = gate_ok.read(self.repo.root, tip)[0]
        self.assertIn('basey', rec['input_keys']['steps'])
        del rec['input_keys']                       # an older kit's record
        gate_ok.write(self.repo.root, rec)
        self.move_main({'o.py': 'o\n'})
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.verdict, 'GATE OK')
        self.assertEqual(self.tree_lines(), [], self.said)
        self.assert_ran(lander.gate_logs, 'unit', 'basey')

    def test_a_batch_reads_no_input_tree(self):
        _, tip = self.full_tip()
        self.move_main({'o.py': 'o\n'})
        real = reuse.planner
        with mock.patch.object(reuse, 'input_tree', wraps=reuse.input_tree) as read, \
                mock.patch.object(reuse, 'planner',
                                  side_effect=lambda *a, **k: real(*a, **dict(k, batch=2))):
            lander, out = self.land(BRANCH, tip)
        read.assert_not_called()
        self.assertEqual(out.verdict, 'GATE OK')
        self.assert_ran(lander.gate_logs, 'unit', 'basey')

    def test_a_default_mode_record_serves(self):
        wt, tip = self.branch('feature', dict(SRC))
        self.assertEqual(self.gate(wt).code, G.OK)
        self.assertEqual(gate_ok.mode_of(gate_ok.read(self.repo.root, tip)[0]), 'select')
        self.move_main({'o.py': 'o\n'})
        lander, out = self.land_as_is(tip)
        self.assertEqual(out.code, G.OK, out.lines)
        # unit and fully hit the cache the branch gate stored; basey, which names {base} and is
        # never stored, comes from the record
        self.assertEqual(out.verdict, 'GATE OK (reused: unit, basey, fully)')
        self.assert_served(tip, 'select', ('basey',))
        self.assert_not_ran(lander.gate_logs, 'unit', 'basey', 'fully')

    def test_the_record_must_be_one_the_landing_takes(self):
        _, tip = self.full_tip()
        moved = self.move_main({'o.py': 'o\n'})
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        cfg = runner.kconfig.load(self.repo.root)
        rec = gate_ok.read(self.repo.root, tip)[0]

        def why(merge=out.merge, base=moved, **changes):
            got, line = reuse.input_tree(self.repo.root, cfg, BRANCH, tip, dict(rec, **changes),
                                         base, merge)
            self.assertIsNone(got)
            return line

        head = reuse.PREFIX[:0] + 'INPUT TREE: the %s record of ' + BRANCH + ' at ' + tip[:10] \
            + ' is not used: %s'
        self.assertEqual(why(mode='docs', full=False), head % (
            'docs', 'only a --full or default-mode record serves (mode docs)'))
        self.assertEqual(why(gate_config='0' * 64), head % (
            'full', 'config: the record was made with another gate config'))
        self.assertEqual(why(base='d' * 40), head % (
            'full', 'parents: the merge %s is not the merge of %s onto dddddddddd'
            % (out.merge[:10], tip[:10])))
        old = gate_ok.utc(datetime.datetime.now(datetime.timezone.utc)
                          - datetime.timedelta(hours=25))
        self.assertEqual(why(ended_utc=old), head % (
            'full', 'age: the record ended %s, not within "landing".reuse_max_age_h (24)' % old))
        self.assertEqual(reuse.input_tree(self.repo.root, cfg, BRANCH, tip, None, moved,
                                          out.merge), (None, None))      # a FLAKE-only tip
        self.assertEqual(reuse.input_tree(self.repo.root, cfg, BRANCH, tip, dict(
            rec, commit='e' * 40), moved, out.merge), (None, None))      # another commit's

    def test_the_bars_are_relaxed_for_base_only(self):
        step = S.Step('basey', [sys.executable, '-c', 'print("ok")', 'b' * 40], None,
                      self.repo.root, [], [], (0,), False, False, [], False)
        self.assertIsNone(reuse.undeclared(BASEY, step, 'b' * 40, input_tree=True))
        self.assertEqual(reuse.undeclared(WHOLE, step, None, input_tree=True),
                         'no declared inputs (no "globs" and no "content".globs)')
        check = S.Step('pins', None, 'pins', '.', [], [], (0,), False, False, [], False)
        self.assertEqual(reuse.undeclared({'name': 'pins', 'check': 'pins', 'reuse': True}, check,
                                          None, input_tree=True), 'a check step')


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

    def key(self, step=None, cfg=None, kit=None, changed=('src/a.py',)):
        key, ins = reuse.step_key(self.root, cfg or self.cfg, UNIT, step or self.step(),
                                  self.files, kit or self.kit, changed)
        self.assertEqual(sorted(ins), ['src/a.py'])
        return key

    def test_the_change_list_is_in_the_key_sorted_and_spelled_alike(self):
        first = self.key()
        self.assertNotEqual(self.key(changed=('src/a.py', 'notes.txt')), first)
        self.assertNotEqual(self.key(changed=()), first)
        self.assertEqual(self.key(changed=['notes.txt', 'src/a.py']),
                         self.key(changed=('src\\a.py', 'notes.txt', 'notes.txt')))

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
        with mock.patch.object(result, 'record_dirs', side_effect=result.ResultError('x')):
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))

    def folder(self, *recs):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        for name, rec in recs:
            with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                json.dump(rec, f)
        return folder

    def test_one_unreadable_record_is_none(self):
        mine = {'kind': 'gate', 'branch': BRANCH, 'tip': 'a' * 40, 'verdict': 'pass'}
        other = dict(mine, branch='kit/other', tip='b' * 40)
        folder = self.folder(('mine.gate.json', mine), ('other.gate.json', other))
        with mock.patch.object(result, 'record_dirs', return_value=[folder]):
            self.assertEqual(reuse.prior_records('.', ['a' * 40]), [mine])
            with open(os.path.join(folder, 'red.gate.json'), 'w', encoding='utf-8') as f:
                f.write('{"kind": "gate", "verdict": "fa')          # cut short: unreadable
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))

    def test_the_old_folders_records_count_too(self):
        """An older kit's red land record of the tip, in the old folder (<git common
        dir>/kit-gate-result), is a prior record as one in the result folder is; one that cannot
        be read there is reuse of nothing too; a missing old folder is no records."""
        mine = {'kind': 'gate', 'branch': BRANCH, 'tip': 'a' * 40, 'verdict': 'pass'}
        red = dict(mine, kind='land', verdict='fail')
        new = self.folder(('mine.gate.json', mine))
        old = self.folder(('mine.land.json', red))
        with mock.patch.object(result, 'record_dirs', return_value=[new, old]):
            self.assertEqual(reuse.prior_records('.', ['a' * 40]), [mine, red])
            with open(os.path.join(old, 'cut.gate.json'), 'w', encoding='utf-8') as f:
                f.write('{"kind": "gate", "verdict": "fa')
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))
        with mock.patch.object(result, 'record_dirs',
                               return_value=[new, os.path.join(old, 'missing')]):
            self.assertEqual(reuse.prior_records('.', ['a' * 40]), [mine])


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
