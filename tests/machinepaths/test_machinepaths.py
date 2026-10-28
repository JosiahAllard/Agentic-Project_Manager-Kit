"""kitlib.machinepaths and tools/machine_paths.py, the guard against hard-coded machine
paths.

Every planted path is assembled at run time from pieces, so this file never carries one itself
and no machine-path sweep of the kit finds one here. Each refusal has a passing twin, because a
guard that fails everything would pass the refusal half alone. The repository's configuration is
a `.kit.json` "machine_paths" section in a throwaway tree (tests/gitfixture.py).
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import machinepaths as M
from kitlib.git import cmd
from tests import gitfixture
from tests.home import TempHomeCase, machine_config as write_machine_config

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'machine_paths.py')

BS = '\\'
DRIVE = 'C' + ':'
GITHUB = DRIVE + BS + 'GitHub' + BS + 'some-repo'
GITHUB_DOUBLED = DRIVE + BS + BS + 'GitHub' + BS + BS + 'some-repo'
GITHUB_FORWARD = DRIVE + '/GitHub/some-repo'
PROFILE = DRIVE + BS + 'Users' + BS + 'someone' + BS + 'AppData' + BS + 'Local'

# A repository's allow-list as configuration (a real one's, restated).
REPO_CONFIG = {
    'allow': [
        {'glob': 'tools/backlog.py', 'line': '^RULINGS_LEDGER = ',
         'why': 'rulings-ledger read: hard-coded on purpose (a security exception)'},
        {'glob': 'CLAUDE.md', 'line': 'rulings ledger, `',
         'why': 'the rulings-ledger path (a security exception); owner-only file'},
        {'glob': '.claude/settings.json', 'line': 'worktree_guard\\.py',
         'why': 'the worktree_guard hook and its repository root (owner)'},
        {'glob': 'docs/backlog/*.md', 'line': '^(?![a-z_]+: )', 'temporary': True,
         'why': 'TEMPORARY - a backlog record BODY (not a `field:` line)'},
    ],
    'fixtures': [
        {'glob': 'tools/test_paths.py', 'hits': ['[C]:\\\\Users\\\\u'], 'why': ' (a LOCALAPPDATA)'},
        {'glob': 'tools/test_deploy_scripts.py', 'hits': ['[C]:\\\\Users\\\\x']},
    ],
    'known_binary': {'gradle/wrapper/gradle-wrapper.jar': "Gradle's own wrapper jar"},
}


# The drive-rooted folder names a planted tree's machine config sets (machine_path_folders),
# invented for the fixtures: unset, the check would refuse only user-profile paths.
FOLDERS = ('GitHub', 'research', 'Agent' + 'Temp')


class PlantedTree(TempHomeCase):
    """A throwaway git repository the guard is pointed at, under a temp home whose machine config
    sets `folders` as machine_path_folders (kitlib.config.user); None writes no machine config."""
    config = None
    folders = FOLDERS

    def setUp(self):
        super().setUp()
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.write('README.md', 'nothing machine-specific here\n')
        if self.config is not None:
            self.set_config(self.config)
        if self.folders is not None:
            self.machine_config(json.dumps({'machine_path_folders': list(self.folders)}))

    def machine_config(self, body):
        """The machine config text `body`: a JSON object split between the fenced PM repository's
        two files, anything else written as its config.json."""
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        write_machine_config(self.home, data if isinstance(data, dict) else body, case=self)

    def set_config(self, section, top=None):
        data = dict(top or {})
        data['machine_paths'] = section
        self.write('.kit.json', json.dumps(data, indent=1) + '\n')

    def git(self, *args):
        return self.repo.git(*args)

    def write(self, rel, text, track=True):
        self.repo.write(rel, text)
        if track:
            self.git('add', rel)

    def run_guard(self):
        code, lines = M.check(self.root)
        return code, '\n'.join(lines)


class APlantedHitFails(PlantedTree):

    def test_a_clean_tree_passes(self):
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('MACHINE PATHS OK', out)

    def test_every_separator_spelling_is_caught(self):
        self.write('notes.md', 'one %s\ntwo %s\nthree %s\n' % (GITHUB, GITHUB_DOUBLED, GITHUB_FORWARD))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        for n in (1, 2, 3):
            self.assertIn('notes.md:%d:' % n, out)
        self.assertIn('MACHINE PATHS FAILED: 3 hit(s)', out)

    def test_a_user_profile_path_is_caught(self):
        self.write('tools/thing.py', 'PLACE = r"%s"\n' % PROFILE)
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/thing.py:1:', out)

    def test_a_posix_home_in_a_tracked_file_is_a_hit(self):
        H = '/' + 'home' + '/'
        self.write('tools/thing.py', 'PLACE = %r\n' % (H + 'someone/x'))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/thing.py:1: hard-coded machine path ' + H + 'someone', out)

    def test_placeholders_and_variables_are_not_paths(self):
        self.write('notes.md', '%s%sUsers%s<name>%sAppData and %%USERPROFILE%%%sDocuments and '
                   '%%BETA_DATA%%%slive\n' % (DRIVE, BS, BS, BS, BS, BS))
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)

    def test_an_untracked_file_is_not_searched_and_a_tracked_one_is(self):
        # A local note beside the checkout (untracked, not ignored) never fails the sweep; the
        # same text in a tracked file does, read from the working tree.
        self.write('note.local.md', PROFILE + '\n', track=False)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertNotIn('note.local.md', out)
        self.write('fresh.md', 'clean\n')
        self.repo.write('fresh.md', PROFILE + '\n')      # tracked; the hit is unstaged work
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('fresh.md:1:', out)
        self.assertNotIn('note.local.md', out)

    def test_the_research_tree_and_agent_temp_are_caught(self):
        self.write('notes.md', 'a %s\nb %s\nc tools%sresearch and %%DEV_ROOT%%%sresearch\n'
                   % ('D' + ':' + BS + 'research' + BS + 'notes', 'D' + ':' + BS + BS + 'AgentTemp',
                      BS, BS))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('notes.md:1:', out)
        self.assertIn('notes.md:2:', out)
        self.assertNotIn('notes.md:3:', out)

    def test_a_file_it_cannot_search_warns_by_name(self):
        with open(os.path.join(self.root, 'blob.bin'), 'wb') as fh:
            fh.write(b'\0' + GITHUB.encode())
        self.git('add', 'blob.bin')
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: blob.bin was not searched: binary', out)

    def test_a_file_over_the_size_cap_warns_by_name(self):
        with open(os.path.join(self.root, 'huge.txt'), 'wb') as fh:
            fh.write(GITHUB.encode() + b'\n' + b'x' * M.MAX_BYTES)
        self.git('add', 'huge.txt')
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: huge.txt was not searched: over %d bytes' % M.MAX_BYTES, out)

    def test_an_unreadable_file_warns_by_name(self):
        # Tracked as a file, then replaced in the working tree by a folder of the same name: it
        # still exists and has a size, but opening it for reading raises OSError.
        self.write('gone.md', GITHUB + '\n')
        os.remove(os.path.join(self.root, 'gone.md'))
        os.mkdir(os.path.join(self.root, 'gone.md'))
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: gone.md was not searched: unreadable', out)


class KnownBinary(PlantedTree):
    """known_binary: default none (a binary warns by name); set, the named file is skipped in
    silence and an unnamed one still warns."""

    def plant_jar(self, rel):
        path = os.path.join(self.root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'PK\0\0')
        self.git('add', rel)

    def test_default_a_jar_warns(self):
        self.plant_jar('gradle/wrapper/gradle-wrapper.jar')
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: gradle/wrapper/gradle-wrapper.jar was not searched: binary', out)

    def test_a_known_binary_is_not_warned_about(self):
        self.set_config(REPO_CONFIG)
        rel = next(iter(REPO_CONFIG['known_binary']))
        self.plant_jar(rel)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertNotIn('WARNING', out)
        self.plant_jar('other/thing.jar')
        rc, out = self.run_guard()
        self.assertIn('WARNING: other/thing.jar was not searched: binary', out)


class TheTreeIsListedByOneGitProcess(PlantedTree):
    """Tracked, untracked and tracked-although-ignored files come from one `ls-files -co`."""

    def test_tracked_untracked_and_ignored_files(self):
        self.write('.gitignore', '*.log\n')
        self.write('kept.log', 'tracked although ignored\n', track=False)
        self.git('add', '-f', 'kept.log')
        self.write('new.txt', 'untracked\n', track=False)
        self.write('skip.log', 'untracked and ignored\n', track=False)
        calls = []
        real = cmd.call

        def counted(args, *a, **k):
            calls.append(args)
            return real(args, *a, **k)
        with mock.patch.object(M.cmd, 'call', counted):
            files = M.tree_files(self.root)
        self.assertEqual(files, ['.gitignore', 'README.md', 'kept.log', 'new.txt'])
        self.assertEqual(len(calls), 1, calls)

    def test_the_sweep_lists_tracked_files_only(self):
        self.write('.gitignore', '*.log\n')
        self.write('kept.log', 'tracked although ignored\n', track=False)
        self.git('add', '-f', 'kept.log')
        self.write('new.txt', 'untracked\n', track=False)
        self.write('skip.log', 'untracked and ignored\n', track=False)
        self.assertEqual(M.tracked_files(self.root), ['.gitignore', 'README.md', 'kept.log'])

    def test_outside_a_repository_reports_the_git_failure(self):
        outside = tempfile.mkdtemp(prefix='machine-paths-outside-')
        self.addCleanup(gitfixture.rmtree, outside)
        with self.assertRaises(M.ListError) as caught:
            M.tree_files(outside)
        self.assertIn('exit 128', str(caught.exception))
        self.assertIn('not a git repository', str(caught.exception).lower())


class TheAllowListIsLineScoped(PlantedTree):
    config = REPO_CONFIG

    def test_the_ledger_constant_passes_and_another_line_in_that_file_does_not(self):
        ledger_line = "RULINGS_LEDGER = r'%s%sLEDGER.md'\n" % (GITHUB, BS)
        self.write('tools/backlog.py', ledger_line)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.write('tools/backlog.py', ledger_line + "OTHER = r'%s'\n" % GITHUB)
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/backlog.py:2:', out)
        self.assertNotIn('tools/backlog.py:1:', out)

    def test_the_ledger_constant_passes_on_the_moved_drive(self):
        # The entry follows the line, not the drive.
        moved = BS.join(('D' + ':', 'GitHub', 'pm-repo', 'rulings', 'LEDGER.md'))
        self.write('tools/backlog.py', "RULINGS_LEDGER = r'%s'\n" % moved)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)

    def test_the_settings_entry_follows_the_hook_not_the_root(self):
        for root in (DRIVE + '/GitHub/beta', 'D' + ':/GitHub/beta'):
            self.write('.claude/settings.json',
                       '"command": "python \\"hooks/worktree_guard.py\\" \\"%s\\""\n' % root)
            rc, out = self.run_guard()
            self.assertEqual(rc, 0, out)

    def test_a_stale_entry_warns_and_does_not_fail(self):
        self.write('tools/backlog.py', 'RULINGS_LEDGER = None\n')
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: allow-list entry tools/backlog.py', out)

    def test_a_stale_glob_entry_warns_too(self):
        self.write('docs/backlog/AA.md', 'id: AA\n\nnothing machine-specific\n')
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: allow-list entry docs/backlog/*.md', out)
        self.assertIn('(it was temporary)', out)

    def test_a_fixture_allows_its_invented_name_and_not_a_real_one(self):
        invented = DRIVE + BS + 'Users' + BS + 'x' + BS + 'Documents'
        real = DRIVE + BS + 'Users' + BS + 'someone-real' + BS + 'Documents'
        self.write('tools/test_deploy_scripts.py', "A = r'%s'\n" % invented)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.write('tools/test_deploy_scripts.py', "A = r'%s'\nB = r'%s'\n" % (invented, real))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/test_deploy_scripts.py:2:', out)
        self.assertNotIn('tools/test_deploy_scripts.py:1:', out)

    def test_a_real_agent_temp_in_a_fixture_file_is_caught(self):
        invented = DRIVE + BS + 'Users' + BS + 'u' + BS + 'AppData'
        real = 'D' + ':' + BS + 'AgentTemp' + BS + 'claude' + BS + 'x'
        self.write('tools/test_paths.py', "A = r'%s'\n" % invented)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.write('tools/test_paths.py', "A = r'%s'\nB = r'%s'\n" % (invented, real))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/test_paths.py:2:', out)
        self.assertNotIn('tools/test_paths.py:1:', out)

    def test_a_backlog_body_passes_and_a_field_does_not(self):
        self.write('docs/backlog/AA.md', 'id: AA\nsummary: s\n\n## Why\n\nsource `%s`\n' % GITHUB)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.write('docs/backlog/AA.md', 'id: AA\nref: %s\n\n## Why\n\nsource\n' % GITHUB)
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('docs/backlog/AA.md:2:', out)

    def test_without_the_config_the_same_line_is_a_hit(self):
        """The allow-list is the configuration: with no `.kit.json`, the ledger line is refused."""
        self.git('rm', '-q', '--cached', '.kit.json')
        os.remove(os.path.join(self.root, '.kit.json'))
        self.write('tools/backlog.py', "RULINGS_LEDGER = r'%s'\n" % GITHUB)
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/backlog.py:1:', out)


class NoBuiltInEntries(PlantedTree):
    """The PM kit's built-in entries (its `_paths.py` fallbacks, `test_paths.py` table, kit stubs
    and backlog.py ledger) are not ported: with no config those lines are hits like any other."""

    def test_a_pm_kit_fallback_line_is_a_hit_without_config(self):
        self.write('tools/_paths.py', "DEV_ROOT_FALLBACK = '%s'\n" % ('D' + ':' + BS + BS + 'GitHub'))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/_paths.py:1:', out)
        self.assertNotIn('WARNING', out)


class Scan(PlantedTree):
    """scan: default every file; set, only matching files are searched."""

    def test_default_searches_every_file(self):
        self.write('deep/down/notes.md', GITHUB + '\n')
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)

    def test_set_searches_only_matching_files(self):
        self.set_config({'scan': ['tools/*.py', '*.md']})
        self.write('launcher/run.ps1', GITHUB + '\n')
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.write('tools/x.py', GITHUB + '\n')
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/x.py:1:', out)
        self.assertNotIn('launcher/run.ps1', out)

    def test_an_empty_scan_refuses(self):
        self.set_config({'scan': []})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)
        self.assertIn('NOT CHECKED', out)


class FallbackVars(PlantedTree):
    """fallback_vars: the path sits on the line that reads its variable."""
    D = DRIVE

    def py(self, var):
        return "ROOT = os.environ.get('%s', r'%s%sGitHub%spm-repo')\n" % (var, self.D, BS, BS)

    def test_default_a_fallback_line_is_a_hit(self):
        self.write('tools/x.py', self.py('PM_ROOT'))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)

    def test_a_fallback_beside_its_variable_is_allowed(self):
        self.set_config({'fallback_vars': ['PM_ROOT', 'GAMMA_REPO']})
        ps = "$repo = if ($env:GAMMA_REPO) { $env:GAMMA_REPO } else { '%s%sGitHub' }\n" % (self.D, BS)
        self.write('tools/x.py', self.py('PM_ROOT'))
        self.write('tools/x.ps1', ps)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)

    def test_a_fallback_for_an_unknown_variable_is_not_allowed(self):
        self.set_config({'fallback_vars': ['PM_ROOT']})
        self.write('tools/x.py', self.py('HOME_DIR'))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/x.py:1:', out)

    def test_a_bad_variable_name_refuses(self):
        self.set_config({'fallback_vars': ['PM ROOT']})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)


class RemovedPieceFixtures(PlantedTree):
    """piece_fixtures is gone: a repository that still sets it is refused as an unknown key."""

    def test_piece_fixtures_refuses_as_an_unknown_key(self):
        self.set_config({'piece_fixtures': [{'piece': 'worker', 'hits': ['[C]:/Users/x']}]})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)
        self.assertIn('piece_fixtures', out)

    def test_the_repo_config_loads(self):
        self.set_config(REPO_CONFIG)
        cfg = M.load_config(self.root)
        self.assertTrue(cfg.allow)


class TheConfigRefusesWhatItCannotUse(PlantedTree):

    def test_an_unknown_key_refuses(self):
        self.set_config({'alow': []})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)
        self.assertIn('unknown key', out)

    def test_an_entry_without_a_why_refuses(self):
        self.set_config({'allow': [{'glob': 'x', 'line': '.'}]})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)
        self.assertIn('why', out)

    def test_an_entry_with_a_blank_why_refuses(self):
        self.set_config({'allow': [{'glob': 'x', 'line': '.', 'why': '  '}]})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)
        self.assertIn('why', out)

    def test_an_allow_entry_without_a_line_refuses(self):
        self.set_config({'allow': [{'glob': 'x', 'why': 'w'}]})
        rc, out = self.run_guard()
        self.assertEqual(rc, 2, out)
        self.assertIn('"line"', out)

    def test_a_bad_regex_refuses(self):
        self.set_config({'allow': [{'glob': 'x', 'line': '(', 'why': 'w'}]})
        with self.assertRaises(M.ConfigError):
            M.load_config(self.root)


class MachinePathFolders(PlantedTree):
    """The machine config's machine_path_folders names the drive-rooted folders refused besides a
    user profile, which is refused whatever it says. Unset, only user-profile paths are refused and
    a NOTE says so; a config the kit cannot read warns and checks user-profile paths only, never
    NOT CHECKED."""
    folders = None
    SRC = 'D' + ':' + BS + 'Src' + BS + 'x'

    def tool(self):
        env = dict(gitfixture.fixture_env(), USERPROFILE=self.home, HOME=self.home)
        return subprocess.run([sys.executable, TOOL, '-C', self.root], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', env=env)

    def test_a_configured_folder_is_caught_and_another_is_not(self):
        self.machine_config(json.dumps({'machine_path_folders': ['Src']}))
        self.write('notes.md', 'a %s\nb %s\nc %s\n' % (self.SRC, GITHUB, PROFILE))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('notes.md:1:', out)
        self.assertNotIn('notes.md:2:', out)
        self.assertIn('notes.md:3:', out)
        self.assertNotIn('NOTE:', out)

    def test_an_empty_list_leaves_the_profile_only(self):
        self.machine_config(json.dumps({'machine_path_folders': []}))
        self.write('notes.md', 'a %s\n' % GITHUB)
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, out)
        self.write('notes.md', 'a %s\nb %s\n' % (GITHUB, PROFILE))
        rc, out = self.run_guard()
        self.assertEqual(rc, 1, out)
        self.assertIn('notes.md:2:', out)

    def test_unset_only_the_profile_is_caught_and_a_note_says_so(self):
        for body in (None, json.dumps({'owner_email': 'someone@example.com'})):
            if body is not None:
                self.machine_config(body)
            self.write('notes.md', 'a %s\nb %s\n' % (self.SRC, GITHUB))
            rc, out = self.run_guard()
            self.assertEqual(rc, 0, out)
            self.assertIn('NOTE:', out)
            self.assertIn('machine_path_folders', out)
            self.write('notes.md', 'a %s\nb %s\n' % (GITHUB, PROFILE))
            rc, out = self.run_guard()
            self.assertEqual(rc, 1, out)
            self.assertNotIn('notes.md:1:', out)
            self.assertIn('notes.md:2:', out)

    def test_a_config_it_cannot_read_warns_and_still_checks_the_profile(self):
        for body in ('{not json', json.dumps({'machine_path_folders': ['a/b']})):
            self.machine_config(body)
            self.write('notes.md', 'a %s\nb %s\n' % (GITHUB, PROFILE))
            rc, out = self.run_guard()
            self.assertEqual(rc, 1, out)
            self.assertNotIn('notes.md:1:', out)
            self.assertIn('notes.md:2:', out)
            self.assertIn('WARNING: machine config', out)
            self.assertNotIn('a/b', out)

    def test_scan_staged_uses_the_configured_folders(self):
        self.machine_config(json.dumps({'machine_path_folders': ['Src']}))
        self.write('notes.md', 'a %s\nb %s\n' % (GITHUB, self.SRC))
        hits, _used, _read, _skipped = M.scan_staged(self.root, ['notes.md'])
        self.assertEqual([h[:2] for h in hits], [('notes.md', 2)])

    def test_the_tool_reads_the_machine_config(self):
        self.machine_config(json.dumps({'machine_path_folders': ['Src']}))
        self.write('notes.md', 'a %s\n' % self.SRC)
        done = self.tool()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('notes.md:1:', done.stdout)

    def test_the_tool_prints_the_note_when_unset(self):
        done = self.tool()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('NOTE:', done.stdout)
        self.assertIn('machine_path_folders', done.stdout)

    def test_find_takes_a_folder_list_and_reads_no_config(self):
        self.machine_config(json.dumps({'machine_path_folders': ['Src']}))
        self.assertEqual(M.find(self.SRC), [])
        self.assertEqual(M.find(GITHUB), [])
        self.assertTrue(M.find(PROFILE))
        self.assertTrue(M.find(self.SRC, ('Src',)))
        self.assertEqual(M.find(GITHUB, ('Src',)), [])
        self.assertTrue(M.find(PROFILE, ()))

    def test_a_folder_name_ending_in_a_symbol_is_caught(self):
        D = 'D' + ':' + BS
        for name in ('C#', 'Dev+'):
            self.assertTrue(M.find(D + name + BS + 'x', (name,)), name)
            self.assertTrue(M.find(D + name, (name,)), name)
            self.assertEqual(M.find(D + name + 'x' + BS + 'y', (name,)), [], name)
        self.assertEqual(M.find(D + 'Srcs' + BS + 'x', ('Src',)), [])


class FindNeedsNoConfig(unittest.TestCase):
    """`find` is what other tools call on one string; a repository's spellings, through it."""

    def test_every_spelling_is_caught(self):
        D = DRIVE
        folders = ('GitHub', 'Agent' + 'Temp')
        for s in (D + '\\GitHub\\Gamma', D + '\\\\GitHub\\\\Gamma', 'c:' + '/github/x',
                  D + '\\Users\\someone\\AppData', D + '\\\\Users\\\\someone', 'D:' + '\\AgentTemp\\x',
                  'd:' + '/GitHub'):
            self.assertTrue(M.find(s, folders), s)

    def test_with_no_folder_list_only_a_profile_is_caught(self):
        self.assertTrue(M.find(DRIVE + '\\Users\\someone\\AppData'))
        self.assertEqual(M.find(DRIVE + '\\GitHub\\Gamma'), [])

    def test_variables_are_not_caught(self):
        for s in ('%GAMMA_REPO%\\.claude', '$env:LOCALAPPDATA', '%PM_ROOT%\\reviews',
                  'c--GitHub-Gamma', DRIVE + '\\Users\\<USER>\\x'):
            self.assertEqual(M.find(s, ('GitHub',)), [], s)

    def test_a_posix_home_is_caught(self):
        H = '/' + 'home' + '/'
        for s in (H + 'someone/x', 'cd ' + H + 'someone', '`' + H + 'a1_b-c/.config`',
                  '"' + H + 'someone"'):
            self.assertTrue(M.find(s), s)
        self.assertEqual(M.find(H + 'someone/x'), [H + 'someone'])
        self.assertTrue(M.find(H + 'someone/x', ('GitHub',)))

    def test_posix_home_placeholders_are_not_paths(self):
        H = '/' + 'home' + '/'
        for s in (H + '<user>/x', H + '<name>', H + '$USER/x', H + '%USER%/x', H + '{user}/x',
                  '~/x', '$HOME/x', '%USERPROFILE%/x'):
            self.assertEqual(M.find(s), [], s)

    def test_prose_that_only_looks_like_a_home_is_not_a_path(self):
        H = '/' + 'home'
        for s in (H, H + '/', H + ' directory', H + 'page/x', H + 'work/x', H + '/.config',
                  'src' + H + '/x', '..' + H + '/x', '$HOME' + H + '/x', 'a/b' + H + '/x',
                  'C:' + BS + 'home' + BS + 'x'):
            self.assertEqual(M.find(s), [], s)

    def test_import_and_find_read_no_configuration(self):
        outside = tempfile.mkdtemp(prefix='machine-paths-noconf-')
        self.addCleanup(gitfixture.rmtree, outside)
        with open(os.path.join(outside, '.kit.json'), 'w') as fh:
            fh.write('{not json')
        probe = ('import sys; sys.path.insert(0, %r); from kitlib import machinepaths; '
                 'print(machinepaths.find("x"))' % KIT)
        got = subprocess.run([sys.executable, '-c', probe], cwd=outside, capture_output=True,
                             text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), '[]')


