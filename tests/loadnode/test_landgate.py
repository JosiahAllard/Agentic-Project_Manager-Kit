"""kitlib.loadnode.landgate: a landing's merged gate on a load node. The node's side runs the merged
gate only with main's kit files and the tree at the merge, and writes the merge's record with its
base as config_rev; main's checker takes a node's record only when every field is what main's own
git, config and step selection say, each refused field named by its own check. Real throwaway
repositories; the merged gate itself is a stand-in (tests/gate covers it)."""
import os
import unittest
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.gate import runner
from kitlib.loadnode import box, landgate
from kitlib.loadnode.job import RecordRefused
from kitlib.records import gate_ok
from tests import gitfixture
from tests.home import result_drive

KITH = 'e' * 64
STEPS = [{'name': 'unit', 'result': 'pass', 'seconds': 1.5},
         {'name': 'lint', 'result': 'warn', 'seconds': 0.5}]


class Repo(unittest.TestCase):

    def setUp(self):
        self.drive = result_drive(self)     # the record folder is under its dev_drive
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.base = self.repo.initial
        self.repo.write('a.py', 'x = 1\n')
        self.merge = self.repo.commit('the merge', 'a.py')
        self.cfg = kconfig.load_at(self.repo.root, self.base)
        self.gate_config = runner.gate_config(self.cfg)


class CheckRecord(Repo):

    def setUp(self):
        Repo.setUp(self)
        self.expected = {'head': self.merge, 'steps': ['unit', 'lint'], 'mode': 'full',
                         'full': True, 'source': 'gate.json',
                         'gate_config': gate_ok.config_hash(self.gate_config)}

    def rec(self, **over):
        rec = gate_ok.build(self.repo.root, self.merge, self.gate_config,
                            {'commit': 'c' * 40, 'files': KITH}, 'kit-test', '2026-10-29T10:00:00Z',
                            STEPS, base=self.base, config_rev=self.base, source='gate.json',
                            full=True)
        rec.update(over)
        return rec

    def check(self, rec, **kw):
        args = dict(root=self.repo.root, rec=rec, merge=self.merge, base=self.base,
                    expected=self.expected, repo_name='kit-test', kit_hash=KITH,
                    gate_config=self.gate_config)
        args.update(kw)
        return landgate.check_record(**args)

    def refused(self, text, rec=None, **kw):
        with self.assertRaises(RecordRefused) as cm:
            self.check(self.rec() if rec is None else rec, **kw)
        self.assertIn(text, str(cm.exception))

    def test_a_record_main_agrees_with_is_taken(self):
        rec = self.rec()
        self.assertIs(self.check(rec), rec)

    def test_a_docs_and_a_select_record_are_taken_when_main_would_run_those(self):
        for mode in ('docs', 'select'):
            with self.subTest(mode=mode):
                self.expected.update(mode=mode, full=False)
                rec = self.rec(mode=mode, full=False)
                self.assertIs(self.check(rec), rec)

    def test_no_record(self):
        self.refused('the node sent no gate-OK record', rec=[])

    def test_another_commit(self):
        self.refused('not the merge', rec=self.rec(commit=self.base))

    def test_a_selection_read_at_another_head(self):
        self.expected['head'] = self.base
        self.refused("main's selection was read at")

    def test_another_tree(self):
        self.refused('names tree', rec=self.rec(tree='0' * 40))

    def test_another_author(self):
        self.refused('names author', rec=self.rec(author='Someone <x@example.invalid>'))

    def test_another_schema_or_verdict(self):
        self.refused('is schema', rec=self.rec(schema=2))
        self.refused('not OK', rec=self.rec(verdict='FAILED'))

    def test_a_missing_key(self):
        rec = self.rec()
        del rec['kit']
        self.refused('lacks kit', rec=rec)

    def test_another_gate_config(self):
        self.refused('another gate config', rec=self.rec(gate_config='0' * 64))

    def test_a_selection_of_another_gate_config(self):
        self.expected['gate_config'] = '1' * 64
        self.refused('another gate config than main reads at the base')

    def test_another_base(self):
        self.refused('names base', rec=self.rec(base=self.merge))

    def test_another_config_rev(self):
        self.refused('names config_rev', rec=self.rec(config_rev=self.merge))
        self.refused('names config_rev', rec=self.rec(config_rev=None))

    def test_another_mode(self):
        self.refused('main would run a full gate', rec=self.rec(mode='select', full=False))
        self.refused('main would run a full gate', rec=self.rec(mode='full', full=False))
        self.expected.update(mode='select', full=False)
        self.refused('main would run a select gate', rec=self.rec())

    def test_another_source(self):
        self.refused("main reads 'gate.json'", rec=self.rec(source='auto'))

    def test_another_set_of_steps(self):
        self.refused('main would run unit, lint', rec=self.rec(steps=STEPS[:1]))
        self.refused('main would run unit, lint',
                     rec=self.rec(steps=STEPS + [{'name': 'more', 'result': 'pass'}]))
        self.refused('main would run unit, lint',
                     rec=self.rec(steps=[STEPS[0], dict(STEPS[0])]))
        self.refused('no list of steps', rec=self.rec(steps={'unit': 'pass'}))
        self.refused('no list of steps', rec=self.rec(steps=['unit', 'lint']))

    def test_a_step_that_did_not_run_and_pass(self):
        for word in ('skip', 'reused', 'fail', None):
            with self.subTest(result=word):
                steps = [STEPS[0], dict(STEPS[1], result=word)]
                self.refused('steps that did not run and pass on the node: lint (%s)' % word,
                             rec=self.rec(steps=steps))

    def test_another_repository(self):
        self.refused("is of repository 'kit-test', not 'other'", repo_name='other')

    def test_another_kit(self):
        self.refused("kit files are not main's", kit_hash='f' * 64)
        self.refused("kit files are not main's", rec=self.rec(kit={'commit': 'c' * 40}))
        self.refused("kit files are not main's", rec=self.rec(kit='c' * 40))


