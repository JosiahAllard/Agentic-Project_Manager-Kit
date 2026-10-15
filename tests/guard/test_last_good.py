"""The machine config's last good copy (kitlib.guard.context: LAST_GOOD, keep_last_good,
changed_keys, Ctx.last_good): one save of the machine config that does not parse no longer
refuses every subagent write. The guard keeps a copy of the last file the strict reader took and,
while the current file does not parse, reads that copy for every key the bad save left alone and
says so when it refuses anything. A rule that depends on a key the bad save changed still fails
closed."""
import json
import os
import unittest

from kitlib.config import user as kuser
from kitlib.guard import context

from . import world as W
from .claudehost import decide


def lines(data):
    return json.dumps(data, indent=2) + '\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        os.makedirs(os.path.dirname(self.w.ledger))
        self.dir = kuser.config_dir(self.w.home)
        self.file = os.path.join(self.dir, 'config.json')
        self.copy = os.path.join(self.dir, context.LAST_GOOD)
        self.pending = os.path.join(self.w.base, 'pending.md')
        self.target = os.path.join(self.w.wt, 'a.py')

    def save(self, text):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.file, 'w', encoding='utf-8', newline='') as f:
            f.write(text)

    def good(self, data):
        """Save `data` pretty-printed and run one guard call, which keeps the copy."""
        text = lines(data)
        self.save(text)
        self.assertIsNone(self.write(self.target))
        return text

    def write(self, path, cwd=None, sub=True):
        return decide(W.event('Write', {'file_path': path, 'content': ''}, cwd or self.w.wt, sub=sub), self.w.ctx())

    def lose_a_quote(self, text):
        """`text` with the closing quote of pending_path's value lost: it no longer parses."""
        return text.replace(json.dumps(self.pending), json.dumps(self.pending)[:-1])

    def read_copy(self):
        with open(self.copy, encoding='utf-8') as f:
            return f.read()

    def config(self):
        return {'ledger_path': self.w.ledger, 'pending_path': self.pending, 'scrub_patterns': ['acme']}


class Keeping(Base):
    def test_a_good_read_keeps_a_copy(self):
        text = self.good(self.config())
        self.assertEqual(self.read_copy(), text)

    def test_a_later_good_save_replaces_it(self):
        self.good(self.config())
        data = dict(self.config(), scrub_patterns=['acme', 'other'])
        self.good(data)
        self.assertEqual(self.read_copy(), lines(data))

    def test_a_file_the_strict_reader_refuses_is_not_kept(self):
        text = self.good(self.config())
        self.save(lines(dict(self.config(), a_key_this_kit_does_not_know=1)))
        self.assertIsNone(self.write(self.target))
        self.assertEqual(self.read_copy(), text)

    def test_no_file_no_copy(self):
        self.assertIsNone(self.write(self.target))
        self.assertFalse(os.path.exists(self.copy))

    def test_a_subagent_cannot_write_the_copy(self):
        self.good(self.config())
        d = self.write(self.copy)
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.verdict, 'deny', d.reason)


