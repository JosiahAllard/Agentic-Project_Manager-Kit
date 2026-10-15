"""kitlib.stubs.memory: the agent memory folder a repository's committed .claude/settings.json
names (autoMemoryDirectory = <memory root>/<repo>), and its writer (tools/kit_stubs.py memory).

The value is computed from fake paths only (a D:, E: or UNC checkout; no real drive is read). The
writes go to a throwaway folder; the main checkout is faked, so the value never names the temp
folder's own drive. No case creates a memory folder: the tool writes only the settings file.
"""
import json
import os
import re
import tempfile
import unittest

from kitlib.stubs import memory


def fake_top(top):
    return lambda path: top


class Value(unittest.TestCase):
    """memory_root and wanted: <the repo's drive>/Agent-memory/<the main checkout's folder name>."""

    def test_the_root_is_on_the_checkouts_own_drive(self):
        self.assertEqual(memory.memory_root('D:\\work\\proj'), 'D:/Agent-memory')
        self.assertEqual(memory.memory_root('E:\\x'), 'E:/Agent-memory')
        self.assertEqual(memory.memory_root('d:/src/proj'), 'd:/Agent-memory')

    def test_a_unc_checkout_uses_its_share(self):
        self.assertEqual(memory.memory_root('\\\\srv\\share\\code\\proj'), '//srv/share/Agent-memory')

    def test_an_override_wins_and_is_written_with_forward_slashes(self):
        self.assertEqual(memory.memory_root('D:\\work\\proj', override='F:\\mem\\'), 'F:/mem')

    def test_the_repo_is_the_main_checkouts_folder_name(self):
        wt = 'D:\\src\\Proj\\.claude\\worktrees\\x'
        self.assertEqual(memory.repo_name(wt, main_top=fake_top('D:\\src\\Proj')), 'Proj')
        self.assertEqual(memory.wanted(wt, main_top=fake_top('D:\\src\\Proj')),
                         'D:/Agent-memory/Proj')

    def test_no_main_checkout_gives_no_value(self):
        self.assertIsNone(memory.wanted('D:\\nowhere', main_top=fake_top(None)))

    def test_the_machine_config_has_no_override_yet(self):
        self.assertIsNone(memory.config_override())

    def test_importing_the_module_reads_no_hosts_json(self):
        import importlib
        from unittest import mock
        from kitlib.host import registry
        self.addCleanup(importlib.reload, memory)
        with mock.patch.object(registry, 'load', side_effect=registry.RegistryError('hosts.json broken')):
            importlib.reload(memory)            # a broken hosts.json fails the call that needs it, not the import
            with self.assertRaises(registry.RegistryError):
                memory.settings_rel()

    def test_the_module_names_no_drive_and_no_path(self):
        with open(memory.__file__, encoding='utf-8') as fh:
            src = fh.read()
        self.assertEqual(re.findall(r'[A-Za-z]:[\\/]', src), [])
        self.assertEqual(src.count("'Agent-memory'"), 1)


