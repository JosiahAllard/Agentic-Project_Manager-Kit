"""The coordinator-start review-session check and its machine config key were removed on the
owner's word (KIT-REVIEW-SESSION-OPTIONAL): neither name may come back in a tracked file.

The two names are built from parts, so this file holds neither itself.
"""
import re
import unittest

from tests.scrub.test_scrub import read_text, tracked_files

RELIC = re.compile('review' + '[_ ]' + 'session', re.I)


def findings(files, read):
    """['path:line'] for every line of every file that names the relic."""
    found = []
    for rel in files:
        text = read(rel)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if RELIC.search(line):
                found.append('%s:%d' % (rel, n))
    return found


class ReviewSessionRelicTest(unittest.TestCase):

    def test_no_tracked_file_names_the_relic(self):
        found = findings(tracked_files(), read_text)
        self.assertEqual(found, [], 'the removed review-session check is back (owner: remove it entirely):\n'
                         + '\n'.join(found))

    def test_the_check_finds_both_spellings_and_reports_the_line(self):
        under, space = 'review' + '_session', 'Review' + ' Session'
        files = {'a.py': 'fine\nx = cfg["%s"]\n' % under, 'b.md': 'the %s check\n' % space,
                 'c.md': 'review-session-get_usage and a review of the session\n'}
        self.assertEqual(findings(sorted(files), files.get), ['a.py:2', 'b.md:1'])


if __name__ == '__main__':
    unittest.main()
