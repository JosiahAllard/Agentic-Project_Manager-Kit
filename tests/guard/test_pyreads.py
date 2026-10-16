"""rules.py_program and rules.py_reads_only: a python -c counts as no write only when the guard
can prove from its text that it reads only (docs/guard.md "Shell writes")."""
import unittest

from kitlib.guard import rules
from kitlib.guard import shell as sh


def words(*texts, dyn=()):
    return [sh.Word(t, i in dyn, 0, 0) for i, t in enumerate(texts)]


def proved(code, flags='I'):
    return rules.py_reads_only(sh.Word(code, False, 0, 0), set(flags))


class PyProgram(unittest.TestCase):
    def test_kinds(self):
        cases = [
            (('-I', '-c', 'print(1)'), 'code', 'print(1)', {'I'}),
            (('-Ic', 'print(1)'), 'code', 'print(1)', {'I'}),
            (('-Icprint(1)',), 'code', 'print(1)', {'I'}),
            (('-cprint(1)',), 'code', 'print(1)', set()),
            (('-X', 'dev', '-c', 'x'), 'code', 'x', {'?'}),
            (('-Xdev', '-c', 'x'), 'code', 'x', {'?'}),
            (('--check-hash-based-pycs', 'always', '-c', 'x'), 'code', 'x', {'?'}),
            (('-m', 'json.tool'), 'module', 'json.tool', set()),
            (('-um', 'json.tool'), 'module', 'json.tool', {'u'}),
            (('-',), 'stdin', None, set()),
            (('script.py', '-c'), 'script', None, set()),
            (('--', '-c'), 'script', None, set()),
            ((), 'none', None, set()),
            (('-c',), 'code', None, set()),
        ]
        for args, kind, value, flags in cases:
            with self.subTest(args):
                got = rules.py_program('python', words(*args))
                self.assertEqual((got[0], got[1].text if got[1] is not None else None, got[2]), (kind, value, flags))

    def test_py_launcher_version(self):
        self.assertEqual(rules.py_program('py', words('-3.12', '-I', '-c', 'x'))[0::2], ('code', {'I'}))
        self.assertIn('3', rules.py_program('python', words('-3', '-c', 'x'))[2])