class Expect(Repo):

    def expect(self, info, full, docs):
        with mock.patch.object(runner, 'merged_steps', return_value=(info, None)) as ms:
            got, why = landgate.expect(self.repo.root, self.cfg, self.base, full, docs)
        self.assertIsNone(why)
        self.assertEqual(ms.call_args[1], {'full': full, 'tree': None, 'docs': docs})
        return got

    def info(self, docs):
        return {'head': self.merge, 'steps': ['unit'], 'docs': docs, 'source': 'auto'}

    def test_the_mode_is_the_one_the_landing_would_record(self):
        self.assertEqual(self.expect(self.info(False), True, False)['mode'], 'full')
        self.assertEqual(self.expect(self.info(False), False, True)['mode'], 'select')
        got = self.expect(self.info(True), False, True)
        self.assertEqual((got['mode'], got['full']), ('docs', False))

    def test_it_carries_the_steps_source_and_gate_config_hash(self):
        got = self.expect(self.info(False), False, False)
        self.assertEqual((got['head'], got['steps'], got['source'], got['gate_config']),
                         (self.merge, ['unit'], 'auto', gate_ok.config_hash(self.gate_config)))

    def test_a_merge_main_would_refuse_is_refused_by_the_checker(self):
        with mock.patch.object(runner, 'merged_steps', return_value=(None, 'no step')):
            with self.assertRaises(RecordRefused) as cm:
                landgate.checker(self.repo.root, self.merge, self.base, self.cfg, 'kit-test',
                                 False, False, kit_hash=KITH)
        self.assertIn("main's own merged gate would refuse the merge: no step", str(cm.exception))

    def test_a_landing_tree_at_another_head_is_refused_by_the_checker(self):
        info = dict(self.info(False), head=self.base)
        with mock.patch.object(runner, 'merged_steps', return_value=(info, None)):
            with self.assertRaises(RecordRefused) as cm:
                landgate.checker(self.repo.root, self.merge, self.base, self.cfg, 'kit-test',
                                 False, False, kit_hash=KITH)
        self.assertIn('the landing tree is at', str(cm.exception))


