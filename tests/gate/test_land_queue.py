"""kitlib.gate.land through the machine lock's landing queue, and batches: a landing books ONE
machine-lock run (resource queue_key(<git common dir>), priority "land") and its merged gate runs
in that slot; landings into one repository take turns, first come first served, each merging onto
main as it is at its turn; a gone holder of the landing lock file with nothing unpushed is broken,
one with something unpushed never is; a batch lands or is undone whole; `land.py --queue` lists
the landings in the queue. On a scratch repository with a bare remote and the scratch lock store
(tests/gate/fixtures.py)."""
import gc
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.gate import land, result, runner
from kitlib.lock import queue as lockqueue
from kitlib.lock.store import EXIT_USAGE, LockError
from tests import testutil
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, HANG_S, LandCase
from tests.platforms import windows_only

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def no_sleep(slept):
    """land's clock with a sleep that only records: a landing that never waits for the landing
    lock file leaves `slept` empty, however long its looks took on a loaded machine."""
    return mock.patch.object(land, 'time', mock.Mock(monotonic=time.monotonic,
                                                     sleep=slept.append))


def waited(said):
    return [s for s in said if s.startswith('waiting up to')]


class QueueCase(LandCase):
    def dead_pid(self):
        """The pid of a process that has ended: a landing lock holder that is surely gone. Its
        Popen is kept until the test's cleanup, so Windows cannot reuse the pid meanwhile
        (tests/testutil.py dead_process)."""
        p = testutil.dead_process()
        self.addCleanup(p.wait)             # holds the Popen, and its handle, until then
        return p.pid

    def key(self):
        return land.queue_key(self.lock().common)

    def entries(self):
        """The scratch machine lock's queue entries: a landing that ended left none."""
        return lockqueue.load(self.store)['entries']

    def queued(self, branch):
        return [e for e in self.entries() if e.get('slug') == runner.slug_of(branch)]

    def until(self, cond, what):
        end = time.monotonic() + HANG_S
        while not cond():
            if time.monotonic() > end:
                self.fail('never: %s' % what)
            time.sleep(0.02)

    def waiter(self, results, name, branch, tip, poll_s=0.05):
        """A landing on a thread with a wait budget; returns (thread, said). It waits for its turn
        in the machine lock (its queue entry `waiting`) or, where that books no resource, for the
        landing lock file (it says `waiting up to`): self.waiting(...) is either."""
        said = []
        t = threading.Thread(target=lambda: results.__setitem__(name, self.lander(
            branch, self.main, tip, wait_s=HANG_S, poll_s=poll_s, say=said.append).run()))
        t.start()
        self.addCleanup(t.join, HANG_S)
        return t, said

    def waiting(self, branch, said):
        return bool(waited(said)) or any(e.get('state') == 'waiting'
                                         for e in self.queued(branch))

    def held_gate(self, inside, go):
        """A merged gate that says it is inside, then waits for `go` before it gates in the slot
        the landing holds."""
        def gate(lander):
            inside.set()
            self.assertTrue(go.wait(HANG_S))
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      log_dir=self.logs, book_kw=self.book_kw, slot=lander.slot)
        return gate


