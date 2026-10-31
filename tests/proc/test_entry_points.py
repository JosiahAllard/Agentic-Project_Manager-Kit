"""Every Python entry point of the kit imports kitlib before it starts anything.

`import kitlib` installs kitlib.proc.console's CreateProcess hook, which gives a start from a
process with no console no window of its own. A program that starts a process before kitlib is
imported starts it unhooked. So every entry point (entry_points below) holds a module-level
kitlib import (in an if, try or with at module level too; `import kitlib`, `from kitlib... import`)
whose line comes before the first line that reaches subprocess, multiprocessing,
os.system/startfile/popen/spawn*/exec*, asyncio's create_subprocess_* or ProcessPoolExecutor
(tests/proc/windowscan.py process_uses). A file that reaches none of them still needs the
import: a tool that starts nothing today can start something tomorrow. A tool that imports kitlib
through another kitlib module (`from kitlib.x import y`) is covered: Python imports the kitlib
package first.

An entry point is found by what it is, not where it sits: a Python file under one of ENTRY_DIRS
with a module-level `if __name__ == '__main__':` block (a person, a host, a scheduled task or
another tool runs it by path), a Python script a host's manifest or plugin JSON names
(`${PLUGIN_ROOT}/x.py`), a Python git hook, any .pyw, and the FIRST_IMPORTED modules a whole kind
of run imports before anything else (they have no __main__ block of their own).

An entry point that deliberately cannot import kitlib gets an ENTRY_EXEMPT row: path -> reason.
A row whose file now meets the rule, or is gone, is stale and fails. A row whose reason says it
starts nothing (STARTS_NOTHING) is held to that: the file must reach no way to start a process.

What it cannot see: a start reached through a module the entry point imports before kitlib (the
reading is of the entry file only), a start built from a string, and a file run by path that has
no __main__ block and is named by nothing above (a scheduled task's command line is not read).
"""
import ast
import json
import os
import re
import unittest

from kitlib import machinepaths
from tests import gitfixture
from tests.proc.windowscan import is_python, kitlib_imports, process_uses

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STARTS_NOTHING = 'starts nothing: '
BOOT_HOOK = (STARTS_NOTHING + 'the one hook entry of every host. Its fast path (a tool the host '
             'adapter need not judge) returns before any kit code loads, so a hook costs no import; '
             'otherwise it binds the kit through kitboot, then imports the host adapter (which '
             'imports kitlib) and kitlib.host.hookmain, both before dispatch runs anything')
BOOT_KITBOOT = (STARTS_NOTHING + 'the check every host entry runs before it imports kitlib: it '
                'finds and validates the kit folder and puts it on sys.path, so it must not '
                'import kitlib itself (tests/host/conformance.py holds that order)')
COMFY = ('a standalone optional tool for the owner\'s local ComfyUI and Blender, standard library '
         'only and run from a console; it imports nothing of the kit (it works copied out of it)')
USAGE = (STARTS_NOTHING + 'a standalone usage reader, run from the kit, or from a copy '
         'of the folder where no kit is on sys.path; its only kit use is _usage.home(), '
         'which imports kitlib.config.user when the kit is there')
POOL_WORKER = (STARTS_NOTHING + 'the test pool\'s interpreter imports nothing of the kit but the '
               'packages named on its command line, so each unit\'s modules can be unloaded after '
               'it; the kit\'s units get the hook from tests/__init__.py, a package it preloads')
PROCWATCH = (STARTS_NOTHING + 'a standalone process report (its own folder alone on sys.path), '
             'run from a console or a scheduled task; its one start, Git\'s `ps -l` in '
             'winproc.py, passes CREATE_NO_WINDOW itself')

CODEX_HOOK_STUB = (STARTS_NOTHING + 'the fixed-bytes stub of the Codex hooks plugin pm-kit-hooks: it '
                   'finds the installed pm-kit beside it in the Codex cache and runs that boot/hook.py '
                   'in its own interpreter (runpy); its bytes never change with the kit, so it imports '
                   'nothing of it')

