"""The hub package as a whole: its docstring lists its modules, and its tests start their child
programs (node for the page, python children for the store's lock and crash tests) through
kitlib.proc.spawn, so a hub test run from a parent with no console opens no console window.

The starts are read from the source of every .py file in this folder with the reader of
tests/proc/spawnscan.py (scan_source); tests/proc/test_spawn_sites.py says what it finds and what
it cannot see. A start made by a helper outside this folder (tests/gitfixture's git) is not read
here."""
import ast
import os
import unittest

from kitlib import hubsite
from tests.proc.spawnscan import scan_source

HERE = os.path.dirname(os.path.abspath(__file__))
SPAWN = ('run', 'call', 'popen')
# the files of this folder that start a child, and how many starts each makes through the helper
STARTS = {'pagedom.py': 1, 'test_page.py': 1, 'test_store.py': 2, 'test_todosteps.py': 2}


def sources():
    """{file name: its text} for every .py file of this folder."""
    out = {}
    for name in sorted(os.listdir(HERE)):
        if name.endswith('.py'):
            with open(os.path.join(HERE, name), encoding='utf-8-sig') as f:
                out[name] = f.read()
    return out


def helper_calls(text, name):
    """How many calls of spawn.run, spawn.call and spawn.popen the module's source makes."""
    return sum(1 for node in ast.walk(ast.parse(text, name))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and isinstance(node.func.value, ast.Name) and node.func.value.id == 'spawn'
               and node.func.attr in SPAWN)


class ModuleList(unittest.TestCase):
    def listed(self):
        """[(module, what the docstring says of it)] from the package docstring's list: every
        line after its title that is not empty. (Python 3.13 takes the list's indentation off a
        docstring, so the indentation is not what tells a row.)"""
        rows = []
        for line in hubsite.__doc__.splitlines()[1:]:
            if line.strip():
                name, _, said = line.strip().partition(' ')
                rows.append((name, said.strip()))
        return rows

    def test_the_package_docstring_names_every_module_once(self):
        folder = os.path.dirname(os.path.abspath(hubsite.__file__))
        modules = sorted(name[:-3] for name in os.listdir(folder)
                         if name.endswith('.py') and name != '__init__.py')
        self.assertIn('tunnel', modules)
        self.assertEqual(sorted(name for name, _ in self.listed()), modules)
        for name, said in self.listed():
            self.assertGreaterEqual(len(said.split()), 3, '%s: says nothing of the module' % name)

    def test_the_tunnel_listener_is_listed_as_what_it_is(self):
        said = dict(self.listed()).get('tunnel', '')
        self.assertIn('tunnel', said)
        self.assertIn('Cloudflare Access', said)


class Starts(unittest.TestCase):
    def test_no_hub_test_starts_a_program_past_the_spawn_helper(self):
        found = []
        for name, text in sources().items():
            found.extend(scan_source(text, 'tests/hubsite/' + name))
        said = ['%s:%d  %s  in %s' % (rel, line, what, func) for rel, func, what, line in found]
        self.assertEqual(said, [], '\n' + '\n'.join(
            said + ['Start it through kitlib.proc.spawn (run, call, popen).']))

    def test_the_helpers_that_start_a_child_do_so_through_the_spawn_helper(self):
        texts = sources()
        made = dict((name, helper_calls(text, name)) for name, text in texts.items())
        self.assertEqual(dict((name, n) for name, n in made.items() if n), STARTS)
        for name in STARTS:
            self.assertIn('from kitlib.proc import spawn', texts[name])

    def test_the_reader_sees_a_start_in_a_test_file(self):
        text = ('import subprocess\n'
                'def js(src):\n'
                '    return subprocess.run(["node", "-"], input=src)\n')
        self.assertEqual([f[1:3] for f in scan_source(text, 'tests/hubsite/made_up.py')],
                         [('js', 'subprocess.run')])
        self.assertEqual(helper_calls('from kitlib.proc import spawn\nspawn.run(["x"])\n', 'm.py'), 1)


if __name__ == '__main__':
    unittest.main()
