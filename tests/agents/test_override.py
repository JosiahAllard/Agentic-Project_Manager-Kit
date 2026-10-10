"""A repo's own `.claude/agents/<name>.md` overrides the kit agent of that name (docs/ROSTER.md).

The host resolves agents by name, project folder first; the kit's part is that every kit agent is
addressed by one name (file stem == frontmatter name), so a same-name repo file is the override
and a differently named file is an extra agent, not an override. This resolves a scratch repo
that way and asserts both."""
import tempfile
import unittest
from pathlib import Path

from tests.agents.test_agents import AGENTS, frontmatter


def resolve(repo_agents, kit_agents):
    """{name: path}: the host's rule, a repo definition wins over the kit's of the same name."""
    out = {}
    for folder in (kit_agents, repo_agents):          # later wins
        for p in sorted(folder.glob('*.md')):
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
        self.write('worker', 'worker')
        got = resolve(self.repo_agents, AGENTS)
        self.assertEqual(got['worker'], self.repo_agents / 'worker.md')
        self.assertIn('repo version', got['worker'].read_text(encoding='utf-8'))

    def test_the_other_kit_agents_stay_the_kits(self):
        self.write('worker', 'worker')
        got = resolve(self.repo_agents, AGENTS)
        for p in AGENTS.glob('*.md'):
            if p.stem != 'worker':
                self.assertEqual(got[p.stem], p)

    def test_a_differently_named_repo_agent_is_an_extra_not_an_override(self):
        self.write('worker-2', 'worker-2')
        got = resolve(self.repo_agents, AGENTS)
        self.assertEqual(got['worker'], AGENTS / 'worker.md')
        self.assertIn('worker-2', got)

    def test_every_kit_agent_can_be_overridden_by_its_file_name(self):
        for p in AGENTS.glob('*.md'):
            self.assertEqual(frontmatter(p.read_text(encoding='utf-8'))['name'], p.stem)


if __name__ == '__main__':
    unittest.main()
