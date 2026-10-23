"""kitlib.agentview and tools/agent_view.py: what one role is given and held to (decision 18,
docs/multi-host-plan.md section 7; docs/content.md "Agent view").

Each section is checked against the live source it is built from (the host's ContentRenderer and
start_text, the kit header, the skills folder, kitlib.backlog.roles, the guard's role policy), on
the real kit content. The repository is a temp folder, and the guard's Ctx is given a classify that
answers it as a main checkout, so nothing asks the repository. The CLI runs under a temp home
(tests/home.py).
"""
import dataclasses
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from kitlib import agentview, content
from kitlib.backlog import roles as broles
from kitlib.git import repo as gitrepo
from kitlib.guard import rolepolicy
from kitlib.guard.context import Ctx
from kitlib.host import content as hcontent, header, registry

from hosts.claude.adapter import content as claude_content
from tests.home import TempHomeCase

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT, 'tools'))
import agent_view as cli  # noqa: E402

TITLES = agentview.GIVEN + agentview.HELD
HOST = registry.host(claude_content.HOST)
RENDERER = claude_content.renderer()
VALUES = RENDERER.tokens()


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
        self.calls = []

    def ctx(self):
        place = gitrepo.Place(gitrepo.MAIN, self.repo, '', None, None)
        return Ctx(environ={}, home=self.home, kit=KIT, classify=lambda path: place)

    def start_text(self, event, kit, payload, environ, part=None):
        self.calls.append((event, payload, environ) + ((part,) if part else ()))
        if part:
            return 'part %s for %s' % (part, environ['PM_KIT_ROLE']) if part != 'empty' else ''
        return header.text(kit) + '\nchecks for ' + environ['PM_KIT_ROLE']

    def view(self, name, **kw):
        kw.setdefault('environ', {})
        kw.setdefault('ctx', self.ctx())
        kw.setdefault('start_text', self.start_text)
        return agentview.view(KIT, self.repo, name, kw.pop('host', HOST), kw.pop('renderer', RENDERER), **kw)

    def section(self, sections, title):
        found = [s for s in sections if s.title.startswith(title)]
        self.assertEqual(len(found), 1, title)
        return found[0]

    def text(self, sections, title):
        return '\n'.join(self.section(sections, title).lines)


class Sections(Base):
    def test_every_section_given_first_then_held(self):
        for name in ('worker', 'pm', 'consultant', 'analyst'):
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
    def test_the_file_is_the_renderers_role_path_and_its_text_is_shown(self):
        path = RENDERER.role_path('worker')
        data = RENDERER.render(content.load(KIT))[path].decode('utf-8')
        full = self.text(self.view('worker'), 'Role file')
        self.assertIn(agentview.block(data), full)
        brief = self.section(self.view('worker', brief=True), 'Role file').lines
        self.assertIn('the host loads: %s' % path, brief)
        self.assertEqual(len(brief), 2)

    def test_a_renderer_that_writes_no_file_there_says_so(self):
        class Off(type(RENDERER)):
            def render(self, c):
                return {p: d for p, d in type(RENDERER).render(self, c).items() if p != self.role_path('worker')}
        got = self.text(self.view('worker', renderer=Off(), brief=True), 'Role file')
        self.assertIn('writes no %s' % RENDERER.role_path('worker'), got)

    def test_a_session_role_says_it_is_never_dispatched(self):
        self.assertIn('run: session', self.text(self.view('pm', brief=True), 'Role file'))
        self.assertNotIn('run: session', self.text(self.view('worker', brief=True), 'Role file'))