class LandGate(Repo):
    """The node's side, with a stand-in merged gate."""

    def setUp(self):
        Repo.setUp(self)
        self.said, self.calls = [], []
        self.outcome = G.Outcome(G.OK, ['GATE OK (warned: lint)'], results=[
            {'name': 'unit', 'status': 'PASS', 'seconds': 1.5},
            {'name': 'lint', 'status': 'WARN', 'seconds': 0.5}], source='gate.json', docs=False)
        self.spec = {'sha': self.merge, 'base': self.base, 'full': True, 'docs': False,
                     'kit_files': KITH, 'slug': 'my-slug'}

    def merged_gate(self, root, cfg, base, **kw):
        self.calls.append((root, base, kw))
        return self.outcome

    def land(self, kit=KITH, merged_gate=None):
        return landgate.land_gate(self.repo.root, self.spec, self.said.append,
                                  merged_gate=merged_gate or self.merged_gate,
                                  kit_hash=lambda: kit)

    def test_a_pass_writes_the_merges_record_with_the_base_as_config_rev_and_the_kit_files(self):
        self.assertEqual(self.land(), 0, self.said)
        root, base, kw = self.calls[0]
        self.assertEqual((root, base, kw['full'], kw['docs'], kw['tree'], kw['slug']),
                         (self.repo.root, self.base, True, False, self.repo.root, 'my-slug'))
        rec, why = landgate.read_record(self.repo.root, self.merge)
        self.assertIsNone(why)
        self.assertEqual((rec['commit'], rec['base'], rec['config_rev'], rec['source']),
                         (self.merge, self.base, self.base, 'gate.json'))
        self.assertEqual((gate_ok.mode_of(rec), rec['kit']['files']), ('full', KITH))
        self.assertEqual(rec['steps'], [dict(STEPS[0]), dict(STEPS[1])])
        self.assertEqual(rec['gate_config'], gate_ok.config_hash(self.gate_config))
        self.assertIn('GATE OK (warned: lint)', self.said)

    def test_a_record_it_writes_is_one_main_takes(self):
        self.assertEqual(self.land(), 0, self.said)
        rec, _ = landgate.read_record(self.repo.root, self.merge)
        expected = {'head': self.merge, 'steps': ['unit', 'lint'], 'mode': 'full', 'full': True,
                    'source': 'gate.json', 'gate_config': gate_ok.config_hash(self.gate_config)}
        self.assertIs(landgate.check_record(self.repo.root, rec, self.merge, self.base, expected,
                                            runner.repo_name(self.cfg, self.repo.root), KITH,
                                            self.gate_config), rec)

    def test_a_docs_only_merge_is_recorded_as_docs(self):
        self.spec['docs'], self.spec['full'] = True, False
        self.outcome.facts['docs'] = True
        self.assertEqual(self.land(), 0, self.said)
        rec, _ = landgate.read_record(self.repo.root, self.merge)
        self.assertEqual((gate_ok.mode_of(rec), rec['full']), ('docs', False))

    def test_another_kit_is_refused_and_nothing_runs(self):
        self.assertEqual(self.land(kit='f' * 64), landgate.EXIT_REFUSED)
        self.assertEqual(self.calls, [])
        self.assertIn("kit files are not main's", ' '.join(self.said))

    def test_a_tree_not_at_the_merge_is_refused_and_nothing_runs(self):
        self.spec['sha'] = self.base
        self.assertEqual(self.land(), landgate.EXIT_REFUSED)
        self.assertEqual(self.calls, [])
        self.assertIn('not the merge', ' '.join(self.said))

    def test_a_failed_gate_is_its_own_code_and_writes_no_record(self):
        self.outcome = G.Outcome(G.FAILED, ['GATE FAILED: unit'])
        self.assertEqual(self.land(), G.FAILED)
        self.assertEqual(landgate.read_record(self.repo.root, self.merge)[0], None)

    def test_a_gate_that_leaves_the_tree_changed_writes_no_record(self):
        def dirty(root, cfg, base, **kw):
            self.repo.write('a.py', 'x = 2\n')
            return self.outcome
        self.assertEqual(self.land(merged_gate=dirty), landgate.EXIT_FAILED)
        self.assertIn('left the job tree changed', ' '.join(self.said))
        self.assertEqual(landgate.read_record(self.repo.root, self.merge)[0], None)

    def test_node_main_refuses_a_job_that_is_no_land_job(self):
        said = []
        d = self.repo.base
        self.assertEqual(landgate.node_main('j-20261029T100000Z-abcdef', os.path.join(d, 'node'),
                                            say=said.append), landgate.EXIT_USAGE)
        self.assertIn('is no land job', said[0])
        self.assertEqual(landgate.node_main('../x', os.path.join(d, 'node'), say=said.append),
                         landgate.EXIT_USAGE)

    def test_node_main_runs_a_land_jobs_spec_in_its_tree(self):
        job = 'j-20261029T100000Z-abcdef'
        b = box.Box(os.path.join(self.repo.base, 'node'))
        os.makedirs(b.job_dir(job))
        spec = box.check_land_spec(dict(self.spec, job=job, repo='my-repo', remote='origin',
                                        main='main', max_jobs=1, note=None))
        box._write_json(b._p(job, 'spec.json'), spec)
        with mock.patch.object(landgate, 'land_gate', return_value=0) as lg:
            self.assertEqual(landgate.node_main(job, b.base, say=self.said.append), 0)
        self.assertEqual(lg.call_args[0][:2], (b.tree(spec), spec))


if __name__ == '__main__':
    unittest.main()
