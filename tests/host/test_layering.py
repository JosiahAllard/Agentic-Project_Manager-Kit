"""The layer rule (ARCHITECTURE.md "Hosts"): kitlib never imports a host folder or an optional
package. Every import statement under kitlib/ is read with ast, function-level ones included, so a
late import cannot slip past. tools/ may import a host (tools/pm_watch.py does); kitlib may not."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FORBIDDEN = ('claude', 'codex', 'hosts', 'optional')


def imported(tree):
    """(line, top-level package name) of every absolute import in `tree`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split('.')[0]
        elif (isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'import_module'
              and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)):
            yield node.lineno, node.args[0].value.split('.')[0]


def kitlib_files():
    for base, dirs, files in os.walk(os.path.join(ROOT, 'kitlib')):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in files:
            if name.endswith('.py'):
                yield os.path.join(base, name)


class Layering(unittest.TestCase):
    def test_no_kitlib_module_imports_a_host_or_an_optional_package(self):
        found = []
        for path in kitlib_files():
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            for line, top in imported(tree):
                if top in FORBIDDEN:
                    found.append('%s:%d imports %s' % (os.path.relpath(path, ROOT), line, top))
        self.assertEqual(found, [])

    def test_the_scan_sees_each_form_of_import(self):
        source = ('import claude.adapter\nfrom codex import x\n'
                  'def f():\n    import optional.bridge\n    importlib.import_module("hosts.y")\n'
                  'from . import claude\n')
        self.assertEqual(sorted(top for _, top in imported(ast.parse(source))),
                         ['claude', 'codex', 'hosts', 'optional'])

    def test_kitlib_host_has_no_kitlib_hosts_twin(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'kitlib', 'hosts')))


if __name__ == '__main__':
    unittest.main()
