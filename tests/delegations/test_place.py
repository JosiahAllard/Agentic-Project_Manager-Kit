"""kitlib.delegations.place: where a hub-approved run may act (docs/delegations.md "Hub approvals").

Every case builds its own stand-in Dev Drive (`<dev-drive>`, the machine config's `dev_drive`),
home folder, kit folder and kit release store in a temp folder outside any git checkout.
"""
import _winapi
import json
import os
import shutil
import tempfile
import unittest

from kitlib.config import user as kuser
from kitlib.delegations import place


def _write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kit-place-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dev = os.path.join(self.tmp, 'dd')
        self.home = os.path.join(self.tmp, 'home')
        self.kit = os.path.join(self.tmp, 'kit')
        self.store = os.path.join(self.tmp, 'release')
        self.other = os.path.join(self.tmp, 'other')
        for d in (self.dev, self.home, self.kit, self.store, self.other):
            os.makedirs(d)
        self.cfg = {'dev_drive': self.dev, 'plugin': {'export_dir': self.store}}

    def d(self, *parts):
        return os.path.join(self.dev, *parts)

    def check(self, values=None, tokens=(), cfg=None, **kw):
        kw.setdefault('home', self.home)
        kw.setdefault('environ', {})
        kit = kw.pop('kit', self.kit)
        return place.check(values or {}, list(tokens), self.cfg if cfg is None else cfg, kit, **kw)


class DevDriveKeyTest(Base):
    def test_the_key_is_read_from_the_machine_config(self):
        _write(os.path.join(self.home, kuser.DIR_NAME, kuser.FILE_NAME),
               json.dumps({'dev_drive': self.dev}))
        self.assertEqual(kuser.dev_drive(kuser.user_config(self.home)), self.dev)

    def test_unset_or_blank_is_none(self):
        self.assertIsNone(kuser.dev_drive(kuser.user_config(self.home)))
        self.assertIsNone(kuser.dev_drive({'dev_drive': None}))
        self.assertIsNone(kuser.dev_drive({'dev_drive': '  '}))
        self.assertIsNone(kuser.dev_drive({}))

    def test_a_relative_key_is_a_config_error(self):
        _write(os.path.join(self.home, kuser.DIR_NAME, kuser.FILE_NAME),
               json.dumps({'dev_drive': 'dd'}))
        with self.assertRaises(kuser.ConfigError):
            kuser.user_config(self.home)

    def test_no_dev_drive_refuses_every_path(self):
        cfg = dict(self.cfg, dev_drive=None)
        self.assertEqual(self.check({'dest': self.d('out')}, cfg=cfg), [place.WHY_NO_DEV])
        self.assertEqual(self.check(cfg=cfg), [place.WHY_NO_DEV])
        self.assertIn('dev_drive', place.WHY_NO_DEV)


class AllowedTest(Base):
    def test_a_folder_and_a_path_to_make_on_the_dev_drive_pass(self):
        _write(self.d('area', 'a.txt'))
        _write(self.d('area', 'sub', 'b.txt'))
        self.assertEqual(self.check({'src': self.d('area'), 'dest': self.d('new', 'deeper')},
                                    [self.d('area'), self.d('new', 'deeper'), '/MIR']), [])

    def test_forward_slashes_and_quotes_pass(self):
        os.makedirs(self.d('area'))
        self.assertEqual(self.check(tokens=['"%s"' % self.d('area').replace('\\', '/')]), [])

    def test_options_are_judged_by_their_value_only(self):
        os.makedirs(self.d('logs'))
        self.assertEqual(self.check(tokens=['/MIR', '/R:3', '-o', '--level=2', '/W:0',
                                            '/LOG:' + self.d('logs', 'x.log'),
                                            '--out=' + self.d('logs')]), [])

    def test_a_value_with_no_path_character_is_not_judged(self):
        self.assertEqual(self.check({'mode': 'mirror'}, ['mirror']), [])

    def test_a_value_merely_holding_dots_is_not_judged(self):
        values = {'a': 'v1.2', 'b': 'a..b', 'c': '...x', 'd': 'x.'}
        tokens = ['v1.2', 'a..b', 'dest=a..b', '--level=1.5', 'x.']
        self.assertEqual(place.candidates(values, tokens), [])
        self.assertEqual(self.check(values, tokens), [])


