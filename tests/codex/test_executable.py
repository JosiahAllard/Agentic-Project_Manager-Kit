import os
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

    def test_off_path_the_desktop_apps_newest_codex_exe_is_found(self):
        """No PM_KIT_CODEX_EXE, none on PATH: the newest by mtime of the desktop app's
        LOCALAPPDATA/OpenAI/Codex/bin/<hash>/codex.exe; a hash folder without one is skipped."""
        with tempfile.TemporaryDirectory() as local:
            bin_dir = Path(local) / "OpenAI" / "Codex" / "bin"
            old, new, other = bin_dir / "aaa" / "codex.exe", bin_dir / "bbb" / "codex.exe", bin_dir / "ccc" / "rg.exe"
            for exe, when in ((old, 1000), (new, 2000), (other, 3000)):
                exe.parent.mkdir(parents=True)
                exe.touch()
                os.utime(exe, (when, when))
            env = {"LOCALAPPDATA": local, "PATH": ""}
            self.assertEqual(native_codex(environ=env), str(new.resolve()))
            os.utime(old, (4000, 4000))
            self.assertEqual(native_codex(environ=env), str(old.resolve()))
            self.assertEqual(native_codex(environ=dict(env, PM_KIT_CODEX_EXE=str(new))), str(new.resolve()))
