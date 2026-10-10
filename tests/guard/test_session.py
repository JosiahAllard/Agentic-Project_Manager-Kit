"""The session mark (kitlib.sessionmark, docs/guard.md "Session mark") and the rules that read it:
who counts as the owner for the owner-only lock tools, which sessions are agent sessions for
kill-by-name and lock-state, the owner-identity rule, and the lock MCP server's owner check."""
import importlib.util
import os
import unittest
from unittest import mock

from kitlib import sessionmark
from kitlib.guard import context, decide, rules
from kitlib.lock import api
from kitlib.lockmcp import server as lockmcp

from . import world as W
from .world import bash, event, ps

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COORDINATOR = {context.ROLE_ENV: 'coordinator'}
PM = {context.ROLE_ENV: 'pm'}
OWNER_EMAIL = 'owner@example.com'


class Mark(unittest.TestCase):
    def test_unset_or_blank_is_unmarked(self):
        for env in ({}, {sessionmark.ENV: ''}, {sessionmark.ENV: '   '}):
            self.assertIsNone(sessionmark.mark(env), env)

    def test_a_mark_is_stripped(self):
        self.assertEqual(sessionmark.mark({sessionmark.ENV: ' coordinator '}), 'coordinator')

    def test_a_reserved_value_never_reads_as_owner_or_subagent(self):
        for value in ('owner', 'OWNER', 'subagent'):
            self.assertEqual(sessionmark.mark({sessionmark.ENV: value}), 'marked-' + value)

    def test_the_session_role(self):
        role = context.default_session_role
        self.assertEqual(role({}, {}), context.OWNER)
        self.assertEqual(role({}, COORDINATOR), 'coordinator')
        self.assertEqual(role({'agent_id': 'a1'}, {}), context.SUBAGENT)
        self.assertEqual(role({'agent_id': 'a1'}, COORDINATOR), context.SUBAGENT)
        self.assertEqual(role({}, {sessionmark.ENV: 'owner'}), 'marked-owner')


