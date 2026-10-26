"""kitlib.pluginsource.codex: plugin_refresh --apply mirrors the export's hosts/codex/shell parts
into the Codex marketplace copy and cache config.toml names (docs/plugin-source.md "Codex copy").
A fake Codex home and marketplace under a temp folder only: no real ~/.codex is read."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import pluginsource as P
from kitlib.pluginsource import claude, codex
from tests import gitfixture
from tests.pluginsource import FakeProc, Kit
from tests.pluginsource import test_tool
from tests.pluginsource.test_tool import TOOL, tree

NOW = datetime.datetime(2026, 10, 11, 9, 0)
UNMARKED = {}
OLD = {'skills/old/SKILL.md': 'old skill\n', 'agents/kit-a.toml': 'old = 1\n',
       'hooks/hooks.json': '{"old": true}\n'}
NEW = {'skills/new/SKILL.md': 'new skill\n', 'agents/kit-a.toml': 'new = 1\n',
       'hooks/hooks.json': '{"new": true}\n', 'plugin.json': '{"version": "9.9.9"}\n',
       'boot/hook.py': 'print(1)\n'}


def write_files(top, files):
    for rel, text in files.items():
        p = os.path.join(top, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)


def parts(top, names=('skills', 'agents', 'hooks')):
    """{relative path: bytes} of the mirrored parts under `top`."""
    out = {}
    for n in names:
        for rel, data in tree(os.path.join(top, n)).items():
            out[os.path.join(n, rel)] = data
    return out


def link(target, at):
    """A junction at `at` to folder `target` (no admin right needed)."""
    r = subprocess.run(['cmd', '/c', 'mklink', '/J', at, target], capture_output=True)
    if r.returncode != 0:
        raise unittest.SkipTest('cannot make a junction here: %r' % r.stderr)


class Home(object):
    """A fake Codex home: config.toml naming pm-kit@pm-kit-local and its marketplace folder, the
    marketplace copy and the 0.4.0 cache holding OLD."""

    def __init__(self, base, entry=None, cache=True, config=None):
        self.home = os.path.join(base, 'codex-home')
        self.market = os.path.join(base, 'market')
        self.copy = os.path.join(self.market, 'plugins', 'pm-kit')
        self.cache = os.path.join(self.home, 'plugins', 'cache', 'pm-kit-local', 'pm-kit', '0.4.0')
        os.makedirs(self.home)
        with open(os.path.join(self.home, 'config.toml'), 'w', encoding='utf-8') as fh:
            fh.write(config if config is not None else
                     "model = 'x'\n\n[marketplaces.pm-kit-local]\nsource = '%s'\n\n"
                     "[plugins.\"pm-kit@pm-kit-local\"]\nenabled = true\n" % self.market)
        manifest = os.path.join(self.market, '.agents', 'plugins', 'marketplace.json')
        os.makedirs(os.path.dirname(manifest))
        with open(manifest, 'w', encoding='utf-8') as fh:
            json.dump({'name': 'pm-kit-local', 'plugins': [
                {'name': 'pm-kit', 'source': entry or {'source': 'local', 'path': './plugins/pm-kit'}}]}, fh)
        write_files(self.copy, dict(OLD, **{'plugin.json': '{"version": "0.4.0"}\n'}))
        if cache:
            write_files(self.cache, dict(OLD, **{'plugin.json': '{"version": "0.4.0"}\n'}))

    def snapshot(self, base):
        return tree(base)


class Case(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-codex-')
        self.addCleanup(gitfixture.rmtree, self.base)
        self.shell = os.path.join(self.base, 'export', 'hosts', 'codex', 'shell')
        write_files(self.shell, NEW)
        self.lines = []

    def out(self, line):
        self.lines.append(line)

    def run_codex(self, h, apply=True, environ=UNMARKED):
        codex.refresh(self.shell, h.home, self.out, apply=apply, environ=environ)

    def text(self):
        return '\n'.join(self.lines)


class MirrorTest(Case):
    def test_apply_mirrors_the_parts_into_the_cache_and_the_marketplace_copy(self):
        h = Home(self.base)
        self.run_codex(h)
        want = parts(self.shell)
        self.assertEqual(parts(h.cache), want)
        self.assertEqual(parts(h.copy), want)
        for top in (h.cache, h.copy):                  # no version bump, no other file touched
            with open(os.path.join(top, 'plugin.json'), encoding='utf-8') as fh:
                self.assertEqual(json.load(fh)['version'], '0.4.0')
            self.assertFalse(os.path.exists(os.path.join(top, 'boot')))
            self.assertEqual(sorted(os.listdir(top)), ['agents', 'hooks', 'plugin.json', 'skills'])
        self.assertIn('hooks changed', self.text())
        self.lines = []
        self.run_codex(h)                              # a second run finds both current
        self.assertEqual(self.lines.count('codex: cache %s: current' % h.cache), 1, self.text())
        self.assertNotIn('refreshed', self.text())

    def test_a_dry_run_changes_nothing(self):
        h = Home(self.base)
        before = tree(self.base)
        self.run_codex(h, apply=False)
        self.assertEqual(tree(self.base), before)
        self.assertIn('would be replaced', self.text())

    def test_a_missing_cache_is_a_note_and_the_marketplace_copy_is_still_updated(self):
        h = Home(self.base, cache=False)
        self.run_codex(h)
        self.assertIn('no cache folder', self.text())
        self.assertEqual(parts(h.copy), parts(self.shell))
        self.assertFalse(os.path.exists(h.cache))

    def test_no_config_is_a_note_and_writes_nothing(self):
        home = os.path.join(self.base, 'none')
        codex.refresh(self.shell, home, self.out, apply=True, environ=UNMARKED)
        self.assertIn('Codex is not set up here', self.text())
        self.assertFalse(os.path.exists(home))

    def test_a_marked_session_writes_nothing(self):
        h = Home(self.base)
        before = tree(self.base)
        self.run_codex(h, environ={'PM_KIT_ROLE': 'pm'})
        self.assertEqual(tree(self.base), before)
        self.assertIn('codex: skipped: a marked session (pm)', self.text())


class RefuseTest(Case):
    def refused(self, h):
        before = tree(self.base)
        self.run_codex(h)
        self.assertEqual(tree(self.base), before)
        self.assertIn('codex: refused, nothing written', self.text())

    def test_a_source_outside_the_marketplace_is_refused(self):
        write_files(os.path.join(self.base, 'elsewhere'), dict(OLD, **{'plugin.json': '{"version": "0.4.0"}\n'}))
        self.refused(Home(self.base, entry={'source': 'local', 'path': './../elsewhere'}))
        self.assertIn('a path outside the marketplace', self.text())

    def test_a_source_not_starting_dot_slash_is_refused(self):
        self.refused(Home(self.base, entry='plugins/pm-kit'))      # the folder is there: the form refuses
        self.assertIn('no local ./ path', self.text())

    def test_an_absolute_source_is_refused(self):
        self.refused(Home(self.base, entry={'source': 'local', 'path': os.path.join(self.base, 'market',
                                                                                    'plugins', 'pm-kit')}))

    def test_a_config_without_the_plugin_entry_is_refused(self):
        h = Home(self.base, config="[marketplaces.pm-kit-local]\nsource = '%s'\n"
                 % os.path.join(self.base, 'market'))
        self.refused(h)
        self.assertIn('[plugins."pm-kit@pm-kit-local"]', self.text())

    def test_a_marketplace_with_no_local_source_is_refused(self):
        self.refused(Home(self.base, config="[plugins.\"pm-kit@pm-kit-local\"]\nenabled = true\n"
                          "[marketplaces.pm-kit-local]\nsource = 'https://example.invalid/x.git'\n"))

    def test_a_junction_on_the_way_to_the_cache_is_refused(self):
        h = Home(self.base, cache=False)
        real = os.path.join(self.base, 'real-cache')
        write_files(os.path.join(real, '0.4.0'), OLD)
        os.makedirs(os.path.dirname(os.path.dirname(h.cache)))
        link(real, os.path.dirname(h.cache))           # ...\pm-kit-local\pm-kit -> real-cache
        self.refused(h)
        self.assertIn('a link or junction at', self.text())
        self.assertEqual(parts(os.path.join(real, '0.4.0')), {k.replace('/', os.sep): v.encode()
                                                              for k, v in OLD.items()})

    def test_a_part_that_is_a_junction_is_left_alone(self):
        h = Home(self.base)
        real = os.path.join(self.base, 'real-skills')
        write_files(real, {'keep/SKILL.md': 'keep\n'})
        shutil.rmtree(os.path.join(h.cache, 'skills'))
        link(real, os.path.join(h.cache, 'skills'))
        self.run_codex(h)
        self.assertIn('is a link or junction, never followed', self.text())
        self.assertEqual(tree(real), {os.path.join('keep', 'SKILL.md'): b'keep\n'})


class SwapTest(Case):
    def test_a_busy_file_leaves_the_old_copy_whole(self):
        h = Home(self.base)
        old = parts(h.cache)
        fh = open(os.path.join(h.cache, 'skills', 'old', 'SKILL.md'), 'rb')
        try:
            self.run_codex(h)
        finally:
            fh.close()
        self.assertIn('busy:', self.text())
        self.assertEqual(parts(h.cache, ('skills',)), {k: v for k, v in old.items()
                                                       if k.startswith('skills')})
        self.assertEqual(sorted(n for n in os.listdir(h.cache) if 'pmkit' in n), [])
        self.assertEqual(parts(h.copy), parts(self.shell))      # the other target still goes

    def test_a_replace_that_fails_after_the_rename_puts_the_old_copy_back(self):
        h = Home(self.base, cache=False)
        old = parts(h.copy)
        real = os.replace

        def fail(a, b, *rest, **kw):
            if os.path.basename(a).startswith('skills' + codex.NEW):
                raise OSError(13, 'forced failure')
            return real(a, b, *rest, **kw)
        with mock.patch.object(codex.os, 'replace', fail):
            self.run_codex(h)
        self.assertIn('could not put the new copy in place', self.text())
        self.assertIn('the old one is back', self.text())
        got = parts(h.copy)
        self.assertEqual({k: v for k, v in got.items() if k.startswith('skills')},
                         {k: v for k, v in old.items() if k.startswith('skills')})
        self.assertEqual(sorted(n for n in os.listdir(h.copy) if 'pmkit' in n), [])
        self.assertEqual(got[os.path.join('agents', 'kit-a.toml')], b'new = 1\n')   # the others went

    def test_a_copy_a_stopped_run_left_is_cleared(self):
        h = Home(self.base)
        write_files(os.path.join(h.cache, 'skills%s4242' % codex.OLD), {'x': 'x\n'})
        self.run_codex(h)
        self.assertEqual(sorted(n for n in os.listdir(h.cache) if 'pmkit' in n), [])
        self.assertEqual(parts(h.cache), parts(self.shell))


class RefreshWiringTest(unittest.TestCase):
    """pluginsource.refresh with codex_home: the export's own hosts/codex/shell is mirrored, also
    when the Claude side is current; a marked session (pm_watch's auto_refresh spawn inherits the
    mark) mirrors nothing."""

    def setUp(self):
        self.k = Kit(self)
        r = self.k.repo
        for rel, text in NEW.items():
            r.write('hosts/codex/shell/' + rel, text)
        r.commit('codex shell', *['hosts/codex/shell/' + rel for rel in NEW])
        r.git('push', '-q', 'origin', 'main')
        self.s = P.Settings(self.k.root, False, 'origin')
        self.h = Home(self.k.base)
        self.f = self.k.folders('.claude')
        self.k.seed_migrated(self.f)
        self.lines = []

    def refresh(self, environ=UNMARKED, apply=True):
        runner = claude.Runner(self.k.home, claude='claude.cmd', run_proc=FakeProc())
        return P.refresh(self.k.kit, self.s, self.f, self.lines.append, apply=apply, runner=runner,
                         now=NOW, own=(1234, 99), alive=lambda pid, ctime: False,
                         codex_home=self.h.home, environ=environ)

    def shell(self):
        sha = self.k.repo.git('rev-parse', 'refs/remotes/origin/main')
        return os.path.join(self.k.root, sha[:12], 'hosts', 'codex', 'shell')

    def test_apply_mirrors_and_a_current_claude_side_still_mirrors(self):
        self.assertEqual(self.refresh(), 0, self.lines)
        self.assertEqual(parts(self.h.cache), parts(self.shell()))
        write_files(self.h.cache, {'skills/new/SKILL.md': 'stale again\n'})
        self.lines = []
        self.assertEqual(self.refresh(), 0, self.lines)
        self.assertIn('nothing to do', '\n'.join(self.lines))
        self.assertEqual(parts(self.h.cache), parts(self.shell()))
        self.assertFalse(os.path.exists(os.path.join(self.k.root, P.PIDFILE)))

    def test_a_marked_session_mirrors_nothing_and_the_exit_code_is_the_claude_sides(self):
        before = (parts(self.h.cache), parts(self.h.copy))
        self.assertEqual(self.refresh(environ={'PM_KIT_ROLE': 'pm'}), 0, self.lines)
        self.assertEqual((parts(self.h.cache), parts(self.h.copy)), before)
        self.assertIn('codex: skipped: a marked session (pm)', '\n'.join(self.lines))

    def test_the_claude_lines_and_exit_code_are_the_same_with_or_without_codex(self):
        runner = claude.Runner(self.k.home, claude='claude.cmd', run_proc=FakeProc())
        plain = []
        code = P.refresh(self.k.kit, self.s, self.f, plain.append, apply=False, runner=runner, now=NOW)
        self.assertEqual(self.refresh(apply=False), code)
        self.assertEqual([l for l in self.lines if not l.startswith('codex:')], plain)
        self.assertFalse([l for l in plain if 'codex' in l])

    def test_a_dry_run_writes_nothing(self):
        before = (tree(self.h.home), tree(self.h.market))
        self.assertEqual(self.refresh(apply=False), 0, self.lines)
        self.assertEqual((tree(self.h.home), tree(self.h.market)), before)
        self.assertFalse(os.path.exists(os.path.join(self.k.root, self.k.repo.git(
            'rev-parse', 'refs/remotes/origin/main')[:12])))
        self.assertIn('would be replaced', '\n'.join(self.lines))


class ToolMarkTest(unittest.TestCase):
    """tools/plugin_refresh.py --apply, as pm_watch's auto_refresh starts it: the Codex home is
    <USERPROFILE>\\.codex (no --home, no CODEX_HOME); a marked session leaves it alone."""

    setUp = test_tool.ToolTest.setUp

    def apply(self, extra):
        env = gitfixture.fixture_env(dict({'USERPROFILE': self.k.home, 'HOME': self.k.home,
                                           'LOCALAPPDATA': self.lad}, **extra))
        env.pop('CLAUDE_CONFIG_DIR', None)
        env.pop('CODEX_HOME', None)
        env['ProgramW6432'] = env['ProgramFiles'] = self.pf
        env['PATH'] = self.bin + os.pathsep + env.get('PATH', '')
        return subprocess.run([sys.executable, TOOL, '--kit', self.k.kit, '--apply'], env=env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=120)

    def test_marked_skips_and_unmarked_mirrors(self):
        r = self.k.repo
        for rel, text in NEW.items():
            r.write('hosts/codex/shell/' + rel, text)
        r.commit('codex shell', *['hosts/codex/shell/' + rel for rel in NEW])
        r.git('push', '-q', 'origin', 'main')
        h = Home(self.k.base)
        shutil.move(h.home, os.path.join(self.k.home, '.codex'))
        cache = os.path.join(self.k.home, '.codex', os.path.relpath(h.cache, h.home))
        before = (tree(cache), tree(h.copy))
        res = self.apply({'PM_KIT_ROLE': 'pm'})
        self.assertIn('codex: skipped: a marked session (pm)', res.stdout, res.stdout + res.stderr)
        self.assertEqual((tree(cache), tree(h.copy)), before)
        res = self.apply({})
        self.assertIn('codex: cache %s: refreshed' % cache, res.stdout, res.stdout + res.stderr)
        self.assertEqual(parts(cache)[os.path.join('agents', 'kit-a.toml')], b'new = 1\n')
        self.assertEqual(parts(h.copy)[os.path.join('agents', 'kit-a.toml')], b'new = 1\n')


if __name__ == '__main__':
    unittest.main()
