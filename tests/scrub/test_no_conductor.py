"""The coordinator was once called the conductor; no agent, profile or prose may say conductor again.

- No `claude/agents/conductor.md`, and `.claude-plugin/plugin.json` lists no conductor.
- No tracked file under claude/, kitlib/, tools/, templates/, optional/ or docs/, nor AGENTS.md,
  has a line matching `(?i)conductor`, unless ALLOWED pairs that file with a substring of the line.
- Each ALLOWED entry carries its reason: W2B or W1 (an old-name fallback or one-release guard, removed
  after the switch by that step) or HISTORY (the go-live migration record and the launcher's port map, which stay).
- An ALLOWED entry that matches no line fails too, so a removed fallback takes its entry with it.
"""
import json
import os
import re
import unittest

from tests.scrub.test_scrub import ROOT, read_text, tracked_files

WORD = re.compile(r'conductor', re.I)
SCOPE = ('claude/', 'kitlib/', 'tools/', 'templates/', 'optional/', 'docs/')
FILES = ('AGENTS.md',)

W2B = 'removed after the switch (W2b)'
W1 = 'removed after the switch (W1)'
HISTORY = 'history: stays'

ALLOWED = (
    # The coordinator start hook still fires for the old session mark (not this branch's file).
    ('claude/hooks/coordinator_session_start.py', '(or the old "conductor")', W2B),
    ('claude/hooks/coordinator_session_start.py', '("coordinator", "conductor")', W2B),
    # The old config key and the old role, read as fallbacks (kitlib/coordinatorstart is not this branch's).
    ('kitlib/coordinatorstart/__init__.py', 'LEGACY_KEY = "conductor_start"', W2B),
    ('kitlib/coordinatorstart/__init__.py', 'LEGACY_ROLE = "conductor"', W2B),
    ('kitlib/config/user.py', "'conductor_start': Field(", W2B),
    ('kitlib/config/user.py', 'unset: coordinator, conductor (removed after the switch)', W2B),
    ('docs/kit-json.md', '- `conductor_start` (object', W2B),
    ('docs/kit-json.md', 'unset: coordinator, conductor (removed after the switch)', W2B),
    # The guard holds the old session mark (kitlib/guard is not this branch's).
    ('kitlib/guard/rules.py', 'its old name conductor', W2B),
    ('kitlib/guard/rules.py', "'conductor' is the", W2B),
    ('kitlib/guard/rules.py', "AGENT_MARKS = ('coordinator', 'conductor'", W2B),
    ('docs/guard.md', 'its old name `conductor`', W2B),
    ('docs/guard.md', "`conductor` (the coordinator's old mark, removed after the switch)", W2B),
    # The launcher still runs the agent by its old name and marks it coordinator.
    ('kitlib/launcher/window.py', "'conductor': COORDINATOR", W2B),
    ('kitlib/launcher/window.py', '(and the old `conductor` name)', W2B),
    ('docs/launcher.md', '(or its old name `conductor`)', W2B),
    # coordinators.json's old name, refused and never read.
    ('kitlib/launcher/state.py', "OLD_COORDINATORS = 'conductors.json'", W2B),
    ('docs/launcher.md', 'Its old name was `conductors.json`', W2B),
    # The autostart task's old name, unregistered by install-autostart.
    ('kitlib/launcher/autostart.py', "OLD_TASK_NAME = 'Claude conductors after reboot'", W2B),
    ('docs/launcher.md', 'its old name, `Claude conductors after reboot`', W2B),
    # The one-release guard against a session still running under its old conductor stem.
    ('kitlib/launcher/lookup.py', 'with coordinator said as conductor', W2B),
    ('kitlib/launcher/lookup.py', "its repo) `<repo>-conductor`", W2B),
    ('kitlib/launcher/lookup.py', 'One-release guard for the conductor-to-coordinator rename', W2B),
    ('kitlib/launcher/lookup.py', "stem.replace('coordinator', 'conductor')", W2B),
    ('kitlib/launcher/lookup.py', "out.append('%s-conductor'", W2B),
    ('kitlib/launcher/reboot.py', 'or whose old `conductor` stem is', W2B),
    ('kitlib/launcher/start.py', '(one-release guard for the conductor-to-coordinator rename)', W2B),
    ('docs/launcher.md', "a session under the entry's old conductor stem", W2B),
    ('docs/launcher.md', 'any entry a session under its old conductor stem runs for', W2B),
    # procwatch matching the old agent name.
    ('optional/procwatch/procwatch.py', '(?:coordinator|conductor)")', W2B),
    # go-live's launcher switch line: the old task name and the marks until the rename.
    ('docs/go-live.md', 'unregisters the old `Claude conductors after reboot`', W2B),
    # The go-live migration record of the rename.
    ('docs/go-live.md', 'These live names still say conductor', HISTORY),
    ('docs/go-live.md', '- Launcher: the old `start-conductor.ps1`', W2B),
    ('docs/go-live.md', r'(W2a, done before the switch): rename `<state_dir>\conductors.json`', HISTORY),
    ('docs/go-live.md', 'the old-path stub `hooks/conductor_session_start.py`', HISTORY),
    ('docs/go-live.md', '- The machine config: key `conductor_start`', W2B),
    ('docs/go-live.md', '- Each repo: `.claude/conductor-profile.md`', HISTORY),
    ('docs/go-live.md', '- Installed copies: `~/.claude/agents/conductor.md`', HISTORY),
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
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'claude', 'agents', 'conductor.md')))

    def test_the_plugin_lists_no_conductor(self):
        with open(os.path.join(ROOT, '.claude-plugin', 'plugin.json'), encoding='utf-8') as f:
            text = f.read()
        agents = json.loads(text).get('agents') or []           # no `agents` key passes
        self.assertEqual([a for a in agents if WORD.search(a)], [], 'plugin.json lists a conductor agent')

    def test_every_entry_has_a_reason(self):
        for f, sub, why in ALLOWED:
            self.assertIn(why, (W2B, W1, HISTORY), (f, sub))
            self.assertTrue(WORD.search(sub), (f, sub))

    def test_no_tracked_file_says_conductor_outside_the_allowlist(self):
        bad, stale = findings(tracked_files(), read_text)
        self.assertEqual(bad, [], 'conductor is back: say coordinator, or (an old-name fallback only) add '
                                  'a (file, line substring, W2B) entry to ALLOWED')
        self.assertEqual(stale, [], 'ALLOWED entries that match no line: remove them')

    def test_the_check_catches_a_new_line(self):
        files = {'claude/agents/x.md': 'Run the Conductor.\n', 'tests/x.py': 'conductor\n',
                 'docs/launcher.md': '| `start-conductor.ps1` | x\n'}
        bad, stale = findings(sorted(files), files.get,
                              (('docs/launcher.md', '| `start-conductor.ps1` |', HISTORY),
                               ('docs/gone.md', 'conductor', W2B)))
        self.assertEqual(bad, ['claude/agents/x.md:1: Run the Conductor.'])
        self.assertEqual(stale, [('docs/gone.md', 'conductor')])


if __name__ == '__main__':
    unittest.main()
