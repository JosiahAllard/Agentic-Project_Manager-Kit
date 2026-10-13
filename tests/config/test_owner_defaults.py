"""The owner-only default paths are built from hosts.json (each host's profile owner_paths and
worktrees folder) plus the neutral ones, and come out exactly as the kit's list was spelled before
they were: the same entries in the same order, so docs/kit-json.md and every repo's default stay
as they were. Codex's folder stays locked until a hosts.json entry declares it."""
import unittest

from kitlib.config import schema
from kitlib.host import registry


class OwnerDefaults(unittest.TestCase):
    def test_the_default_is_the_list_it_was(self):
        self.assertEqual(schema.OWNER_PATHS, [
            'AGENTS.md', 'CLAUDE.md', '.claude/', '.codex/', 'docs/agent-profile.md',
            '.github/workflows/', '.kit.json', 'tools/gate.json', 'kitlib/gate/strict_defaults.py',
            'kitlib/backlog/roles.py'])
        self.assertEqual(schema.OWNER_PATHS_EXCEPT, ['.claude/worktrees/', '.codex/worktrees/'])

    def test_every_hosts_owner_paths_and_worktrees_folder_are_in_it(self):
        for profile in registry.profiles():
            for p in profile.owner_paths:
                self.assertIn(p, schema.OWNER_PATHS)
            self.assertIn(profile.worktrees_directory + '/', schema.OWNER_PATHS_EXCEPT)

    def test_the_section_default_is_that_list(self):
        owner = schema.SECTIONS['owner']
        self.assertEqual(owner['paths'].default, schema.OWNER_PATHS)
        self.assertEqual(owner['paths_except'].default, schema.OWNER_PATHS_EXCEPT)


if __name__ == '__main__':
    unittest.main()
