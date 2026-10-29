"""kitlib.modulemap.scopelog and tools/module_map.py --scope --record: a gate's scope record, its
pruning and the weekly sum (docs/module-map.md "Scope records and the weekly sum")."""
import contextlib
import datetime
import io
import json
import os
import sys
import unittest
from unittest import mock

from kitlib.gate import result as gresult
from kitlib.git import repo as gitrepo
from kitlib.modulemap import scope, scopelog

from tests.gitfixture import TempRepo, fixture_env
from tests.home import result_drive
from tests.modulemap._fixtures import MAP, mapped

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import module_map as cli  # noqa: E402

NOW = datetime.datetime(2026, 10, 29, 12, 0, tzinfo=datetime.timezone.utc)
EDGES = [{'from': 'brain', 'to': 'node', 'kind': 'build', 'why': 'tests drive a node'}]
EDGE_WHY = 'via edge build: tests drive a node'
MAP_REL = 'docs/module-map.json'
MB = 'a' * 40


class RepoCase(unittest.TestCase):
    """A repository on branch `topic` whose .kit.json names a module map (none with `map=False`),
    under a temp home whose machine config sets a temp dev_drive."""
    map = True

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.drive = result_drive(self)
        self.repo.write(MAP_REL, json.dumps(dict(MAP, edges=EDGES)))
        self.repo.write('.kit.json', json.dumps({'modules': {'map': MAP_REL}} if self.map else {}))
        self.repo.write('node/n.cs', 'class N {}')
        self.repo.commit('map', '.')
        self.repo.git('checkout', '-q', '-b', 'topic')
        self.mm = mapped({'edges': EDGES})

    def folder(self):
        common, _ = gitrepo.common_dir(self.root)
        return os.path.join(self.drive, 'pm-kit', 'kit-gate-scope', gresult.repo_key(common))

    def put(self, when, rec, name=None):
        """A record file stamped `when` in the scope folder; `rec` a dict, or text written as is."""
        os.makedirs(self.folder(), exist_ok=True)
        name = name or '%s-1-%04x.json' % (when.strftime(scopelog.STAMP), len(os.listdir(self.folder())))
        with open(os.path.join(self.folder(), name), 'w', encoding='utf-8') as fh:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec))
        return name

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(['-C', self.root] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()


class WriteTest(RepoCase):

    def test_a_change_is_kept_as_one_record_in_the_repositorys_scope_folder(self):
        s = scope.scope_of(self.mm, ['node/n.cs', 'README.md'])
        path, why = scopelog.write(self.root, s, 'main', MB, now=NOW)
        self.assertIsNone(why)
        self.assertEqual(os.path.dirname(path), self.folder())
        self.assertRegex(os.path.basename(path), r'^20261029T120000Z-\d+-[0-9a-f]{4}\.json$')
        self.assertEqual(os.listdir(self.folder()), [os.path.basename(path)])   # no temp file left
        with open(path, encoding='utf-8') as fh:
            rec = json.load(fh)
        head = self.repo.git('rev-parse', 'HEAD').strip()
        self.assertEqual(rec, {
            'schema': 1, 'at': '2026-10-29T12:00:00Z', 'head': head, 'branch': 'topic',
            'base': 'main', 'merge_base': MB,
            'paths': {'node/n.cs': 'node', 'README.md': '(root)'}, 'paths_total': 2,
            'changed': ['(root)', 'node'],
            'widen': [{'module': 'brain', 'from': 'node', 'why': EDGE_WHY}], 'widened': True})

    def test_a_change_that_widens_nothing_says_so(self):
        path, _ = scopelog.write(self.root, scope.scope_of(self.mm, ['brain/b.cs']), 'main', MB,
                                 now=NOW)
        with open(path, encoding='utf-8') as fh:
            rec = json.load(fh)
        self.assertEqual((rec['widen'], rec['widened']), ([], False))

    def test_no_changed_path_writes_nothing(self):
        self.assertEqual(scopelog.write(self.root, scope.scope_of(self.mm, []), 'main', MB),
                         (None, 'no changed path'))
        self.assertFalse(os.path.exists(self.folder()))

    def test_paths_are_capped_and_all_counted(self):
        paths = ['brain/f%03d.cs' % i for i in range(scopelog.MAX_PATHS + 50)]
        path, _ = scopelog.write(self.root, scope.scope_of(self.mm, paths), 'main', MB, now=NOW)
        with open(path, encoding='utf-8') as fh:
            rec = json.load(fh)
        self.assertEqual(len(rec['paths']), scopelog.MAX_PATHS)
        self.assertEqual(rec['paths_total'], scopelog.MAX_PATHS + 50)
        self.assertIn('brain/f000.cs', rec['paths'])

    def test_a_write_prunes_records_and_temp_files_past_the_retention(self):
        old = NOW - datetime.timedelta(days=scopelog.RETAIN_DAYS + 1)
        recent = NOW - datetime.timedelta(days=scopelog.RETAIN_DAYS - 1)
        gone = self.put(old, {'schema': 1})
        gone_tmp = self.put(old, '{', name='.%s-2-abcd.json.tmp' % old.strftime(scopelog.STAMP))
        kept = self.put(recent, {'schema': 1})
        self.put(old, 'x', name='notes.txt')
        path, _ = scopelog.write(self.root, scope.scope_of(self.mm, ['node/n.cs']), 'main', MB,
                                 now=NOW)
        left = sorted(os.listdir(self.folder()))
        self.assertEqual(left, sorted([kept, 'notes.txt', os.path.basename(path)]))
        self.assertNotIn(gone, left)
        self.assertNotIn(gone_tmp, left)

    def test_no_dev_drive_writes_nothing_and_says_why(self):
        with mock.patch.object(gresult, 'kit_folder',
                               side_effect=gresult.ResultError('no dev_drive set')):
            got = scopelog.write(self.root, scope.scope_of(self.mm, ['node/n.cs']), 'main', MB)
        self.assertEqual(got, (None, 'no dev_drive set'))
        self.assertFalse(os.path.exists(self.folder()))

    def test_the_tool_keeps_a_record_only_with_record(self):
        self.repo.write('node/n.cs', 'class N2 {}')
        code, out = self.cli('--scope')
        self.assertEqual(code, 0, out)
        self.assertNotIn('record', out)
        self.assertFalse(os.path.exists(self.folder()))
        code, out = self.cli('--scope', '--record')
        self.assertEqual(code, 0, out)
        names = os.listdir(self.folder())
        self.assertEqual(len(names), 1)
        self.assertIn('module_map scope: record kept %s' % os.path.join(self.folder(), names[0]),
                      out)

    def test_a_record_that_cannot_be_kept_is_a_line_and_exit_0(self):
        self.repo.write('node/n.cs', 'class N2 {}')
        with mock.patch.object(gresult, 'kit_folder',
                               side_effect=gresult.ResultError('no dev_drive set')):
            code, out = self.cli('--scope', '--record')
        self.assertEqual(code, 0, out)
        self.assertIn('module_map scope: record not kept: no dev_drive set', out)

    def test_record_goes_with_scope_only(self):
        code, _ = self.cli('--check', '--record')
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.folder()))


