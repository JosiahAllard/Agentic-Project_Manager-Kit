"""Every Python entry point of the kit imports kitlib before it starts anything.

`import kitlib` installs kitlib.proc.console's CreateProcess hook, which gives a start from a
process with no console no window of its own. A program that starts a process before kitlib is
imported starts it unhooked. So every entry point (entry_points below: a file Python is handed to
run, by a person, a hook manifest, a git hook or a host) holds a module-level kitlib import (in an
if, try or with at module level too; `import kitlib`, `from kitlib... import`) whose line comes
before the first line that reaches subprocess, multiprocessing, os.system/startfile/popen/spawn*/
exec*, asyncio's create_subprocess_* or ProcessPoolExecutor (tests/proc/windowscan.py
process_uses). A file that reaches none of them still needs the import: a tool that starts nothing
today can start something tomorrow. A tool that imports kitlib through another kitlib module
(`from kitlib.x import y`) is covered: Python imports the kitlib package first.

An entry point that deliberately cannot import kitlib gets an ENTRY_EXEMPT row: path -> reason.
A row whose file now meets the rule, or is gone, is stale and fails. A row whose reason says it
starts nothing (STARTS_NOTHING) is held to that: the file must reach no way to start a process.

What it cannot see: a start reached through a module the entry point imports before kitlib (the
reading is of the entry file only), and a start built from a string.
"""
import json
import os
import re
import unittest

from kitlib import machinepaths
from tests import gitfixture
from tests.proc.test_spawn_sites import is_python
from tests.proc.windowscan import kitlib_imports, process_uses

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STARTS_NOTHING = 'starts nothing: '
BOOT_HOOK = (STARTS_NOTHING + 'the one hook entry of every host. Its fast path (a tool the host '
             'adapter need not judge) returns before any kit code loads, so a hook costs no import; '
             'otherwise it binds the kit through kitboot, then imports the host adapter (which '
             'imports kitlib) and kitlib.host.hookmain, both before dispatch runs anything')
BOOT_KITBOOT = (STARTS_NOTHING + 'the check every host entry runs before it imports kitlib: it '
                'finds and validates the kit folder and puts it on sys.path, so it must not '
                'import kitlib itself (tests/host/conformance.py holds that order)')

ENTRY_EXEMPT = {
    'boot/hook.py': BOOT_HOOK,
    'boot/kitboot.py': BOOT_KITBOOT,
    'hosts/codex/shell/boot/hook.py': BOOT_HOOK + ' (the Codex plugin\'s byte copy of boot/hook.py)',
    'hosts/codex/shell/boot/kitboot.py': BOOT_KITBOOT + ' (the Codex plugin\'s byte copy)',
}

HOOK_SCRIPT = re.compile(r'\$\{\w+\}/([\w./-]+\.pyw?)')


def manifest_scripts(root, files):
    """The Python scripts the hosts' hook manifests (hosts/<host>/shell/hooks/hooks.json) name,
    each found under the repository root or the manifest's shell folder, whichever holds it."""
    out = set()
    for rel in files:
        parts = rel.split('/')
        if not (len(parts) == 5 and parts[0] == 'hosts' and parts[2:] == ['shell', 'hooks', 'hooks.json']):
            continue
        with open(os.path.join(root, rel), encoding='utf-8-sig') as f:
            manifest = json.load(f)
        texts = []

        def walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str):
                texts.append(o)
        walk(manifest)
        shell = '/'.join(parts[:3])
        for text in texts:
            for script in HOOK_SCRIPT.findall(text):
                for cand in (script, shell + '/' + script):
                    if cand in files:
                        out.add(cand)
    return out


