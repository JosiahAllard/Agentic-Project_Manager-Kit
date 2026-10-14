"""tests/shipped.py's install_patterns, forbidden and violations read the machine config's
scrub_patterns and machine_path_folders at call time, from an injected home (a temp folder here, never the real one)."""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kitlib.config import user as kuser
from kitlib.config.types import ConfigError
import tests
from tests import shipped


class InstallPatternsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name

    def write_config(self, text):
        d = os.path.join(self.home, kuser.DIR_NAME)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, kuser.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(text)

    def test_scrub_patterns_become_case_insensitive_install_names(self):
        self.write_config(json.dumps({'scrub_patterns': [r'\bacme\b', 'widgetco']}))
        pats = shipped.install_patterns(self.home)
        self.assertEqual([what for _rx, what in pats], ['install name', 'install name'])
        self.assertTrue(pats[0][0].search('the ACME repo'))
        self.assertTrue(pats[1][0].search('WidgetCo owner'))
        self.assertIsNone(pats[0][0].search('acmeish'))

    def test_machine_path_folders_become_machine_path_patterns(self):
        bs = chr(92)
        self.write_config(json.dumps({'machine_path_folders': ['Src', 'C#']}))
        pats = shipped.install_patterns(self.home)
        self.assertEqual([what for _rx, what in pats], ['machine path'])
        rx = pats[0][0]
        for hit in ('D:' + bs + 'Src' + bs + 'x', 'd:/src/y', 'E:' + bs * 2 + 'C#' + bs * 2 + 'z'):
            self.assertTrue(rx.search('see %s here' % hit), hit)
        for miss in ('D:' + bs + 'Srcs' + bs + 'x', 'D:/other/Src/x', '%SRC%' + bs + 'x'):
            self.assertIsNone(rx.search('see %s here' % miss), miss)

    def test_violations_names_a_file_holding_a_configured_machine_folder(self):
        self.write_config(json.dumps({'machine_path_folders': ['Src']}))
        folder = Path(self.home) / 'shipped'
        folder.mkdir()
        (folder / 'a.md').write_text('Kept at %s:/%s/x.\n' % ('D', 'Src'), encoding='utf-8')
        (folder / 'b.md').write_text('Nothing specific.\n', encoding='utf-8')
        self.assertEqual(shipped.violations(folder, home=self.home, root=Path(self.home)),
                         [('shipped/a.md', 'machine path')])

    def test_no_config_gives_no_install_names(self):
        self.assertEqual(shipped.install_patterns(self.home), [])
        self.assertEqual(shipped.forbidden(self.home), shipped.BASE_FORBIDDEN)

    def test_violations_names_a_file_holding_an_install_name(self):
        self.write_config(json.dumps({'scrub_patterns': ['acme']}))
        folder = Path(self.home) / 'shipped'
        folder.mkdir()
        (folder / 'a.md').write_text('Built for Acme.\n', encoding='utf-8')
        (folder / 'b.md').write_text('Nothing specific.\n', encoding='utf-8')
        self.assertEqual(shipped.violations(folder, home=self.home, root=Path(self.home)),
                         [('shipped/a.md', 'install name')])

    def test_a_malformed_config_fails_the_call_not_the_import(self):
        self.write_config('{"scrub_patterns": ["(unclosed"]}')
        with self.assertRaises(ConfigError):
            shipped.install_patterns(self.home)
        with mock.patch.object(kuser, 'user_config', side_effect=ConfigError('malformed')):
            importlib.reload(shipped)          # importing reads no machine config
        importlib.reload(shipped)