ENTRY_EXEMPT = {
    'boot/hook.py': BOOT_HOOK,
    'hosts/codex/shell/hooks-plugin/pm_kit_hook.py': CODEX_HOOK_STUB,
    'boot/kitboot.py': BOOT_KITBOOT,
    'kitlib/tooldeps/pool_worker.py': POOL_WORKER,
    'optional/comfy/compare_sheet.py': STARTS_NOTHING + COMFY,
    'optional/comfy/render_glb.py': COMFY + ', and it runs inside Blender\'s own Python, where '
                                            'no kit is on sys.path. Its one start, subprocess.call '
                                            'of the machine lock '
                                            'tool to run itself again under the lock, hands on '
                                            'its own console, so the child shows no window of '
                                            'its own (test_spawn_sites EXEMPT, STANDALONE)',
    'optional/procwatch/procwatch.py': PROCWATCH,
    'optional/usage/agent_costs.py': USAGE,
    'optional/usage/api_cost.py': USAGE,
    'optional/usage/coordinator_profile.py': USAGE,
    'optional/usage/repetition_report.py': USAGE,
    'optional/usage/testing_cost.py': USAGE,
    'optional/usage/usage_watch.py': USAGE,
}

# Where a file with a __main__ block is an entry point. templates/ is left out: its tools are
# copied into another repository, which has no kitlib (test_spawn_sites OWN_GATE_TOOL).
ENTRY_DIRS = ('boot', 'githooks', 'hosts', 'kitlib', 'mcp', 'optional', 'tools')

# Modules a whole kind of run imports before anything else; none has a __main__ block.
FIRST_IMPORTED = {
    'boot/kitboot.py': 'every host entry runs it before it imports kitlib',
    'kitlib/host/hookmain.py': 'the one hook runner behind boot/hook.py',
    'tests/__init__.py': 'every test run imports the tests package before any test module',
    'tools/_host.py': 'the shared tools import it to reach a host adapter',
}

HOOK_SCRIPT = re.compile(r'\$\{\w+\}/([\w./-]+\.pyw?)')


def main_guarded(text, rel='<source>'):
    """True when the module has a module-level `if` whose test compares __name__ with '__main__'
    (`if __name__ == '__main__' and ...:` too)."""
    for node in ast.parse(text, rel).body:
        if not isinstance(node, ast.If):
            continue
        for test in ast.walk(node.test):
            if isinstance(test, ast.Compare):
                sides = [test.left] + list(test.comparators)
                if (any(isinstance(n, ast.Name) and n.id == '__name__' for n in sides)
                        and any(isinstance(n, ast.Constant) and n.value == '__main__' for n in sides)):
                    return True
    return False