class FormTest(Base):
    def test_a_path_with_no_drive_is_refused(self):
        for text in ('dd/out', 'out\\x', '/x/y', '\\x', 'C:foo', '\\\\srv\\share\\x',
                     '//srv/share/x', '\\\\?\\C:\\x', '\\\\.\\C:\\x', 'R:3'):
            with self.subTest(text=text):
                self.assertEqual(self.check({'p': text}),
                                 [place.WHY_RELATIVE % (text, self.dev)])

    def test_a_dot_or_dot_dot_is_refused(self):
        for text in ('..', '.', 'dest=..', 'dest=.'):
            with self.subTest(form='token', text=text):
                self.assertEqual(self.check(tokens=[text]),
                                 [place.WHY_RELATIVE % (text, self.dev)])
            with self.subTest(form='value', text=text):
                self.assertEqual(self.check({'p': text}),
                                 [place.WHY_RELATIVE % (text, self.dev)])
        for tok, val in (('--dest=..', '..'), ('/LOG:..', '..'), ('--out=.', '.')):
            with self.subTest(form='option', text=tok):
                self.assertEqual(self.check(tokens=[tok]),
                                 [place.WHY_RELATIVE % (val, self.dev)])

    def test_an_option_value_with_no_drive_is_refused(self):
        self.assertEqual(self.check(tokens=['--out=dd/x']),
                         [place.WHY_RELATIVE % ('dd/x', self.dev)])

    def test_off_the_dev_drive_is_refused(self):
        self.assertEqual(self.check({'p': self.other}), [place.WHY_OFF % (self.other, self.dev)])
        self.assertEqual(self.check(tokens=['/LOG:' + self.other]),
                         [place.WHY_OFF % (self.other, self.dev)])

    def test_dot_dot_out_of_the_dev_drive_is_refused(self):
        text = self.d('..', 'other')
        self.assertEqual(self.check({'p': text}), [place.WHY_OFF % (text, self.dev)])

    def test_a_sibling_sharing_the_prefix_is_off(self):
        text = self.dev + 'x'
        self.assertEqual(self.check({'p': text}), [place.WHY_OFF % (text, self.dev)])

    def test_the_dev_drive_root_is_refused(self):
        for text in (self.dev, self.dev + os.sep, self.d('a', '..')):
            with self.subTest(text=text):
                self.assertEqual(self.check({'p': text}), [place.WHY_ROOT % text])

    def test_case_does_not_matter(self):
        os.makedirs(self.d('area'))
        self.assertEqual(self.check({'p': self.d('area').upper()}), [])

    def test_each_path_is_judged_once(self):
        self.assertEqual(place.candidates({'b': 'x/y', 'a': 'mode'}, ['x/y', '/MIR', '"x/y"']),
                         ['x/y'])