class BadSave(Base):
    def test_a_bad_save_away_from_the_guard_keys_reads_the_copy(self):
        text = self.good(self.config())
        # a quote lost on the pending_path line: the file no longer parses
        self.save(self.lose_a_quote(text))
        self.assertIsNone(self.write(self.target))
        user = self.w.ctx().user()
        self.assertEqual(user['ledger_path'], self.w.ledger)
        self.assertIsNone(user['pending_path'])
        self.assertEqual(user['scrub_patterns'], ['acme'])
        self.assertFalse(user.get('broken'))
        # the ledger is still guarded, and the refusal says the copy stands in
        d = self.write(self.w.ledger, cwd=self.w.plain)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        self.assertIn('does not parse', d.reason)
        self.assertIn('config.last-good.json', d.reason)
        self.assertIn('changed: pending_path', d.reason)
        self.assertTrue(d.reason.endswith('[kit guard: rule=ledger role=subagent]'), d.reason)
        self.assertIn('Instead: python ', d.reason)

    def test_a_trailing_comma_or_a_lost_brace_changes_no_value(self):
        text = self.good(self.config())
        for bad in (text.rstrip().rstrip('}') + '\n', text.replace('"acme"\n  ]', '"acme"\n  ],')):
            with self.subTest(bad):
                self.save(bad)
                self.assertIsNone(self.write(self.target))
                self.assertEqual(self.w.ctx().user()['ledger_path'], self.w.ledger)

    def test_a_bad_save_of_ledger_path_fails_closed(self):
        text = self.good(self.config())
        self.save(text.replace('"ledger_path": "', '"ledger_path": '))
        d = self.write(self.target)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d and d.reason)
        self.assertIn('not valid JSON', d.reason)
        self.assertIn('does not stand in for ledger_path', d.reason)

    def test_a_key_the_bad_save_adds_counts_as_changed(self):
        text = self.good(self.config())
        head, rest = text.split('\n', 1)
        for added, key in (('  "guard_mcp_tools": ["x"\n', 'guard_mcp_tools'),
                           ('  ledger_path: "C:/elsewhere.md",\n', 'ledger_path'),
                           ('  "hub": {"store": \n', 'hub')):
            with self.subTest(key):
                self.save(head + '\n' + added + rest)
                d = self.write(self.target)
                self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d and d.reason)
                self.assertIn('does not stand in for %s' % key, d.reason)

    def test_a_changed_mark_root_table_drops_its_roots(self):
        data = dict(self.config(), guard_mark_roots={'coordinator': [{'path': '~/notes', 'mode': 'open'}]})
        text = self.good(data)
        self.assertTrue(self.w.ctx().user()['guard_mark_roots'])
        self.save(text.replace('"open"', '"open'))
        user = self.w.ctx().user()
        self.assertIsNone(user['guard_mark_roots'])
        self.assertFalse(user.get('broken'))

    def test_not_an_object(self):
        self.good(self.config())
        self.save('[]\n')
        d = self.write(self.target)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d and d.reason)
        self.assertIn('must be an object', d.reason)

    def test_no_copy_keeps_the_old_refusal(self):
        self.save('{\n  "pending_path": "x\n}\n')
        d = self.write(self.target)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d and d.reason)
        self.assertNotIn('last good', d.reason)

    def test_a_copy_the_strict_reader_refuses_is_not_used(self):
        text = self.good(self.config())
        with open(self.copy, 'w', encoding='utf-8') as f:
            f.write(lines(dict(self.config(), a_key_this_kit_does_not_know=1)))
        self.save(text.replace('"acme"', '"acme'))
        d = self.write(self.target)
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d and d.reason)

    def test_the_main_session_is_not_refused(self):
        text = self.good(self.config())
        self.save(text.replace('"ledger_path": "', '"ledger_path": '))
        self.assertIsNone(self.write(self.target, sub=False))


class PmData(Base):
    def config(self):
        return {'pm_data': {'path': self.w.data}, 'pending_path': self.pending}

    def test_unchanged_pm_data_is_read_from_the_copy(self):
        text = self.good(self.config())
        self.save(self.lose_a_quote(text))
        ctx = self.w.ctx()
        self.assertTrue(ctx.is_pm_data(self.w.data))
        self.assertIsNone(ctx.pm_data_error(self.target))
        d = self.write(os.path.join(self.w.data, 'x.md'), cwd=self.w.data)
        self.assertEqual((d.verdict, d.rule), ('deny', 'main-checkout'), d and d.reason)
        self.assertIn('last good copy', d.reason)
        self.assertIsNone(self.write(self.target))

    def test_a_changed_pm_data_fails_closed(self):
        text = self.good(self.config())
        self.save(text.replace('"path": "', '"path": '))
        d = self.write(self.target)
        self.assertEqual((d.verdict, d.rule), ('deny', 'pm-data'), d and d.reason)
        self.assertIn('does not stand in for pm_data', d.reason)


class Changed(unittest.TestCase):
    """changed_keys on texts alone."""

    GOOD = '{\n  "a": 1,\n  "b": {\n    "c": [1, 2]\n  },\n  "d": "x"\n}\n'

    def keys(self, bad, names=('a', 'b', 'd', 'e')):
        return context.changed_keys(self.GOOD, bad, names)

    def test_a_value_line(self):
        self.assertEqual(self.keys(self.GOOD.replace('"x"', '"x')), {'d'})

    def test_inside_a_nested_value(self):
        self.assertEqual(self.keys(self.GOOD.replace('[1, 2]', '[1, 2')), {'b'})
        self.assertEqual(self.keys(self.GOOD.replace('    "c"', '    "z": 0,\n    "c"')), {'b'})

    def test_a_line_between_members(self):
        self.assertEqual(self.keys(self.GOOD.replace('  "d"', '  junk\n  "d"')), set())
        self.assertEqual(self.keys(self.GOOD.replace('  "d"', '  "e": 1\n  "d"')), {'e'})
        self.assertEqual(self.keys(self.GOOD.replace('  "d"', '  e = 1\n  "d"')), {'e'})

    def test_a_whole_rewrite_on_one_line(self):
        self.assertEqual(self.keys('{"a": 1, "b": {"c": [1, 2]}, "d": "x"'), {'a', 'b', 'd'})

    def test_a_good_text_that_is_no_object(self):
        self.assertIsNone(context.changed_keys('[1]\n', '[1', ('a',)))


if __name__ == '__main__':
    unittest.main()