class FailedGitReads(unittest.TestCase):
    """A listing git could not give is NOT CHECKED (exit 2) and says why, never a pass."""

    def check_failure(self, result, message):
        cfg = M.Config((), {}, ('*',), None)
        with mock.patch.object(M, 'load_config', return_value=cfg), \
                mock.patch.object(M.cmd, 'call', return_value=result):
            code, lines = M.check(KIT)
        out = '\n'.join(lines)
        self.assertEqual(code, 2, out)
        self.assertIn('MACHINE PATHS NOT CHECKED', out)
        self.assertIn('git ls-files', out)
        self.assertIn(message, out)

    def test_failed_listing_names_exit_and_stderr(self):
        self.check_failure(cmd.Result(128, b'', 'fatal: not a git repository'),
                           'exit 128: fatal: not a git repository')

    def test_failed_listing_without_stderr_names_exit(self):
        self.check_failure(cmd.Result(128, b'', ''), 'exit 128: (nothing on stderr)')

    def test_missing_git_is_reported_as_not_checked(self):
        self.check_failure(cmd.Result(None, b'', 'could not run git in x: git missing'),
                           'could not run git in x: git missing')

    def test_timed_out_git_is_reported_as_not_checked(self):
        self.check_failure(cmd.Result(None, b'', 'git ls-files timed out after 60 seconds'),
                           'timed out after 60 seconds')

    def test_successful_empty_listing_is_an_answer(self):
        with mock.patch.object(M.cmd, 'call', return_value=cmd.Result(0, b'', '')):
            self.assertEqual(M.tree_files(KIT), [])

    def test_an_empty_tree_is_not_checked(self):
        cfg = M.Config((), {}, ('*',), None)
        with mock.patch.object(M, 'load_config', return_value=cfg), \
                mock.patch.object(M.cmd, 'call', return_value=cmd.Result(0, b'', '')):
            code, lines = M.check(KIT)
        self.assertEqual(code, 2, lines)
        self.assertIn('no file was read', '\n'.join(lines))


