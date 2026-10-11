"""kitlib.gate.land.queue (land.py --queue) with a lock file that is not a record: shown as held by
nobody named (lane ?), with the read error, and the queue still listed - the state in which
docs/gate.md "The landing queue" says to check --queue before a lock is cleared by hand."""
import unittest

from kitlib import gate as G
from kitlib.gate import land
from kitlib.lock import resource as lockres
from tests.gate.test_land import LandCase


class AnUnreadableLock(LandCase):

    def write_lock(self, data):
        with open(self.lock().path, 'wb') as f:
            f.write(data)

    def joined(self, label):
        turn = lockres.join(self.store, land.queue_key(self.lock().common), label)
        self.addCleanup(turn.leave)
        return turn

    def shown(self):
        return land.queue(self.repo.root, self.store)

    def assert_held_by_nobody_named(self, out, error):
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[0].startswith('landing lock: HELD - lane ?'), out.lines)
        self.assertIn('unreadable', out.lines[0])
        self.assertIn(error, out.lines[0])

    def test_an_empty_lock_is_held_and_the_queue_is_listed(self):
        self.write_lock(b'')
        self.joined('repo full: kit/waiting')
        out = self.shown()
        self.assert_held_by_nobody_named(out, 'is not a lock record')
        self.assertEqual(out.lines[1], 'landing queue: 1 waiting or holding')
        self.assertEqual(len(out.lines), 3, out.lines)
        self.assertIn('repo full: kit/waiting', out.lines[2])

    def test_a_lock_that_is_not_utf8_is_held_with_an_empty_queue(self):
        self.write_lock(b'\xff\xfe\x80\n')
        out = self.shown()
        self.assert_held_by_nobody_named(out, 'cannot read')
        self.assertEqual(out.lines[1:], ['landing queue: empty'])

    def test_a_free_lock_is_still_free(self):
        out = self.shown()
        self.assertEqual(out.lines, ['landing lock: free', 'landing queue: empty'])


if __name__ == '__main__':
    unittest.main()
