"""Where the pm-kit marketplace points, against the export root: kitlib.pluginsource.claude
marketplace_mismatches (each account folder's plugins/known_marketplaces.json installLocation and
the managed settings file's extraKnownMarketplaces path), tools/plugin_refresh.py
marketplace_warnings and the session start's pluginsource.current_lines. Every file is in a temp tree; the real home and Program Files are never read."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import pluginsource
from kitlib.pluginsource import claude
from tests.pluginsource import Kit


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
        cases = ['{not json', '[1, 2]', json.dumps({'pm-kit': 'Q:\\x'}),
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

    def test_pluginsource_re_exports_the_check(self):
        acct, m = self.account('a', self.elsewhere), self.managed(self.elsewhere)
        lines = pluginsource.marketplace_mismatches(self.export, [acct], managed=m)
        self.assertEqual(lines, claude.marketplace_mismatches(self.export, [acct], managed=m))
        self.assertEqual(len(lines), 2, lines)

    def test_nothing_is_written(self):
        acct, m = self.account('a', self.elsewhere), self.managed(self.elsewhere)
        files = [os.path.join(acct, *claude.KNOWN_MARKETPLACES), m]
        before = [(os.path.getmtime(f), _bytes(f)) for f in files]
        claude.marketplace_mismatches(self.export, [acct], managed=m)
        self.assertEqual([(os.path.getmtime(f), _bytes(f)) for f in files], before)


class StartCheckTest(MarketplaceCase):
    """pluginsource.current_lines (the session start's plugin check) carries the mismatch lines, and
    nothing in the check can raise out of it."""

    def setUp(self):
        super().setUp()
        self.k = Kit(self)
        self.export = self.k.root
        self.s = pluginsource.Settings(self.k.root, False, 'origin')

    def current(self, folders, migrated=None):
        """A current export state: every folder of `migrated` (default `folders`) migrated and
        refreshed at the kit's tracking main."""
        sha = self.k.repo.git('rev-parse', 'refs/remotes/origin/main')
        os.makedirs(self.k.root, exist_ok=True)
        pluginsource.write_state(self.k.root, {'sha': sha, 'folders': {
            f: {'migrated': True, 'rc': 0} for f in (folders if migrated is None else migrated)}})

    def test_a_migrated_folder_pointing_elsewhere_is_a_line(self):
        # red first: current_lines gave [] here before it called marketplace_mismatches
        good, bad = self.account('a', self.export), self.account('b', self.elsewhere)
        self.current([good, bad])
        lines = pluginsource.current_lines(self.k.kit, self.s, folders=[good, bad])
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('in %s is %s, not the export %s' % (bad, self.elsewhere, self.export), lines[0])

    def test_the_managed_settings_file_is_checked(self):
        acct, m = self.account('a', self.export), self.managed(self.elsewhere)
        self.current([acct])
        lines = pluginsource.current_lines(self.k.kit, self.s, folders=[acct], managed=m)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('extraKnownMarketplaces', lines[0])

    def test_all_at_the_export_gives_no_line(self):
        acct, m = self.account('a', self.export), self.managed(self.export)
        self.current([acct])
        self.assertEqual(pluginsource.current_lines(self.k.kit, self.s, folders=[acct], managed=m), [])

    def test_given_folders_do_not_read_the_machine_managed_file(self):
        acct = self.account('a', self.export)
        self.current([acct])
        with mock.patch('kitlib.machinesetup.extras.Host.managed_path',
                        side_effect=AssertionError('read the machine file')):
            self.assertEqual(pluginsource.current_lines(self.k.kit, self.s, folders=[acct]), [])

    def test_no_folders_given_reads_the_machine_managed_file(self):
        acct, m = self.account('a', self.export), self.managed(self.elsewhere)
        self.current([acct])
        with mock.patch.object(claude, 'account_folders', return_value=[acct]), \
                mock.patch('kitlib.machinesetup.extras.Host.managed_path', return_value=m):
            lines = pluginsource.current_lines(self.k.kit, self.s)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('in the managed settings is %s' % self.elsewhere, lines[0])

    def test_a_folder_not_migrated_is_not_checked(self):
        done, todo = self.account('a', self.export), self.account('b', self.elsewhere)
        self.current([done, todo], migrated=[done])
        lines = pluginsource.current_lines(self.k.kit, self.s, folders=[done, todo])
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('not migrated', lines[0])

    def test_a_corrupt_or_missing_file_never_raises(self):
        corrupt, none = self.account('a', text='{not json'), self.account('b')
        m = self.managed(text='\x00garbage')
        self.current([corrupt, none])
        lines = pluginsource.current_lines(self.k.kit, self.s, folders=[corrupt, none], managed=m)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(l.startswith('could not check the pm-kit marketplace path in ')
                            for l in lines), lines)
        gone = os.path.join(self.top, 'no-such', 'managed-settings.json')
        self.assertEqual(pluginsource.current_lines(self.k.kit, self.s, folders=[none], managed=gone), [])

    def test_a_failing_check_is_one_line_never_a_raise(self):
        acct = self.account('a', self.export)
        self.current([acct])
        with mock.patch.object(claude, 'marketplace_mismatches', side_effect=RuntimeError('boom')):
            lines = pluginsource.current_lines(self.k.kit, self.s, folders=[acct])
        self.assertEqual(lines, ['could not check the pm-kit marketplace paths: RuntimeError: boom'])
        with mock.patch.object(claude, 'account_folders', return_value=[acct]), \
                mock.patch('kitlib.machinesetup.extras.Host.managed_path',
                           side_effect=ValueError('no registry')):
            lines = pluginsource.current_lines(self.k.kit, self.s)
        self.assertEqual(lines, ['could not check the pm-kit marketplace paths: ValueError: no registry'])


if __name__ == '__main__':
    unittest.main()
