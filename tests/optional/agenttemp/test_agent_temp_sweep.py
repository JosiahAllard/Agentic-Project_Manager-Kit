"""Tests for optional/agenttemp/agent_temp_sweep.py. Scratch folders only: every call names a
scratch home (its own machine config) and a scratch folder to sweep, never the real ones. Entries
are aged by moving the clock forward (`now`), since a file's creation time cannot be set back."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', 'optional', 'agenttemp'))
import agent_temp_sweep as S  # noqa: E402
from kitlib.config import user as kuser  # noqa: E402

LATER = time.time() + 10 * S.DAY        # every entry made now is 10 days old then


def put(path, text='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-temp-sweep-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.dir = os.path.join(self.tmp, 'agenttemp')
        os.makedirs(self.home)
        os.makedirs(self.dir)

    def config(self, cfg):
        put(kuser.config_file(self.home), json.dumps(cfg))

    def young(self, path):
        """Makes path written 1 day before LATER."""
        os.utime(path, (LATER - S.DAY, LATER - S.DAY))

    def sweep(self, apply=False, age=3):
        return {e.name: e.action for e in S.sweep(self.dir, age, apply=apply, now=LATER,
                                                   home=self.home)}

    def names(self):
        return sorted(os.listdir(self.dir))


class Config(Case):
    def test_check(self):
        good = os.path.abspath(self.dir)
        self.assertEqual(S.check({'dir': good}), [])
        self.assertEqual(S.check({'dir': good, 'max_age_days': 0.5}), [])
        for cfg, part in (([], 'must be an object'), ({'dir': 'rel'}, 'absolute path'),
                          ({}, 'absolute path'), ({'dir': good, 'max_age_days': 0}, 'above 0'),
                          ({'dir': good, 'max_age_days': True}, 'above 0'),
                          ({'dir': good, 'other': 1}, 'unknown key')):
            self.assertTrue(any(part in e for e in S.check(cfg)), cfg)

    def test_load(self):
        self.assertIsNone(S.load(self.home))                     # no machine config
        self.config({'ledger_path': self.tmp})
        self.assertIsNone(S.load(self.home))                     # no key
        self.config({S.KEY: {'dir': self.dir}})
        self.assertEqual(S.load(self.home), (self.dir, S.DEFAULT_MAX_AGE_DAYS))
        self.config({S.KEY: {'dir': self.dir, 'max_age_days': 7}})
        self.assertEqual(S.load(self.home), (self.dir, 7))
        self.config({S.KEY: {'dir': 'relative'}})
        with self.assertRaises(S.SweepConfigError):
            S.load(self.home)

    def test_unsafe_dirs_are_refused(self):
        root = os.path.abspath(os.sep)
        for d in (root, self.home, self.tmp, os.path.join(self.tmp, 'missing')):
            self.assertIsNotNone(S.unsafe_dir(d, self.home), d)
            with self.assertRaises(S.SweepConfigError):
                S.sweep(d, 3, apply=True, now=LATER, home=self.home)
        self.assertIsNone(S.unsafe_dir(self.dir, self.home))


class Sweep(Case):
    def test_a_dry_run_lists_and_removes_nothing(self):
        put(os.path.join(self.dir, 'old', 'a.txt'))
        put(os.path.join(self.dir, 'old.log'))
        self.assertEqual(self.sweep(), {'old': 'would', 'old.log': 'would'})
        self.assertEqual(self.names(), ['old', 'old.log'])

    def test_apply_removes_old_entries_and_keeps_young_ones(self):
        put(os.path.join(self.dir, 'old', 'deep', 'a.txt'))
        put(os.path.join(self.dir, 'old.log'))
        put(os.path.join(self.dir, 'busy', 'deep', 'fresh.txt'))
        self.young(os.path.join(self.dir, 'busy', 'deep', 'fresh.txt'))   # newest time is inside
        put(os.path.join(self.dir, 'new.log'))
        self.young(os.path.join(self.dir, 'new.log'))
        self.assertEqual(self.sweep(apply=True), {'old': 'removed', 'old.log': 'removed',
                                                  'busy': 'young', 'new.log': 'young'})
        self.assertEqual(self.names(), ['busy', 'new.log'])

    def test_a_read_only_file_is_removed(self):
        f = os.path.join(self.dir, 'ro', 'a.txt')
        put(f)
        os.chmod(f, stat.S_IREAD)
        self.assertEqual(self.sweep(apply=True), {'ro': 'removed'})

    def test_a_failed_removal_puts_what_is_left_back(self):
        put(os.path.join(self.dir, 'old', 'a.txt'))
        put(os.path.join(self.dir, 'old', 'b.txt'))
        real = S._unlink

        def unlink(path):
            if path.endswith('b.txt'):
                raise PermissionError(13, 'denied')
            real(path)
        with mock.patch.object(S, '_unlink', unlink):
            got = S.sweep(self.dir, 3, apply=True, now=LATER, home=self.home)
        self.assertEqual([(e.name, e.action) for e in got], [('old', 'failed')])
        self.assertIn('b.txt', got[0].why)
        self.assertEqual(self.names(), ['old'])
        self.assertEqual(os.listdir(os.path.join(self.dir, 'old')), ['b.txt'])

    def test_a_write_between_listing_and_claim_keeps_it(self):
        put(os.path.join(self.dir, 'old', 'a.txt'))
        real = os.rename
        late = os.path.join(self.dir, 'old', 'late.txt')

        def rename(src, dst):
            if S.CLAIM in os.path.basename(dst) and not os.path.exists(late):
                put(late)                   # a process writes just before the claim lands
                self.young(late)
            real(src, dst)
        with mock.patch.object(S.os, 'rename', rename):
            got = S.sweep(self.dir, 3, apply=True, now=LATER, home=self.home)
        self.assertEqual([(e.name, e.action) for e in got], [('old', 'young')])
        self.assertIn('after it was listed', got[0].why)
        self.assertEqual(self.names(), ['old'])
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, 'old'))), ['a.txt', 'late.txt'])

    @unittest.skipUnless(os.name == 'nt', 'claiming fails on an open file on Windows')
    def test_an_entry_with_an_open_file_is_kept(self):
        f = os.path.join(self.dir, 'open', 'deep', 'a.txt')
        put(f)
        with open(f):
            self.assertEqual(self.sweep(apply=True), {'open': 'in-use'})
        self.assertTrue(os.path.exists(f))

    @unittest.skipUnless(os.name == 'nt', 'claiming fails on a working folder on Windows')
    def test_an_entry_a_process_works_in_is_kept(self):
        cwd = os.path.join(self.dir, 'cwd', 'deep')
        os.makedirs(cwd)
        ready = os.path.join(self.tmp, 'ready')
        p = subprocess.Popen([sys.executable, '-c',
                              "import sys,time; open(sys.argv[1],'w').close(); time.sleep(60)",
                              ready], cwd=cwd)
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.kill)
        end = time.time() + 30
        while not os.path.exists(ready) and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.sweep(apply=True), {'cwd': 'in-use'})
        self.assertTrue(os.path.isdir(cwd))

    @unittest.skipUnless(os.name == 'nt' and hasattr(__import__('_winapi'), 'CreateJunction'),
                         'a junction needs Windows')
    def test_a_link_is_removed_never_its_target(self):
        import _winapi
        target = os.path.join(self.tmp, 'keep')
        put(os.path.join(target, 'precious.txt'))
        os.makedirs(os.path.join(self.dir, 'old'))
        _winapi.CreateJunction(target, os.path.join(self.dir, 'old', 'link'))
        _winapi.CreateJunction(target, os.path.join(self.dir, 'toplink'))
        self.assertEqual(self.sweep(apply=True), {'old': 'removed', 'toplink': 'removed'})
        self.assertEqual(self.names(), [])
        self.assertTrue(os.path.exists(os.path.join(target, 'precious.txt')))


SID_OLD = '0a1b2c3d-1111-4222-8333-944455556666'
SID_NEW = '0a1b2c3d-2222-4222-8333-944455556666'
SID_LIVE = '0a1b2c3d-3333-4222-8333-944455556666'


def procs(*cmds):
    """A fake process lister: one process per command line."""
    return lambda: [{'Name': 'claude.exe', 'CommandLine': c} for c in cmds]


def unreadable():
    raise RuntimeError('powershell exit 1: access denied')


@unittest.skipUnless(os.name == 'nt', 'the sweep and its tests are Windows only')
class SessionDirs(Case):
    """Claude Code's scratch is <dir>/claude/<project>/<session>: each session folder is its own unit,
    the keep-list holds names back, and the folder to sweep may not lie in a repository or the kit."""

    def sweep(self, apply=False, age=3, keep=(), lister=None):
        return {e.name: e.action for e in S.sweep(self.dir, age, apply=apply, now=LATER, home=self.home,
                                                   keep=keep, lister=lister or procs())}

    def session(self, project, sid, young=False):
        f = os.path.join(self.dir, 'claude', project, sid, 'scratchpad', 'a.txt')
        put(f)
        if young:
            self.young(f)
        return os.path.dirname(os.path.dirname(f))

    def test_old_session_dirs_under_claude_are_removed_though_a_sibling_is_young(self):
        old = self.session('proj', SID_OLD)
        new = self.session('proj', SID_NEW, young=True)
        got = self.sweep(apply=True)
        self.assertEqual(got['claude/proj/' + SID_OLD], 'removed')
        self.assertEqual(got['claude/proj/' + SID_NEW], 'young')
        self.assertNotIn('claude', got)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))
        self.assertEqual(os.listdir(os.path.join(self.dir, 'claude')), ['proj'])

    def test_a_claude_project_dir_is_removed_only_when_empty_and_old(self):
        os.makedirs(os.path.join(self.dir, 'claude', 'empty-old'))
        self.session('emptied', SID_OLD)
        self.session('busy', SID_NEW, young=True)
        fresh = os.path.join(self.dir, 'claude', 'empty-young')
        os.makedirs(fresh)
        self.young(fresh)
        got = self.sweep(apply=True)
        self.assertEqual(got['claude/empty-old'], 'removed')
        self.assertEqual(got['claude/emptied'], 'removed')
        self.assertNotIn('claude/busy', got)
        self.assertNotIn('claude/empty-young', got)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, 'claude'))), ['busy', 'empty-young'])
        shutil.rmtree(os.path.join(self.dir, 'claude', 'busy'))
        shutil.rmtree(fresh)
        self.sweep(apply=True)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, 'claude')))     # claude itself never goes

    def test_a_session_dir_named_in_the_process_list_is_live_and_kept(self):
        live = self.session('proj', SID_LIVE)
        self.session('proj', SID_OLD)
        got = self.sweep(apply=True, lister=procs('claude.exe --resume %s' % SID_LIVE.upper(), None))
        self.assertEqual(got['claude/proj/' + SID_LIVE], 'live')
        self.assertEqual(got['claude/proj/' + SID_OLD], 'removed')
        self.assertTrue(os.path.isdir(live))

    def test_an_unreadable_process_list_removes_no_session_dir(self):
        s = self.session('proj', SID_OLD)
        put(os.path.join(self.dir, 'old.log'))
        got = S.sweep(self.dir, 3, apply=True, now=LATER, home=self.home, lister=unreadable)
        acts = {e.name: (e.action, e.why) for e in got}
        self.assertEqual(acts['claude/proj/' + SID_OLD][0], 'live')
        self.assertIn('process list', acts['claude/proj/' + SID_OLD][1])
        self.assertEqual(acts['old.log'][0], 'removed')         # outside claude/ the sweep goes on
        self.assertTrue(os.path.isdir(s))

    def test_the_default_lister_gives_the_process_list_30_seconds(self):
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return []
        with mock.patch('kitlib.pmstart.list_processes', fake):
            self.assertEqual(S.default_lister(), [])
        self.assertEqual(seen, {'timeout': S.LIST_TIMEOUT})
        self.assertEqual(S.LIST_TIMEOUT, 30)

    def test_a_project_folder_that_cannot_be_listed_is_skipped_and_the_rest_goes_on(self):
        self.session('bad', SID_OLD)
        good = self.session('good', SID_OLD)
        gone = os.path.join(self.dir, 'claude', 'gone')
        os.makedirs(gone)
        put(os.path.join(self.dir, 'old.log'))
        bad = os.path.normcase(os.path.join(self.dir, 'claude', 'bad'))
        real = os.scandir

        def scandir(path='.'):
            p = os.path.normcase(os.fspath(path))
            if p == bad:
                raise PermissionError(13, 'Access is denied', path)
            if p == os.path.normcase(gone):
                raise FileNotFoundError(2, 'The system cannot find the path specified', path)
            return real(path)
        with mock.patch.object(S.os, 'scandir', scandir):
            got = S.sweep(self.dir, 3, apply=True, now=LATER, home=self.home, lister=procs())
        acts = {e.name: (e.action, e.why) for e in got}
        self.assertEqual(acts['claude/bad'][0], 'skipped')
        self.assertIn('Access is denied', acts['claude/bad'][1])
        self.assertEqual(acts['claude/gone'][0], 'skipped')
        self.assertEqual(acts['claude/good/' + SID_OLD][0], 'removed')
        self.assertEqual(acts['old.log'][0], 'removed')
        self.assertFalse(os.path.exists(good))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, 'claude', 'bad', SID_OLD)))
        self.assertTrue(os.path.isdir(gone))

    def test_a_dir_that_cannot_be_listed_is_a_config_error_not_a_traceback(self):
        real, d = os.scandir, os.path.normcase(self.dir)

        def scandir(path='.'):
            if os.path.normcase(os.fspath(path)) == d:
                raise PermissionError(13, 'Access is denied', path)
            return real(path)
        with mock.patch.object(S.os, 'scandir', scandir):
            with self.assertRaises(S.SweepConfigError):
                S.sweep(self.dir, 3, apply=True, now=LATER, home=self.home, lister=procs())

    def test_keep_names_are_never_removed(self):
        put(os.path.join(self.dir, 'kit-conductor', 'plan.md'))
        pinned = self.session('proj', 'pinned-notes')
        self.session('proj', SID_OLD)
        got = self.sweep(apply=True, keep=['kit-conductor', 'claude/*/pinned-*'])
        self.assertEqual(got['kit-conductor'], 'kept')
        self.assertEqual(got['claude/proj/pinned-notes'], 'kept')
        self.assertEqual(got['claude/proj/' + SID_OLD], 'removed')
        self.assertTrue(os.path.isdir(pinned))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'kit-conductor', 'plan.md')))

    def test_coordinator_and_consultant_folders_are_kept_with_no_keep_list(self):
        # kitlib.launcher.start keeps a consultant's notes in <repo>-consultant and a coordinator's
        # state note in <repo>-coordinator; both outlive any age, whatever the config says.
        kept = ('kit-coordinator', 'Alpha-Consultant', 'my-repo-consultant')
        for name in kept + ('kit-coordinator-old', 'consultant', 'coordinator'):
            put(os.path.join(self.dir, name, 'notes.md'))
        self.session('proj', 'x-consultant')
        self.assertEqual({n: a for n, a in self.sweep().items() if n in kept}, {n: 'kept' for n in kept})
        got = self.sweep(apply=True)
        for name in kept:
            self.assertEqual(got[name], 'kept', name)
            self.assertTrue(os.path.isfile(os.path.join(self.dir, name, 'notes.md')), name)
        for name in ('kit-coordinator-old', 'consultant', 'coordinator', 'claude/proj/x-consultant'):
            self.assertEqual(got[name], 'removed', name)    # a top-level <name>-coordinator/-consultant only
        self.assertEqual(S.BUILT_IN_KEEP, ('*-coordinator', '*-consultant', 'shared'))

    def test_shared_folder_is_kept_with_no_keep_list(self):
        # the agents' cross-session files live in <dir>/shared; it outlives any age, whatever the config says.
        for name in ('shared', 'shared-old'):
            put(os.path.join(self.dir, name, 'f.txt'))
        got = self.sweep(apply=True)
        self.assertEqual(got['shared'], 'kept')
        self.assertEqual(got['shared-old'], 'removed')    # the name alone, not a prefix
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'shared', 'f.txt')))

    def test_keep_is_case_insensitive_and_matches_by_relpath_not_basename(self):
        put(os.path.join(self.dir, 'Work-Notes', 'a.txt'))
        put(os.path.join(self.dir, 'x', 'work-notes', 'a.txt'))
        self.session('proj', 'work-notes')
        got = self.sweep(apply=True, keep=['wORK-nOTES'])
        self.assertEqual(got['Work-Notes'], 'kept')
        self.assertEqual(got['x'], 'removed')
        self.assertEqual(got['claude/proj/work-notes'], 'removed')
        self.assertFalse(S.is_kept('claude/proj/a', ['claude*a']))       # * matches within one name
        self.assertTrue(S.is_kept('claude/proj', ['claude/proj/a']))    # an item inside a unit keeps it
        self.assertFalse(S.is_kept('claude/other', ['claude/proj/a']))  # ... only the unit holding it
        self.assertTrue(S.is_kept('claude/proj/a', ['claude/proj']))    # a kept folder keeps what is in it
        self.assertTrue(S.is_kept('claude/proj/a', ['claude/*']))       # ... matched by a pattern too

    def test_a_keep_item_deeper_than_a_unit_keeps_the_whole_unit(self):
        # The sweep never enters a unit, so an item under one (kit-conductor/r2) keeps all of it;
        # before, the unit kit-conductor matched no item and went whole once old.
        put(os.path.join(self.dir, 'kit-conductor', 'r2', 'plan.md'))
        put(os.path.join(self.dir, 'kit-conductor', 'r1', 'old.md'))
        put(os.path.join(self.dir, 'other', 'a.txt'))
        pinned = self.session('proj', SID_OLD)
        got = self.sweep(apply=True, keep=['kit-conductor/r2', 'claude/proj/%s/scratchpad' % SID_OLD])
        self.assertEqual(got['kit-conductor'], 'kept')
        self.assertEqual(got['claude/proj/' + SID_OLD], 'kept')
        self.assertEqual(got['other'], 'removed')
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'kit-conductor', 'r2', 'plan.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'kit-conductor', 'r1', 'old.md')))
        self.assertTrue(os.path.isdir(pinned))

    def test_check_rejects_bad_keep(self):
        good = os.path.abspath(self.dir)
        self.assertEqual(S.check({'dir': good, 'keep': ['a', 'claude/*/b*']}), [])
        for keep in ('a', [''], [os.path.abspath(self.tmp)], ['a/../b'], ['..'], [3]):
            self.assertTrue(any('keep' in e for e in S.check({'dir': good, 'keep': keep})), keep)

    def test_load_reads_keep(self):
        self.config({S.KEY: {'dir': self.dir, 'keep': ['kit-conductor']}})
        cfg = S.load_config(self.home)
        self.assertEqual((cfg.dir, cfg.max_age_days, cfg.keep), (self.dir, 3, ['kit-conductor']))
        self.config({S.KEY: {'dir': self.dir}})
        self.assertEqual(S.load_config(self.home).keep, [])

    def test_load_still_returns_the_two_tuple(self):
        self.config({S.KEY: {'dir': self.dir, 'max_age_days': 5, 'keep': ['x']}})
        self.assertEqual(S.load(self.home), (self.dir, 5))

    def test_a_dir_inside_a_git_checkout_is_refused(self):
        for marker in ('folder', 'file'):
            repo = os.path.join(self.tmp, 'repo-' + marker)
            git = os.path.join(repo, '.git')
            if marker == 'folder':
                os.makedirs(git)
            else:
                put(git, 'gitdir: elsewhere')
            d = os.path.join(repo, 'a', 'b')
            put(os.path.join(d, 'old.txt'))
            self.assertIn('git', S.unsafe_dir(d, self.home))
            with self.assertRaises(S.SweepConfigError):
                S.sweep(d, 3, apply=True, now=LATER, home=self.home, lister=procs())
            self.assertTrue(os.path.isfile(os.path.join(d, 'old.txt')))

    def test_a_dir_under_the_machine_config_folder_is_refused(self):
        d = os.path.join(kuser.config_dir(self.home), 'temp')
        put(os.path.join(d, 'old.txt'))
        self.assertIsNotNone(S.unsafe_dir(d, self.home))
        self.assertIsNotNone(S.unsafe_dir(kuser.config_dir(self.home), self.home))
        with self.assertRaises(S.SweepConfigError):
            S.sweep(d, 3, apply=True, now=LATER, home=self.home, lister=procs())
        self.assertTrue(os.path.isfile(os.path.join(d, 'old.txt')))

    def test_the_kit_and_folders_holding_it_are_refused(self):
        self.assertIsNotNone(S.unsafe_dir(S.KIT_ROOT, self.home))
        self.assertIsNotNone(S.unsafe_dir(os.path.dirname(S.KIT_ROOT), self.home))

    def test_a_folder_in_the_home_folder_is_refused(self):
        d = os.path.join(self.home, 'AppData', 'Local', 'Temp')
        put(os.path.join(d, 'old.txt'))
        self.assertIn('home folder', S.unsafe_dir(d, self.home))

    def junction(self, link, target):
        import _winapi
        os.makedirs(os.path.dirname(link), exist_ok=True)
        _winapi.CreateJunction(target, link)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))   # the junction, not its target

    @unittest.skipUnless(hasattr(__import__('_winapi'), 'CreateJunction'), 'a junction needs Windows')
    def test_a_junction_above_dir_into_a_repo_is_refused(self):
        # dir is no link itself, but a folder above it is a junction into a git checkout: by its
        # text the walk-up finds no .git; by its real path it does.
        repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        put(os.path.join(repo, 'src', 'temp', 'old.txt'))
        self.junction(os.path.join(self.tmp, 'via'), os.path.join(repo, 'src'))
        d = os.path.join(self.tmp, 'via', 'temp')
        self.assertIn('git', S.unsafe_dir(d, self.home) or '')
        with self.assertRaises(S.SweepConfigError):
            S.sweep(d, 3, apply=True, now=LATER, home=self.home, lister=procs())
        self.assertTrue(os.path.isfile(os.path.join(repo, 'src', 'temp', 'old.txt')))

    @unittest.skipUnless(hasattr(__import__('_winapi'), 'CreateJunction'), 'a junction needs Windows')
    def test_a_junction_above_dir_into_home_or_the_config_folder_is_refused(self):
        self.junction(os.path.join(self.tmp, 'up'), self.tmp)
        self.assertIn('home folder', S.unsafe_dir(os.path.join(self.tmp, 'up', 'home'), self.home) or '')
        cfg = kuser.config_dir(self.home)
        put(os.path.join(cfg, 'temp', 'old.txt'))
        self.junction(os.path.join(self.tmp, 'cfgvia'), cfg)
        self.assertIsNotNone(S.unsafe_dir(os.path.join(self.tmp, 'cfgvia', 'temp'), self.home))

    @unittest.skipUnless(hasattr(__import__('_winapi'), 'CreateJunction'), 'a junction needs Windows')
    def test_nothing_outside_dir_is_touched(self):
        import _winapi
        outside = os.path.join(self.tmp, 'outside')
        put(os.path.join(outside, 'precious.txt'))
        os.makedirs(os.path.join(self.dir, 'claude', 'proj'))
        _winapi.CreateJunction(outside, os.path.join(self.dir, 'claude', 'proj', SID_OLD))
        _winapi.CreateJunction(outside, os.path.join(self.dir, 'claude', 'linkproj'))
        got = self.sweep(apply=True)
        self.assertEqual(got['claude/proj/' + SID_OLD], 'removed')
        self.assertEqual(got['claude/linkproj'], 'removed')
        self.assertTrue(os.path.isfile(os.path.join(outside, 'precious.txt')))
        self.assertEqual(os.listdir(outside), ['precious.txt'])

    def test_dry_run_default_removes_nothing_for_session_dirs(self):
        old = self.session('proj', SID_OLD)
        os.makedirs(os.path.join(self.dir, 'claude', 'empty'))
        got = {e.name: e.action for e in S.sweep(self.dir, 3, now=LATER, home=self.home, lister=procs())}
        self.assertEqual(got['claude/proj/' + SID_OLD], 'would')
        self.assertEqual(got['claude/empty'], 'would')
        self.assertTrue(os.path.isdir(old))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, 'claude', 'empty')))


class Cli(Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stderr', err):
            code = S.main(list(argv), home=self.home, out=out, now=LATER)
        return code, out.getvalue(), err.getvalue()

    def test_no_config_is_refused(self):
        code, _out, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn('nothing to sweep', err)

    def test_home_option_names_the_home_whose_machine_config_is_read(self):
        self.config({S.KEY: {'dir': self.dir}})
        put(os.path.join(self.dir, 'old.log'))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stderr', err):
            code = S.main(['--home', self.home], out=out, now=LATER)     # no home= argument
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn('would    old.log', out.getvalue())

    def test_an_entry_name_outside_the_pipe_code_page_does_not_fail_the_run(self):
        self.config({S.KEY: {'dir': self.dir}})
        name = '中文-☃.log'                 # in no Western code page
        put(os.path.join(self.dir, name))
        pipe = io.BytesIO()
        stdout = io.TextIOWrapper(pipe, encoding='cp1252', errors='strict', write_through=True)
        err = io.StringIO()
        with mock.patch.object(sys, 'stdout', stdout), mock.patch.object(sys, 'stderr', err):
            code = S.main([], home=self.home, now=LATER)                 # out=None: the process's stdout
        stdout.flush()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn(b'would', pipe.getvalue())

    def test_an_unsafe_dir_is_refused(self):
        self.config({S.KEY: {'dir': self.home}})
        code, _out, err = self.main('--apply')
        self.assertEqual(code, 2)
        self.assertIn('home folder', err)

    @unittest.skipUnless(os.name == 'nt', 'the sweep and its tests are Windows only')
    def test_cli_prints_kept_and_live_counts_as_kept_and_exit_0(self):
        self.config({S.KEY: {'dir': self.dir, 'keep': ['pinned']}})
        put(os.path.join(self.dir, 'pinned', 'a.txt'))
        put(os.path.join(self.dir, 'claude', 'proj', SID_LIVE, 'a.txt'))
        put(os.path.join(self.dir, 'old.log'))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stderr', err):
            code = S.main(['--apply'], home=self.home, out=out, now=LATER,
                          lister=procs('claude --session-id %s' % SID_LIVE))
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertIn('kept     pinned', text)
        self.assertIn('live     claude/proj/%s' % SID_LIVE, text)
        self.assertIn('removed  old.log', text)
        self.assertIn('3 entries, 2 kept', text)
        self.assertEqual(sorted(os.listdir(self.dir)), ['claude', 'pinned'])

    def test_an_unreadable_process_list_prints_one_line_and_exits_3(self):
        self.config({S.KEY: {'dir': self.dir}})
        put(os.path.join(self.dir, 'claude', 'proj', SID_OLD, 'a.txt'))
        put(os.path.join(self.dir, 'old.log'))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stderr', err):
            code = S.main(['--apply'], home=self.home, out=out, now=LATER, lister=unreadable)
        text = out.getvalue()
        self.assertEqual(code, S.EXIT_LIST_UNAVAILABLE, text)
        self.assertEqual(S.EXIT_LIST_UNAVAILABLE, 3)
        self.assertEqual(text.count('kept every claude/ folder: process list unavailable: '
                                    'RuntimeError: powershell exit 1: access denied\n'), 1, text)
        self.assertIn('removed  old.log', text)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, 'claude', 'proj', SID_OLD)))

    def test_dry_run_then_apply(self):
        self.config({S.KEY: {'dir': self.dir, 'max_age_days': 2}})
        put(os.path.join(self.dir, 'old', 'a.txt'))
        code, out, _err = self.main()
        self.assertEqual(code, 0)
        self.assertIn('would', out)
        self.assertIn('dry run', out)
        self.assertEqual(self.names(), ['old'])
        code, out, _err = self.main('--apply')
        self.assertEqual(code, 0)
        self.assertIn('removed', out)
        self.assertEqual(self.names(), [])

    @unittest.skipUnless(os.name == 'nt', 'claiming fails on an open file on Windows')
    def test_an_entry_kept_in_use_exits_1(self):
        self.config({S.KEY: {'dir': self.dir}})
        f = os.path.join(self.dir, 'open.log')
        put(f)
        with open(f):
            code, out, _err = self.main('--apply')
        self.assertEqual(code, 1)
        self.assertIn('in-use', out)

    @unittest.skipUnless(os.name == 'nt', 'the installed layout is a Windows junction')
    def test_it_runs_through_an_installed_junction(self):
        # machine setup installs <tools>/agenttemp as a junction into the kit; the script finds
        # kitlib through the junction's real path, not beside the junction.
        link = os.path.join(self.tmp, 'tools', 'agenttemp')
        os.makedirs(os.path.dirname(link))
        real = os.path.join(HERE, '..', '..', '..', 'optional', 'agenttemp')
        subprocess.run(['cmd', '/c', 'mklink', '/J', link, os.path.abspath(real)], check=True,
                       capture_output=True)
        self.addCleanup(os.rmdir, link)          # the junction only, before the folder goes
        r = self.run_alone(os.path.join(link, 'agent_temp_sweep.py'))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('nothing to sweep', r.stderr)

    def run_alone(self, script):
        """The script in a child python whose only way to kitlib is the script's own location."""
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home)
        env.pop('PYTHONPATH', None)              # an inherited path must not find kitlib for it
        return subprocess.run([sys.executable, script], env=env, cwd=self.tmp, capture_output=True,
                              text=True, timeout=60)

    def test_a_copy_outside_the_kit_refuses_with_exit_2(self):
        # Exit 1 means a partial sweep; a missing kit library sweeps nothing, so it is a refusal.
        alone = os.path.join(self.tmp, 'a', 'b', 'c', 'agent_temp_sweep.py')
        os.makedirs(os.path.dirname(alone))
        shutil.copyfile(S.__file__, alone)
        r = self.run_alone(alone)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('the kit library (kitlib) is not in the kit root', r.stderr)
        self.assertNotIn('Traceback', r.stderr)


