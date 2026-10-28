"""The per-repository cap (docs/backlog.md, verb table; docs/kit-json.md `backlog.cap`): `next`
offers up to it and `pull` checks against the same value (opener.repo_cap, plan.Context.cap),
5 when `.kit.json` names none; `next --cap` overrides it for one call; a value that is not a
whole number >= 1 is refused. Every worktree, the PM data one included, reads the main checkout's
cap, and only a verb that makes an offer reads it."""
import json
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import pmdata
from kitlib.backlog import offer as omod
from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Failure, Refusal

from tests import inproc
from tests.backlog import test_cli
from tests.backlog.helpers import ADD_FLAGS, StoreCase, kit_json_ref, record_text

WORK = 'tools/wk'
WT_BRANCH = 'tools/wt'


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

    # --- from a worker worktree: the cap is always the main checkout's ---------------------------

    def run_in(self, root, *args):
        rc, out, err = inproc.run_main(test_cli.BACKLOG.main, ['-C', root] + list(args),
                                       self.env, root)
        return rc, out + err

    def test_a_worktree_cut_before_the_cap_reads_the_main_checkouts(self):
        wt = self.repo.worktree('wt', branch=WT_BRANCH)  # its .kit.json names no cap: 5
        self.set_cap(10)                                  # the owner sets 10 on main afterwards
        rc, out = self.run_in(wt, 'next')
        self.assertEqual(rc, 0, out)
        self.assertIn('NEXT (10 offered)', out)
        for i in range(5, 10):
            rc, out = self.run_in(wt, 'pull', 'R%02d' % i)
            self.assertEqual(rc, 0, out)
            self.assertIn('pulled_by:  %s' % WT_BRANCH, out)

    def test_a_verb_that_makes_no_offer_never_reads_the_cap(self):
        wt = self.repo.worktree('wt', branch=WT_BRANCH)
        # The main checkout's file now fails to load; the worktree's own is fine.
        self.repo.write('.kit.json', json.dumps({'backlog': {'store': 'ref', 'cap': 0}}) + '\n')
        rc, out = self.run_in(wt, 'show', 'R00')
        self.assertEqual(rc, 0, out)
        self.assertIn('R00', out)
        rc, out = self.run_in(wt, 'add', 'NEW', '--summary', 'a new record to add', '--scope',
                              'area/new/**', *ADD_FLAGS)
        self.assertEqual(rc, 0, out)
        self.assertIn('NEXT (11 offered, --all: no cap)', self.run_in(wt, 'next', '--all')[1])
        for args in (('next',), ('pull', 'R00')):
            rc, out = self.run_in(wt, *args)
            self.assertEqual(rc, 1, out)
            self.assertIn('cannot read the backlog cap from the main checkout', out)
            self.assertIn('"backlog".cap must be a whole number >= 1', out)

    def test_pull_takes_a_record_only_the_offer_without_the_branch_holds(self):
        # `also` (plan.current_offer): the offer as the main checkout prints it. WT_BRANCH holds
        # R01 and its diff touches R00's scope, so the main checkout passes R00 over (it folds into
        # that branch) and offers R02..R11, while the branch's own offer keeps R00 to bundle and
        # stops at R10. R11 is in order only through `also`, at the repository's cap of 10.
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        backend.publish(smod.Store(backend, rmod.Rules()).load(),
                        {'R11': record_text(id='R11', priority='2', priority_by='agent',
                                            scope='area/r11/**')}, 'seed\n')
        self.set_cap(10)
        wt = self.repo.worktree('wt', branch=WT_BRANCH)
        self.repo.write('area/r00/x.txt', 'work\n', root=wt)
        self.repo.commit('touch R00', 'area/r00/x.txt', cwd=wt)
        rc, out = self.run_in(wt, 'pull', 'R01')
        self.assertEqual(rc, 0, out)
        out = self.run_in(wt, 'next')[1]
        self.assertIn('passed over R11 (rank', out)       # not in the branch's own offer
        rc, out = self.run_in(wt, 'pull', 'R11')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('preempt', out)

    def test_next_cap_below_one_is_usage(self):
        rc, out = self.run_tool('next', '--cap', '0')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: ', out)


class RepoCapValue(StoreCase):
    """opener.repo_cap: the main checkout's `.kit.json`, whatever worktree asks."""

    def write_cap(self, root, cap):
        data = {'backlog': {'store': 'ref', 'cap': cap}}
        self.repo.write('.kit.json', json.dumps(data) + '\n', root=root)

    def test_the_main_checkout_reads_its_own_cap(self):
        self.write_cap(self.repo.root, 10)
        self.assertEqual(opener.repo_cap(self.repo.root), 10)

    def test_a_worker_worktree_reads_the_main_checkouts_cap(self):
        wt = self.repo.worktree('wt')
        self.write_cap(self.repo.root, 10)
        self.write_cap(wt, 5)                     # the branch's own value is not the repo's
        self.assertEqual(opener.repo_cap(wt), 10)

    def test_a_value_the_schema_did_not_check_is_refused(self):
        for bad in (0, -1, '7', True, None):
            with self.assertRaises(Refusal) as cm:
                opener.checked_cap(bad, 'repo')
            self.assertIn('must be a whole number >= 1', str(cm.exception))
        self.assertEqual(opener.checked_cap(1, 'repo'), 1)

    def pm_worktree(self):
        tree = self.repo.git('rev-parse', 'main^{tree}')
        root = self.repo.git('commit-tree', tree, '-m', 'PM data root')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)
        return self.repo.worktree('pm', branch='pmdata')

    def test_the_pm_data_worktree_reads_the_main_checkouts_cap(self):
        pm = self.pm_worktree()
        self.write_cap(self.repo.root, 10)
        self.write_cap(pm, 3)                     # the data branch's own value is not the repo's
        self.assertEqual(opener.repo_cap(pm), 10)

    def test_an_unreadable_main_checkout_configuration_is_an_error_not_a_default(self):
        pm = self.pm_worktree()
        with mock.patch.object(kconfig, 'load', side_effect=kconfig.ConfigError('bad file')):
            with self.assertRaises(Failure) as cm:
                opener.repo_cap(pm)
        self.assertIn('bad file', str(cm.exception))

    def test_the_context_reads_a_callable_cap_once_and_only_when_asked(self):
        calls = []
        ctx = pmod.Context(self.store, self.repo.root, cap=lambda: calls.append(1) or 7)
        self.assertEqual(calls, [])
        self.assertEqual((ctx.cap, ctx.cap), (7, 7))
        self.assertEqual(calls, [1])
        self.assertEqual(pmod.Context(self.store, self.repo.root).cap, omod.DEFAULT_CAP)


if __name__ == '__main__':
    unittest.main()
