"""kitlib.gate.runner.merged_steps: what merged_gate would run, read without running anything, so a
landing whose merged gate ran on a load node can check the node's record against it
(kitlib.loadnode.landgate). It must name exactly the steps merged_gate runs, in order, and its
docs-only call, for every way merged_gate selects."""
import unittest
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.gate import runner
from tests.gate import fixtures
from tests.gate.test_branch_gate import mode_step


class MergedSteps(fixtures.GateRepoCase):
    GATE = {'smoke_steps': ['boot'],
            'docs_only': {'globs': ['docs/*.md'], 'steps': ['scrub']},
            'steps': [{'name': 'suite', 'always': True, 'cmd': mode_step('suite'), 'pass': '^mode '},
                      {'name': 'boot', 'manual': True, 'cmd': mode_step('boot'), 'pass': '^mode '},
                      {'name': 'scrub', 'globs': ['*.md'], 'cmd': mode_step('scrub'),
                       'pass': '^mode '}]}

    def same(self, wt, **kw):
        cfg = kconfig.load_at(wt, self.main)
        info, why = runner.merged_steps(wt, cfg, self.main, tree=wt, **kw)
        self.assertIsNone(why)
        out = runner.merged_gate(wt, cfg, self.main, log_dir=self.logs, book_kw=self.book_kw,
                                 tree=wt, **kw)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(info['steps'], [r['name'] for r in out.results])
        self.assertEqual((info['docs'], info['source'], info['head']),
                         (bool(out.facts['docs']), out.facts['source'], out.head))
        return info

    def test_a_docs_only_merge_with_docs_names_only_the_docs_steps(self):
        wt, _ = self.branch('docs', {'docs/a.md': 'words\n'})
        info = self.same(wt, docs=True)
        self.assertEqual((info['steps'], info['docs']), (['scrub'], True))

    def test_a_docs_only_merge_without_docs_names_every_step_a_landing_runs(self):
        wt, _ = self.branch('docs', {'docs/a.md': 'words\n'})
        self.assertFalse(self.same(wt)['docs'])

    def test_a_full_merge_never_takes_the_docs_path(self):
        wt, _ = self.branch('docs', {'docs/a.md': 'words\n'})
        self.assertFalse(self.same(wt, full=True, docs=True)['docs'])

    def test_a_code_merge(self):
        wt, _ = self.branch('code', {'a.py': 'x = 1\n'})
        self.same(wt, docs=True)

    def test_a_merge_it_would_refuse_is_why_not(self):
        wt, _ = self.branch('code', {'a.py': 'x = 1\n'})
        cfg = kconfig.load_at(wt, self.main)
        with mock.patch.object(runner.S, 'select', return_value=([], None)):
            info, why = runner.merged_steps(wt, cfg, self.main, tree=wt)
        self.assertIsNone(info)
        self.assertEqual(why, runner.NO_STEP_FULL)


if __name__ == '__main__':
    unittest.main()
