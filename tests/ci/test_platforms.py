"""tests/platforms.py: windows_only and posix_only record their reason and skip off their platform."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests import platforms

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def skipped(obj):
    """(skipped, why) of a decorated class or function, as unittest reads it."""
    return (bool(getattr(obj, '__unittest_skip__', False)),
            getattr(obj, '__unittest_skip_why__', None))


class Markers(unittest.TestCase):

    def forget(self, key):
        self.addCleanup(platforms.REASONS.pop, key, None)

    def test_the_reason_is_recorded_for_a_class_and_a_method(self):
        @platforms.windows_only('a junction is a Windows feature')
        class Marked(unittest.TestCase):
            @platforms.posix_only('a file mode bit')
            def test_m(self):
                pass
        key = '%s.%s' % (__name__, Marked.__qualname__)
        self.forget(key)
        self.forget(key + '.test_m')
        self.assertTrue(key.endswith('<locals>.Marked'), key)
        self.assertEqual(platforms.REASONS[key], ('windows', 'a junction is a Windows feature'))
        self.assertEqual(platforms.REASONS[key + '.test_m'], ('posix', 'a file mode bit'))

    def test_a_module_level_key_is_module_and_qualname(self):
        """A scratch module, so the suite's real REASONS keep only the suite's markers."""
        source = ('from tests import platforms\n'
                  '@platforms.windows_only("the probe")\n'
                  'class Probe:\n'
                  '    @platforms.posix_only("the probe")\n'
                  '    def probe(self):\n'
                  '        pass\n')
        key = 'tests.ci.scratch_probe.Probe'
        self.forget(key)
        self.forget(key + '.probe')
        exec(compile(source, '<scratch_probe>', 'exec'), {'__name__': 'tests.ci.scratch_probe'})
        self.assertEqual(platforms.REASONS[key], ('windows', 'the probe'))
        self.assertEqual(platforms.REASONS[key + '.probe'], ('posix', 'the probe'))

    def test_it_skips_off_its_platform(self):
        for flag, marker in (('WINDOWS', platforms.windows_only), ('POSIX', platforms.posix_only)):
            for on in (True, False):
                with self.subTest(flag=flag, on=on), mock.patch.object(platforms, flag, on):
                    def test_f(self):
                        pass

                    class C(unittest.TestCase):
                        pass
                    for obj in (test_f, C):
                        self.forget('%s.%s' % (obj.__module__, obj.__qualname__))
                    f, c = marker('why %s' % flag)(test_f), marker('why %s' % flag)(C)
                    want = (False, None) if on else (True, 'why %s' % flag)
                    self.assertEqual(skipped(f), want)
                    self.assertEqual(skipped(c), want)

    def test_a_skipped_test_reports_its_reason(self):
        class C(unittest.TestCase):
            def test_x(self):
                raise AssertionError('ran')
        self.forget('%s.%s' % (C.__module__, C.__qualname__))
        with mock.patch.object(platforms, 'WINDOWS', False):
            C = platforms.windows_only('no junctions here')(C)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(C).run(result)
        self.assertEqual(result.failures + result.errors, [])
        self.assertEqual([why for _t, why in result.skipped], ['no junctions here'])

    def test_an_empty_reason_is_refused(self):
        for reason in ('', '   ', None, 3):
            for marker in (platforms.windows_only, platforms.posix_only):
                with self.subTest(reason=reason, marker=marker.__name__):
                    with self.assertRaises(ValueError):
                        marker(reason)

    def test_importing_it_loads_no_kitlib(self):
        """A -I child loads the file by path (the tests package itself imports kitlib)."""
        code = ('import importlib.util, json, sys\n'
                'spec = importlib.util.spec_from_file_location("platforms", sys.argv[1])\n'
                'm = importlib.util.module_from_spec(spec)\n'
                'spec.loader.exec_module(m)\n'
                'assert m.windows_only and m.posix_only\n'
                'print(json.dumps(sorted(n for n in sys.modules if n.split(".")[0] == "kitlib")))\n')
        r = subprocess.run([sys.executable, '-I', '-c', code,
                            os.path.join(KIT, 'tests', 'platforms.py')],
                           cwd=KIT, capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), [])


if __name__ == '__main__':
    unittest.main()
