"""kitlib.pieces and tools/pieces.py (docs/repo-checks.md): the path-to-piece table.

The parser and its answers, under the layout before and after a repository's move
(`_pieces_fixtures`); the fixtures themselves; the CLI on a fixture repository; and a repository
with no table. A repository's audit of its OWN table against its tree is repository data and
stays in that repository.
"""
import contextlib
import io
import json
import os
import posixpath
import sys
import unittest
from unittest import mock

from kitlib import pieces
from kitlib.config import ConfigError
from tests.gitfixture import TempRepo, fixture_env
from tests.pieces import _pieces_fixtures as fx
from tests.pieces._pieces_fixtures import both_tables

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import pieces as cli  # noqa: E402


def piece(**kw):
    p = {'id': 'x', 'group': 'worker', 'kind': 'spec', 'dir': 'x'}
    p.update(kw)
    return p


class Parsing(unittest.TestCase):
    """load() refuses a table that would answer wrongly rather than fail later."""

    def refuses(self, data, needle):
        with self.assertRaises(pieces.TableError) as e:
            pieces.load(data)
        self.assertIn(needle, str(e.exception))

    def test_a_good_minimal_table_loads(self):
        t = pieces.load({'schema': 1, 'pieces': [piece()]})
        self.assertEqual(t.ids(), ['x'])

    def test_each_malformed_table_is_refused(self):
        self.refuses({'schema': 2}, 'schema')
        self.refuses({'schema': 1, 'extra': 1}, 'unknown top-level')
        self.refuses({'schema': 1, 'pieces': [piece(), piece()]}, 'repeated')
        self.refuses({'schema': 1, 'pieces': [piece(colour='red')]}, 'unknown field')
        self.refuses({'schema': 1, 'pieces': [piece(kind='weird')]}, 'unknown kind')
        self.refuses({'schema': 1, 'pieces': [piece(group='')]}, 'no group')
        for bad in ('x/', '/x', 'a\\b', 'a/../b'):
            with self.subTest(dir=bad):
                self.refuses({'schema': 1, 'pieces': [piece(dir=bad)]}, 'repo-relative')
        self.refuses({'schema': 1, 'pieces': [piece(kind='gradle')]}, 'gradle piece')
        self.refuses({'schema': 1, 'pieces': [piece(gradle=':x')]}, 'gradle piece')
        self.refuses({'schema': 1, 'pieces': [piece(kind='gradle', gradle='x')]}, 'gradle piece')
        self.refuses({'schema': 1, 'root': ['docs/'], 'pieces': []}, 'root path')

    def test_bad_deps_are_refused(self):
        a, b = piece(id='a', dir='a'), piece(id='b', dir='b')
        self.refuses({'schema': 1, 'pieces': [dict(a, deps='b'), b]}, 'deps must be a list')
        self.refuses({'schema': 1, 'pieces': [dict(a, deps=['b', 'b']), b]}, 'distinct')
        self.refuses({'schema': 1, 'pieces': [dict(a, deps=['a'])]}, 'itself or no piece')
        self.refuses({'schema': 1, 'pieces': [dict(a, deps=['zz'])]}, 'itself or no piece')
        self.refuses({'schema': 1, 'pieces': [dict(a, deps=['b']), dict(b, deps=['a'])]}, 'cycle')

    def test_an_unknown_id_names_the_table(self):
        with self.assertRaises(KeyError) as e:
            fx.OLD.dir('no-such-piece')
        self.assertIn('.kit.json "pieces"', e.exception.args[0])


