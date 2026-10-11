"""The plugin's hooks (.claude-plugin/plugin.json "hooks", docs/install.md "Plugin install"): one
coordinator SessionStart hook in exec form, its script under ${CLAUDE_PLUGIN_ROOT}; guard.py and the
PM's hook stay in settings, and nothing depends on the plugin's name."""
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
        self.assertEqual(self.rel, './claude/hooks/hooks.json')
        self.assertTrue(os.path.isfile(self.path), self.path)

    def test_only_the_coordinator_session_start_hook(self):
        hooks = self.hooks()
        self.assertEqual(list(hooks), ['SessionStart'])
        self.assertEqual([g['matcher'] for g in hooks['SessionStart']], ['startup|resume|compact'])
        scripts = [os.path.basename(a) for _, h in self.all_hooks() for a in h['args']]
        self.assertEqual(scripts, ['coordinator_session_start.py'])

    def test_every_hook_is_exec_form_and_runs_a_kit_file(self):
        for event, h in self.all_hooks():
            self.assertEqual(h['type'], 'command')
            self.assertNotRegex(h['command'], r'[\s/\\]', h)          # one program, no shell line
            self.assertIsInstance(h['args'], list)
            for a in h['args']:
                self.assertTrue(a.startswith(PREFIX), a)
                self.assertTrue(os.path.isfile(os.path.join(ROOT, *a[len(PREFIX):].split('/'))), a)

    def test_guard_and_the_pm_hook_stay_in_settings(self):
        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('guard.py', text)
        self.assertNotIn('pm_session_start', text)

    def test_nothing_names_the_plugin(self):
        with open(self.path, encoding='utf-8') as f:
            self.assertNotIn(self.manifest['name'], f.read())


if __name__ == '__main__':
    unittest.main()