class OneBooking(QueueCase):
    def test_the_machine_lock_books_the_landing_resource(self):
        # kitlib.gate.land books resource= and priority=: without the machine lock's landing queue
        # (kit/land-queue-lock) every real landing would raise TypeError. Red until both are in.
        self.assertTrue(fixtures.HAS_RESOURCE, 'kitlib.lock.run.book takes no resource=: the '
                        'machine lock\'s landing queue must land before this landing does')

    def test_a_landing_books_once_and_its_merged_gate_runs_in_that_slot(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len(self.book_calls), 1, self.book_calls)
        call = self.book_calls[0]
        cfg = kconfig.load(self.repo.root)
        self.assertEqual((call['kind'], call['repo'], call['slug']),
                         (cfg.section('gate')['kind'], runner.repo_name(cfg, self.repo.root),
                          'kit-feature'))
        self.assertEqual({k: call.get(k) for k in ('gate', 'resource', 'priority', 'store',
                                                   'wait_s', 'no_wait', 'cores', 'detached')},
                         {'gate': True, 'resource': self.key(), 'priority': 'land',
                          'store': self.store, 'wait_s': 0, 'no_wait': True, 'cores': 1,
                          'detached': None})
        self.assertEqual(call['note'], 'landing: kit/feature')
        self.assertTrue([s for s in said
                         if s.startswith('the gate runs in the slot the landing holds (run ')], said)
        self.assertEqual(len(self.store.history_records(1)), 1)    # one run, ended
        self.assertEqual(self.entries(), [])

    def test_the_wait_budget_is_the_configs_unless_given(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        refused = LockError(6, 'not now')
        for wait_s, want in ((None, kconfig.load(self.repo.root).section('landing')['wait_s']),
                             (7, 7), (0, 0)):
            with self.subTest(wait_s=wait_s):
                calls = []

                def book(kind, repo, slug, **kw):
                    calls.append(kw)
                    raise refused
                self.lander(BRANCH, self.main, tip, wait_s=wait_s, book=book).run()
                self.assertEqual([(c['wait_s'], c['no_wait']) for c in calls], [(want, want == 0)])

    def test_a_machine_lock_refusal_is_not_run_and_changes_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})

        def busy(kind, repo, slug, **kw):
            raise LockError(6, 'another landing of this repository holds its turn')
        out = self.lander(BRANCH, self.main, tip, book=busy).run()
        self.assertEqual(out.lines, ['NOT RUN: the machine lock did not give the landing its turn: '
                                     'another landing of this repository holds its turn; nothing '
                                     'was changed'])
        self.assert_untouched(out, 6)
        self.assertIsNone(out.result_record)
        self.assertEqual(result.read(self.repo.root, BRANCH, kind='land'), (None, 'none'))

        def usage(kind, repo, slug, **kw):
            raise LockError(EXIT_USAGE, 'usage: no kind "nosuch"')
        out = self.lander(BRANCH, self.main, tip, book=usage).run()
        self.assertEqual(out.code, G.USAGE, out.lines)
        self.assertTrue(out.lines[0].startswith('USAGE: the machine lock: no kind "nosuch"'),
                        out.lines)


class FirstComeFirstServed(QueueCase):
    def test_a_second_landing_waits_for_its_turn_and_lands_on_the_first(self):
        # either way the machine lock books: the second landing's turn comes once the first has
        # landed and given it up, and it merges onto main as the first left it
        _, tip_a = self.branch('feature', {'a.py': 'a\n'})
        _, tip_b = self.branch('bee', {'b.py': 'b\n'})
        inside, go = threading.Event(), threading.Event()
        results = {}
        a = threading.Thread(target=lambda: results.__setitem__(
            'a', self.lander(BRANCH, self.main, tip_a,
                             merged_gate=self.held_gate(inside, go)).run()))
        a.start()
        self.addCleanup(a.join, HANG_S)
        self.addCleanup(go.set)
        self.assertTrue(inside.wait(HANG_S))
        b, said = self.waiter(results, 'b', 'kit/bee', tip_b)
        self.until(lambda: self.waiting('kit/bee', said), 'kit/bee waits for its turn')
        self.assertNotIn('b', results)
        go.set()
        for t in (a, b):
            t.join(HANG_S)
            self.assertFalse(t.is_alive())
        self.assertEqual(results['a'].code, G.OK, results['a'].lines)
        self.assertEqual(results['b'].code, G.OK, results['b'].lines)
        merge_a = results['a'].merge
        self.assertEqual(self.repo.git('rev-parse', results['b'].merge + '^1'), merge_a)
        self.assertIn('main moved on from %s to %s; merging onto %s' % (
            self.main[:10], merge_a[:10], merge_a[:10]), said)
        self.assertEqual(self.remote_main(), results['b'].merge)
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.lock().read())

    def test_every_outcome_gives_the_turn_up(self):
        _, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.entries(), [])
        with mock.patch.object(land.Lander, 'do_merge', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.lock().read())
        self.assertEqual(len(self.book_calls), 2)

    def test_the_queue_is_one_per_repository(self):
        other = fixtures.gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(other.cleanup)
        wt = os.path.join(self.repo.root, '.claude', 'worktrees')
        self.branch('feature', {'a.py': 'a\n'})
        self.assertEqual(land.queue_key(self.lock().common),
                         land.queue_key(land.landing.LandingLock(os.path.join(wt, 'feature')).common))
        self.assertNotEqual(self.key(), land.queue_key(land.landing.LandingLock(other.root).common))