class NoMapTest(RepoCase):
    map = False

    def test_no_map_keeps_no_record_and_sums_nothing(self):
        self.repo.write('node/n.cs', 'class N2 {}')
        code, out = self.cli('--scope', '--record')
        self.assertEqual(code, 2, out)
        self.assertFalse(os.path.exists(self.folder()))
        self.put(NOW, {'schema': 1, 'widen': []})
        self.assertEqual(scopelog.summary(self.root, NOW), (None, []))


class SummaryTest(RepoCase):

    def rec(self, head, widen=()):
        return {'schema': 1, 'head': head, 'widen': [
            {'module': m, 'from': src, 'why': why} for m, src, why in widen]}

    def test_with_a_map_and_no_record_the_sum_is_zero_with_a_note(self):
        got, notes = scopelog.summary(self.root, NOW)
        self.assertEqual(got, {'days': 7, 'runs': 0, 'heads': 0, 'widened': 0,
                               'widened_heads': 0, 'routes': []})
        self.assertEqual(len(notes), 1)
        self.assertIn('no gate scope record in the last 7 days', notes[0])

    def test_the_sum_counts_runs_widenings_and_routes_over_the_window(self):
        hour = datetime.timedelta(hours=1)
        edge = [('brain', 'node', EDGE_WHY)]
        self.put(NOW - hour, self.rec('h1', edge))
        self.put(NOW - 2 * hour, self.rec('h1', edge))       # the same head gated again
        self.put(NOW - 3 * hour, self.rec('h2', [(m, 'protocol', 'via messaging')
                                                 for m in ('brain', 'node', 'worker')]))
        self.put(NOW - 4 * hour, self.rec('h3'))
        self.put(NOW - datetime.timedelta(days=8), self.rec('old', edge))   # outside the window
        self.put(NOW - 5 * hour, '{not json')
        self.put(NOW - 6 * hour, {'schema': 2})
        got, notes = scopelog.summary(self.root, NOW)
        self.assertEqual(got, {
            'days': 7, 'runs': 4, 'heads': 3, 'widened': 3, 'widened_heads': 2,
            'routes': [{'from': 'node', 'why': EDGE_WHY, 'modules': ['brain'], 'runs': 2},
                       {'from': 'protocol', 'why': 'via messaging',
                        'modules': ['brain', 'node', 'worker'], 'runs': 1}]})
        self.assertEqual(notes, ['module scope: 2 record(s) could not be read'])
        self.assertEqual(scopelog.line(got), 'module scope: 4 gate run(s) in 7 days (3 head(s)), '
                                             '3 widened beyond the changed modules (75%)')

    def test_a_record_the_tool_kept_is_summed(self):
        self.repo.write('node/n.cs', 'class N2 {}')
        self.assertEqual(self.cli('--scope', '--record')[0], 0)
        got, notes = scopelog.summary(self.root)
        self.assertEqual((got['runs'], got['widened']), (1, 1), notes)
        self.assertEqual(got['routes'], [{'from': 'node', 'why': EDGE_WHY, 'modules': ['brain'],
                                          'runs': 1}])


if __name__ == '__main__':
    unittest.main()