class CheckoutTest(Base):
    def test_inside_a_checkout_is_refused(self):
        os.makedirs(self.d('repo', '.git'))
        os.makedirs(self.d('repo', 'src'))
        for text in (self.d('repo'), self.d('repo', 'src'), self.d('repo', 'new', 'x')):
            with self.subTest(text=text):
                self.assertEqual(self.check({'p': text}),
                                 [place.WHY_INSIDE % (text, self.d('repo'))])

    def test_inside_a_worktree_is_refused(self):
        _write(self.d('wt', '.git'), 'gitdir: elsewhere\n')
        text = self.d('wt', 'a')
        self.assertEqual(self.check({'p': text}), [place.WHY_INSIDE % (text, self.d('wt'))])

    def test_holding_a_checkout_is_refused(self):
        os.makedirs(self.d('area', 'deep', 'repo', '.git'))
        text = self.d('area')
        self.assertEqual(self.check({'p': text}),
                         [place.WHY_HOLDS % (text, self.d('area', 'deep', 'repo'))])

    def test_holding_a_worktree_is_refused(self):
        _write(self.d('area', 'wt', '.GIT'), 'gitdir: elsewhere\n')
        text = self.d('area')
        self.assertEqual(self.check({'p': text}),
                         [place.WHY_HOLDS % (text, self.d('area', 'wt'))])

    def test_a_link_below_is_refused(self):
        os.makedirs(self.d('area'))
        link = self.d('area', 'j')
        _winapi.CreateJunction(self.other, link)
        text = self.d('area')
        self.assertEqual(self.check({'p': text}), [place.WHY_LINK % (text, link)])

    def test_a_path_through_a_link_off_the_dev_drive_is_refused(self):
        _winapi.CreateJunction(self.other, self.d('j'))
        text = self.d('j', 'x')
        self.assertEqual(self.check({'p': text}), [place.WHY_OFF % (
            '%s (resolves to %s)' % (text, os.path.join(self.other, 'x')), self.dev)])

    def test_a_path_through_a_link_into_a_checkout_is_refused(self):
        repo = os.path.join(self.other, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        os.makedirs(os.path.join(repo, 'src'))
        cfg = dict(self.cfg, dev_drive=self.tmp)
        _winapi.CreateJunction(os.path.join(repo, 'src'), self.d('j'))
        text = self.d('j', 'x')
        self.assertEqual(self.check({'p': text}, cfg=cfg), [place.WHY_INSIDE % (text, repo)])

    def test_over_the_scan_cap_is_refused(self):
        for i in range(5):
            _write(self.d('big', 'f%d' % i))
        text = self.d('big')
        self.assertEqual(self.check({'p': text}, scan_cap=4), [place.WHY_CAP % (text, 4)])
        self.assertEqual(self.check({'p': text}, scan_cap=5), [])


class OwnerOnlyTest(Base):
    def test_the_owner_places_are_refused(self):
        home = self.d('home')
        cases = [
            (os.path.join(home, kuser.DIR_NAME), 'the machine config folder'),
            (os.path.join(home, kuser.DIR_NAME, 'hub', 'key'), 'the machine config folder'),
            (os.path.join(home, '.claude', 'machine-lock'), 'a host state folder'),
        ]
        for text, label in cases:
            with self.subTest(text=text):
                whys = self.check({'p': text}, home=home)
                self.assertTrue(whys and all(w.startswith('%s: owner-only (%s ' % (text, label))
                                             for w in whys), whys)

    def test_the_retired_config_folder_is_refused_too(self):
        home = self.d('home')
        loc = kuser.locate(home)
        text = os.path.dirname(loc.old)
        self.assertNotEqual(text, os.path.dirname(loc.new))
        self.assertEqual(self.check({'p': text}, home=home),
                         [place.WHY_OWNER % (text, 'the machine config folder', text)])

    def test_the_kit_the_release_store_and_the_ledger_are_refused(self):
        kit, store = self.d('kit'), self.d('release')
        cfg = dict(self.cfg, plugin={'export_dir': store}, ledger_dir=self.d('ledgers'),
                   ledger_path=self.d('one', 'ledger.md'), hub={'store': self.d('hubstore')},
                   pm_data={'path': self.d('pmdata')})
        cases = [(self.d('kit', 'tools'), 'the running kit', kit),
                 (self.d('release', 'abc'), 'the kit release store', store),
                 (self.d('ledgers', 'kit.md'), 'the ledger folder', self.d('ledgers')),
                 (self.d('one', 'ledger.md'), 'the ledger', self.d('one', 'ledger.md')),
                 (self.d('hubstore'), 'the hub store', self.d('hubstore')),
                 (self.d('pmdata', 'x'), 'the PM data worktree', self.d('pmdata'))]
        for text, label, where in cases:
            with self.subTest(text=text):
                self.assertEqual(self.check({'p': text}, cfg=cfg, kit=kit),
                                 [place.WHY_OWNER % (text, label, where)])

    def test_holding_an_owner_place_is_refused(self):
        store = self.d('area', 'pm-kit', 'plugin')
        cfg = dict(self.cfg, plugin={'export_dir': store})
        text = self.d('area')
        self.assertEqual(self.check({'p': text}, cfg=cfg),
                         [place.WHY_HOLDS_OWNER % (text, 'the kit release store', store)])

    def test_an_unplaceable_release_store_refuses_every_path(self):
        cfg = dict(self.cfg, plugin=None)
        whys = self.check({'p': self.d('x')}, cfg=cfg, environ={})
        self.assertEqual(len(whys), 1)
        self.assertIn('kit release store cannot be placed', whys[0])
        self.assertEqual(self.check({'p': self.d('x')}, cfg=cfg,
                                    environ={'LOCALAPPDATA': self.other}), [])

    def test_every_why_is_given(self):
        os.makedirs(self.d('repo', '.git'))
        whys = self.check({'a': 'dd/x', 'b': self.other}, [self.d('repo', 'x')])
        self.assertEqual(whys, [place.WHY_RELATIVE % ('dd/x', self.dev),
                                place.WHY_OFF % (self.other, self.dev),
                                place.WHY_INSIDE % (self.d('repo', 'x'), self.d('repo'))])


if __name__ == '__main__':
    unittest.main()