CHANGE = LATER - 5 * S.DAY       # the fake time the user environment (TEMP) was last written


def at(t):
    """A process start time as kitlib.pmstart.list_processes gives it (ISO, local offset, 7 digits)."""
    import datetime
    s = datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec='microseconds')
    return s[:26] + '0' + s[26:]


def started(*pairs):
    """A fake process lister: (Name, start time) pairs, each with a command line naming no session."""
    return lambda: [{'Name': n, 'CommandLine': n, 'ProcessId': 100 + i, 'Created': c}
                    for i, (n, c) in enumerate(pairs)]


@unittest.skipUnless(os.name == 'nt', 'the sweep and its tests are Windows only')
class UserTemp(Case):
    """The second root: <user temp>/claude, its session units only (agent_temp_sweep.user_temp), and
    the one-time drain of the Windows default user temp's claude folder (--drain-old-user-temp)."""

    def setUp(self):
        super().setUp()
        self.user = os.path.join(self.tmp, 'usertemp')          # the registry's TEMP, outside home
        self.local = os.path.join(self.home, 'AppData', 'Local', 'Temp')    # %LOCALAPPDATA%\Temp
        os.makedirs(self.user)
        os.makedirs(self.local)
        for name, value in (('registry_user_temp', lambda: self.user),
                            ('local_temp', lambda: self.local),
                            ('env_changed_at', lambda: CHANGE)):
            p = mock.patch.object(S, name, value)
            p.start()
            self.addCleanup(p.stop)

    def main(self, *argv, lister=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stderr', err):
            code = S.main(list(argv), home=self.home, out=out, now=LATER,
                          lister=lister or started(('claude.exe', at(CHANGE + 60))))
        return code, out.getvalue() + err.getvalue()

    def session(self, root, project, sid, young=False):
        f = os.path.join(root, 'claude', project, sid, 'scratchpad', 'a.txt')
        put(f)
        if young:
            self.young(f)
        return os.path.dirname(os.path.dirname(f))

    # --- the config key ---

    def test_check_takes_user_temp_as_a_boolean(self):
        good = os.path.abspath(self.dir)
        self.assertEqual(S.check({'dir': good, 'user_temp': True}), [])
        self.assertEqual(S.check({'dir': good, 'user_temp': False}), [])
        for bad in ('yes', 1, None, []):
            self.assertTrue(any('user_temp' in e for e in S.check({'dir': good, 'user_temp': bad})), bad)

    def test_load_config_reads_user_temp_default_false(self):
        self.config({S.KEY: {'dir': self.dir}})
        self.assertFalse(S.load_config(self.home).user_temp)
        self.config({S.KEY: {'dir': self.dir, 'user_temp': True}})
        self.assertTrue(S.load_config(self.home).user_temp)

    # --- the claude-only units mode ---

    def test_sessions_only_sweeps_claude_units_and_nothing_else_of_the_folder(self):
        old = self.session(self.user, 'proj', SID_OLD)
        new = self.session(self.user, 'proj', SID_NEW, young=True)
        put(os.path.join(self.user, 'old.log'))
        put(os.path.join(self.user, 'other', 'a.txt'))
        got = {e.name: e.action for e in S.sweep(self.user, 3, apply=True, now=LATER, home=self.home,
                                                  lister=procs(), sessions_only=True)}
        self.assertEqual(got, {'claude/proj/' + SID_OLD: 'removed', 'claude/proj/' + SID_NEW: 'young'})
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))
        self.assertTrue(os.path.isfile(os.path.join(self.user, 'old.log')))
        self.assertTrue(os.path.isfile(os.path.join(self.user, 'other', 'a.txt')))

    def test_sessions_only_keeps_the_live_keep_and_fail_closed_rules(self):
        live = self.session(self.user, 'proj', SID_LIVE)
        pinned = self.session(self.user, 'proj', 'pinned')
        got = {e.name: e.action for e in S.sweep(
            self.user, 3, apply=True, now=LATER, home=self.home, keep=['claude/*/pinned'],
            lister=procs('claude --resume %s' % SID_LIVE), sessions_only=True)}
        self.assertEqual(got, {'claude/proj/' + SID_LIVE: 'live', 'claude/proj/pinned': 'kept'})
        self.assertTrue(os.path.isdir(live) and os.path.isdir(pinned))
        got = {e.name: e.action for e in S.sweep(self.user, 3, apply=True, now=LATER, home=self.home,
                                                  lister=unreadable, sessions_only=True)}
        self.assertEqual(got['claude/proj/' + SID_LIVE], 'live')
        self.assertTrue(os.path.isdir(live))

    # --- the second root's safety ---

    def test_a_second_root_outside_home_passes(self):
        os.makedirs(os.path.join(self.user, 'claude'))
        self.assertIsNone(S.second_root_unsafe(os.path.join(self.user, 'claude'), self.home))

    def test_the_home_exception_is_exactly_localappdata_temp_claude(self):
        root = os.path.join(self.local, 'claude')
        os.makedirs(root)
        self.assertIsNone(S.second_root_unsafe(root, self.home))
        for other in (os.path.join(self.home, 'AppData', 'Local', 'Temp2', 'claude'),
                      os.path.join(self.local, 'x', 'claude'),
                      os.path.join(self.home, 'claude'),
                      os.path.join(self.local, 'Claude-other')):
            os.makedirs(other, exist_ok=True)
            self.assertIn('home folder', S.second_root_unsafe(other, self.home) or '', other)
        with mock.patch.object(S, 'local_temp', lambda: None):      # no LOCALAPPDATA: no exception
            self.assertIn('home folder', S.second_root_unsafe(root, self.home) or '')

    def test_a_second_root_in_a_git_checkout_or_the_config_folder_is_refused(self):
        os.makedirs(os.path.join(self.local, '.git'))
        os.makedirs(os.path.join(self.local, 'claude'), exist_ok=True)
        self.assertIn('git', S.second_root_unsafe(os.path.join(self.local, 'claude'), self.home) or '')
        cfg = os.path.join(kuser.config_dir(self.home), 'claude')
        os.makedirs(cfg)
        self.assertIsNotNone(S.second_root_unsafe(cfg, self.home))

    @unittest.skipUnless(hasattr(__import__('_winapi'), 'CreateJunction'), 'a junction needs Windows')
    def test_a_second_root_that_is_a_link_or_under_one_is_refused(self):
        import _winapi
        target = os.path.join(self.tmp, 'target')
        os.makedirs(os.path.join(target, 'claude'))
        _winapi.CreateJunction(target, os.path.join(self.tmp, 'via'))
        self.addCleanup(os.rmdir, os.path.join(self.tmp, 'via'))
        self.assertIn('link', S.second_root_unsafe(os.path.join(self.tmp, 'via', 'claude'), self.home) or '')
        _winapi.CreateJunction(target, os.path.join(self.user, 'claude'))
        self.addCleanup(os.rmdir, os.path.join(self.user, 'claude'))
        self.assertIn('link', S.second_root_unsafe(os.path.join(self.user, 'claude'), self.home) or '')
        # home junctioned elsewhere: the real path is Temp, the text is not
        _winapi.CreateJunction(self.local, os.path.join(self.tmp, 'tempvia'))
        self.addCleanup(os.rmdir, os.path.join(self.tmp, 'tempvia'))
        os.makedirs(os.path.join(self.local, 'claude'))
        self.assertIn('link', S.second_root_unsafe(os.path.join(self.tmp, 'tempvia', 'claude'), self.home)
                      or '')

    # --- the daily run with user_temp ---

    def test_user_temp_on_sweeps_the_registry_temps_claude_folder(self):
        self.config({S.KEY: {'dir': self.dir, 'user_temp': True}})
        old = self.session(self.user, 'proj', SID_OLD)
        put(os.path.join(self.user, 'old.log'))
        put(os.path.join(self.dir, 'main.log'))
        code, text = self.main('--apply')
        self.assertEqual(code, 0, text)
        self.assertIn('removed  claude/proj/%s' % SID_OLD, text)
        self.assertIn('removed  main.log', text)
        self.assertIn(os.path.join(self.user, 'claude'), text)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(os.path.join(self.user, 'old.log')))
        self.assertTrue(os.path.isdir(os.path.join(self.user, 'claude')))     # the root never goes

    def test_user_temp_off_leaves_the_registry_temp_alone(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.user, 'proj', SID_OLD)
        code, text = self.main('--apply')
        self.assertEqual(code, 0, text)
        self.assertTrue(os.path.isdir(old))
        self.assertNotIn(SID_OLD, text)

    def test_user_temp_with_no_registry_value_notes_it_and_sweeps_the_main_root(self):
        self.config({S.KEY: {'dir': self.dir, 'user_temp': True}})
        put(os.path.join(self.dir, 'main.log'))
        with mock.patch.object(S, 'registry_user_temp', lambda: None):
            code, text = self.main('--apply')
        self.assertEqual(code, 0, text)
        self.assertIn('user temp: skipped', text)
        self.assertIn('removed  main.log', text)

    def test_user_temp_equal_to_or_inside_the_main_root_is_swept_once(self):
        self.config({S.KEY: {'dir': self.dir, 'user_temp': True}})
        old = self.session(self.dir, 'proj', SID_OLD)
        for user in (self.dir, os.path.join(self.dir, 'nested')):
            os.makedirs(user, exist_ok=True)
            with mock.patch.object(S, 'registry_user_temp', lambda u=user: u):
                code, text = self.main()
            self.assertEqual(code, 0, text)
            self.assertIn('user temp: skipped', text)
            self.assertEqual(text.count('would    claude/proj/%s' % SID_OLD), 1, text)
        self.assertTrue(os.path.isdir(old))

    def test_user_temp_unreadable_process_list_exits_3(self):
        self.config({S.KEY: {'dir': self.dir, 'user_temp': True}})
        old = self.session(self.user, 'proj', SID_OLD)
        code, text = self.main('--apply', lister=unreadable)
        self.assertEqual(code, S.EXIT_LIST_UNAVAILABLE, text)
        self.assertTrue(os.path.isdir(old))

    # --- the one-time drain ---

    def test_drain_exit_code_is_4_and_free(self):
        self.assertEqual(S.EXIT_DRAIN_REFUSED, 4)
        self.assertNotIn(S.EXIT_DRAIN_REFUSED, (0, 1, 2, S.EXIT_LIST_UNAVAILABLE))

    def test_drain_is_refused_while_a_claude_process_predates_the_change(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        lister = started(('claude.exe', at(CHANGE + 60)), ('Claude.exe', at(CHANGE - 60)))
        code, text = self.main('--drain-old-user-temp', '--apply', lister=lister)
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        self.assertIn('pid 101', text)              # the one that predates the change (case-blind name)
        self.assertNotIn('pid 100', text)
        self.assertTrue(os.path.isdir(old))
        code, text = self.main('--drain-old-user-temp', lister=lister)     # the dry run says so too
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        self.assertIn('would    claude/proj/%s' % SID_OLD, text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_removes_old_units_once_every_claude_process_is_newer(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        new = self.session(self.local, 'proj', SID_NEW, young=True)
        put(os.path.join(self.local, 'other.tmp'))
        put(os.path.join(self.dir, 'main.log'))
        lister = started(('claude.exe', at(CHANGE + 60)), ('node.exe', at(CHANGE - 600)))
        code, text = self.main('--drain-old-user-temp', lister=lister)       # dry run first
        self.assertEqual(code, 0, text)
        self.assertIn('would    claude/proj/%s' % SID_OLD, text)
        self.assertTrue(os.path.isdir(old))
        code, text = self.main('--drain-old-user-temp', '--apply', lister=lister)
        self.assertEqual(code, 0, text)
        self.assertIn('removed  claude/proj/%s' % SID_OLD, text)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))
        self.assertTrue(os.path.isfile(os.path.join(self.local, 'other.tmp')))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'main.log')))  # the drain sweeps only its root

    def test_drain_is_refused_when_the_process_list_or_the_change_time_cannot_be_read(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        code, text = self.main('--drain-old-user-temp', '--apply', lister=unreadable)
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        with mock.patch.object(S, 'env_changed_at', lambda: None):
            code, text = self.main('--drain-old-user-temp', '--apply')
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        code, text = self.main('--drain-old-user-temp', '--apply',
                               lister=started(('claude.exe', 'not a time')))
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_is_refused_when_the_registry_temp_is_relative_or_unexpanded(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        for u in ('%USERPROFILE%\\AppData\\Local\\Temp', 'rel\\temp'):
            with mock.patch.object(S, 'registry_user_temp', lambda u=u: u):
                code, text = self.main('--drain-old-user-temp', '--apply')
            self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
            self.assertIn('not absolute', text)
            self.assertTrue(os.path.isdir(old), u)

    def test_drain_is_refused_when_an_absolute_registry_temp_holds_an_unexpanded_var(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        u = os.path.join(self.tmp, '%USERNAME%', 'Temp')
        with mock.patch.object(S, 'registry_user_temp', lambda: u):
            code, text = self.main('--drain-old-user-temp', '--apply')
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_is_refused_without_a_registry_temp(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        with mock.patch.object(S, 'registry_user_temp', lambda: None):
            code, text = self.main('--drain-old-user-temp', '--apply')
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        self.assertIn('no TEMP or TMP', text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_is_refused_for_an_empty_process_list(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        code, text = self.main('--drain-old-user-temp', '--apply', lister=lambda: [])
        self.assertEqual(code, S.EXIT_DRAIN_REFUSED, text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_has_nothing_to_do_when_the_folder_lies_in_dir(self):
        self.config({S.KEY: {'dir': self.local}})
        old = self.session(self.local, 'proj', SID_OLD)
        code, text = self.main('--drain-old-user-temp', '--apply')
        self.assertEqual(code, 0, text)
        self.assertIn('nothing to drain', text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_has_nothing_to_do_when_the_registry_temp_is_the_default(self):
        self.config({S.KEY: {'dir': self.dir}})
        old = self.session(self.local, 'proj', SID_OLD)
        with mock.patch.object(S, 'registry_user_temp', lambda: self.local):
            code, text = self.main('--drain-old-user-temp', '--apply')
        self.assertEqual(code, 0, text)
        self.assertIn('nothing to drain', text)
        self.assertTrue(os.path.isdir(old))

    def test_drain_has_nothing_to_do_without_the_folder(self):
        self.config({S.KEY: {'dir': self.dir}})
        code, text = self.main('--drain-old-user-temp', '--apply')
        self.assertEqual(code, 0, text)
        self.assertIn('nothing to drain', text)

    def test_the_change_time_and_start_times_parse(self):
        self.assertAlmostEqual(S.started_at(at(CHANGE)), CHANGE, places=3)
        self.assertAlmostEqual(S.started_at('2026-10-16T09:56:05.7019330Z'),
                               S.started_at('2026-10-16T05:56:05.701933-04:00'), places=3)
        self.assertIsNone(S.started_at('soon'))
        self.assertIsNone(S.started_at(None))


if __name__ == '__main__':
    unittest.main()