@both_tables
class Answers(unittest.TestCase):
    """What the tools ask, under the layout before and after the move."""

    def test_path_keeps_a_trailing_slash(self):
        self.assertEqual(self.P('pvp', 'puffer/patches/'), self.table.dir('pvp') + '/puffer/patches/')
        self.assertEqual(self.P('pvp'), self.table.dir('pvp'))

    def test_the_longest_dir_wins(self):
        self.assertEqual(self.table.piece_of(self.P('model-sums')), 'model-sums')
        self.assertEqual(self.table.piece_of(self.P('delta-scripts', 'src/A.java')), 'delta-scripts')
        self.assertEqual(self.table.piece_of(self.P('node-docs')), 'node-docs')
        self.assertEqual(self.table.piece_of(self.P('brain-docs', 'auth.md')), 'brain-docs')
        self.assertIsNone(self.table.piece_of('docs/INDEX.md'))
        self.assertIsNone(self.table.piece_of(self.table.dir('scripts') + 'x/A.java'))   # no bare prefix

    def test_every_gradle_piece_is_its_own_module(self):
        for module, d in self.table.gradle_pieces():
            with self.subTest(module=module):
                self.assertEqual(self.table.gradle_module_of(d + '/src/A.java'), module)
                self.assertIsNone(self.table.gradle_module_of(d))

    def test_the_wire_and_its_forced_module(self):
        spec = self.P('protocol', 'fixtures/x.json')
        self.assertTrue(spec.startswith(self.table.wire_prefixes()))
        self.assertEqual(self.table.forces_of(spec), ['worker'])
        self.assertEqual(self.table.forces_of(self.P('worker', 'A.java')), [])

    def test_a_md_in_a_forcing_piece_forces_nothing(self):
        # A doc in a forcing piece must force no module's tests.
        for rel in ('README.md', 'brain-node.md', 'notes/Spec.MD'):
            with self.subTest(rel=rel):
                self.assertEqual(self.table.forces_of(self.P('protocol', rel)), [])

    def test_any_other_file_in_a_forcing_piece_still_forces(self):
        for rel in ('brain-node.schema.json', 'relogin-exemption-cases.json', 'gen/Wire.java',
                    'build.gradle.kts', 'README.md.txt'):
            with self.subTest(rel=rel):
                self.assertEqual(self.table.forces_of(self.P('protocol', rel)), ['worker'])

    def test_dotnet_changes(self):
        for pid in ('Fleet.Brain', 'Fleet.Node.Core', 'Fleet.Protocol.Tests', 'brain-ops'):
            with self.subTest(piece=pid):
                self.assertTrue(self.table.is_dotnet(self.P(pid, 'X.cs')))
        self.assertTrue(self.table.is_dotnet(self.table.solution()))
        self.assertFalse(self.table.is_dotnet(self.P('worker', 'A.java')))
        self.assertFalse(self.table.is_dotnet('docs/x.md'))

    def test_a_path_under_the_solution_folder_no_piece_holds_is_dotnet(self):
        # A new project, Directory.Packages.props or nuget.config next to the solution: dotnet may
        # read it, so it counts before the table lists it (as the old `fleet/` prefix rule did).
        sdir = self.table.solution_dir()
        for rel in ('NewProj/A.cs', 'Directory.Packages.props', 'nuget.config', 'X.cs'):
            path = posixpath.join(sdir, rel)
            with self.subTest(path=path):
                self.assertIsNone(self.table.owner(path))
                self.assertTrue(self.table.is_dotnet(path))
        if sdir:
            self.assertFalse(self.table.is_dotnet('nowhere/x.cs'))

    def test_the_owner_of_a_path(self):
        self.assertEqual(self.table.owner(self.P('worker', 'A.java')), ('piece', 'worker'))
        self.assertEqual(self.table.owner('docs/INDEX.md'), ('root', 'docs'))
        self.assertEqual(self.table.owner(self.table.solution()), ('dotnet-root', self.table.solution()))
        self.assertIsNone(self.table.owner('nowhere/x'))


