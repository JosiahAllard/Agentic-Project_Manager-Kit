"""The kit's own docs/INDEX.md (docs/doc-tools.md "doc_index"): the file `--find` and `--tools`
read must exist in the kit, and its entries must be searchable. Read from the working tree."""
import io
import os
import unittest

from kitlib.docs import index

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(KIT, 'docs', 'INDEX.md')


class TheKitsOwnIndex(unittest.TestCase):
    def test_the_index_file_exists(self):
        self.assertTrue(os.path.isfile(PATH), 'the kit has no docs/INDEX.md')

    def test_every_entry_has_a_kw_field(self):
        with open(PATH, encoding='utf-8') as fh:
            entries = index.index_entries(fh.read())
        self.assertTrue(entries, 'docs/INDEX.md has no entry line')
        for n, _section, line, keys in entries:
            self.assertTrue(keys, 'docs/INDEX.md:%d has no kw: field: %s' % (n, line))

    def test_find_index_hits_and_tools_prints_a_section(self):
        out = io.StringIO()
        self.assertEqual(index.run(KIT, lambda s: out.write(s + '\n'), find='index'), 0, out.getvalue())
        out = io.StringIO()
        self.assertEqual(index.run(KIT, lambda s: out.write(s + '\n'), tools=True), 0, out.getvalue())


if __name__ == '__main__':
    unittest.main()