class Instructions(Base):
    def test_the_host_instructions_file_and_what_it_imports(self):
        name = VALUES['instructions_file']
        write(os.path.join(self.repo, name), '@AGENTS.md\n@docs/missing.md\n')
        write(os.path.join(self.repo, 'AGENTS.md'), '# rules\n@AGENTS.md\n')
        lines = self.section(self.view('worker', brief=True), 'Repository instructions').lines
        self.assertEqual(lines, [name, '  imports AGENTS.md', '  imports docs/missing.md (not there)'])
        full = self.text(self.view('worker'), 'Repository instructions')
        self.assertIn(agentview.INDENT + '# rules', full)

    def test_no_instructions_file_is_said(self):
        lines = self.section(self.view('worker', brief=True), 'Repository instructions').lines
        self.assertEqual(lines, ['%s (not there)' % VALUES['instructions_file']])

    def test_the_user_file_is_in_the_hosts_per_user_folder_and_never_read(self):
        line = self.text(self.view('worker', brief=True), 'User instructions')
        self.assertIn('%s/%s' % (VALUES['user_folder'], VALUES['instructions_file']), line)
        self.assertIn('the kit never reads it', line)
        moved = self.text(self.view('worker', brief=True, environ={VALUES['config_dir_env']: self.home}),
                          'User instructions')
        self.assertIn(os.path.join(self.home, VALUES['instructions_file']), moved)


class Start(Base):
    def test_the_header_is_kitlib_host_header(self):
        self.assertIn(agentview.block(header.text(KIT)), self.text(self.view('worker'), 'Kit header'))
        self.assertNotIn(header.text(KIT), self.text(self.view('worker', brief=True), 'Kit header'))

    def test_the_start_text_past_the_header_for_a_session_marked_with_the_role(self):
        got = self.view('pm', environ={'OTHER': '1'})
        self.assertEqual(self.section(got, 'At start').lines, [agentview.block('checks for pm')])
        self.assertIn('SessionStart', self.section(got, 'Kit header').title)
        self.assertEqual(self.calls, [('SessionStart', {'cwd': os.path.abspath(self.repo)},
                                       {'OTHER': '1', 'PM_KIT_ROLE': 'pm'})])

    def test_a_dispatched_role_is_asked_at_subagent_start(self):
        self.view('worker')
        self.assertEqual([c[0] for c in self.calls], ['SubagentStart'])

    def test_a_dispatched_role_on_claude_shows_the_agent_start_block(self):
        # The block's default budget is 1.0 s of wall time. Under the time fence (docs/flaky-tests.md)
        # it is the ceiling, so a reading slower than that (a loaded machine; here a stub that sleeps
        # past it) still finishes: the hook's own default is what is checked, with no budget passed in.
        from hosts.claude.adapter.host import ADAPTER
        from kitlib import agentstart
        real = agentstart.repo.classify
        calls = []

        def slow(path, *args, **kwargs):
            calls.append(path)
            time.sleep(1.2)
            return real(path, *args, **kwargs)
        with mock.patch.object(agentstart.repo, 'classify', slow):
            got = self.text(self.view('worker', start_text=ADAPTER.start_text), 'At start')
        self.assertEqual(len(calls), 1)
        self.assertIn('Agent start (worker):', got)
        self.assertIn('checkout  NOT CHECKED', got)      # the temp repo is in no git work tree
        self.assertNotIn('over the', got)

    def test_a_real_run_keeps_the_one_second_budget(self):
        # Without the fence's marker (a real run) the hook still gives up at 1.0 s.
        from hosts.claude.adapter.host import ADAPTER
        from kitlib import agentstart
        real = agentstart.repo.classify
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(path, *args, **kwargs):
            release.wait(60)
            return real(path, *args, **kwargs)
        with mock.patch.dict(os.environ), mock.patch.object(agentstart.repo, 'classify', stuck):
            os.environ.pop('KIT_TIME_FENCE', None)
            got = self.text(self.view('worker', start_text=ADAPTER.start_text), 'At start')
        self.assertIn('Agent start (worker):', got)
        self.assertIn('NOT CHECKED - the rest: over the 1.0 s budget', got)

    def test_nothing_past_the_header_is_said(self):
        got = self.view('worker', start_text=lambda event, kit, payload, environ, part: header.text(kit))
        self.assertEqual(self.section(got, 'At start').lines, ['nothing'])

    def test_brief_runs_no_checks(self):
        self.assertIn('run without --brief', self.text(self.view('pm', brief=True), 'At start'))
        self.assertEqual(self.calls, [])

    def test_each_part_the_role_runs_is_asked_as_the_hook_asks_it(self):
        parts = lambda event, role: (None, 'empty') + (('pm',) if role == 'pm' else ())
        got = self.view('pm', start_parts=parts)
        self.assertEqual(self.section(got, 'At start').lines,
                         [agentview.block('checks for pm'), 'part empty: nothing', 'part pm:',
                          agentview.block('part pm for pm')])
        self.assertEqual([c[3:] for c in self.calls], [(), ('empty',), ('pm',)])
        self.calls[:] = []
        self.view('coordinator', start_parts=parts)
        self.assertEqual([c[3:] for c in self.calls], [(), ('empty',)])

    def test_a_failing_part_is_a_line(self):
        def boom(event, kit, payload, environ, part=None):
            if part:
                raise ValueError('no part')
            return header.text(kit)
        got = self.view('pm', start_text=boom, start_parts=lambda event, role: (None, 'x'))
        self.assertEqual(self.section(got, 'At start').lines, ['part x: could not make it: ValueError: no part'])

    def test_a_host_with_no_start_text_is_not_guessed(self):
        self.assertIn('not known', self.text(self.view('pm', start_text=None), 'At start'))

    def test_a_failing_start_text_is_a_line(self):
        def boom(*a):
            raise OSError('no disk')
        self.assertIn('could not make it: OSError: no disk', self.text(self.view('pm', start_text=boom), 'At start'))


