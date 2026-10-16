"""The guard as Claude Code runs it, `boot/hook.py claude PreToolUse`: a PreToolUse event on stdin,
hook JSON (or nothing) on stdout, exit 0. HOME/USERPROFILE point at a scratch folder, so no real
machine config is read, and the payloads touch no repository (a temp folder that is not a checkout).
The entry hands the event to hosts.claude.adapter through kitlib.host.hookmain; these tests run it
end to end."""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from hosts.claude.adapter import events, host
from kitlib.host import registry

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRY = os.path.join(KIT_ROOT, 'boot', 'hook.py')
ARGS = ['claude', 'PreToolUse']
# what a copy of the entry needs, beside boot/hook.py itself, to deny by itself when kitlib.guard
# will not import: the hook imports these and nothing else of the kit
HOOK_FILES = ('boot/kitboot.py', 'tools/machine_lock.py', 'hosts/__init__.py', 'hosts/claude/__init__.py',
              'hosts/claude/adapter/__init__.py', 'hosts/claude/adapter/events.py',
              'hosts/claude/adapter/host.py', 'kitlib/host/__init__.py', 'kitlib/host/hookmain.py',
              'kitlib/host/registry.py', 'hosts.json',
              'kitlib/agentname.py', 'kitlib/lockmcp/__init__.py')


