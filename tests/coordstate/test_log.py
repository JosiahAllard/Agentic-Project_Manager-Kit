"""kitlib.coordstate.log: the opt-in event log (docs/coord-state.md "The event log"): no file and
no folder until start; note() appends one whole line under the lock, gives up within BOUND
seconds on a held lock, stops at MAX_BYTES and never raises; compact keeps what was asked and
every line appended since the read; stop and start turn it off and on."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from kitlib import sessionmark
from kitlib.coordstate import log
from kitlib.fs import treelock
from tests.coordstate.helpers import SESSION, SLUG, TempAgentFolder

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHILD = '''
import sys
from kitlib.coordstate import log
path, who, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
for i in range(count):
    log.append(path, log.entry('backlog', {'verb': 'set', 'id': '%s-%d' % (who, i),
                                           'pad': 'x' * 300}), wait=30)
'''


class Off(TempAgentFolder):
    def test_no_log_no_line_and_no_file(self):
        self.assertIsNone(log.note('backlog', self.env, verb='add', id='A'))
        self.assertEqual(os.listdir(self.temp), [])

    def test_no_session_no_line(self):
        self.turn_on()
        env = dict(self.env)
        env.pop(sessionmark.SESSION_NAME_ENV)
        self.assertIsNone(log.note('backlog', env, verb='add', id='A'))
        env[sessionmark.SESSION_NAME_ENV] = '../evil'
        self.assertIsNone(log.note('backlog', env, verb='add', id='A'))

    def test_another_sessions_log_takes_no_line(self):
        f = self.turn_on()
        env = dict(self.env, **{sessionmark.SESSION_NAME_ENV: 'coord-other'})
        self.assertIsNone(log.note('backlog', env, verb='add', id='A'))
        self.assertEqual(self.events(f), [])


class On(TempAgentFolder):
    def test_start_makes_the_log_in_the_slug_folder(self):
        got = log.start(self.env, slug=SLUG)
        self.assertFalse(got.already)
        self.assertEqual(got.files.events,
                         os.path.join(self.temp, SLUG + log.FOLDER_SUFFIX, 'events-%s.jsonl' % SESSION))
        self.assertTrue(os.path.isfile(got.files.events))
        self.assertTrue(log.start(self.env, slug='other').already)

    def test_a_note_is_one_json_line(self):
        f = self.turn_on()
        self.assertEqual(log.note('backlog', self.env, verb='pull', id='A', branch=None), f.events)
        with open(f.events, encoding='utf-8') as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        ev = json.loads(lines[0])
        self.assertEqual((ev['v'], ev['kind'], ev['verb'], ev['id']), (1, 'backlog', 'pull', 'A'))
        self.assertNotIn('branch', ev)
        self.assertRegex(ev['utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_start_copies_a_hand_written_note_to_judgments(self):
        folder = os.path.join(self.temp, 'mine' + log.FOLDER_SUFFIX)
        os.makedirs(folder)
        with open(os.path.join(folder, 'state-%s.md' % SESSION), 'w', encoding='utf-8') as fh:
            fh.write('hand written\n')
        got = log.start(self.env, slug=SLUG)            # the note's folder wins over the slug
        self.assertTrue(got.seeded)
        self.assertEqual(got.files.folder, folder)
        with open(got.files.judgments, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'hand written\n')

    def test_a_rendered_note_is_not_copied_but_a_later_hand_written_one_is(self):
        f = self.turn_on()
        with open(f.state, 'w', encoding='utf-8') as fh:
            fh.write(log.NOTE_HEAD + SESSION + '\n')
        log.stop(self.env)
        self.assertFalse(log.start(self.env).seeded)
        self.assertFalse(os.path.exists(f.judgments))
        log.stop(self.env)
        with open(f.judgments, 'w', encoding='utf-8') as fh:
            fh.write('earlier\n')
        with open(f.state, 'w', encoding='utf-8') as fh:
            fh.write('by hand after the stop')
        self.assertTrue(log.start(self.env).seeded)
        with open(f.judgments, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'earlier\nby hand after the stop\n')

    def test_stop_then_start_keeps_the_lines(self):
        f = self.turn_on()
        log.note('backlog', self.env, verb='add', id='A')
        self.assertEqual(log.stop(self.env), f.off)
        self.assertIsNone(log.note('backlog', self.env, verb='add', id='B'))
        self.assertIsNone(log.stop(self.env))
        got = log.start(self.env)
        self.assertTrue(got.resumed)
        self.assertEqual([e['id'] for e in self.events(f)], ['A'])

    def test_start_without_a_session_is_refused(self):
        env = {'AGENT_TEMP': self.temp}
        with self.assertRaises(log.LogError):
            log.start(env, slug=SLUG)
        with self.assertRaises(log.LogError):
            log.start(self.env, slug=None)          # no folder holds a note, no slug
        self.assertEqual(os.listdir(self.temp), [])


class Bounds(TempAgentFolder):
    def test_a_held_lock_gives_up_within_the_bound(self):
        f = self.turn_on()
        held = treelock.TreeLock(f.lock).acquire()
        self.addCleanup(held.release)
        began = time.monotonic()
        self.assertIsNone(log.note('backlog', self.env, verb='add', id='A'))
        self.assertLess(time.monotonic() - began, log.BOUND + 0.5)
        with self.assertRaises(log.Busy):
            log.append(f.events, log.entry('backlog', {}), wait=0.05)
        held.release()
        self.assertEqual(self.events(f), [])

    def test_a_full_log_takes_no_line(self):
        f = self.turn_on()
        with mock.patch.object(log, 'MAX_BYTES', 200):
            log.note('backlog', self.env, verb='add', id='A')
            size = os.path.getsize(f.events)
            self.assertIsNone(log.note('backlog', self.env, verb='add', id='B' * 200))
            with self.assertRaises(log.Full):
                log.append(f.events, log.entry('backlog', {'id': 'B' * 200}))
        self.assertEqual(os.path.getsize(f.events), size)

    def test_note_never_raises(self):
        self.turn_on()
        with mock.patch.object(log, 'append', side_effect=OSError('disk gone')):
            self.assertIsNone(log.note('backlog', self.env, verb='add', id='A'))
        with mock.patch.object(log, 'locate', side_effect=RuntimeError('odd')):
            self.assertIsNone(log.note('backlog', self.env, verb='add', id='A'))

    def test_a_log_that_went_away_is_not_made_again(self):
        f = self.turn_on()
        os.remove(f.events)
        with self.assertRaises(FileNotFoundError):
            log.append(f.events, log.entry('backlog', {}))
        self.assertFalse(os.path.exists(f.events))


class Concurrent(TempAgentFolder):
    def test_lines_from_concurrent_processes_never_interleave(self):
        f = self.turn_on()
        procs, count = 4, 40
        kids = [subprocess.Popen([sys.executable, '-c', CHILD, f.events, 'p%d' % n, str(count)],
                                 cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for n in range(procs)]
        for kid in kids:
            out, err = kid.communicate(timeout=120)
            self.assertEqual(kid.returncode, 0, err.decode('utf-8', 'replace'))
        events, skipped, _size = log.read_events(f.events)
        self.assertEqual(skipped, 0)
        self.assertEqual(sorted(e['id'] for e in events),
                         sorted('p%d-%d' % (n, i) for n in range(procs) for i in range(count)))


class ReadAndCompact(TempAgentFolder):
    def test_a_torn_last_line_is_skipped(self):
        f = self.turn_on()
        log.note('backlog', self.env, verb='add', id='A')
        with open(f.events, 'ab') as fh:
            fh.write(b'{"v":1,"kind":"backl')
        events, skipped, _size = log.read_events(f.events)
        self.assertEqual(([e['id'] for e in events], skipped), (['A'], 1))

    def test_a_line_after_a_torn_one_stays_whole(self):
        f = self.turn_on()
        with open(f.events, 'ab') as fh:
            fh.write(b'{"v":1,"kind":"backl')
        log.note('backlog', self.env, verb='add', id='B')
        events, skipped, _size = log.read_events(f.events)
        self.assertEqual(([e['id'] for e in events], skipped), (['B'], 1))

    def test_compact_leaves_a_line_still_being_written(self):
        f = self.turn_on()
        log.note('backlog', self.env, verb='add', id='A')
        with open(f.events, 'ab') as fh:
            fh.write(b'{"v":1,"kind":"backlog","id":"B"')       # the read saw it half written
        events, _skipped, size = log.read_events(f.events)
        with open(f.events, 'ab') as fh:
            fh.write(b'}\n')
        self.assertTrue(log.compact(f.events, {e['_n'] for e in events}, size))
        self.assertEqual([e['id'] for e in self.events(f)], ['A', 'B'])

    def test_compact_keeps_the_asked_lines_and_every_later_one(self):
        f = self.turn_on()
        for rid in 'ABCD':
            log.note('backlog', self.env, verb='add', id=rid)
        events, _skipped, size = log.read_events(f.events)
        keep = {e['_n'] for e in events if e['id'] in ('B', 'D')}
        log.note('backlog', self.env, verb='add', id='E')     # after the read, before the compact
        self.assertTrue(log.compact(f.events, keep, size))
        self.assertEqual([e['id'] for e in self.events(f)], ['B', 'D', 'E'])

    def test_compact_after_another_compaction_writes_nothing(self):
        f = self.turn_on()
        for rid in 'AB':
            log.note('backlog', self.env, verb='add', id=rid)
        events, _skipped, size = log.read_events(f.events)
        self.assertTrue(log.compact(f.events, {events[1]['_n']}, size))
        self.assertFalse(log.compact(f.events, set(), size))
        self.assertEqual([e['id'] for e in self.events(f)], ['B'])


if __name__ == '__main__':
    unittest.main()
