"""The Codex hooks plugin pm-kit-hooks (hosts/codex/shell/hooks-plugin; docs/plugin-source.md "Codex
hooks plugin"). Codex asks the owner to trust a plugin's hooks again when any byte of it changes, so
its four files are pinned here; its hooks are the ones the Codex renderer would write; its stub runs
boot/hook.py of the newest pm-kit beside it in the Codex cache, passing argv, stdin, output and exit
code, and fails closed with the fix in one line; and the export places it unchanged. Temp folders
and child Pythons only: no Codex runs."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from hosts.codex.adapter import content as cc
from kitlib import pluginsource as P
from kitlib.host import registry
from tests.pluginsource import Kit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN_REL = 'hosts/codex/shell/hooks-plugin'         # the adapter's HOOKS_PLUGIN_REL (test_the_paths_are_the_adapters)
STUB_ENTRY = 'pm_kit_hook.py'                          # the adapter's STUB_ENTRY
PLUGIN = os.path.join(ROOT, *PLUGIN_REL.split('/'))
# sha256 of each file, line ends read as LF. A change here makes Codex ask the owner to trust the
# four hooks once more: change the stub only for a reason worth that, and say so where it lands.
PINNED = {
    '.codex-plugin/plugin.json': '58ca94eed40a8a18d38340d5c508522e490ae11805e5c3f2aa54d7ba68c14a22',
    'hooks/hooks.json': 'a644344b6c7d5ef3cc8bd1707d2b59bbe21ff9c13e93688c6d0472225e841a24',
    'hooks/hooks.linux.json': 'eb0ec9606c52193595be6c000679761820f50a79179637edcc64f41dcfe26368',
    'pm_kit_hook.py': '5d9083528cf784c5a0fe6348bca8a321854eb1641cf90dee1f35935e8e5e7c89',
}
FIX = 'reinstall the kit: codex plugin add pm-kit@pm-kit (with Codex closed)'
# A stand-in boot/hook.py: reports what it was given, then exits with the code stdin names.
FAKE_HOOK = '''import json, os, sys
data = sys.stdin.read()
print(json.dumps({"argv": sys.argv[1:], "stdin": data, "name": __name__,
                  "file": os.path.abspath(__file__), "path0": os.path.abspath(sys.path[0])}))
sys.exit(json.loads(data).get("rc", 0))
'''


def files(top):
    """{relative path: bytes, line ends as LF} of every file under `top` (no __pycache__)."""
    out = {}
    for base, dirs, names in os.walk(top):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for n in names:
            path = os.path.join(base, n)
            with open(path, 'rb') as f:
                out[os.path.relpath(path, top).replace(os.sep, '/')] = f.read().replace(b'\r\n', b'\n')
    return out


def child_env():
    """This environment without the session mark or a kit root: an unmarked owner's session."""
    env = dict(os.environ)
    for name in ('PM_KIT_ROLE', 'PM_KIT_ROOT', 'PYTHONPATH'):
        env.pop(name, None)
    return env


class Bytes(unittest.TestCase):
    def test_the_paths_are_the_adapters(self):
        self.assertEqual((getattr(cc, 'HOOKS_PLUGIN_REL', None), getattr(cc, 'STUB_ENTRY', None)),
                         (PLUGIN_REL, STUB_ENTRY))

    def test_the_four_files_are_pinned(self):
        got = {rel: hashlib.sha256(body).hexdigest() for rel, body in files(PLUGIN).items()}
        self.assertEqual(got, PINNED, 'pm-kit-hooks changed: Codex asks the owner to trust its hooks again')

    def test_the_manifest_names_the_plugin_its_fixed_version_and_its_hooks(self):
        with open(os.path.join(PLUGIN, '.codex-plugin', 'plugin.json'), encoding='utf-8') as f:
            plugin = json.load(f)
        self.assertEqual(sorted(plugin), ['description', 'hooks', 'name', 'version'])
        self.assertEqual((plugin['name'], plugin['version'], plugin['hooks']),
                         ('pm-kit-hooks', '1.0.0', './hooks/hooks.json'))

    def test_the_hooks_are_the_ones_the_codex_renderer_writes(self):
        for platform in ('windows', 'linux'):
            with self.subTest(platform=platform):
                with open(os.path.join(ROOT, *cc.hooks_rel(platform).split('/')), 'rb') as f:
                    have = f.read().replace(b'\r\n', b'\n')
                self.assertEqual(have, cc.dump(cc.renderer().hooks(platform=platform)))

    def test_the_stub_knows_the_start_events_of_hosts_json(self):
        caps, spec = registry.host('codex').capabilities, registry.host('codex').hook
        stub = load_stub()
        self.assertEqual(sorted(stub.STARTS), sorted(set(caps.hook_events) - {spec.tool_event}))
        self.assertEqual(stub.FIX, FIX)