class ScanStaged(PlantedTree):
    """scan_staged reads the INDEX copy, never the working tree, and fails closed."""

    def test_the_staged_copy_is_what_is_searched(self):
        self.write('notes.md', GITHUB + '\n')                     # staged with the hit
        self.repo.write('notes.md', 'clean now\n')                # the work tree no longer has it
        hits, _used, read, _skipped = M.scan_staged(self.root, ['notes.md'])
        self.assertEqual(read, ['notes.md'])
        self.assertEqual([h[:2] for h in hits], [('notes.md', 1)])

    def test_a_clean_staged_copy_passes_whatever_the_work_tree_holds(self):
        self.write('notes.md', 'clean\n')
        self.repo.write('notes.md', GITHUB + '\n')
        hits, _used, read, _skipped = M.scan_staged(self.root, ['notes.md'])
        self.assertEqual((hits, read), ([], ['notes.md']))

    def test_a_path_with_no_staged_blob_fails_closed(self):
        got, why = M.scan_staged(self.root, ['never-staged.md'])
        self.assertIsNone(got)
        self.assertIn('no staged blob for never-staged.md', why)


class TheCommandLine(PlantedTree):
    """tools/machine_paths.py: -C names the work tree, the exit code is the library's, and a
    usage mistake exits 2 with a USAGE: line."""

    def cli(self, *args, cwd=None):
        done = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or KIT,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env())
        return done.returncode, done.stdout + done.stderr

    def test_dash_c_names_the_tree_and_a_hit_exits_1(self):
        rc, out = self.cli('-C', self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn('MACHINE PATHS OK', out)
        self.write('tools/notes.md', GITHUB + '\n')
        rc, out = self.cli('-C', os.path.join(self.root, 'tools'))
        self.assertEqual(rc, 1, out)
        self.assertIn('tools/notes.md:1:', out)

    def test_root_is_the_same_option(self):
        rc, out = self.cli('--root', self.root)
        self.assertEqual(rc, 0, out)

    def test_the_current_directory_is_the_default(self):
        rc, out = self.cli(cwd=self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn('MACHINE PATHS OK', out)

    def test_a_directory_in_no_work_tree_is_not_checked(self):
        outside = tempfile.mkdtemp(prefix='machine-paths-cli-')
        self.addCleanup(gitfixture.rmtree, outside)
        rc, out = self.cli('-C', outside)
        self.assertEqual(rc, 2, out)
        self.assertIn('MACHINE PATHS NOT CHECKED', out)

    def test_an_unknown_option_is_a_usage_error(self):
        rc, out = self.cli('--roo', self.root)
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE:', out)


if __name__ == '__main__':
    unittest.main()
