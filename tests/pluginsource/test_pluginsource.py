"""kitlib.pluginsource: the export of pushed main, the marketplace switch, the per-folder claude
runs, prune, the refresh pid file and the behind lines (docs/plugin-source.md)."""
import datetime
import json
import os
import time
import unittest
from unittest import mock

from kitlib import pluginsource as P
from kitlib.pluginsource import claude
from tests.pluginsource import FakeProc, Kit, MANIFEST, read_json

NOW = datetime.datetime(2026, 10, 11, 9, 0)
ME = (1234, 99)


def never_alive(pid, ctime):
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.k = Kit(self)
        self.s = P.Settings(self.k.root, False, 'origin')
        self.lines = []

    def out(self, line):
        self.lines.append(line)

    def refresh(self, folders, proc=None, claude_path='claude.cmd', **kw):
        runner = claude.Runner(self.k.home, claude=claude_path, run_proc=proc or FakeProc())
        kw.setdefault('apply', True)
        return P.refresh(self.k.kit, self.s, folders, self.out, runner=runner, now=NOW, own=ME,
                         alive=never_alive, **kw)

    def remote_sha(self):
        return self.k.repo.git('rev-parse', 'refs/remotes/origin/main')


class ExportTest(Base):
    def test_tracked_files_of_the_pushed_main_only(self):
        r = self.k.repo
        r.write('.claude/worktrees/x/f.txt', 'untracked\n')
        r.write('local.txt', 'not pushed\n')
        r.commit('local only', 'local.txt')
        sha = P.remote_main(self.k.kit, 'origin')
        self.assertEqual(sha, self.remote_sha())
        self.assertNotEqual(sha, r.head())
        folder, _v = P.export(self.k.kit, sha, self.k.root, NOW)
        self.assertEqual(os.path.basename(folder), sha[:12])
        files = sorted(os.path.relpath(os.path.join(d, f), folder).replace(os.sep, '/')
                       for d, _s, fs in os.walk(folder) for f in fs)
        self.assertEqual(files, ['.claude-plugin/marketplace.json', '.claude-plugin/plugin.json',
                                 P.MARKER, 'README.md', 'tools/x.py'])
        self.assertFalse(os.path.exists(folder + '.tmp'))

    def test_the_kits_tests_folder_is_left_out(self):
        """Half an export was the kit's tests/, which nothing reads from an export (a gate runs
        tools/kit_tests.py from its own checkout); a tests folder deeper down stays."""
        r = self.k.repo
        r.write('tests/test_x.py', 'x = 1\n')
        r.write('tests/sub/data.txt', 'y\n')
        r.write('docs/tests/notes.md', 'kept\n')
        r.commit('tests', 'tests/test_x.py', 'tests/sub/data.txt', 'docs/tests/notes.md')
        r.git('push', '-q', 'origin', 'main')
        sha = P.remote_main(self.k.kit, 'origin')
        folder, _v = P.export(self.k.kit, sha, self.k.root, NOW)
        self.assertFalse(os.path.exists(os.path.join(folder, 'tests')))
        self.assertTrue(os.path.isfile(os.path.join(folder, 'docs', 'tests', 'notes.md')))
        self.assertTrue(os.path.isfile(os.path.join(folder, 'tools', 'x.py')))
        self.assertNotIn('tests/', json.dumps(P.read_marker(folder).get('skipped') or []))

    def test_each_hooks_json_holds_the_running_platforms_hooks_file(self):
        """The renderers write hooks/hooks.json (Windows) and hooks/hooks.linux.json beside it; the
        plugin names hooks.json, so a Linux export writes the Linux file's bytes there."""
        r = self.k.repo
        files = {'hosts/a/shell/hooks/hooks.json': 'windows', 'hosts/a/shell/hooks/hooks.linux.json': 'linux',
                 'hosts/b/shell/hooks/hooks.json': 'only windows'}
        for rel, text in files.items():
            r.write(rel, text)
        r.commit('hooks', *files)
        r.git('push', '-q', 'origin', 'main')
        sha = P.remote_main(self.k.kit, 'origin')

        def read(folder, rel):
            with open(os.path.join(folder, *rel.split('/')), encoding='utf-8') as fh:
                return fh.read()
        for platform, want in (('windows', 'windows'), ('linux', 'linux')):
            with self.subTest(platform):
                root = os.path.join(self.k.root, platform)
                folder, _v = P.export(self.k.kit, sha, root, NOW, platform=platform)
                self.assertEqual(read(folder, 'hosts/a/shell/hooks/hooks.json'), want)
                self.assertEqual(read(folder, 'hosts/a/shell/hooks/hooks.linux.json'), 'linux')
                self.assertEqual(read(folder, 'hosts/b/shell/hooks/hooks.json'), 'only windows')
        self.assertEqual(P.platform_manifests(list(files), 'macos'), {})

    def test_the_mcp_and_plugin_manifests_hold_the_running_platforms_file(self):
        """Codex's mcp.json and Claude's plugin.json have a `.linux.json` beside them (their MCP
        servers run python3 there); a Linux export writes those bytes in their place, plugin.json
        still stamped, and a Windows export keeps the commit's own."""
        r = self.k.repo
        linux_manifest = MANIFEST.replace(b'"0.1.0"', b'"0.1.0", "linux": 1')
        r.write('hosts/a/shell/mcp.json', 'windows')
        r.write('hosts/a/shell/mcp.linux.json', 'linux')
        with open(r.path('.claude-plugin', 'plugin.linux.json'), 'wb') as fh:
            fh.write(linux_manifest)
        r.commit('mcp', 'hosts/a/shell/mcp.json', 'hosts/a/shell/mcp.linux.json', '.claude-plugin/plugin.linux.json')
        r.git('push', '-q', 'origin', 'main')
        sha = P.remote_main(self.k.kit, 'origin')
        for platform, mcp, manifest in (('windows', b'windows', MANIFEST), ('linux', b'linux', linux_manifest)):
            with self.subTest(platform):
                folder, version = P.export(self.k.kit, sha, os.path.join(self.k.root, platform), NOW, platform=platform)
                with open(os.path.join(folder, 'hosts', 'a', 'shell', 'mcp.json'), 'rb') as fh:
                    self.assertEqual(fh.read(), mcp)
                with open(os.path.join(folder, '.claude-plugin', 'plugin.json'), 'rb') as fh:
                    self.assertEqual(fh.read(), manifest.replace(b'"0.1.0"', ('"%s"' % version).encode()))

    def test_a_main_the_kit_has_not_fetched_is_refused(self):
        sha = self.k.push_from_clone()
        with self.assertRaises(P.RefreshError) as cm:
            P.remote_main(self.k.kit, 'origin')
        self.assertIn('fetch the kit first', str(cm.exception))
        self.assertIn(sha[:12], str(cm.exception))
        self.assertEqual(self.refresh([]), 1)
        self.assertFalse(os.path.exists(self.k.root))

    def test_plugin_json_version_stamped_and_the_repo_file_unchanged(self):
        sha = P.remote_main(self.k.kit, 'origin')
        folder, version = P.export(self.k.kit, sha, self.k.root, NOW)
        count = self.k.repo.git('rev-list', '--count', sha)
        self.assertEqual(version, '0.1.0-main.%s' % count)
        with open(os.path.join(folder, '.claude-plugin', 'plugin.json'), 'rb') as fh:
            got = fh.read()
        self.assertEqual(got, MANIFEST.replace(b'"0.1.0"', ('"%s"' % version).encode()))
        self.assertTrue(got.startswith(b'\xef\xbb\xbf'))          # its own BOM kept
        with open(self.k.repo.path('.claude-plugin', 'plugin.json'), 'rb') as fh:
            self.assertEqual(fh.read(), MANIFEST)

    def same_path(self, a, b):
        self.assertEqual(os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b)))

    def test_the_marker_names_the_main_checkout(self):
        """kitlib.gate.pin pins a tool started from the export through it (docs/plugin-source.md)."""
        sha = P.remote_main(self.k.kit, 'origin')
        folder, _v = P.export(self.k.kit, sha, self.k.root, NOW)
        self.same_path(P.read_marker(folder)['checkout'], self.k.kit)
        self.same_path(P.export_checkout(folder), self.k.kit)

    def test_an_export_made_from_a_linked_worktree_names_the_main_checkout(self):
        wt = self.k.repo.worktree('elsewhere')
        self.addCleanup(self.k.repo.git, 'worktree', 'remove', '--force', wt)
        sha = P.remote_main(self.k.kit, 'origin')
        folder, _v = P.export(wt, sha, self.k.root, NOW)
        self.same_path(P.read_marker(folder)['checkout'], self.k.kit)
        self.same_path(P.export_checkout(folder), self.k.kit)

    def test_a_kit_git_cannot_place_leaves_the_key_out(self):
        sha = P.remote_main(self.k.kit, 'origin')
        with mock.patch.object(P.repo, 'main_checkout_toplevel', return_value=None):
            folder, _v = P.export(self.k.kit, sha, self.k.root, NOW)
        self.assertNotIn('checkout', P.read_marker(folder))
        self.assertIsNone(P.export_checkout(folder))

    def test_export_checkout_names_only_a_main_checkout(self):
        """No marker, an older marker without the key, a value that is not a path, a linked
        worktree (its .git is a file) and a checkout that has gone: None."""
        folder = os.path.join(self.k.base, 'by-hand')
        os.makedirs(folder)
        self.assertIsNone(P.export_checkout(folder))
        linked = os.path.join(self.k.base, 'linked')
        os.makedirs(linked)
        with open(os.path.join(linked, '.git'), 'w', encoding='utf-8') as fh:
            fh.write('gitdir: elsewhere\n')
        for marker in ({'sha': 'x'}, {'checkout': 7}, {'checkout': ''}, {'checkout': linked},
                       {'checkout': os.path.join(self.k.base, 'gone')}):
            with open(os.path.join(folder, P.MARKER), 'w', encoding='utf-8') as fh:
                json.dump(marker, fh)
            self.assertIsNone(P.export_checkout(folder), marker)
        with open(os.path.join(folder, P.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'checkout': self.k.kit}, fh)
        self.assertEqual(P.export_checkout(folder), self.k.kit)

    def test_stamp_refuses_other_than_one_version(self):
        for body in (b'{"name": "x"}', b'{"version": "1", "x": {"version": "2"}}'):
            with self.assertRaises(P.RefreshError):
                P.stamp(body, '1-main.2')


