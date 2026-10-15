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
                'print([x * 2 for x in range(3)])'):
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
