"""The per-repository cap (docs/backlog.md, verb table; docs/kit-json.md `backlog.cap`): `next`
offers up to it and `pull` checks against the same value (opener.repo_cap, plan.Context.cap),
5 when `.kit.json` names none; `next --cap` overrides it for one call; a value that is not a
whole number >= 1 is refused. In the PM data worktree the cap is the main checkout's."""
import json
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import pmdata
from kitlib.backlog import offer as omod
from kitlib.backlog import opener
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal

from tests.backlog import test_cli
from tests.backlog.helpers import StoreCase, kit_json_ref, record_text

WORK = 'tools/wk'


class RepoCapCli(unittest.TestCase):
    """Through tools/backlog.py, on a ref store holding R00..R10 (rank order = id order)."""
    setUp_cli = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def setUp(self):
        self.setUp_cli()
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap, {'R%02d' % i: record_text(id='R%02d' % i, priority='2',
                                                        priority_by='agent',
                                                        scope='area/r%02d/**' % i)
                               for i in range(11)}, 'seed\n')
        self.repo.git('branch', WORK)

    def set_cap(self, value):
        kit_json_ref(self.repo, {'backlog': {'cap': value}})

    def pull(self, rid):
        return self.run_tool('--as', WORK, 'pull', rid)

    def store_tip(self):
        return self.repo.git('ls-remote', 'origin', smod.STORE_REF)

    def test_the_default_cap_stays_five(self):
        self.assertEqual(kconfig.load(self.repo.root).section('backlog')['cap'], omod.DEFAULT_CAP)
        self.assertEqual(omod.DEFAULT_CAP, 5)
        self.assertIn('NEXT (5 offered)', self.ok('next'))
        rc, out = self.pull('R05')
        self.assertEqual(rc, 2, out)
        self.assertIn("`R05` is not in `next`'s offer", out)

    def test_a_cap_of_ten_lets_pull_take_records_six_through_ten(self):
        rc, out = self.pull('R05')
        self.assertEqual(rc, 2, out)                 # refused under the default
        self.set_cap(10)
        for i in range(5, 10):
            rc, out = self.pull('R%02d' % i)
            self.assertEqual(rc, 0, out)
            self.assertIn('pulled_by:  %s' % WORK, out)
            self.assertNotIn('preempt', out)         # in order, not a pre-emption

    def test_next_and_pull_agree_on_the_cap(self):
        self.set_cap(10)
        out = self.ok('next')
        self.assertIn('NEXT (10 offered)', out)
        self.assertIn('R09', out.split('passed over')[0])
        self.assertIn('passed over R10 (rank 11): not reached - the cap was already full', out)
        tip = self.store_tip()
        rc, out = self.pull('R10')
        self.assertEqual(rc, 2, out)
        self.assertIn("`R10` is not in `next`'s offer", out)
        self.assertEqual(self.store_tip(), tip)
        rc, out = self.pull('R09')
        self.assertEqual(rc, 0, out)

    def test_next_cap_still_overrides_for_that_call(self):
        self.set_cap(10)
        self.assertIn('NEXT (2 offered)', self.ok('next', '--cap', '2'))
        self.assertIn('NEXT (10 offered)', self.ok('next'))

    def test_a_cap_that_is_not_a_positive_whole_number_is_refused(self):
        for bad in (0, -3, '10', True, 2.5):
            self.set_cap(bad)
            tip = self.store_tip()
            for args in (('next',), ('--as', WORK, 'pull', 'R00')):
                rc, out = self.run_tool(*args)
                self.assertNotEqual(rc, 0, (bad, args, out))
                self.assertIn('"backlog".cap must be a whole number >= 1', out, (bad, out))
            self.assertEqual(self.store_tip(), tip)

    def test_next_cap_below_one_is_usage(self):
        rc, out = self.run_tool('next', '--cap', '0')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: ', out)


class FakeConfig(object):
    def __init__(self, cap, root='repo'):
        self.root = root
        self._cap = cap

    def section(self, name):
        return {'cap': self._cap}


class RepoCapValue(StoreCase):
    """opener.repo_cap: the checkout's own `.kit.json`, except in the PM data worktree."""

    def write_cap(self, root, cap):
        data = {'backlog': {'store': 'ref', 'cap': cap}}
        self.repo.write('.kit.json', json.dumps(data) + '\n', root=root)

    def test_a_plain_checkout_reads_its_own_cap(self):
        self.write_cap(self.repo.root, 10)
        self.assertEqual(opener.repo_cap(self.repo.root, kconfig.load(self.repo.root)), 10)

    def test_a_value_the_schema_did_not_check_is_refused(self):
        for bad in (0, -1, '7', True, None):
            with self.assertRaises(Refusal) as cm:
                opener.repo_cap(self.repo.root, FakeConfig(bad))
            self.assertIn('must be a whole number >= 1', str(cm.exception))

    def pm_worktree(self):
        tree = self.repo.git('rev-parse', 'main^{tree}')
        root = self.repo.git('commit-tree', tree, '-m', 'PM data root')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)
        return self.repo.worktree('pm', branch='pmdata')

    def test_the_pm_data_worktree_reads_the_main_checkouts_cap(self):
        pm = self.pm_worktree()
        self.write_cap(self.repo.root, 10)
        self.write_cap(pm, 3)                     # the data branch's own value is not the repo's
        self.assertEqual(opener.repo_cap(pm, kconfig.load(pm)), 10)
        self.assertEqual(opener.repo_cap(self.repo.root, kconfig.load(self.repo.root)), 10)

    def test_an_unreadable_main_checkout_configuration_is_refused_not_defaulted(self):
        pm = self.pm_worktree()
        cfg = kconfig.load(pm)
        with mock.patch.object(kconfig, 'load', side_effect=kconfig.ConfigError('bad file')):
            with self.assertRaises(Refusal) as cm:
                opener.repo_cap(pm, cfg)
        self.assertIn('bad file', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
