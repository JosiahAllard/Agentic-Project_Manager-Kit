"""The generated-file rule (docs/guard.md "Generated files"): in a kit checkout, no session edits a
file tools/render_content.py writes; the refusal names the source to edit and the render command.
The renderer itself runs as a process and is never refused. kitlib.guard.generated maps a rendered
path to its source."""
import importlib
import json
import os
import shutil
import tempfile
import unittest

from kitlib import content as kcontent
from kitlib.guard import context, generated, rules
from kitlib.host import hookmain, registry

from . import world as W
from .claudehost import decide
from .world import bash, event, ps

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def touch(path, text='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def kit_layout(top):
    touch(os.path.join(top, 'content', 'roles', 'worker.md'))
    touch(os.path.join(top, 'tools', 'render_content.py'))


class Source(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.top = self.w.wt
        kit_layout(self.top)

    def test_each_rendered_path_names_its_source(self):
        manifest = 'kit.json, hosts.json and hosts/%s/adapter/content.py'
        for rel, want in (
                ('hosts/claude/shell/agents/worker.md', 'content/roles/worker.md'),
                ('hosts/codex/shell/agents/kit-worker.toml', 'content/roles/worker.md'),
                ('hosts/codex/shell/skills/land-branch/SKILL.md', 'content/skills/land-branch/SKILL.md'),
                ('hosts/claude/shell/skills/land-branch/SKILL.md', 'content/skills/land-branch/SKILL.md'),
                ('hosts/codex/shell/boot/hook.py', 'boot/hook.py'),
                ('hosts/codex/shell/hosts.json', 'hosts.json'),
                ('hosts/codex/shell/plugin.json', manifest % 'codex'),
                ('hosts/codex/shell/mcp.json', manifest % 'codex'),
                ('hosts/claude/shell/hooks/hooks.json', manifest % 'claude'),
                ('hosts/claude/shell/hooks/hooks.linux.json', manifest % 'claude'),
                ('hosts/codex/shell/hooks/hooks.linux.json', manifest % 'codex'),
                ('hosts/codex/shell/mcp.linux.json', manifest % 'codex'),
                ('.claude-plugin/plugin.json', manifest % 'claude'),
                ('.claude-plugin/plugin.linux.json', manifest % 'claude'),
                ('.claude-plugin/marketplace.json', manifest % 'claude'),
                ('hosts/claude/shell/agents', 'content/roles/'),                  # the folders themselves
                ('hosts/codex/shell/skills/', 'content/skills/'),
                ('hosts/codex/shell/boot', 'boot/'),
                ('hosts\\claude\\shell\\agents\\worker.md', 'content/roles/worker.md'),
                ('HOSTS/Claude/Shell/Agents/worker.md', 'content/roles/worker.md')):
            with self.subTest(rel):
                self.assertEqual(generated.source(self.top, rel), want)

    def test_sources_and_hand_written_host_files_are_not_rendered(self):
        for rel in ('content/roles/worker.md', 'content/skills/a/SKILL.md', 'kit.json', 'hosts.json',
                    'hosts/codex/shell/scripts/launch.py', 'hosts/codex/shell/mcp/lock_server.py',
                    'hosts/claude/adapter/content.py',
                    'docs/ROSTER.md', 'hosts/claude/shell/../shell/agents/worker.md'):
            with self.subTest(rel):
                self.assertIsNone(generated.source(self.top, rel))

    def test_a_checkout_without_the_kit_layout_has_none(self):
        self.assertIsNone(generated.source(self.w.main, 'hosts/claude/shell/agents/worker.md'))
        self.assertIsNone(generated.source(None, 'hosts/claude/shell/agents/worker.md'))


class EveryRenderedManifest(unittest.TestCase):
    """generated.MANIFESTS names each hooks file by hand. This ties it to hosts.json: each host's
    renderer, given every platform the hook interpreter knows (hookmain.INTERPRETERS, a superset of
    the hosts' own platforms), writes no manifest the rule leaves unguarded, so a new platform's
    `hooks/hooks.<platform>.json` fails here before it can be rendered unguarded."""

    def registry_with_every_platform(self):
        with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
            raw = json.load(f)
        known = [p for p in registry.PLATFORMS if p in hookmain.INTERPRETERS]
        for name in registry.names():
            entry = raw[name]
            self.assertLessEqual(set(entry['platforms']), set(known), name)
            entry['platforms'] = known
            names = entry['profile']['process_names']
            for p in known:
                names.setdefault(p, names[next(iter(names))])
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, 'hosts.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(raw, f)
        return path, known

    def test_each_hosts_manifests_for_each_platform_are_generated_files(self):
        path, known = self.registry_with_every_platform()
        self.assertIn('linux', known)
        kit, content = kcontent.load_kit(ROOT), kcontent.load(ROOT)
        for name in registry.names():
            r = importlib.import_module(registry.host(name).adapter).ADAPTER.content().renderer()
            files = r.manifests(kit, content, registry_path=path)
            hooks = [rel for rel in files if os.path.basename(rel).startswith('hooks.')]
            # Codex's hooks are its fixed-bytes pm-kit-hooks plugin (hosts/codex/shell/hooks-plugin),
            # never rendered.
            self.assertEqual(len(hooks), 0 if name == 'codex' else len(known), (name, sorted(files)))
            for rel in files:
                with self.subTest(host=name, path=rel):
                    self.assertIsNotNone(generated.source(ROOT, rel))


class Rule(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        kit_layout(self.w.wt)
        self.agent = os.path.join(self.w.wt, 'hosts', 'claude', 'shell', 'agents', 'worker.md')

    def write(self, target, sub=True, env=None, tool='Write'):
        ev = event(tool, {'file_path': target, 'content': 'x', 'old_string': 'a', 'new_string': 'b'}, self.w.wt, sub)
        return decide(ev, self.w.ctx(environ=env or {}))

    def refused(self, d):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', 'generated-file'), d.reason)
        return d

    def test_an_edit_of_an_agent_file_names_the_role_file_and_the_render(self):
        for tool in ('Write', 'Edit', 'MultiEdit'):
            with self.subTest(tool):
                d = self.refused(self.write(self.agent, tool=tool))
                self.assertIn('content/roles/worker.md', d.reason)
                tool_path = W.fwd(os.path.join(self.w.wt, 'tools', 'render_content.py'))
                self.assertEqual(d.instead, 'python "%s" --write' % tool_path)
                self.assertTrue(d.reason.endswith(rules.tag('generated-file', 'subagent')), d.reason)

    def test_every_session_is_refused(self):
        self.refused(self.write(self.agent, sub=False))                                   # the owner's own
        self.refused(self.write(self.agent, sub=False, env={context.ROLE_ENV: 'coordinator'}))

    def test_skills_copies_and_manifests_are_refused(self):
        for rel in (('hosts', 'codex', 'shell', 'skills', 'land-branch', 'SKILL.md'),
                    ('hosts', 'claude', 'shell', 'skills', 'new', 'SKILL.md'),            # a retired folder too
                    ('.claude-plugin', 'plugin.json'), ('hosts', 'codex', 'shell', 'hooks', 'hooks.json'),
                    ('hosts', 'claude', 'shell', 'hooks', 'hooks.linux.json')):
            with self.subTest(rel):
                self.refused(self.write(os.path.join(self.w.wt, *rel)))

    def test_a_shell_write_is_refused(self):
        self.refused(decide(bash('echo x > hosts/claude/shell/agents/worker.md', self.w.wt, True), self.w.ctx()))
        text = 'Set-Content hosts/codex/shell/agents/kit-worker.toml x'
        self.refused(decide(ps(text, self.w.wt, True), self.w.ctx()))
        for text in ('rm -rf hosts/claude/shell/agents', 'mv a hosts/codex/shell/skills/x/SKILL.md'):
            with self.subTest(text):
                self.refused(decide(bash(text, self.w.wt, False), self.w.ctx()))
        self.refused(decide(ps('Remove-Item -Recurse hosts\\codex\\shell\\skills', self.w.wt, True), self.w.ctx()))

    def test_git_that_restores_or_removes_rendered_files_is_not_judged_by_it(self):
        """A checkout, merge or `git rm` writes what a commit holds, never a hand edit."""
        for text in ('git checkout -- hosts/claude/shell/agents/worker.md', 'git rm hosts/claude/shell/agents/worker.md',
                     'git restore --source main hosts/claude/shell/agents', 'git merge --no-commit main'):
            with self.subTest(text):
                self.assertIsNone(decide(bash(text, self.w.wt, True), self.w.ctx()))

    def test_the_renderer_and_the_sources_are_not(self):
        for text in ('python tools/render_content.py --write',
                     'python "%s/tools/render_content.py" -C "%s" --write' % (W.fwd(self.w.wt), W.fwd(self.w.wt))):
            with self.subTest(text):
                self.assertIsNone(decide(bash(text, self.w.wt, True), self.w.ctx()))
        self.assertIsNone(self.write(os.path.join(self.w.wt, 'content', 'roles', 'worker.md')))
        self.assertIsNone(self.write(os.path.join(self.w.wt, 'hosts', 'codex', 'shell', 'scripts', 'launch.py')))

    def test_a_repository_that_is_not_the_kit_is_not_judged(self):
        os.remove(os.path.join(self.w.wt, 'tools', 'render_content.py'))
        self.assertIsNone(self.write(self.agent))


if __name__ == '__main__':
    unittest.main()
