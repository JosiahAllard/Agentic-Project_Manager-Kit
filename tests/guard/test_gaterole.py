"""Who may lift a branch's gate-run budget (docs/guard.md "Gate allow"): `gate.py allow` is the
owner's and the coordinator's (or the PM's), so every subagent and every other marked main session
is refused it (gate-allow), in every spelling the guard reads a kit tool run in."""
import os
import unittest

from hosts.codex.adapter.host import ADAPTER as CODEX
from kitlib import guard
from kitlib.guard import context
from kitlib.host import hookmain

from . import claudehost

from . import world as W
from .claudehost import decide
from .world import event

ARGS = 'kit/x --runs 8 --why "two flaky runs"'
# named here, not imported, so the file runs (and goes red) against a kit without the rule
RULE = 'gate-allow'                             # kitlib.guard.RULE
INSTEAD = ('stop and send your coordinator what each gate run found and why another is needed; '
           'it runs gate.py allow')             # kitlib.guard.INSTEAD


def mark(m):
    return {context.ROLE_ENV: m}


class GateAllow(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.wt = W.fwd(self.w.wt)
        self.tool = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'gate.py'))
        self.back = self.tool.replace('/', '\\')

    def cmd(self, text, agent_type='worker', tool='Bash', sub=True, env=None):
        ev = event(tool, {'command': text, 'description': 'test'}, self.w.wt, sub, agent_type)
        return decide(ev, self.w.ctx(environ=env if env is not None else {}))

    def refused(self, d, text=''):
        self.assertIsNotNone(d, text)
        self.assertEqual((d.verdict, d.rule), ('deny', RULE), '%s: %s' % (text, d.reason))
        self.assertIn('Instead: %s [kit guard: rule=gate-allow' % INSTEAD, d.reason)
        self.assertEqual(d.instead, INSTEAD)

    def passed(self, d, text=''):
        self.assertIsNone(d, '%s: %s' % (text, d and d.reason))

    def bash_spellings(self):
        return ['python "%s" allow %s' % (self.tool, ARGS),
                'python tools/gate.py allow %s' % ARGS,
                'python ./tools/gate.py allow %s -C "%s"' % (ARGS, self.wt),
                'python -u tools/gate.py allow %s' % ARGS,
                'python3.12 tools/gate.py allow %s' % ARGS,
                'py -3 tools/gate.py allow %s' % ARGS,
                'python -m tools.gate allow %s' % ARGS,
                'python -m runpy tools.gate allow %s' % ARGS,
                'cmd /c "python tools\\gate.py allow kit/x --runs 8 --why w"',
                'bash -c "python tools/gate.py allow kit/x --runs 8 --why w"',
                'git status; python tools/gate.py allow %s' % ARGS,
                'cd "%s" && python tools/gate.py allow %s' % (self.wt, ARGS)]

    def ps_spellings(self):
        return ['python "%s" allow %s' % (self.back, ARGS),
                '& python "%s" allow %s' % (self.tool, ARGS),
                '& "python" tools\\gate.py allow %s' % ARGS,
                "Start-Process python -ArgumentList 'tools/gate.py allow kit/x --runs 8 --why w' -Wait",
                "Start-Process python -ArgumentList 'tools/gate.py','allow','kit/x' -Wait",
                'Get-Date; python -m tools.gate allow %s' % ARGS]

    def each(self, check, **kw):
        for text in self.bash_spellings():
            with self.subTest(shell='bash', text=text, **kw):
                check(self.cmd(text, tool='Bash', **kw), text)
        for text in self.ps_spellings():
            with self.subTest(shell='ps', text=text, **kw):
                check(self.cmd(text, tool='PowerShell', **kw), text)

    # ------------------------------------------------------------ refused

    def test_every_builder_subagent_is_refused(self):
        for role in ('worker', 'weak-worker', 'strong-worker', 'senior-worker', 'pm-kit:senior-worker',
                     'docs-writer', 'helper'):
            self.each(self.refused, agent_type=role)

    def test_a_reading_testing_or_landing_subagent_is_refused(self):
        for role in ('analyst', 'test-runner', 'lander', 'pm-kit:lander', 'general-purpose', 'Explore'):
            self.each(self.refused, agent_type=role)

    def test_a_subagent_named_for_a_controller_role_is_refused(self):
        for role in ('coordinator', 'pm-kit:coordinator', 'pm', 'conductor'):
            self.each(self.refused, agent_type=role)

    def test_a_subagent_of_a_coordinator_session_is_refused(self):
        # a subagent inherits its main session's environment, mark included
        for m in ('coordinator', 'pm'):
            self.each(self.refused, agent_type='worker', env=mark(m))

    def test_another_marked_main_session_is_refused(self):
        for m in ('deep-worker', 'consultant', 'plain', 'pm-kit:deep-worker', 'worker'):
            self.each(self.refused, sub=False, env=mark(m))

    def test_the_refusal_names_the_session(self):
        d = self.cmd('python tools/gate.py allow %s' % ARGS, agent_type='pm-kit:analyst')
        self.refused(d)
        self.assertIn('a subagent (analyst)', d.reason)
        self.assertIn('[kit guard: rule=gate-allow role=subagent]', d.reason)
        d = self.cmd('python tools/gate.py allow %s' % ARGS, sub=False, env=mark('deep-worker'))
        self.refused(d)
        self.assertIn('a session marked deep-worker', d.reason)
        self.assertIn('role=main]', d.reason)

    # ------------------------------------------------------------ allowed

    def test_the_owner_may_allow(self):
        self.each(self.passed, sub=False, env={})

    def test_a_coordinator_or_pm_main_session_may_allow(self):
        for m in ('coordinator', 'conductor', 'pm', 'pm-kit:coordinator', 'PM', 'Pm-Kit:pm'):
            self.each(self.passed, sub=False, env=mark(m))

    # ------------------------------------------------------------ not a gate.py allow run

    def test_other_gate_runs_pass(self):
        for text in ('python tools/gate.py --full',
                     'python "%s" -C "%s" --full' % (self.tool, self.wt),
                     'python "%s" wait-result kit/x --tip %s' % (self.tool, 'a' * 40),
                     'python tools/gate.py --only allow',
                     'python tools/gate.py -C "%s" allow %s' % (self.wt, ARGS),
                     'python tools/gate.py land kit/x',
                     'python tools/allow.py allow kit/x'):
            with self.subTest(text=text):
                self.passed(self.cmd(text), text)

    def test_text_that_names_the_command_passes(self):
        for tool, text in (('Bash', 'git log --oneline --grep "gate.py allow"'),
                           ('Bash', 'echo python tools/gate.py allow kit/x'),
                           ('Bash', 'grep -n "gate.py allow" docs/gate.md'),
                           ('PowerShell', 'Select-String -Path docs/gate.md -Pattern "gate.py allow"'),
                           ('Bash', 'git -C "%s" commit -m "gate.py allow is the coordinator\'s" -- x.py'
                            % self.wt)):
            with self.subTest(text=text):
                self.passed(self.cmd(text, tool=tool), text)

    # ------------------------------------------------------------ a host with no subagent identity

    def codex(self, text, m):
        """The call as hookmain hands it to the guard on Codex, whose hooks name no subagent: a
        marked session's call (its children's too) comes as agent UNIDENTIFIED, agent_role the mark."""
        env = mark(m) if m else {}
        ev = event('Bash', {'command': text, 'description': 'test'}, self.w.wt, sub=False)
        act = hookmain.unidentified(CODEX, claudehost.action(ev), env)
        d = guard.decide(act, self.w.ctx(environ=env))
        return None if d.verdict == 'allow' else d

    def test_an_unidentified_call_is_judged_by_its_mark(self):
        text = 'python tools/gate.py allow %s' % ARGS
        for m in ('coordinator', 'pm', 'pm-kit:coordinator', None):
            with self.subTest(mark=m):
                self.passed(self.codex(text, m), text)
        for m in ('deep-worker', 'consultant', 'plain'):
            with self.subTest(mark=m):
                d = self.codex(text, m)
                self.refused(d, text)
                self.assertIn('a session marked %s' % m, d.reason)

    def test_a_computed_subcommand_is_not_read(self):
        self.passed(self.cmd('python tools/gate.py $V %s' % ARGS))
        self.passed(self.cmd('python tools/gate.py $env:V %s' % ARGS, tool='PowerShell'))


if __name__ == '__main__':
    unittest.main()
