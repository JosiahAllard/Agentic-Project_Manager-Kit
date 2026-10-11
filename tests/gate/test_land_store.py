"""kitlib.gate.land and the backlog store (docs/backlog-store.md "Lifecycle changes", "Merge
safety"): a landing closes the records its branch still holds pulled once the push is verified, a
close that fails is a WARNING and the landing stands, and a branch whose history holds the store's
root commit is refused before any merge. Scratch repository and bare remote in temp only."""
import json
import os
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import gate as G
from kitlib import pmdata
from kitlib.backlog import at_landing
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.git import cmd, refs
from tests.backlog.helpers import priced
from tests.gate import test_land
from tests.gitfixture import fixture_env
from tests.realgit import real_git

BRANCH = test_land.BRANCH
TODAY = '2026-10-07'


def pulled(rid, by=BRANCH, **over):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=by, **over)


class StoreLandCase(test_land.LandCase):
    """A LandCase whose store layer (which inherits os.environ) runs on the fixture's git
    environment, with helpers to seed and read the store on the bare remote."""
    STORE = 'ref'

    def setUp(self):
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        test_land.LandCase.setUp(self)

    def backend(self):
        if self.STORE == 'branch':
            return smod.RefBackend(self.repo.root, 'origin', 'refs/heads/backlog',
                                   'refs/remotes/origin/backlog', no_remote_ref='refs/heads/backlog')
        return smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)

    @property
    def remote_ref(self):
        return 'refs/heads/backlog' if self.STORE == 'branch' else smod.STORE_REF

    def seed(self, *records, message='seed\n'):
        writes = {}
        for r in records:
            writes.update(r)
        backend = self.backend()
        return backend.publish(backend.load(rmod.Rules()), writes, message)

    def stems(self):
        return sorted(r.stem for r in self.backend().load(rmod.Rules()).records)

    def remote_store(self):
        out = self.repo.git('ls-remote', 'origin', self.remote_ref)
        return out.split('\t')[0] if out else ''

    def subjects(self):
        return self.repo.git('log', '--format=%s', self.remote_store()).split('\n')


