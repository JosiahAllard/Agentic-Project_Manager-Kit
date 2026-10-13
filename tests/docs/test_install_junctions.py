"""docs/install.md "After the plugin install: remove the per-item junctions" must exist and give
the skills removal step and the single agents sentence."""
import os
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(KIT, 'docs', 'install.md')
HEADING = '### After the plugin install: remove the per-item junctions'
AGENTS = ('Remove ~/.claude/agents only after the plugin providing pm-kit agents is installed '
          'and the launcher uses pm-kit:coordinator.')


class InstallJunctionsSection(unittest.TestCase):
    def setUp(self):
        with open(PATH, encoding='utf-8') as fh:
            self.text = fh.read()

    def test_heading_follows_the_plugin_install_section(self):
        self.assertIn(HEADING, self.text)
        self.assertLess(self.text.index('### Plugin install (local marketplace)'),
                        self.text.index(HEADING))

    def test_skills_junctions_are_removed_with_rmdir(self):
        section = self.text.split(HEADING, 1)[1]
        self.assertIn('cmd /c rmdir', section)

    def test_skills_removal_is_limited_to_the_kit_skill_names(self):
        section = self.text.split(HEADING, 1)[1]
        self.assertIn('$_.Name -in $kit', section)
        names = sorted(n for n in os.listdir(os.path.join(KIT, 'hosts', 'claude', 'shell', 'skills'))
                       if os.path.isdir(os.path.join(KIT, 'hosts', 'claude', 'shell', 'skills', n)))
        for name in names:
            self.assertIn("'%s'" % name, section)

    def test_agents_sentence_is_one_standalone_line(self):
        self.assertIn('\n' + AGENTS + '\n', self.text)
        self.assertIn('KIT-AGENTS-IN-PLUGIN', self.text)


if __name__ == '__main__':
    unittest.main()