def entry_points(root):
    """[path relative to root, with /] of every Python entry point git lists, sorted."""
    files = set(rel for rel in machinepaths.tree_files(root)
                if '.claude' not in rel.split('/')[:-1] and os.path.isfile(os.path.join(root, rel)))
    out = manifest_scripts(root, files)
    for rel in files:
        parts = rel.split('/')
        py = rel.endswith('.py')
        if (len(parts) == 2 and parts[0] in ('tools', 'boot', 'mcp') and py
                or parts[0] == 'githooks' and is_python(os.path.join(root, rel))
                or rel.endswith('.pyw')
                or rel in ('tests/__init__.py', 'kitlib/host/hookmain.py')
                or len(parts) == 5 and parts[0] == 'hosts' and parts[2] == 'shell'
                and parts[3] in ('boot', 'mcp', 'scripts') and py
                or len(parts) == 4 and parts[:3] == ['optional', 'bridge', 'tools'] and py):
            out.add(rel)
    return sorted(out)


def judge(text, rel):
    """None when the source meets the rule, else what is wrong, in words."""
    kit, uses = kitlib_imports(text, rel), process_uses(text, rel)
    if not kit:
        return 'no module-level kitlib import' + (' (first start at line %d)' % uses[0] if uses else '')
    if uses and uses[0] < kit[0]:
        return 'line %d reaches a process start before the kitlib import on line %d' % (uses[0], kit[0])
    return None


def check(root, exempt):
    """(the entry points that break the rule with no row: [(rel, what)], the stale rows)."""
    broken, stale = [], []
    entries = entry_points(root)
    for rel in entries:
        with open(os.path.join(root, rel), encoding='utf-8-sig') as f:
            wrong = judge(f.read(), rel)
        if wrong and rel not in exempt:
            broken.append((rel, wrong))
        elif not wrong and rel in exempt:
            stale.append(rel)
    stale.extend(rel for rel in exempt if rel not in entries)
    return broken, sorted(stale)


FIX = ('Add `import kitlib` at module level, before anything that starts a process (after a '
       'sys.path line or a kitboot.boot() call, if the file has one), so the CreateProcess hook '
       'is in place; or, if it truly cannot import kitlib, add a row with its reason to '
       'ENTRY_EXEMPT in tests/proc/test_entry_points.py.')


class Judge(unittest.TestCase):
    """The rule bites, on sources and on a throwaway repository."""

    def test_sources(self):
        good = 'import sys\nsys.path.insert(0, "x")\nimport kitlib  # noqa\nimport subprocess\nsubprocess.run([])\n'
        self.assertIsNone(judge(good, 'g.py'))
        self.assertIsNone(judge('from kitlib.proc import spawn\nimport subprocess\nsubprocess.PIPE\n', 'g.py'))
        self.assertIsNone(judge('try:\n    import kitlib\nexcept ImportError:\n    pass\n', 'g.py'))
        self.assertEqual(judge('import os\n', 't.py'), 'no module-level kitlib import')
        self.assertEqual(judge('import subprocess\nsubprocess.run([])\nimport kitlib\n', 't.py'),
                         'line 2 reaches a process start before the kitlib import on line 3')
        self.assertEqual(judge('import os\nos.system("x")\n', 't.py'),
                         'no module-level kitlib import (first start at line 2)')
        self.assertEqual(judge('def main():\n    import kitlib\n', 't.py'), 'no module-level kitlib import')
        self.assertEqual(judge('import multiprocessing\nimport kitlib\n', 't.py'),
                         'line 1 reaches a process start before the kitlib import on line 2')

    def test_a_new_tool_that_forgets_the_import_fails_and_says_how_to_fix_it(self):
        repo = gitfixture.TempRepo()
        self.addCleanup(repo.cleanup)
        repo.write('tools/good.py', 'import kitlib\nimport subprocess\nsubprocess.run(["x"])\n')
        repo.write('tools/forgot.py', 'import subprocess\nsubprocess.run(["x"])\n')
        repo.write('tools/sub/deep.py', 'import os\n')                 # not an entry point
        repo.write('githooks/pre-push', '#!/usr/bin/env python\nimport os\n')
        repo.write('githooks/pre-commit', '#!/bin/sh\nexit 0\n')         # not Python
        repo.write('ui.pyw', 'import kitlib\n')
        repo.write('optional/bridge/tools/svc.py', 'import kitlib\n')
        repo.write('hosts/x/shell/scripts/run.py', 'import os\nos.startfile("x")\n')
        repo.write('hosts/x/shell/boot/hook.py', 'import json\n')
        repo.write('hosts/x/shell/hooks/hooks.json',
                   json.dumps({'hooks': [{'command': 'pythonw "${PLUGIN_ROOT}/hooks/h.py"'}]}))
        repo.write('hosts/x/shell/hooks/h.py', 'import os\n')
        repo.write('.claude/worktrees/w/tools/old.py', 'import os\n')
        repo.commit('fixture', 'tools/good.py')
        self.assertEqual(entry_points(repo.root), [
            'githooks/pre-push', 'hosts/x/shell/boot/hook.py', 'hosts/x/shell/hooks/h.py',
            'hosts/x/shell/scripts/run.py', 'optional/bridge/tools/svc.py', 'tools/forgot.py',
            'tools/good.py', 'ui.pyw'])
        broken, stale = check(repo.root, {'hosts/x/shell/boot/hook.py': 'why', 'tools/gone.py': 'why'})
        self.assertEqual([rel for rel, _ in broken], [
            'githooks/pre-push', 'hosts/x/shell/hooks/h.py', 'hosts/x/shell/scripts/run.py',
            'tools/forgot.py'])
        self.assertIn(('tools/forgot.py', 'no module-level kitlib import (first start at line 2)'), broken)
        self.assertEqual(stale, ['tools/gone.py'])
        repo.write('hosts/x/shell/boot/hook.py', 'import kitlib\n')
        self.assertEqual(check(repo.root, {'hosts/x/shell/boot/hook.py': 'why'})[1],
                         ['hosts/x/shell/boot/hook.py'])
        self.assertIn('import kitlib', FIX)
        self.assertIn('ENTRY_EXEMPT', FIX)


