"""optional/bridge/hosts/claude/records.py: Claude's registry records, bounded and strict.

Covers duplicate keys, non-finite numbers and size limits in a registry record
(read_record)."""
from pathlib import Path
import tempfile
import unittest

from kitlib.proc.errors import ProcError
from optional.bridge.hosts.claude.records import MAX_RECORD_BYTES, read_record


class RegistryRecords(unittest.TestCase):
    def test_registry_duplicate_nonfinite_and_size_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            for data in (b'{"pid":1,"pid":2}', b'{"pid":NaN}', b'[]', b'{}\n{}', b'x' * 8193):
                with self.subTest(data=data[:20]):
                    path.write_bytes(data)
                    with self.assertRaises(ProcError):
                        read_record(path)
            path.write_bytes(b'{"pid":42}\n')
            self.assertEqual(read_record(path), {'pid': 42})

    def test_a_record_at_the_limit_is_read_and_one_byte_more_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            body = b'{"pad":"' + b'x' * (MAX_RECORD_BYTES - 10) + b'"}'
            self.assertEqual(len(body), MAX_RECORD_BYTES)
            path.write_bytes(body)
            self.assertEqual(len(read_record(path)['pad']), MAX_RECORD_BYTES - 10)
            path.write_bytes(body + b' ')
            with self.assertRaisesRegex(ProcError, 'registry-record-limit'):
                read_record(path)

    def test_a_folder_is_not_a_record(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ProcError, 'nonregular-registry-record'):
                read_record(directory)


if __name__ == '__main__':
    unittest.main()
