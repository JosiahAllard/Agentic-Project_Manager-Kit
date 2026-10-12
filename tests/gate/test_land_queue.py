"""kitlib.gate.land through the landing queue (kitlib.lock.resource), and batches: landings into one
repository are served first come first served, a gone holder with nothing unpushed is broken, one
with something unpushed never is, and a batch lands or is undone whole. On a scratch repository
with a bare remote and the scratch lock store (tests/gate/fixtures.py)."""
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
from kitlib.gate import land, result, runner
from kitlib.lock import resource as lockres
from tests import testutil
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, HANG_S, LandCase

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def no_sleep(slept):
    """land's clock with a sleep that only records: a landing that never waits leaves `slept`
    empty, however long its looks took on a loaded machine."""
    return mock.patch.object(land, 'time', mock.Mock(monotonic=time.monotonic,
                                                     sleep=slept.append))


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
        return lockres.entries(self.store, self.key())

    def waiter(self, results, name, branch, tip, poll_s):
        """A landing on a thread that waits for its turn; returns (thread, Event set once it said
        it is waiting)."""
        waiting = threading.Event()

        def say(line):
            if line.startswith('waiting up to'):
                waiting.set()
        t = threading.Thread(target=lambda: results.__setitem__(name, self.lander(
            branch, self.main, tip, wait_s=HANG_S, poll_s=poll_s, say=say).run()))
        t.start()
        self.addCleanup(t.join, HANG_S)
        return t, waiting