class MarketplaceTest(Base):
    def test_names_the_new_folder_and_same_sha_twice_is_a_no_op(self):
        folders = self.k.folders('.claude', '.claude-two')
        self.k.seed_migrated(folders)
        proc = FakeProc()
        self.assertEqual(self.refresh(folders, proc), 0)
        sha = self.remote_sha()
        m = read_json(P.marketplace_path(self.k.root))
        self.assertEqual((m['name'], m['plugins'][0]['name'], m['plugins'][0]['source']),
                         ('pm-kit', 'pm-kit', './' + sha[:12]))
        with open(P.marketplace_path(self.k.root), 'rb') as fh:
            self.assertFalse(fh.read().startswith(b'\xef\xbb\xbf'))
        self.assertEqual(len(proc.calls), 4)
        again = FakeProc()
        self.lines = []
        self.assertEqual(self.refresh(folders, again), 0)
        self.assertEqual(again.calls, [])
        self.assertIn('nothing to do', '\n'.join(self.lines))

    def test_dry_run_writes_nothing(self):
        folders = self.k.folders('.claude')
        proc = FakeProc()
        self.assertEqual(self.refresh(folders, proc, apply=False, migrate=True), 0)
        self.assertEqual(self.refresh(folders, proc, apply=False), 1)      # not migrated: refused
        self.assertFalse(os.path.exists(self.k.root))
        self.assertEqual(proc.calls, [])
        self.assertIn('DRY RUN', '\n'.join(self.lines))


