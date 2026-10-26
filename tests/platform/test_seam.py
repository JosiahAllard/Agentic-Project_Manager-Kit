"""kitlib.platform, the seam: NAME, the backend each platform gets, and the contract every
backend provides in full (docs/linux-support-plan.md section 2.1)."""
import importlib
import unittest
from unittest import mock

import kitlib.platform
from kitlib import platform
from kitlib.platform import api
from tests.platforms import windows_only


class Name(unittest.TestCase):
    def test_each_sys_platform_maps_to_one_name(self):
        for raw, name in (('win32', 'windows'), ('linux', 'linux'), ('linux2', 'linux'),
                          ('darwin', 'unsupported'), ('cygwin', 'unsupported'),
                          ('freebsd14', 'unsupported')):
            with self.subTest(raw=raw):
                self.assertEqual(platform._pick(raw), name)

    def test_platform_unsupported_is_an_os_error(self):
        self.assertTrue(issubclass(platform.PlatformUnsupported, OSError))


class Backends(unittest.TestCase):
    def test_the_contract_names_each_primitive_once_in_its_area(self):
        names = [n for names, _slice in api.AREAS.values() for n in names]
        self.assertEqual(sorted(names), sorted(api.NAMES))
        self.assertEqual(len(set(names)), len(names))

    @windows_only('the Windows backend binds the real kitlib.proc functions')
    def test_the_windows_backend_is_todays_kitlib_proc_functions(self):
        from kitlib.proc import cpu, process
        windows = importlib.import_module('kitlib.platform.windows')
        for name in api.NAMES:
            with self.subTest(name=name):
                today = getattr(process, name, None) or getattr(cpu, name)
                self.assertIs(getattr(windows, name), today)
        self.assertEqual(sorted(windows.__all__), sorted(api.NAMES))

    def test_the_linux_hardware_primitives_are_the_readers(self):
        linux = importlib.import_module('kitlib.platform.linux')
        hardware = importlib.import_module('kitlib.platform.linux.hardware')
        self.assertEqual(sorted(linux.BUILT), ['hardware', 'process'])
        for name in api.HARDWARE:
            with self.subTest(name=name):
                self.assertIs(getattr(linux, name), getattr(hardware, name))

    def test_every_other_linux_primitive_is_a_stub_naming_itself_and_its_slice(self):
        linux = importlib.import_module('kitlib.platform.linux')
        self.assertEqual(sorted(linux.__all__), sorted(api.NAMES))
        for area, (names, slice_number) in api.AREAS.items():
            if area in linux.BUILT:
                continue
            for name in names:
                with self.subTest(name=name):
                    with self.assertRaisesRegex(platform.PlatformUnsupported,
                                                r'%s is not built on Linux yet \(the %s area, slice %d '
                                                % (name, area, slice_number)):
                        getattr(linux, name)(1, 2)

    def test_a_primitive_resolves_to_the_running_platforms_backend(self):
        linux = importlib.import_module('kitlib.platform.linux')
        with mock.patch.object(kitlib.platform, 'NAME', 'linux'):
            self.assertIs(platform.backend(), linux)
            self.assertIs(kitlib.platform.alive, linux.alive)
        with mock.patch.object(kitlib.platform, 'NAME', 'unsupported'):
            with self.assertRaisesRegex(platform.PlatformUnsupported, 'no backend'):
                platform.backend()
            with self.assertRaises(platform.PlatformUnsupported):
                kitlib.platform.alive

    def test_a_name_outside_the_contract_is_an_attribute_error(self):
        with self.assertRaises(AttributeError):
            kitlib.platform.snapshot
        self.assertFalse(hasattr(kitlib.platform, 'no_such_primitive'))


if __name__ == '__main__':
    unittest.main()
