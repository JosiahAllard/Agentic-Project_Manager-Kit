import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kitlib import kitrelease as R
from kitlib.kitrelease import store as S
from kitlib import packageexport as P
from tests import gitfixture


class Store(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        bootstrap = Path(R.__file__).with_name('bootstrap.py').read_bytes()
        self.repo.write('kitlib/kitrelease/bootstrap.py', bootstrap.decode('utf-8'))
        self.repo.write('kitlib/fixture.py', 'value = 1\n')
        self.repo.write('tools/fixture.py', 'pass\n')
        self.repo.commit('runtime one', 'kitlib/kitrelease/bootstrap.py',
                         'kitlib/fixture.py', 'tools/fixture.py')
        self.scratch = tempfile.TemporaryDirectory(prefix='shared-store-')
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.store = self.base / 'managed'
        self.first = R.export(Path(self.repo.root), self.repo.head(), self.base / 'artifact-one')
        self.repo.write('kitlib/fixture.py', 'value = 2\n')
        self.repo.commit('runtime two', 'kitlib/fixture.py')
        self.second = R.export(Path(self.repo.root), self.repo.head(), self.base / 'artifact-two')

    def install(self, info=None, expected=None):
        info = self.first if info is None else info
        return S.install(self.store, Path(info['root']), info['identity'], expected)

    def state(self):
        return json.loads((self.store / S.STATE).read_bytes())

    def rewrite_state(self, value):
        (self.store / S.STATE).write_bytes(P.encoded(value))

    def test_install_publishes_once_and_returns_same_shared_directory(self):
        installed = self.install()
        self.assertFalse(installed['ready'])
        self.assertEqual(installed['activation'], 'disabled')
        self.assertEqual(installed['identity'], self.first['identity'])
        self.assertTrue(Path(installed['root']).is_dir())
        self.assertEqual(S.current(self.store, self.first['identity']), installed)
        again = self.install(expected=self.first['identity'])
        self.assertEqual(again, installed)
        self.assertEqual(len(list((self.store / 'releases').iterdir())), 1)
        self.assertFalse(any('plugin' in entry.name for entry in self.store.iterdir()))

    def test_explicit_update_then_pinned_rollback(self):
        first = self.install()
        second = self.install(self.second, self.first['identity'])
        self.assertEqual(second['previous']['identity'], self.first['identity'])
        self.assertNotEqual(second['root'], first['root'])
        back = S.rollback(self.store, self.second['identity'], self.first['identity'])
        self.assertEqual(back['root'], first['root'])
        self.assertEqual(back['previous']['identity'], self.second['identity'])
        with self.assertRaises(S.StoreError):
            S.rollback(self.store, self.second['identity'], self.first['identity'])
        self.assertEqual(len(list((self.store / 'releases').iterdir())), 2)

    def test_update_requires_explicit_current_compare_and_swap(self):
        self.install()
        for expected in (None, 'f' * 64):
            with self.subTest(expected=expected), self.assertRaises(S.StoreError):
                self.install(self.second, expected)
        self.assertEqual(len(list((self.store / 'releases').iterdir())), 1)
        self.assertEqual(self.state()['current']['identity'], self.first['identity'])

    def test_missing_or_wrong_current_pin_refused(self):
        with self.assertRaises(S.StoreError):
            S.current(self.store, self.first['identity'])
        self.install()
        for identity in ('f' * 64, None, 'short', self.first['identity'].upper()):
            with self.subTest(identity=identity), self.assertRaises(S.StoreError):
                S.current(self.store, identity)

    def test_wrong_expected_artifact_never_creates_store(self):
        with self.assertRaises(S.StoreError):
            S.install(self.store, Path(self.first['root']), 'f' * 64)
        self.assertFalse(self.store.exists())

    def test_invalid_expected_current_never_creates_store(self):
        with self.assertRaises(S.StoreError):
            self.install(expected='short')
        self.assertFalse(self.store.exists())

    def test_nonempty_expected_current_on_missing_store_writes_nothing(self):
        with self.assertRaises(S.StoreError):
            self.install(expected=self.first['identity'])
        self.assertFalse(self.store.exists())

    def test_unowned_existing_directory_not_adopted(self):
        self.store.mkdir()
        sentinel = self.store / 'owner.txt'
        sentinel.write_bytes(b'keep')
        with self.assertRaises(S.StoreError):
            self.install()
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(list(self.store.iterdir()), [sentinel])

    def test_foreign_plugin_settings_remain_unchanged(self):
        sentinel = self.base / '.codex' / 'config.toml'
        sentinel.parent.mkdir()
        sentinel.write_bytes(b'foreign settings\n')
        plugin = self.base / 'other-plugin'
        plugin.mkdir()
        (plugin / 'manifest.json').write_bytes(b'foreign plugin\n')
        self.install()
        self.install(self.second, self.first['identity'])
        S.rollback(self.store, self.second['identity'], self.first['identity'])
        self.assertEqual(sentinel.read_bytes(), b'foreign settings\n')
        self.assertEqual((plugin / 'manifest.json').read_bytes(), b'foreign plugin\n')

    def test_tampered_ownership_refused(self):
        self.install()
        owner = json.loads((self.store / S.OWNER).read_bytes())
        owner['activation'] = 'enabled'
        (self.store / S.OWNER).write_bytes(P.encoded(owner))
        with self.assertRaises(S.StoreError):
            S.current(self.store, self.first['identity'])
        with self.assertRaises(S.StoreError):
            self.install(self.second, self.first['identity'])

    def test_boolean_schema_is_not_integer_schema(self):
        self.install()
        state = self.state()
        state['schema'] = True
        self.rewrite_state(state)
        with self.assertRaises(S.StoreError):
            S.current(self.store, self.first['identity'])

    def test_invalid_state_and_binding_refused(self):
        self.install()
        original = self.state()
        for change in ('extra', 'version', 'commit', 'identity', 'null', 'same'):
            state = json.loads(json.dumps(original))
            if change == 'extra':
                state['authority'] = True
            elif change in ('version', 'commit', 'identity'):
                state['current'][change] = '../escape'
            elif change == 'null':
                state['previous'] = state['current']
                state['current'] = None
            else:
                state['previous'] = state['current']
            self.rewrite_state(state)
            with self.subTest(change=change), self.assertRaises(S.StoreError):
                S.current(self.store, self.first['identity'])
        self.rewrite_state(original)

    def test_duplicate_json_keys_refused(self):
        self.install()
        state_path = self.store / S.STATE
        original = state_path.read_bytes()
        state_path.write_bytes(original.replace(b'"schema": 1', b'"schema": 1, "schema": 1'))
        with self.assertRaises(S.StoreError):
            S.current(self.store, self.first['identity'])

    def test_selected_content_tampering_blocks_read_and_update(self):
        first = self.install()
        (Path(first['root']) / 'kitlib' / 'fixture.py').write_bytes(b'tampered\n')
        with self.assertRaises(S.StoreError):
            S.current(self.store, self.first['identity'])
        with self.assertRaises(S.StoreError):
            self.install(self.second, self.first['identity'])
        self.assertEqual(self.state()['current']['identity'], self.first['identity'])

    def test_changed_previous_release_blocks_rollback(self):
        first = self.install()
        self.install(self.second, self.first['identity'])
        (Path(first['root']) / 'tools' / 'fixture.py').write_bytes(b'tampered\n')
        with self.assertRaises(S.StoreError):
            S.rollback(self.store, self.second['identity'], self.first['identity'])
        self.assertEqual(self.state()['current']['identity'], self.second['identity'])

    def test_unknown_target_and_same_target_refused(self):
        self.install()
        for target in ('f' * 64, self.first['identity']):
            with self.subTest(target=target), self.assertRaises(S.StoreError):
                S.rollback(self.store, self.first['identity'], target)
        self.assertEqual(self.state()['current']['identity'], self.first['identity'])

    def test_single_writer_refused_without_breaking_lock(self):
        self.install()
        with P.exclusive(self.store):
            with self.assertRaises(S.StoreError):
                self.install(self.second, self.first['identity'])
            with self.assertRaises(S.StoreError):
                S.rollback(self.store, self.first['identity'], self.second['identity'])
            self.assertTrue((self.store / '.kit-prepare.lock').exists())
        self.assertFalse((self.store / '.kit-prepare.lock').exists())

    def test_failed_pointer_publication_leaves_old_selection_and_inactive_version(self):
        self.install()
        with mock.patch.object(S.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.install(self.second, self.first['identity'])
        self.assertEqual(S.current(self.store, self.first['identity'])['identity'], self.first['identity'])
        self.assertEqual(len(list((self.store / 'releases').iterdir())), 2)
        self.assertFalse(any(path.name.startswith('.kit-state-') for path in self.store.iterdir()))
        retry = self.install(self.second, self.first['identity'])
        self.assertEqual(retry['identity'], self.second['identity'])

    def test_unrecognized_store_and_version_entries_refused(self):
        self.install()
        for parent in (self.store, self.store / 'releases'):
            unknown = parent / 'foreign'
            unknown.mkdir()
            with self.subTest(parent=parent), self.assertRaises(S.StoreError):
                S.current(self.store, self.first['identity'])
            unknown.rmdir()

    def test_existing_wrong_version_never_overwritten(self):
        self.install()
        destination = self.store / 'releases' / (self.second['commit'] + '-' + self.second['identity'])
        destination.mkdir()
        keep = destination / 'foreign.txt'
        keep.write_bytes(b'keep')
        with self.assertRaises(S.StoreError):
            self.install(self.second, self.first['identity'])
        self.assertEqual(keep.read_bytes(), b'keep')
        self.assertEqual(self.state()['current']['identity'], self.first['identity'])

    def test_overlapping_artifact_and_store_refused(self):
        artifact = Path(self.first['root'])
        for store in (artifact, artifact / 'store', artifact.parent):
            with self.subTest(store=store), self.assertRaises(S.StoreError):
                S.install(store, artifact, self.first['identity'])

    def test_relative_paths_refused(self):
        with self.assertRaises(S.StoreError):
            S.install('relative', Path(self.first['root']), self.first['identity'])
        with self.assertRaises(S.StoreError):
            S.install(self.store, 'relative', self.first['identity'])

    def test_runtime_store_uses_no_git_commands(self):
        with mock.patch.object(P.cmd, 'run', side_effect=AssertionError('Git unavailable')):
            self.install()
            self.install(self.second, self.first['identity'])
            S.current(self.store, self.second['identity'])
            S.rollback(self.store, self.second['identity'], self.first['identity'])

    def test_reparse_store_path_refused_before_read(self):
        self.install()
        original = Path.lstat

        def metadata(path, *args, **kwargs):
            if path == self.store:
                return mock.Mock(st_mode=0, st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'lstat', metadata), self.assertRaises(S.StoreError):
            S.current(self.store, self.first['identity'])
