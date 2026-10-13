from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hosts.codex.adapter.sessions import native_codex


class NativeExecutable(unittest.TestCase):
    def test_explicit_exe_and_environment_binding(self):
        with tempfile.TemporaryDirectory() as scratch:
            binary = Path(scratch) / "codex.exe"
            binary.touch()
            self.assertEqual(native_codex(binary), str(binary.resolve()))
            self.assertEqual(native_codex(environ={"PM_KIT_CODEX_EXE": str(binary)}), str(binary.resolve()))

    def test_shims_missing_and_relative_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            shim = Path(scratch) / "codex.cmd"
            shim.touch()
            for candidate in (shim, Path(scratch) / "absent.exe", "codex.exe"):
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    native_codex(candidate)
        with mock.patch("shutil.which", return_value=None), self.assertRaises(ValueError):
            native_codex(environ={})
