"""kitlib.modulemap.page and the page modes of tools/module_map.py (docs/module-map.md "The
architecture page")."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from kitlib.config import load as load_config
from kitlib.modulemap import MapError
from kitlib.modulemap import check as C
from kitlib.modulemap import page
from kitlib.modulemap.load import ModuleMap

from tests.gitfixture import TempRepo, fixture_env
from tests.modulemap._fixtures import MAP

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import module_map as cli  # noqa: E402

FULL = dict(MAP, modules=dict(MAP['modules'], brain={'paths': ['brain'], 'why': 'the planner'},
                              _note='not a module'),
            edges=[{'from': 'brain', 'to': 'node', 'kind': 'build', 'why': 'tests drive | a node'}],
            ratchets=[{'id': 'brain-source-node', 'from': 'brain', 'to': 'node',
                       'kinds': ['source'], 'goal': 0, 'record': 'EX-12'},
                      {'id': 'worker-data-root', 'from': 'worker', 'to': '(root)', 'goal': 2,
                       'goal_reason': 'the fixtures stay shared'}])
MAP_REL = 'docs/module-map.json'
PAGE_REL = 'docs/architecture.md'


class RenderTest(unittest.TestCase):
    def test_the_page_lists_modules_messaging_edges_and_ratchets(self):
        text = page.render(ModuleMap(FULL), MAP_REL)
        self.assertEqual(text, '\n'.join([
            '# Module map', '',
            'Generated from `docs/module-map.json` by `module_map.py --page --write` (the PM '
            "kit's docs/module-map.md). Do not edit it by hand: change the map, then write the page "
            'again; `module_map.py --check` fails while the two differ.', '',
            'A crossing the map does not allow (an edge of its kind, a messaging module or a '
            'ratchet) is refused. A path no module holds is in `(root)`.', '',
            '## Modules', '',
            '| Module | Folders | What it is |', '|---|---|---|',
            '| brain | `brain` | the planner |',
            '| node | `node` | - |',
            '| protocol | `shared/protocol`, `shared/Fleet.Protocol*` | - |',
            '| worker | `worker` | - |', '',
            '## Messaging', '', 'Every module may use protocol.', '',
            '## Allowed edges', '',
            '| From | To | Kind | Why |', '|---|---|---|---|',
            '| brain | node | build | tests drive \\| a node |', '',
            '## Ratchets', '',
            'Crossings counted, not refused; each count is driven down to its goal.', '',
            '| Ratchet | From | To | Counts | Goal | Record |', '|---|---|---|---|---|---|',
            '| brain-source-node | brain | node | source | 0 | EX-12 |',
            '| worker-data-root | worker | (root) | every kind | 2 (the fixtures stay shared) | - |',
        ]) + '\n')

    def test_an_empty_map_says_none(self):
        text = page.render(ModuleMap({'modules': {'a': {'paths': ['a']}}}), 'm.json')
        self.assertIn('## Messaging\n\nNone: no module is open to every other.\n', text)
        self.assertIn('## Allowed edges\n\nNone.\n', text)
        self.assertTrue(text.endswith('## Ratchets\n\nNone.\n'), text)

    def test_a_java_imports_ratchet_names_its_files_and_imports(self):
        mm = ModuleMap({'modules': {'api': {'paths': ['api']}}, 'ratchets': [
            {'id': 'api-internal', 'from': 'api', 'to': 'api', 'detector': 'java-imports',
             'goal': 0, 'files': ['api/src/*'], 'imports': ['org.x.internal']}]})
        self.assertIn('| api-internal | api | api | java-imports of org.x.internal in api/src/* | 0 '
                      '| - |', page.render(mm, 'm.json'))

    def test_stale_ignores_line_endings_only(self):
        self.assertFalse(page.stale('a\r\nb\n', 'a\nb\n'))
        self.assertTrue(page.stale('a\nc\n', 'a\nb\n'))
        self.assertTrue(page.stale(None, 'a\n'))


class PageRepoTest(unittest.TestCase):
    """A main repository with the map; the work happens in a linked worktree, as the kit's
    write rule (kitlib.git.checkout.write_text) asks."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.repo.write(MAP_REL, json.dumps(FULL))
        self.repo.write('.kit.json', json.dumps({'owner': {'paths': [MAP_REL]},
                                                 'modules': {'map': MAP_REL, 'page': PAGE_REL}}))
        self.repo.commit('map', '.')
        self.root = self.repo.worktree('page')

    def cli(self, *argv, root=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', root or self.root] + list(argv))
        return code, out.getvalue()

    def path(self, rel):
        return os.path.join(self.root, *rel.split('/'))

    def test_page_prints_the_page_and_writes_nothing(self):
        code, out = self.cli('--page')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.replace('\r\n', '\n'), page.render(ModuleMap(FULL), MAP_REL))
        self.assertFalse(os.path.exists(self.path(PAGE_REL)))

    def test_write_then_check_passes_and_a_stale_page_fails_naming_the_fix(self):
        code, out = self.cli('--check')
        self.assertEqual(code, 1, out)
        self.assertIn('PAGE STALE  docs/architecture.md does not exist: run module_map.py --page '
                      '--write and commit it', out)
        code, out = self.cli('--page', '--write')
        self.assertEqual((code, out.strip()), (0, 'module_map: wrote the page docs/architecture.md'))
        code, out = self.cli('--check')
        self.assertEqual(code, 0, out)
        self.assertIn('page docs/architecture.md matches the map', out)
        with open(self.path(PAGE_REL), 'a', encoding='utf-8', newline='\n') as fh:
            fh.write('a hand edit\n')
        code, out = self.cli('--check')
        self.assertEqual(code, 1, out)
        self.assertIn('PAGE STALE  docs/architecture.md does not match the map: run '
                      'module_map.py --page --write and commit it', out)
        self.assertIn('module_map: FAIL', out)

    def test_a_map_change_makes_the_written_page_stale(self):
        self.cli('--page', '--write')
        with open(self.path(MAP_REL), 'w', encoding='utf-8') as fh:
            json.dump(dict(FULL, messaging=[]), fh)
        code, out = self.cli('--check')
        self.assertEqual(code, 1, out)
        self.assertIn('does not match the map', out)

    def test_no_page_key_checks_no_page(self):
        with open(self.path('.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump({'owner': {'paths': [MAP_REL]}, 'modules': {'map': MAP_REL}}, fh)
        code, out = self.cli('--check')
        self.assertEqual(code, 0, out)
        self.assertNotIn('page', out.replace('module_map', ''))
        code, out = self.cli('--page', '--write')
        self.assertEqual(code, 2, out)
        self.assertIn('module_map: NOT CHECKED: .kit.json has no "modules".page', out)

    def test_a_map_on_the_command_line_skips_the_page_check(self):
        out = C.run(self.root, load_config(self.root), 'check', map_path=self.path(MAP_REL),
                    base_rev='main')
        self.assertEqual(out.code, C.OK, out.lines)
        self.assertIn('note: the page docs/architecture.md is not checked: the map was given on '
                      'the command line', out.lines)

    def test_write_is_refused_in_the_main_checkout(self):
        with self.assertRaisesRegex(MapError, 'cannot write the page docs/architecture.md: .*MAIN'):
            C.run(self.repo.root, load_config(self.repo.root), 'page', write=True)

    def test_write_without_page_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(['-C', self.root, '--check', '--write'])
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
