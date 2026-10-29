"""Codex from the plugin export (docs/plugin-source.md "Codex"): the export places Codex's manifest
at .codex-plugin/plugin.json, stamped; write_marketplace writes Codex's marketplace beside Claude's;
hosts.codex.adapter.plugin_install installs the export by `codex plugin add` (a stand-in runs it);
and refresh's prune keeps the export Codex's cache was installed from. Temp folders and a fake
codex.exe only: no real ~/.codex is read and no codex runs."""
import datetime
import json
import os
import subprocess
import tempfile
import unittest

from hosts.codex.adapter import plugin_copy as codex
from hosts.codex.adapter import plugin_install as pi
from kitlib import pluginsource as P
from kitlib.pluginsource import claude
from tests import gitfixture
from tests.pluginsource import FakeProc, Kit
from tests.pluginsource.test_codex import link

NOW = datetime.datetime(2026, 10, 11, 9, 0)
CODEX_MANIFEST = '{\n  "name": "pm-kit",\n  "version": "0.1.0",\n  "skills": "./content/skills/"\n}\n'
HOOKS = '{"hooks": {}}\n'


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.k = Kit(self)
        self.s = P.Settings(self.k.root, False, 'origin')
        self.lines = []

    def push(self, files):
        r = self.k.repo
        for rel, text in files.items():
            r.write(rel, text)
        r.commit('codex files', *files)
        r.git('push', '-q', 'origin', 'main')
        return P.remote_main(self.k.kit, 'origin')

    def text(self):
        return '\n'.join(self.lines)


class ExportTest(Base):
    def test_the_codex_manifest_is_placed_at_the_root_and_stamped_like_claudes(self):
        sha = self.push({'hosts/codex/shell/plugin.json': CODEX_MANIFEST})
        folder, version = P.export(self.k.kit, sha, self.k.root, NOW)
        placed = read(os.path.join(folder, '.codex-plugin', 'plugin.json'))
        self.assertEqual(placed, CODEX_MANIFEST.replace('"0.1.0"', '"%s"' % version))
        with open(os.path.join(folder, '.claude-plugin', 'plugin.json'), encoding='utf-8-sig') as fh:
            self.assertEqual(json.load(fh)['version'], version)
        self.assertEqual(read(os.path.join(folder, 'hosts', 'codex', 'shell', 'plugin.json')), CODEX_MANIFEST)
        self.assertFalse(os.path.exists(self.k.repo.path('.codex-plugin')))
        self.assertEqual(pi.manifest_version(folder), version)

    def test_a_tree_without_the_codex_manifest_exports_without_one(self):
        sha = P.remote_main(self.k.kit, 'origin')
        folder, _v = P.export(self.k.kit, sha, self.k.root, NOW)
        self.assertFalse(os.path.exists(os.path.join(folder, '.codex-plugin')))
        self.assertIsNone(pi.manifest_version(folder))

    def test_a_codex_manifest_without_one_version_refuses_the_export(self):
        sha = self.push({'hosts/codex/shell/plugin.json': '{"name": "pm-kit", "version": "1", "x": {"version": "2"}}\n'})
        with self.assertRaises(P.RefreshError) as cm:
            P.export(self.k.kit, sha, self.k.root, NOW)
        self.assertIn('.codex-plugin/plugin.json holds 2 "version" keys', str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.k.root, sha[:12])))


class HostManifestsTest(unittest.TestCase):
    def test_a_hosts_shell_manifest_goes_where_that_host_looks_unless_the_tree_has_it(self):
        oids = {'hosts/codex/shell/plugin.json': 'c1', 'hosts/claude/shell/plugin.json': 'k1',
                '.claude-plugin/plugin.json': 'k0', 'hosts/codex/shell/mcp.json': 'm1',
                'hosts/codex/shell/sub/plugin.json': 's1'}
        self.assertEqual(P.host_manifests(oids), {'.codex-plugin/plugin.json': 'c1'})


class FakeStep(object):
    label = 'fakehost'

    def __init__(self, names=(), fail=None):
        self.names, self.fail = set(names), fail

    def __call__(self, export, out, apply=False):
        pass

    def in_use(self):
        if self.fail:
            raise self.fail
        return self.names