class FolderRunTest(Base):
    def test_each_folder_updates_in_order_with_its_config_dir(self):
        default, two = self.k.folders('.claude', '.claude-two')
        self.k.seed_migrated([default, two])
        proc = FakeProc()
        self.assertEqual(self.refresh([default, two], proc), 0)
        self.assertEqual(proc.calls, [
            (['plugin', 'marketplace', 'update', 'pm-kit'], None),
            (['plugin', 'update', 'pm-kit@pm-kit'], None),
            (['plugin', 'marketplace', 'update', 'pm-kit'], two),
            (['plugin', 'update', 'pm-kit@pm-kit'], two)])

    def test_a_failed_folder_is_recorded_the_others_run_and_only_it_reruns(self):
        default, two, three = self.k.folders('.claude', '.claude-two', '.claude-three')
        self.k.seed_migrated([default, two, three])
        proc = FakeProc(fail={('.claude-two', 'marketplace'): 1})
        self.assertEqual(self.refresh([default, two, three], proc), 1)
        self.assertEqual([c[1] for c in proc.calls], [None, None, two, three, three])
        state = P.read_state(self.k.root)
        self.assertEqual({f: r['rc'] for f, r in state['folders'].items()},
                         {default: 0, two: 1, three: 0})
        self.assertIn('FAILED %s' % two, '\n'.join(self.lines))
        again = FakeProc()
        self.assertEqual(self.refresh([default, two, three], again), 0)
        self.assertEqual([c[1] for c in again.calls], [two, two])

    def test_claude_missing_is_refused_after_the_export(self):
        folders = self.k.folders('.claude')
        self.k.seed_migrated(folders)
        self.assertEqual(self.refresh(folders, claude_path=''), 1)
        sha = self.remote_sha()
        self.assertTrue(os.path.isdir(os.path.join(self.k.root, sha[:12])))
        self.assertIn('claude is not on PATH', '\n'.join(self.lines))

    def test_migrate_removes_adds_and_installs_and_lists_old_caches(self):
        default, two = self.k.folders('.claude', '.claude-two')
        proc = FakeProc(fail={('<default>', 'marketplace'): 0})
        self.assertEqual(self.refresh([default, two], proc, migrate=True), 0)
        self.assertEqual(proc.calls[:3], [
            (['plugin', 'marketplace', 'remove', 'pm-kit'], None),
            (['plugin', 'marketplace', 'add', self.k.root], None),
            (['plugin', 'install', 'pm-kit@pm-kit', '-s', 'user'], None)])
        text = '\n'.join(self.lines)
        for f in (default, two):
            self.assertIn(os.path.join(f, 'plugins', 'cache', 'pm-kit', 'pm-kit', '0.1.0'), text)
        self.assertFalse(os.path.exists(os.path.join(default, 'plugins')))     # named, not made

    def test_migrate_goes_on_when_the_old_marketplace_is_not_there(self):
        (two,) = self.k.folders('.claude-two')
        proc = FakeProc(fail={('.claude-two', 'marketplace'): 1})    # remove and add both "fail"
        self.assertEqual(self.refresh([two], proc, migrate=True), 1)
        self.assertEqual([c[0][2] for c in proc.calls], ['remove', 'add'])
        self.assertEqual(P.read_state(self.k.root)['folders'][two]['step'],
                         'plugin marketplace add %s' % self.k.root)


