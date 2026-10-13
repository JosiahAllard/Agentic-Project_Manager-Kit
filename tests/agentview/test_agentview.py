"""kitlib.agentview and tools/agent_view.py: what one role is given and held to (decision 18,
docs/multi-host-plan.md section 7; docs/content.md "Agent view").

Each section is checked against the live source it is built from (the host's renderer, the kit
header, the skills folder, kitlib.backlog.roles, the guard's role policy), on the real kit content.
The repository is a temp folder, and the guard's Ctx is given a classify that answers it as a main
checkout, so nothing asks the repository. The CLI runs under a temp home (tests/home.py).
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from kitlib import agentview, content
from kitlib.backlog import roles as broles
from kitlib.git import repo as gitrepo
from kitlib.guard import rolepolicy
from kitlib.guard.context import Ctx
from kitlib.host import header, registry

from hosts.claude.adapter import content as claude_content
from tests.home import TempHomeCase

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT, 'tools'))
import agent_view as cli  # noqa: E402

TITLES = ('Role file', 'Repository instructions', 'User instructions', 'Kit header', 'At start',
          'Skills', 'Repo profile', 'Model, mode and capabilities', 'Backlog rights', '.kit.json switches',
          'Guard rules for its mode')
HOST = registry.host(claude_content.HOST)
VALUES = claude_content.tokens()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.ran = []

    def ctx(self):
        place = gitrepo.Place(gitrepo.MAIN, self.repo, '', None, None)
        return Ctx(environ={}, home=self.home, kit=KIT, classify=lambda path: place)

    def view(self, name, **kw):
        kw.setdefault('values', VALUES)
        kw.setdefault('tools', claude_content.TOOLS)
        kw.setdefault('environ', {})
        kw.setdefault('ctx', self.ctx())
        kw.setdefault('checklists', {'pm': lambda: self.ran.append('pm') or 'PM LIST',
                                     'coordinator': lambda repo: self.ran.append(repo) or 'COORD LIST'})
        return agentview.view(KIT, self.repo, name, kw.pop('host', HOST), kw.pop('renderer', claude_content.renderer()),
                              **kw)

    def section(self, sections, title):
        found = [s for s in sections if s.title.startswith(title)]
        self.assertEqual(len(found), 1, title)
        return found[0]

    def text(self, sections, title):
        return '\n'.join(self.section(sections, title).lines)


class Sections(Base):
    def test_every_section_in_the_order_the_agent_gets_them(self):
        for name in ('worker', 'pm', 'consultant', 'reviewer'):
            with self.subTest(role=name):
                got = [s.title for s in self.view(name, brief=True)]
                self.assertEqual(len(got), len(TITLES))
                for t, want in zip(got, TITLES):
                    self.assertTrue(t.startswith(want), (t, want))

    def test_unknown_role_names_every_role(self):
        with self.assertRaises(agentview.UnknownRole) as cm:
            self.view('nobody')
        self.assertEqual(cm.exception.known, tuple(r.name for r in content.roster(KIT)))
        self.assertIn('worker', str(cm.exception))

    def test_an_old_name_is_its_new_role(self):
        old, new = next(iter(content.ALIASES.items()))
        self.assertIn('content/roles/%s.md' % new, self.text(self.view(old, brief=True), 'Role file'))


class RoleFile(Base):
    def test_the_rendered_file_is_the_renderers_and_its_text_is_shown(self):
        r = claude_content.renderer()
        c = content.load(KIT)
        files = agentview.role_files(c.role('worker'), c, r)
        self.assertEqual(list(files), ['%s/worker.md' % claude_content.AGENTS_REL])
        self.assertEqual(files['%s/worker.md' % claude_content.AGENTS_REL], r.render(c)['%s/worker.md' % claude_content.AGENTS_REL])
        full = self.text(self.view('worker'), 'Role file')
        self.assertIn(files['%s/worker.md' % claude_content.AGENTS_REL].decode('utf-8').rstrip('\n'), full)
        brief = self.section(self.view('worker', brief=True), 'Role file').lines
        self.assertIn('rendered: %s/worker.md' % claude_content.AGENTS_REL, brief)
        self.assertFalse(any(line.startswith('---') for line in brief))

    def test_every_host_renders_one_file_per_role(self):
        import importlib
        c = content.load(KIT)
        for h in registry.names():
            module = importlib.import_module(registry.host(h).adapter).ADAPTER.content()
            for role in c.roles:
                with self.subTest(host=h, role=role.name):
                    self.assertEqual(len(agentview.role_files(role, c, module.renderer())), 1)


class Instructions(Base):
    def test_the_host_instructions_file_and_what_it_imports(self):
        name = VALUES['instructions_file']
        write(os.path.join(self.repo, name), '@AGENTS.md\n@docs/missing.md\n')
        write(os.path.join(self.repo, 'AGENTS.md'), '# rules\n@AGENTS.md\n')
        lines = self.section(self.view('worker', brief=True), 'Repository instructions').lines
        self.assertEqual(lines, [name, '  imports AGENTS.md', '  imports docs/missing.md (not there)'])
        full = self.text(self.view('worker'), 'Repository instructions')
        self.assertIn('# rules', full)

    def test_no_instructions_file_is_said(self):
        lines = self.section(self.view('worker', brief=True), 'Repository instructions').lines
        self.assertEqual(lines, ['%s (not there)' % VALUES['instructions_file']])

    def test_the_user_file_is_in_the_hosts_per_user_folder_and_never_read(self):
        line = self.text(self.view('worker', brief=True), 'User instructions')
        self.assertIn('~/%s/%s' % (HOST.profile.state_directory, VALUES['instructions_file']), line)
        self.assertIn('not read by the kit, may exist', line)
        moved = self.text(self.view('worker', brief=True, environ={VALUES['config_dir_env']: self.home}),
                          'User instructions')
        self.assertIn(os.path.join(self.home, VALUES['instructions_file']), moved)


class Start(Base):
    def test_the_header_is_kitlib_host_header(self):
        self.assertIn(header.text(KIT), self.section(self.view('worker', brief=True), 'Kit header').lines)

    def test_a_dispatched_role_gets_the_subagent_header_and_no_checklist(self):
        got = self.view('worker')
        self.assertIn('SubagentStart', self.section(got, 'Kit header').title)
        self.assertIn('nothing', self.text(got, 'At start'))
        self.assertEqual(self.ran, [])

    def test_the_pm_and_the_coordinator_get_their_checklists(self):
        self.assertIn('PM LIST', self.text(self.view('pm'), 'At start'))
        self.assertIn('COORD LIST', self.text(self.view('coordinator'), 'At start'))
        self.assertEqual(self.ran, ['pm', self.repo])

    def test_brief_runs_no_checks(self):
        self.assertIn('kitlib.pmstart.checklist', self.text(self.view('pm', brief=True), 'At start'))
        self.assertEqual(self.ran, [])

    def test_a_host_start_text_past_the_header_is_shown(self):
        calls = []

        def host_start(event, kit, payload, environ):
            calls.append((event, payload, environ))
            return header.text(kit) + '\nchecks for ' + environ['PM_KIT_ROLE']
        self.assertEqual(self.section(self.view('pm', host_start=host_start), 'At start').lines,
                         ['checks for pm'])
        self.assertEqual(calls, [('SessionStart', {'cwd': os.path.abspath(self.repo)}, {'PM_KIT_ROLE': 'pm'})])
        self.assertEqual(self.ran, [])


class GivenAndHeld(Base):
    def test_skills_are_content_skills(self):
        lines = self.section(self.view('worker', brief=True), 'Skills').lines
        names = [s.name for s in content.load(KIT).skills]
        self.assertEqual([line.split(' ', 1)[0] for line in lines], names)

    def test_a_host_without_skills_lists_none(self):
        import dataclasses
        caps = dataclasses.replace(HOST.capabilities, skills=False)
        host = dataclasses.replace(HOST, capabilities=caps)
        self.assertIn('none', self.text(self.view('worker', brief=True, host=host), 'Skills'))

    def test_the_profile_in_use_or_none(self):
        rel = VALUES['profile_path']
        self.assertIn('none', self.text(self.view('worker', brief=True), 'Repo profile'))
        write(os.path.join(self.repo, *rel.split('/')), 'profile\n')
        self.assertEqual(self.section(self.view('worker', brief=True), 'Repo profile').lines[0],
                         'profile_path: %s' % rel)

    def test_capabilities_are_the_hosts_tools_and_the_model_its_tier_map(self):
        c = content.load(KIT)
        role = next(r for r in c.roles if not r.all_tools)
        text = self.text(self.view(role.name, brief=True), 'Model, mode')
        for cap in role.capabilities:
            self.assertIn('  %s -> %s' % (cap, claude_content.TOOLS[cap]), text)
        model = role.model_for(HOST.name, claude_content.renderer().tier_models())
        self.assertIn('tier %s -> model %s' % (role.tier, model), text)
        self.assertIn('mode: %s' % role.mode, text)

    def test_backlog_rights_are_the_policy_rows(self):
        for name, action, want in (('worker', 'pull', 'yes'), ('reviewer', 'add', 'no'),
                                   ('reviewer', 'read', 'yes'), ('lander', 'close', 'only with %s' % broles.VIA_LAND)):
            with self.subTest(role=name, action=action):
                lines = self.section(self.view(name, brief=True), 'Backlog rights').lines
                self.assertEqual(len(lines), 1 + len(broles.ACTIONS))
                row = next(line for line in lines if line.startswith(action + ' ('))
                self.assertTrue(row.endswith(': ' + want), row)

    def test_a_fact_grant_is_named(self):
        lines = self.section(self.view('worker', brief=True), 'Backlog rights').lines
        row = next(line for line in lines if line.startswith('release ('))
        self.assertTrue(row.endswith('only with %s' % broles.OWN_PULL), row)


class Switches(Base):
    def test_policy_off_with_no_kit_json(self):
        text = self.text(self.view('reviewer', brief=True), '.kit.json')
        self.assertIn('none: every default', text)
        self.assertIn('guard.role_policy: false', text)
        self.assertIn('mode as the guard reads it: none', text)

    def test_policy_on_and_a_roles_override_are_what_the_guard_reads(self):
        write(os.path.join(self.repo, '.kit.json'), json.dumps(
            {'guard': {'role_policy': True}, 'roles': {'reviewer': {'mode': 'test'}}}))
        text = self.text(self.view('reviewer', brief=True), '.kit.json')
        self.assertIn('guard.role_policy: true', text)
        self.assertIn("roles.reviewer: {'mode': 'test'}", text)
        self.assertIn('mode as the guard reads it: test', text)
        self.assertIn('edit_main_checkout: False', text)

    def test_a_session_role_is_read_from_its_mark(self):
        write(os.path.join(self.repo, '.kit.json'), json.dumps({'guard': {'role_policy': True}}))
        text = self.text(self.view('consultant', brief=True), '.kit.json')
        self.assertIn('mode as the guard reads it: read', text)

    def test_a_broken_kit_json_is_said(self):
        write(os.path.join(self.repo, '.kit.json'), '{"nope": 1}')
        self.assertIn('does not load', self.text(self.view('worker', brief=True), '.kit.json'))


class GuardRules(Base):
    def test_rule_text_comes_from_the_role_policy(self):
        texts = agentview.rule_text()
        self.assertEqual(sorted(texts), ['role-edit', 'role-lock', 'role-shell', 'role-workdir'])
        self.assertTrue(all(t for t in texts.values()))

    def test_a_read_mode_is_held_to_more_than_a_write_mode(self):
        read = self.text(self.view('reviewer', brief=True), 'Guard rules')
        write_ = self.text(self.view('worker', brief=True), 'Guard rules')
        self.assertIn('role-edit:', read)
        self.assertNotIn('role-edit:', write_)
        self.assertIn(rolepolicy.verbs_text(rolepolicy.LOCK_VERBS['read']), read)
        self.assertIn(rolepolicy.verbs_text(rolepolicy.LOCK_VERBS['write']), write_)
        for p in registry.union('owner_paths'):
            self.assertIn(p, read)

    def test_a_role_file_writes_list_is_named(self):
        role = next(r for r in content.load(KIT).roles if r.writes)
        text = self.text(self.view(role.name, brief=True), 'Guard rules')
        for w in role.writes:
            self.assertIn(w, text)


class Cli(TempHomeCase):
    def setUp(self):
        super().setUp()
        nowhere = gitrepo.Place(gitrepo.NONE, None, 'not a repository', None, None)
        patch = mock.patch.object(agentview, '_ctx', lambda kit, ctx: Ctx(environ={}, home=self.home, kit=kit,
                                                                           classify=lambda path: nowhere))
        patch.start()
        self.addCleanup(patch.stop)
    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            try:
                code = cli.main(list(argv), out, environ if environ is not None else {})
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_unknown_role_exits_2_naming_the_roles(self):
        code, out, _ = self.run_cli('nobody', '--brief')
        self.assertEqual(code, cli.USAGE)
        for r in content.roster(KIT):
            self.assertIn(r.name, out)

    def test_unknown_host_exits_2(self):
        code, out, _ = self.run_cli('worker', '--host', 'nope')
        self.assertEqual(code, cli.USAGE)
        self.assertIn('nope', out)
        code, out, _ = self.run_cli('worker', environ={registry.ENV: 'nope'})
        self.assertEqual(code, cli.USAGE)

    def test_no_role_is_usage(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, cli.USAGE)
        self.assertIn('USAGE:', err)

    def test_a_role_prints_every_section(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        for host in registry.names():
            with self.subTest(host=host):
                code, out, _ = self.run_cli('worker', '--brief', '--host', host, '-C', repo)
                self.assertEqual(code, 0, out)
                for t in TITLES:
                    self.assertIn('== %s' % t, out)
                self.assertIn('on host %s' % host, out)

    def test_the_default_host_is_pm_kit_host_else_the_registry_default(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        other = next(h for h in registry.names() if h != registry.default())
        _, out, _ = self.run_cli('worker', '--brief', '-C', repo, environ={registry.ENV: other})
        self.assertIn('on host %s' % other, out)
        _, out, _ = self.run_cli('worker', '--brief', '-C', repo)
        self.assertIn('on host %s' % registry.default(), out)


if __name__ == '__main__':
    unittest.main()
