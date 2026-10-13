from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hosts.codex.adapter.permissions_canary import (DENIED_EXIT, DENIED_MARKER, READ_MARKER,
                                                fixture_directory, read_probe, write_probe)


class PermissionsCanary(unittest.TestCase):
    def test_temp_and_relative_roots_are_refused(self):
        for value in (None, 'relative', tempfile.gettempdir()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fixture_directory(value)

    def test_transport_errors_do_not_prove_denial(self):
        host = mock.Mock()
        host.run_argv.side_effect = RuntimeError('connection closed')
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(RuntimeError, 'connection closed'):
            write_probe(host, Path(folder) / 'marker', False)

    def test_permission_error_is_denial_only_for_negative_probe(self):
        host = mock.Mock()
        host.run_argv.side_effect = RuntimeError('sandbox denied exec error: PermissionError')
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'marker'
            self.assertEqual(write_probe(host, marker, False), 'denied')
            with self.assertRaises(RuntimeError):
                write_probe(host, marker, True)

    def test_existing_owner_file_stays_unchanged_on_denial(self):
        host = mock.Mock()
        host.run_argv.return_value = {'exitCode': DENIED_EXIT, 'stdout': DENIED_MARKER, 'stderr': ''}
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'AGENTS.md'
            marker.write_text('owner text', encoding='utf-8')
            self.assertEqual(write_probe(host, marker, False, existing=True), 'denied')
            self.assertEqual(marker.read_text(encoding='utf-8'), 'owner text')

    def test_existing_file_change_fails_even_when_native_result_claims_denial(self):
        host = mock.Mock()
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'AGENTS.md'
            marker.write_text('owner text', encoding='utf-8')
            def mutate(*_):
                marker.write_text('changed', encoding='utf-8')
                return {'exitCode': 1}
            host.run_argv.side_effect = mutate
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                write_probe(host, marker, False, existing=True)

    def test_existing_file_requires_explicit_overwrite_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'keep'
            marker.touch()
            host = mock.Mock()
            with self.assertRaisesRegex(ValueError, 'must not exist'):
                write_probe(host, marker, False)
            host.run_argv.assert_not_called()

    def test_unrelated_command_errors_are_not_permission_denials(self):
        host = mock.Mock()
        for result in ({'exitCode': 1},
                       {'exitCode': 1, 'stdout': '', 'stderr': 'ModuleNotFoundError'},
                       {'exitCode': DENIED_EXIT, 'stdout': '', 'stderr': ''},
                       {'exitCode': DENIED_EXIT, 'stdout': DENIED_MARKER, 'stderr': 'startup failed'}):
            with tempfile.TemporaryDirectory() as folder, self.subTest(result=result):
                host.run_argv.return_value = result
                with self.assertRaisesRegex(RuntimeError, 'differs'):
                    write_probe(host, Path(folder) / 'marker', False)

    def test_reads_require_checked_contents_marker_and_success(self):
        host = mock.Mock()
        for result in ({'exitCode': 1, 'stdout': READ_MARKER, 'stderr': ''},
                       {'exitCode': 0, 'stdout': 'stat-ok', 'stderr': ''},
                       {'exitCode': 0, 'stdout': READ_MARKER, 'stderr': 'read failed'}):
            with self.subTest(result=result):
                host.run_argv.return_value = result
                with self.assertRaisesRegex(RuntimeError, 'content read failed'):
                    read_probe(host, [(Path.cwd() / 'fixture', 'known contents')])
        host.run_argv.return_value = {'exitCode': 0, 'stdout': READ_MARKER + '\n', 'stderr': ''}
        self.assertEqual(read_probe(host, [(Path.cwd() / 'fixture', 'known contents')]),
                         'enumerated and contents verified')
