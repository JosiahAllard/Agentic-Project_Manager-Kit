"""kitlib.gate.verdicts: the verdict queue on the scratch repository of tests/gate/fixtures.py
GateRepoCase, and ready() on fake tip_of and strict_hits callables."""
import hashlib
import json
import os
import threading
import time
import unittest
from unittest import mock

from kitlib.gate import verdicts as V
from tests.gate.fixtures import GateRepoCase


def given(branch, tip, base='0' * 10, **over):
    """An entry as the coordinator states it."""
    e = {'branch': branch, 'tip': tip, 'verdict': 'LAND', 'risk': 'Low', 'reviewer': 'reviewer',
         'reviewer_model': 'opus', 'reviewer_id': 'agent-r1', 'author': 'Kit Test / worker',
         'range': '%s..%s' % (base, tip[:10]), 'session': 'coord-1', 'records': ['EX-1']}
    e.update(over)
    return e


class QueueCase(GateRepoCase):
    def setUp(self):
        GateRepoCase.setUp(self)
        self.common = self.repo.common_dir()

    def more(self, wt, name, files):
        """Another commit on the worktree `wt`: its tip."""
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        return self.repo.commit('work on %s' % name, *files, cwd=wt)

    def folder_names(self):
        try:
            return sorted(os.listdir(V.queue_dir(self.common)))
        except FileNotFoundError:
            return []