def load_stub():
    """The stub as a module, read without writing a __pycache__ beside it."""
    import types
    with open(os.path.join(PLUGIN, STUB_ENTRY), encoding='utf-8') as f:
        source = f.read()
    module = types.ModuleType('pm_kit_hook')
    module.__file__ = os.path.join(PLUGIN, STUB_ENTRY)
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


class Stub(unittest.TestCase):
    """The stub copied into a Codex cache layout: <cache>/pm-kit/pm-kit-hooks/1.0.0 beside
    <cache>/pm-kit/pm-kit/<version>, as the probe found it."""

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='codex-cache-')
        self.addCleanup(shutil.rmtree, self.cache, True)
        self.stub = os.path.join(self.cache, 'pm-kit', 'pm-kit-hooks', '1.0.0')
        shutil.copytree(PLUGIN, self.stub, ignore=shutil.ignore_patterns('__pycache__'))
        self.kits = os.path.join(self.cache, 'pm-kit', 'pm-kit')

    def kit(self, version, hook=FAKE_HOOK, mtime=None):
        folder = os.path.join(self.kits, version)
        os.makedirs(os.path.join(folder, 'boot'))
        with open(os.path.join(folder, 'boot', 'hook.py'), 'w', encoding='utf-8') as f:
            f.write(hook)
        if mtime is not None:
            os.utime(folder, (mtime, mtime))
        return folder

    def run_stub(self, event, payload=None, env=None):
        p = subprocess.run([sys.executable, '-B', os.path.join(self.stub, STUB_ENTRY), 'codex', event],
                           input=json.dumps(payload or {}), capture_output=True, text=True,
                           env=env or child_env(), timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_it_runs_the_kits_hook_with_its_arguments_input_and_exit_code(self):
        folder = self.kit('0.4.0-main.7')
        for rc in (0, 2, 5):
            with self.subTest(rc=rc):
                code, out, err = self.run_stub('PreToolUse', {'rc': rc, 'tool_name': 'Write'})
                self.assertEqual((code, err), (rc, ''))
                seen = json.loads(out)
                self.assertEqual(seen['argv'], ['codex', 'PreToolUse'])
                self.assertEqual(json.loads(seen['stdin']), {'rc': rc, 'tool_name': 'Write'})
                self.assertEqual(seen['name'], '__main__')
                hook = os.path.join(folder, 'boot', 'hook.py')
                self.assertEqual(os.path.normcase(seen['file']), os.path.normcase(hook))
                self.assertEqual(os.path.normcase(seen['path0']), os.path.normcase(os.path.dirname(hook)))

    def test_it_picks_the_highest_version_even_when_an_older_one_is_newer_on_disk(self):
        self.kit('0.4.0-main.9', mtime=2000000000)
        newest = self.kit('0.4.0-main.10', mtime=1000000000)
        os.makedirs(os.path.join(self.kits, '0.4.0-main.11'))          # no boot/hook.py: skipped
        _c, out, _e = self.run_stub('SessionStart')
        self.assertEqual(os.path.normcase(os.path.dirname(os.path.dirname(json.loads(out)['file']))),
                         os.path.normcase(newest))

    def test_the_modified_time_breaks_a_version_tie(self):
        self.kit('1.0', mtime=1000000000)
        later = self.kit('1.00', mtime=2000000000)                      # the same version by number
        _c, out, _e = self.run_stub('SessionStart')
        self.assertEqual(os.path.normcase(os.path.dirname(os.path.dirname(json.loads(out)['file']))),
                         os.path.normcase(later))

    def test_with_no_kit_a_tool_event_is_blocked_with_the_fix_in_one_line(self):
        for setup in ('no pm-kit folder', 'an empty pm-kit folder'):
            with self.subTest(setup):
                if setup == 'an empty pm-kit folder':
                    os.makedirs(self.kits)
                code, out, err = self.run_stub('PreToolUse', {'tool_name': 'Write'})
                self.assertEqual((code, out), (2, ''))
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertTrue(err.startswith('kit hook: blocked, the kit did not load: '), err)
                self.assertTrue(err.rstrip('\n').endswith(FIX), err)

    def test_with_no_kit_a_start_event_says_so_and_exits_0(self):
        for event in ('SessionStart', 'PostCompact', 'SubagentStart'):
            with self.subTest(event):
                code, out, err = self.run_stub(event)
                self.assertEqual((code, err), (0, ''))
                self.assertTrue(out.startswith('PM kit: the kit did not load: '), out)
                self.assertTrue(out.rstrip('\n').endswith(FIX), out)

    def test_a_hook_that_raises_or_ends_without_an_exit_code_blocks(self):
        for hook, why in (('raise ValueError("broken")\n', 'ValueError: broken'),
                          ('x = 1\n', 'ended without an exit code')):
            with self.subTest(why):
                shutil.rmtree(self.kits, True)
                self.kit('1.0.0', hook=hook)
                code, _out, err = self.run_stub('PreToolUse', {'tool_name': 'Write'})
                self.assertEqual(code, 2)
                self.assertIn(why, err)
                self.assertTrue(err.rstrip('\n').endswith(FIX), err)

    def test_the_kits_own_hook_runs_through_it_as_it_runs_alone(self):
        """The real boot/hook.py and hosts.json in the cache layout: its unmarked fast path allows a
        read (exit 0, nothing written) and, marked, with no kit to boot, it blocks; the stub gives the
        same exit code and output as running boot/hook.py itself."""
        folder = os.path.join(self.kits, '0.4.0-main.1')
        os.makedirs(os.path.join(folder, 'boot'))
        shutil.copy(os.path.join(ROOT, 'boot', 'hook.py'), os.path.join(folder, 'boot', 'hook.py'))
        shutil.copy(os.path.join(ROOT, 'hosts.json'), os.path.join(folder, 'hosts.json'))
        marked = dict(child_env(), PM_KIT_ROLE='worker')
        for env, tool, want in ((child_env(), 'Read', 0), (marked, 'Write', 2)):
            with self.subTest(tool=tool):
                got = self.run_stub('PreToolUse', {'tool_name': tool}, env=env)
                alone = subprocess.run([sys.executable, '-B', os.path.join(folder, 'boot', 'hook.py'),
                                        'codex', 'PreToolUse'], input=json.dumps({'tool_name': tool}),
                                       capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(got[0], want, got)
                self.assertEqual(got, (alone.returncode, alone.stdout, alone.stderr))

    def test_a_run_writes_nothing_into_the_plugin(self):
        before = files(self.stub)
        self.kit('1.0.0')
        self.run_stub('PreToolUse', {'tool_name': 'Write'})
        shutil.rmtree(self.kits)
        self.run_stub('PreToolUse', {'tool_name': 'Write'})
        self.assertFalse(os.path.exists(os.path.join(self.stub, '__pycache__')))
        self.assertEqual(files(self.stub), before)


class Export(unittest.TestCase):
    """The export writes the plugin's bytes unchanged and unstamped (its version stays 1.0.0), with
    Linux's hooks file at hooks/hooks.json on Linux."""

    def test_the_export_places_the_plugin_unchanged(self):
        k = Kit(self)
        stub = files(PLUGIN)
        for rel, body in stub.items():
            path = k.repo.path(PLUGIN_REL + '/' + rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(body)
        k.repo.commit('hooks plugin', *[PLUGIN_REL + '/' + rel for rel in stub])
        k.repo.git('push', '-q', 'origin', 'main')
        sha = P.remote_main(k.kit, 'origin')
        for platform, root in (('windows', 'w'), ('linux', 'l')):
            with self.subTest(platform=platform):
                folder, version = P.export(k.kit, sha, os.path.join(k.root, root), platform=platform)
                self.assertNotEqual(version, '1.0.0')
                got = files(os.path.join(folder, *PLUGIN_REL.split('/')))
                want = dict(stub)
                if platform == 'linux':
                    want['hooks/hooks.json'] = stub['hooks/hooks.linux.json']
                self.assertEqual(got, want)


if __name__ == '__main__':
    unittest.main()