class GoneHolder(QueueCase):
    @windows_only('pid reuse is checked on Windows')
    def test_a_dead_pid_stays_unused_until_the_test_ends(self):
        # The helper once kept only the pid, the Popen's handle closed with it,
        # and Windows could give the pid to a live process mid-test
        pid = self.dead_pid()
        gc.collect()
        self.assertTrue(testutil.exited_object(pid))

    def test_a_gone_holder_with_nothing_unpushed_is_broken_and_the_landing_lands(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.lock().take(['kit/gone'], pid=self.dead_pid())
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(any(s.startswith('broke the landing lock of a holder that is gone')
                            for s in said), said)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertIsNone(self.lock().read())

    def test_a_gone_holder_with_an_unpushed_merge_is_reported_never_broken(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.repo.write('u.py', 'unpushed\n')
        local = self.repo.commit('left unpushed by the gone lander', 'u.py')
        rec = self.lock().take(['kit/gone'], pid=self.dead_pid())
        said, slept = [], []
        with no_sleep(slept):
            out = self.lander(BRANCH, self.main, tip, wait_s=60, poll_s=0.05,
                              say=said.append).run()
        self.assertEqual((slept, waited(said)), ([], []))   # BUSY at once: waiting cannot help
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertIn('not broken', out.lines[0])
        self.assertIn('break_lock', out.lines[0])
        self.assertEqual(self.lock().read()['token'], rec['token'])
        self.assertEqual(self.main_now(), local)
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.entries(), [])

    def test_a_holder_gone_after_its_push_names_the_next_landing_and_the_break(self):
        # A lander killed after its verified push, before the main checkout's fast-forward,
        # leaves the lock at phase push and main only behind origin/main: the next landing says
        # the push most likely landed, gives the fast-forward, and names the next landing and
        # break_lock; once main is fast-forwarded, a landing breaks the lock itself and lands.
        _, tip_a = self.branch('feature', {'a.py': 'a\n'})
        _, tip_b = self.branch('bee', {'b.py': 'b\n'})

        class Killed(BaseException):
            pass

        def no_recover(lander, e):          # killed outright: nothing recovers
            raise e
        with mock.patch.object(land.Lander, 'sync_main', side_effect=Killed), \
                mock.patch.object(land.Lander, 'recover', no_recover):
            with self.assertRaises(Killed):
                self.lander(BRANCH, self.main, tip_a, pid=self.dead_pid()).run()
        rec = self.lock().read()
        self.assertEqual(rec['phase'], 'push')
        pushed = self.remote_main()
        self.assertNotEqual(pushed, self.main)
        out = self.lander('kit/bee', self.main, tip_b).run()
        self.assertEqual(out.code, G.BUSY, out.lines)
        line = out.lines[0]
        for want in ('its holder is gone after its push: origin/main (%s) moved on from %s, '
                     'where it took the lock, so its push most likely landed and only the main '
                     'checkout\'s fast-forward was skipped' % (pushed[:10], self.main[:10]),
                     'Not broken, because main is only behind origin/main',
                     'merge --ff-only origin/main. Then land again: the next landing breaks it '
                     'itself; or break it with: ', 'break_lock()'):
            self.assertIn(want, line)
        self.assertEqual(self.lock().read()['token'], rec['token'])
        self.assertEqual(self.main_now(), self.main)
        self.repo.git('merge', '--ff-only', '--quiet', 'origin/main')     # as the hint says
        said = []
        out = self.lander('kit/bee', pushed, tip_b, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(any(s.startswith('broke the landing lock of a holder that is gone')
                            for s in said), said)
        self.assertIsNone(self.lock().read())


class Batch(QueueCase):
    def three(self, last=None):
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', last or {'two.py': '2\n'})
        return ['kit/one', 'kit/two'], [t1, t2]

    def test_a_green_batch_is_merged_in_order_gated_once_and_pushed(self):
        branches, tips = self.three()
        gates = []
        real = runner.merged_gate

        def counted(*a, **kw):
            gates.append((kw.get('slug'), kw.get('slot') is not None))
            return real(*a, **kw)
        with mock.patch.object(runner, 'merged_gate', counted):
            out = self.lander(branches, self.main, tips, summary='batch').run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(gates, [('kit/one+kit/two', True)])
        top = out.merge
        first = self.repo.git('rev-parse', top + '^1')
        self.assertEqual(self.repo.git('rev-parse', top + '^2'), tips[1])
        self.assertEqual(self.repo.git('rev-parse', first + '^1'), self.main)
        self.assertEqual(self.repo.git('rev-parse', first + '^2'), tips[0])
        self.assertEqual(self.repo.git('log', '-1', '--format=%s', first), 'Merge kit/one: batch')
        self.assertEqual(self.remote_main(), top)
        self.assertTrue(out.lines[-1].startswith('LANDED kit/one, kit/two on main at %s' % top))
        self.assertEqual(len(out.result_record), 2)
        for b, t in zip(branches, tips):
            rec, why = result.read(self.repo.root, b, kind='land')
            self.assertIsNone(why)
            self.assertEqual((rec['tip'], rec['verdict']), (t, 'pass'))
        self.assertEqual(self.entries(), [])

    def test_a_red_batch_is_undone_whole(self):
        branches, tips = self.three(last={'RED': 'x\n'})
        out = self.lander(branches, self.main, tips).run()
        self.assertTrue(out.undone)
        self.assertIn('NOT LANDED: the merge of kit/one, kit/two was undone', out.lines[-1])
        self.assert_untouched(out, G.FAILED)
        for b in branches:
            rec, why = result.read(self.repo.root, b, kind='land')
            self.assertIsNone(why)
            self.assertEqual(rec['verdict'], 'fail')


class Follow(QueueCase):
    """At its turn a landing merges onto <remote>/<main> as fetched: main moved on is said, never
    refused; a local main only behind is fast-forwarded first; a local main with commits the
    remote lacks is refused; the base's config must still take the landing."""

    def main_commit(self, name, kit=None, push=True):
        self.repo.write(name, name + '\n')
        paths = [name]
        if kit is not None:                     # this commit also changes .kit.json
            with open(os.path.join(self.repo.root, '.kit.json'), encoding='utf-8') as f:
                cfg = json.load(f)
            kit(cfg)
            self.repo.write('.kit.json', json.dumps(cfg, indent=1) + '\n')
            paths.append('.kit.json')
        sha = self.repo.commit('main: ' + name, *paths)
        if push:
            self.repo.git('push', '-q', '-f', 'origin', 'main')
        return sha

    def pushed_elsewhere(self, name, kit=None):
        """A commit on the remote's main that the main checkout does not have: made here, pushed,
        and main put back where it was (as when another clone pushed)."""
        before = self.main_now()
        sha = self.main_commit(name, kit)
        self.repo.git('reset', '-q', '--keep', before)
        return sha

    def test_a_main_only_behind_the_remote_is_fast_forwarded_and_followed(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        moved = self.pushed_elsewhere('o.py')
        self.assertEqual(self.main_now(), self.main)
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('fast-forwarded main from %s to origin/main %s' % (self.main[:10],
                                                                        moved[:10]), said)
        self.assertIn('main moved on from %s to %s; merging onto %s' % (
            self.main[:10], moved[:10], moved[:10]), said)
        self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), moved)
        self.assertEqual(self.main_now(), out.merge)
        self.assertEqual(self.remote_main(), out.merge)

    def test_head_switched_off_main_before_the_turn_is_refused_not_merged(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        take = land.Lander.take

        def switch_then_take(lander):       # after preconditions() checked HEAD, before the lock
            self.repo.git('switch', '-q', '-c', 'side')
            return take(lander)
        with mock.patch.object(land.Lander, 'take', switch_then_take):
            out = self.lander(BRANCH, None, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn("the main checkout is on side, not main, at the landing's turn", out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assertEqual(self.repo.git('rev-parse', 'side'), self.main)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])

    def test_a_main_behind_that_is_not_clean_is_not_fast_forwarded(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        moved = self.pushed_elsewhere('o.py')
        self.repo.write('README.md', 'edited, not committed\n')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('main (%s) is behind origin/main (%s), and the main checkout is not clean, '
                      'so it is not fast-forwarded' % (self.main[:10], moved[:10]), out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assertEqual(self.main_now(), self.main)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])

    def test_a_main_with_commits_the_remote_lacks_is_refused(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        local = self.main_commit('u.py', push=False)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('main is at %s, with commits origin/main (%s) does not have: push them or '
                      'set them aside by hand' % (local[:10], self.main[:10]), out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assertEqual(self.main_now(), local)
        self.assertEqual(self.remote_main(), self.main)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.records(), [])

    def test_a_base_whose_config_names_another_main_or_remote_is_refused(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        changes = {
            'main': lambda c: c.setdefault('repo', {}).update(main_branch='trunk'),
            'remote': lambda c: c.setdefault('repo', {}).update(remote='upstream'),
        }
        for n, (what, change) in enumerate(sorted(changes.items())):
            with self.subTest(what=what):
                self.repo.git('reset', '-q', '--keep', self.main)       # each from the base
                self.repo.git('push', '-q', '-f', 'origin', 'main')
                moved = self.pushed_elsewhere('m%d.py' % n, kit=change)
                out = self.lander(BRANCH, self.main, tip).run()
                self.assertEqual(out.code, G.REFUSED, out.lines)
                self.assertIn('main moved on from %s to %s, and the config there names another '
                              'main branch or remote than this landing holds: land again (the '
                              'next landing reads the config at %s)' % (
                                  self.main[:10], moved[:10], moved[:10]), out.lines[0])
                self.assertIn('nothing was merged', out.lines[0])
                self.assertEqual(self.main_now(), moved)            # fast-forwarded, no more
                self.assertEqual(self.remote_main(), moved)
                self.assertIsNone(self.lock().read())
                self.assertEqual(self.entries(), [])


def entry(id_, ticket, state, slug, resource, priority='land', **kw):
    return dict({'id': id_, 'ticket': ticket, 'state': state, 'pid': ticket * 10, 'repo': 'repo',
                 'slug': slug, 'kind': 'engine-test', 'since': 'since-%d' % ticket,
                 'held_at': None, 'resource': resource, 'priority': priority}, **kw)


class Listing(QueueCase):
    """land.queue (land.py --queue): the lock file's line, then the landings in the machine
    lock's queue, holding first, then waiting in ticket order; ordinary runs are left out."""

    def shown(self, entries):
        with mock.patch.object(land.lockqueue, 'load',
                               return_value={'ticket': 9, 'entries': entries}) as load:
            out = land.queue(self.repo.root, self.store)
        load.assert_called_once_with(self.store)
        return out

    def test_holding_then_waiting_in_turn_order(self):
        key = self.key()
        out = self.shown([
            entry('r5', 5, 'waiting', 'kit-sea', key),
            entry('r3', 3, 'waiting', 'kit-bee', 'landing:c:\\elsewhere\\.git'),
            entry('r4', 4, 'waiting', 'nightly', None, priority=None),       # not a landing
            entry('r1', 1, 'held', 'kit-feature', key, held_at='held-at-1'),
        ])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.lines, [
            'landing lock: free',
            'landing queue: 3 holding or waiting',
            '  1. held repo/kit-feature (engine-test, run r1, pid 10) since held-at-1',
            '  2. waiting repo/kit-bee (engine-test, run r3, pid 30) since since-3; another '
            'repository',
            '  3. waiting repo/kit-sea (engine-test, run r5, pid 50) since since-5',
        ])

    def test_no_landing_is_an_empty_queue(self):
        out = self.shown([entry('r4', 4, 'held', 'nightly', None, priority=None)])
        self.assertEqual(out.lines, ['landing lock: free', 'landing queue: empty'])
        out = land.queue(self.repo.root, self.store)                # the real, empty store
        self.assertEqual(out.lines, ['landing lock: free', 'landing queue: empty'])

    def test_the_lock_file_is_the_first_line(self):
        rec = self.lock().take(['kit/someone'], pid=os.getpid())
        self.addCleanup(self.lock().release, rec['token'])
        out = self.shown([])
        self.assertTrue(out.lines[0].startswith('landing lock: HELD - branches '
                                                'kit/someone, pid %d' % os.getpid()), out.lines)

    def test_a_queue_that_cannot_be_read_is_refused(self):
        with mock.patch.object(land.lockqueue, 'load', side_effect=OSError('denied')):
            out = land.queue(self.repo.root, self.store)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(out.lines, ['REFUSED: the landing queue of %s cannot be read: denied'
                                     % self.repo.root])


class Entry(unittest.TestCase):
    """tools/land.py: usage errors exit 2 before anything is read."""

    def run_tool(self, *args):
        return subprocess.run([sys.executable, os.path.join(KIT_ROOT, 'tools', 'land.py')]
                              + list(args), capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, KIT_PINNED='live'))

    def test_usage(self):
        tip = ['--expect-tip', 'b' * 40]
        for args in ([], ['kit/x'], ['kit/x', '--expect-main', 'a' * 40],
                     ['--queue', 'kit/x'], ['--queue', '--submit'],
                     ['kit/x'] + tip + ['--wait', '-1'],
                     ['kit/x'] + tip + ['--submit', '--submitted', 'payload'],
                     ['kit/x'] + tip + ['--reviewed-by', 'agent-r1'],
                     ['kit/x', 'kit/y'] + tip + ['--submit']):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, G.USAGE, (args, r.stdout, r.stderr))
            # argparse's on stderr; the landing's own (a batch's tips) is its outcome, on stdout
            self.assertIn('USAGE:', r.stderr + r.stdout)


if __name__ == '__main__':
    unittest.main()