class CachePruneAfterRefreshTest(Base):
    """A refresh that went through prunes each account's old plugin cache versions, every guard of
    prune_caches kept (the installed one, the newest, one replaced in the last day, one in use)."""

    def account(self, versions, installed, name='.claude', seed=True):
        (f,) = self.k.folders(name)
        cache = P.cache_dir(f)
        old = time.time() - 30 * 86400
        for i, v in enumerate(versions):
            os.makedirs(os.path.join(cache, v, 'tools'))
            os.utime(os.path.join(cache, v), (old + i * 60, old + i * 60))
        with open(os.path.join(f, 'plugins', 'installed_plugins.json'), 'w', encoding='utf-8') as fh:
            json.dump({'version': 2, 'plugins': {P.PLUGIN_ID: [
                {'scope': 'user', 'installPath': os.path.join(cache, installed)}]}}, fh)
        if seed:
            self.k.seed_migrated([f])
        return f, cache

    def reads(self, texts=()):
        return lambda: P.InUse([t.lower() for t in texts], set(), [])

    def test_apply_removes_the_unused_old_versions(self):
        f, cache = self.account(['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3', '0.1.0-main.4'],
                                installed='0.1.0-main.3')
        named = 'node %s' % os.path.join(cache, '0.1.0-main.2', 'x.js')
        self.assertEqual(self.refresh([f], in_use=self.reads([named])), 0, self.lines)
        self.assertEqual(sorted(os.listdir(cache)), ['0.1.0-main.2', '0.1.0-main.3', '0.1.0-main.4'])
        self.assertIn('pruned cache %s' % os.path.join(cache, '0.1.0-main.1'), self.lines)

    def test_a_refused_folder_keeps_its_old_versions_while_the_updated_one_is_pruned(self):
        versions = ['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3']
        a, a_cache = self.account(versions, installed='0.1.0-main.3')
        b, b_cache = self.account(versions, installed='0.1.0-main.3', name='.claude-b', seed=False)
        self.assertEqual(self.refresh([a, b], in_use=self.reads()), 1, self.lines)   # b refused
        self.assertIn('REFUSED %s: not migrated to the export yet' % b, '\n'.join(self.lines))
        self.assertEqual(sorted(os.listdir(a_cache)), ['0.1.0-main.3'])    # installed and newest
        self.assertEqual(sorted(os.listdir(b_cache)), versions)
        self.assertFalse([l for l in self.lines if l.startswith('pruned cache') and b_cache in l])

    def test_nothing_is_pruned_when_a_folder_failed_on_a_migrate_or_unread(self):
        f, cache = self.account(['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3'],
                                installed='0.1.0-main.3')
        self.assertEqual(self.refresh([f], FakeProc(fail={('<default>', 'update'): 1}),
                                      in_use=self.reads()), 1)
        self.assertEqual(len(os.listdir(cache)), 3)
        self.assertEqual(self.refresh([f], migrate=True, in_use=self.reads()), 0, self.lines)
        self.assertEqual(len(os.listdir(cache)), 3)
        self.assertEqual(self.refresh([f]), 0, self.lines)              # in use not read: all kept
        self.assertEqual(len(os.listdir(cache)), 3)
        self.assertFalse([l for l in self.lines if l.startswith('pruned cache')])


class PruneTest(Base):
    def make(self, name, at, marker=True):
        p = os.path.join(self.k.root, name)
        os.makedirs(p)
        if marker:
            with open(os.path.join(p, P.MARKER), 'w', encoding='utf-8') as fh:
                json.dump({'sha': name + '0' * 28, 'exported_at': at}, fh)
        return p

    def test_keeps_the_three_newest_and_the_named_one_and_no_unmarked_folder(self):
        names = ['%012x' % i for i in range(1, 7)]
        for i, n in enumerate(names):
            self.make(n, '2026-10-0%dT00:00:00' % (i + 1))
        stranger = self.make('abcdefabcdef', '', marker=False)
        other = os.path.join(self.k.root, 'notes')
        os.makedirs(other)
        P.write_marketplace(self.k.root, names[0])          # the oldest is the one installed
        removed, failed = P.prune(self.k.root, now=NOW)
        self.assertEqual(failed, [])
        self.assertEqual(sorted(os.path.basename(r) for r in removed), names[1:3])
        left = sorted(os.listdir(self.k.root))
        for n in [names[0]] + names[3:]:
            self.assertIn(n, left)
        self.assertTrue(os.path.isdir(stranger) and os.path.isdir(other))


class RefreshLockTest(Base):
    def test_a_live_holder_refuses_a_second_and_a_stale_one_is_taken_over(self):
        os.makedirs(self.k.root)
        first = P.RefreshLock(self.k.root, own=(1, 10), alive=lambda pid, ct: True)
        first.acquire()
        with self.assertRaises(P.Busy):
            P.RefreshLock(self.k.root, own=(2, 20), alive=lambda pid, ct: True).acquire()
        self.assertTrue(P.lock_live(self.k.root, alive=lambda pid, ct: True))
        self.assertEqual(P.refresh(self.k.kit, self.s, [], self.out, apply=True, own=(3, 30),
                                   alive=lambda pid, ct: True), 1)
        self.assertIn('already running', '\n'.join(self.lines))
        # dead pid, or a reused pid whose start time differs: stale, taken over
        for alive in (never_alive, lambda pid, ct: ct != 10):
            second = P.RefreshLock(self.k.root, own=(2, 20), alive=alive)
            second.acquire()
            self.assertTrue(second.held)
            second.release()
            self.assertFalse(os.path.exists(os.path.join(self.k.root, P.PIDFILE)))
            first.acquire()
        first.release()
        self.assertFalse(os.path.exists(os.path.join(self.k.root, P.PIDFILE)))

    def test_release_leaves_a_file_another_run_took_over(self):
        os.makedirs(self.k.root)
        a = P.RefreshLock(self.k.root, own=(1, 10), alive=never_alive)
        a.acquire()
        P.RefreshLock(self.k.root, own=(2, 20), alive=never_alive).acquire()
        a.release()
        self.assertEqual(read_json(os.path.join(self.k.root, P.PIDFILE))['pid'], 2)


