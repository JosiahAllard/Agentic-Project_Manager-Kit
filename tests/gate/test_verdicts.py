"""kitlib.gate.verdicts: the verdict queue on the scratch repository of tests/gate/fixtures.py
GateRepoCase."""
import contextlib
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


class Check(unittest.TestCase):
    def test_a_reviewer_that_names_no_agent_type_is_refused(self):
        tip = 'a' * 40
        for reviewer in ('pm-kit:', ' / ', ':', ', ;', 'pm-kit: / other:'):
            for stored in (False, True):
                e = given('kit/a', tip, reviewer=reviewer)
                if stored:
                    e.update(queued_utc='2026-01-01T00:00:00Z', state=V.QUEUED, last_result=None)
                with self.subTest(reviewer=reviewer, stored=stored):
                    self.assertIn('reviewer names no agent type', V.check(e, stored=stored) or '')
        for reviewer in ('pm-kit:analyst', 'reviewer', 'pm-kit: / analyst'):
            with self.subTest(reviewer=reviewer):
                self.assertIsNone(V.check(given('kit/a', tip, reviewer=reviewer), stored=False))


class SelfReviewWhy(unittest.TestCase):
    """A High-risk verdict the queueing coordinator reviewed itself is refused (verdicts.
    self_review_why); a Low or Medium self-read, or a High one by another reviewer, is not."""

    def test_a_high_risk_coordinator_self_review_is_refused_with_the_instead_line(self):
        tip = 'a' * 40
        for over in ({'reviewer_id': 'coord-1'}, {'reviewer_id': ' Coord-1 '},
                     {'reviewer_id': 'agent-x / coord-1'},
                     {'session': ' Coord-1 ', 'reviewer_id': 'coord-1'},
                     {'reviewer': 'pm-kit:coordinator'}, {'reviewer': 'Coordinator'},
                     {'reviewer': 'kit-coordinator'}, {'reviewer': 'pm-kit:analyst / pm-kit:coordinator'},
                     {'reviewer': 'pm-kit:coordinator', 'reviewer_id': 'coord-1'}):
            with self.subTest(**over):
                why = V.self_review_why(given('kit/a', tip, risk='High', **over))
                self.assertIn('a High-risk verdict needs a reviewer other than the coordinator', why or '')
                self.assertEqual(why.splitlines()[-1], V.SELF_REVIEW_INSTEAD)
                self.assertIn('--reviewer pm-kit:analyst --reviewer-id <its agent id>', why)

    def test_a_small_self_read_and_another_reviewer_stay_allowed(self):
        tip = 'a' * 40
        for risk in ('Low', 'Medium'):
            for over in ({'reviewer_id': 'coord-1'}, {'reviewer': 'pm-kit:coordinator'}):
                with self.subTest(risk=risk, **over):
                    self.assertIsNone(V.self_review_why(given('kit/a', tip, risk=risk, **over)))
        for over in ({}, {'reviewer': 'pm-kit:analyst', 'reviewer_id': 'agent-a7'},
                     {'reviewer_id': 'coord-10'}, {'reviewer': 'pm-kit:coordinators-helper'}):
            with self.subTest(**over):
                self.assertIsNone(V.self_review_why(given('kit/a', tip, risk='High', **over)))