class Deps(unittest.TestCase):
    """`deps` and their transitive `dependents` (a change fans out to every user)."""

    T = pieces.load({'schema': 1, 'pieces': [
        piece(id='api', dir='api'), piece(id='bind', dir='bind', deps=['api']),
        piece(id='work', dir='work', deps=['bind', 'api']), piece(id='lone', dir='lone')]})

    def test_direct_deps_and_their_closure(self):
        self.assertEqual(self.T.deps('work'), ['bind', 'api'])
        self.assertEqual(self.T.deps('api'), [])
        self.assertEqual(self.T.closure_deps('work'), {'bind', 'api'})

    def test_dependents_are_transitive_and_include_the_changed_piece(self):
        self.assertEqual(self.T.dependents(['api']), {'api', 'bind', 'work'})
        self.assertEqual(self.T.dependents(['bind']), {'bind', 'work'})
        self.assertEqual(self.T.dependents(['work', 'lone']), {'work', 'lone'})
        self.assertEqual(self.T.dependents([]), set())


class Repath(unittest.TestCase):
    """repath maps a path before the move to where it is after it (NEW only carries `was`)."""

    def test_every_moved_piece_maps_through_its_was(self):
        for p in fx.NEW.pieces:
            if 'was' in p:
                with self.subTest(piece=p['id']):
                    self.assertEqual(fx.NEW.repath(p['was']), p['dir'])
                    self.assertEqual(fx.NEW.repath(p['was'] + '/a/b.txt'), p['dir'] + '/a/b.txt')

    def test_the_longest_was_wins(self):
        self.assertEqual(fx.NEW.repath('fleet/docs/node.md'), fx.NEW.dir('node-docs'))
        self.assertEqual(fx.NEW.repath('delta-scripts/models/SHA256SUMS'), fx.NEW.dir('model-sums'))

    def test_root_files_paths_outside_any_piece_and_the_old_table(self):
        self.assertEqual(fx.NEW.repath('fleet/global.json'), 'global.json')
        self.assertEqual(fx.NEW.repath('docs/INDEX.md'), 'docs/INDEX.md')
        self.assertEqual(fx.OLD.repath('worker/A.java'), 'worker/A.java')
        self.assertIn('pvp', fx.NEW.legacy_dirs())
        self.assertIn('fleet/.gitignore', fx.NEW.legacy_dirs())
        self.assertEqual(fx.OLD.legacy_dirs(), [])

    def test_a_removed_folder_maps_to_none(self):
        t = pieces.load({'schema': 1, 'removed': ['gone'], 'pieces': [piece()]})
        self.assertIsNone(t.repath('gone/a.txt'))
        self.assertEqual(t.repath('gonex/a.txt'), 'gonex/a.txt')


class Fixtures(unittest.TestCase):
    """OLD is the table phase 1 wrote, NEW is OLD moved; the real table is one of them."""

    @staticmethod
    def shape(data):
        """The table without its prose and its build edges: the layout, which decides where a path
        lives. `deps` came after both layouts and is checked against the build files by
        test_gate.BuildGraphOracle, not here."""
        out = {k: v for k, v in data.items() if k != 'about'}
        out['pieces'] = [{k: v for k, v in p.items() if k not in ('transitional', 'deps')}
                         for p in data['pieces']]
        return out

    def test_new_moves_every_piece_but_plans(self):
        moved = {fx.OLD_ID.get(p['id'], p['id']) for p in fx.NEW.pieces if 'was' in p}
        self.assertEqual(sorted(set(fx.OLD.ids()) - moved), ['plans'])
        self.assertEqual(sorted(fx.MOVES), sorted(moved))

    def test_both_tables_hold_every_piece_id(self):
        # Every OLD piece is in NEW under its id after a split, plus the pieces a split added.
        self.assertEqual(sorted(fx.NEW.ids()),
                         sorted([fx.SPLITS.get(i, {}).get('id', i) for i in fx.OLD.ids()]
                                + [p['id'] for _, p in fx.ADDED]))

    def test_both_tables_run_a_decorated_class_twice(self):
        self.assertIs(Answers.table, fx.OLD)
        self.assertIs(AnswersNew.table, fx.NEW)   # noqa: F821  (made by both_tables)