class SettingsTest(unittest.TestCase):
    def test_defaults_and_checks(self):
        s = P.settings(None, environ={'LOCALAPPDATA': r'C:\lad'})
        self.assertEqual(s, P.Settings(os.path.normpath(r'C:\lad\pm-kit\plugin'), False, 'origin'))
        with self.assertRaises(P.RefreshError):
            P.settings(None, environ={})
        self.assertEqual(P.settings({'export_dir': r'D:\x', 'auto_refresh': True}, environ={}).auto_refresh, True)
        for bad in ({'export_dir': 'rel'}, {'auto_refresh': 'yes'}, {'remote': '-x'}, {'nope': 1}, []):
            self.assertTrue(P.check(bad), bad)


class DefaultExportDirTest(unittest.TestCase):
    """The export root with no plugin.export_dir, per platform (kitlib.platform.NAME patched; the
    Linux profile from the password database seam kitlib.platform.linux.paths._pw_dir)."""
    LINUX = '/var/lib/kit/.local/share/pm-kit/plugin'

    def on(self, name, pw_dir=None):
        def no_pw():
            raise AssertionError('the password database was read')
        self.enterContext(mock.patch('kitlib.platform.NAME', name))
        self.enterContext(mock.patch('kitlib.platform.linux.paths._pw_dir', pw_dir or no_pw))

    def test_linux_is_under_the_profile_from_the_password_database(self):
        self.on('linux', lambda: '/var/lib/kit')
        s = P.settings(None, environ={})
        self.assertEqual(s, P.Settings(os.path.normpath(self.LINUX), False, 'origin'))
        self.assertEqual(P.default_export_dir({}), self.LINUX)

    def test_linux_reads_no_environment(self):
        self.on('linux', lambda: '/var/lib/kit')
        env = {'XDG_DATA_HOME': '/elsewhere/data', 'HOME': '/elsewhere/home',
               'LOCALAPPDATA': os.path.join(os.path.abspath(os.sep), 'lad')}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(P.settings(None).export_dir, os.path.normpath(self.LINUX))
            self.assertEqual(P.default_export_dir(), self.LINUX)
        self.assertEqual(P.default_export_dir(env), self.LINUX)

    def test_linux_without_a_profile_names_the_config_key(self):
        def gone():
            raise KeyError('uid 4242')
        self.on('linux', gone)
        with self.assertRaisesRegex(P.RefreshError, 'profile folder .*plugin.export_dir'):
            P.settings(None, environ={})

    def test_linux_without_a_password_database_module_is_a_refresh_error(self):
        # Linux patched in on Windows: no pwd module. A RefreshError, which a caller such as the
        # guard's advice_kit catches, never a bare ImportError
        def no_module():
            raise ModuleNotFoundError("No module named 'pwd'")
        self.on('linux', no_module)
        with self.assertRaisesRegex(P.RefreshError, 'profile folder .*plugin.export_dir'):
            P.settings(None, environ={'LOCALAPPDATA': os.path.join(os.path.abspath(os.sep), 'lad')})

    def test_an_explicit_export_dir_wins_on_either_platform(self):
        mine = os.path.join(os.path.abspath(os.sep), 'srv', 'pm-export')
        for name in ('linux', 'windows'):
            with self.subTest(platform=name), mock.patch('kitlib.platform.NAME', name):
                with mock.patch('kitlib.platform.linux.paths._pw_dir', lambda: '/var/lib/kit'):
                    got = P.settings({'export_dir': mine}, environ={'LOCALAPPDATA': r'C:\lad'})
                self.assertEqual(got.export_dir, os.path.normpath(mine))

    def test_windows_is_unchanged(self):
        self.on('windows')
        lad = os.path.join(os.path.abspath(os.sep), 'lad')
        env = {'LOCALAPPDATA': lad, 'XDG_DATA_HOME': '/x'}
        self.assertEqual(P.settings(None, environ=env).export_dir,
                         os.path.normpath(os.path.join(lad, 'pm-kit', 'plugin')))
        with self.assertRaisesRegex(P.RefreshError, 'LOCALAPPDATA is not set'):
            P.settings(None, environ={'XDG_DATA_HOME': '/x'})
        self.assertEqual(P.default_export_dir(env), os.path.join(lad, 'pm-kit', 'plugin'))