class OwnerCheck(unittest.TestCase):
    """The lock MCP server's owner check: the default owner, or (in a session nothing marks) a
    caller under the session's agent host."""

    OWNER = {'role': 'owner', 'claude': None}
    UNDER_HOST = {'role': 'agent', 'claude': [100, 1.5]}
    UNREAD = {'role': 'agent', 'claude': None}           # the tree could not be read

    def test_unmarked(self):
        check = sessionmark.owner_check({})
        self.assertIsNone(check.marked)
        self.assertTrue(check(self.OWNER))
        self.assertTrue(check(self.UNDER_HOST))
        self.assertFalse(check(self.UNREAD))
        self.assertFalse(check(None))

    def test_marked(self):
        check = sessionmark.owner_check(COORDINATOR)
        self.assertEqual(check.marked, 'coordinator')
        self.assertTrue(check(self.OWNER))
        self.assertFalse(check(self.UNDER_HOST))

    def test_the_environment_is_read_once(self):
        env = dict(COORDINATOR)
        check = sessionmark.owner_check(env)
        env.pop(sessionmark.ENV)
        self.assertFalse(check(self.UNDER_HOST))

    def test_lock_server_main_installs_it(self):
        spec = importlib.util.spec_from_file_location('kit_lock_server_entry', os.path.join(REPO, 'mcp', 'lock_server.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old = api.set_owner_check(None)
        self.addCleanup(api.set_owner_check, old)
        with mock.patch.object(mod, 'serve') as serve, mock.patch.object(mod.sys, 'stdout'):
            self.assertEqual(mod.main(environ={}), 0)
            serve.assert_called_once()
            self.assertTrue(api.is_owner(self.UNDER_HOST))
            self.assertEqual(mod.main(environ=COORDINATOR), 0)
            self.assertFalse(api.is_owner(self.UNDER_HOST))
            self.assertTrue(api.is_owner(self.OWNER))


class LockMcpByMark(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def ev(self, tool, sub=False):
        return event(tool, {'id': 'h-1'}, self.w.wt, sub=sub)

    def test_owner_only_names_are_lock_tools(self):
        self.assertTrue(set(lockmcp.OWNER_ONLY) <= set(lockmcp.TOOLS))

    def test_unmarked_main_session_is_the_owner(self):
        for name in lockmcp.OWNER_ONLY:
            self.assertIsNone(decide(self.ev(rules.MCP_PREFIX + name), self.w.ctx(environ={})), name)

    def test_marked_and_subagent_sessions_are_denied(self):
        name = rules.MCP_PREFIX + lockmcp.OWNER_ONLY[0]
        for env, sub, who in (({}, True, 'subagent'), (PM, False, 'pm'), (COORDINATOR, False, 'coordinator'),
                              ({context.ROLE_ENV: 'owner'}, False, 'marked-owner')):
            with self.subTest(who):
                d = decide(self.ev(name, sub), self.w.ctx(environ=env))
                self.assertEqual((d.kind, d.rule), ('deny', 'lock-mcp'))
                self.assertIn('This session is %s.' % who, d.reason)

    def test_full_names_from_the_machine_config(self):
        self.w.user_config({'guard_mcp_tools': ['mcp__plugin_x_lock__reserve_slots']})
        d = decide(self.ev('mcp__plugin_x_lock__reserve_slots', sub=True), self.w.ctx())
        self.assertEqual((d.kind, d.rule), ('deny', 'lock-mcp'))
        self.assertIsNone(decide(self.ev('mcp__plugin_x_lock__other', sub=True), self.w.ctx()))

    def test_an_unlisted_full_name_is_open(self):
        self.assertIsNone(decide(self.ev('mcp__plugin_x_lock__reserve_slots', sub=True), self.w.ctx()))


class AgentSessions(unittest.TestCase):
    """kill-by-name and lock-state hold subagents and the marks in guard_agent_marks (default:
    coordinator); the owner's session and other marks (a PM) are exempt."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def kill(self, env, sub=False):
        return decide(ps('Stop-Process -Name godot -Force', self.w.wt, sub), self.w.ctx(environ=env))

    def edit_state(self, env, sub=False):
        target = os.path.join(self.w.home, '.claude', 'machine-lock', 'state.json')
        return decide(event('Write', {'file_path': target, 'content': '{}'}, self.w.wt, sub), self.w.ctx(environ=env))

    def test_default_marks(self):
        for check in (self.kill, self.edit_state):
            with self.subTest(check.__name__):
                self.assertEqual(check({}, sub=True).kind, 'deny')
                d = check(COORDINATOR)
                self.assertEqual(d.kind, 'deny')
                self.assertTrue(d.reason.endswith(rules.tag(d.rule, 'main')), d.reason)
                self.assertIsNone(check(PM))
                self.assertIsNone(check({}))

    def test_old_conductor_mark_is_still_held(self):
        # the live launcher sets the old mark until go-live renames it
        self.assertEqual(self.kill({context.ROLE_ENV: 'conductor'}).kind, 'deny')

    def test_lock_state_names_the_rule_and_the_status_command(self):
        d = self.edit_state(COORDINATOR)
        self.assertEqual(d.rule, 'lock-state')
        self.assertIn('Instead: python %s/.claude/tools/machine_lock/machine_lock.py status ' % W.fwd(self.w.home), d.reason)

    def test_lock_state_by_shell_redirect(self):
        target = W.fwd(os.path.join(self.w.home, '.claude', 'machine-lock', 'state.json'))
        d = decide(bash('echo {} > %s' % target, self.w.wt, sub=True), self.w.ctx())
        self.assertEqual((d.kind, d.rule), ('deny', 'lock-state'))

    def test_beside_the_lock_state_is_not_lock_state(self):
        # the home folder is outside a subagent's write roots, so the write jail, not lock-state, denies it
        target = os.path.join(self.w.home, '.claude', 'machine-lock-notes.md')
        d = decide(event('Write', {'file_path': target, 'content': 'x'}, self.w.wt, True), self.w.ctx())
        self.assertEqual((d.kind, d.rule), ('deny', 'write-jail'))

    def test_marks_from_the_machine_config(self):
        self.w.user_config({'guard_agent_marks': ['coordinator', 'deep-worker']})
        deep = {context.ROLE_ENV: 'deep-worker'}
        self.assertEqual(self.kill(deep).rule, 'kill-by-name')
        self.assertEqual(self.edit_state(deep).rule, 'lock-state')
        self.assertIsNone(self.kill(PM))

    def test_marks_compare_case_ignored(self):
        self.assertEqual(self.kill({context.ROLE_ENV: 'Coordinator'}).rule, 'kill-by-name')
        self.w.user_config({'guard_agent_marks': ['Deep-Worker']})
        self.assertEqual(self.kill({context.ROLE_ENV: 'deep-worker'}).rule, 'kill-by-name')
        self.assertIsNone(self.kill({context.ROLE_ENV: 'PM'}))


class OwnerIdentity(unittest.TestCase):
    """A session other than the owner's own may not give a commit the owner's email."""

    DENIED = [
        ('bash', 'git commit --author="Owner <%s>" -m x' % OWNER_EMAIL),
        ('bash', 'git commit --author "Owner <%s>" -m x' % OWNER_EMAIL.upper()),
        ('bash', 'git -c user.email=%s commit -m x' % OWNER_EMAIL),
        ('bash', 'GIT_AUTHOR_EMAIL=%s git commit -m x' % OWNER_EMAIL),
        ('bash', 'export GIT_COMMITTER_EMAIL=%s' % OWNER_EMAIL),
        ('ps', "$env:GIT_AUTHOR_EMAIL = '%s'; git commit -m x" % OWNER_EMAIL),
        ('bash', 'git config user.email %s' % OWNER_EMAIL),
        ('ps', 'git config --global author.email %s' % OWNER_EMAIL),
        ('bash', 'env GIT_AUTHOR_EMAIL=%s git commit -m x' % OWNER_EMAIL),
        ('bash', 'export EMAIL=%s' % OWNER_EMAIL),
        ('ps', 'setx GIT_COMMITTER_EMAIL %s' % OWNER_EMAIL),
        ('ps', 'Set-Item -Path env:GIT_AUTHOR_EMAIL -Value %s' % OWNER_EMAIL),
        ('ps', "[Environment]::SetEnvironmentVariable('GIT_AUTHOR_EMAIL', '%s', 'User')" % OWNER_EMAIL),
        ('ps', "$env:git_author_email = '%s'" % OWNER_EMAIL),
        ('bash', 'git config -f .git/config user.email %s' % OWNER_EMAIL),
        ('ps', "$env:GIT_AUTHOR_EMAIL='%s'; git commit -m x" % OWNER_EMAIL),
        ('ps', "${env:GIT_COMMITTER_EMAIL} = '%s'" % OWNER_EMAIL),
        ('ps', "$null = [Environment]::SetEnvironmentVariable('GIT_AUTHOR_EMAIL','%s','User')" % OWNER_EMAIL),
        ('ps', 'Set-Item env:GIT_AUTHOR_EMAIL -Value:%s' % OWNER_EMAIL),
        ('bash', 'EMAIL+=%s git commit -m x' % OWNER_EMAIL),
        ('bash', 'git config -f "$(git rev-parse --git-common-dir)/config" user.email %s' % OWNER_EMAIL),
    ]
    ALLOWED = [
        ('bash', 'curl -d email=%s https://example.com/form' % OWNER_EMAIL),
        ('bash', 'some-tool --email=%s' % OWNER_EMAIL),
        ('bash', 'git commit -m "GIT_AUTHOR_EMAIL=%s"' % OWNER_EMAIL),
        ('bash', 'echo EMAIL=%s' % OWNER_EMAIL),
        ('ps', 'Write-Output "GIT_AUTHOR_EMAIL=%s"' % OWNER_EMAIL),
        ('bash', 'GIT_AUTHOR_EMAIL=me@example.com git commit -m x'),
        ('bash', 'export MY_EMAIL=%s' % OWNER_EMAIL),
        ('bash', 'git log --author=%s' % OWNER_EMAIL),
        ('bash', 'git commit --author="Me <me@example.com>" -m x'),
        ('bash', 'git config --get user.email'),
        ('bash', 'git commit -m "thanks %s"' % OWNER_EMAIL),
        ('bash', 'git shortlog -se --author=%s' % OWNER_EMAIL),
        ('bash', 'git config user.email other.owner@example.com'),
        ('ps', 'Write-Output $env:GIT_AUTHOR_EMAIL; git log --author=%s' % OWNER_EMAIL),
        ('ps', "$x = [Environment]::GetEnvironmentVariable('GIT_AUTHOR_EMAIL')"),
    ]

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo'}, 'owner': {'email': OWNER_EMAIL}})
        self.addCleanup(self.w.cleanup)

    def run_one(self, shell, text, env=None, sub=False):
        return decide((bash if shell == 'bash' else ps)(text, self.w.wt, sub), self.w.ctx(environ=env or {}))

    def test_denied(self):
        for shell, text in self.DENIED:
            for env, sub, role in (({}, True, 'subagent'), (PM, False, 'main'), (COORDINATOR, False, 'main')):
                with self.subTest(text, env=env, sub=sub):
                    d = self.run_one(shell, text, env, sub)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.kind, d.rule), ('deny', 'owner-identity'), d.reason)
                    self.assertTrue(d.reason.endswith(' Instead: git -C "%s" config --get user.email %s' % (
                        W.fwd(self.w.wt), rules.tag('owner-identity', role))), d.reason)

    def test_allowed(self):
        for shell, text in self.ALLOWED:
            with self.subTest(text):
                self.assertIsNone(self.run_one(shell, text, sub=True))

    def test_the_owner_session_may(self):
        for shell, text in self.DENIED:
            with self.subTest(text):
                self.assertIsNone(self.run_one(shell, text, {}))

    def test_no_owner_email_no_rule(self):
        w = W.World()
        self.addCleanup(w.cleanup)
        self.assertIsNone(decide(bash(self.DENIED[0][1], w.wt, True), w.ctx()))


if __name__ == '__main__':
    unittest.main()
