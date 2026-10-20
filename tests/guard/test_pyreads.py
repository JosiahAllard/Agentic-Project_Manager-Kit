"""rules.py_program: how python reads its program. Code on the line (-c) and a program read from
standard input count as a write of the directory python runs in, whatever the code is; the guard
does not read it (docs/guard.md "Shell writes"). rules.ps_native_unsure: the PowerShell words
that make any python run such a write."""
import unittest

from kitlib.guard import rules
from kitlib.guard import shell as sh


def words(*texts, dyn=()):
    return [sh.Word(t, i in dyn, 0, 0) for i, t in enumerate(texts)]


class PyProgram(unittest.TestCase):
    def test_kinds(self):
        cases = [
            (('-I', '-c', 'print(1)'), 'code', 'print(1)'),
            (('-Ic', 'print(1)'), 'code', 'print(1)'),
            (('-Icprint(1)',), 'code', 'print(1)'),
            (('-cprint(1)',), 'code', 'print(1)'),
            (('-X', 'dev', '-c', 'x'), 'code', 'x'),
            (('-Xdev', '-c', 'x'), 'code', 'x'),
            (('--check-hash-based-pycs', 'always', '-c', 'x'), 'code', 'x'),
            (('-m', 'json.tool'), 'module', 'json.tool'),
            (('-um', 'json.tool'), 'module', 'json.tool'),
            (('-',), 'stdin', None),
            (('-I', '-'), 'stdin', None),
            (('script.py', '-c'), 'script', None),
            (('--', '-c'), 'script', None),
            ((), 'none', None),
            (('-c',), 'code', None),
        ]
        for args, kind, value in cases:
            with self.subTest(args):
                got = rules.py_program('python', words(*args))
                self.assertEqual((got[0], got[1].text if got[1] is not None else None), (kind, value))

    def test_py_launcher_version(self):
        self.assertEqual(rules.py_program('py', words('-3.12', '-I', '-c', 'x'))[0], 'code')
        self.assertEqual(rules.py_program('py', words('-3', 'x.py'))[0], 'script')
        self.assertEqual(rules.py_program('python', words('-3', '-c', 'x'))[0], 'code')


class PsNativeUnsure(unittest.TestCase):
    def test_ps_native_unsure(self):
        """The words Windows PowerShell 5.1 may hand a program as other than one argument: one
        holding a double quote, an empty one, and one with a blank that ends in a backslash."""
        for text in ('a"b', '"', '', 'a b\\', ' \\'):
            with self.subTest(text=text):
                self.assertTrue(rules.ps_native_unsure(sh.Word(text, False, 0, 0)))
        for text in ('print(1)', 'a b', 'a\\b', 'ab\\', ' ', "it's"):
            with self.subTest(text=text):
                self.assertFalse(rules.ps_native_unsure(sh.Word(text, False, 0, 0)))


class NoProver(unittest.TestCase):
    def test_the_guard_proves_no_code_reads_only(self):
        """The read-only prover is gone (docs/guard-v2.md, the python program proving row)."""
        for name in ('py_reads_only', 'PY_READ_MODULES', 'PY_C_HINT', 'sleep_wait', 'py_only_sleeps'):
            with self.subTest(name):
                self.assertFalse(hasattr(rules, name))


if __name__ == '__main__':
    unittest.main()
