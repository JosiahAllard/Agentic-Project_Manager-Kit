"""The backlog's audit log in the guard (docs/guard.md "Backlog roles"; docs/backlog.md "Audit
log"): `<pm_data>/audit/` holds the lines tools/backlog.py appends after its writes, and only that
tool writes there, so every session but the owner's is refused a file tool edit or a shell write
in it (pm-data-audit), a kept deny."""
import os
import unittest

from kitlib.guard import context, modes

from . import world as W
from .claudehost import decide
from .world import event

COORDINATOR = {context.ROLE_ENV: 'coordinator'}
PM = {context.ROLE_ENV: 'pm'}
PLAIN = {context.ROLE_ENV: 'plain'}
RULE = 'pm-data-audit'


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.w.user_config({'pm_data': {'path': self.w.data}})
        self.audit = os.path.join(self.w.data, 'audit')
        self.log = os.path.join(self.audit, 'backlog.jsonl')

    def edit(self, target, tool='Write', sub=True, env=None):
        tool_input = {'file_path': target, 'content': 'x'} if tool == 'Write' else \
            {'file_path': target, 'old_string': 'a', 'new_string': 'b'}
        ev = event(tool, tool_input, self.w.wt, sub, 'worker')
        return decide(ev, self.w.ctx(environ=env if env is not None else {}))

    def shell(self, text, tool='Bash', sub=True, env=None):
        ev = event(tool, {'command': text, 'description': 'test'}, self.w.wt, sub, 'worker')
        return decide(ev, self.w.ctx(environ=env if env is not None else {}))

    def refused(self, d):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', RULE), d.reason)
        return d

    def not_this_rule(self, d):
        self.assertTrue(d is None or d.rule != RULE, d and d.reason)


class Writes(Base):
    def test_a_subagents_file_tool_edit_is_refused(self):
        for tool in ('Write', 'Edit'):
            with self.subTest(tool):
                self.refused(self.edit(self.log, tool))
        self.refused(self.edit(os.path.join(self.audit, 'other.txt')))

    def test_every_marked_session_is_refused(self):
        for env in (COORDINATOR, PM, PLAIN):
            with self.subTest(env):
                self.refused(self.edit(self.log, sub=False, env=env))

    def test_a_shell_write_is_refused(self):
        log, audit = W.fwd(self.log), W.fwd(self.audit)
        for tool, text in (('Bash', 'echo x >> "%s"' % log), ('Bash', 'rm "%s"' % log),
                           ('Bash', 'rm -rf "%s"' % audit), ('Bash', 'cp a.jsonl "%s"' % log),
                           ('PowerShell', "Add-Content -Path '%s' -Value x" % log),
                           ('PowerShell', "Remove-Item -Recurse '%s'" % audit)):
            with self.subTest(text):
                self.refused(self.shell(text, tool))

    def test_the_refusal_names_the_backlog_tool(self):
        d = self.refused(self.edit(self.log))
        self.assertIn('Instead: python "%s/tools/backlog.py" ' % W.fwd(self.w.kit_dir), d.reason)
        self.assertIn('backlog.py', d.reason.split('Instead:')[0])

    def test_the_owner_writes_it(self):
        self.not_this_rule(self.edit(self.log, sub=False, env={}))
        self.assertIsNone(self.edit(self.log, sub=False, env={}))

    def test_beside_the_folder_is_not_the_audit_log(self):
        self.not_this_rule(self.edit(os.path.join(self.w.data, 'audit-notes', 'backlog.jsonl')))
        self.not_this_rule(self.edit(os.path.join(self.w.data, 'backlog.jsonl')))

    def test_with_no_pm_data_named_it_is_no_audit_log(self):
        self.w.user_config({})
        self.not_this_rule(self.edit(self.log))

    def test_a_read_is_not_refused(self):
        self.not_this_rule(self.shell('cat "%s"' % W.fwd(self.log)))


class Mode(unittest.TestCase):
    def test_it_is_a_kept_deny(self):
        self.assertNotIn(RULE, modes.DEFAULTS)
        self.assertEqual(modes.mode(RULE), modes.DENY)


if __name__ == '__main__':
    unittest.main()
