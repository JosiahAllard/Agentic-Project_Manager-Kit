"""Every section of a kit doc that content/ cites by title (`docs/<file>.md "<title>"`) is one the
section reader finds (kitlib/docs/index.py `print_section`, which `doc_index.py --section` runs).

A title written as a bold paragraph instead of a heading reads the same to a person but is a miss
to the reader: docs/gate.md "Waiting on a gate: who arms what" was one, cited by seven roles, and a
worker that looked it up got "no heading contains"."""
import os
import re
import unittest

from kitlib.docs import index

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CITATION = re.compile(r'(docs/[\w./-]+\.md)\s+"([^"\n]+)"')


def citations(root=KIT_ROOT):
    """{(doc, title): [content files citing it]} for every citation under content/."""
    found = {}
    for folder, _dirs, names in os.walk(os.path.join(root, 'content')):
        for name in names:
            if not name.endswith('.md'):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding='utf-8') as f:
                text = f.read()
            for doc, title in CITATION.findall(text):
                found.setdefault((doc, title), []).append(os.path.relpath(path, root))
    return found


class DocCitations(unittest.TestCase):
    def test_content_cites_doc_sections(self):
        self.assertIn(('docs/gate.md', 'Waiting on a gate: who arms what'), citations())

    def test_the_section_reader_finds_every_cited_section(self):
        misses = []
        for (doc, title), where in sorted(citations().items()):
            if not os.path.isfile(os.path.join(KIT_ROOT, doc)):
                misses.append('%s is not a file (cited by %s)' % (doc, ', '.join(where)))
                continue
            said = []
            if not index.print_section(KIT_ROOT, title, [doc], said.append, doc=doc):
                misses.append('%s "%s" (cited by %s): %s'
                              % (doc, title, ', '.join(sorted(where)), ' '.join(said)))
        self.assertEqual([], misses)


if __name__ == '__main__':
    unittest.main()
