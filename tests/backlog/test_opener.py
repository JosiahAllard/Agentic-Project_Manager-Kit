"""kitlib.backlog.opener in process (docs/backlog.md): open_store's defaults against a ref store on
a temporary repository with a bare remote, open_repo's Failure outside a repository, the verbs
`planner` maps (block's reason, unblock's one blocker, retarget and its dry run), and the
`kitlib.backlog.open_store` name."""
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import kitlib.backlog as backlog
from kitlib.backlog import errors, opener
from kitlib.backlog import ledger as lmod
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.backlog.helpers import StoreCase, TODAY, rec, record_text
from tests.gitfixture import TempRepo, fixture_env, rmtree

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WHY = 'waiting for the live client to ship its next build'
OLD, NEW = 'docs/guide', 'docs/design/guide'


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class OpenStore(unittest.TestCase):
    """FOO is on the remote's store and the local cache of it is gone: only a fetch finds it."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        home = self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        # An empty home: no machine config is read from the machine running the tests. The
        # library's git inherits os.environ.
        env = mock.patch.dict(os.environ, fixture_env({'USERPROFILE': home, 'HOME': home}),
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap, {'FOO': record_text()}, 'seed\n')
        self.repo.git('update-ref', '-d', smod.STORE_REF)

    def test_the_defaults_fetch_with_no_session_and_no_branch(self):
        ctx = opener.open_store(self.repo.root)
        self.assertIsNotNone(ctx.store.load().get('FOO'), 'the default open did not fetch')
        self.assertIs(ctx.store.backend.fetch, True)
        self.assertIsNone(ctx.session)
        self.assertIsNone(ctx.store.session)
        self.assertIsNone(ctx.branch)
        self.assertTrue(same_path(ctx.root, self.repo.root), ctx.root)
        self.assertEqual((ctx.remote, ctx.main_branch), ('origin', 'main'))
        self.assertEqual(ctx.current_branch(), 'main')

    def test_the_context_is_built_without_a_ledger(self):
        self.assertFalse(hasattr(lmod, 'Ledger'))
        with mock.patch.object(lmod, 'read_entries') as read_entries:
            ctx = opener.open_store(self.repo.root, fetch=False)
        read_entries.assert_not_called()
        self.assertIsInstance(ctx, pmod.Context)
        self.assertFalse(hasattr(ctx, 'ledger'))
        self.assertFalse(hasattr(ctx.store, 'ledger'))
        top, cfg, rules = opener.open_repo(self.repo.root)
        self.assertTrue(same_path(top, self.repo.root), top)
        self.assertEqual((cfg.remote, cfg.main_branch), ('origin', 'main'))
        self.assertIsInstance(rules, rmod.Rules)

    def test_each_argument_reaches_the_context(self):
        ctx = opener.open_store(self.repo.root, fetch=False, session='S-1', branch='tools/wk')
        self.assertIsNone(ctx.store.load().get('FOO'), 'fetch=False fetched')
        self.assertIs(ctx.store.backend.fetch, False)
        self.assertEqual(ctx.session, 'S-1')
        self.assertEqual(ctx.branch, 'tools/wk')

    def test_a_folder_that_is_no_repository_is_a_failure(self):
        with self.assertRaises(errors.Failure):
            opener.open_repo(os.path.join(self.home, 'no-such-dir'))


def args(verb, **kw):
    """The object tools/backlog.py's parser hands `planner`: `verb` and that verb's own names."""
    return types.SimpleNamespace(verb=verb, **kw)


class Planner(StoreCase):
    """Each verb main's entry point plans, planned through opener.planner and written."""
    remote = False

    def write(self, a):
        change, _tip = self.store.write(opener.planner(a, self.ctx))
        return change

    def test_block_carries_the_reason(self):
        self.seed(rec('FOO'))
        change = self.write(args('block', id='FOO', on=['live-client'], owner_ask=None,
                                 ruling=None, reason=WHY, until=None))
        self.assertEqual(change.subject, 'backlog: block FOO on live-client: %s' % WHY)
        self.assertTrue(self.get('FOO').body.rstrip('\n').endswith(
            'Blocked on live-client (%s): %s' % (TODAY, WHY)))

    def test_block_carries_the_until(self):
        self.seed(rec('FOO'))
        self.write(args('block', id='FOO', on=['live-client'], owner_ask=None, ruling=None,
                        reason=None, until='todo:T-7 done'))
        self.assertEqual(self.get('FOO').all('until'), ['live-client todo:T-7 done'])

    def test_unblock_clears_the_named_blocker_with_its_reason(self):
        self.seed(rec('FOO', state='blocked', blocked_on=['item:BAR', 'live-client']), rec('BAR'))
        change = self.write(args('unblock', id='FOO', ruling=None, on='item:BAR', reason=WHY))
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', ['live-client']))
        self.assertEqual(change.subject, 'backlog: unblock FOO from item:BAR: %s' % WHY)

    def test_retarget_rewrites_the_moved_path(self):
        self.seed(rec('FOO', scope=OLD + '/**'), rec('BAR'))
        change = self.write(args('retarget', old=OLD, new=NEW))
        self.assertEqual(sorted(change.writes), ['FOO'])
        self.assertEqual(self.get('FOO').all('scope'), [NEW + '/**'])
        self.assertEqual(change.subject, 'backlog: retarget %s -> %s in 1 record(s)' % (OLD, NEW))

    def test_a_verb_that_is_no_write_is_usage_and_plans_nothing(self):
        # `why` and `ruling` are given so that a fall-through to `retire` would delete FOO.
        self.seed(rec('FOO'))
        for verb in ('show', 'no-such-verb'):
            with self.subTest(verb=verb):
                with self.assertRaises(errors.Usage) as caught:
                    self.write(args(verb, id='FOO', why=WHY, ruling=None))
                self.assertIn('`%s` is not a write verb' % verb, str(caught.exception))
                self.assertIsNotNone(self.get('FOO'), '%s deleted the record' % verb)

    def test_retire_is_still_planned(self):
        self.seed(rec('FOO'))
        self.write(args('retire', id='FOO', why=WHY, ruling=None))
        self.assertIsNone(self.get('FOO'))

    def test_retarget_dry_refuses_a_store_with_an_unreadable_record(self):
        self.seed(rec('FOO', scope=OLD + '/**'), {'BAD': 'no separator here\n'})
        with self.assertRaises(errors.Failure) as caught:
            opener.retarget_dry(args('retarget', old=OLD, new=NEW), self.ctx)
        self.assertIn('unreadable (BAD)', str(caught.exception))

    def test_retarget_dry_checks_the_change_it_planned(self):
        # No retarget of valid paths is known to add a finding, so the check itself is replaced.
        self.seed(rec('FOO', scope=OLD + '/**'))
        with mock.patch.object(smod, 'check_change',
                               side_effect=errors.Usage('left invalid')) as check:
            with self.assertRaises(errors.Usage) as caught:
                opener.retarget_dry(args('retarget', old=OLD, new=NEW), self.ctx)
        self.assertEqual(str(caught.exception), 'left invalid')
        snap, change = check.call_args.args
        self.assertIsNotNone(snap.get('FOO'))
        self.assertEqual(sorted(change.writes), ['FOO'])

    def test_retarget_dry_plans_the_same_change_and_writes_nothing(self):
        self.seed(rec('FOO', scope=OLD + '/**'))
        tip = self.remote_tip()
        change = opener.retarget_dry(args('retarget', old=OLD, new=NEW), self.ctx)
        self.assertEqual(sorted(change.writes), ['FOO'])
        self.assertIn(NEW + '/**', change.writes['FOO'])
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.get('FOO').all('scope'), [OLD + '/**'])

    def test_retarget_dry_prints_the_machine_config_warning_as_the_write_does(self):
        self.seed(rec('FOO', scope=OLD + '/**'))
        note = 'WARNING: machine config X could not be read'
        with mock.patch.object(pmod.machinepaths, 'folders', return_value=((), note)):
            change = opener.retarget_dry(args('retarget', old=OLD, new=NEW), self.ctx)
            self.assertEqual(change.lines.count(note), 1, change.lines)
            written, _tip = self.store.write(
                lambda s: opener.retmod.plan_retarget(s, self.ctx, OLD, NEW))
        self.assertEqual(written.lines.count(note), 1, written.lines)

    def test_retarget_dry_refuses_a_bad_path_as_the_write_does(self):
        self.seed(rec('FOO', scope=OLD + '/**'))
        with self.assertRaises(errors.Usage):
            opener.retarget_dry(args('retarget', old=OLD, new=OLD), self.ctx)


class ReExport(unittest.TestCase):

    def test_the_package_name_is_the_openers_own_function(self):
        from kitlib.backlog import open_store
        self.assertIs(open_store, opener.open_store)
        self.assertIs(backlog.open_store, opener.open_store)

    def test_another_name_is_still_an_attribute_error(self):
        with self.assertRaises(AttributeError):
            backlog.no_such_name

    def test_importing_another_module_does_not_load_the_opener(self):
        code = ('import sys, kitlib.backlog.scope; '
                'print("kitlib.backlog.opener" in sys.modules, "kitlib.config" in sys.modules)')
        p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           encoding='utf-8', cwd=ROOT, timeout=120)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, 'False False'), p.stderr)

    def test_the_first_use_keeps_the_function_in_the_package(self):
        code = ('import kitlib.backlog as b; before = "open_store" in vars(b); f = b.open_store; '
                'import kitlib.backlog.opener as o; '
                'print(before, vars(b).get("open_store") is o.open_store is f)')
        p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           encoding='utf-8', cwd=ROOT, timeout=120)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, 'False True'), p.stderr)


if __name__ == '__main__':
    unittest.main()
