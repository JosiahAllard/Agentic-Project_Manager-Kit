"""Text that dispatches a kit agent names it with the kit plugin's prefix (docs/ROSTER.md).

The plugin registers every kit agent as `<plugin>:<name>` (kitlib.agentname.PREFIX); once the old
~/.claude/agents link is gone, a bare kit name no longer resolves. So in the agent files, the skills
and docs/ROSTER.md, every `subagent_type` value, every `--agent` argument and every backticked kit
agent type carries the prefix. Bare names stay where they are names, not dispatches: frontmatter
`name:` lines and ROSTER.md's table keys (neither is backticked), and the ALLOWED pairs below."""
import re
import unittest
from pathlib import Path

from kitlib import agentname
from tests.agents.test_agents import AGENTS

ROOT = AGENTS.parents[1]
KIT = sorted(p.stem for p in AGENTS.glob('*.md'))
NAME = r'(?:%s)?(%s)' % (re.escape(agentname.PREFIX), '|'.join(re.escape(n) for n in sorted(KIT, key=len, reverse=True)))
# a whole backticked span that is a kit agent type, bare or qualified
TICKED = re.compile(r'`%s`' % NAME)
# an --agent argument, a subagent_type value
ARG = re.compile(r'--agent[ =]+[\'"]?%s(?![\w-])' % NAME)
SUBAGENT = re.compile(r'subagent_type\W{0,4}%s(?![\w-])' % NAME)
# (file relative to the kit root, the exact bare text) that is allowed to stay bare; none today
ALLOWED = set()


def scanned_files():
    out = sorted(AGENTS.glob('*.md')) + sorted((ROOT / 'claude' / 'skills').rglob('*.md'))
    return out + [ROOT / 'docs' / 'ROSTER.md']


def bare_uses(text):
    """[(line number, matched text)] of every dispatch-shaped kit agent name without the prefix,
    and the count of qualified ones seen."""
    bad, good = [], 0
    for n, line in enumerate(text.splitlines(), 1):
        for rx in (TICKED, ARG, SUBAGENT):
            for m in rx.finditer(line):
                if agentname.PREFIX + m.group(1) in m.group(0):
                    good += 1
                else:
                    bad.append((n, m.group(0)))
    return bad, good


class DispatchNamesTest(unittest.TestCase):
    def test_every_dispatch_name_is_qualified(self):
        seen = 0
        for p in scanned_files():
            rel = p.relative_to(ROOT).as_posix()
            bad, good = bare_uses(p.read_text(encoding='utf-8'))
            seen += good
            with self.subTest(file=rel):
                self.assertEqual([b for b in bad if (rel, b[1]) not in ALLOWED], [],
                                 '%s names a kit agent without %r' % (rel, agentname.PREFIX))
        self.assertGreater(seen, 20, 'the scan found almost no qualified names: is it reading the files?')

    def test_the_scan_catches_each_form_and_passes_the_qualified_one(self):
        p = agentname.PREFIX
        for text in ('dispatch `worker`', 'claude --agent coordinator -n x', 'subagent_type: "lander"',
                     "subagent_type='test-runner'", 'run `deep-worker`.'):
            with self.subTest(text=text):
                self.assertEqual(len(bare_uses(text)[0]), 1)
        for text in ('dispatch `%sworker`' % p, 'claude --agent %scoordinator -n x' % p,
                     'subagent_type: "%slander"' % p):
            with self.subTest(text=text):
                self.assertEqual(bare_uses(text), ([], 1))

    def test_names_that_are_not_dispatches_are_left_alone(self):
        for text in ('name: worker', '| worker | sonnet | medium | routine |', '`worker-2`', '`coordinator-dispatch`',
                     '`consultant-<repo>`', 'the worker reports', '`general-purpose`'):
            with self.subTest(text=text):
                self.assertEqual(bare_uses(text), ([], 0))

    def test_every_allowed_pair_is_still_there(self):
        for rel, bare in ALLOWED:
            self.assertIn(bare, (ROOT / rel).read_text(encoding='utf-8'), rel)


if __name__ == '__main__':
    unittest.main()
