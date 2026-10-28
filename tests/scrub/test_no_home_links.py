"""No tracked file reaches the kit's tools or knowledge through a link in a home config folder
(`~/.claude/tools/...`, `%USERPROFILE%\\.claude-N\\knowledge\\...`): machine setup makes no such
link any more (docs/machine-setup.md "Links"), so a path through one breaks once the owner
removes the leftovers. Code names the kit folder instead, and docs say `<kit>/tools/...`.

An allowed hit is matched by its file and the markdown heading it stands under, and an
entry that no longer matches anything fails too, so the list cannot go stale. None is allowed now."""
import re
import unittest

from tests.scrub.test_scrub import read_text, tracked_files

HOME_LINK = re.compile(
    r'(?:~|%USERPROFILE%|\$env:USERPROFILE|\$HOME|\$\{HOME\})[/\\]+'
    r'\.claude(?:-[\w.-]+)?[/\\]+(?:tools|knowledge)\b', re.I)
HEADING = re.compile(r'^#{1,6}\s+(.*?)\s*$')

ALLOWED = ()


def hits(rel, text):
    """[(heading, line number, matched text)] for each home-link path in one file."""
    found, heading, fence = [], '', False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('```'):
            fence = not fence
        elif not fence and rel.endswith('.md'):
            m = HEADING.match(line)
            if m:
                heading = m.group(1)
        found.extend((heading, n, m.group()) for m in HOME_LINK.finditer(line))
    return found


def scan():
    """{rel: hits} over every tracked file outside tests/."""
    out = {}
    for rel in tracked_files():
        if rel.startswith('tests/'):
            continue
        text = read_text(rel)
        if text:
            found = hits(rel, text)
            if found:
                out[rel] = found
    return out


class NoHomeLinks(unittest.TestCase):

    maxDiff = None

    def test_no_tracked_file_reaches_the_kit_through_a_home_link(self):
        allowed = {(rel, heading) for rel, heading, _ in ALLOWED}
        bad = ['%s:%d: %s' % (rel, n, path) for rel, found in sorted(scan().items())
               for heading, n, path in found if (rel, heading) not in allowed]
        self.assertEqual(bad, [], 'a path through a home tools or knowledge link: name the kit '
                                  'folder instead (code) or `<kit>/...` (docs)')

    def test_every_allowed_entry_still_matches(self):
        seen = {(rel, heading) for rel, found in scan().items() for heading, _, _ in found}
        stale = ['%s "%s"' % (rel, heading) for rel, heading, _ in ALLOWED
                 if (rel, heading) not in seen]
        self.assertEqual(stale, [], 'an ALLOWED entry no file matches any more: remove it')


if __name__ == '__main__':
    unittest.main()
