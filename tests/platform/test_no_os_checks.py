"""Rule 1 of docs/linux-support-plan.md section 2.1: code asks kitlib.platform which operating
system it runs on, never os.name or sys.platform itself.

The scan reads every kit .py file outside tests/ and kitlib/platform/ (and outside dot-folders
and __pycache__) and finds each read of os.name or sys.platform, under any alias
(`import os as _os`, `from sys import platform`). The files still doing so today are ALLOWED:
each later slice moves its files onto kitlib.platform and takes them off the list, so the list
only shrinks. A file not on the list that starts asking is a failure, and so is a listed file
that no longer asks (take it off)."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ALLOWED = frozenset({
    'kitlib/__init__.py',
    'kitlib/ci/ratchet.py',
    'kitlib/config/user.py',
    'kitlib/gate/detect.py',
    'kitlib/guard/context.py',
    'kitlib/guard/place.py',
    'kitlib/guard/rules.py',
    'kitlib/host/hookmain.py',
    'kitlib/launcher/host.py',
    'kitlib/ledger/safepath.py',
    'kitlib/machinesetup/extras.py',
    'kitlib/pluginsource/__init__.py',
    'kitlib/pluginsource/claude.py',
    'kitlib/proc/console.py',
    'kitlib/proc/winwatch.py',
    'kitlib/redfirst/run.py',
    'kitlib/redfirst/verdict.py',
    'kitlib/tooldeps/selfrun.py',
    'kitlib/worktree/holders.py',
    'optional/procwatch/procwatch.py',
    'optional/usage/_usage.py',
    'optional/usage/agent_costs.py',
    'optional/usage/usage_watch.py',
    'tools/ci_ratchet.py',
    'tools/kit.py',
})


def os_checks(tree):
    """The line numbers in `tree` that read os.name or sys.platform."""
    names = {'os': {'os'}, 'sys': {'sys'}}
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in names:
                    names[alias.name].add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in names:
            lines.extend(node.lineno for alias in node.names
                         if (node.module, alias.name) in (('os', 'name'), ('sys', 'platform')))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if ((node.value.id in names['os'] and node.attr == 'name')
                    or (node.value.id in names['sys'] and node.attr == 'platform')):
                lines.append(node.lineno)
    return sorted(lines)


def kit_sources(root=ROOT):
    """Every .py file the rule covers, as a repo path with forward slashes."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d != '__pycache__')
        rel = os.path.relpath(folder, root).replace(os.sep, '/')
        for name in sorted(files):
            path = name if rel == '.' else rel + '/' + name
            if name.endswith('.py') and not path.startswith(('tests/', 'kitlib/platform/')):
                yield path


def asking(root=ROOT):
    """{repo path: [line]} of every covered file that reads os.name or sys.platform."""
    out = {}
    for path in kit_sources(root):
        with open(os.path.join(root, path), 'rb') as f:
            lines = os_checks(ast.parse(f.read(), path))
        if lines:
            out[path] = lines
    return out


class NoOsChecks(unittest.TestCase):
    def test_only_the_allowed_files_ask_the_os_themselves(self):
        found = asking()
        self.assertEqual(sorted(set(found) - ALLOWED), [],
                         'ask kitlib.platform.NAME instead of os.name or sys.platform '
                         '(docs/linux-support-plan.md section 2.1, rule 1): %r' % found)
        self.assertEqual(sorted(ALLOWED - set(found)), [],
                         'these no longer ask the OS themselves: take them off ALLOWED')

    def test_the_scan_sees_each_spelling(self):
        for text in ('import os\nos.name', 'import sys\nif sys.platform == "x": pass',
                     'import os as _os\n_os.name', 'from sys import platform',
                     'from os import name as n', 'import sys as s\ns.platform.startswith("l")'):
            with self.subTest(text=text):
                self.assertTrue(os_checks(ast.parse(text)))
        for text in ('import os\nos.path.basename("x")', 'name = 1\nname.name',
                     'import platform\nplatform.system()', 'x.platform', 'from os import path'):
            with self.subTest(text=text):
                self.assertEqual(os_checks(ast.parse(text)), [])

    def test_the_scan_covers_the_kit_and_skips_tests_and_the_seam(self):
        sources = set(kit_sources())
        self.assertIn('kitlib/proc/process.py', sources)
        self.assertIn('tools/gate.py', sources)
        self.assertIn('boot/hook.py', sources)
        self.assertFalse({p for p in sources if p.startswith(('tests/', 'kitlib/platform/'))})


if __name__ == '__main__':
    unittest.main()
