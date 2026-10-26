"""kitlib.platform.dll: a DLL is loaded at its first use, never at import, and a missing export
fails at the first call that needs it, naming the function (docs/linux-support-plan.md section
2.1, rule 3)."""
import ctypes
import unittest
from unittest import mock

from kitlib import platform
from kitlib.platform import dll
from tests.platforms import windows_only


class FakeDll(object):
    """A loaded DLL that exports `exports` (each a fresh object that records its prototype)."""

    def __init__(self, exports):
        self.exports = set(exports)

    def __getattr__(self, name):
        if name.startswith('_') or name not in self.exports:
            raise AttributeError('function %r not found' % name)
        function = type(name, (object,), {})()
        self.__dict__[name] = function
        return function


class Loader(object):
    def __init__(self, exports):
        self.exports, self.loads = exports, []

    def __call__(self, name, **kw):
        self.loads.append((name, kw))
        return FakeDll(self.exports)


class LazyDLLTest(unittest.TestCase):
    def test_nothing_loads_until_a_function_is_asked_for(self):
        loader = Loader({'Open'})
        lazy = dll.LazyDLL('fake32', {'Open': (ctypes.c_int, [ctypes.c_void_p])}, loader,
                           use_last_error=True)
        self.assertEqual(loader.loads, [])
        self.assertEqual(repr(lazy), '<LazyDLL fake32>')
        function = lazy.Open
        self.assertEqual(loader.loads, [('fake32', {'use_last_error': True})])
        self.assertEqual((function.restype, function.argtypes), (ctypes.c_int, [ctypes.c_void_p]))
        self.assertIs(lazy.Open, function)
        self.assertEqual(len(loader.loads), 1)
        self.assertEqual(repr(lazy), '<LazyDLL fake32 (loaded)>')

    def test_a_removed_export_fails_at_its_first_call_naming_it_and_every_time(self):
        loader = Loader({'Open'})
        lazy = dll.LazyDLL('fake32', {'Open': (None, []), 'Gone': (None, [])}, loader)
        self.assertIsNotNone(lazy.Open)
        for _ in range(2):
            with self.assertRaisesRegex(AttributeError, 'Gone'):
                lazy.Gone
        self.assertIsNotNone(lazy.Open)
        self.assertEqual(len(loader.loads), 1)

    def test_a_function_without_a_prototype_is_left_as_the_dll_gives_it(self):
        lazy = dll.LazyDLL('fake32', None, Loader({'Plain'}))
        self.assertFalse(hasattr(lazy.Plain, 'restype'))

    def test_private_names_never_reach_the_dll(self):
        loader = Loader({'_secret'})
        lazy = dll.LazyDLL('fake32', None, loader)
        self.assertFalse(hasattr(lazy, '_secret'))
        self.assertFalse(hasattr(lazy, '__wrapped__'))
        self.assertEqual(loader.loads, [])

    def test_a_patch_of_a_function_replaces_it_and_its_end_restores_the_dll(self):
        lazy = dll.LazyDLL('fake32', None, Loader({'Open'}))
        real = lazy.Open
        with mock.patch.object(lazy, 'Open', 'fake'):
            self.assertEqual(lazy.Open, 'fake')
        self.assertIs(lazy.Open, real)

    def test_off_windows_a_dll_is_platform_unsupported_naming_it(self):
        with mock.patch.object(dll, '_WINDLL', None):
            lazy = dll.LazyDLL('kernel32')
            with self.assertRaisesRegex(platform.PlatformUnsupported, 'kernel32'):
                lazy.OpenProcess
            with self.assertRaises(OSError):
                dll.windll('user32')


HIDDEN_EXPORT = r'''
import ctypes, json, sys
real = ctypes.WinDLL


class Hiding(object):
    """kernel32 with OpenProcess removed, as on a Windows build that lacks an export."""
    def __init__(self, name, **kw):
        self.__dict__['_real'] = real(name, **kw)

    def __getattr__(self, name):
        if name == 'OpenProcess':
            raise AttributeError('function %r not found' % name)
        return getattr(self._real, name)


ctypes.WinDLL = Hiding
sys.path.insert(0, sys.argv[1])
out = {}
try:
    from kitlib.proc import win32
    out['import'] = 'ok'
except Exception as e:
    out['import'] = repr(e)
else:
    try:
        win32.K32.OpenProcess
        out['call'] = 'no error'
    except AttributeError as e:
        out['call'] = str(e)
    out['other'] = bool(win32.K32.GetCurrentProcessId())
print(json.dumps(out))
'''


class KitBindingsTest(unittest.TestCase):
    @windows_only('the real kernel32, one export hidden')
    def test_a_removed_export_lets_win32_import_and_fails_at_its_first_call(self):
        import json
        import os
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        done = subprocess.run([sys.executable, '-c', HIDDEN_EXPORT, root], cwd=root, text=True,
                              capture_output=True, timeout=60,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(out['import'], 'ok')
        self.assertIn('OpenProcess', out['call'])
        self.assertTrue(out['other'])

    @windows_only('the real kernel32')
    def test_the_kits_kernel32_gets_its_prototype_at_first_use(self):
        from ctypes import wintypes
        from kitlib.proc import win32
        self.assertIsInstance(win32.K32, dll.LazyDLL)
        self.assertEqual(win32.K32.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(tuple(win32.K32.OpenProcess.argtypes),
                         (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD))

    @windows_only('the real kernel32')
    def test_a_pipe_function_binds_its_export_at_the_first_call(self):
        from kitlib.proc import pipe
        self.assertEqual(pipe.close.__name__, 'CloseHandle')
        with mock.patch.object(pipe, 'K', dll.LazyDLL('kernel32', None, Loader(set()))):
            with self.assertRaisesRegex(AttributeError, 'NoSuchExport'):
                pipe._later(pipe.K, 'NoSuchExport', [], None)()


if __name__ == '__main__':
    unittest.main()
