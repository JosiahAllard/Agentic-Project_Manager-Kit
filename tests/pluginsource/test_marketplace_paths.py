"""Where the pm-kit marketplace points, against the export root: kitlib.pluginsource.claude
marketplace_mismatches (each account folder's plugins/known_marketplaces.json installLocation and
the managed settings file's extraKnownMarketplaces path) and tools/plugin_refresh.py
marketplace_warnings. Every file is in a temp tree; the real home and Program Files are never read."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib.pluginsource import claude


def _bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


class MarketplaceCase(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='kit-mkt-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.export = os.path.join(self.top, 'export')
        self.elsewhere = os.path.join(self.top, 'elsewhere')

    def account(self, name, location=None, text=None):
        """An account folder whose known_marketplaces.json names `location` for pm-kit (None and
        no `text`: no file; `text`: the file's raw text instead)."""
        folder = os.path.join(self.top, name)
        os.makedirs(os.path.join(folder, 'plugins'))
        if text is None and location is not None:
            text = json.dumps({'other-market': {'installLocation': self.elsewhere},
                               'pm-kit': {'source': {'source': 'directory', 'path': location},
                                          'installLocation': location}})
        if text is not None:
            with open(os.path.join(folder, *claude.KNOWN_MARKETPLACES), 'w', encoding='utf-8') as f:
                f.write(text)
        return folder

    def managed(self, path=None, text=None):
        p = os.path.join(self.top, 'pf', 'ClaudeCode', 'managed-settings.json')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if text is None:
            text = json.dumps({'extraKnownMarketplaces': {
                'pm-kit': {'source': {'source': 'directory', 'path': path}}}})
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p


class MismatchTest(MarketplaceCase):
    def test_an_install_location_elsewhere_is_reported(self):
        # red first: no line was printed for this before marketplace_mismatches existed
        good, bad = self.account('a', self.export), self.account('b', self.elsewhere)
        lines = claude.marketplace_mismatches(self.export, [good, bad])
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('in %s is %s, not the export %s' % (bad, self.elsewhere, self.export), lines[0])
        self.assertIn('installLocation', lines[0])

    def test_a_managed_settings_path_elsewhere_is_reported(self):
        m = self.managed(self.elsewhere)
        lines = claude.marketplace_mismatches(self.export, [self.account('a', self.export)], managed=m)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('in the managed settings is %s, not the export' % self.elsewhere, lines[0])
        self.assertIn('extraKnownMarketplaces', lines[0])

    def test_every_path_at_the_export_gives_no_line(self):
        m = self.managed(self.export)
        self.assertEqual(claude.marketplace_mismatches(self.export, [self.account('a', self.export)],
                                                       managed=m), [])

    def test_slashes_case_and_a_trailing_separator_do_not_count(self):
        same = self.export.replace('\\', '/').upper() + '/'
        m = self.managed(same)
        self.assertEqual(claude.marketplace_mismatches(self.export, [self.account('a', same)],
                                                       managed=m), [])

    def test_a_missing_file_or_entry_gives_no_line(self):
        none = self.account('none')
        other = self.account('other', text=json.dumps({'other-market': {'installLocation': 'X:\\y'}}))
        m = self.managed(text=json.dumps({'env': {}}))
        self.assertEqual(claude.marketplace_mismatches(self.export, [none, other], managed=m), [])
        gone = os.path.join(self.top, 'no-such', 'managed-settings.json')
        self.assertEqual(claude.marketplace_mismatches(self.export, [none], managed=gone), [])
        self.assertEqual(claude.marketplace_mismatches(self.export, [none], managed=None), [])

    def test_a_corrupt_or_odd_file_is_one_could_not_check_line(self):
        cases = ['{not json', '[1, 2]', json.dumps({'pm-kit': 'Z:\\x'}),
                 json.dumps({'pm-kit': {'installLocation': 7}}),
                 json.dumps({'pm-kit': {'source': {}}})]
        for i, text in enumerate(cases):
            acct = self.account('c%d' % i, text=text)
            lines = claude.marketplace_mismatches(self.export, [acct])
            self.assertEqual(len(lines), 1, (text, lines))
            self.assertTrue(lines[0].startswith('could not check the pm-kit marketplace path in '),
                            lines)
        m = self.managed(text=json.dumps({'extraKnownMarketplaces': {'pm-kit': {'source': 'dir'}}}))
        lines = claude.marketplace_mismatches(self.export, [], managed=m)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('could not check', lines[0])

    def test_nothing_is_written(self):
        acct, m = self.account('a', self.elsewhere), self.managed(self.elsewhere)
        files = [os.path.join(acct, *claude.KNOWN_MARKETPLACES), m]
        before = [(os.path.getmtime(f), _bytes(f)) for f in files]
        claude.marketplace_mismatches(self.export, [acct], managed=m)
        self.assertEqual([(os.path.getmtime(f), _bytes(f)) for f in files], before)


if __name__ == '__main__':
    unittest.main()
