"""No ruling id in any tracked file: the kit is public and cites no one's rulings (reasons are
written out in words instead).

Every file `git ls-files` lists is read. A ruling-id-shaped token (`R-` or `r-` and three or more digits,
whatever follows them) fails the test, with file:line, unless the file is under one of the ALLOWED fixture folders, and
even there only a made-up `R-9nnn` id may appear. Placeholders such as `R-nnnn` hold no digits and
pass everywhere.
"""
import os
import re
import unittest
from kitlib.git import cmd as gitcmd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Not a machine-lock run id (r-20261008T175145Z-f9cf22): eight digits, then T and a digit.
RULING = re.compile(r'\bR-(?![0-9]{8}T[0-9])[0-9]{3,}', re.I)
MADE_UP = re.compile('R' + r'-9[0-9]{3}')    # built, so this file holds no id itself

# The one list of folders whose tests need id-shaped strings in their fixtures: the tools that
# parse ruling ids. test_no_cites and test_builtin read it too.
FIXTURE_FOLDERS = (
    ('tests/backlog/', 'backlog pricing checks a ruling against a fixture ledger'),
    ('tests/backloglint/fixtures/', "a record's priority_ruling field"),
    ('tests/ledger/', 'kitlib/ledger parses ledger headers'),
)
ALLOWED = tuple(p for p, _ in FIXTURE_FOLDERS)


def tracked_files():
    out = gitcmd.subprocess_run(['git', '-C', ROOT, 'ls-files', '-z'], capture_output=True,
                                check=True)
    return [p for p in out.stdout.decode('utf-8').split('\0') if p]


def findings(files, read):
    """['path:line: id'] for every ruling id the rules above refuse."""
    found = []
    for rel in files:
        text = read(rel)
        if text is None:
            continue
        fixture = rel.startswith(ALLOWED)
        for n, line in enumerate(text.splitlines(), 1):
            for m in RULING.finditer(line):
                if not (fixture and MADE_UP.fullmatch(m.group(0))):
                    found.append('%s:%d: %s' % (rel, n, m.group(0)))
    return found


def read_text(rel):
    try:
        with open(os.path.join(ROOT, rel), 'rb') as f:
            data = f.read()
    except OSError:            # listed but absent from this checkout (a deleted, unstaged file)
        return None
    if b'\0' in data:
        return None            # binary
    return data.decode('utf-8', errors='replace')


class Scrub(unittest.TestCase):
    def test_no_ruling_id_in_a_tracked_file(self):
        found = findings(tracked_files(), read_text)
        self.assertEqual(found, [], 'ruling ids in tracked files (write the reason instead):\n'
                         + '\n'.join(found))

    def test_the_check_reports_file_and_line(self):
        def rid(digits):       # built, so this file holds no id itself
            return 'R-' + digits
        files = {'docs/a.md': 'fine\nsee %s for why\n' % rid('0123'),
                 'tests/ledger/x.py': '%s %s\n' % (rid('9001'), rid('0001')),
                 'kitlib/b.py': 'R-nnnn and %s pass\n' % rid('12'),
                 'tests/other/y.py': '%s\n' % rid('9001'),
                 'tests/z.py': '%sa %s_x %s\n' % (rid('1234'), rid('1234'), rid('1234').lower()),
                 'tests/run.py': 'r-20261008T175145Z-f9cf22 is a lock run id\n'}
        self.assertEqual(findings(sorted(files), files.get),
                         ['docs/a.md:2: ' + rid('0123'), 'tests/ledger/x.py:1: ' + rid('0001'),
                          'tests/other/y.py:1: ' + rid('9001'), 'tests/z.py:1: ' + rid('1234'),
                          'tests/z.py:1: ' + rid('1234'), 'tests/z.py:1: ' + rid('1234').lower()])


if __name__ == '__main__':
    unittest.main()
