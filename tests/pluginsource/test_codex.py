"""hosts.codex.adapter.plugin_copy's Step as kitlib.pluginsource.refresh runs it, the step the Codex
adapter hands it through tools/_host.py (docs/plugin-source.md "Host plugin copies"): a Codex home
that names only the old hand-made pm-kit-local copy gets the switch steps, and that copy is never
written again. A fake Codex home and marketplace under a temp folder only: no real ~/.codex is read."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import unittest

from hosts.codex.adapter import ADAPTER
from hosts.codex.adapter import plugin_copy as codex
from kitlib import pluginsource as P
from kitlib.pluginsource import claude
from tests import gitfixture
from tests.pluginsource import FakeProc, Kit
from tests.host.conformance import load_tool
from tests.pluginsource import test_tool
from tests.pluginsource.test_tool import TOOL, tree

SWITCH = 'codex: the pm-kit plugin is the hand-made pm-kit-local copy'
NOW = datetime.datetime(2026, 10, 11, 9, 0)
UNMARKED = {}
OLD = {'skills/old/SKILL.md': 'old skill\n', 'agents/kit-a.toml': 'old = 1\n',
       'hooks/hooks.json': '{"old": true}\n'}
NEW = {'skills/new/SKILL.md': 'new skill\n', 'agents/kit-a.toml': 'new = 1\n',
       'hooks/hooks.json': '{"new": true}\n', 'plugin.json': '{"version": "9.9.9"}\n'}


def link(target, at):
    """A junction at `at` to folder `target` (no admin right needed)."""
    r = subprocess.run(['cmd', '/c', 'mklink', '/J', at, target], capture_output=True)
    if r.returncode != 0:
        raise unittest.SkipTest('cannot make a junction here: %r' % r.stderr)


def write_files(top, files):
    for rel, text in files.items():
        p = os.path.join(top, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)


class Home(object):
    """A fake Codex home: config.toml naming pm-kit@pm-kit-local and its marketplace folder, the
    marketplace copy and the 0.4.0 cache holding OLD."""

    def __init__(self, base):
        self.home = os.path.join(base, 'codex-home')
        self.market = os.path.join(base, 'market')
        self.copy = os.path.join(self.market, 'plugins', 'pm-kit')
        self.cache = os.path.join(self.home, 'plugins', 'cache', 'pm-kit-local', 'pm-kit', '0.4.0')
        os.makedirs(self.home)
        with open(os.path.join(self.home, 'config.toml'), 'w', encoding='utf-8') as fh:
            fh.write("model = 'x'\n\n[marketplaces.pm-kit-local]\nsource = '%s'\n\n"
                     "[plugins.\"pm-kit@pm-kit-local\"]\nenabled = true\n" % self.market)
        manifest = os.path.join(self.market, '.agents', 'plugins', 'marketplace.json')
        os.makedirs(os.path.dirname(manifest))
        with open(manifest, 'w', encoding='utf-8') as fh:
            json.dump({'name': 'pm-kit-local', 'plugins': [
                {'name': 'pm-kit', 'source': {'source': 'local', 'path': './plugins/pm-kit'}}]}, fh)
        write_files(self.copy, dict(OLD, **{'plugin.json': '{"version": "0.4.0"}\n'}))
        write_files(self.cache, dict(OLD, **{'plugin.json': '{"version": "0.4.0"}\n'}))


class RefreshWiringTest(unittest.TestCase):
    """pluginsource.refresh with the Codex step in `copies`: a home naming only pm-kit-local gets the
    switch steps and its copy is left as it was; a marked session (pm_watch's auto_refresh spawn
    inherits the mark) is skipped; a step that raises is a line, never the exit code."""

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

    def refresh(self, environ=UNMARKED, apply=True, copies=None):
        runner = claude.Runner(self.k.home, claude='claude.cmd', run_proc=FakeProc())
        if copies is None:
            copies = [codex.Step(environ=dict(environ, CODEX_HOME=self.h.home))]
        return P.refresh(self.k.kit, self.s, self.f, self.lines.append, apply=apply, runner=runner,
                         now=NOW, own=(1234, 99), alive=lambda pid, ctime: False, copies=copies)

    def old_copy(self):
        return (tree(self.h.cache), tree(self.h.copy))

    def test_apply_gives_the_switch_steps_and_never_writes_the_old_copy(self):
        before = self.old_copy()
        self.assertEqual(self.refresh(), 0, self.lines)
        self.assertIn(SWITCH, '\n'.join(self.lines))
        self.assertEqual(self.old_copy(), before)
        self.assertFalse(os.path.exists(os.path.join(self.k.root, P.PIDFILE)))

    def test_a_marked_session_is_skipped_and_the_exit_code_is_the_claude_sides(self):
        before = self.old_copy()
        self.assertEqual(self.refresh(environ={'PM_KIT_ROLE': 'pm'}), 0, self.lines)
        self.assertEqual(self.old_copy(), before)
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
        self.assertIn('codex: would write', '\n'.join(self.lines))

    def test_a_step_that_raises_is_a_line_and_the_next_step_still_runs(self):
        seen = []

        def broken(folder, out, apply):
            raise ValueError('boom')
        broken.label = 'other'
        good = codex.Step(environ={'CODEX_HOME': self.h.home})
        self.assertEqual(self.refresh(copies=[broken, lambda f, o, a: seen.append((f, a)), good]),
                         0, self.lines)
        self.assertIn('other: not refreshed: ValueError: boom', self.lines)
        sha = self.k.repo.git('rev-parse', 'refs/remotes/origin/main')
        self.assertEqual(seen, [(os.path.join(self.k.root, sha[:12]), True)])   # the export folder
        self.assertIn(SWITCH, '\n'.join(self.lines))


class HostEdgeTest(unittest.TestCase):
    """tools/_host.py plugin_copies: the step of each registered host whose adapter has the hook."""

    def test_the_codex_adapter_hands_its_step_and_no_other_host_has_one(self):
        steps = load_tool('_host').plugin_copies('X:\\home', {'PM_KIT_ROLE': 'pm'})
        self.assertEqual([type(s) for s in steps], [codex.Step])
        self.assertEqual(steps[0].home, os.path.join('X:\\home', '.codex'))
        self.assertEqual(steps[0].environ, {'PM_KIT_ROLE': 'pm'})
        self.assertEqual(ADAPTER.plugin_copy(None, {'CODEX_HOME': 'X:\\c'}).home, 'X:\\c')


class ToolMarkTest(unittest.TestCase):
    """tools/plugin_refresh.py --apply, as pm_watch's auto_refresh starts it: the Codex home is
    <USERPROFILE>\\.codex (no --home, no CODEX_HOME); a marked session is skipped, and an unmarked
    one gives the switch steps and leaves the old copy alone."""

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

    def test_marked_skips_and_unmarked_gives_the_switch_steps(self):
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
        self.assertIn(SWITCH, res.stdout, res.stdout + res.stderr)
        self.assertEqual((tree(cache), tree(h.copy)), before)


if __name__ == '__main__':
    unittest.main()