class RepoCase(unittest.TestCase):
    """A real repository (tests/gitfixture.py); the process cache cleared around each test."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        pieces.clear_cache()
        self.addCleanup(pieces.clear_cache)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(['-C', self.root] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()


class Cli(RepoCase):
    """The CLI on a repository whose table is the NEW fixture (`.kit.json` "pieces" by path)."""

    def setUp(self):
        super().setUp()
        self.repo.write('cfg/p.json', json.dumps(fx.NEW_DATA))
        self.repo.write('.kit.json', json.dumps({'pieces': 'cfg/p.json'}))

    def test_dir_path_piece_of_and_list(self):
        t = pieces.table(self.root)
        self.assertEqual(t.data, fx.NEW_DATA)
        self.assertEqual(self.run_cli('--dir', 'worker'), (0, t.dir('worker') + '\n'))
        self.assertEqual(self.run_cli('--path', 'pvp', 'env/x.h'), (0, t.path('pvp', 'env/x.h') + '\n'))
        self.assertEqual(self.run_cli('--piece-of', t.path('model-sums')), (0, 'model-sums\n'))
        code, out = self.run_cli('--list')
        self.assertEqual((code, len(out.splitlines())), (0, len(t.pieces)))

    def test_gradle_output_and_was(self):
        t = pieces.table(self.root)
        self.assertEqual(self.run_cli('--gradle', 'worker'), (0, t.get('worker')['gradle'] + '\n'))
        self.assertEqual(self.run_cli('--output', 'worker', 'install'), (0, t.output('worker', 'install') + '\n'))
        self.assertEqual(self.run_cli('--was', 'worker'), (0, t.get('worker').get('was', '') + '\n'))
        self.assertEqual(self.run_cli('--was', 'plans'), (0, '\n'))
        self.assertEqual(self.run_cli('--gradle', 'pvp')[0], 2)
        self.assertEqual(self.run_cli('--output', 'scripts', 'nope')[0], 2)

    def test_an_unknown_id_or_path_exits_2(self):
        self.assertEqual(self.run_cli('--dir', 'nope')[0], 2)
        self.assertEqual(self.run_cli('--piece-of', 'docs/INDEX.md')[0], 2)
        self.assertEqual(self.run_cli()[0], 2)

    def test_an_inline_table_reads_the_same(self):
        self.repo.write('.kit.json', json.dumps({'pieces': fx.NEW_DATA}))
        self.assertEqual(pieces.load_repo(self.root).data, fx.NEW_DATA)

    def test_a_malformed_table_refuses_with_exit_2(self):
        self.repo.write('cfg/p.json', json.dumps({'schema': 2}))
        code, out = self.run_cli('--list')
        self.assertEqual(code, 2, out)
        self.assertIn('pieces: refused: schema must be 1', out)

    def test_the_table_is_read_once_per_root(self):
        first = pieces.table(self.root)
        self.repo.write('.kit.json', '{}')
        self.assertIs(pieces.table(self.root), first)
        pieces.clear_cache()
        with self.assertRaises(pieces.TableError):
            pieces.table(self.root)


class NoTable(RepoCase):
    """A repository without a pieces table is not an error until a tool needs one."""

    def test_has_table_is_false_and_table_refuses(self):
        self.assertFalse(pieces.has_table(self.root))
        with self.assertRaises(pieces.TableError):
            pieces.table(self.root)
        code, out = self.run_cli('--list')
        self.assertEqual(code, 2, out)
        self.assertIn('no pieces table', out)

    def test_a_tools_pieces_json_alone_is_not_a_table(self):
        # Retired fallback: the kit config reads a section only from `.kit.json` (inline or the
        # file it names), never from a file because it exists.
        self.repo.write('tools/pieces.json', json.dumps({'schema': 1, 'pieces': [piece()]}))
        self.assertFalse(pieces.has_table(self.root))

    def test_a_named_file_that_is_missing_refuses(self):
        self.repo.write('.kit.json', json.dumps({'pieces': 'cfg/none.json'}))
        with self.assertRaises(ConfigError):
            pieces.has_table(self.root)
        self.assertEqual(self.run_cli('--list')[0], 2)


if __name__ == '__main__':
    unittest.main()