class CurrentLinesTest(Base):
    def test_no_state_behind_failed_and_current(self):
        default, two = self.k.folders('.claude', '.claude-two')
        both = [default, two]
        lines = P.current_lines(self.k.kit, self.s, folders=both)
        self.assertEqual(len(lines), 1)
        self.assertIn('no export', lines[0])
        self.assertTrue(lines[0].endswith(P.refresh_cmd(True)), lines[0])
        self.k.seed_migrated(both)
        self.refresh(both, FakeProc(fail={('.claude-two', 'update'): 2}))
        lines = P.current_lines(self.k.kit, self.s, folders=both)
        self.assertEqual(len(lines), 1)
        self.assertIn('refresh failed in %s' % two, lines[0])
        self.assertIn(P.refresh_cmd(), lines[0])
        self.refresh(both)
        self.assertEqual(P.current_lines(self.k.kit, self.s, folders=both), [])
        later = self.advance()
        lines = P.current_lines(self.k.kit, self.s, folders=both)
        self.assertEqual(len(lines), 1)
        self.assertIn('behind kit main', lines[0])
        self.assertIn(later[:12], lines[0])
        self.assertTrue(lines[0].endswith(P.refresh_cmd()), lines[0])

    def test_a_folder_not_migrated_names_the_migrate_command(self):
        default, two = self.k.folders('.claude', '.claude-two')
        self.k.seed_migrated([default])
        self.assertEqual(self.refresh([default]), 0)
        lines = P.current_lines(self.k.kit, self.s, folders=[default, two])
        self.assertEqual(len(lines), 1)
        self.assertIn('not migrated to the export in %s' % two, lines[0])
        self.assertIn('%s (one folder first: --folder DIR)' % P.refresh_cmd(True), lines[0])
        self.advance()
        lines = P.current_lines(self.k.kit, self.s, folders=[default, two])
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith(P.refresh_cmd(True)), lines[1])
        self.assertEqual(P.current_lines(self.k.kit, self.s, folders=[default])[0][-len(P.refresh_cmd()):],
                         P.refresh_cmd())

    def advance(self):
        r = self.k.repo
        r.write('later.txt', 'z\n')
        r.commit('later', 'later.txt')
        r.git('push', '-q', 'origin', 'main')
        return r.head()

    def test_a_migrated_folder_not_yet_refreshed_is_not_a_failure(self):
        (two,) = self.k.folders('.claude-two')
        self.k.seed_migrated([two])
        _root, sha = P.ensure_export(self.k.kit, self.s, now=NOW, own=ME, alive=never_alive)
        lines = P.current_lines(self.k.kit, self.s, folders=[two])
        self.assertEqual(lines, ['pm-kit plugin in %s: not refreshed to %s: %s'
                                 % (two, sha[:12], P.refresh_cmd())])

    def test_unreadable_state(self):
        os.makedirs(self.k.root)
        with open(os.path.join(self.k.root, P.STATE), 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        lines = P.current_lines(self.k.kit, self.s, folders=[])
        self.assertEqual(len(lines), 1)
        self.assertIn('could not check the pm-kit plugin', lines[0])


class MigratedTest(Base):
    def test_apply_refuses_a_folder_never_migrated_and_runs_the_others(self):
        default, two = self.k.folders('.claude', '.claude-two')
        self.k.seed_migrated([default])
        proc = FakeProc()
        self.assertEqual(self.refresh([default, two], proc, apply=False), 1)
        self.assertEqual(proc.calls, [])
        self.assertIn('REFUSED %s: not migrated to the export yet: %s --folder "%s"'
                      % (two, P.refresh_cmd(True), two), self.lines)
        self.assertEqual(self.refresh([default, two], proc), 1)
        self.assertEqual([c[1] for c in proc.calls], [None, None])
        state = P.read_state(self.k.root)
        self.assertEqual(sorted(state['folders']), [default])
        self.assertTrue(os.path.isdir(os.path.join(self.k.root, self.remote_sha()[:12])))

    def test_migrate_records_the_flag_and_a_failed_migrate_does_not(self):
        two, three = self.k.folders('.claude-two', '.claude-three')
        proc = FakeProc(fail={('.claude-three', 'install'): 1})
        self.assertEqual(self.refresh([two, three], proc, migrate=True), 1)
        state = P.read_state(self.k.root)
        self.assertIs(state['folders'][two]['migrated'], True)
        self.assertIs(state['folders'][three]['migrated'], False)
        self.lines = []
        again = FakeProc()
        self.assertEqual(self.refresh([two, three], again), 1)     # three refused, two current
        self.assertEqual(again.calls, [])
        self.assertIn('--folder "%s"' % three, '\n'.join(self.lines))
        self.assertEqual(self.refresh([two], again), 0)

    def test_a_new_export_keeps_each_folders_migrated_flag(self):
        (two,) = self.k.folders('.claude-two')
        self.assertEqual(self.refresh([two], migrate=True), 0)
        r = self.k.repo
        r.write('later.txt', 'z\n')
        r.commit('later', 'later.txt')
        r.git('push', '-q', 'origin', 'main')
        proc = FakeProc()
        self.assertEqual(self.refresh([two], proc), 0)
        self.assertEqual([c[0][1] for c in proc.calls], ['marketplace', 'update'])
        state = P.read_state(self.k.root)
        self.assertEqual(state['sha'], r.head())
        self.assertEqual((state['folders'][two]['migrated'], state['folders'][two]['rc']), (True, 0))

    def test_pick_folders(self):
        a, b = self.k.folders('.claude', '.claude-two')
        self.assertEqual(P.pick_folders([a, b], []), ([a, b], []))
        self.assertEqual(P.pick_folders([a, b], [b.upper(), b + os.sep, 'X:\\nope']), ([b], ['X:\\nope']))


class ExportStepsTest(Base):
    def test_ensure_export_makes_the_export_once_and_keeps_the_flags(self):
        (two,) = self.k.folders('.claude-two')
        self.k.seed_migrated([two])
        root, sha = P.ensure_export(self.k.kit, self.s, now=NOW, own=ME, alive=never_alive)
        self.assertEqual((root, sha), (self.k.root, self.remote_sha()))
        self.assertEqual(P.current_folder(root), sha[:12])
        state = P.read_state(root)
        self.assertEqual((state['sha'], state['folders']), (sha, {two: {'migrated': True}}))
        before = os.path.getmtime(os.path.join(root, P.STATE))
        self.assertEqual(P.ensure_export(self.k.kit, self.s, now=NOW, own=ME, alive=never_alive),
                         (root, sha))
        self.assertEqual(os.path.getmtime(os.path.join(root, P.STATE)), before)
        self.assertFalse(os.path.exists(os.path.join(root, P.PIDFILE)))

    def test_ensure_export_refuses_an_unreadable_state_and_writes_nothing(self):
        os.makedirs(self.k.root)
        path = os.path.join(self.k.root, P.STATE)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{"folders": {"x": {"migrated": true}')
        with self.assertRaises(P.StateError):
            P.ensure_export(self.k.kit, self.s, now=NOW, own=ME, alive=never_alive)
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), '{"folders": {"x": {"migrated": true}')
        self.assertEqual(sorted(os.listdir(self.k.root)), [P.STATE])

    def test_a_leftover_tmp_folder_is_replaced_by_a_whole_export(self):
        sha = self.remote_sha()
        tmp = os.path.join(self.k.root, sha[:12] + '.tmp')
        os.makedirs(os.path.join(tmp, 'junk'))
        with open(os.path.join(tmp, 'junk', 'half.txt'), 'w', encoding='utf-8') as fh:
            fh.write('half\n')
        folder, _v = P.export(self.k.kit, sha, self.k.root, NOW)
        self.assertFalse(os.path.exists(tmp))
        self.assertFalse(os.path.exists(os.path.join(folder, 'junk')))
        self.assertTrue(os.path.isfile(os.path.join(folder, 'tools', 'x.py')))

    def test_an_export_failing_partway_leaves_no_final_folder_and_a_retry_succeeds(self):
        folders = self.k.folders('.claude')
        self.k.seed_migrated(folders)
        sha = self.remote_sha()
        final = os.path.join(self.k.root, sha[:12])
        proc = FakeProc()
        with mock.patch.object(P, 'stamp', side_effect=P.RefreshError('boom')):
            self.assertEqual(self.refresh(folders, proc), 1)
        self.assertIn('REFUSED: the export failed: boom', self.lines)
        self.assertFalse(os.path.exists(final))
        self.assertEqual(proc.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.k.root, P.PIDFILE)))
        with mock.patch.object(P, 'write_marketplace', side_effect=OSError('disk full')):
            self.assertEqual(self.refresh(folders, proc), 1)
        self.assertIn('REFUSED: the marketplace write failed: disk full', self.lines)
        self.assertEqual(proc.calls, [])
        self.assertEqual(self.refresh(folders, proc), 0)
        self.assertTrue(os.path.isdir(final))
        self.assertFalse(os.path.exists(final + '.tmp'))
        self.assertEqual(P.current_folder(self.k.root), sha[:12])


