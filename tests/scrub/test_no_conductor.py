"""The coordinator was once called the conductor; no agent, profile or prose may say conductor again.

- No `hosts/claude/shell/agents/conductor.md`, and `.claude-plugin/plugin.json` lists no conductor.
- No tracked file under hosts/, claude/, boot/, kitlib/, tools/, templates/, optional/ or docs/, nor AGENTS.md,
  has a line matching `(?i)conductor`, unless ALLOWED pairs that file with a substring of the line.
- Each ALLOWED entry carries its reason, HISTORY (the launcher's port map, which stays): the old-name fallbacks are removed, so no other reason is left.
- An ALLOWED entry that matches no line fails too, so a removed fallback takes its entry with it.
"""
import json
import os
import re
import unittest

from tests.scrub.test_scrub import ROOT, read_text, tracked_files

WORD = re.compile(r'conductor', re.I)
SCOPE = ('hosts/', 'claude/', 'boot/', 'kitlib/', 'tools/', 'templates/', 'optional/', 'docs/')
FILES = ('AGENTS.md',)

HISTORY = 'history: stays'

ALLOWED = (
    # The launcher's port map names the PM repo's old scripts and example file.
    ('docs/launcher.md', '| `start-conductor.ps1` |', HISTORY),
    ('docs/launcher.md', '| `sessions.example.json`, `conductors.example.json` |', HISTORY),
)


def in_scope(rel):
    return rel.startswith(SCOPE) or rel in FILES


def findings(files, read, allowed=ALLOWED):
    """(['path:line: text'] not allowed, [allowed entries that matched no line])."""
    bad, used = [], set()
    for rel in files:
        if not in_scope(rel):
            continue
        text = read(rel)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if not WORD.search(line):
                continue
            hits = [i for i, (f, sub, _) in enumerate(allowed) if f == rel and sub in line]
            if hits:
                used.update(hits)
            else:
                bad.append('%s:%d: %s' % (rel, n, line.strip()[:160]))
    return bad, [allowed[i][:2] for i in range(len(allowed)) if i not in used]


class NoConductorTest(unittest.TestCase):

    def test_no_conductor_agent_file(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'hosts', 'claude', 'shell', 'agents', 'conductor.md')))

    def test_the_plugin_lists_no_conductor(self):
        with open(os.path.join(ROOT, '.claude-plugin', 'plugin.json'), encoding='utf-8') as f:
            text = f.read()
        agents = json.loads(text).get('agents') or []           # no `agents` key passes
        self.assertEqual([a for a in agents if WORD.search(a)], [], 'plugin.json lists a conductor agent')

    def test_every_entry_has_a_reason(self):
        for f, sub, why in ALLOWED:
            self.assertEqual(why, HISTORY, (f, sub))
            self.assertTrue(WORD.search(sub), (f, sub))

    def test_no_tracked_file_says_conductor_outside_the_allowlist(self):
        bad, stale = findings(tracked_files(), read_text)
        self.assertEqual(bad, [], 'conductor is back: say coordinator, or (a history line only) add '
                                  'a (file, line substring, HISTORY) entry to ALLOWED')
        self.assertEqual(stale, [], 'ALLOWED entries that match no line: remove them')

    def test_the_check_catches_a_new_line(self):
        files = {'hosts/claude/shell/agents/x.md': 'Run the Conductor.\n', 'tests/x.py': 'conductor\n',
                 'docs/launcher.md': '| `start-conductor.ps1` | x\n'}
        bad, stale = findings(sorted(files), files.get,
                              (('docs/launcher.md', '| `start-conductor.ps1` |', HISTORY),
                               ('docs/gone.md', 'conductor', HISTORY)))
        self.assertEqual(bad, ['hosts/claude/shell/agents/x.md:1: Run the Conductor.'])
        self.assertEqual(stale, [('docs/gone.md', 'conductor')])


if __name__ == '__main__':
    unittest.main()
