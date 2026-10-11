"""A repo's own `.claude/agents/<name>.md` beside the kit's plugin agents (docs/ROSTER.md).

The plugin registers every kit agent under its prefix (kitlib.agentname.PREFIX + name); a repo's
project agent keeps its bare frontmatter name. So a same-name repo file is a second agent, `<name>`,
reached by dispatching the bare name, and `PREFIX + <name>` stays the kit's. A repo that wants its
own version lists the bare name in its coordinator profile; every other dispatch uses the prefixed
name. This resolves a scratch repo that way and asserts it."""
import tempfile
import unittest
from pathlib import Path

from kitlib import agentname
from tests.agents.test_agents import AGENTS, frontmatter


def resolve(repo_agents, kit_agents):
    """{dispatch name: path}: the kit's plugin agents under PREFIX + name, the repo's project agents
    under their bare name."""
    out = {}
    for p in sorted(kit_agents.glob('*.md')):
        out[agentname.qualified(frontmatter(p.read_text(encoding='utf-8'))['name'])] = p
    for p in sorted(repo_agents.glob('*.md')):
        out[frontmatter(p.read_text(encoding='utf-8'))['name']] = p
    return out


class RepoOverrideTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo_agents = Path(tmp.name) / '.claude' / 'agents'
        self.repo_agents.mkdir(parents=True)

    def write(self, stem, name, body='repo version'):
        (self.repo_agents / (stem + '.md')).write_text(
            '---\nname: %s\ndescription: d\nmodel: sonnet\n---\n%s\n' % (name, body), encoding='utf-8')

    def test_a_same_name_repo_file_replaces_the_kit_agent(self):
        """...only under the bare name: the repo's `worker` is the repo file, the prefixed name the kit's."""
        self.write('worker', 'worker')
        got = resolve(self.repo_agents, AGENTS)
        self.assertEqual(got['worker'], self.repo_agents / 'worker.md')
        self.assertIn('repo version', got['worker'].read_text(encoding='utf-8'))
        self.assertEqual(got[agentname.PREFIX + 'worker'], AGENTS / 'worker.md')

    def test_the_other_kit_agents_stay_the_kits(self):
        self.write('worker', 'worker')
        got = resolve(self.repo_agents, AGENTS)
        for p in AGENTS.glob('*.md'):
            self.assertEqual(got[agentname.PREFIX + p.stem], p)
            if p.stem != 'worker':
                self.assertNotIn(p.stem, got)

    def test_a_differently_named_repo_agent_is_an_extra_not_an_override(self):
        self.write('worker-2', 'worker-2')
        got = resolve(self.repo_agents, AGENTS)
        self.assertEqual(got[agentname.PREFIX + 'worker'], AGENTS / 'worker.md')
        self.assertNotIn('worker', got)
        self.assertIn('worker-2', got)

    def test_every_kit_agent_can_be_overridden_by_its_file_name(self):
        """A repo file named after a kit agent is reachable by that bare name: file stem == name."""
        for p in AGENTS.glob('*.md'):
            self.assertEqual(frontmatter(p.read_text(encoding='utf-8'))['name'], p.stem)


if __name__ == '__main__':
    unittest.main()