class GivenAndHeld(Base):
    def test_skills_are_content_skills(self):
        names = [s.name for s in content.load(KIT).skills]
        lines = self.section(self.view('worker'), 'Skills').lines
        self.assertEqual([line.split(':', 1)[0] for line in lines], names)
        brief = self.text(self.view('worker', brief=True), 'Skills')
        self.assertIn(', '.join(names), brief)

    def test_a_tool_list_without_the_skill_tool_is_said(self):
        role = next(r for r in content.load(KIT).roles if not r.all_tools and 'skill' not in r.capabilities)
        self.assertIn('cannot load these', self.text(self.view(role.name, brief=True), 'Skills'))
        self.assertNotIn('cannot load these', self.text(self.view('worker', brief=True), 'Skills'))

    def test_a_host_without_skills_lists_none(self):
        caps = dataclasses.replace(HOST.capabilities, skills=False)
        host = dataclasses.replace(HOST, capabilities=caps)
        self.assertIn('none', self.text(self.view('worker', brief=True, host=host), 'Skills'))

    def test_the_profile_in_use_or_none(self):
        rel = VALUES['profile_path']
        none = self.section(self.view('worker', brief=True), 'Repo profile').lines[0]
        self.assertIn('none', none)
        # The profile is opt-in (decision 26): with none, the role reads the kit's default.
        default = os.path.join(KIT, 'content', 'default-profile.md')
        self.assertTrue(os.path.isfile(default))
        self.assertTrue(none.endswith("; in use: the kit's default, %s" % default), none)
        write(os.path.join(self.repo, *rel.split('/')), 'profile\n')
        self.assertEqual(self.section(self.view('worker', brief=True), 'Repo profile').lines[0],
                         'profile_path: %s' % rel)

    def test_settings_and_tools_are_the_renderers(self):
        c = content.load(KIT)
        role = next(r for r in c.roles if not r.all_tools)
        text = self.text(self.view(role.name, brief=True), 'Model, tools')
        tools = RENDERER.tools()
        for cap in role.capabilities:
            self.assertIn('%s -> %s' % (cap, tools[cap]), text)
        self.assertIn('; '.join('%s %s' % kv for kv in RENDERER.settings(role)), text)
        self.assertIn('mode: %s' % role.mode, text)

    def test_a_file_with_no_model_says_the_host_default_runs(self):
        class NoModel(type(RENDERER)):
            def settings(self, role, *a):
                return [kv for kv in type(RENDERER).settings(self, role, *a) if kv[0] != 'model']
        self.assertIn('default model', self.text(self.view('worker', renderer=NoModel(), brief=True), 'Model, tools'))

    def test_backlog_rights_are_the_policy_rows(self):
        for name, action, want in (('worker', 'pull', 'may'), ('analyst', 'add', 'may not'),
                                   ('analyst', 'read', 'may'), ('lander', 'close', 'only through the landing tool'),
                                   ('worker', 'release', 'only on a record it pulled itself')):
            with self.subTest(role=name, action=action):
                lines = self.section(self.view(name, brief=True), 'Backlog rights').lines
                row = next(line for line in lines if (' %s (' % action) in ' ' + line.split(': ', 1)[-1])
                self.assertEqual(row.split(': ', 1)[0], want, row)

    def test_every_action_is_listed_once(self):
        lines = self.section(self.view('worker', brief=True), 'Backlog rights').lines
        listed = ' '.join(lines[1:-1])
        for action in broles.ACTIONS:
            self.assertIn('%s (' % action, listed)


