"""The account inventory (docs/machine-setup.md "Account inventory"): read only, one entry per account
folder. Fixture folders under a temp home (tests/home.py temp_home; docs/test-tools.md "Home fence");
nothing here reads or writes the real home."""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest

from kitlib.machinesetup import inventory
from tests import gitfixture
from tests.home import TempHomeCase, machine_config
from tests.platforms import windows_only

SECRET = 'fixture-credential-text'


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def guard_settings(kit):
    return json.dumps({'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': 'pythonw',
         'args': [kit.replace('\\', '/') + '/boot/hook.py', 'claude', 'PreToolUse']}]}]}})


def junction(link, target):
    import _winapi
    _winapi.CreateJunction(target, link)


def snapshot(*tops):
    """{path: (sha256 or None for a folder, mtime_ns)} of every file and folder under the tops,
    links not followed."""
    out = {}
    for top in tops:
        for dirpath, dirnames, filenames in os.walk(top):
            for n in dirnames + filenames:
                p = os.path.join(dirpath, n)
                st = os.lstat(p)
                digest = None
                if n in filenames and os.path.isfile(p) and not os.path.islink(p):
                    with open(p, 'rb') as f:
                        digest = hashlib.sha256(f.read()).hexdigest()
                out[p] = (digest, st.st_mtime_ns)
            out[dirpath] = (None, os.lstat(dirpath).st_mtime_ns)
    return out


