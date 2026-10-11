"""kitlib.records.landing: break_lock(holder_gone=True) never removes a lock whose file is empty or
not a JSON record - what a taker leaves between its exclusive create and its write
(KIT-LANDLOCK-BREAK-LIVE). Fail closed: such a file is never judged "holder gone"."""
import json
import os
import unittest
from unittest import mock

from kitlib.records import landing
from tests import gitfixture

ALIVE = lambda pid, start: True      # noqa: E731


class BreakLiveCase(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)

    def lock(self):
        return landing.LandingLock(self.repo.root, start_of=lambda pid: 1234, is_alive=ALIVE)

    def write_raw(self, text):
        with open(self.lock().path, 'w', encoding='utf-8') as f:
            f.write(text)

    def on_disk(self):
        with open(self.lock().path, encoding='utf-8') as f:
            return f.read()


class ATakerMidWriteTest(BreakLiveCase):

    def test_a_taker_between_its_create_and_its_write_keeps_its_lock(self):
        # The taker's half of LandingLock.take, by hand: the exclusive create, then (while a
        # break is under way) its write. The break's remove is refused while the taker's fd is
        # open; the taker writes and closes during the break's first retry sleep.
        lk = self.lock()
        fd = os.open(lk.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        record = {'lane': 'full', 'branches': ['kit/taker'], 'pid': None, 'pid_start': None,
                  'token': 'feedfacefeedface', 'phase': 'merge'}
        state = {'fd': fd}

        def taker_finishes(_s):
            if state['fd'] is not None:
                os.write(state['fd'], (json.dumps(record) + '\n').encode('utf-8'))
                os.close(state['fd'])
                state['fd'] = None

        def close_left():
            if state['fd'] is not None:
                os.close(state['fd'])
        self.addCleanup(close_left)
        with mock.patch.object(landing.time, 'sleep', taker_finishes):
            try:
                ok, why = lk.break_lock(holder_gone=True)
            except landing.LockError as e:       # a remove that gave up is also not a break
                ok, why = False, str(e)
        taker_finishes(None)        # the taker writes now if the break never slept
        self.assertFalse(ok, why)
        self.assertEqual(json.loads(self.on_disk()), record)


class AnUnreadableFileTest(BreakLiveCase):

    def check_refused(self, text):
        self.write_raw(text)
        ok, why = self.lock().break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('not a JSON record', why)
        self.assertEqual(self.on_disk(), text)

    def test_an_empty_lock_file_is_not_broken_even_when_declared_gone(self):
        self.check_refused('')

    def test_a_half_written_record_is_not_broken(self):
        whole = json.dumps({'lane': 'full', 'token': 't' * 16, 'pid': None}) + '\n'
        self.check_refused(whole[:len(whole) // 2])

    def test_json_that_is_no_object_is_not_broken(self):
        self.check_refused('[]\n')

    def test_a_file_that_cannot_be_read_is_not_broken(self):
        self.write_raw('{"lane": "full"}')
        lk = self.lock()
        with mock.patch.object(lk, '_text', lambda: None):
            ok, why = lk.break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('cannot be read', why)
        self.assertTrue(os.path.exists(lk.path))


class WhatStillBreaksTest(BreakLiveCase):

    def test_an_old_format_record_with_no_process_still_breaks_when_declared_gone(self):
        self.write_raw(json.dumps({'lane': 'pvp', 'holder': 'an older lander'}) + '\n')
        self.assertFalse(self.lock().break_lock()[0])
        ok, rec = self.lock().break_lock(holder_gone=True)
        self.assertTrue(ok, rec)
        self.assertFalse(os.path.exists(self.lock().path))

    def test_a_free_lock_still_has_nothing_to_break(self):
        self.assertEqual(self.lock().break_lock(True),
                         (False, 'nobody holds the landing lock: nothing to break'))


if __name__ == '__main__':
    unittest.main()