class PyReadsOnly(unittest.TestCase):
    def test_proved(self):
        for code in (
                'import json,sys; d=json.load(open(sys.argv[1])); print(len(d))',
                "import json; print(json.load(open('x.json', encoding='utf-8'))['a'])",
                "with open('x', 'rb') as f: print(len(f.read()))",
                "with open('x', mode='rt', errors='replace') as f: print(f.readline())",
                'from os.path import exists, getsize; print(exists("x") and getsize("x"))',
                'import re, collections; print(collections.Counter(re.findall(r"\\w+", open("x").read())))',
                'import json, sys; json.dump(json.load(sys.stdin), sys.stdout, indent=1)',
                'import sys; sys.stdout.write(open(sys.argv[1]).read()); sys.exit(0)',
                'print([x * 2 for x in range(3)])',
                'import sys\nfor line in sys.stdin:\n    if line.strip(): print(line.split()[0])',
                'd = {}; d["a"] = 1; print(sorted(d.items(), key=len), f"{len(d):>3}")',
                "import re; m = re.search('a', 'ab'); print(m and m.group(0))"):
            with self.subTest(code):
                self.assertTrue(proved(code))

    def test_not_proved(self):
        for code in (
                "open('x', 'w').write('y')",
                "open('x', 'a')",
                "open('x', 'r+')",
                "open('x', mode='x')",
                'open("x", m)',
                'open("x", **k)',
                'open(*a)',
                'open("x", "r", opener=f)',
                'f = open; f("x", "w")',
                'import os; os.remove("x")',
                'import os.path; os.remove("x")',
                'from os import remove; remove("x")',
                'from os.path import *',
                'import shutil',
                'import subprocess',
                'import pathlib; pathlib.Path("x").write_text("y")',
                'import sys; sys.modules["os"].remove("x")',
                'import json; json.codecs.open("x", "w")',
                'import json as j; k = j; k.codecs',
                'import json; print(json)',
                'import sys; sys = 1',
                "exec('print(1)')",
                "eval('1')",
                "compile('1', 'x', 'exec')",
                "getattr(__builtins__, 'open')('x', 'w')",
                '__import__("os").remove("x")',
                'print(().__class__.__base__.__subclasses__())',
                'g = (x for x in [1]); print(g.gi_frame.f_globals)',
                'from . import x',
                'breakpoint()',
                'print(1'):
            with self.subTest(code):
                self.assertFalse(proved(code))

    def test_not_proved_beyond_names_and_attributes(self):
        """Syntax that binds or reaches an attribute with no Name or Attribute node, a type
        reached from a value, a value attribute off the allowlist, a denied name as an attribute
        or keyword, and a new name a def or lambda brings (docs/guard.md "Shell writes")."""
        for code in (
                'match 1:\n case int(_a=y): pass',              # a class pattern's keyword attribute
                'match 1:\n case y: print(y)',                  # a capture pattern binds a name
                'match [1]:\n case [*y]: pass',
                "type(open('x'))('y', 'w')",                    # the type of a read file object
                "print(type(1))",
                "object",
                "open('x').buffer.raw",                         # a file object's raw I/O object
                "f = open('x'); print(f.name)",
                "import sys; sys.stdin.buffer",
                "x = 1; x.exec('y')",                           # a denied name as an attribute
                "x = 1; x.eval",
                "x = 1; x.open('y')",
                "x = 1; x.compile",
                "print(1, exec=2)",
                'def f(_a): pass',                              # a def's or lambda's underscore arg
                'print((lambda _a: 0)(1))',
                'def f(a): pass',
                'class C: pass',
                'try:\n print(1)\nexcept Exception: pass',
                'while 0: pass',
                'global x',
                'del x',
                '(x := 1)',
                'x = 1\nprint(x.real)',                         # a value attribute off the allowlist
                'print(undefined)',                             # a name never bound
                'iter = 1',                                     # a builtin rebound
                'if 0: id = 1\nprint(id(1))',
                'from json import load as print',
                'import json as str',
                'x = 1; x.f_globals',
                'f_x = 1',
                'import json as m; import re as m; print(m.dumps(1))',   # one name, two modules
                'import json; from json import load as json',
                'from json import load; from json import dumps as load',
                'from json import load, load'):
            with self.subTest(code):
                self.assertFalse(proved(code))

    def test_builtin_names_fixed(self):
        """The builtins a stored name may not shadow are a list in the code, not only the running
        python's: a name a newer python adds is refused on any python."""
        self.assertLessEqual({'aiter', 'anext', 'breakpoint', 'ExceptionGroup', 'EncodingWarning',
                              'PythonFinalizationError'}, rules.PY_BUILTIN_NAMES)
        self.assertLessEqual(rules.PY_BUILTIN_NAMES, rules.PY_SHADOWED)
        for code in ('PythonFinalizationError = 1', 'anext = 1', 'import json as aiter'):
            with self.subTest(code):
                self.assertFalse(proved(code))

    def test_powershell_double_quote(self):
        """Through PowerShell or cmd.exe a double quote or a backslash in the code is not proved:
        Windows PowerShell 5.1's native-argument quoting, and cmd.exe's quote reading, can hand
        python other text than the guard read."""
        code = sh.Word('print("x")', False, 0, 0)
        self.assertTrue(rules.py_reads_only(code, {'I'}))
        self.assertFalse(rules.py_reads_only(code, {'I'}, ps=True))
        self.assertTrue(rules.py_reads_only(sh.Word("print('x')", False, 0, 0), {'I'}, ps=True))
        for text in ("print('a b') #\\", "print('\\\\')", "print(1)\\"):
            with self.subTest(text):
                self.assertFalse(rules.py_reads_only(sh.Word(text, False, 0, 0), {'I'}, ps=True))
        self.assertTrue(rules.py_reads_only(sh.Word("print('a b') #\\", False, 0, 0), {'I'}))

    def test_ps_native_unsure(self):
        """The words Windows PowerShell 5.1 may hand a program as other than one argument: one
        holding a double quote, an empty one, and one with a blank that ends in a backslash."""
        for text in ('a"b', '"', '', 'a b\\', ' \\'):
            with self.subTest(text=text):
                self.assertTrue(rules.ps_native_unsure(sh.Word(text, False, 0, 0)))
        for text in ('print(1)', 'a b', 'a\\b', 'ab\\', ' ', "it's"):
            with self.subTest(text=text):
                self.assertFalse(rules.ps_native_unsure(sh.Word(text, False, 0, 0)))

    def test_empty_code(self):
        """Empty or blank code is not proved, through any shell: Windows PowerShell 5.1 drops an
        empty native argument, so python takes the next word as its code."""
        for text in ('', ' ', '\n'):
            for ps in (False, True):
                with self.subTest(text=text, ps=ps):
                    self.assertFalse(rules.py_reads_only(sh.Word(text, False, 0, 0), {'I'}, ps=ps))

    def test_flags(self):
        code = 'import json; print(1)'
        self.assertFalse(proved(code, ''))               # no -I: the working directory comes first on sys.path
        self.assertFalse(proved(code, 'E'))
        self.assertFalse(proved(code, 'I?'))             # a -X or -W value, or a long option
        self.assertFalse(proved(code, 'Ii'))             # -i: then reads code from standard input
        self.assertTrue(proved(code, 'IEsBu'))

    def test_text_not_literal(self):
        self.assertFalse(rules.py_reads_only(sh.Word('print(1)', True, 0, 0), {'I'}))
        self.assertFalse(rules.py_reads_only(None, {'I'}))


if __name__ == '__main__':
    unittest.main()
