"""tests/shipped.py's install_patterns, forbidden and violations read the machine config's
scrub_patterns at call time, from an injected home (a temp folder here, never the real one)."""
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
    install-name keys only; install_patterns() with no home uses it and skips by name without them.
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
            'temp_dir': self.home,                                     # unlisted: left out
            'pm_start': {'hub_url': 'https://example.invalid/a/hubid', 'sessions_file': sessions,
                         'repos': ['alpha'], 'standing': ['unlisted']},
            'coordinator_start': {'cap': {'beta': 2}},
            'conductor_start': {'cap': {'gamma': 1}}}))           # shadowed, as install_names reads
        got, seen = self.opened(tests.real_install_config)
        self.assertEqual(got, ({'scrub_patterns': ['acme', 'widgetco'],
                                'pm_start': {'hub_url': 'https://example.invalid/a/hubid',
                                             'sessions_file': sessions, 'repos': ['alpha']},
                                'cap': {'beta': 2}}, None))
        self.assertEqual(seen, [os.path.normcase(kuser.config_file(self.home))])
        pats = shipped.install_patterns()
        self.assertEqual([rx.pattern for rx, _what in pats], ['acme', 'widgetco'])

    def test_conductor_start_cap_when_coordinator_start_is_unset(self):
        self.write_config(json.dumps({'conductor_start': {'cap': {'gamma': 1}}}))
        cfg, _why = tests.real_install_config()
        self.assertEqual(cfg, {'scrub_patterns': None, 'pm_start': {}, 'cap': {'gamma': 1}})

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

    def test_a_malformed_real_config_fails(self):
        self.write_config('{"scrub_patterns": ["(unclosed"]}')
        with self.assertRaises(ConfigError):
            tests.real_install_config()


if __name__ == '__main__':
    unittest.main()
