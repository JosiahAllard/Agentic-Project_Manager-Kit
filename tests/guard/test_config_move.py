"""The guard across the machine config folder's move (docs/kit-json.md "Moving the machine config
folder"; docs/guard.md "Known limits"): the ledger stays protected while only the old file
exists, a new file or folder the guard cannot read fails closed for subagents, and with both
present the new file's ledger_path is the one protected."""
import json
import os
import unittest

from kitlib.config import migration, user as kuser
from kitlib.guard import decide

from . import world as W
from .world import event


class ConfigMove(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        os.makedirs(os.path.dirname(self.w.ledger))
        self.new_dir = os.path.join(self.w.home, kuser.DIR_NAME)
        self.old_dir = os.path.join(self.w.home, migration.OLD_DIR_NAME)
        self.target = os.path.join(self.w.wt, 'a.py')

    def put(self, folder, body):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, kuser.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def edit_ledger(self, ledger, sub=False):
        return decide(event('Write', {'file_path': ledger, 'content': ''}, self.w.plain, sub=sub), self.w.ctx())

    def subagent_write(self):
        return decide(event('Write', {'file_path': self.target, 'content': ''}, self.w.wt, sub=True), self.w.ctx())

    def test_the_old_file_alone_still_protects_the_ledger(self):
        self.put(self.old_dir, {'ledger_path': self.w.ledger})
        d = self.edit_ledger(self.w.ledger)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)

    def test_an_empty_new_folder_does_not_drop_the_old_ledger(self):
        os.makedirs(self.new_dir)
        self.put(self.old_dir, {'ledger_path': self.w.ledger})
        d = self.edit_ledger(self.w.ledger, sub=True)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)

    def test_a_broken_new_file_denies_subagent_writes_though_the_old_one_reads(self):
        self.put(self.old_dir, {'ledger_path': self.w.ledger})
        self.put(self.new_dir, '{')
        d = self.subagent_write()
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn(kuser.DIR_NAME, d.reason)

    def test_a_new_folder_that_is_a_file_denies_subagent_writes(self):
        self.put(self.old_dir, {'ledger_path': self.w.ledger})
        with open(self.new_dir, 'w', encoding='utf-8') as f:
            f.write('not a folder')
        d = self.subagent_write()
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn('not a folder', d.reason)

    def test_with_both_present_the_new_ledger_is_the_one_protected(self):
        other = os.path.join(os.path.dirname(self.w.ledger), 'OLD-LEDGER.md')
        self.put(self.new_dir, {'ledger_path': self.w.ledger})
        self.put(self.old_dir, {'ledger_path': other})
        d = self.edit_ledger(self.w.ledger)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIsNone(self.edit_ledger(other))      # the known limit docs/guard.md states

    def test_one_hook_call_reads_one_located_file(self):
        self.put(self.old_dir, {'ledger_path': self.w.ledger, 'a_key_this_kit_does_not_know': 1})
        ctx = self.w.ctx()
        first = ctx.config_location()
        self.put(self.new_dir, {})                       # appears mid-call: not followed
        self.assertIs(ctx.config_location(), first)
        self.assertEqual(ctx.user()['ledger_path'], self.w.ledger)


if __name__ == '__main__':
    unittest.main()
