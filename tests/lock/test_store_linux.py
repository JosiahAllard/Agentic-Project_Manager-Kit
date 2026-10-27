"""The lock store's exclusion on Linux (docs/linux-support-plan.md section 8, slice 3): a named
lock (Store.lock) and a run's liveness lock (Store.run_mutex, run_mutex_held, run_dead) are flocks
on files in a scratch store (kitlib.platform.linux.locks). Two processes contend for a named lock;
a killed holder frees it; run_dead sees a killed run. Store() with no root is the machine's store
under the profile folder from the password database (slice 9), fenced under the tests.

BumpRaises runs on every platform: a lock the platform cannot take (PlatformUnsupported, an
OSError) is raised by Store.bump, never skipped as a busy lock is, so a stub cannot hide there."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import kitlib.platform
from kitlib.config.user import HomeFenced
from kitlib.lock import store as lockstore
from kitlib.lock.store import Store
from kitlib.platform import PlatformUnsupported
from kitlib.platform.linux import paths
from tests.home import unmarked_env
from tests.platforms import posix_only

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The password database's home folder in MachineStore: /home/u on Linux (on Windows, with a drive).
PW_DIR = os.path.abspath(os.path.join(os.sep, 'home', 'u'))
RID = 'r-20261026T000000Z-abcd'
# A child that holds, in the scratch store at argv[2], the named lock argv[4] (argv[3] "lock") or
# the run lock of id argv[4] (argv[3] "run"), says "held", and keeps it until its stdin closes.
HOLDER = ('import sys; sys.path.insert(0, sys.argv[1]); from kitlib.lock.store import Store\n'
          's = Store(root=sys.argv[2])\n'
          'if sys.argv[3] == "run":\n'
          '    m = s.run_mutex(sys.argv[4]); print("held", flush=True); sys.stdin.read()\n'
          'else:\n'
          '    with s.lock(sys.argv[4], 5):\n'
          '        print("held", flush=True); sys.stdin.read()\n')


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-lock-linux-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store(root=self.root)

    def holder(self, what, name):
        """A child process holding `what` ("lock" or "run") `name` in the scratch store."""
        p = subprocess.Popen([sys.executable, '-c', HOLDER, ROOT, self.root, what, name],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._end, p)
        self.assertEqual(p.stdout.readline().strip(), 'held')
        return p

    @staticmethod
    def _end(p):
        if p.poll() is None:
            p.kill()             # only this test's own child, by its handle
        p.wait(30)
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass

    def assert_not_free(self, name, wait_s=0.2):
        with self.assertRaises(OSError) as cm:
            with self.store.lock(name, wait_s):
                pass
        self.assertNotIsInstance(cm.exception, PlatformUnsupported)
        self.assertIn('the %s mutex was not free within' % name, str(cm.exception))


@posix_only('the store locks are flocks on Linux')
class NamedLock(Scratch):
    def test_a_second_process_waits_and_gets_it_once_released(self):
        p = self.holder('lock', 'queue')
        self.assert_not_free('queue')
        p.stdin.close()
        p.wait(30)
        with self.store.lock('queue', 5):
            pass
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'locks', 'queue.lock')))

    def test_a_killed_holder_frees_it(self):
        p = self.holder('lock', 'queue')
        self.assert_not_free('queue')
        p.kill()
        p.wait(30)
        with self.store.lock('queue', 5):
            pass

    def test_another_name_is_another_lock(self):
        self.holder('lock', 'queue')
        with self.store.lock('holds', 0):
            pass

    def test_it_nests_in_its_thread_and_excludes_another_thread(self):
        seen = []

        def other():
            try:
                with self.store.lock('queue', 0.2):
                    seen.append('took')
            except OSError as e:
                seen.append(str(e))
        with self.store.lock('queue'):
            with self.store.lock('queue', 0):
                t = threading.Thread(target=other)
                t.start()
                t.join(30)
        self.assertEqual(len(seen), 1)
        self.assertIn('not free within', seen[0])
        t = threading.Thread(target=other)
        t.start()
        t.join(30)
        self.assertEqual(seen[1], 'took')

    def test_bump_counts_under_the_lock(self):
        self.store.bump()
        self.store.bump()
        self.assertEqual(self.store.revision(), 2)


@posix_only('the run locks are flocks on Linux')
class RunLiveness(Scratch):
    def alive_file(self):
        return os.path.join(self.root, 'runs', RID + '.alive')

    def test_run_dead_sees_a_killed_run(self):
        p = self.holder('run', RID)
        entry = {'id': RID, 'pid': p.pid, 'ctime': None}
        self.assertIs(self.store.run_mutex_held(RID), True)
        self.assertFalse(self.store.run_dead(entry))
        p.kill()
        p.wait(30)
        self.assertIs(self.store.run_mutex_held(RID), False)
        self.assertTrue(self.store.run_dead(entry))

    def test_a_released_run_is_dead_and_leaves_no_file(self):
        m = self.store.run_mutex(RID)
        self.assertTrue(os.path.isfile(self.alive_file()))
        self.assertIs(self.store.run_mutex_held(RID), True)
        m.release()
        self.assertFalse(os.path.exists(self.alive_file()))
        self.assertIs(self.store.run_mutex_held(RID), False)
        self.assertTrue(self.store.run_dead({'id': RID, 'pid': os.getpid()}))

    def test_an_unknown_run_is_dead(self):
        self.assertIs(self.store.run_mutex_held(RID), False)

    def test_a_second_holder_is_refused_until_the_first_lets_go(self):
        m = self.store.run_mutex(RID)
        with self.assertRaises(OSError) as cm:
            self.store.run_mutex(RID)
        self.assertIn('is owned by another process', str(cm.exception))
        m.release()
        self.store.run_mutex(RID).release()

    def test_a_run_lock_that_cannot_be_opened_cannot_be_told(self):
        if os.geteuid() == 0:
            self.skipTest('root opens any file')
        m = self.store.run_mutex(RID)
        self.addCleanup(m.release)
        os.chmod(self.alive_file(), 0)
        self.assertIsNone(self.store.run_mutex_held(RID))
        self.assertFalse(self.store.run_dead({'id': RID, 'pid': os.getpid()}))


class MachineStore(unittest.TestCase):
    """The machine's store on Linux (slice 9): ~/.claude/machine-lock under the profile folder
    from the password database, never $HOME; fenced under the tests like the Windows one. Runs on
    every platform: NAME and the password database are patched."""

    def setUp(self):
        for patch in (mock.patch.object(kitlib.platform, 'NAME', 'linux'),
                      mock.patch.object(paths, '_pw_dir', lambda: PW_DIR)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_real_root_is_under_the_password_database_s_home(self):
        with mock.patch.dict(os.environ, {'HOME': '/elsewhere'}):
            self.assertEqual(lockstore.profile_dir(), PW_DIR)
            self.assertEqual(lockstore.real_root(), os.path.join(PW_DIR, '.claude', 'machine-lock'))

    def test_no_root_is_the_machine_s_store_and_it_is_fenced(self):
        s = Store()
        self.assertFalse(s.testing)
        self.assertEqual(s.root, os.path.abspath(lockstore.real_root()))
        for call in (lambda: s.path('queue.json'), lambda: s.mutex_name('queue'),
                     lockstore.machine_home):
            with self.assertRaises(HomeFenced):
                call()

    def test_a_given_root_is_a_scratch_store(self):
        root = tempfile.mkdtemp(prefix='kit-lock-linux-')
        self.addCleanup(shutil.rmtree, root, True)
        s = Store(root=root)
        self.assertTrue(s.testing)
        self.assertEqual(s.root, os.path.abspath(root))
        self.assertEqual(s.path('queue.json'), os.path.join(os.path.abspath(root), 'queue.json'))

    def test_no_password_entry_is_an_os_error_not_a_scratch_store(self):
        def missing():
            raise KeyError('uid not found')
        with mock.patch.object(paths, '_pw_dir', missing):
            with self.assertRaises(OSError):
                Store()
            with self.assertRaises(OSError):
                Store(root=tempfile.gettempdir())


@posix_only('a real run on Linux: an unfenced child opens the machine store')
class RealMachineStore(unittest.TestCase):
    def test_an_unfenced_child_names_the_real_root(self):
        import pwd
        env = unmarked_env()
        env['HOME'] = tempfile.gettempdir()
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from kitlib.lock.store import Store\n'
                's = Store(); print(s.testing); print(s.root)')
        r = subprocess.run([sys.executable, '-c', code, ROOT], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split('\n')[:2],
                         ['False', os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.claude',
                                                'machine-lock')])


class BumpRaises(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-lock-bump-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store(root=self.root)

    def test_a_lock_the_platform_cannot_take_is_raised_not_skipped(self):
        with mock.patch.object(Store, 'lock', side_effect=PlatformUnsupported('no lock here')):
            with self.assertRaises(PlatformUnsupported):
                self.store.bump()
        self.assertEqual(self.store.revision(), 0)

    def test_a_lock_not_had_is_still_skipped(self):
        with mock.patch.object(Store, 'lock', side_effect=OSError('not free')):
            self.store.bump()
        self.assertEqual(self.store.revision(), 0)


if __name__ == '__main__':
    unittest.main()