def manifest_scripts(root, files):
    """The Python scripts a host's JSON names as `${...}/x.py` (hosts/<host>/shell/**/*.json: hook
    manifests, MCP and plugin manifests; .claude-plugin/*.json), each found under the repository
    root or the manifest's shell folder, whichever holds it."""
    out = set()
    for rel in files:
        parts = rel.split('/')
        if not rel.endswith('.json') or not (
                len(parts) == 2 and parts[0] == '.claude-plugin'
                or len(parts) >= 4 and parts[0] == 'hosts' and parts[2] == 'shell'):
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
        shell = '/'.join(parts[:3]) if parts[0] == 'hosts' else ''
        for text in texts:
            for script in HOOK_SCRIPT.findall(text):
                for cand in (script, shell + '/' + script) if shell else (script,):
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
        path = os.path.join(root, rel)
        if rel in FIRST_IMPORTED or rel.endswith('.pyw'):
            out.add(rel)
        elif parts[0] == 'githooks' and is_python(path):
            out.add(rel)                # git runs each hook by its name
        elif parts[0] in ENTRY_DIRS and rel.endswith('.py'):
            with open(path, encoding='utf-8-sig') as f:
                if main_guarded(f.read(), rel):
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
        main = '\nif __name__ == "__main__":\n    pass\n'
        repo.write('tools/good.py', 'import kitlib\nimport subprocess\nsubprocess.run(["x"])' + main)
        repo.write('tools/forgot.py', 'import subprocess\nsubprocess.run(["x"])' + main)
        repo.write('tools/sub/deep.py', 'import os' + main)            # any depth
        repo.write('tools/lib.py', 'import os\n')                       # no __main__: not run
        repo.write('tools/inner.py', 'def f():\n    if __name__ == "__main__":\n        pass\n')
        repo.write('githooks/pre-push', '#!/usr/bin/env python\nimport os\n')
        repo.write('githooks/pre-commit', '#!/bin/sh\nexit 0\n')         # not Python
        repo.write('ui.pyw', 'import kitlib\n')
        repo.write('optional/bridge/tools/svc.py', 'import kitlib' + main)
        repo.write('optional/x/sweep.py', 'import os\nif "__main__" == __name__ and os:\n    pass\n')
        repo.write('kitlib/x/worker.py', 'import json' + main)
        repo.write('kitlib/x/lib.py', 'import json\n')
        repo.write('templates/t/tools/gate.py', 'import os' + main)     # another repository's
        repo.write('tests/test_x.py', 'import os' + main)               # a test module
        repo.write('hosts/x/shell/scripts/run.py', 'import os\nos.startfile("x")' + main)
        repo.write('hosts/x/shell/boot/hook.py', 'import json' + main)
        repo.write('hosts/x/shell/hooks/hooks.json',
                   json.dumps({'hooks': [{'command': 'pythonw "${PLUGIN_ROOT}/hooks/h.py"'}]}))
        repo.write('hosts/x/shell/hooks/h.py', 'import os\n')
        repo.write('hosts/x/shell/mcp.json',
                   json.dumps({'s': {'args': ['${PLUGIN_ROOT}/mcp/serve.py']}}))
        repo.write('hosts/x/shell/mcp/serve.py', 'import kitlib\n')
        repo.write('.claude-plugin/plugin.json',
                   json.dumps({'mcpServers': {'s': {'args': ['${CLAUDE_PLUGIN_ROOT}/mcp/m.py']}}}))
        repo.write('mcp/m.py', 'import os\n')
        repo.write('.claude/worktrees/w/tools/old.py', 'import os' + main)
        repo.commit('fixture', 'tools/good.py')
        self.assertEqual(entry_points(repo.root), [
            'githooks/pre-push', 'hosts/x/shell/boot/hook.py', 'hosts/x/shell/hooks/h.py',
            'hosts/x/shell/mcp/serve.py', 'hosts/x/shell/scripts/run.py', 'kitlib/x/worker.py',
            'mcp/m.py', 'optional/bridge/tools/svc.py', 'optional/x/sweep.py', 'tools/forgot.py',
            'tools/good.py', 'tools/sub/deep.py', 'ui.pyw'])
        broken, stale = check(repo.root, {'hosts/x/shell/boot/hook.py': 'why', 'tools/gone.py': 'why'})
        self.assertEqual([rel for rel, _ in broken], [
            'githooks/pre-push', 'hosts/x/shell/hooks/h.py', 'hosts/x/shell/scripts/run.py',
            'kitlib/x/worker.py', 'mcp/m.py', 'optional/x/sweep.py', 'tools/forgot.py',
            'tools/sub/deep.py'])
        self.assertIn(('tools/forgot.py', 'no module-level kitlib import (first start at line 2)'), broken)
        self.assertEqual(stale, ['tools/gone.py'])
        repo.write('hosts/x/shell/boot/hook.py', 'import kitlib' + main)
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
                    'boot/kitboot.py', 'kitlib/host/hookmain.py',
                    'mcp/lock_server.py', 'hosts/codex/shell/mcp/lock_server.py',
                    'hosts/codex/shell/scripts/kit.py', 'optional/bridge/tools/bridge_service.py',
                    'optional/agenttemp/agent_temp_sweep.py', 'optional/procwatch/procwatch.py',
                    'optional/usage/usage_watch.py', 'optional/bridge/mcp_frontend.py',
                    'optional/bridge/hosts/claude/hook.py', 'kitlib/tooldeps/pool_worker.py',
                    'optional/bridge/hosts/codex/delivery_worker.py', 'optional/comfy/render_glb.py',
                    'tools/worktree_remove.py'):
            self.assertIn(rel, self.entries)
        self.assertNotIn('githooks/pre-commit', self.entries)
        self.assertFalse([rel for rel in self.entries if rel.startswith(('templates/', 'tests/'))
                          and rel != 'tests/__init__.py'])


if __name__ == '__main__':
    unittest.main()