def load():
    """boot/hook.py as a module."""
    spec = importlib.util.spec_from_file_location('kit_hook_entry', ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def settings(mod):
    """What `hook.py claude --settings-entry` prints, from the loaded entry module."""
    out = io.StringIO()
    if mod.main(['claude', '--settings-entry'], io.StringIO(), out) != 0:
        raise AssertionError('--settings-entry did not exit 0')
    return json.loads(out.getvalue())


class Entry(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-guard-entry-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = os.path.join(self.base, 'home')
        self.cwd = os.path.join(self.base, 'work')
        os.makedirs(self.home)
        os.makedirs(self.cwd)
        # a guard started with the inherited environment gets this home too, not the real one
        home = mock.patch.dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        home.start()
        self.addCleanup(home.stop)

    def run_guard(self, payload):
        env = {k: v for k, v in os.environ.items() if not k.startswith('MACHINE_LOCK_')}
        env.update(HOME=self.home, USERPROFILE=self.home, GIT_CEILING_DIRECTORIES=self.base)
        env.pop('PM_KIT_ROLE', None)
        r = subprocess.run([sys.executable, ENTRY] + ARGS, input=payload, capture_output=True, text=True,
                           encoding='utf-8', env=env, cwd=self.cwd, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def event(self, tool, tool_input):
        return json.dumps({'session_id': 's', 'transcript_path': 't', 'cwd': self.cwd,
                           'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': tool_input})

    def test_a_deny_prints_hook_json(self):
        code, out, err = self.run_guard(self.event('Bash', {'command': 'find / -name x'}))
        self.assertEqual(code, 0, err)
        hso = json.loads(out)['hookSpecificOutput']
        self.assertEqual(hso['hookEventName'], 'PreToolUse')
        self.assertEqual(hso['permissionDecision'], 'deny')
        self.assertIn('Instead: ', hso['permissionDecisionReason'])
        self.assertIn('[kit guard: rule=root-search role=main]', hso['permissionDecisionReason'])

    def test_an_allow_prints_nothing(self):
        code, out, err = self.run_guard(self.event('Read', {'file_path': os.path.join(self.cwd, 'a')}))
        self.assertEqual((code, out), (0, ''), err)

    def test_a_subagent_read_of_the_hub_owner_token_is_denied(self):
        # Read, Grep and Glob reach the guard (host.MATCHER) and
        # hub-secrets refuses the token folder; the path is built from kitlib's names
        from kitlib.config import user as kuser
        from kitlib.guard import hub as hubg
        folder = hubg.secret_folder(kuser.locate(self.home).new)
        ev = json.loads(self.event('Read', {'file_path': os.path.join(folder, hubg.SECRET_FILES[0])}))
        ev['agent_id'] = 'a1'
        code, out, err = self.run_guard(json.dumps(ev))
        self.assertEqual(code, 0, err)
        hso = json.loads(out)['hookSpecificOutput']
        self.assertEqual(hso['permissionDecision'], 'deny')
        self.assertIn('[kit guard: rule=hub-secrets role=subagent]', hso['permissionDecisionReason'])
        # a subagent Glob of the folder is refused too; the owner's own session (unmarked main) reads it
        ev = json.loads(self.event('Glob', {'pattern': '*', 'path': folder}))
        ev['agent_id'] = 'a1'
        code, out, err = self.run_guard(json.dumps(ev))
        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny', err)
        code, out, err = self.run_guard(self.event('Read', {'file_path': os.path.join(folder, hubg.SECRET_FILES[0])}))
        self.assertEqual((code, out), (0, ''), err)

    def test_unreadable_input_fails_closed(self):
        code, out, _ = self.run_guard('{not json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny')


class GoLive(unittest.TestCase):
    """The guard started exactly as the settings entry tells Claude Code to start it (docs/guard.md
    "Go-live"): the entry's command (pythonw.exe) with its args, no shell, the event JSON piped on
    stdin. A known-denied command must come back denied."""
    setUp, event = Entry.setUp, Entry.event

    def test_a_known_denied_command_is_denied_the_go_live_way(self):
        (hook,) = settings(load())['PreToolUse'][0]['hooks']
        self.assertTrue(re.fullmatch(host.MATCHER, 'Bash'))
        env = {k: v for k, v in os.environ.items() if not k.startswith('MACHINE_LOCK_')}
        env.update(HOME=self.home, USERPROFILE=self.home, GIT_CEILING_DIRECTORIES=self.base)
        env.pop('PM_KIT_ROLE', None)
        r = subprocess.run([hook['command']] + hook['args'], input=self.event('Bash', {'command': 'find / -name x'}),
                           capture_output=True, text=True, encoding='utf-8', env=env, cwd=self.cwd, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        hso = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(hso['permissionDecision'], 'deny')
        self.assertIn('[kit guard: rule=root-search role=main]', hso['permissionDecisionReason'])

    def test_no_stdout_and_no_stderr_still_blocks(self):
        # pythonw started without std handles has sys.stdout and sys.stderr None: the deny cannot be
        # written, and the last-resort handler must still exit 2 (a block) rather than crash with 1.
        code = ('import runpy, sys; sys.stdout = sys.stderr = None; sys.argv[1:] = %r; '
                'runpy.run_path(%r, run_name="__main__")' % (ARGS, ENTRY))
        r = subprocess.run([sys.executable, '-c', code], input=self.event('Bash', {'command': 'find / -name x'}),
                           capture_output=True, text=True, encoding='utf-8', cwd=self.cwd, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)


class GuardError(unittest.TestCase):
    """Any exception inside the guard denies, whatever the tool (an mcp__ call included), and
    names a command that reproduces it; so does a kitlib that will not import."""

    def load(self):
        return load()

    def test_every_guarded_tool_is_denied_on_a_guard_error(self):
        mod = self.load()

        def boom(*args, **kw):
            raise RuntimeError('boom')
        with mock.patch('kitlib.guard.decide', boom):
            for tool in ('mcp__x__y', 'Edit', 'Bash'):
                with self.subTest(tool):
                    out = io.StringIO()
                    ev = {'tool_name': tool, 'tool_input': {}, 'cwd': '.', 'hook_event_name': 'PreToolUse'}
                    self.assertEqual(mod.main(ARGS, io.StringIO(json.dumps(ev)), out, environ={}), 0)
                    hso = json.loads(out.getvalue())['hookSpecificOutput']
                    self.assertEqual(hso['permissionDecision'], 'deny')
                    reason = hso['permissionDecisionReason']
                    self.assertIn('RuntimeError: boom', reason)
                    self.assertIn('Instead: python "%s" claude PreToolUse < event.json ['
                                  % ENTRY.replace(chr(92), '/'), reason)
                    self.assertTrue(reason.endswith('[kit guard: rule=guard-error role=main]'), reason)

    def copy(self, files=HOOK_FILES):
        """A copy of boot/hook.py, and `files`, beside a kitlib whose guard package raises on
        import."""
        base = tempfile.mkdtemp(prefix='kit-guard-import-')
        self.addCleanup(shutil.rmtree, base, True)
        os.makedirs(os.path.join(base, 'boot'))
        os.makedirs(os.path.join(base, 'kitlib', 'guard'))
        shutil.copy(ENTRY, os.path.join(base, 'boot', 'hook.py'))
        if 'hosts.json' not in files:                  # the entry reads it before it boots
            shutil.copy(os.path.join(KIT_ROOT, 'hosts.json'), os.path.join(base, 'hosts.json'))
        for rel in files:
            os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
            shutil.copy(os.path.join(KIT_ROOT, rel), os.path.join(base, rel))
        with open(os.path.join(base, 'kitlib', '__init__.py'), 'w', encoding='utf-8') as f:
            f.write('')
        with open(os.path.join(base, 'kitlib', 'guard', '__init__.py'), 'w', encoding='utf-8') as f:
            f.write("raise ImportError('broken on purpose')" + chr(10))
        return base

    def test_a_kitlib_that_will_not_import_denies(self):
        base = self.copy()
        entry = os.path.join(base, 'boot', 'hook.py')
        ev = json.dumps({'tool_name': 'mcp__x__y', 'tool_input': {}, 'cwd': base, 'agent_id': 'a1',
                         'hook_event_name': 'PreToolUse'})
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        r = subprocess.run([sys.executable, entry] + ARGS, input=ev, capture_output=True, text=True,
                           encoding='utf-8', env=env, cwd=base, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        hso = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(hso['permissionDecision'], 'deny')
        reason = hso['permissionDecisionReason']
        self.assertIn('ImportError: broken on purpose', reason)
        self.assertIn('Instead: python "%s" claude PreToolUse < event.json [' % entry.replace(chr(92), '/'), reason)
        self.assertTrue(reason.endswith('[kit guard: rule=guard-error role=subagent]'), reason)
        # A tool no rule can refuse never reaches kitlib, so even this broken one allows it.
        for tool in ('TodoWrite', 'WebFetch', 'Agent'):
            ev = json.dumps({'tool_name': tool, 'tool_input': {}, 'cwd': base, 'agent_id': 'a1',
                             'hook_event_name': 'PreToolUse'})
            r = subprocess.run([sys.executable, entry] + ARGS, input=ev, capture_output=True, text=True,
                               encoding='utf-8', env=env, cwd=base, timeout=60)
            self.assertEqual((r.returncode, r.stdout), (0, ''), tool)

    def test_an_adapter_that_will_not_import_blocks_by_exit_code(self):
        # with no hosts.claude.adapter or kitlib.host beside it the entry cannot write a deny: it exits
        # 2, which Claude Code reads as a block; a tool no rule can refuse is still allowed
        base = self.copy(files=('boot/kitboot.py', 'tools/machine_lock.py'))
        entry = os.path.join(base, 'boot', 'hook.py')
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        for tool, want in (('mcp__x__y', 2), ('Bash', 2), ('Read', 2), ('TodoWrite', 0)):
            ev = json.dumps({'tool_name': tool, 'tool_input': {}, 'cwd': base, 'hook_event_name': 'PreToolUse'})
            r = subprocess.run([sys.executable, entry] + ARGS, input=ev, capture_output=True, text=True,
                               encoding='utf-8', env=env, cwd=base, timeout=60)
            self.assertEqual((r.returncode, r.stdout), (want, ''), tool)

    def test_an_unguarded_call_loads_no_kit_module(self):
        """The fast path: a TodoWrite decides on hosts.json alone, so the hook loads no kit module but
        itself (no kitboot, no kitlib, no adapter), and a guarded call loads HOOK_FILES' modules."""
        code = ('import io, json, os, sys; sys.path.insert(0, %r); import hook; '
                'code = hook.main(%r, io.StringIO(sys.stdin.read()), io.StringIO(), environ={}); '
                'root = %r; '
                'print(json.dumps([code, sorted(os.path.relpath(m.__file__, root).replace(os.sep, "/") '
                'for m in list(sys.modules.values()) if getattr(m, "__file__", None) '
                'and os.path.abspath(m.__file__).startswith(root + os.sep))]))'
                % (os.path.dirname(ENTRY), ARGS, os.path.dirname(os.path.dirname(ENTRY))))
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PM_KIT_ROOT', 'PM_KIT_RELEASE_ID')}

        def loaded(tool):
            ev = json.dumps({'tool_name': tool, 'tool_input': {}, 'cwd': self.base, 'hook_event_name': 'PreToolUse'})
            r = subprocess.run([sys.executable, '-c', code], input=ev, capture_output=True, text=True,
                               encoding='utf-8', env=env, cwd=self.base, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout.splitlines()[-1])
        self.assertEqual(loaded('TodoWrite'), [0, ['boot/hook.py']])
        code_, files = loaded('mcp__x__y')
        self.assertEqual(code_, 0)
        need = {f for f in HOOK_FILES if f.endswith('.py') and f.startswith(('boot/', 'hosts/', 'kitlib/host/'))}
        self.assertTrue(need <= set(files), files)

    setUp = Entry.setUp


class Unguarded(unittest.TestCase):
    """The early exit (hosts.json's claude hook.guarded_tools, read by boot/hook.py) and the
    settings entry cover exactly the tools kitlib.guard.decide can refuse (KIT-HOOK-SPAWNS)."""

    def load(self):
        return load()

    def hook(self):
        with open(os.path.join(KIT_ROOT, 'hosts.json'), encoding='utf-8') as f:
            return json.load(f)['claude']['hook']

    def unguarded(self, event):
        return self.load().skips(self.hook(), event, {})

    def test_guarded_is_the_adapter_tool_tables(self):
        self.assertEqual(set(self.hook()['guarded_tools']),
                         set(events.FILE_TOOLS) | set(events.SHELL_TOOLS) | set(events.READ_TOOLS) | {'mcp__*'})
        self.assertEqual(registry.host('claude').hook.guarded_tools, tuple(self.hook()['guarded_tools']))
        self.assertEqual(set(host.MATCHER.split('|')),
                         {t[:-1] + '.*' if t.endswith('*') else t for t in self.hook()['guarded_tools']})

    def test_only_unguarded_tools_exit_early(self):
        for tool in ('WebFetch', 'Agent', 'TodoWrite'):
            self.assertTrue(self.unguarded({'tool_name': tool}), tool)
            self.assertTrue(self.load().skips(self.hook(), {'tool_name': tool}, {'PM_KIT_ROLE': 'worker'}),
                            'a marked session takes the fast path too on this host')
        for ev in ({'tool_name': 'Bash'}, {'tool_name': 'Edit'}, {'tool_name': 'NotebookEdit'},
                   {'tool_name': 'Read'}, {'tool_name': 'Grep'}, {'tool_name': 'Glob'},
                   {'tool_name': 'mcp__x__lock_free'}, {'tool_name': 3}, {}, [], None):
            self.assertFalse(self.unguarded(ev), ev)

    def test_every_rule_row_tool_is_matched_and_guarded(self):
        mod = self.load()
        from .test_rules import ROWS
        from .world import World
        w = World()
        self.addCleanup(w.cleanup)
        tools = {make(w)['tool_name'] for _n, make, *_rest in ROWS}
        from kitlib.lockmcp import tool_prefix
        tools |= {tool_prefix() + 'lock_free', 'mcp__other__lock_free'}
        for tool in tools:
            self.assertTrue(re.fullmatch(host.MATCHER, tool), tool)
            self.assertFalse(mod.skips(self.hook(), {'tool_name': tool}, {}), tool)
        for tool in events.READ_TOOLS:
            self.assertTrue(re.fullmatch(host.MATCHER, tool), tool)
            self.assertFalse(mod.skips(self.hook(), {'tool_name': tool}, {}), tool)
        for tool in ('TodoWrite', 'WebFetch', 'Agent'):
            self.assertIsNone(re.fullmatch(host.MATCHER, tool), tool)

    def test_settings_entry_is_exec_form(self):
        mod = self.load()
        entry = settings(mod)['PreToolUse'][0]
        self.assertEqual(entry['matcher'], host.MATCHER)
        (hook,) = entry['hooks']
        self.assertEqual(hook['type'], 'command')
        self.assertEqual(hook['args'], [ENTRY.replace(chr(92), '/')] + ARGS)
        self.assertTrue(os.path.isfile(hook['command']), hook['command'])
        if os.name == 'nt':
            self.assertEqual(os.path.basename(hook['command']).lower(), 'pythonw.exe')
            self.assertEqual(os.path.normcase(os.path.dirname(hook['command'])),
                             os.path.normcase(os.path.dirname(sys.executable)))


if __name__ == '__main__':
    unittest.main()