class ClosesAtLanding(StoreLandCase):
    def test_a_landing_closes_the_records_its_branch_holds_pulled(self):
        self.seed(pulled('FOO'), pulled('BAR'), pulled('QUX', by='kit/other'), priced('BAZ'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        merge = out.merge
        self.assertEqual(self.remote_main(), merge)
        self.assertEqual(self.stems(), ['BAZ', 'QUX'])
        # One commit per record, so `git log --grep "^closes FOO: "` finds each close.
        self.assertEqual(sorted(self.subjects()[:2]),
                         ['closes BAR: landed %s' % merge, 'closes FOO: landed %s' % merge])
        landed = [i for i, l in enumerate(out.lines) if l.startswith('LANDED ')]
        self.assertEqual(len(landed), 1, out.lines)
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 2, out.lines)
        self.assertTrue(all(out.lines.index(l) > landed[0] for l in closed), out.lines)
        self.assertFalse([l for l in out.lines if l.startswith('WARNING')], out.lines)
        self.assertIsNone(self.lock().read())

    def test_nothing_pulled_writes_nothing(self):
        before = self.seed(priced('BAZ'), pulled('QUX', by='kit/other'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_store(), before)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)

    def test_no_store_on_the_remote_creates_none_and_adds_no_refspec(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)
        self.assertEqual(self.remote_store(), '')
        self.assertEqual(self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF,
                                       check=False), '')
        fetch = self.repo.git('config', '--get-all', 'remote.origin.fetch', check=False)
        self.assertNotIn('backlog', fetch)

    def test_a_close_the_store_refuses_is_a_warning_and_the_landing_stands(self):
        # An owner-held record closes only with --ruling: the landing cannot close it.
        self.seed(pulled('FOO', priority='-1', priority_by='-', owner_ask='may this ship?'),
                  pulled('BAR'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        merge = out.merge
        self.assertEqual(self.remote_main(), merge)
        self.assertEqual(self.stems(), ['FOO'])
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('FOO', warn[0])
        self.assertIn('owner-held', warn[0])
        # A backlog write refuses in the main checkout, so the command names a worktree, never
        # the main checkout the landing ran in.
        self.assertIn('backlog.py" -C "%s" close FOO --reason "landed %s"'
                      % (at_landing.WORKTREE, merge), warn[0])
        self.assertNotIn(self.repo.root, warn[0])
        self.assertEqual(len([l for l in out.lines if l.startswith('LANDED ')]), 1, out.lines)
        self.assertIsNone(self.lock().read())

    def test_an_exception_while_closing_is_a_warning_and_the_landing_stands(self):
        self.seed(pulled('FOO'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(at_landing, 'close_landed', side_effect=RuntimeError('boom')):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('RuntimeError: boom', warn[0])
        self.assertIn('backlog.py" -C "%s" landable %s' % (self.repo.root, BRANCH), warn[0])
        self.assertEqual(self.stems(), ['FOO'])
        self.assertIsNone(self.lock().read())

    def test_ctrl_c_while_closing_is_a_warning_and_the_merge_is_not_undone(self):
        self.seed(pulled('FOO'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(at_landing, 'close_landed', side_effect=KeyboardInterrupt):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.main_now(), out.merge)            # never undone after the push
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('Ctrl+C', warn[0])
        self.assertIsNone(self.lock().read())

    def test_a_landing_that_does_not_land_closes_nothing(self):
        before = self.seed(pulled('FOO'))
        _, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.remote_store(), before)
        self.assertFalse([l for l in out.lines if l.startswith('closed ')], out.lines)


class ClosesOnABranchStore(StoreLandCase):
    STORE = 'branch'
    KIT = dict(test_land.LandCase.KIT, backlog={'store': 'branch'})

    def test_the_backlog_branch_store_is_closed_too(self):
        self.seed(pulled('FOO'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), [])
        self.assertEqual(self.subjects()[0], 'closes FOO: landed %s' % out.merge)


class MainStoreUntouched(StoreLandCase):
    KIT = dict(test_land.LandCase.KIT, backlog={'store': 'main'})

    def test_a_main_store_is_left_to_the_landing(self):
        before = self.seed(pulled('FOO'))          # a stray ref store the config does not name
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_store(), before)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)


class AncestryRefusal(StoreLandCase):
    def merged_store_branch(self, store_rev):
        """kit/feature with the store's history merged into it (a forced unrelated merge)."""
        wt = self.repo.worktree('feature')
        self.repo.write('a.py', 'x = 1\n', root=wt)
        self.repo.commit('work', 'a.py', cwd=wt)
        self.repo.git('merge', '-q', '--allow-unrelated-histories', '--no-edit', store_rev, cwd=wt)
        return self.repo.git('rev-parse', 'HEAD', cwd=wt)

    def test_a_branch_holding_the_store_root_is_refused_before_any_merge(self):
        root = self.seed(priced('BAZ'))
        tip = self.merged_store_branch(root)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(len(out.lines), 1, out.lines)
        self.assertIn('backlog store', out.lines[0])
        self.assertIn(root[:10], out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_a_later_store_commit_carries_the_root_in(self):
        root = self.seed(priced('BAZ'))
        later = self.seed(priced('QUX'), message='second\n')
        self.assertNotEqual(root, later)
        tip = self.merged_store_branch(later)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn(root[:10], out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_the_remote_store_is_read_when_the_local_copy_is_gone(self):
        root = self.seed(priced('BAZ'))
        tip = self.merged_store_branch(root)
        self.repo.git('update-ref', '-d', smod.STORE_REF)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn(root[:10], out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_a_branch_without_the_store_history_lands(self):
        self.seed(pulled('FOO', by='kit/other'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)


class AncestryRefusalBranchStore(AncestryRefusal):
    STORE = 'branch'
    KIT = dict(test_land.LandCase.KIT, backlog={'store': 'branch'})

    def test_the_remote_store_is_read_when_the_local_copy_is_gone(self):
        root = self.seed(priced('BAZ'))
        tip = self.merged_store_branch(root)
        self.repo.git('update-ref', '-d', 'refs/remotes/origin/backlog')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn(root[:10], out.lines[0])
        self.assert_untouched(out, G.REFUSED)


class PmDataAncestry(StoreLandCase):
    """ancestry_refusal also refuses a branch whose history holds the PM data branch's recorded
    root (kit-pm-data.json in the git common dir; docs/pm-data-branch.md "Pre-push guard"),
    checked before the backlog store, so a store that takes no part refuses it too."""

    def setUp(self):
        StoreLandCase.setUp(self)
        # an orphan root (pm_data.py init uses the empty tree, which the fake git does not know)
        tree = self.repo.git('rev-parse', self.main + '^{tree}')
        self.root = self.repo.git('commit-tree', tree, '-m', 'PM data root')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.root)

    def merged_root_branch(self):
        wt = self.repo.worktree('feature')
        self.repo.write('a.py', 'x = 1\n', root=wt)
        self.repo.commit('work', 'a.py', cwd=wt)
        self.repo.git('merge', '-q', '--allow-unrelated-histories', '--no-edit', self.root, cwd=wt)
        return self.repo.git('rev-parse', 'HEAD', cwd=wt)

    def test_ancestry_refusal_refuses_a_branch_holding_the_pmdata_root(self):
        tip = self.merged_root_branch()
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(len(out.lines), 1, out.lines)
        self.assertIn('PM data root commit %s' % self.root[:10], out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_a_branch_without_the_root_lands(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_an_unreadable_record_or_a_missing_root_refuses(self):
        cfg = kconfig.load_at(self.repo.root, self.main)
        path = pmdata.record_path(self.repo.common_dir())
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"branch": "pmdata"}')
        why = at_landing.ancestry_refusal(self.repo.root, cfg, self.main)
        self.assertIn('cannot read the PM data record', why)
        self.assertIn(pmdata.FILE_NAME, why)
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', '1' * 40)
        why = at_landing.ancestry_refusal(self.repo.root, cfg, self.main)
        self.assertIn('PM data root commit 1111111111 that', why)
        self.assertIn('is not in this clone', why)

    def test_an_undecidable_ancestry_refuses(self):
        cfg = kconfig.load_at(self.repo.root, self.main)
        with mock.patch.object(refs, 'is_ancestor', return_value=(None, 'boom')):
            why = at_landing.ancestry_refusal(self.repo.root, cfg, self.main)
        self.assertIn('cannot tell whether the branch holds the PM data root commit', why)
        self.assertIn('boom', why)

    def test_a_git_directory_git_cannot_name_refuses(self):
        cfg = kconfig.load_at(self.repo.root, self.main)
        with mock.patch.object(at_landing.gitrepo, 'common_dir', return_value=(None, 'git timed out')):
            why = at_landing.ancestry_refusal(self.repo.root, cfg, self.main)
        self.assertEqual(why, 'cannot find the git directory to look for the PM data record: git timed out')


class PmDataAncestryFolderStore(PmDataAncestry):
    """The same with a folder store, whose backend takes no part in ancestry_refusal."""
    KIT = dict(test_land.LandCase.KIT, backlog={'store': 'docs/tasks'})

    def test_the_folder_store_has_no_backend_here(self):
        cfg = kconfig.load_at(self.repo.root, self.main)
        self.assertIsNone(at_landing.ref_store(self.repo.root, cfg))


class PmDataClose(StoreLandCase):
    """A landing closes what the branch holds pulled in the PM data branch's store too: a folder
    store in the worktree that has the recorded data branch checked out (kitlib.pmdata), where
    the PM keeps this repository's records while the configured store stays empty."""

    def setUp(self):
        StoreLandCase.setUp(self)
        tree = self.repo.git('rev-parse', self.main + '^{tree}')
        root = self.repo.git('commit-tree', tree, '-m', 'PM data root')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)

    def pm_worktree(self, *records):
        wt = self.repo.worktree('pm', branch='pmdata')
        with open(os.path.join(wt, '.kit.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        cfg['backlog'] = {'store': 'kit/backlog'}
        self.repo.write('.kit.json', json.dumps(cfg, indent=1) + '\n', root=wt)
        for r in records:
            for stem, text in r.items():
                self.repo.write('kit/backlog/%s.md' % stem, text, root=wt)
        return wt

    def record(self, wt, rid):
        return os.path.join(wt, 'kit', 'backlog', rid + '.md')

    def pm_root(self, wt):
        """The PM data worktree as git spells it (what the printed commands name): `wt`."""
        found, why = at_landing.pm_data_root(self.repo.root)
        self.assertIsNotNone(found, why)
        self.assertTrue(os.path.samefile(found, wt), (found, wt))
        return found

    def land(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def test_a_landing_closes_the_records_the_branch_holds_in_the_pm_data_store(self):
        wt = self.pm_worktree(pulled('FOO'), pulled('QUX', by='kit/other'), priced('BAZ'))
        out = self.land()
        self.assertFalse(os.path.exists(self.record(wt, 'FOO')), out.lines)
        self.assertTrue(os.path.exists(self.record(wt, 'QUX')))
        self.assertTrue(os.path.exists(self.record(wt, 'BAZ')))
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 1, out.lines)
        self.assertIn('closed FOO in the backlog store kit/backlog in ', closed[0])
        self.assertIn('landed %s' % out.merge, closed[0])
        self.assertFalse([l for l in out.lines if l.startswith('WARNING')], out.lines)
        landed = [i for i, l in enumerate(out.lines) if l.startswith('LANDED ')]
        self.assertEqual(len(landed), 1, out.lines)
        self.assertGreater(out.lines.index(closed[0]), landed[0], out.lines)

    def test_a_record_the_pm_data_store_cannot_close_names_the_command_and_the_landing_stands(self):
        wt = self.pm_worktree(pulled('FOO', priority='-1', priority_by='-',
                                     owner_ask='may this ship?'))
        out = self.land()
        self.assertTrue(os.path.exists(self.record(wt, 'FOO')))
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('owner-held', warn[0])
        self.assertIn('backlog.py" -C "%s" close FOO --reason "landed %s" --ruling R-nnnn'
                      % (self.pm_root(wt), out.merge), warn[0])
        self.assertEqual(self.main_now(), out.merge)
        self.assertIsNone(self.lock().read())

    def test_no_worktree_on_the_pm_data_branch_is_a_warning_and_the_landing_stands(self):
        out = self.land()
        warn = [l for l in out.lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('no worktree has the PM data branch pmdata checked out', warn[0])
        self.assertIn('landable %s' % BRANCH, warn[0])
        self.assertEqual(self.main_now(), out.merge)

    def test_the_landable_commands_name_the_pm_data_worktree(self):
        wt = self.pm_worktree()
        cfg = kconfig.load_at(self.repo.root, self.main)
        hows = at_landing.landable_commands(self.repo.root, cfg, BRANCH)
        self.assertEqual(len(hows), 2, hows)
        self.assertIn('-C "%s" landable %s' % (self.repo.root, BRANCH), hows[0])
        self.assertIn('-C "%s" landable %s' % (self.pm_root(wt), BRANCH), hows[1])


class CloseEdges(StoreLandCase):
    """The lookup key, the owner-held command, the re-pull race and an unreadable store."""

    def land(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def warnings(self, out):
        return [l for l in out.lines if l.startswith('WARNING')]

    def test_a_record_whose_id_header_differs_from_its_file_name_is_closed(self):
        # `check` reports the mismatch (ID_FILENAME, a FAIL finding); the record stays readable,
        # and the store looks records up by file name.
        self.seed({'FOO': pulled('OTHER')['OTHER']})
        out = self.land()
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.stems(), [])
        self.assertEqual(self.subjects()[0], 'closes FOO: landed %s' % out.merge)

    def test_an_owner_held_records_command_carries_the_ruling_it_needs(self):
        self.seed(pulled('FOO', priority='-1', priority_by='-', owner_ask='may this ship?'))
        out = self.land()
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('close FOO --reason "landed %s" --ruling R-nnnn' % out.merge, warn[0])

    def test_an_owner_held_records_command_names_a_worktree_not_the_main_checkout(self):
        self.seed(pulled('FOO', priority='-1', priority_by='-', owner_ask='may this ship?'))
        out = self.land()
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('backlog.py" -C "%s" close FOO --reason "landed %s" --ruling R-nnnn'
                      % (at_landing.WORKTREE, out.merge), warn[0])
        self.assertNotIn(self.repo.root, warn[0])

    def test_the_close_command_runs_the_repositorys_own_backlog_tool_when_it_has_one(self):
        self.assertEqual(at_landing.close_command(self.repo.root, 'close', 'FOO'),
                         'python "%s" -C "%s" close FOO'
                         % (at_landing.backlog_py(), at_landing.WORKTREE))
        self.repo.write(os.path.join('tools', 'backlog.py'), '')
        self.assertEqual(at_landing.close_command(self.repo.root, 'close', 'FOO'),
                         'python tools/backlog.py -C "%s" close FOO' % at_landing.WORKTREE)

    def test_a_record_re_pulled_by_another_branch_mid_close_is_left_as_it_is(self):
        self.seed(pulled('FOO'))
        test, real_init = self, smod.Store.__init__

        def init(store, *args, **kw):
            real_init(store, *args, **kw)

            def repull(attempt):
                if attempt == 0:
                    test.seed(pulled('FOO', by='kit/other'), message='re-pull\n')
            store.before_publish = repull

        with mock.patch.object(smod.Store, '__init__', init):
            out = self.land()
        self.assertEqual(self.warnings(out), [])
        self.assertIn('FOO is no longer pulled by %s; left as it is' % BRANCH, out.lines)
        self.assertEqual(self.stems(), ['FOO'])
        self.assertEqual(self.subjects()[0], 're-pull')
        foo = self.backend().load(rmod.Rules()).get('FOO')
        self.assertEqual(foo.get('pulled_by'), 'kit/other')

    def test_an_unreadable_store_is_a_warning_and_the_landing_stands(self):
        before = self.seed(pulled('FOO'), {'BAD': 'not a header line\n'})
        out = self.land()
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('could not be read', warn[0])
        self.assertIn('backlog.py" -C "%s" landable %s' % (self.repo.root, BRANCH), warn[0])
        self.assertEqual(self.remote_store(), before)
        self.assertIsNone(self.lock().read())

    def test_a_remote_that_cannot_be_asked_is_a_warning(self):
        self.seed(pulled('FOO'))
        cfg = kconfig.load_at(self.repo.root, self.main)
        with mock.patch.object(refs, 'ls_remote', return_value=(None, 'boom')):
            lines = at_landing.close_landed(self.repo.root, cfg, BRANCH, 'f' * 40)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('WARNING: %s landed' % BRANCH), lines)
        self.assertIn('boom', lines[0])
        self.assertEqual(self.stems(), ['FOO'])


class AncestryCase(StoreLandCase):
    def check(self):
        cfg = kconfig.load_at(self.repo.root, self.main)
        return at_landing.ancestry_refusal(self.repo.root, cfg, self.main)


class AncestryFailsClosed(AncestryCase):
    """Every way the store cannot be read refuses the landing (ancestry_refusal, called directly
    with the branch's config; tip = main, which does not hold the store)."""

    def test_a_readable_store_the_tip_does_not_hold_passes(self):
        self.seed(priced('BAZ'))
        self.assertIsNone(self.check())

    def test_a_remote_that_cannot_be_asked_refuses(self):
        self.seed(priced('BAZ'))
        with mock.patch.object(refs, 'ls_remote', return_value=(None, 'boom')):
            why = self.check()
        self.assertIn('cannot read the backlog store', why)
        self.assertIn('boom', why)

    def test_a_store_that_cannot_be_fetched_refuses(self):
        self.seed(priced('BAZ'))
        with mock.patch.object(at_landing, '_has_commit', return_value=False):
            why = self.check()
        self.assertIn('cannot fetch the backlog store', why)

    def test_a_failed_root_listing_refuses(self):
        self.seed(priced('BAZ'))
        real = cmd.run_text

        def run_text(args, *rest, **kw):
            if args[0] == 'rev-list':
                return None, 'boom'
            return real(args, *rest, **kw)

        with mock.patch.object(cmd, 'run_text', side_effect=run_text):
            why = self.check()
        self.assertIn("cannot list the backlog store's root commit: boom", why)

    def test_an_undecidable_ancestry_refuses(self):
        self.seed(priced('BAZ'))
        with mock.patch.object(refs, 'is_ancestor', return_value=(None, 'boom')):
            why = self.check()
        self.assertIn('cannot tell whether the branch holds', why)


@real_git('a broken loose ref: git warns "ignoring broken ref"; the fake imitates that only in '
          'for-each-ref (kitlib/git/fake/refs.py BrokenRef)')
class BrokenLocalStoreRef(AncestryCase):
    """AncestryFailsClosed, for a local store ref holding garbage."""

    def test_a_broken_local_store_ref_refuses(self):
        self.seed(priced('BAZ'))
        path = os.path.join(self.repo.common_dir(), *smod.STORE_REF.split('/'))
        self.assertTrue(os.path.isfile(path), path)               # a loose ref to break
        with open(path, 'w') as f:
            f.write('garbage\n')
        why = self.check()
        self.assertIsNotNone(why)
        self.assertIn('cannot read the local backlog store', why)


class AncestryMovesNoRef(StoreLandCase):
    """The ancestry check fetches a store commit it lacks without moving any ref, although a
    configured refspec matches the store (docs/backlog-store.md "Merge safety")."""
    TRIGGER = 'refs/backlog/main'

    def test_fetching_a_newer_remote_store_moves_no_ref(self):
        first = self.seed(priced('BAZ'))
        self.stems()                    # a read that finds the store adds the store's refspec
        fetch = self.repo.git('config', '--get-all', 'remote.origin.fetch')
        self.assertIn(self.TRIGGER, fetch)                  # a refspec git could update through
        tree = self.repo.git('rev-parse', first + '^{tree}')
        newer = self.repo.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                              'commit-tree', tree, '-p', first, '-m', 'newer',
                              cwd=self.repo.remote)
        self.repo.git('update-ref', self.remote_ref, newer, cwd=self.repo.remote)
        self.assertEqual(self.repo.git('cat-file', '-t', newer, check=False), '')
        refs_before = self.repo.git('for-each-ref', '--format=%(refname) %(objectname)')
        cfg = kconfig.load_at(self.repo.root, self.main)
        self.assertIsNone(at_landing.ancestry_refusal(self.repo.root, cfg, self.main))
        self.assertEqual(self.repo.git('cat-file', '-t', newer), 'commit')      # fetched
        self.assertEqual(self.repo.git('for-each-ref', '--format=%(refname) %(objectname)'),
                         refs_before)


class AncestryMovesNoRefBranchStore(AncestryMovesNoRef):
    STORE = 'branch'
    KIT = dict(test_land.LandCase.KIT, backlog={'store': 'branch'})
    TRIGGER = 'refs/heads/*:refs/remotes/origin/*'


if __name__ == '__main__':
    unittest.main()