class Files(unittest.TestCase):
    """survey, apply and problem_line on a throwaway checkout whose main checkout is faked to D:."""

    TOP = 'D:\\src\\proj'
    WANT = 'D:/Agent-memory/proj'

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = os.path.realpath(d.name)
        self.path = os.path.join(self.root, '.claude', 'settings.json')

    def survey(self):
        return memory.survey(self.root, main_top=fake_top(self.TOP))

    def put(self, raw):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'wb') as fh:
            fh.write(raw)

    def raw(self):
        with open(self.path, 'rb') as fh:
            return fh.read()

    def test_no_file_is_missing_and_apply_creates_it_with_the_key_alone(self):
        m = self.survey()
        self.assertEqual((m.state, m.want), (memory.MISSING, self.WANT))
        lines = memory.apply(m)
        self.assertIn(self.WANT, '\n'.join(lines))
        self.assertEqual(json.loads(self.raw().decode('utf-8')), {'autoMemoryDirectory': self.WANT})
        self.assertEqual(self.survey().state, memory.OK)

    def test_a_file_without_the_key_keeps_its_keys_and_layout(self):
        before = '{\r\n    "permissions": {\r\n        "allow": [\r\n            "Bash"\r\n        ]\r\n    }\r\n}\r\n'
        self.put(b'\xef\xbb\xbf' + before.encode('utf-8'))
        m = self.survey()
        self.assertEqual(m.state, memory.MISSING)
        memory.apply(m)
        raw = self.raw()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        text = raw[3:].decode('utf-8')
        self.assertNotIn('\n', text.replace('\r\n', ''))
        self.assertEqual(json.loads(text), {'permissions': {'allow': ['Bash']}, 'autoMemoryDirectory': self.WANT})
        self.assertIn('    "autoMemoryDirectory": "%s"' % self.WANT, text)
        self.assertIn(before.rstrip('}\r\n').rstrip(), text)

    def test_a_hand_formatted_file_gets_one_line_and_nothing_else_changes(self):
        before = '{ "env": {"A": "1"},\n  "model":"x" }\n'
        self.put(before.encode('utf-8'))
        memory.apply(self.survey())
        after = self.raw().decode('utf-8')
        self.assertEqual(json.loads(after), {'env': {'A': '1'}, 'model': 'x', 'autoMemoryDirectory': self.WANT})
        self.assertEqual(after.replace('\n  "autoMemoryDirectory": "%s",' % self.WANT, ''), before)

    def test_an_empty_object_gets_the_key(self):
        self.put(b'{}')
        memory.apply(self.survey())
        self.assertEqual(json.loads(self.raw().decode('utf-8')), {'autoMemoryDirectory': self.WANT})

    def test_the_same_value_is_ok_and_nothing_is_written(self):
        self.put(json.dumps({'autoMemoryDirectory': self.WANT}).encode('utf-8'))
        m = self.survey()
        self.assertEqual(m.state, memory.OK)
        self.assertEqual(memory.apply(m), [])
        self.assertIsNone(memory.problem_line(self.root, main_top=fake_top(self.TOP)))

    def test_another_value_is_kept_never_replaced(self):
        raw = json.dumps({'autoMemoryDirectory': '~/mem/proj'}).encode('utf-8')
        self.put(raw)
        m = self.survey()
        self.assertEqual((m.state, m.value), (memory.OTHER, '~/mem/proj'))
        self.assertEqual(memory.apply(m), [])
        self.assertEqual(self.raw(), raw)
        self.assertIsNone(memory.problem_line(self.root, main_top=fake_top(self.TOP)))

    def test_what_cannot_be_edited_is_bad_and_untouched(self):
        for raw in (b'{"a": ', b'[1]', b'{"autoMemoryDirectory": 3}', b'{"autoMemoryDirectory": "mem/proj"}'):
            self.put(raw)
            m = self.survey()
            self.assertEqual(m.state, memory.BAD, raw)
            self.assertTrue(m.why, raw)
            with self.assertRaises(memory.Refused):
                memory.apply(m)
            self.assertEqual(self.raw(), raw)
            line = memory.problem_line(self.root, main_top=fake_top(self.TOP))
            self.assertTrue(line.startswith('could not check memory ('), line)

    def test_a_file_changed_since_the_survey_is_refused(self):
        self.put(b'{"a": 1}\n')
        m = self.survey()
        self.put(b'{"a": 2}\n')
        with self.assertRaises(memory.Refused):
            memory.apply(m)
        self.assertEqual(self.raw(), b'{"a": 2}\n')

    def test_a_file_made_since_the_survey_is_refused(self):
        m = self.survey()
        self.put(b'{"a": 2}\n')
        with self.assertRaises(memory.Refused):
            memory.apply(m)
        self.assertEqual(self.raw(), b'{"a": 2}\n')

    def test_the_missing_line_holds_the_owners_exact_command(self):
        line = memory.problem_line(self.root, label='proj', main_top=fake_top(self.TOP), kit='K')
        self.assertIn('memory (proj): ', line)
        self.assertIn(self.WANT, line)
        self.assertIn(memory.owner_command(self.root, kit='K'), line)
        self.assertEqual(memory.owner_command(self.root, kit='K'),
                         'python "%s" memory -C "%s" --apply' % (os.path.join('K', 'tools', 'kit_stubs.py'), self.root))
        # An owner step, worded as the checklist's other owner-only commands (kitlib.pmstart.fglock):
        # an agent that ran it would dirty another repository's main checkout.
        self.assertIn('Ask the owner (via the hub) to run, in their own PowerShell window: '
                      + memory.owner_command(self.root, kit='K'), line)

    def test_run_dry_run_writes_nothing_and_names_the_command(self):
        got = memory.run(self.root, main_top=fake_top(self.TOP), kit='K')
        self.assertEqual(got.code, 0, got.lines)
        out = '\n'.join(got.lines)
        self.assertIn('would add autoMemoryDirectory = %s' % self.WANT, out)
        self.assertIn(memory.owner_command(self.root, kit='K'), out)
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))

    def test_run_apply_writes_and_names_the_owners_commit(self):
        got = memory.run(self.root, apply=True, main_top=fake_top(self.TOP), kit='K')
        self.assertEqual(got.code, 0, got.lines)
        self.assertIn('the owner commits .claude/settings.json', '\n'.join(got.lines))
        self.assertEqual(self.survey().state, memory.OK)

    def test_run_on_a_bad_file_exits_1(self):
        self.put(b'[1]')
        got = memory.run(self.root, apply=True, main_top=fake_top(self.TOP), kit='K')
        self.assertEqual(got.code, 1, got.lines)
        self.assertEqual(self.raw(), b'[1]')


if __name__ == '__main__':
    unittest.main()