class FirstComeFirstServed(QueueCase):
    def test_waiters_take_their_turns_in_the_order_they_came_and_land(self):
        _, tip_a = self.branch('feature', {'a.py': 'a\n'})
        _, tip_b = self.branch('bee', {'b.py': 'b\n'})
        _, tip_c = self.branch('sea', {'c.py': 'c\n'})
        inside, go = threading.Event(), threading.Event()
        order = []
        real_checks = land.Lander.locked_checks

        def checks(lander):
            order.append(lander.branch)             # under the lock: the order turns were had
            return real_checks(lander)

        def held_gate(lander):
            inside.set()
            self.assertTrue(go.wait(HANG_S))
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      log_dir=self.logs, book_kw=self.book_kw)
        results = {}
        with mock.patch.object(land.Lander, 'locked_checks', checks):
            a = threading.Thread(target=lambda: results.__setitem__(
                'a', self.lander(BRANCH, self.main, tip_a, merged_gate=held_gate).run()))
            a.start()
            self.addCleanup(a.join, HANG_S)
            self.addCleanup(go.set)
            self.assertTrue(inside.wait(HANG_S))
            # B comes first and looks rarely; C comes second and looks all the time: without the
            # queue C would almost always see the free lock first
            b, b_waiting = self.waiter(results, 'b', 'kit/bee', tip_b, poll_s=0.5)
            self.assertTrue(b_waiting.wait(HANG_S))
            c, c_waiting = self.waiter(results, 'c', 'kit/sea', tip_c, poll_s=0.01)
            self.assertTrue(c_waiting.wait(HANG_S))
            self.assertEqual([(e['label'].split(': ')[-1], e['state']) for e in self.entries()],
                             [(BRANCH, 'held'), ('kit/bee', 'waiting'), ('kit/sea', 'waiting')])
            shown = land.queue(self.repo.root, self.store)
            self.assertEqual(shown.code, G.OK, shown.lines)
            self.assertTrue(shown.lines[0].startswith('landing lock: HELD'), shown.lines)
            self.assertEqual(shown.lines[1], 'landing queue: 3 waiting or holding')
            for line, want in zip(shown.lines[2:], ['  1. held ', '  2. waiting ', '  3. waiting ']):
                self.assertTrue(line.startswith(want), shown.lines)
            for line, name in zip(shown.lines[2:], [BRANCH, 'kit/bee', 'kit/sea']):
                self.assertIn('full: %s (pid %d' % (name, os.getpid()), line)
            self.assertEqual(len(shown.lines), 5, shown.lines)
            go.set()
            for t in (a, b, c):
                t.join(HANG_S)
                self.assertFalse(t.is_alive())
        self.assertEqual(order, [BRANCH, 'kit/bee', 'kit/sea'])
        # each waiter finds main moved forward by the landing ahead of it, follows it, and lands
        # on top of it: A, then B on A's merge, then C on B's
        base = self.main
        for name, tip in zip('abc', (tip_a, tip_b, tip_c)):
            out = results[name]
            self.assertEqual(out.code, G.OK, out.lines)
            self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), base)
            self.assertEqual(self.repo.git('rev-parse', out.merge + '^2'), tip)
            base = out.merge
        self.assertEqual(self.remote_main(), results['c'].merge)
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.lock().read())

    def test_a_waiter_that_times_out_leaves_the_queue(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        rec = self.lock().take('full', ['kit/someone'], pid=os.getpid())   # a live holder
        self.addCleanup(self.lock().release, rec['token'])
        said = []
        t0 = time.monotonic()
        out = self.lander(BRANCH, self.main, tip, wait_s=1, poll_s=0.05, say=said.append).run()
        self.assertGreaterEqual(time.monotonic() - t0, 1)
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertIn('pid %d' % os.getpid(), out.lines[0])
        self.assertEqual(len([s for s in said if s.startswith('waiting up to 1 s')]), 1, said)
        self.assertEqual(self.entries(), [])
        self.assertIsNone(out.result_record)

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

    def test_the_queue_is_one_per_repository(self):
        other = fixtures.gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(other.cleanup)
        wt = os.path.join(self.repo.root, '.claude', 'worktrees')
        self.branch('feature', {'a.py': 'a\n'})
        self.assertEqual(land.queue_key(self.lock().common),
                         land.queue_key(land.landing.LandingLock(os.path.join(wt, 'feature')).common))
        self.assertNotEqual(self.key(), land.queue_key(land.landing.LandingLock(other.root).common))


class GoneHolder(QueueCase):
    @unittest.skipUnless(os.name == 'nt', 'pid reuse is checked on Windows')
    def test_a_dead_pid_stays_unused_until_the_test_ends(self):
        # The helper once kept only the pid, the Popen's handle closed with it,
        # and Windows could give the pid to a live process mid-test
        pid = self.dead_pid()
        gc.collect()
        self.assertTrue(testutil.exited_object(pid))

    def test_a_gone_holder_with_nothing_unpushed_is_broken_and_the_landing_lands(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.lock().take('full', ['kit/gone'], pid=self.dead_pid())
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
        rec = self.lock().take('full', ['kit/gone'], pid=self.dead_pid())
        said, slept = [], []
        with no_sleep(slept):
            out = self.lander(BRANCH, self.main, tip, wait_s=60, poll_s=0.05,
                              say=said.append).run()
        self.assertEqual((slept, said), ([], []))           # BUSY at once: waiting cannot help
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertIn('not broken', out.lines[0])
        self.assertIn('break_lock', out.lines[0])
        self.assertEqual(self.lock().read()['token'], rec['token'])
        self.assertEqual(self.main_now(), local)
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.entries(), [])

    def test_a_queue_turn_whose_process_died_is_skipped(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        child = ('import sys; sys.path.insert(0, %r)\n'
                 'from kitlib.lock.store import Settings, Store\n'
                 'from kitlib.lock import resource as rs\n'
                 'import os\n'
                 't = rs.join(Store(root=%r, settings=Settings(queue_read_s=0.3)), %r, "gone")\n'
                 't.try_take()\n'
                 'os._exit(0)\n' % (KIT_ROOT, self.root, self.key()))
        r = subprocess.run([sys.executable, '-c', child], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        # the gone caller's entry is still in the file, holding the turn
        self.assertEqual([(e['label'], e['state']) for e in lockres.load(self.store)['entries']],
                         [('gone', 'held')])
        out = self.lander(BRANCH, self.main, tip).run()     # wait_s 0: BUSY if it were not skipped
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(lockres.load(self.store)['entries'], [])


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
            gates.append(kw.get('slug'))
            return real(*a, **kw)
        with mock.patch.object(runner, 'merged_gate', counted):
            out = self.lander(branches, self.main, tips, summary='batch').run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(gates, ['kit/one+kit/two'])
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

    def test_a_conflict_on_a_later_branch_undoes_the_earlier_merges(self):
        _, t1 = self.branch('one', {'x.txt': 'one\n'})
        _, t2 = self.branch('two', {'x.txt': 'two\n'})
        out = self.lander(['kit/one', 'kit/two'], self.main, [t1, t2]).run()
        self.assertIn('kit/two does not merge cleanly', out.lines[0])
        self.assertIn('the merge of kit/one before it was undone', out.lines[0])
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.FAILED)

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

    def test_batch_usage_and_refusals(self):
        branches, tips = self.three()
        self.assertEqual(self.lander(branches, self.main, tips[:1]).run().code, G.USAGE)
        self.assertEqual(self.lander(['kit/one', 'kit/one'], self.main, tips).run().code, G.USAGE)
        # a tip inside another tip of the batch
        wt = self.repo.path('.claude', 'worktrees', 'one')
        self.repo.git('branch', 'kit/three', 'kit/one')
        self.repo.write('three.py', '3\n', root=wt)
        self.repo.git('switch', '-q', 'kit/three', cwd=wt)
        t3 = self.repo.commit('three', 'three.py', cwd=wt)
        out = self.lander(['kit/one', 'kit/three'], self.main, [tips[0], t3]).run()
        self.assertIn('in the same batch', out.lines[0])
        self.assert_untouched(out, G.REFUSED)


class BatchInterrupted(QueueCase):
    three = Batch.three

    def interrupt_second_merge(self, after):
        """Run the batch with Ctrl+C at its second merge: before it starts, or once git made it."""
        branches, tips = self.three()
        real = land.Lander.git
        merges = []

        def git(lander, *args, **kw):
            if args[:2] == ('merge', '--no-ff'):
                merges.append(args)
                if len(merges) == 2:
                    if after:
                        r = real(lander, *args, **kw)
                        self.assertEqual(r.rc, 0, r.err)
                    raise KeyboardInterrupt
            return real(lander, *args, **kw)
        with mock.patch.object(land.Lander, 'git', git):
            lander = self.lander(branches, self.main, tips)
            with self.assertRaises(KeyboardInterrupt):
                lander.run()
        self.assertEqual(len(merges), 2)
        self.assertEqual(len(lander.merges), 2 if after else 1)    # half_merge saw the second
        self.assertEqual(self.main_now(), self.main)                # both undone
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])

    def test_ctrl_c_before_the_second_merge_undoes_the_first(self):
        self.interrupt_second_merge(after=False)

    def test_ctrl_c_once_the_second_merge_is_made_undoes_both(self):
        self.interrupt_second_merge(after=True)


