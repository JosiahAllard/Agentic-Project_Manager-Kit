"""kitlib.config.copies: which kit copies on this machine would refuse a machine config key. Each
copy's kitlib/config/user.py is read as text and parsed, never imported; every folder is a temp
tree here."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib.config import copies

NEW = '''
RETIRED = ('ledger_refs',)
SCHEMA = {
    'owner_email': Field(Email(), None, 'x'),
    'hub': Field(Owned('kitlib.hubsite.settings'), None, 'x'),
    'plugin': Field(Owned('kitlib.pluginsource'), None, 'x'),
}
'''
OLD = '''
SCHEMA = {
    'owner_email': Field(Email(), None, 'x'),
    'ledger_refs': Field(Str(), None, 'x'),
}
'''


class CopiesCase(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='kit-copies-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.root = os.path.join(self.top, 'plugin')
        os.makedirs(self.root)

    def copy(self, folder, text):
        """`folder` with a kitlib/config/user.py holding `text` (None: no such file)."""
        os.makedirs(os.path.join(folder, 'kitlib', 'config'), exist_ok=True)
        if text is not None:
            with open(os.path.join(folder, *copies.USER_PY), 'w', encoding='utf-8') as f:
                f.write(text)
        return folder


class SchemaKeysTest(CopiesCase):
    def test_this_checkouts_own_schema_is_read_and_knows_hub(self):
        """The live module, not a fixture: a SCHEMA rewritten into a form this reader cannot
        parse would make every copy 'cannot tell'."""
        kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        known, retired = copies.schema_keys(kit)
        self.assertIn('hub', known)
        self.assertIn('owner_email', known)
        self.assertEqual(copies.refused(kit, ['hub', 'owner_email', 'plugin']), ())


    def test_the_keys_and_the_retired_keys_are_read_without_importing(self):
        folder = self.copy(os.path.join(self.top, 'a'), NEW + '\nraise SystemExit("imported")\n')
        known, retired = copies.schema_keys(folder)
        self.assertEqual(known, frozenset(['owner_email', 'hub', 'plugin']))
        self.assertEqual(retired, frozenset(['ledger_refs']))

    def test_a_copy_with_no_retired_tuple_retires_nothing(self):
        self.assertEqual(copies.schema_keys(self.copy(os.path.join(self.top, 'a'), OLD))[1],
                         frozenset())

    def test_what_cannot_be_read_as_literal_keys_is_a_copy_error(self):
        for text, why in ((None, 'cannot read'), ('SCHEMA = {', 'cannot parse'),
                          ('X = 1\n', 'no SCHEMA'), ('SCHEMA = dict(a=1)\n', 'no SCHEMA'),
                          ('SCHEMA = {NAME: 1}\n', 'not a literal string'),
                          ('SCHEMA = {**OTHER}\n', 'not a literal string')):
            folder = os.path.join(self.top, 'c%d' % len(os.listdir(self.top)))
            self.copy(folder, text)
            with self.assertRaises(copies.CopyError) as cm:
                copies.schema_keys(folder)
            self.assertIn(why, str(cm.exception), text)


class RefusedTest(CopiesCase):
    def test_a_copy_refuses_each_key_it_neither_knows_nor_retired(self):
        new = self.copy(os.path.join(self.top, 'new'), NEW)
        old = self.copy(os.path.join(self.top, 'old'), OLD)
        keys = ['hub', 'ledger_refs', 'owner_email', 'plugin']
        self.assertEqual(copies.refused(new, keys), ())
        self.assertEqual(copies.refused(old, keys), ('hub', 'plugin'))

    def test_note_keys_are_never_refused(self):
        old = self.copy(os.path.join(self.top, 'old'), OLD)
        self.assertEqual(copies.refused(old, ['_note', 'comment', 'owner_email']), ())


class ConfigKeysTest(CopiesCase):
    def test_the_keys_are_the_files_own_besides_notes_plus_the_wanted_ones(self):
        path = os.path.join(self.top, 'config.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'_why': 1, 'comment': 2, 'owner_email': 'o@example.invalid', 'plugin': {}}, f)
        self.assertEqual(copies.config_keys(path, ('hub',)), ('hub', 'owner_email', 'plugin'))

    def test_an_absent_file_holds_only_the_wanted_keys(self):
        self.assertEqual(copies.config_keys(os.path.join(self.top, 'none.json'), ('hub',)),
                         ('hub',))


class FindTest(CopiesCase):
    def test_exports_pins_and_each_accounts_install_are_found_once_each(self):
        current = self.copy(os.path.join(self.root, 'aaaaaaaaaaaa'), NEW)
        stale = self.copy(os.path.join(self.root, 'bbbbbbbbbbbb'), OLD)
        pin = self.copy(os.path.join(self.root, copies.PINS, 'cccccccccccc'), NEW)
        os.makedirs(os.path.join(self.root, copies.PINS, 'leases'))
        os.makedirs(os.path.join(self.root, 'dddddddddddd.tmp'))
        os.makedirs(os.path.join(self.root, 'not-an-export'))
        one, two = os.path.join(self.top, 'acct1'), os.path.join(self.top, 'acct2')
        cache = self.copy(os.path.join(one, 'plugins', 'cache', 'pm-kit', 'pm-kit', '1.0'), OLD)
        installs = [(cache, one), (current, two)]   # two loads in place: one copy, two roles
        found = {os.path.normcase(c.path): c for c in copies.find(self.root, 'aaaaaaaaaaaa',
                                                                 installs)}
        self.assertEqual(set(found), {os.path.normcase(p) for p in (current, stale, pin, cache)})
        self.assertTrue(found[os.path.normcase(current)].in_use)
        self.assertIn('current export', found[os.path.normcase(current)].role)
        self.assertIn('installed in %s' % two, found[os.path.normcase(current)].role)
        self.assertFalse(found[os.path.normcase(stale)].in_use)
        self.assertTrue(found[os.path.normcase(pin)].in_use)
        self.assertTrue(found[os.path.normcase(cache)].in_use)
        self.assertEqual(found[os.path.normcase(cache)].role, 'installed in %s' % one)

    def test_an_absent_root_and_no_installs_find_nothing(self):
        self.assertEqual(copies.find(os.path.join(self.top, 'none'), None, []), [])


class CheckTest(CopiesCase):
    def test_each_copy_gets_one_line_and_only_copies_in_use_decide(self):
        self.copy(os.path.join(self.root, 'aaaaaaaaaaaa'), NEW)
        self.copy(os.path.join(self.root, 'bbbbbbbbbbbb'), OLD)
        report = copies.check(copies.find(self.root, 'aaaaaaaaaaaa', []), ('hub', 'owner_email'))
        self.assertTrue(report.ready)
        text = '\n'.join(report.lines)
        self.assertIn('aaaaaaaaaaaa (current export): knows every key', text)
        self.assertIn('bbbbbbbbbbbb (not in use): would refuse hub', text)
        self.assertEqual(report.stale, 1)

    def test_a_copy_in_use_that_refuses_or_cannot_be_read_is_not_ready(self):
        self.copy(os.path.join(self.root, 'aaaaaaaaaaaa'), OLD)
        self.copy(os.path.join(self.root, copies.PINS, 'cccccccccccc'), 'SCHEMA = {')
        report = copies.check(copies.find(self.root, 'aaaaaaaaaaaa', []), ('hub',))
        self.assertFalse(report.ready)
        text = '\n'.join(report.lines)
        self.assertIn('aaaaaaaaaaaa (current export): would refuse hub', text)
        self.assertIn('cccccccccccc (pin): cannot tell: cannot parse', text)

    def test_an_install_whose_folder_is_gone_says_to_refresh_that_account(self):
        self.copy(os.path.join(self.root, 'aaaaaaaaaaaa'), NEW)
        gone, acct = os.path.join(self.top, 'gone'), os.path.join(self.top, 'acct')
        report = copies.check(copies.find(self.root, 'aaaaaaaaaaaa', [(gone, acct)]), ('hub',),
                              'python plugin_refresh.py --apply')
        self.assertFalse(report.ready)
        self.assertIn('%s (installed in %s): cannot tell: the folder is missing: refresh that '
                      'account (python plugin_refresh.py --apply)' % (gone, acct), report.lines)

    def test_no_copy_found_is_not_ready(self):
        report = copies.check([], ('hub',))
        self.assertFalse(report.ready)
        self.assertEqual(report.lines, [copies.NONE_FOUND])


if __name__ == '__main__':
    unittest.main()
