from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest import mock

from hosts.codex.adapter.command_canary import _remove_fixtures, _write_probe, probe
from tests.platforms import windows_only


class Canary(unittest.TestCase):
    def test_expected_native_rpc_denial_is_a_negative_write_probe(self):
        host = mock.Mock()
        host.run_argv.side_effect = RuntimeError('sandbox denied exec error: PermissionError')
        marker = mock.Mock()
        marker.exists.return_value = False
        self.assertEqual(_write_probe(host, Path.cwd(), marker, False), 'denied')

    def test_native_transport_failure_is_never_claimed_as_denial(self):
        host = mock.Mock()
        host.run_argv.side_effect = RuntimeError('connection closed')
        with self.assertRaisesRegex(RuntimeError, 'connection closed'):
            _write_probe(host, Path.cwd(), mock.Mock(), False)

    def test_writable_permission_failure_is_never_claimed_as_success(self):
        host = mock.Mock()
        host.run_argv.side_effect = RuntimeError('sandbox denied exec error: PermissionError')
        with self.assertRaises(RuntimeError):
            _write_probe(host, Path.cwd(), mock.Mock(), True)

    def test_file_written_under_read_only_permissions_fails_the_canary(self):
        host = mock.Mock()
        host.run_argv.return_value = {'exitCode': 0, 'stdout': '', 'stderr': ''}
        marker = mock.Mock()
        marker.exists.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'write behavior'):
            _write_probe(host, Path.cwd(), marker, False)

    def test_system_temp_and_relative_fixtures_are_refused_before_execution(self):
        with mock.patch('hosts.codex.adapter.command_canary.native_codex', return_value='unused'), \
                mock.patch('hosts.codex.adapter.command_canary._scenario') as scenario:
            for path in (None, 'relative', tempfile.gettempdir()):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    probe(fixture_root=path)
            scenario.assert_not_called()

    def test_cleanup_cannot_remove_a_different_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder).resolve()
            innocent = parent / 'keep'
            innocent.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'cleanup target'):
                _remove_fixtures(innocent, parent)
            self.assertTrue(innocent.is_dir())

    @windows_only('Windows Git read-only file attribute')
    def test_cleanup_removes_git_read_only_objects_only_in_the_fixture(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder).resolve()
            fixture = parent / 'pmkit-native-fixtures-test'
            fixture.mkdir()
            obj = fixture / 'object'
            obj.write_bytes(b'fake object')
            obj.chmod(stat.S_IREAD)
            _remove_fixtures(fixture, parent)
            self.assertFalse(fixture.exists())