class Follow(QueueCase):
    """A landing that waited follows main forward from --expect-main; never any other move, and
    never a landing that took its turn at once."""

    def main_commit(self, name, kit=None):
        self.repo.write(name, name + '\n')
        paths = [name]
        if kit is not None:                     # this commit also changes .kit.json
            with open(os.path.join(self.repo.root, '.kit.json'), encoding='utf-8') as f:
                cfg = json.load(f)
            kit(cfg)
            self.repo.write('.kit.json', json.dumps(cfg, indent=1) + '\n')
            paths.append('.kit.json')
        sha = self.repo.commit('main: ' + name, *paths)
        self.repo.git('push', '-q', 'origin', 'main')
        return sha

    def waited(self, tip, expect_main=None):
        """Land while a token holder (a tool outside the queue) has the landing lock: the first
        look fails, the landing says it waits, and only then is the lock released."""
        rec = self.lock().take('full', ['kit/ahead'])
        said = []

        def say(line):
            said.append(line)
            if line.startswith('waiting up to') and self.lock().read():
                self.lock().release(rec['token'])
        out = self.lander(BRANCH, expect_main or self.main, tip, wait_s=30, poll_s=0.05,
                          say=say).run()
        self.assertEqual(len([s for s in said if s.startswith('waiting up to 30 s')]), 1, said)
        return out, said

    def test_a_waiter_follows_main_forward_and_lands_on_it(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        moved = self.main_commit('m.py')
        out, said = self.waited(tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('main moved on from --expect-main %s to %s, which descends from it: merging '
                      'onto %s' % (self.main[:10], moved[:10], moved[:10]), said)
        self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), moved)
        self.assertEqual(self.remote_main(), out.merge)
        rec, why = land.gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual(rec['base'], moved)                        # gated against the new main
        rec, why = result.read(self.repo.root, BRANCH, kind='land')
        self.assertIsNone(why)
        self.assertEqual(rec['verdict'], 'pass')

    def test_without_a_wait_main_must_be_expect_main_exactly(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        moved = self.main_commit('m.py')
        out = self.lander(BRANCH, self.main, tip, wait_s=0).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('main is at %s, and the caller expects %s' % (moved[:10], self.main[:10]),
                      out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assertEqual(self.main_now(), moved)
        self.assertIsNone(self.lock().read())

    def test_a_waiter_refuses_main_that_moved_another_way(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        expected = self.main_commit('m.py')
        self.repo.git('reset', '-q', '--keep', self.main)           # main rewritten: m.py gone,
        rewritten = self.main_commit_forced('r.py')                 # r.py in its place
        out, _ = self.waited(tip, expected)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('which does not descend from --expect-main %s: main moved another way'
                      % expected[:10], out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assertEqual(self.main_now(), rewritten)
        self.assertEqual(self.remote_main(), rewritten)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])

    def main_commit_forced(self, name):
        self.repo.write(name, name + '\n')
        sha = self.repo.commit('main: ' + name, name)
        self.repo.git('push', '-q', '-f', 'origin', 'main')
        return sha

    def test_a_waiter_does_not_follow_when_main_is_only_behind_the_remote(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        other = self.repo.worktree('other')
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        out, _ = self.waited(tip)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('follows main forward only when the two agree', out.lines[0])
        self.assertIn('main is only behind origin/main, with nothing unpushed: fast-forward it '
                      'with: git -C "%s" merge --ff-only origin/main' % self.repo.root, out.lines[0])
        self.assertEqual(self.main_now(), self.main)
        self.assertIsNone(self.lock().read())


    def test_a_landing_that_took_its_turn_at_once_keeps_the_exact_check(self):
        # a wait budget, but the turn and the lock were free at the first look: no waiting, so
        # a main that moved forward is refused as without a wait
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        moved = self.main_commit('m.py')
        said = []
        out = self.lander(BRANCH, self.main, tip, wait_s=30, poll_s=0.05, say=said.append).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('main is at %s, and the caller expects %s' % (moved[:10], self.main[:10]),
                      out.lines[0])
        self.assertFalse([s for s in said if s.startswith(('waiting up to', 'main moved on'))],
                         said)
        self.assertEqual(self.main_now(), moved)
        self.assertEqual(self.remote_main(), moved)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])

    def test_a_waiter_refuses_a_new_config_naming_another_main_remote_or_lock(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        changes = {
            'main': lambda c: c.setdefault('repo', {}).update(main_branch='trunk'),
            'remote': lambda c: c.setdefault('repo', {}).update(remote='upstream'),
            'lock': lambda c: c.setdefault('landing', {}).update(lock_file='git:other.lock'),
        }
        base = self.main
        for n, (what, change) in enumerate(sorted(changes.items())):
            with self.subTest(what=what):
                self.repo.git('reset', '-q', '--keep', base)            # each from the base
                self.repo.git('push', '-q', '-f', 'origin', 'main')
                moved = self.main_commit('m%d.py' % n, kit=change)
                out, said = self.waited(tip, base)
                self.assertEqual(out.code, G.REFUSED, out.lines)
                self.assertIn('main moved on from --expect-main %s to %s, and the config there '
                              'names another main branch, remote or landing lock: land again with '
                              '--expect-main %s' % (base[:10], moved[:10], moved), out.lines[0])
                self.assertIn('nothing was merged', out.lines[0])
                self.assertEqual(self.main_now(), moved)
                self.assertEqual(self.remote_main(), moved)
                self.assertIsNone(self.lock().read())
                self.assertEqual(self.entries(), [])


class OddLocks(QueueCase):
    def test_an_unreadable_or_old_format_lock_is_waited_for_then_busy(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        lock = self.lock()
        self.addCleanup(lambda: os.path.exists(lock.path) and os.remove(lock.path))
        for text in ('not json at all', '{"pid": 1, "branch": "kit/old", "since": "then"}\n'):
            with self.subTest(text=text):
                with open(lock.path, 'w', encoding='utf-8') as f:
                    f.write(text)
                said = []
                t0 = time.monotonic()
                out = self.lander(BRANCH, self.main, tip, wait_s=1, poll_s=0.05,
                                  say=said.append).run()
                self.assertGreaterEqual(time.monotonic() - t0, 1)   # waited: it may be released
                self.assertEqual(out.code, G.BUSY, out.lines)
                self.assertIn('landing lock: HELD - lane ?', out.lines[0])
                self.assertEqual(len([s for s in said if s.startswith('waiting up to 1 s')]), 1,
                                 said)
                with open(lock.path, encoding='utf-8') as f:
                    self.assertEqual(f.read(), text)                # never broken or rewritten
                self.assertEqual(self.entries(), [])
                self.assertEqual(self.main_now(), self.main)


class AfterThePush(QueueCase):
    """Stopped once the push is verified: LANDED with a WARNING, never STUCK."""

    def interrupt(self, method):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        real = getattr(land.Lander, method)
        hit = []

        def stop(lander, *a, **kw):
            if lander.phase == 'pushed' and not hit:
                hit.append(method)
                raise KeyboardInterrupt
            return real(lander, *a, **kw)
        with mock.patch.object(land.Lander, method, stop):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(hit, [method])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse([l for l in out.lines if l.startswith('STUCK')], out.lines)
        self.assertIn('LANDED kit/feature on main at %s (pushed and verified on origin)' % out.merge,
                      out.lines)
        self.assertIn('WARNING: Ctrl+C after the verified push; the landing stands', out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])
        rec, why = result.read(self.repo.root, BRANCH, kind='land')
        self.assertIsNone(why)
        self.assertEqual(rec['verdict'], 'pass')

    def test_ctrl_c_in_the_fetch_after_the_verified_push(self):
        self.interrupt('fetch')

    def test_ctrl_c_in_the_release_after_the_verified_push(self):
        self.interrupt('release')

    def test_ctrl_c_again_in_every_release_after_the_verified_push(self):
        # the release keeps failing: the second Ctrl+C is inside landed_anyway itself
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        real = land.Lander.release
        hits = []

        def stop(lander):
            if lander.phase in ('pushed', 'landed'):
                hits.append(lander.phase)
                raise KeyboardInterrupt
            return real(lander)
        lander = self.lander(BRANCH, self.main, tip)
        with mock.patch.object(land.Lander, 'release', stop):
            out = lander.run()
        held = self.lock().read()
        if held:
            self.addCleanup(self.lock().release, held['token'])
        self.addCleanup(lander.leave_queue)
        self.assertGreaterEqual(len(hits), 2, hits)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse([l for l in out.lines if l.startswith('STUCK')], out.lines)
        self.assertIn('LANDED kit/feature on main at %s (pushed and verified on origin)' % out.merge,
                      out.lines)
        self.assertIn('WARNING: Ctrl+C after the verified push; the landing stands', out.lines)
        warn = [l for l in out.lines if l.startswith('WARNING: the landing lock was not released')]
        self.assertEqual(warn, ['WARNING: the landing lock was not released (Ctrl+C); check that '
                                'no landing is running, then break it with: %s'
                                % lander.break_command()], out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(held['token'], lander.token)               # still held, as it says

    def test_ctrl_c_in_the_store_lookup_after_the_verified_push(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        hit = []

        def stop(lander, *a, **kw):
            if lander.phase in ('pushed', 'landed') and not hit:
                hit.append(lander.phase)
                raise KeyboardInterrupt
            return real(lander, *a, **kw)
        real = land.Lander.fetch
        real_store, real_how = land.at_landing.ref_store, land.at_landing.backlog_command

        def again(real_fn):             # once the first Ctrl+C was had: Ctrl+C again
            def fn(*a, **kw):
                if hit:
                    raise KeyboardInterrupt
                return real_fn(*a, **kw)
            return fn
        with mock.patch.object(land.Lander, 'fetch', stop), \
                mock.patch.object(land.at_landing, 'ref_store', again(real_store)), \
                mock.patch.object(land.at_landing, 'backlog_command', again(real_how)):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(hit, ['pushed'])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse([l for l in out.lines if l.startswith('STUCK')], out.lines)
        self.assertIn('WARNING: the records kit/feature holds pulled in the backlog store were not '
                      'closed; see what is left with: the backlog tool', out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.entries(), [])


class BehindOnly(QueueCase):
    def test_a_gone_holder_with_main_only_behind_gets_the_fast_forward_command(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        other = self.repo.worktree('other')
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        rec = self.lock().take('full', ['kit/gone'], pid=self.dead_pid())
        said, slept = [], []
        with no_sleep(slept):
            out = self.lander(BRANCH, self.main, tip, wait_s=60, poll_s=0.05,
                              say=said.append).run()
        self.assertEqual((slept, said), ([], []))                   # BUSY at once
        self.assertEqual(out.code, G.BUSY, out.lines)
        ff = 'git -C "%s" merge --ff-only origin/main' % self.repo.root
        self.assertIn('main is only behind origin/main, with nothing unpushed: fast-forward it '
                      'with: %s' % ff, out.lines[0])
        self.assertIn('The next landing breaks it itself once main has caught up', out.lines[0])
        self.assertNotIn('unpushed merge or commit', out.lines[0])
        self.assertEqual(self.lock().read()['token'], rec['token'])
        # once fast-forwarded, the next landing breaks the lock at its first look and lands;
        # it did not wait, so it is given the new main (the coordinator re-confirms it)
        self.repo.git('merge', '-q', '--ff-only', 'origin/main')
        said = []
        out = self.lander(BRANCH, moved, tip, wait_s=1, poll_s=0.05, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(any(s.startswith('broke the landing lock of a holder that is gone')
                            for s in said), said)
        self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), moved)

    def test_behind_only_with_a_merge_in_progress_gets_the_settle_by_hand_message(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        other = self.repo.worktree('other')
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        rec = self.lock().take('full', ['kit/gone'], pid=self.dead_pid())
        merge_head = os.path.join(self.lock().common, 'MERGE_HEAD')
        with open(merge_head, 'w', encoding='utf-8') as f:       # a merge the gone holder left
            f.write(moved + '\n')
        self.addCleanup(lambda: os.path.exists(merge_head) and os.remove(merge_head))
        out = self.lander(BRANCH, self.main, tip, wait_s=60, poll_s=0.05).run()
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertIn('main (%s) != origin/main (%s): an unpushed merge or commit. Settle that by '
                      'hand, then break it with: ' % (self.main[:10], moved[:10]), out.lines[0])
        self.assertNotIn('fast-forward', out.lines[0])
        self.assertEqual(self.lock().read()['token'], rec['token'])


class BatchMax(QueueCase):
    KIT = dict(LandCase.KIT, landing=dict(LandCase.KIT['landing'], batch_max=1))

    def test_a_batch_over_batch_max_is_refused(self):
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out = self.lander(['kit/one', 'kit/two'], self.main, [t1, t2]).run()
        self.assertIn('more than "landing".batch_max (1)', out.lines[0])
        self.assert_untouched(out, G.REFUSED)


# A step that passes only when the lane's merged_args follow the step's own.
ARGS_STEP = ['{python}', '-c', 'import sys; good = sys.argv[1:] == ["--merged", "--docs"]; '
                               'print("ok" if good else "args %r" % sys.argv[1:]); '
                               'sys.exit(0 if good else 1)']


class LaneArgs(QueueCase):
    KIT = {'landing': {'lanes': {'docs': {'branch_prefix': ['docs/'], 'paths': ['docs/*'],
                                          'merged_args': ['--docs']}}}}
    GATE = {'steps': [{'name': 'unit', 'always': True, 'cmd': ARGS_STEP,
                       'merged_args': ['--merged'], 'pass': '^ok$'}]}

    def docs_branch(self):
        wt = self.repo.worktree('d', branch='docs/d')
        self.repo.write('docs/x.md', 'x\n', root=wt)
        return self.repo.commit('doc', 'docs/x.md', cwd=wt)

    def test_the_lanes_merged_args_follow_the_steps(self):
        tip = self.docs_branch()
        out = self.lander('docs/d', self.main, tip, lane='docs').run()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_a_full_landing_gets_only_the_steps(self):
        tip = self.docs_branch()
        out = self.lander('docs/d', self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.undone)


class Entry(unittest.TestCase):
    """tools/land.py: usage errors exit 2 before anything is read."""

    def run_tool(self, *args):
        return subprocess.run([sys.executable, os.path.join(KIT_ROOT, 'tools', 'land.py')]
                              + list(args), capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, KIT_PINNED='live'))

    def test_usage(self):
        for args in ([], ['kit/x'], ['kit/x', '--expect-main', 'a' * 40],
                     ['--queue', 'kit/x'], ['kit/x', '--expect-main', 'a' * 40,
                                            '--expect-tip', 'b' * 40, '--wait', '-1']):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, G.USAGE, (args, r.stdout, r.stderr))
            self.assertIn('USAGE:', r.stderr)


if __name__ == '__main__':
    unittest.main()
