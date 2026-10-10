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
        put(os.path.join(self.home, '.claude-pm-kit', 'config.json'), json.dumps(cfg))

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

    def test_its_config_folder_is_the_kits(self):
        # The script ships alone, so it keeps its own copy of the folder name.
        from kitlib.config import user
        self.assertEqual(S.CONFIG_DIR, user.DIR_NAME)

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

    def test_an_unsafe_dir_is_refused(self):
        self.config({S.KEY: {'dir': self.home}})
        code, _out, err = self.main('--apply')
        self.assertEqual(code, 2)
        self.assertIn('home folder', err)

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


if __name__ == '__main__':
    unittest.main()
