"""tools/redfirst.py --help lists -C: the content tests check every command shown in a skill or the
profile against that help, and split_root (not argparse) is what acts on -C."""
import importlib.util
import os
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_redfirst_entry_for_help_test',
                                               os.path.join(KIT, 'tools', 'redfirst.py'))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class HelpListsRoot(unittest.TestCase):
    def test_help_names_dash_c(self):
        import argparse
        flags = []
        orig = argparse.ArgumentParser.print_help
        try:
            argparse.ArgumentParser.print_help = lambda self, *a, **k: flags.extend(
                o for act in self._actions for o in act.option_strings)
            with self.assertRaises(SystemExit):
                tool.parse_args(['--help'])
        finally:
            argparse.ArgumentParser.print_help = orig
        self.assertIn('-C', flags)

    def test_split_root_still_takes_it_out(self):
        self.assertEqual(tool.split_root(['-C', 'w', '--tests', 'a']), ('w', ['--tests', 'a'], None))


if __name__ == '__main__':
    unittest.main()
