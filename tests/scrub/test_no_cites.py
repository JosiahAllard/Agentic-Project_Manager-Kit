"""Nothing cites a ruling id any more; the ledger is the PM's private record.

No tracked file has a line matching PATTERN (an `R-` id of 3 or more digits, the `R-nnnn` and `R-<id>`
placeholders, the `<PM_LEDGER_PATH>` placeholder, `PM R-`, or the `--ruling` flag), unless the file is on
ALLOWED, by path or by folder prefix (ending in `/`).
- PRIVATE: the ledger itself, which is the PM's record and may name its own ids.
- SELF: tests that must spell a pattern to test it.
- GOING: code and docs that the ruling-cite removal clears on other branches; each entry goes with its branch.
- An ALLOWED entry that no longer matches a hit fails too, so a cleaned file takes its entry with it.
"""
import re
import unittest

from tests.scrub.test_scrub import read_text, tracked_files

PATTERN = re.compile(r'R-[0-9]{3,}|R-nnn|R-<id>|<PM_LEDGER_PATH>|PM R-|--ruling\b')

PRIVATE = 'the ledger, the PM\'s private record'
SELF = 'spells the pattern to test it'
GOING = 'removed by another branch of the ruling-cite removal'

ALLOWED = (
    ('docs/ledger.md', PRIVATE),
    ('kitlib/ledger/', PRIVATE),
    ('tests/ledger/', PRIVATE),
    ('tools/ledger.py', PRIVATE),
    ('tests/scrub/test_scrub.py', SELF),
    ('tests/scrub/test_no_cites.py', SELF),
    # Going with the backlog and ledger branches.
    ('kitlib/backlog/', GOING),
    ('tests/backlog/', GOING),
    ('tests/backloglint/fixtures/', GOING),
    ('tools/backlog.py', GOING),
    ('claude/skills/backlog-ops/SKILL.md', GOING),
    ('docs/backlog.md', GOING),
    ('docs/backlog-store.md', GOING),
    ('docs/kit-json.md', GOING),
    ('docs/repo-checks.md', GOING),
    ('kitlib/config/user.py', GOING),
    ('kitlib/ledgerrefs/', GOING),
    ('tools/ledger_refs.py', GOING),
    ('kitlib/rulingcites/', GOING),
    ('tools/ruling_cites.py', GOING),
    ('tests/rulingcites/', GOING),
    ('tests/coordinatorstart/', GOING),
    ('tests/gate/test_land_store.py', GOING),
)

RID = 'R-' + '1234'     # built, so this file holds no id for test_scrub to flag


def allowed_by(rel, allowed):
    return [i for i, (p, _) in enumerate(allowed) if rel == p or (p.endswith('/') and rel.startswith(p))]


def findings(files, read, allowed=ALLOWED):
    """(['path:line: text'] not allowed, [allowed entries that matched no hit])."""
    bad, used = [], set()
    for rel in files:
        text = read(rel)
        if text is None:
            continue
        hits = [n for n, line in enumerate(text.splitlines(), 1) if PATTERN.search(line)]
        if not hits:
            continue
        ok = allowed_by(rel, allowed)
        if ok:
            used.update(ok)
            continue
        lines = text.splitlines()
        bad.extend('%s:%d: %s' % (rel, n, lines[n - 1].strip()[:160]) for n in hits)
    return bad, [allowed[i][0] for i in range(len(allowed)) if i not in used]


class NoCitesTest(unittest.TestCase):

    def test_every_entry_has_a_reason(self):
        for path, why in ALLOWED:
            self.assertIn(why, (PRIVATE, SELF, GOING), path)

    def test_no_tracked_file_cites_a_ruling_outside_the_allowlist(self):
        bad, stale = findings(tracked_files(), read_text)
        self.assertEqual(bad, [], 'a ruling id, placeholder or flag is back: the ledger is private, so '
                                  'cite nobody (see the module docstring)')
        self.assertEqual(stale, [], 'ALLOWED entries that match no hit: remove them')

    def test_the_check_catches_a_new_line_and_a_stale_entry(self):
        files = {'AGENTS.md': 'Rulings are cited as PM ' + RID + '.\n', 'a/b.md': 'fine\n',
                 'templates/x.md': 'R-<id> | text\n', 'docs/ledger.md': RID + ' private\n',
                 'kitlib/ledger/x.py': 'R-nnnn\n'}
        bad, stale = findings(sorted(files), files.get,
                              (('docs/ledger.md', PRIVATE), ('kitlib/ledger/', PRIVATE),
                               ('docs/gone.md', GOING), ('kitlib/gone/', GOING)))
        self.assertEqual(bad, ['AGENTS.md:1: Rulings are cited as PM ' + RID + '.',
                               'templates/x.md:1: R-<id> | text'])
        self.assertEqual(stale, ['docs/gone.md', 'kitlib/gone/'])


if __name__ == '__main__':
    unittest.main()
