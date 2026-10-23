"""kitlib.pmstart.allow_rules: the session-start check for permission rules that name a plugin export
other than the current one (docs/pm-start.md "Allow rules"). Fake export roots and repositories in
temp folders only."""
import datetime
import json
import os
import tempfile
import unittest

from kitlib import kitroot, pluginsource, pmstart
from kitlib.stubs import memory as kmemory
from tests.pmstart import keep_off_hub_site, keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 6, 9, 0)
CUR, OLD, GONE = '0123456789ab', 'aaaaaaaaaaaa', 'bbbbbbbbbbbb'


def setUpModule():
    keep_off_launcher_repos()
    keep_off_hub_site()


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


class AllowRulesCheckTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = os.path.join(d.name, 'export')
        for sha in (CUR, OLD):
            write(os.path.join(self.root, sha, pluginsource.MARKER), {'sha': sha})
        pluginsource.write_marketplace(self.root, CUR)
        self.settings = pluginsource.Settings(self.root, False, 'origin')
        self.repo = os.path.join(d.name, 'repo')
        os.makedirs(self.repo)
        self.slash = self.root.replace('\\', '/')

    def rule(self, sha, sep='/'):
        return 'Bash(python %s%s%s/tools/land.py -C Y:/repo * --expect-tip * --submit)' % (
            self.root if sep == '\\' else self.slash, sep, sha)

    def check(self, **kw):
        kw.setdefault('repos', {'alpha': self.repo})
        kw.setdefault('pm_repo', lambda cfg: None)
        return pmstart.allow_rules({}, settings=self.settings, **kw)

    def local(self, data):
        write(kmemory.local_path(self.repo), data)

    def test_current_hash_is_silent(self):
        self.local({'permissions': {'allow': [self.rule(CUR)], 'deny': [self.rule(CUR)]}})
        self.assertEqual(self.check(), [])

    def test_gone_hash_is_a_line_with_count_and_command(self):
        self.local({'permissions': {'allow': [self.rule(GONE), self.rule(GONE, '\\'), self.rule(CUR)],
                                    'ask': [self.rule(GONE).upper().replace(GONE.upper(), GONE)]}})
        got = self.check()
        self.assertEqual(got, ['allow rules (alpha): %s names plugin export %s (gone) in 3 rule(s); '
                               'the current export is %s: %s prints the land lines to paste; any other rule '
                               'naming the old export needs its hash changed by hand (owner)'
                               % (kmemory.local_rel(), GONE, CUR, kitroot.command('allow_rules', self.repo))])

    def test_not_current_hash_is_a_line(self):
        write(kmemory.settings_path(self.repo), {'permissions': {'deny': [self.rule(OLD)]}})
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertIn('%s names plugin export %s (not current) in 1 rule(s)' % (kmemory.settings_rel(), OLD), got[0])

    def test_gone_and_not_current_are_separate_lines(self):
        self.local({'permissions': {'allow': [self.rule(OLD), self.rule(GONE)]}})
        got = self.check()
        self.assertEqual(len(got), 2)
        self.assertIn('(gone)', got[0])
        self.assertIn('(not current)', got[1])

    def test_unreadable_file_is_a_could_not_check_line(self):
        self.local('{not json')
        got = self.check()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith('could not check allow_rules (alpha): %s: ' % kmemory.local_rel()), got)

    def test_missing_file_is_silent(self):
        self.assertEqual(self.check(), [])

    def test_missing_repo_folder_is_a_could_not_check_line(self):
        gone = os.path.join(self.repo, 'gone')
        self.assertEqual(self.check(repos={'g': gone}),
                         ['could not check allow_rules (g): no repository folder %s' % gone])

    def test_non_kit_path_is_ignored(self):
        self.local({'permissions': {'allow': ['Bash(python Q:/other/%s/tools/land.py *)' % GONE,
                                              'Bash(python %s/%sx/tools/land.py)' % (self.slash, GONE),
                                              'Read(%s/%s/**)' % (self.slash, CUR)]}})
        self.assertEqual(self.check(), [])

    def test_pm_repo_checked_once(self):
        self.local({'permissions': {'allow': [self.rule(GONE)]}})
        self.assertEqual(len(self.check(pm_repo=lambda cfg: self.repo)), 1)
        got = self.check(repos={}, pm_repo=lambda cfg: self.repo)
        self.assertEqual(len(got), 1)
        self.assertIn('allow rules (PM repo)', got[0])

    def test_no_repos_reads_no_settings(self):
        def boom():
            raise AssertionError('read the machine config')
        orig = pmstart.plugin_settings
        pmstart.plugin_settings = boom
        try:
            self.assertEqual(pmstart.allow_rules({}, repos={}, pm_repo=lambda cfg: None), [])
        finally:
            pmstart.plugin_settings = orig

    def test_checklist_wiring(self):
        self.local({'permissions': {'allow': [self.rule(GONE)]}})
        fakes = dict(pm_data=lambda: None, handoff={'globber': lambda p: []}, reminders={'read': lambda p: '[]'},
                     liveness={'read': lambda p: '{}'}, stubs={'repos': {}, 'check': lambda p: None},
                     memory={'repos': {}, 'pm_repo': lambda cfg: None, 'folder': lambda top: None},
                     allow_rules={'repos': {'alpha': self.repo}, 'pm_repo': lambda cfg: None,
                                  'settings': self.settings},
                     watcher={'beat': lambda: None, 'now': 0}, foreground_lock={'read': lambda: 0})
        txt = pmstart.checklist({}, NOW, procs=[], **fakes)
        self.assertIn('- allow rules (alpha): %s names plugin export %s (gone)' % (kmemory.local_rel(), GONE), txt)


if __name__ == '__main__':
    unittest.main()
