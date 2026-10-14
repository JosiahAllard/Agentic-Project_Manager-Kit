"""A gate started from the plugin's export (`<kit>/tools/gate.py -C <repository>`, the command the
kit's skills give) checks a repository's committed stubs against the kit's main checkout, as a
start from that checkout does.

The stubs a repository commits name the kit's main checkout (kitlib.stubs.stub_text). The gate's
kit-stubs step runs `{here}/kit_stubs.py check` (kitlib.gate.steps.KIT_TOOLS), and kit_stubs
compares each stub with the one kitlib.stubs.kit_root() would write: the main checkout only while
KIT_LIVE_ROOT names it (kitlib.pluginsource.live_root). A start from the main checkout pins and
gets it (kitlib.gate.pin); a start from the plugin's export ran live with none, so every stub of
every repository read as stale. Now the export's marker names the main checkout and the start pins
through it.

The kit here is a temp repository holding this tree's tools/gate.py, tools/land.py,
tools/kit_stubs.py, kitlib and a probe, tools/export_probe.py, that does what tools/gate.py does
first (pin.run) and then what the gate's kit-stubs step runs. Its plugin export is
pluginsource.export's, in a temp LOCALAPPDATA; the repository is another temp repository. Every
run gets temp USERPROFILE, HOME and LOCALAPPDATA, and no KIT_PINNED or KIT_LIVE_ROOT.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from kitlib import pluginsource, stubs
from tests import gitfixture
from tests.gate.test_live_pin import DROP, HERE, _kit_files, pins_root, temp_places

PROBE = '''"""tools/gate.py's start (pin.run), then the gate's kit-stubs step on the checkout argv[1]."""
import os
import subprocess
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

if __name__ == '__main__':
    from kitlib.gate import pin
    code, said = pin.run(__file__, sys.argv[1:])
    if said:
        sys.stderr.write(said + '\\n')
    if code is not None:
        sys.exit(code)
    print('probe ran from %s' % KIT, flush=True)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.exit(subprocess.call([sys.executable, os.path.join(here, 'kit_stubs.py'), 'check',
                              '-C', sys.argv[1]]))
'''

TOOLS = ('gate', 'land')


class ExportStubs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.kit = gitfixture.TempRepo()
        cls.addClassCleanup(cls.kit.cleanup)
        files = _kit_files() + ['tools/kit_stubs.py']
        for rel in files:
            dest = os.path.join(cls.kit.root, *rel.split('/'))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(HERE, *rel.split('/')), dest)
        cls.kit.write('tools/export_probe.py', PROBE)
        cls.sha = cls.kit.commit('the kit', 'tools/export_probe.py', *files)
        cls.main = os.path.realpath(cls.kit.root)

    def setUp(self):
        self.places = temp_places(self)
        self.kit.git('update-ref', 'refs/remotes/origin/main', self.sha)
        plugin = os.path.join(self.places['LOCALAPPDATA'], 'pm-kit', 'plugin')
        self.export, _v = pluginsource.export(self.kit.root, self.sha, plugin)
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('.kit.json', json.dumps({'stubs': {'tools': list(TOOLS)}}))
        for name in TOOLS:
            self.stub(name, self.main)

    def stub(self, name, kit):
        self.repo.write('tools/%s.py' % name, stubs.stub_text(name, kit=kit))

    def probe(self):
        env = gitfixture.fixture_env(self.places)
        for k in DROP:
            env.pop(k, None)
        r = subprocess.run([sys.executable, os.path.join(self.export, 'tools', 'export_probe.py'),
                            self.repo.root], cwd=self.repo.root, env=env, capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=120)
        return r.returncode, r.stdout + r.stderr

    def pinned(self):
        return pluginsource.read_marker(os.path.join(pins_root(self.places), self.sha[:12]))

    def test_stubs_naming_the_main_checkout_are_current(self):
        code, said = self.probe()
        self.assertEqual(code, 0, said)
        self.assertIn('kit_stubs: %d stub(s) current' % len(TOOLS), said)
        self.assertNotIn('stale', said)
        self.assertIsNotNone(self.pinned(), 'the run was not pinned:\n' + said)
        self.assertIn('probe ran from %s' % os.path.join(pins_root(self.places), self.sha[:12]),
                      said)

    def test_a_stub_naming_another_kit_is_still_stale(self):
        other = tempfile.mkdtemp(prefix='kit-other-')
        self.addCleanup(gitfixture.rmtree, other)
        self.stub('land', other)
        code, said = self.probe()
        self.assertEqual(code, stubs.PROBLEMS, said)
        tools = os.path.join(os.path.realpath(self.repo.root), 'tools')
        self.assertIn('stale    %s (runs the kit at %r)'
                      % (os.path.join(tools, 'land.py'), os.path.normpath(other)), said)
        self.assertNotIn('stale    %s' % os.path.join(tools, 'gate.py'), said)

    def test_a_pin_that_finds_nothing_pushed_runs_live_and_still_names_the_main_checkout(self):
        self.kit.git('update-ref', '-d', 'refs/remotes/origin/main')
        code, said = self.probe()
        self.assertEqual(code, 0, said)
        self.assertIn('kit_stubs: %d stub(s) current' % len(TOOLS), said)
        self.assertIn('probe ran from %s' % self.export, said)
        self.assertIsNone(self.pinned())

    def test_an_export_whose_marker_names_no_checkout_still_runs_live(self):
        """An export made before markers named the checkout: no pin, and the old false stale
        lines (a plugin refresh replaces it)."""
        path = os.path.join(self.export, pluginsource.MARKER)
        marker = pluginsource.read_marker(self.export)
        marker.pop('checkout', None)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(marker, fh)
        code, said = self.probe()
        self.assertEqual(code, stubs.PROBLEMS, said)
        self.assertIn('runs the kit at %r' % os.path.normpath(self.main), said)
        self.assertIsNone(self.pinned())


if __name__ == '__main__':
    unittest.main()