class RealScrubPatternsTest(unittest.TestCase):
    """tests.real_install_config, the home fence's one exception, with the home it records before
    the fence pointed at a temp folder: it opens the machine config only and returns its
    install-name keys only; install_patterns() with no home uses it and skips by name without them,
    while scan and violations still run the base patterns first and fail on a base finding.
    The process stays fenced, so any other home lookup would raise HomeFenced."""

    write_config = InstallPatternsTest.write_config

    def setUp(self):
        InstallPatternsTest.setUp(self)
        p = mock.patch.object(tests, '_REAL_HOME', self.home)
        p.start()
        self.addCleanup(p.stop)

    def opened(self, fn):
        """(fn's result, [every file it opened])."""
        seen, real = [], open

        def spy(file, *a, **kw):
            seen.append(os.path.normcase(os.path.abspath(file)))
            return real(file, *a, **kw)
        with mock.patch('builtins.open', spy):
            return fn(), seen

    def test_reads_only_the_install_name_keys(self):
        sessions = os.path.join(self.home, 'sessions.json')
        self.write_config(json.dumps({
            'scrub_patterns': ['acme', 'widgetco'],
            'machine_path_folders': ['Src'],
            'temp_dir': self.home,                                     # unlisted: left out
            'pm_start': {'hub_url': 'https://example.invalid/a/hubid', 'sessions_file': sessions,
                         'repos': ['alpha'], 'standing': ['unlisted']},
            'coordinator_start': {'cap': {'beta': 2}},
            'conductor_start': {'cap': {'gamma': 1}}}))           # shadowed, as install_names reads
        got, seen = self.opened(tests.real_install_config)
        self.assertEqual(got, ({'scrub_patterns': ['acme', 'widgetco'],
                                'machine_path_folders': ['Src'],
                                'pm_start': {'hub_url': 'https://example.invalid/a/hubid',
                                             'sessions_file': sessions, 'repos': ['alpha']},
                                'cap': {'beta': 2}}, None))
        self.assertEqual(seen, [os.path.normcase(kuser.config_file(self.home))])
        pats = shipped.install_patterns()
        self.assertEqual([what for _rx, what in pats],
                         ['install name', 'install name', 'machine path'])
        self.assertEqual([rx.pattern for rx, _what in pats][:2], ['acme', 'widgetco'])

    def test_conductor_start_cap_when_coordinator_start_is_unset(self):
        self.write_config(json.dumps({'conductor_start': {'cap': {'gamma': 1}}}))
        cfg, _why = tests.real_install_config()
        self.assertEqual(cfg, {'scrub_patterns': None, 'machine_path_folders': None,
                               'pm_start': {}, 'cap': {'gamma': 1}})

    def test_no_config_file_is_a_named_skip(self):
        cfg, why = tests.real_install_config()
        self.assertIsNone(cfg)
        self.assertIn('no machine config file', why)
        with self.assertRaises(unittest.SkipTest) as cm:
            shipped.install_patterns()
        self.assertEqual(str(cm.exception), shipped.NO_INSTALL_NAMES % why)

    def test_no_scrub_patterns_key_is_a_named_skip(self):
        self.write_config(json.dumps({'pm_start': {'hub_url': 'https://example.invalid/a/hubid'}}))
        cfg, why = tests.real_install_config()
        self.assertIsNone(cfg['scrub_patterns'])
        with self.assertRaises(unittest.SkipTest) as cm:
            shipped.forbidden()
        self.assertIn('sets no scrub_patterns', str(cm.exception))

    def shipped_folder(self):
        """A temp folder of shipped text: one file with a ruling id and a user path (each built
        here, so this file holds none) and a drive path into another folder, which the base
        patterns do not refuse; one clean file."""
        folder = Path(self.home) / 'shipped'
        folder.mkdir()
        bad = 'see %s-%d, %s:\\%s\\x and %s:/%s/y\n' % ('R', 7, 'C', 'Users', 'D', 'Src')
        (folder / 'a.md').write_text(bad, encoding='utf-8')
        (folder / 'b.md').write_text('Nothing specific.\n', encoding='utf-8')
        return folder

    def test_base_patterns_run_with_no_config_file(self):
        folder = self.shipped_folder()
        try:                                    # a skip here would hide the base patterns: fail
            found = shipped.violations(folder, root=Path(self.home))
        except unittest.SkipTest:
            self.fail('base patterns were skipped')
        self.assertEqual(found, [('shipped/a.md', 'ruling id'), ('shipped/a.md', 'Windows user path')])
        (folder / 'a.md').unlink()
        with self.assertRaises(unittest.SkipTest) as cm:          # clean base scan: the named skip
            shipped.violations(folder, root=Path(self.home))
        self.assertIn('no machine config file', str(cm.exception))

    def test_base_patterns_run_with_no_scrub_patterns_key(self):
        self.write_config(json.dumps({'pm_start': {}}))
        folder = self.shipped_folder()
        try:                                    # a skip here would hide the base patterns: fail
            found = shipped.scan([('a', (folder / 'a.md').read_text(encoding='utf-8'))])
        except unittest.SkipTest:
            self.fail('base patterns were skipped')
        self.assertEqual(found, [('a', 'ruling id'), ('a', 'Windows user path')])
        with self.assertRaises(unittest.SkipTest) as cm:
            shipped.scan([('b', 'Nothing specific.')])
        self.assertIn('sets no scrub_patterns', str(cm.exception))

    def test_the_real_configs_machine_path_folders_are_refused_with_no_scrub_patterns(self):
        # The key set, the drive path into another folder that shipped_folder writes is refused
        # too, and no skip hides it: the folder list is read as the install names are.
        self.write_config(json.dumps({'machine_path_folders': ['Src']}))
        folder = self.shipped_folder()
        self.assertEqual(shipped.violations(folder, root=Path(self.home)),
                         [('shipped/a.md', 'ruling id'), ('shipped/a.md', 'Windows user path'),
                          ('shipped/a.md', 'machine path')])

    def test_install_names_run_after_the_base_patterns(self):
        self.write_config(json.dumps({'scrub_patterns': ['acme']}))
        text = 'Acme, %s-%d\n' % ('R', 7)
        self.assertEqual(shipped.scan([('a', text)]), [('a', 'ruling id'), ('a', 'install name')])
        self.assertEqual(shipped.scan([('b', 'plain')]), [])

    def test_a_malformed_real_config_fails(self):
        self.write_config('{"scrub_patterns": ["(unclosed"]}')
        with self.assertRaises(ConfigError):
            tests.real_install_config()


if __name__ == '__main__':
    unittest.main()