class Switches(Base):
    def test_policy_off_with_no_kit_json(self):
        text = self.text(self.view('analyst', brief=True), '.kit.json')
        self.assertIn('none: every default', text)
        self.assertIn('guard.role_policy: false', text)
        self.assertIn('mode as the guard reads it: none', text)

    def test_policy_on_and_a_roles_override_are_what_the_guard_reads(self):
        write(os.path.join(self.repo, '.kit.json'), json.dumps(
            {'guard': {'role_policy': True}, 'roles': {'analyst': {'mode': 'test'}}}))
        text = self.text(self.view('analyst', brief=True), '.kit.json')
        self.assertIn('guard.role_policy: true', text)
        self.assertIn("roles.analyst: {'mode': 'test'}", text)
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
    def test_the_rules_are_the_role_policys_rows_for_the_mode(self):
        for name in ('analyst', 'worker', 'lander', 'test-runner'):
            role = content.load(KIT).role(name)
            with self.subTest(role=name):
                text = self.text(self.view(name, brief=True), 'Guard rules')
                for rule, said in rolepolicy.rules_for(role.mode):
                    self.assertIn('- %s: %s' % (rule, said), text)
                self.assertIn(rolepolicy.verbs_text(rolepolicy.LOCK_VERBS[role.mode]), text)

    def test_a_read_mode_is_held_to_more_than_a_write_mode(self):
        read = self.text(self.view('analyst', brief=True), 'Guard rules')
        write_ = self.text(self.view('worker', brief=True), 'Guard rules')
        self.assertIn('role-edit:', read)
        self.assertNotIn('role-edit:', write_)
        self.assertNotIn('write-jail', write_)
        owner = next(line for line in read.splitlines() if 'owner paths' in line)
        for p in registry.union('owner_paths'):
            self.assertIn(p, owner)

    def test_a_role_file_writes_list_is_named(self):
        """No kit role names a `writes` path now, so the role is a synthetic role file in a temp folder."""
        path = os.path.join(self.repo, 'roles', 'scribe.md')
        write(path, '---\nname: scribe\ndescription: Notes.\ntier: standard\nsandbox: read-only\n'
                    'mode: read\nwrites: [docs/ops/notes.local.md, notes/*.txt]\ncapabilities: all\n---\nbody\n')
        with open(path, 'rb') as f:
            role = hcontent.parse_role(f.read(), path)
        self.assertEqual(role.writes, ('docs/ops/notes.local.md', 'notes/*.txt'))
        text = '\n'.join(agentview.guard_rules(role).lines)
        for w in role.writes:
            self.assertIn(w, text)
        self.assertIn('(its writes: docs/ops/notes.local.md, notes/*.txt)', text)


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

    def test_a_role_prints_every_section_numbered(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        for host in registry.names():
            with self.subTest(host=host):
                code, out, _ = self.run_cli('worker', '--brief', '--host', host, '-C', repo)
                self.assertEqual(code, 0, out)
                for n, t in enumerate(TITLES, 1):
                    self.assertIn('== %d. %s' % (n, t), out)
                self.assertIn('on host %s' % host, out)
                self.assertIn('Sections 1-%d: what it is given' % len(agentview.GIVEN), out)

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
