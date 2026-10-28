"""tools/kit_tests.py --smoke and --scrub (docs/test-tools.md "kit_tests", docs/gate.md "Smoke
steps"): --select --smoke adds tests/selection.json's "smoke" modules unless every changed path
is prose, and refuses a smoke entry naming no test module; --scrub runs tests/scrub only, in one
pass. Also the kit table's smoke set and the `paths` entries for tests that read another folder."""
import importlib.util
import io
import os
import unittest
from unittest import mock

from kitlib.tooldeps import selftree
from kitlib.tooldeps.deps import ALL

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_smoke_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

SMOKE = ['tests.guard.test_rules', 'tests.gate.test_land']
SELECTED = ['tests.scrub.test_scrub', 'tests.gate.test_land']
MODULES = sorted(set(SMOKE + SELECTED + ['tests.docs.test_budget']))


def table(**over):
    data = {'prose': ['docs/**', '*.md'], 'smoke': list(SMOKE)}
    data.update(over)
    return selftree.config(KIT, data=data)


class SmokeSelection(unittest.TestCase):
    """selftree.smoke: which smoke modules a change adds."""

    def test_a_code_change_adds_the_smoke_modules_not_selected_yet(self):
        add, why = selftree.smoke(['kitlib/stubs/x.py', 'docs/a.md'], table(), SELECTED, MODULES)
        self.assertEqual(add, ['tests.guard.test_rules'])
        self.assertEqual(why, '1 smoke module(s) added')

    def test_nothing_selected_takes_every_smoke_module(self):
        add, _why = selftree.smoke(['kitlib/stubs/x.py'], table(), None, MODULES)
        self.assertEqual(add, SMOKE)

    def test_a_prose_only_change_adds_none(self):
        add, why = selftree.smoke(['docs/a.md', 'README.md', 'docs\\b.md'], table(), SELECTED,
                                  MODULES)
        self.assertEqual((add, why), ([], 'every changed path is prose'))

    def test_an_empty_change_adds_none(self):
        self.assertEqual(selftree.smoke([], table(), None, MODULES), ([], 'no changed path'))

    def test_a_selection_holding_them_all_adds_none(self):
        add, why = selftree.smoke(['kitlib/x/y.py'], table(), MODULES, MODULES)
        self.assertEqual(add, [])
        self.assertIn('already holds', why)

    def test_a_stale_entry_is_refused_even_on_prose(self):
        cfg = table(smoke=SMOKE + ['tests.gone.test_gone'])
        for changed in (['kitlib/x/y.py'], ['docs/a.md']):
            with self.assertRaises(selftree.ConfigError) as e:
                selftree.smoke(changed, cfg, None, MODULES)
            self.assertIn('tests.gone.test_gone', str(e.exception))

    def test_bad_smoke_tables_are_refused(self):
        for bad in ('tests.a.test_a', ['a.test_a'], [['tests.a.test_a']]):
            with self.assertRaises(selftree.ConfigError):
                selftree.config(KIT, data={'smoke': bad})
        self.assertEqual(selftree.config(KIT, data={})['smoke'], [])


class KitTable(unittest.TestCase):
    """The kit's own table: the smoke set and the folder-reading tests its paths reach."""

    @classmethod
    def setUpClass(cls):
        cls.tree = selftree.scan(KIT)
        cls.cfg = selftree.config(KIT)

    def select(self, path):
        return selftree.selection([path], self.tree, self.cfg)[0]

    def test_the_smoke_set_is_guard_land_lock_and_commit_approval(self):
        smoke = self.cfg['smoke']
        mods = set(selftree.all_test_modules(KIT))
        for module in smoke:
            self.assertIn(module, mods)
        for pkg in ('guard', 'gate', 'lock', 'commit'):
            self.assertTrue([m for m in smoke if m.startswith('tests.%s.' % pkg)], pkg)
        self.assertIn('tests.gate.test_land', smoke)
        self.assertIn('tests.commit.test_commit_approval', smoke)
        self.assertNotIn('tests.commit.test_commit', smoke)     # 107 s, not split by class

    def test_a_docs_heading_change_runs_the_content_citation_tests(self):
        # tests/content/test_doc_citations.py checks the docs headings content/ cites; no test
        # in tests/content names commit.md
        picked = self.select('docs/commit.md')
        self.assertIsNot(picked, ALL)
        self.assertIn('content', picked)

    def test_a_new_role_or_agent_runs_the_launcher_tests(self):
        # tests/launcher/test_start.py pins the file lists of both folders
        for path in ('content/roles/new-role.md', 'hosts/claude/shell/agents/new-role.md'):
            picked = self.select(path)
            self.assertTrue(picked is ALL or 'launcher' in picked, path)

    def test_a_new_skill_runs_the_docs_tests(self):
        # the selection row is kept although no docs test reads skills now; removing it is the owner's call
        picked = self.select('content/skills/new-skill/SKILL.md')
        self.assertIsNot(picked, ALL)
        self.assertIn('docs', picked)

    def test_a_kitlib_leaf_runs_the_host_tests(self):
        # tests/host test_layering and test_leaks walk all of kitlib/
        picked = self.select('kitlib/redfirst/__init__.py')
        self.assertIsNot(picked, ALL)
        self.assertIn('host', picked)