class InUseTest(Base):
    def make(self, name, at):
        folder = os.path.join(self.k.root, name)
        os.makedirs(folder)
        with open(os.path.join(folder, P.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': name + '0' * 28, 'exported_at': at}, fh)

    def exports(self):
        names = ['%012x' % i for i in range(1, 6)]
        for i, n in enumerate(names):
            self.make(n, '2026-0%d-01T00:00:00' % (i + 1))
        P.write_marketplace(self.k.root, names[-1])
        return names

    def whole(self):
        return P.InUse(['c:\\x\\python.exe'], set(), [])

    def test_an_export_a_host_copy_names_is_kept(self):
        names = self.exports()
        removed, failed = P.prune(self.k.root, now=NOW, out=self.lines.append,
                                  in_use=P.with_hosts(self.whole, [FakeStep([names[0]])]))
        self.assertEqual((failed, [os.path.basename(r) for r in removed]), ([], [names[1]]))
        self.assertIn('host plugin copy names it: %s' % names[0], self.text())

    def test_without_the_host_the_same_export_goes(self):
        names = self.exports()
        removed, _f = P.prune(self.k.root, now=NOW, in_use=P.with_hosts(self.whole, [lambda f, o, a: None]))
        self.assertEqual(sorted(os.path.basename(r) for r in removed), names[:2])

    def test_a_host_read_that_fails_keeps_every_young_export(self):
        names = self.exports()
        reader = P.with_hosts(self.whole, [FakeStep(fail=OSError('denied'))])
        u = reader()
        self.assertEqual(u.errors, ['the fakehost plugin copy could not be read (OSError: denied)'])
        march = datetime.datetime(2026, 3, 5)       # the second export was replaced 4 days before
        removed, _f = P.prune(self.k.root, now=march, out=self.lines.append, in_use=reader)
        self.assertEqual([os.path.basename(r) for r in removed], [names[0]])
        self.assertIn('could not be read whole', self.text())
        self.make('%012x' % 9, '2026-01-15T00:00:00')
        removed, _f = P.prune(self.k.root, now=march, in_use=P.with_hosts(self.whole, [FakeStep()]))
        self.assertEqual(sorted(os.path.basename(r) for r in removed), ['%012x' % 2, '%012x' % 9])

    def test_no_base_reader_still_reads_the_host_and_counts_as_partial(self):
        u = P.with_hosts(None, [FakeStep(['abcdef012345', 'not-a-sha'])])()
        self.assertEqual(u.named, {'abcdef012345'})
        self.assertTrue(u.errors)
        self.assertIsNone(P.with_hosts(None, [lambda f, o, a: None]))

    def test_a_base_reader_that_raises_is_an_error_not_a_lost_host(self):
        def broken():
            raise RuntimeError('boom')
        u = P.with_hosts(broken, [FakeStep(['abcdef012345'])])()
        self.assertEqual(u.named, {'abcdef012345'})
        self.assertEqual(u.errors, ['RuntimeError: boom'])


class Done(object):
    def __init__(self, rc=0, out=b'', err=b''):
        self.returncode, self.stdout, self.stderr = rc, out, err


class InstallCase(unittest.TestCase):
    """A Codex home whose config.toml names the marketplace pm-kit at an export root, an export
    folder at version 1.0.0-main.2, and a fake codex.exe (never run: `run` stands in)."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-codex-install-')
        self.addCleanup(gitfixture.rmtree, self.base)
        self.home = os.path.join(self.base, 'codex-home')
        self.root = os.path.join(self.base, 'plugin')
        self.export = os.path.join(self.root, 'bbbbbbbbbbbb')
        write(os.path.join(self.export, '.codex-plugin', 'plugin.json'), '{"name": "pm-kit", "version": "1.0.0-main.2"}\n')
        write(os.path.join(self.export, *pi.HOOKS), HOOKS)
        write(os.path.join(self.export, P.MARKER), '{"sha": "%s"}\n' % ('b' * 40))
        self.exe = os.path.join(self.base, 'bin', 'codex.exe')
        write(self.exe, 'not a program\n')
        self.env = {'PM_KIT_CODEX_EXE': self.exe, 'PATH': ''}
        self.config('\\\\?\\' + self.root)
        self.lines, self.calls = [], []

    def config(self, source, extra=''):
        write(os.path.join(self.home, 'config.toml'),
              "[marketplaces.pm-kit]\nsource_type = \"local\"\nsource = '%s'\n\n"
              "[plugins.\"pm-kit@pm-kit\"]\nenabled = true\n%s" % (source, extra))

    def cache(self, version, sha='a' * 40, hooks=HOOKS):
        folder = os.path.join(self.home, 'plugins', 'cache', 'pm-kit', 'pm-kit', version)
        write(os.path.join(folder, P.MARKER), '{"sha": "%s"}\n' % sha)
        write(os.path.join(folder, *pi.HOOKS), hooks)
        return folder

    def installs(self, rc=0, version='1.0.0-main.2'):
        """A stand-in for codex plugin add: records the call; on rc 0 swaps the cache to `version`."""
        def run(argv, env):
            self.calls.append((argv, env.get('CODEX_HOME')))
            if rc == 0 and version:
                top = os.path.join(self.home, 'plugins', 'cache', 'pm-kit', 'pm-kit')
                for n in os.listdir(top) if os.path.isdir(top) else ():
                    gitfixture.rmtree(os.path.join(top, n))
                self.cache(version, 'b' * 40)
            return Done(rc, b'', b'error: no such marketplace' if rc else b'')
        return run

    def install(self, apply=True, run=None, env=None):
        pi.install(self.export, self.home, pi.read_config(self.home), self.lines.append, apply=apply,
                   environ=self.env if env is None else env, run=run or self.installs())

    def text(self):
        return '\n'.join(self.lines)


class InstallTest(InstallCase):
    def test_apply_runs_codex_plugin_add_with_the_codex_home(self):
        self.cache('1.0.0-main.1', hooks='{"hooks": {"old": 1}}\n')
        self.install()
        self.assertEqual(self.calls, [([os.path.realpath(self.exe), 'plugin', 'add', 'pm-kit@pm-kit'], self.home)])
        self.assertIn('codex: installed pm-kit 1.0.0-main.2 from %s (was 1.0.0-main.1)' % self.export, self.text())
        self.assertIn('the pm-kit hooks changed', self.text())

    def test_unchanged_hooks_ask_for_no_new_trust(self):
        self.cache('1.0.0-main.1')
        self.install()
        self.assertIn('installed pm-kit 1.0.0-main.2', self.text())
        self.assertNotIn('hooks changed', self.text())

    def test_a_cache_at_the_exports_version_is_current(self):
        self.cache('1.0.0-main.2')
        self.install()
        self.assertEqual(self.calls, [])
        self.assertIn('codex: pm-kit 1.0.0-main.2 is installed from the export: current', self.text())

    def test_a_dry_run_names_the_command_and_runs_nothing(self):
        self.cache('1.0.0-main.1')
        self.install(apply=False)
        self.assertEqual(self.calls, [])
        self.assertIn('codex: would run codex plugin add pm-kit@pm-kit (with CODEX_HOME=%s): the cache holds '
                      '1.0.0-main.1, the export is 1.0.0-main.2' % self.home, self.text())

    def test_a_marketplace_elsewhere_is_refused(self):
        self.config(os.path.join(self.base, 'elsewhere'))
        self.install()
        self.assertEqual(self.calls, [])
        self.assertIn('refused, nothing installed', self.text())
        self.assertIn('not the plugin export root %s' % self.root, self.text())

    def test_a_marketplace_with_no_local_source_is_refused(self):
        write(os.path.join(self.home, 'config.toml'), '[marketplaces.pm-kit]\nsource_type = "git"\nsource = "x"\n')
        self.install()
        self.assertEqual(self.calls, [])
        self.assertIn('gives [marketplaces.pm-kit] no local source folder', self.text())

    def test_an_export_without_the_codex_manifest_installs_nothing(self):
        os.remove(os.path.join(self.export, '.codex-plugin', 'plugin.json'))
        self.install()
        self.assertEqual(self.calls, [])
        self.assertIn('has no .codex-plugin/plugin.json with a version yet', self.text())

    def test_no_codex_exe_is_the_hand_command(self):
        self.install(env={'PATH': ''})
        self.assertEqual(self.calls, [])
        self.assertIn('codex: not installed (codex.exe is unavailable', self.text())
        self.assertIn('run by hand: codex plugin add pm-kit@pm-kit (with CODEX_HOME=%s)' % self.home, self.text())

    def test_a_failed_add_is_a_line_with_what_codex_said(self):
        self.install(run=self.installs(rc=1))
        self.assertIn('codex: FAILED: codex plugin add exited 1: error: no such marketplace', self.text())

    def test_an_add_that_leaves_another_version_is_a_failure(self):
        self.install(run=self.installs(version='0.0.1'))
        self.assertIn('codex: FAILED: codex plugin add exited 0 but the cache holds 0.0.1, not 1.0.0-main.2',
                      self.text())

    def test_an_add_that_times_out_is_a_line(self):
        def slow(argv, env):
            raise subprocess.TimeoutExpired(argv, pi.INSTALL_TIMEOUT)
        self.install(run=slow)
        self.assertIn('codex: FAILED: codex plugin add did not finish in %d s' % pi.INSTALL_TIMEOUT, self.text())

    def test_an_add_that_cannot_start_is_a_line(self):
        def gone(argv, env):
            raise OSError('not a valid Win32 application')
        self.install(run=gone)
        self.assertIn('codex: FAILED: codex plugin add could not start', self.text())

    def test_a_config_that_is_not_toml_is_refused(self):
        write(os.path.join(self.home, 'config.toml'), '[marketplaces\n')
        with self.assertRaises(pi.Refused):
            pi.read_config(self.home)


class CacheInUseTest(InstallCase):
    def test_the_cache_markers_name_their_exports(self):
        self.cache('1.0.0-main.1', 'c' * 40)
        os.makedirs(os.path.join(self.home, 'plugins', 'cache', 'pm-kit', 'pm-kit', '0.9.0'))   # no marker
        self.assertEqual(pi.in_use(self.home), {'c' * 12})
        self.assertEqual(codex.Step(environ={'CODEX_HOME': self.home}).in_use(), {'c' * 12})

    def test_no_cache_names_nothing(self):
        self.assertEqual(pi.in_use(os.path.join(self.base, 'no-home')), set())

    def test_a_marker_that_cannot_be_read_is_an_error(self):
        folder = self.cache('1.0.0-main.1')
        write(os.path.join(folder, P.MARKER), '{not json')
        with self.assertRaises(OSError):
            pi.in_use(self.home)


class StepTest(InstallCase):
    def setUp(self):
        super().setUp()
        pi.marketplace(self.export, lambda line: None, apply=True)    # current: the step says nothing of it

    def step(self, export=None, apply=False):
        codex.Step(environ=dict(self.env, CODEX_HOME=self.home))(export or self.export, self.lines.append, apply)

    def test_the_export_marketplace_installs(self):
        self.step()
        self.assertIn('codex: would run codex plugin add pm-kit@pm-kit', self.text())
        self.assertNotIn('pm-kit-local', self.text())

    def test_the_old_copy_too_is_a_line_naming_its_removal(self):
        self.config('\\\\?\\' + self.root, "\n[marketplaces.pm-kit-local]\nsource = 'C:\\\\x'\n")
        self.step()
        self.assertIn('also names the old pm-kit-local copy: Codex loads both', self.text())
        self.assertIn('codex plugin remove pm-kit@pm-kit-local', self.text())

    def test_only_the_old_copy_gets_the_switch_steps_then_the_mirror(self):
        write(os.path.join(self.home, 'config.toml'), "[marketplaces.pm-kit-local]\nsource = 'C:\\\\nowhere'\n")
        self.step()
        lines = self.lines
        self.assertIn('the pm-kit plugin is the old pm-kit-local copy', lines[0])
        self.assertEqual(lines[1:5], ['codex:   1. codex plugin remove pm-kit@pm-kit-local',
                                      'codex:   2. codex plugin marketplace remove pm-kit-local',
                                      'codex:   3. codex plugin marketplace add "%s"' % self.root,
                                      'codex:   4. codex plugin add pm-kit@pm-kit'])
        self.assertIn('codex: refused, nothing written', self.text())       # the mirror's own line

    def test_no_pm_kit_marketplace_names_the_install_steps(self):
        write(os.path.join(self.home, 'config.toml'), "model = 'x'\n")
        self.step()
        self.assertEqual(self.lines, ['codex: config.toml names no pm-kit marketplace; to install the plugin from '
                                      'the export, run, with CODEX_HOME=%s: codex plugin marketplace add "%s", then '
                                      'codex plugin add pm-kit@pm-kit' % (self.home, self.root)])

    def test_a_marked_session_installs_nothing(self):
        codex.Step(environ=dict(self.env, CODEX_HOME=self.home, PM_KIT_ROLE='pm'))(self.export, self.lines.append, True)
        self.assertIn('codex: skipped: a marked session (pm)', self.text())
        self.assertFalse(os.path.exists(os.path.join(self.home, 'plugins')))


class MarketplaceTest(InstallCase):
    def path(self):
        return os.path.join(self.root, *pi.MARKETPLACE)

    def test_apply_writes_codexs_marketplace_naming_the_export_folder(self):
        self.assertTrue(pi.marketplace(self.export, self.lines.append, apply=True))
        got = json.loads(read(self.path()))
        self.assertEqual(got, pi.marketplace_body('bbbbbbbbbbbb'))
        self.assertEqual(got['plugins'][0]['source'], {'source': 'local', 'path': './bbbbbbbbbbbb'})
        self.assertEqual(self.lines, ['codex: marketplace %s names ./bbbbbbbbbbbb' % self.path()])
        self.lines[:] = []
        self.assertTrue(pi.marketplace(self.export, self.lines.append, apply=True))   # current: no line
        self.assertEqual(self.lines, [])

    def test_a_dry_run_names_the_write_and_writes_nothing(self):
        self.assertFalse(pi.marketplace(self.export, self.lines.append, apply=False))
        self.assertFalse(os.path.exists(self.path()))
        self.assertEqual(self.lines, ['codex: would write %s naming ./bbbbbbbbbbbb' % self.path()])

    def test_a_junction_on_the_way_is_never_followed(self):
        elsewhere = os.path.join(self.base, 'elsewhere')
        os.makedirs(elsewhere)
        link(elsewhere, os.path.join(self.root, '.agents'))
        self.assertFalse(pi.marketplace(self.export, self.lines.append, apply=True))
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertIn('is a link: never followed', self.text())

    def test_a_marketplace_that_cannot_be_written_installs_nothing(self):
        write(os.path.join(self.root, '.agents'), 'a file where the folder goes\n')
        codex.Step(environ=dict(self.env, CODEX_HOME=self.home))(self.export, self.lines.append, True)
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn('codex: FAILED: cannot write the Codex marketplace', self.lines[0])
        self.assertIn('nothing installed', self.lines[0])


class RefreshTest(Base):
    """plugin_refresh's run with the Codex step: the export, then Codex's install of it."""

    def test_refresh_exports_then_codex_installs_and_prune_keeps_codexs_export(self):
        sha = self.push({'hosts/codex/shell/plugin.json': CODEX_MANIFEST})
        home = os.path.join(self.k.base, 'codex-home')
        write(os.path.join(home, 'config.toml'), "[marketplaces.pm-kit]\nsource = '%s'\n\n"
              "[plugins.\"pm-kit@pm-kit\"]\nenabled = true\n" % self.k.root)
        for n, month in (('a', 1), ('b', 2), ('c', 3), ('d', 4)):
            folder = os.path.join(self.k.root, n * 12)
            os.makedirs(folder)
            write(os.path.join(folder, P.MARKER),
                  json.dumps({'sha': n * 40, 'exported_at': '2026-0%d-01T00:00:00' % month}))
        old = os.path.join(self.k.root, 'aaaaaaaaaaaa')
        cache = os.path.join(home, 'plugins', 'cache', 'pm-kit', 'pm-kit', '0.1.0-main.1')
        write(os.path.join(cache, P.MARKER), '{"sha": "%s"}\n' % ('a' * 40))
        exe = os.path.join(self.k.base, 'codex.exe')
        write(exe, 'x\n')
        calls = []
        step = codex.Step(environ={'CODEX_HOME': home, 'PM_KIT_CODEX_EXE': exe, 'PATH': ''})
        f = self.k.folders('.claude')
        self.k.seed_migrated(f)
        runner = claude.Runner(self.k.home, claude='claude.cmd', run_proc=FakeProc())
        import hosts.codex.adapter.plugin_install as mod
        real = mod._run
        mod._run = lambda argv, env: calls.append(argv) or Done(1, b'', b'stand-in')
        self.addCleanup(setattr, mod, '_run', real)
        code = P.refresh(self.k.kit, self.s, f, self.lines.append, apply=True, runner=runner, now=NOW,
                         own=(1234, 99), alive=lambda pid, ctime: False, copies=[step],
                         in_use=lambda: P.InUse(['x'], set(), []))
        self.assertEqual(code, 0, self.lines)
        self.assertTrue(os.path.isdir(old), self.lines)            # Codex's cache came from it
        self.assertFalse(os.path.isdir(os.path.join(self.k.root, 'b' * 12)), self.lines)
        self.assertIn('host plugin copy names it: aaaaaaaaaaaa', self.text())
        self.assertEqual(calls, [[os.path.realpath(exe), 'plugin', 'add', 'pm-kit@pm-kit']])
        self.assertIn('codex: FAILED: codex plugin add exited 1: stand-in', self.text())
        self.assertTrue(os.path.isfile(os.path.join(self.k.root, sha[:12], '.codex-plugin', 'plugin.json')))


if __name__ == '__main__':
    unittest.main()