class PruneRulesTest(Base):
    make = PruneTest.make

    def test_an_old_but_young_export_survives(self):
        names = ['%012x' % i for i in range(1, 8)]
        dates = ['2026-09-01T00:00:00', '2026-09-02T00:00:00', '2026-09-03T00:00:00',
                 '2026-10-06T00:00:00', '2026-10-07T00:00:00', '2026-10-08T00:00:00', 'not a date']
        for n, at in zip(names, dates):
            self.make(n, at)
        P.write_marketplace(self.k.root, names[0])
        removed, failed = P.prune(self.k.root, now=NOW)
        self.assertEqual(failed, [])
        self.assertEqual([os.path.basename(r) for r in removed], [names[1]])   # replaced 09-03
        self.assertTrue(os.path.isdir(os.path.join(self.k.root, names[2])))   # replaced 10-06, not in the 3 newest
        self.assertTrue(os.path.isdir(os.path.join(self.k.root, names[6])))   # date unreadable

    def test_an_old_export_current_until_recently_survives(self):
        # 001 was current from 08-01 to 09-01 and is long replaced; 002 was current for weeks,
        # until three quick landings on 10-09, 10-10 and 10-11 replaced it two days ago
        names = ['%012x' % i for i in range(1, 6)]
        dates = ['2026-08-01T00:00:00', '2026-09-01T00:00:00', '2026-10-09T00:00:00',
                 '2026-10-10T00:00:00', '2026-10-11T00:00:00']
        for n, at in zip(names, dates):
            self.make(n, at)
        P.write_marketplace(self.k.root, names[4])
        removed, failed = P.prune(self.k.root, now=NOW)
        self.assertEqual(failed, [])
        self.assertEqual([os.path.basename(r) for r in removed], [names[0]])
        self.assertTrue(os.path.isdir(os.path.join(self.k.root, names[1])))
        P.write_marketplace(self.k.root, names[1])        # the current one is kept however old
        removed, _f = P.prune(self.k.root, now=NOW + datetime.timedelta(days=30))
        self.assertEqual(sorted(os.listdir(self.k.root)), sorted(['.claude-plugin'] + names[1:]))

    def test_stale_tmp_folders_go_and_the_current_ones_stay(self):
        cur = self.make('%012x' % 9, '2026-09-01T00:00:00')
        P.write_marketplace(self.k.root, os.path.basename(cur))
        stale = os.path.join(self.k.root, 'a' * 12 + '.tmp')
        mine = cur + '.tmp'
        os.makedirs(os.path.join(stale, 'x'))
        os.makedirs(mine)
        removed, failed = P.prune(self.k.root, now=NOW)
        self.assertEqual((removed, failed), ([stale], []))
        self.assertTrue(os.path.isdir(mine))

    def test_a_junction_is_never_a_candidate(self):
        import _winapi
        target = os.path.join(self.k.base, 'elsewhere', '%012x' % 5)
        os.makedirs(target)
        with open(os.path.join(target, P.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': 'f' * 40, 'exported_at': '2020-01-01T00:00:00'}, fh)
        os.makedirs(self.k.root)
        link = os.path.join(self.k.root, '%012x' % 5)
        _winapi.CreateJunction(target, link)
        os.makedirs(os.path.join(self.k.base, 'elsewhere', 'b' * 12))
        _winapi.CreateJunction(os.path.join(self.k.base, 'elsewhere', 'b' * 12),
                               os.path.join(self.k.root, 'b' * 12 + '.tmp'))
        removed, failed = P.prune(self.k.root, keep=0, now=NOW)
        self.assertEqual((removed, failed), ([], []))
        self.assertTrue(os.path.isfile(os.path.join(target, P.MARKER)))
        self.assertTrue(os.path.lexists(link))


class LockGraceTest(Base):
    def test_an_empty_pid_file_is_live_until_the_grace_passes(self):
        os.makedirs(self.k.root)
        path = os.path.join(self.k.root, P.PIDFILE)
        open(path, 'w').close()
        with self.assertRaises(P.Busy) as cm:
            P.RefreshLock(self.k.root, own=(2, 20), alive=never_alive).acquire()
        self.assertIn('its pid not yet written', str(cm.exception))
        self.assertTrue(P.lock_live(self.k.root, alive=never_alive))
        old = os.path.getmtime(path) - P.LOCK_GRACE_S - 5
        os.utime(path, (old, old))
        self.assertFalse(P.lock_live(self.k.root, alive=never_alive))
        lock = P.RefreshLock(self.k.root, own=(2, 20), alive=never_alive)
        lock.acquire()
        self.assertEqual(read_json(path), {'pid': 2, 'ctime': 20})
        lock.release()


class SafePathTest(unittest.TestCase):
    def test_safe_rel(self):
        for good in ('a/b.txt', 'CONFIG.md', 'console/x.py', 'a/com10.txt', 'nullable.txt'):
            self.assertTrue(P._safe_rel(good), good)
        for bad in ('a\\b.txt', 'CON', 'con.txt', 'a/nul', 'a/Com1.json', 'LPT9.x.y', 'aux ',
                    'a/b.', 'a/b ', '../x', 'a//b', 'c:x', '/abs'):
            self.assertFalse(P._safe_rel(bad), bad)


class PlacementTest(unittest.TestCase):
    def test_an_export_root_inside_the_kit_or_the_sweep_root_is_refused(self):
        kit, sweep = r'C:\work\kit', r'D:\scratch'
        self.assertTrue(P.check({'export_dir': r'C:\work\kit\export'}, kit=kit))
        self.assertTrue(P.check({'export_dir': r'D:\Scratch\pm'}, sweep_dir=sweep))
        self.assertEqual(P.check({'export_dir': r'C:\work\kit2'}, kit=kit, sweep_dir=sweep), [])
        with self.assertRaises(P.RefreshError):
            P.settings({'export_dir': kit}, kit=kit)
        with self.assertRaises(P.RefreshError):
            P.settings(None, environ={'LOCALAPPDATA': r'D:\scratch\lad'}, sweep_dir=sweep)
        self.assertEqual(P.settings({'export_dir': r'E:\pm'}, kit=kit, sweep_dir=sweep).export_dir, r'E:\pm')


class AccountFoldersTest(unittest.TestCase):
    def test_default_and_dash_folders_each_once(self):
        k = Kit(self)
        a, b = k.folders('.claude', '.claude-two')
        self.assertEqual(claude.account_folders(k.home), [a, b])
        self.assertTrue(claude.is_default(a, k.home))
        self.assertFalse(claude.is_default(b, k.home))
        env = claude.env_for(a, k.home, {'CLAUDE_CONFIG_DIR': b, 'X': '1'})
        self.assertNotIn('CLAUDE_CONFIG_DIR', env)
        self.assertEqual(claude.env_for(b, k.home, {})['CLAUDE_CONFIG_DIR'], b)


if __name__ == '__main__':
    unittest.main()
