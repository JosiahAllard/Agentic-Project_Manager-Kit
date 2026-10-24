"""kitlib.records.landing: a lock file that is not UTF-8 is unreadable, so held - never a
UnicodeDecodeError out of read, take, break_lock or gate_refusal, and never broken."""
import os
import unittest

from kitlib.records import landing
from tests import gitfixture

ALIVE = lambda pid, start: True      # noqa: E731
NOT_UTF8 = b'\xff\xfe{"lane": "full"}\x80\n'


class NotUtf8Test(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.lk = landing.LandingLock(self.repo.root, start_of=lambda pid: 1234, is_alive=ALIVE)
        with open(self.lk.path, 'wb') as f:
            f.write(NOT_UTF8)

    def still_there(self):
        with open(self.lk.path, 'rb') as f:
            self.assertEqual(f.read(), NOT_UTF8)

    def test_read_is_a_lock_error(self):
        with self.assertRaises(landing.LockError) as got:
            self.lk.read()
        self.assertIn('cannot read', str(got.exception))

    def test_take_is_busy_by_nobody_named(self):
        with self.assertRaises(landing.Busy) as got:
            self.lk.take(['kit/a'], pid=42)
        self.assertEqual(got.exception.record['phase'], '?')
        self.still_there()

    def test_break_lock_refuses_even_when_declared_gone(self):
        ok, why = self.lk.break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('cannot be read', why)
        self.assertIn('only once no landing runs', why)
        self.still_there()

    def test_a_gate_is_refused(self):
        self.assertIn('cannot be read', self.lk.gate_refusal())
        self.assertTrue(os.path.exists(self.lk.path))


if __name__ == '__main__':
    unittest.main()