class Fixture(TempHomeCase):
    """A home with launcher accounts 1 to 3 (.claude-a, -b, -c), a kit with a managed settings
    template, and a plugin export whose state.json records the folders in `migrated`."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix='kit-inv-')
        self.addCleanup(gitfixture.rmtree, self.root)
        self.kit = os.path.join(self.root, 'kit')
        self.export = os.path.join(self.root, 'export')
        os.makedirs(os.path.join(self.kit, 'content', 'skills', 'builder'))
        os.makedirs(self.export)
        write(os.path.join(self.kit, 'hosts', 'claude', 'managed-settings.json'),
              guard_settings('{kit}').replace('"pythonw"', '"{pythonw}"'))
        self.names = {'a': '1', 'b': '2', 'c': '3'}
        machine_config(self.home, {'launcher': {'accounts': {n: ['~/.claude-' + k]
                                                             for k, n in self.names.items()}}}, case=self)
        self.dirs = {k: os.path.join(self.home, '.claude-' + k) for k in self.names}
        for d in self.dirs.values():
            write(os.path.join(d, '.credentials.json'), SECRET)
        self.state = {'sha': 'a' * 40, 'folders': {}}
        for d in self.dirs.values():
            self.migrate(d)

    def migrate(self, folder, rc=0):
        self.state['folders'][folder] = {'migrated': True, 'rc': rc}
        write(os.path.join(self.export, 'state.json'), json.dumps(self.state))

    def run_inventory(self):
        return inventory.inventory(self.home, self.kit, export_dir=self.export)

    def entry(self, res, key):
        shown = inventory._hide({'f': self.dirs[key]}, os.path.abspath(self.home))['f']
        return [f for f in res['folders'] if f['folder'] == shown][0]


class Planted(Fixture):
    """One of each kind of drift, on its own folder."""

    def setUp(self):
        super().setUp()
        write(os.path.join(self.dirs['a'], 'settings.json'), guard_settings(self.kit))
        self.stray = os.path.join(self.home, '.claude-stray')
        write(os.path.join(self.stray, 'settings.json'), '{}')
        self.state['folders'].pop(self.dirs['c'])             # an unmigrated plugin
        write(os.path.join(self.export, 'state.json'), json.dumps(self.state))
        self.res = self.run_inventory()


class Reports(Planted):
    def test_duplicated_guard_hook_is_reported(self):
        dups = self.entry(self.res, 'a')['duplicates']
        self.assertEqual(1, len(dups))
        self.assertIn('PreToolUse', dups[0])
        self.assertIn('hook.py claude PreToolUse', dups[0])
        self.assertEqual([], self.entry(self.res, 'b')['duplicates'])

    def test_a_folder_that_is_not_an_account_is_flagged(self):
        stray = [f for f in self.res['folders'] if f['not_account']]
        self.assertEqual(['.claude-stray'], [os.path.basename(f['folder']) for f in stray])
        self.assertIn('not an account; check before retiring', stray[0]['issues'])
        self.assertFalse(self.entry(self.res, 'a')['not_account'])

    def test_an_unmigrated_plugin_is_reported(self):
        c = self.entry(self.res, 'c')
        self.assertFalse(c['plugin']['migrated'])
        self.assertTrue(any('not migrated to the export' in i for i in c['issues']))
        self.assertTrue(self.entry(self.res, 'a')['plugin']['migrated'])

    def test_a_migrated_plugin_not_refreshed_is_reported(self):
        self.migrate(self.dirs['b'], rc=None)
        b = self.entry(self.run_inventory(), 'b')
        self.assertTrue(b['plugin']['migrated'])
        self.assertFalse(b['plugin']['refreshed'])
        self.assertTrue(any('not refreshed' in i for i in b['issues']))

    @windows_only('directory junctions are Windows only')
    def test_a_leftover_link_is_reported_with_its_removal_command(self):
        junction(os.path.join(self.dirs['b'], 'skills'), os.path.join(self.kit, 'content', 'skills'))
        b = self.entry(self.run_inventory(), 'b')
        self.assertEqual(1, len(b['links']))
        self.assertIn('leftover link', b['links'][0])
        self.assertIn('remove it with: cmd /c rmdir', b['links'][0])

    def test_labels_and_sign_in_without_reading_credentials(self):
        a = self.entry(self.res, 'a')
        self.assertEqual('1', a['label'])
        self.assertTrue(a['signed_in'])
        self.assertIsNone([f for f in self.res['folders'] if f['not_account']][0]['label'])
        self.assertFalse([f for f in self.res['folders'] if f['not_account']][0]['signed_in'])
        text = '\n'.join(inventory.lines(self.res)) + json.dumps(self.res)
        self.assertNotIn(SECRET, text)

    def test_the_home_folder_is_not_printed(self):
        text = '\n'.join(inventory.lines(self.res)) + json.dumps(self.res)
        self.assertNotIn(os.path.basename(self.home), text)
        self.assertIn(inventory.HOME_TEXT, text)

    def test_the_summary_counts_the_findings(self):
        s = self.res['summary']
        self.assertEqual(4, s['folders'])
        self.assertEqual(3, s['with_findings'])
        self.assertIn('summary: 4 folder(s), 3 need attention', inventory.lines(self.res)[-1])


class ReadOnly(Planted):
    def test_a_run_changes_no_byte_and_no_mtime(self):
        if os.name == 'nt':
            junction(os.path.join(self.dirs['b'], 'skills'), os.path.join(self.kit, 'content', 'skills'))
        before = snapshot(self.home, self.root)
        self.assertGreater(len(before), 10)
        inventory.lines(self.run_inventory())
        self.assertEqual(before, snapshot(self.home, self.root))


class Clean(Fixture):
    def test_a_clean_fixture_reports_nothing_to_do(self):
        res = self.run_inventory()
        self.assertEqual(3, res['summary']['folders'])
        self.assertEqual(0, res['summary']['findings'])
        self.assertEqual([], [f for f in res['folders'] if f['issues']])
        self.assertEqual([], res['notes'])
        self.assertEqual('summary: 3 folder(s), nothing to do', inventory.lines(res)[-1])


class Entry(Planted):
    def test_the_tool_exits_zero_whatever_it_found(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'account_inventory', os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), 'tools', 'account_inventory.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mod.main(['--json', '--home', self.home, '--kit', self.kit, '--export-dir', self.export])
        self.assertEqual(0, code)
        self.assertEqual(4, json.loads(out.getvalue())['summary']['folders'])


if __name__ == '__main__':
    unittest.main()
