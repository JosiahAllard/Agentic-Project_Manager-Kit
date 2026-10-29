"""kitlib.pmstart.down_entries: the sessions.json entries that should run and do not."""
import unittest

from kitlib import pmstart


class DownEntries(unittest.TestCase):
    def test_lists_named_entries_not_running_and_not_switched_off(self):
        sessions = {'_note': 'x',
                    'up': {'name': 'up-a1'},
                    'down': {'name': 'down-a1'},
                    'off': {'name': 'off-a1', 'autostart': False},
                    'nameless': {'stem': 'n'},
                    'bad': 'not an object'}
        self.assertEqual(pmstart.down_entries(sessions, {'up-a1'}), [('down', {'name': 'down-a1'})])

    def test_the_name_match_is_exact(self):
        self.assertEqual([k for k, _ in pmstart.down_entries({'k': {'name': 'k-a1'}}, {'k-a1-r2', 'K-A1'})],
                         ['k'])


if __name__ == '__main__':
    unittest.main()
