"""A Windows-only test says so with tests/platforms.py `windows_only(reason)`, never with a
skipUnless or skipIf of its own on os.name, sys.platform or WINDOWS, nor a unittest.skip under an
`if` on them (docs/test-tools.md "Platform markers and the ratchet"): the ratchet counts only the
markers' reasons."""
import ast
import os
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIPS = {'skipUnless', 'skipIf'}


def names_platform(node):
    """Whether the expression `node` reads os.name, sys.platform or a name WINDOWS."""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and (n.value.id, n.attr) in (('os', 'name'), ('sys', 'platform')):
            return True
        if isinstance(n, ast.Name) and n.id == 'WINDOWS':
            return True
    return False


def callee(node):
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def hand_skips(tree):
    """Line numbers of the hand-written platform skips in a parsed module."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and callee(node) in SKIPS and node.args \
                and names_platform(node.args[0]):
            found.append(node.lineno)
        if isinstance(node, ast.If) and names_platform(node.test):
            for inner in node.body + node.orelse:
                for n in ast.walk(inner):
                    if isinstance(n, ast.Call) and callee(n) == 'skip':
                        found.append(n.lineno)
    return sorted(set(found))


def scan(root):
    """'rel:line' of every hand-written platform skip under root/tests, and the files read."""
    found, scanned = [], 0
    for base, dirs, files in os.walk(os.path.join(root, 'tests')):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            path = os.path.join(base, name)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            scanned += 1
            rel = os.path.relpath(path, root).replace(os.sep, '/')
            found.extend('%s:%d' % (rel, line) for line in hand_skips(tree))
    return found, scanned


class HandWrittenPlatformSkips(unittest.TestCase):

    def test_none_under_tests(self):
        found, scanned = scan(KIT)
        self.assertGreater(scanned, 500)
        self.assertEqual(found, [], 'use tests/platforms.py windows_only(reason) instead')

    def test_each_form_is_found(self):
        source = '\n'.join([
            "import os, sys, unittest",
            "WINDOWS = os.name == 'nt'",
            "@unittest.skipUnless(os.name == 'nt', 'r')",                      # 3
            "class A(unittest.TestCase):",
            "    @unittest.skipIf(sys.platform != 'win32', 'r')",              # 5
            "    def test_a(self): pass",
            "    @skipUnless(WINDOWS and HAVE, 'r')",                          # 7
            "    def test_b(self): pass",
            "    if os.name != 'nt':",
            "        test_b = unittest.skip('r')(test_b)",                     # 10
            "@unittest.skipUnless(DRIVE, 'not a platform test')",
            "class B(unittest.TestCase): pass",
        ])
        self.assertEqual(hand_skips(ast.parse(source)), [3, 5, 7, 10])


if __name__ == '__main__':
    unittest.main()
