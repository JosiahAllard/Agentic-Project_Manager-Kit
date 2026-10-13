"""The plugin's hooks (.claude-plugin/plugin.json "hooks", docs/install.md "Plugin install"): the kit
header on SessionStart and SubagentStart and the coordinator SessionStart hook, in exec form, each
the kit's one hook entry ${CLAUDE_PLUGIN_ROOT}/boot/hook.py; the guard and the PM's hook stay in
settings, and nothing depends on the plugin's name. The files are rendered (tests/content
ManifestTest)."""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(ROOT, '.claude-plugin', 'plugin.json')
PREFIX = '${CLAUDE_PLUGIN_ROOT}/'


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


class PluginHooks(unittest.TestCase):
    def setUp(self):
        self.manifest = load(MANIFEST)
        self.rel = self.manifest.get('hooks')
        self.path = os.path.join(ROOT, os.path.normpath(self.rel or 'missing'))

    def hooks(self):
        return load(self.path)['hooks']

    def all_hooks(self):
        for event, groups in self.hooks().items():
            for g in groups:
                for h in g['hooks']:
                    yield event, h

    def test_the_manifest_names_the_hooks_file(self):
        self.assertEqual(self.rel, './hosts/claude/shell/hooks/hooks.json')
        self.assertTrue(os.path.isfile(self.path), self.path)

    def test_the_kit_header_and_the_coordinator_session_start_hook(self):
        hooks = self.hooks()
        self.assertEqual(sorted(hooks), ['SessionStart', 'SubagentStart'])
        runs = sorted((event, g.get('matcher'), h['args'])
                      for event, groups in hooks.items() for g in groups for h in g['hooks'])
        entry = PREFIX + 'boot/hook.py'
        self.assertEqual(runs, [
            ('SessionStart', 'startup|resume|clear|compact', [entry, 'claude', 'SessionStart']),
            ('SessionStart', 'startup|resume|compact', [entry, 'claude', 'SessionStart', 'coordinator']),
            ('SubagentStart', None, [entry, 'claude', 'SubagentStart'])])

    def test_every_hook_is_exec_form_and_runs_a_kit_file(self):
        for event, h in self.all_hooks():
            self.assertEqual(h['type'], 'command')
            self.assertNotRegex(h['command'], r'[\s/\\]', h)          # one program, no shell line
            self.assertIsInstance(h['args'], list)
            a = h['args'][0]
            self.assertTrue(a.startswith(PREFIX), a)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *a[len(PREFIX):].split('/'))), a)

    def test_guard_and_the_pm_hook_stay_in_settings(self):
        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('PreToolUse', text)
        self.assertNotIn('"pm"', text)

    def test_nothing_names_the_plugin(self):
        with open(self.path, encoding='utf-8') as f:
            self.assertNotIn(self.manifest['name'], f.read())


if __name__ == '__main__':
    unittest.main()