class Queue(QueueCase):
    def test_add_list_drop_round_trip(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        path, stored = V.add(self.common, given('kit/a', tip))
        self.assertEqual(path, os.path.join(self.common, 'kit-verdicts', 'kit%2Fa.json'))
        self.assertEqual(stored['state'], V.QUEUED)
        self.assertIsNone(stored['last_result'])
        self.assertRegex(stored['queued_utc'], V.UTC_RE)
        self.assertEqual(V.entries(self.common), [stored])
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), stored)
        self.assertTrue(V.drop(self.common, 'kit/a'))
        self.assertEqual(V.entries(self.common), [])
        self.assertFalse(V.drop(self.common, 'kit/a'))

    def test_the_file_name_round_trips_a_branch_with_slashes(self):
        _, tip = self.branch('deep/x', {'x.txt': 'x\n'})
        path, _ = V.add(self.common, given('kit/deep/x', tip))
        self.assertEqual(os.path.basename(path), 'kit%2Fdeep%2Fx.json')
        self.assertEqual(V.branch_of('kit%2Fdeep%2Fx.json'), 'kit/deep/x')
        self.assertEqual([e['branch'] for e in V.entries(self.common)], ['kit/deep/x'])
        self.assertEqual(V.file_name('a%2Fb'), 'a%252Fb.json')       # '%' itself is encoded
        self.assertEqual(V.branch_of('a%252Fb.json'), 'a%2Fb')
        self.assertIsNone(V.branch_of('kit%2fdeep%2fx.json'))       # not a name file_name makes

    def test_add_refuses_a_tip_that_is_not_the_branch_head(self):
        self.branch('a', {'a.txt': 'a\n'})
        with self.assertRaises(V.TipMismatch) as cm:
            V.add(self.common, given('kit/a', self.main))
        self.assertIn('kit/a is at', str(cm.exception))
        with self.assertRaises(V.TipMismatch):
            V.add(self.common, given('kit/nosuch', self.main))
        self.assertEqual(self.folder_names(), [])

    def test_a_re_add_replaces_the_entry(self):
        wt, first = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', first))
        second = self.more(wt, 'a', {'a.txt': 'b\n'})
        V.add(self.common, given('kit/a', second, risk='Medium'))
        got = V.entries(self.common)
        self.assertEqual([(e['tip'], e['risk']) for e in got], [(second, 'Medium')])
        self.assertEqual(self.folder_names(), ['kit%2Fa.json'])

    def test_a_moved_tip_is_stale_and_never_in_the_batch(self):
        wt, tip = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', tip))
        moved = self.more(wt, 'a', {'a.txt': 'moved\n'})
        batch, skipped = V.ready(V.entries(self.common),
                                 lambda b: V.head_of(self.common, b), lambda e: [], 5)
        self.assertEqual(batch, [])
        self.assertEqual([(s.branch, s.reason) for s in skipped], [('kit/a', V.SKIP_STALE)])
        self.assertIn(moved[:10], skipped[0].why)

    def test_a_malformed_entry_is_refused_and_nothing_written(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        bad = [given('kit/a', tip, reviewer_id=''),
               given('kit/a', tip, verdict='MAYBE'),
               given('kit/a', tip, risk='low'),
               given('kit/a', tip, range='main..kit/a'),
               given('kit/a', tip, range='abc..%s' % self.main[:10]),   # another commit reviewed
               given('kit/a', tip[:12]),
               given('kit/a', tip, state='queued'),                     # add sets it, not the caller
               given('refs/heads/kit/a', tip)]
        for missing in ('reviewer', 'reviewer_model', 'reviewer_id', 'author'):
            e = given('kit/a', tip)
            del e[missing]
            bad.append(e)
        for e in bad:
            with self.assertRaises(V.BadEntry, msg=e):
                V.add(self.common, e)
        self.assertEqual(self.folder_names(), [])

    def test_a_windows_device_name_is_refused(self):
        for name in ('con', 'NUL', 'com1', 'aux.x'):
            with self.assertRaises(V.BadEntry, msg=name):
                V.file_name(name)

    def test_a_failed_replace_leaves_no_temp_file_and_the_old_entry(self):
        wt, first = self.branch('a', {'a.txt': 'a\n'})
        path, old = V.add(self.common, given('kit/a', first))
        second = self.more(wt, 'a', {'a.txt': 'b\n'})
        with mock.patch.object(V.os, 'replace', side_effect=OSError('replace refused')) as rep:
            with self.assertRaises(V.VerdictError) as cm:
                V.add(self.common, given('kit/a', second))
        self.assertEqual(rep.call_count, 1)
        self.assertIn('replace refused', str(cm.exception))
        self.assertEqual(self.folder_names(), ['kit%2Fa.json'])        # no .tmp left
        self.assertEqual(V.entries(self.common), [old])

    def test_mark_failed_keeps_it_out_until_a_re_add(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', tip))
        self.assertFalse(V.mark_failed(self.common, 'kit/a', 'red', tip=self.main))
        self.assertTrue(V.mark_failed(self.common, 'kit/a', 'merged gate red: unit', tip=tip))
        [e] = V.entries(self.common)
        self.assertEqual((e['state'], e['last_result']), (V.FAILED, 'merged gate red: unit'))
        batch, skipped = V.ready([e], lambda b: tip, lambda e: [], 5)
        self.assertEqual((batch, [s.reason for s in skipped]), ([], [V.SKIP_FAILED]))
        V.add(self.common, given('kit/a', tip))
        [e] = V.entries(self.common)
        self.assertEqual((e['state'], e['last_result']), (V.QUEUED, None))
        self.assertFalse(V.mark_failed(self.common, 'kit/nosuch', 'red'))

    def test_drop_with_a_tip_keeps_an_entry_queued_at_another_tip(self):
        wt, first = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', first))
        second = self.more(wt, 'a', {'a.txt': 'b\n'})
        V.add(self.common, given('kit/a', second))       # re-queued while the first was landing
        self.assertFalse(V.drop(self.common, 'kit/a', tip=first))
        self.assertEqual([e['tip'] for e in V.entries(self.common)], [second])
        self.assertTrue(V.drop(self.common, 'kit/a', tip=second))

    def test_a_file_that_is_not_an_entry_stops_entries_and_is_named(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', tip))
        folder = V.queue_dir(self.common)
        with open(os.path.join(folder, 'kit%2Fb.json'), 'w', encoding='utf-8') as f:
            f.write('{not json')
        with open(os.path.join(folder, 'kit%2Fc.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(given('kit/C', tip), queued_utc=V.utc(), state='queued',
                           last_result=None), f)                       # another branch's entry
        with self.assertRaises(V.VerdictError) as cm:
            V.entries(self.common)
        self.assertIn('kit%2Fb.json', str(cm.exception))
        self.assertIn('tools/verdict.py drop kit/b', str(cm.exception))
        good, bad = V.scan(self.common)
        self.assertEqual([e['branch'] for e in good], ['kit/a'])
        self.assertEqual(sorted(os.path.basename(p) for p, _ in bad), ['kit%2Fb.json', 'kit%2Fc.json'])
        _, tip_b = self.branch('b', {'b.txt': 'b\n'})
        with self.assertRaises(V.VerdictError) as cm:
            V.add(self.common, given('kit/b', tip_b))   # an unreadable entry is not replaced
        self.assertIs(type(cm.exception), V.VerdictError)
        self.assertIn('not a verdict entry', str(cm.exception))
        with self.assertRaises(V.OtherBranch):
            V.drop(self.common, 'kit/B')                 # unreadable, and named for kit/b
        self.assertTrue(V.drop(self.common, 'kit/b'))    # drop without a tip removes it
        with self.assertRaises(V.OtherBranch):
            V.drop(self.common, 'kit/c')                 # it holds kit/C's entry
        self.assertTrue(V.drop(self.common, 'kit/C'))
        self.assertEqual([e['branch'] for e in V.entries(self.common)], ['kit/a'])

    def test_drop_refuses_a_branch_differing_only_in_case(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', tip))
        for t in (None, tip):
            with self.assertRaises(V.VerdictError, msg=t) as cm:
                V.drop(self.common, 'kit/A', tip=t)
            self.assertEqual(type(cm.exception).__name__, 'OtherBranch')
        self.assertEqual([e['branch'] for e in V.entries(self.common)], ['kit/a'])

    def test_a_temp_file_left_by_a_dead_write_is_named_and_kept(self):
        folder = V.queue_dir(self.common)
        os.makedirs(folder)
        tmp = os.path.join(folder, '.kit%2Fa.json.0123abcd.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('{')
        good, bad = V.scan(self.common)
        self.assertEqual((good, [p for p, _ in bad]), ([], [tmp]))
        self.assertIn('temp file', bad[0][1])
        with self.assertRaises(V.VerdictError) as cm:
            V.entries(self.common)
        self.assertIn('remove it by hand', str(cm.exception))
        self.assertNotIn('tools/verdict.py drop', str(cm.exception))
        self.assertEqual(self.folder_names(), [os.path.basename(tmp)])

    def test_drop_says_so_when_the_queue_folder_cannot_be_listed(self):
        folder = V.queue_dir(self.common)
        os.makedirs(folder)
        with open(os.path.join(folder, 'kit%2Fb.json'), 'w', encoding='utf-8') as f:
            f.write('{not json')
        real = os.listdir

        def listdir(p):
            if os.path.normcase(p) == os.path.normcase(folder):
                raise OSError('listing refused')
            return real(p)
        with mock.patch.object(V.os, 'listdir', side_effect=listdir):
            with self.assertRaises(V.VerdictError) as cm:
                V.drop(self.common, 'kit/b')
        self.assertIn('listing refused', str(cm.exception))
        self.assertEqual(self.folder_names(), ['kit%2Fb.json'])

    def test_add_refuses_a_windows_device_name(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        for name in ('con', 'aux.x'):
            with self.assertRaises(V.BadEntry, msg=name):
                V.add(self.common, given(name, tip))
        self.assertEqual(self.folder_names(), [])


class Lock(QueueCase):
    def race(self, call):
        """Queue kit/a at its first tip, move it, then run call(first) with an add of the new tip
        started between call's read of the entry and its write. The read waits (no timing) until
        the add either finds the queue locked or has finished, and records what the add had done
        then. `(first, second, (blocked, finished, tip on disk) at that moment, queue after)`."""
        wt, first = self.branch('a', {'a.txt': 'a\n'})
        path, _ = V.add(self.common, given('kit/a', first))
        second = self.more(wt, 'a', {'a.txt': 'b\n'})
        real_load, real_clear = V._load, getattr(V, '_clear_stale', lambda p: None)
        racer, failed, seen = [], [], []
        blocked, finished, moved = threading.Event(), threading.Event(), threading.Event()

        def add_second():
            try:
                V.add(self.common, given('kit/a', second))
            except Exception as e:                       # seen by the assertion below
                failed.append(e)
            finally:
                finished.set()
                moved.set()

        def clear(lock):                                 # the add found the lock held
            blocked.set()
            moved.set()
            return real_clear(lock)

        def load(path_, branch):
            got = real_load(path_, branch)
            if not racer:
                racer.append(threading.Thread(target=add_second))
                racer[0].start()
                moved.wait(60)                           # a hang guard, not a timing assumption
                with open(path, encoding='utf-8') as f:
                    seen.append((blocked.is_set(), finished.is_set(), json.load(f)['tip']))
            return got
        with mock.patch.object(V, '_load', side_effect=load), \
                mock.patch.object(V, '_clear_stale', side_effect=clear, create=True):
            self.assertTrue(call(first))
        racer[0].join(60)
        self.assertEqual(failed, [])
        return first, second, seen[0], V.entries(self.common)

    def assert_add_waited(self, first, second, seen, queue):
        self.assertEqual(seen, (True, False, first))     # blocked on the lock, nothing written
        self.assertEqual([(e['tip'], e['state']) for e in queue], [(second, V.QUEUED)])

    def test_an_add_racing_a_drop_survives(self):
        self.assert_add_waited(*self.race(lambda first: V.drop(self.common, 'kit/a', tip=first)))

    def test_an_add_racing_mark_failed_survives(self):
        self.assert_add_waited(*self.race(
            lambda first: V.mark_failed(self.common, 'kit/a', 'red', tip=first)))

    def test_a_lock_that_cannot_be_made_is_refused_at_once_with_its_cause(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        for err in (PermissionError('denied by test'), OSError('disk said no')):
            with mock.patch.object(V, 'LOCK_WAIT_S', 0.3, create=True), \
                    mock.patch.object(V.os, 'open', side_effect=err) as opened:
                with self.assertRaises(V.VerdictError) as cm:
                    V.add(self.common, given('kit/a', tip))
            self.assertEqual(opened.call_count, 1, err)
            self.assertIn(str(err), str(cm.exception))
            self.assertNotIn('locked for', str(cm.exception))
        self.assertEqual(self.folder_names(), [])

    def test_a_held_lock_is_waited_for_and_a_stale_one_removed(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        folder = V.queue_dir(self.common)
        os.makedirs(folder)
        lock = os.path.join(folder, '.queue.lock')
        with open(lock, 'w', encoding='utf-8') as f:
            f.write('pid 1\n')
        with mock.patch.object(V, 'LOCK_WAIT_S', 0.3, create=True):
            with self.assertRaises(V.VerdictError) as cm:
                V.add(self.common, given('kit/a', tip))
        self.assertIn('locked', str(cm.exception))
        self.assertEqual(self.folder_names(), ['.queue.lock'])
        old = time.time() - 3600
        os.utime(lock, (old, old))
        V.add(self.common, given('kit/a', tip))
        self.assertEqual(self.folder_names(), ['kit%2Fa.json'])     # the lock is gone after


def entry(branch, utc, tip=None, **over):
    """A stored entry for ready(), no repository needed."""
    tip = tip or hashlib.sha1(branch.encode('utf-8')).hexdigest()
    state = over.pop('state', V.QUEUED)
    last_result = over.pop('last_result', None)
    return dict(given(branch, tip, **over), queued_utc=utc, state=state, last_result=last_result)


def tips(items):
    return {e['branch']: e['tip'] for e in items}.get


class Ready(unittest.TestCase):
    def test_high_strict_and_failed_entries_are_never_in_the_batch(self):
        items = [entry('kit/low', '2026-01-01T00:00:01Z'),
                 entry('kit/high', '2026-01-01T00:00:02Z', risk='High'),
                 entry('kit/strict', '2026-01-01T00:00:03Z', risk='Medium'),
                 entry('kit/failed', '2026-01-01T00:00:04Z', state=V.FAILED, last_result='red'),
                 entry('kit/med', '2026-01-01T00:00:05Z', risk='Medium')]
        hits = {'kit/strict': ['kitlib/gate/land.py']}
        batch, skipped = V.ready(items, tips(items), lambda e: hits.get(e['branch'], []), 10)
        self.assertEqual([e['branch'] for e in batch], ['kit/low', 'kit/med'])
        self.assertEqual([(s.branch, s.reason) for s in skipped],
                         [('kit/high', V.SKIP_HIGH), ('kit/strict', V.SKIP_STRICT),
                          ('kit/failed', V.SKIP_FAILED)])
        self.assertIn('kitlib/gate/land.py', skipped[1].why)

    def test_batch_max_caps_the_batch_oldest_first(self):
        items = [entry('kit/c', '2026-01-01T00:00:03Z'), entry('kit/a', '2026-01-01T00:00:01Z'),
                 entry('kit/d', '2026-01-01T00:00:04Z'), entry('kit/b', '2026-01-01T00:00:02Z')]
        batch, skipped = V.ready(items, tips(items), lambda e: [], 2)
        self.assertEqual([e['branch'] for e in batch], ['kit/a', 'kit/b'])
        self.assertEqual([(s.branch, s.reason) for s in skipped],
                         [('kit/c', V.SKIP_LATER), ('kit/d', V.SKIP_LATER)])

    def test_a_tie_in_time_is_settled_by_the_branch_name(self):
        items = [entry('kit/b', '2026-01-01T00:00:01Z'), entry('kit/a', '2026-01-01T00:00:01Z')]
        batch, _ = V.ready(items, tips(items), lambda e: [], 1)
        self.assertEqual([e['branch'] for e in batch], ['kit/a'])

    def test_strict_unknown_and_a_gone_branch_are_held(self):
        items = [entry('kit/unknown', '2026-01-01T00:00:01Z'),
                 entry('kit/gone', '2026-01-01T00:00:02Z')]
        known = tips(items)
        batch, skipped = V.ready(items, lambda b: None if b == 'kit/gone' else known(b),
                                 lambda e: None, 5)
        self.assertEqual(batch, [])
        self.assertEqual([(s.branch, s.reason) for s in skipped],
                         [('kit/unknown', V.SKIP_STRICT), ('kit/gone', V.SKIP_STALE)])

    def test_strict_hits_that_is_not_a_list_holds_the_entry(self):
        for hits in (None, False, 0, '', (), {}):
            items = [entry('kit/a', '2026-01-01T00:00:01Z')]
            batch, skipped = V.ready(items, tips(items), lambda e: hits, 5)
            self.assertEqual((batch, [s.reason for s in skipped]), ([], [V.SKIP_STRICT]), msg=hits)
            self.assertIn('cannot tell', skipped[0].why)

    def test_a_stale_entry_is_not_asked_about_strict_paths(self):
        items = [entry('kit/a', '2026-01-01T00:00:01Z', risk='High')]
        asked = []
        batch, skipped = V.ready(items, lambda b: 'f' * 40, lambda e: asked.append(e) or [], 5)
        self.assertEqual(([s.reason for s in skipped], asked), ([V.SKIP_STALE], []))

    def test_a_malformed_entry_is_skipped_as_invalid(self):
        bad = entry('kit/a', '2026-01-01T00:00:01Z')
        del bad['reviewer_model']
        batch, skipped = V.ready([bad, 'junk'], lambda b: bad['tip'], lambda e: [], 5)
        self.assertEqual(batch, [])
        self.assertEqual(sorted(s.reason for s in skipped), [V.SKIP_INVALID, V.SKIP_INVALID])

    def test_batch_max_is_a_whole_number_of_at_least_one(self):
        for n in (0, -1, 1.5, True, None):
            with self.assertRaises(ValueError, msg=n):
                V.ready([], lambda b: None, lambda e: [], n)


if __name__ == '__main__':
    unittest.main()