class FakeClock(object):
    """time.monotonic and time.sleep for the `with` of patched(): a sleep is recorded and moves
    the clock on at once, so a retry loop's tries and pauses are counted exactly."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(time, 'monotonic', self.monotonic))
        stack.enter_context(mock.patch.object(time, 'sleep', self.sleep))
        return stack


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


def lines(n, tag='x'):
    return ''.join('%s %d\n' % (tag, i) for i in range(n))


class SelfReviewSize(QueueCase):
    """With "landing".self_review_max_lines given, add() refuses a verdict of any risk the queueing
    coordinator reviewed itself when its range changes more lines than that, or git cannot say
    (verdicts.self_review_size_why); a dispatched reviewer, or no cap, queues as before."""
    CAP = 4

    def add(self, tip, cap=CAP, base=None, main=None, main_why=None, **over):
        return V.add(self.common, given('kit/a', tip, base=base or self.main[:10], **over),
                     self_review_max_lines=cap, main=main or self.main, main_why=main_why)

    def refused(self, tip, cap=CAP, **over):
        with self.assertRaises(V.SelfReview) as cm:
            self.add(tip, cap=cap, **over)
        self.assertEqual(self.folder_names(), [])
        why = str(cm.exception)
        self.assertEqual(why.splitlines()[-1], V.SELF_REVIEW_INSTEAD)
        self.assertIn('("landing".self_review_max_lines)', why)
        self.assertIn('nothing was queued', why)
        return why

    def test_a_self_read_over_the_cap_is_refused_with_the_counts(self):
        _, tip = self.branch('a', {'a.txt': lines(3), 'b.txt': lines(2)})
        for risk in ('Low', 'Medium'):
            with self.subTest(risk=risk):
                why = self.refused(tip, risk=risk, reviewer_id='coord-1')
                self.assertIn('covers at most 4 changed lines', why)
                self.assertIn('changes 5 (+5 -0 in 2 files; the larger of the range given and %s...%s from '
                              'main as fetched)' % (self.main[:10], tip[:10]), why)
                self.assertIn('its reviewer id coord-1 is the session queueing it', why)

    def test_the_coordinator_role_as_reviewer_counts_as_a_self_read(self):
        _, tip = self.branch('a', {'a.txt': lines(5)})
        why = self.refused(tip, reviewer='pm-kit:coordinator', reviewer_id='agent-z')
        self.assertIn('its reviewer pm-kit:coordinator is the coordinator role', why)
        self.assertIn('changes 5 (+5 -0 in 1 file;', why)

    def test_a_self_read_at_the_cap_is_queued(self):
        wt, _ = self.branch('a', {'a.txt': lines(3)})
        tip = self.more(wt, 'a', {'a.txt': lines(4)})        # +4 -0 from main in all
        _, stored = self.add(tip, reviewer_id='coord-1')
        self.assertEqual(stored['tip'], tip)

    def test_removed_lines_count_too(self):
        self.repo.write('m.txt', lines(3))
        main2 = self.repo.commit('main work', 'm.txt')
        wt = self.repo.worktree('b')
        self.repo.write('m.txt', lines(1), root=wt)                  # -2 against main2
        self.repo.write('n.txt', lines(3), root=wt)                  # +3
        tip = self.repo.commit('work on b', 'm.txt', 'n.txt', cwd=wt)
        self.assertEqual(V.diff_lines(self.common, '%s..%s' % (main2, tip)), (3, 2, 2))
        with self.assertRaises(V.SelfReview) as cm:
            V.add(self.common, given('kit/b', tip, base=main2[:10], reviewer_id='coord-1'),
                  self_review_max_lines=4, main=main2)
        self.assertIn('changes 5 (+3 -2 in 2 files;', str(cm.exception))

    def test_commits_the_base_gained_after_the_branch_left_it_are_not_counted(self):
        _, tip = self.branch('a', {'a.txt': lines(2)})
        self.repo.write('m.txt', lines(50))
        main2 = self.repo.commit('main moved on', 'm.txt')
        _, stored = self.add(tip, base=main2[:10], main=main2, reviewer_id='coord-1')
        self.assertEqual(stored['tip'], tip)

    def test_a_narrow_stated_base_on_a_big_branch_is_refused(self):
        wt, first = self.branch('a', {'a.txt': lines(5)})
        tip = self.more(wt, 'a', {'b.txt': lines(1)})
        self.assertEqual(V.diff_lines(self.common, '%s..%s' % (first, tip)), (1, 0, 1))
        why = self.refused(tip, base=first[:10], reviewer_id='coord-1')
        self.assertIn('%s..%s changes 6 (+6 -0 in 2 files; the larger of the range given and '
                      '%s...%s from main as fetched)' % (first[:10], tip[:10], self.main[:10],
                                                         tip[:10]), why)

    def test_a_branch_that_merged_main_in_is_not_over_counted(self):
        wt, _ = self.branch('a', {'a.txt': lines(2)})
        self.repo.write('m.txt', lines(50))
        main2 = self.repo.commit('main moved on', 'm.txt')
        self.repo.git('merge', '-q', '--no-ff', '--no-edit', main2, cwd=wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertEqual(V.diff_lines(self.common, '%s..%s' % (main2, tip)), (2, 0, 1))
        _, stored = self.add(tip, base=main2[:10], main=main2, reviewer_id='coord-1')
        self.assertEqual(stored['tip'], tip)

    def test_main_as_fetched_unread_is_refused_only_with_a_cap_on_a_self_read(self):
        _, tip = self.branch('a', {'a.txt': lines(1)})
        with self.assertRaises(V.SelfReview) as cm:
            V.add(self.common, given('kit/a', tip, base=self.main[:10], reviewer_id='coord-1'),
                  self_review_max_lines=self.CAP, main=None, main_why='origin/main: no such ref')
        why = str(cm.exception)
        self.assertIn('main as fetched cannot be read (origin/main: no such ref), so the size of '
                      '%s..%s cannot be told' % (self.main[:10], tip[:10]), why)
        self.assertEqual(why.splitlines()[-1], V.SELF_REVIEW_INSTEAD)
        self.assertEqual(self.folder_names(), [])
        for over, cap in (({'reviewer_id': 'coord-1'}, None),
                          ({'reviewer': 'pm-kit:analyst', 'reviewer_id': 'a7'}, self.CAP)):
            with self.subTest(cap=cap, **over):
                _, stored = V.add(self.common, given('kit/a', tip, base=self.main[:10], **over),
                                  self_review_max_lines=cap, main=None, main_why='no ref')
                self.assertEqual(stored['tip'], tip)

    def test_a_binary_file_counts_no_lines(self):
        _, tip = self.branch('a', {'a.txt': lines(4), 'img.bin': 'PNG\x00\x01\x02' * 40})
        _, stored = self.add(tip, reviewer_id='coord-1')
        self.assertEqual(stored['tip'], tip)
        self.assertEqual(V.diff_lines(self.common, '%s..%s' % (self.main, tip)), (4, 0, 2))

    def test_a_dispatched_reviewer_or_no_cap_queues_any_size(self):
        _, tip = self.branch('a', {'a.txt': lines(40)})
        _, stored = self.add(tip, reviewer='pm-kit:analyst', reviewer_id='agent-a7')
        self.assertEqual(stored['tip'], tip)
        _, stored = self.add(tip, cap=None, reviewer_id='coord-1')
        self.assertEqual(stored['reviewer_id'], 'coord-1')

    def test_a_size_git_cannot_tell_is_refused_only_with_a_cap(self):
        _, tip = self.branch('a', {'a.txt': lines(1)})
        why = self.refused(tip, base='nosuchbase', reviewer_id='coord-1')
        self.assertIn('git cannot tell how many lines nosuchbase..%s changes' % tip[:10], why)
        _, stored = self.add(tip, cap=None, base='nosuchbase', reviewer_id='coord-1')
        self.assertEqual(stored['tip'], tip)
        _, stored = self.add(tip, base='nosuchbase', reviewer='pm-kit:analyst', reviewer_id='a7')
        self.assertEqual(stored['tip'], tip)

    def test_a_high_risk_self_review_is_still_refused_whatever_its_size(self):
        _, tip = self.branch('a', {'a.txt': lines(1)})
        with self.assertRaises(V.SelfReview) as cm:
            self.add(tip, risk='High', reviewer_id='coord-1')
        self.assertIn('a High-risk verdict needs a reviewer other than', str(cm.exception))


class SelfReviewSizeRenames(QueueCase):
    """diff_lines finds renames whatever the repository's diff.renames says, so a moved file
    counts only its edits towards "landing".self_review_max_lines."""

    def test_a_renamed_file_counts_only_its_edits(self):
        self.repo.git('config', 'diff.renames', 'false')    # a user's config must not matter
        self.repo.write('big.txt', lines(30))
        main2 = self.repo.commit('main work', 'big.txt')
        wt = self.repo.worktree('r')
        self.repo.git('mv', 'big.txt', 'moved.txt', cwd=wt)
        self.repo.write('moved.txt', lines(29) + 'edited\n', root=wt)
        tip = self.repo.commit('move big', 'moved.txt', cwd=wt)
        self.assertEqual(V.diff_lines(self.common, '%s..%s' % (main2, tip)), (1, 1, 1))
        _, stored = V.add(self.common, given('kit/r', tip, base=main2[:10], reviewer_id='coord-1'),
                          self_review_max_lines=4, main=main2)
        self.assertEqual(stored['tip'], tip)


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

    def test_entries_are_oldest_first_and_the_branch_name_settles_a_tie(self):
        folder = V.queue_dir(self.common)
        os.makedirs(folder)
        queued = [('kit/c', '2026-01-01T00:00:03Z'), ('kit/b', '2026-01-01T00:00:01Z'),
                  ('kit/d', '2026-01-01T00:00:02Z'), ('kit/a', '2026-01-01T00:00:01Z')]
        for branch, when in queued:
            with open(os.path.join(folder, V.file_name(branch)), 'w', encoding='utf-8') as f:
                json.dump(dict(given(branch, 'a' * 40), queued_utc=when, state=V.QUEUED,
                               last_result=None), f)
        want = ['kit/a', 'kit/b', 'kit/d', 'kit/c']          # a and b tie in time: name first
        self.assertEqual([e['branch'] for e in V.entries(self.common)], want)
        good, bad = V.scan(self.common)
        self.assertEqual(([e['branch'] for e in good], bad), (want, []))
        # the folder lists names in order, so the tie is shown again from reversed input
        self.assertEqual([e['branch'] for e in V.order(list(reversed(good)))], want)

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

    def test_a_high_risk_self_review_is_refused_and_nothing_written(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        for over in ({'reviewer_id': 'coord-1'}, {'reviewer': 'pm-kit:coordinator'}):
            with self.subTest(**over), self.assertRaises(V.SelfReview) as cm:
                V.add(self.common, given('kit/a', tip, risk='High', **over))
            self.assertIsInstance(cm.exception, V.BadEntry)
        self.assertEqual(self.folder_names(), [])
        _, stored = V.add(self.common, given('kit/a', tip, risk='Medium', reviewer_id='coord-1'))
        self.assertEqual(stored['reviewer_id'], 'coord-1')       # a small self-read is queued

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

    def test_a_replace_held_open_is_tried_five_times_0_2_s_apart_then_a_verdict_error(self):
        # The equivalence test of _write's move onto kitlib.fs.replace: the loop it replaced
        # tried RETRIES (5) times, sleeping RETRY_SLEEP (0.2 s) between, then raised the
        # PermissionError (a VerdictError here). The clock is faked, so the count is exact.
        for fails, tries, written in ((2, 3, True), (4, 5, True), (5, 5, False), (9, 5, False)):
            with self.subTest(fails=fails):
                name = 'held%d' % fails
                _, tip = self.branch(name, {'%s.txt' % name: 'x\n'})
                real, calls, clock = os.replace, [], FakeClock()

                def flaky(a, b):
                    calls.append(b)
                    if len(calls) <= fails:
                        raise PermissionError('held open')
                    return real(a, b)
                with clock.patched(), mock.patch.object(V.os, 'replace', flaky):
                    if written:
                        V.add(self.common, given('kit/' + name, tip))
                    else:
                        with self.assertRaises(V.VerdictError) as cm:
                            V.add(self.common, given('kit/' + name, tip))
                self.assertEqual(len(calls), tries)
                self.assertEqual(clock.sleeps, [0.2] * (tries - 1))
                queued = [e['branch'] for e in V.entries(self.common)]
                self.assertEqual('kit/' + name in queued, written)
                if not written:
                    self.assertIn('held open', str(cm.exception))
                self.assertEqual([n for n in self.folder_names() if n.endswith('.tmp')], [])

    def test_mark_failed_holds_until_a_re_add(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        V.add(self.common, given('kit/a', tip))
        self.assertFalse(V.mark_failed(self.common, 'kit/a', 'red', tip=self.main))
        self.assertTrue(V.mark_failed(self.common, 'kit/a', 'merged gate red: unit', tip=tip))
        [e] = V.entries(self.common)
        self.assertEqual((e['state'], e['last_result']), (V.FAILED, 'merged gate red: unit'))
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

    def test_a_file_holding_another_branchs_entry_is_not_offered_to_drop(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        folder = V.queue_dir(self.common)
        os.makedirs(folder)
        with open(os.path.join(folder, 'kit%2Fc.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(given('kit/C', tip), queued_utc=V.utc(), state='queued',
                           last_result=None), f)
        with self.assertRaises(V.VerdictError) as cm:
            V.entries(self.common)
        self.assertIn('kit%2Fc.json', str(cm.exception))
        self.assertIn('remove it by hand', str(cm.exception))
        self.assertNotIn('tools/verdict.py drop', str(cm.exception))   # drop would refuse it
        with self.assertRaises(V.VerdictError):
            V.drop(self.common, 'kit/c')
        self.assertEqual(self.folder_names(), ['kit%2Fc.json'])

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


class Expire(QueueCase):
    """expire(): an entry whose tip is in main as fetched, or whose branch is gone, is dropped;
    a live one, and any whose fate git cannot tell, is kept."""

    def queued(self, name):
        wt, tip = self.branch(name, {'%s.txt' % name: name + '\n'})
        return wt, V.add(self.common, given('kit/%s' % name, tip))[1]

    def land(self, tip):
        """Push `tip` as the remote's main and fetch it: origin/main as fetched holds it."""
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % tip)
        self.repo.git('fetch', '-q', 'origin')
        return self.repo.git('rev-parse', 'refs/remotes/origin/main')

    def gone(self, wt, branch):
        self.repo.git('worktree', 'remove', '--force', wt)
        self.repo.git('branch', '-D', branch)

    def test_a_landed_entry_is_dropped_and_a_live_one_kept(self):
        _, landed = self.queued('a')
        _, live = self.queued('b')
        main = self.land(landed['tip'])
        gone = V.expire(self.common, V.entries(self.common), main)
        self.assertEqual([(e['branch'], why.split(':')[0]) for e, why in gone],
                         [('kit/a', 'landed')])
        self.assertEqual(V.entries(self.common), [live])

    def test_an_entry_whose_branch_is_gone_is_superseded(self):
        wt, _ = self.queued('a')
        self.gone(wt, 'kit/a')
        gone = V.expire(self.common, V.entries(self.common), self.main)
        self.assertEqual([(e['branch'], why) for e, why in gone],
                         [('kit/a', 'superseded: there is no branch kit/a')])
        self.assertEqual(self.folder_names(), [])

    def test_a_moved_branch_keeps_its_entry_for_a_carry(self):
        wt, e = self.queued('a')
        self.more(wt, 'a', {'a.txt': 'moved\n'})
        self.assertEqual(V.expire(self.common, V.entries(self.common), self.main), [])
        self.assertEqual(V.entries(self.common), [e])

    def test_nothing_is_dropped_when_ancestry_cannot_be_told(self):
        wt, e = self.queued('a')
        self.gone(wt, 'kit/a')              # gone, yet kept: its tip's fate in main is unknown
        for main in (None, ''):
            self.assertEqual(V.expire(self.common, [e], main), [], main)
        with mock.patch.object(V.refs, 'is_ancestor', return_value=(None, 'git gave no answer')):
            self.assertEqual(V.expire(self.common, [e], self.main), [])
        self.assertEqual(V.entries(self.common), [e])

    def test_a_branch_git_cannot_read_keeps_its_entry(self):
        _, e = self.queued('a')
        with mock.patch.object(V.refs, 'ref_commit', return_value=(None, 'no answer')):
            self.assertEqual(V.expire(self.common, [e], self.main), [])
        self.assertEqual(V.entries(self.common), [e])

    def test_an_entry_re_added_at_another_tip_meanwhile_is_kept(self):
        wt, first = self.queued('a')
        second = self.more(wt, 'a', {'a.txt': 'b\n'})
        again = V.add(self.common, given('kit/a', second))[1]
        main = self.land(first['tip'])      # the old tip landed; the re-added one did not
        self.assertEqual(V.expire(self.common, [first], main), [])
        self.assertEqual(V.entries(self.common), [again])


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

    def test_a_lasting_permission_error_on_the_lock_refuses_after_the_wait_naming_it(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        err = PermissionError('denied by test')
        with mock.patch.object(V, 'LOCK_WAIT_S', 0.3, create=True), \
                mock.patch.object(V.os, 'open', side_effect=err) as opened:
            with self.assertRaises(V.VerdictError) as cm:
                V.add(self.common, given('kit/a', tip))
        self.assertGreater(opened.call_count, 1)         # retried, as a lock being removed is
        self.assertIn(str(err), str(cm.exception))
        self.assertNotIn('locked for', str(cm.exception))
        self.assertEqual(self.folder_names(), [])

    def test_another_os_error_on_the_lock_refuses_at_once_naming_it(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        err = OSError('disk said no')
        with mock.patch.object(V, 'LOCK_WAIT_S', 0.3, create=True), \
                mock.patch.object(V.os, 'open', side_effect=err) as opened:
            with self.assertRaises(V.VerdictError) as cm:
                V.add(self.common, given('kit/a', tip))
        self.assertEqual(opened.call_count, 1)
        self.assertIn(str(err), str(cm.exception))
        self.assertEqual(self.folder_names(), [])

    def test_a_permission_error_on_the_lock_that_clears_is_waited_out(self):
        _, tip = self.branch('a', {'a.txt': 'a\n'})
        real, denied = os.open, []

        def open_(path, *a, **k):
            if os.path.basename(path) == '.queue.lock' and not denied:
                denied.append(path)
                raise PermissionError('delete pending, by test')
            return real(path, *a, **k)
        with mock.patch.object(V.os, 'open', side_effect=open_):
            V.add(self.common, given('kit/a', tip))
        self.assertEqual(len(denied), 1)
        self.assertEqual([(e['tip'], e['state']) for e in V.entries(self.common)],
                         [(tip, V.QUEUED)])
        self.assertEqual(self.folder_names(), ['kit%2Fa.json'])

    def test_a_lock_removal_held_open_is_tried_five_times_0_2_s_apart_then_left(self):
        # The equivalence test of _locked's lock removal onto kitlib.fs.replace.remove: the loop
        # it replaced tried RETRIES (5) times, sleeping RETRY_SLEEP (0.2 s) between, and a lock
        # it could not remove was left to the stale-lock rule; the add itself stands either way.
        folder = V.queue_dir(self.common)
        lock = os.path.join(folder, V.LOCK_NAME)
        for fails, tries, removed in ((2, 3, True), (4, 5, True), (5, 5, False), (9, 5, False)):
            with self.subTest(fails=fails):
                name = 'unlock%d' % fails
                _, tip = self.branch(name, {'%s.txt' % name: 'x\n'})
                real, calls, clock = os.remove, [], FakeClock()

                def flaky(path):
                    if os.path.basename(path) == V.LOCK_NAME:
                        calls.append(path)
                        if len(calls) <= fails:
                            raise PermissionError('held open')
                    return real(path)
                with clock.patched(), mock.patch.object(V.os, 'remove', flaky):
                    V.add(self.common, given('kit/' + name, tip))
                left = os.path.exists(lock)
                if left:
                    os.remove(lock)              # as the stale-lock rule would, later
                self.assertEqual(len(calls), tries)
                self.assertEqual(clock.sleeps, [0.2] * (tries - 1))
                self.assertEqual(left, not removed)
                self.assertIn('kit/' + name, [e['branch'] for e in V.entries(self.common)])

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


if __name__ == '__main__':
    unittest.main()
