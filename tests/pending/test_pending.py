"""kitlib.pending: the owner's pending-steps file edited over text, every untouched byte kept
(docs/pending.md)."""
import os
import tempfile
import unittest

from kitlib import pending
from kitlib.ledger.errors import Refused, Usage

TEXT = ('# Pending\n\nIntro line.\n\n1. First step\n    with a second line\n'
        '2. DONE 10-01. Was: Second step\n3. Third step\n\nTrailer.\n')


class ReadTest(unittest.TestCase):
    def test_open_items_skip_done_and_closed(self):
        text = TEXT + '4. CLOSED: not needed\n5. Donee is a word\n'
        self.assertEqual(pending.open_items(text), ['1. First step', '3. Third step', '5. Donee is a word'])

    def test_read_and_write_keep_line_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'PENDING.md')
            raw = TEXT.replace('\n', '\r\n')
            pending.write_atomic(path, raw)
            self.assertEqual(pending.read_raw(path), raw)
            with open(path, 'rb') as f:
                self.assertIn(b'\r\n', f.read())
            self.assertEqual(os.listdir(tmp), ['PENDING.md'])
            with self.assertRaises(Refused):
                pending.read_raw(os.path.join(tmp, 'missing.md'))


class AddTest(unittest.TestCase):
    def test_add_numbers_past_the_highest_and_goes_after_the_last_item(self):
        new, n = pending.add(TEXT, 'Fourth\nmore detail')
        self.assertEqual(n, 4)
        self.assertEqual(new, TEXT.replace('3. Third step\n', '3. Third step\n4. Fourth\n    more detail\n'))

    def test_add_keeps_crlf_and_bom(self):
        raw = pending.BOM + TEXT.replace('\n', '\r\n')
        new, n = pending.add(raw, 'Fourth')
        self.assertEqual(new, raw.replace('3. Third step\r\n', '3. Third step\r\n4. Fourth\r\n'))

    def test_add_to_a_file_with_no_items_is_number_one_before_the_final_newline(self):
        self.assertEqual(pending.add('# Pending\n', 'Step'), ('# Pending\n1. Step\n', 1))
        self.assertEqual(pending.add('# Pending', 'Step'), ('# Pending\n1. Step', 1))

    def test_empty_text_is_usage(self):
        with self.assertRaises(Usage):
            pending.add(TEXT, '  \n ')


class DoneEditTest(unittest.TestCase):
    def test_done_marks_the_first_line_only(self):
        new = pending.done(TEXT, 1, '10-10', 'ran it')
        self.assertEqual(new, TEXT.replace('1. First step\n', '1. DONE 10-10 (ran it). Was: First step\n'))
        self.assertEqual(pending.done(TEXT, 3, '10-10'), TEXT.replace('3. Third', '3. DONE 10-10. Was: Third'))

    def test_done_refuses_a_bad_date_a_missing_item_and_a_done_one(self):
        with self.assertRaises(Usage):
            pending.done(TEXT, 1, '2026-10-10')
        with self.assertRaises(Refused):
            pending.done(TEXT, 9, '10-10')
        with self.assertRaises(Refused):
            pending.done(TEXT, 2, '10-10')

    def test_done_keeps_a_bom_on_the_first_line(self):
        raw = pending.BOM + '1. Only\n'
        self.assertEqual(pending.done(raw, 1, '10-10'), pending.BOM + '1. DONE 10-10. Was: Only\n')

    def test_edit_replaces_the_item_and_its_continuation_lines(self):
        new = pending.edit(TEXT, 1, 'New first\n  indented again')
        self.assertEqual(new, TEXT.replace('1. First step\n    with a second line\n',
                                           '1. New first\n    indented again\n'))
        with self.assertRaises(Refused):
            pending.edit(TEXT, 9, 'x')
        with self.assertRaises(Usage):
            pending.edit(TEXT, 1, '')


if __name__ == '__main__':
    unittest.main()