class Cli(unittest.TestCase):
    """--select --smoke and --scrub through main(), the unit runs mocked."""

    def run_main(self, argv, cfg=None, top=None):
        calls, out = [], io.StringIO()

        def run(modules, root, jobs, **kw):
            calls.append((kw['environ'].get('KIT_FAKE_GIT'), list(modules)))
            kw['out'].write('kit_tests: OK in 1s\n')
            return 0, ''
        here = top or os.path.abspath('.')
        patches = [mock.patch.dict(os.environ, {'KIT_FAKE_GIT': '1'}),
                   mock.patch.object(cli.gitrepo, 'toplevel', return_value=(here, None)),
                   mock.patch.object(cli.gitrepo, 'common_dir', return_value=(here, None)),
                   mock.patch.object(cli.giveback, 'drainer', return_value=lambda n: None),
                   mock.patch.object(cli.selfrun, 'run', side_effect=run)]
        if top is None:
            patches += [mock.patch.object(cli.selftree, 'config', return_value=cfg or table()),
                        mock.patch.object(cli.selftree, 'all_test_modules', return_value=MODULES),
                        mock.patch.object(cli, 'plan', return_value=list(SELECTED))]
        for p in patches:
            p.start()
        try:
            rc = cli.main(['--jobs', '2'] + list(argv), out=out)
        finally:
            for p in reversed(patches):
                p.stop()
        return rc, calls, out.getvalue()

    def test_select_smoke_on_code_runs_the_selection_and_the_smoke_modules(self):
        rc, calls, text = self.run_main(['--select', '--smoke', '--changed', 'kitlib/stubs/x.py'])
        self.assertEqual(rc, 0, text)
        self.assertEqual(calls, [('1', SELECTED + ['tests.guard.test_rules'])], text)
        self.assertIn('kit_tests: --smoke: 1 smoke module(s) added: tests.guard.test_rules\n',
                      text)

    def test_select_smoke_on_prose_runs_the_selection_only(self):
        rc, calls, text = self.run_main(['--select', '--smoke', '--changed', 'docs/a.md'])
        self.assertEqual(rc, 0, text)
        self.assertEqual(calls, [('1', SELECTED)], text)
        self.assertIn('kit_tests: --smoke: every changed path is prose\n', text)

    def test_select_without_smoke_adds_nothing(self):
        rc, calls, text = self.run_main(['--select', '--changed', 'kitlib/stubs/x.py'])
        self.assertEqual(calls, [('1', SELECTED)], text)
        self.assertNotIn('--smoke', text)

    def test_a_stale_smoke_entry_refuses_and_runs_nothing(self):
        cfg = table(smoke=SMOKE + ['tests.gone.test_gone'])
        rc, calls, text = self.run_main(['--select', '--smoke', '--changed', 'kitlib/x/y.py'],
                                        cfg=cfg)
        self.assertEqual((rc, calls), (cli.USAGE, []), text)
        self.assertRegex(text, r'(?m)^kit_tests: refused: .*tests\.gone\.test_gone.* - no test ran')

    def test_smoke_goes_with_select_only(self):
        for argv in (['--full', '--smoke'], ['--scrub', '--smoke'], ['--list', '--smoke']):
            with self.assertRaises(SystemExit) as e, mock.patch('sys.stderr', io.StringIO()):
                cli.main(argv)
            self.assertEqual(e.exception.code, cli.USAGE, argv)

    def test_scrub_runs_the_scrub_package_only_in_one_pass(self):
        rc, calls, text = self.run_main(['--scrub'], top=KIT)
        self.assertEqual(rc, 0, text)
        scrub = selftree.test_modules(KIT, ['scrub'])
        self.assertTrue(scrub)
        self.assertTrue(all(m.startswith('tests.scrub.') for m in scrub), scrub)
        self.assertEqual(calls, [('1', scrub)], text)
        self.assertIn('kit_tests: --scrub: tests/scrub only, %d module(s)\n' % len(scrub), text)
        self.assertNotIn('real-git pass', text)
        self.assertEqual(text.splitlines()[-1], 'kit_tests: OK in 1s')

    def test_scrub_plan_runs_nothing(self):
        rc, calls, text = self.run_main(['--scrub', '--plan'], top=KIT)
        self.assertEqual((rc, calls), (0, []), text)
        self.assertIn('kit_tests: --scrub: tests/scrub only', text)

    def test_scrub_takes_no_change_list(self):
        for argv in (['--scrub', '--base', 'main'], ['--scrub', '--changed', 'a'],
                     ['--scrub', '--both-tiers']):
            with self.assertRaises(SystemExit) as e, mock.patch('sys.stderr', io.StringIO()):
                cli.main(argv)
            self.assertEqual(e.exception.code, cli.USAGE, argv)


if __name__ == '__main__':
    unittest.main()