class EntryPoints(unittest.TestCase):
    """The kit's own entry points."""

    @classmethod
    def setUpClass(cls):
        cls.entries = entry_points(ROOT)
        cls.broken, cls.stale = check(ROOT, ENTRY_EXEMPT)

    def test_every_entry_point_imports_kitlib_before_it_starts_anything(self):
        said = ['%s: %s' % b for b in self.broken]
        self.assertEqual(said, [], '\n' + '\n'.join(said + [FIX]))

    def test_no_exemption_outlives_its_reason(self):
        self.assertEqual(self.stale, [], 'ENTRY_EXEMPT rows for a file that now imports kitlib in '
                                         'time, or is gone: remove the row')

    def test_an_entry_point_that_says_it_starts_nothing_starts_nothing(self):
        for rel, reason in ENTRY_EXEMPT.items():
            with self.subTest(rel=rel):
                self.assertGreaterEqual(len(reason.split()), 6, 'a reason is a sentence')
                if reason.startswith(STARTS_NOTHING):
                    with open(os.path.join(ROOT, rel), encoding='utf-8-sig') as f:
                        self.assertEqual(process_uses(f.read(), rel), [])

    def test_the_list_saw_every_kind_of_entry_point(self):
        for rel in ('tools/gate.py', 'tools/_host.py', 'tools/agent_view.py', 'tools/kit_release.py',
                    'tools/render_content.py', 'tools/machine_lock.py', 'githooks/pre-push',
                    'githooks/pre-commit.py', 'reserve_ui.pyw', 'tests/__init__.py', 'boot/hook.py',
                    'boot/kitboot.py', 'hosts/codex/shell/boot/hook.py', 'kitlib/host/hookmain.py',
                    'mcp/lock_server.py', 'hosts/codex/shell/mcp/lock_server.py',
                    'hosts/codex/shell/scripts/kit.py', 'optional/bridge/tools/bridge_service.py'):
            self.assertIn(rel, self.entries)
        self.assertNotIn('githooks/pre-commit', self.entries)


if __name__ == '__main__':
    unittest.main()
