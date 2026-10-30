"""kitlib.gate.land: one landing into a scratch repository with a bare remote in temp, gated on the
scratch lock store. Each test proves one contract: refusals before any merge, a red merge undone,
the push verified, landings into one repository taking turns."""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import gate as G
from kitlib.gate import land, result, runner
from kitlib.git import refs
from kitlib.lock import queue as lockqueue
from kitlib.records import gate_ok, landing
from tests.gate import fixtures
from tests.realgit import real_git

BRANCH = 'kit/feature'
# Seconds a test waits on a thread, an event or a waiting landing's budget when the wait is not
# what it tests: the test goes on as soon as the event is had, so only a hang reaches it. A loaded
# machine (a full suite beside others) stretches a landing to tens of seconds.
HANG_S = 600
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class LandCase(fixtures.GateRepoCase):
    KIT = {}
    # lander() gives each tip the full-gate OK record a landing requires (fixtures.gate_full);
    # a test of that check itself sets it False and gates its tips (test_land_tip_gated)
    GATE_TIPS = True
    REVIEW_TIPS = True      # fixtures.queue_review: each branch without an entry gets a verdict

    def lander(self, branch, expect_main, expect_tip, **kw):
        kw.setdefault('git_env', self.env)
        kw.setdefault('book_kw', self.book_kw)
        kw.setdefault('log_dir', self.logs)
        if self.GATE_TIPS:
            self.gate_tips(expect_tip)
        if self.REVIEW_TIPS:
            fixtures.queue_review(self.repo.root, branch, expect_tip)
        return land.Lander(self.repo.root, branch, expect_main, expect_tip, **kw)

    def gate_tips(self, *tips):
        """fixtures.gate_full on each tip (a tip may be a list, as a batch's --expect-tip is);
        the record files it made are left out of records()."""
        flat = [t for tip in tips for t in (tip if isinstance(tip, (list, tuple)) else [tip])]
        made = fixtures.gate_full(self.repo.root, *flat)
        self.__dict__.setdefault('fixture_records', set()).update(made)

    def lock(self):
        return landing.LandingLock(self.repo.root)

    def main_now(self):
        return self.repo.git('rev-parse', 'refs/heads/main')

    def records(self):
        """The gate-OK record files, less those gate_tips made for the tips."""
        d = gate_ok.record_dir(self.repo.root)
        made = self.__dict__.get('fixture_records', set())
        return sorted(n for n in os.listdir(d) if n not in made) if os.path.isdir(d) else []

    def assert_untouched(self, out, code):
        self.assertEqual(out.code, code, out.lines)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.records(), [])


class Lands(LandCase):
    def test_green_lands_pushes_and_verifies(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip, summary='add a').run()
        self.assertEqual(out.code, G.OK, out.lines)
        merge = out.merge
        self.assertTrue(out.lines[-1].startswith('LANDED kit/feature on main at %s' % merge))
        self.assertEqual(self.remote_main(), merge)                # the push really happened
        self.assertEqual(self.main_now(), merge)
        self.assertEqual(self.repo.git('rev-parse', 'refs/remotes/origin/main'), merge)
        self.assertEqual(self.repo.git('rev-parse', merge + '^1'), self.main)
        self.assertEqual(self.repo.git('rev-parse', merge + '^2'), tip)
        self.assertEqual(self.repo.git('log', '-1', '--format=%s', merge), 'Merge kit/feature: add a')
        self.assertIsNone(self.lock().read())
        rec, why = gate_ok.verify(self.repo.root, merge)
        self.assertIsNone(why)
        self.assertEqual(rec['base'], self.main)
        self.assertEqual([s['name'] for s in rec['steps']], ['unit'])

    def test_red_merge_is_undone(self):
        _, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.undone)
        self.assertTrue(out.verdict.startswith('GATE FAILED: unit'), out.verdict)
        self.assertIn('NOT LANDED: the merge of kit/feature was undone', out.lines[-1])
        self.assert_untouched(out, G.FAILED)
        self.assertFalse(os.path.exists(self.repo.path('RED')))

    def test_wrong_expect_tip_is_refused_before_any_merge(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, self.repo.initial).run()
        self.assertIn('kit/feature is at %s' % tip[:10], out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_main_moved_on_the_remote_is_followed(self):
        # the landing merges onto main as it is at its turn (tests/gate/test_land_queue.py Follow)
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        other = self.repo.worktree('other')
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('main moved on from %s to %s; merging onto %s' % (
            self.main[:10], moved[:10], moved[:10]), said)
        self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), moved)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertIsNone(self.lock().read())

    def test_conflict_is_aborted(self):
        _, tip = self.branch('feature', {'README.md': 'branch\n'})
        self.repo.write('README.md', 'main\n')
        self.main = self.repo.commit('main edit', 'README.md')
        self.repo.git('push', '-q', 'origin', 'main')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('merge was aborted', out.lines[0])
        self.assertEqual(out.lines[-1], 'CONFLICT kit/feature with main %s: the author merges main'
                         % self.main)
        self.assertEqual(out.facts.get('conflict'), BRANCH)
        self.assert_untouched(out, G.FAILED)

    def test_usage_and_refusals(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.assertEqual(self.lander(BRANCH, 'abc', tip).run().code, G.USAGE)
        wt = self.repo.path('.claude', 'worktrees', 'feature')
        out = land.Lander(wt, BRANCH, self.main, tip, git_env=self.env).run()
        self.assertEqual(out.code, G.REFUSED)
        self.assertIn('not a main checkout', out.lines[0])
        self.assertEqual(self.lander('main', self.main, tip).run().code, G.REFUSED)
        self.assertEqual(self.lander('kit/nosuch', self.main, tip).run().code, G.REFUSED)
        self.assertIsNone(self.lock().read())


@real_git('git reflog')
class RefusedBeforeAnyMerge(LandCase):
    """Moved out of Lands for the real-git tier."""

    def test_wrong_expect_tip_is_refused_before_any_merge(self):
        # a wrong --expect-main is followed, never refused; the tip still is
        self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, self.repo.initial).run()
        self.assertIn('nothing was merged', out.lines[0])
        self.assert_untouched(out, G.REFUSED)
        self.assertNotIn('merge', self.repo.git('reflog', '-n', '5', 'main'))


class PushCase(LandCase):
    def assert_stuck(self, out, tip):
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertTrue(out.lines[0].startswith('STUCK: landing kit/feature stopped in phase push'),
                        out.lines)
        rec = self.lock().read()
        self.assertEqual(rec['phase'], 'stuck')                 # no landing starts on top of it
        self.assertTrue(out.merge)                              # the merge was made and gated,
        self.assertEqual(self.main_now(), self.main)            # in the landing tree: the main
        # checkout was never merged into, so nothing is left there to guess at
        self.assertEqual(self.remote_main(), self.main)
        # the next landing is refused while it is stuck
        nxt = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(nxt.code, G.BUSY, nxt.lines)


@real_git('the remote rejects the push in a pre-receive hook')
class PushRejected(PushCase):
    """Moved out of PushVerified for the real-git tier."""

    def test_a_push_the_remote_rejects_is_not_landed(self):
        # git exits 1 and the remote's main is still the base: nothing was published, so the
        # landing is NOT LANDED and the lock released, as on a red merged gate
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        hook = os.path.join(self.repo.remote, 'hooks', 'pre-receive')
        with open(hook, 'w', newline='\n') as f:
            f.write('#!/bin/sh\necho rejected by test >&2\nexit 1\n')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[-1].startswith('NOT LANDED: '), out.lines)
        self.assertIn('still the base %s' % self.main[:10], out.lines[-1])
        self.assertFalse([l for l in out.lines if l.startswith('STUCK')], out.lines)
        self.assertTrue(out.merge)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        os.remove(hook)                                 # the next landing is not BUSY: it lands
        nxt = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(nxt.code, G.OK, nxt.lines)


class PushVerified(PushCase):
    def test_a_push_ls_remote_does_not_confirm_is_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(refs, 'ls_remote', side_effect=main_only(('0' * 40, None))) as ls:
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(ls.called)
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertIn('exited 0', out.lines[0])
        self.assertEqual(self.lock().read()['phase'], 'stuck')



def main_only(fake):
    """A `refs.ls_remote` side effect that answers `fake` for refs/heads/main and asks the real
    remote for every other ref (the backlog store's ancestry check)."""
    real = refs.ls_remote

    def ls_remote(remote, ref, cwd, **kw):
        full = ref if ref.startswith('refs/') else 'refs/heads/%s' % ref
        return fake if full == 'refs/heads/main' else real(remote, ref, cwd, **kw)
    return ls_remote


STRICT = (
    'import os\n'
    'def strict(ctx):\n'
    '    ctx["log"].write("main check\\n")\n'
    '    return not os.path.exists(os.path.join(ctx["root"], "RED"))\n'
    'GATE_CHECKS = {"strict": strict}\n')
LENIENT = (
    'def strict(ctx):\n'
    '    ctx["log"].write("branch check\\n")\n'
    '    return True\n'
    'GATE_CHECKS = {"strict": strict}\n')


class HookFromMain(LandCase):
    """The merged gate runs the hook module as --expect-main has it, like its config."""
    KIT = dict(LandCase.KIT, hook={'module': 'kit_hooks'})
    GATE = {'steps': [{'name': 'strict', 'always': True, 'check': 'strict'}]}

    def setUp(self):
        LandCase.setUp(self)
        self.repo.write('tools/kit_hooks.py', STRICT)
        self.main = self.repo.commit('strict check', 'tools/kit_hooks.py')
        self.repo.git('push', '-q', 'origin', 'main')

    def test_a_branch_that_loosens_the_check_is_still_gated_by_mains(self):
        wt, tip = self.branch('feature', {'tools/kit_hooks.py': LENIENT, 'RED': 'x\n'})
        own = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=os.path.join(self.tmp, 'own'))
        self.assertEqual(own.code, G.OK, own.lines)       # its own check passes it
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.verdict.startswith('GATE FAILED: strict'), out.lines)
        self.assertTrue(out.undone)
        self.assertEqual(self.records(), [tip + '.json'])  # the branch gate's; none for a merge
        os.remove(gate_ok.record_path(self.repo.root, tip))
        self.assert_untouched(out, G.FAILED)

    def test_mains_check_runs_even_when_the_branch_breaks_the_module(self):
        _, tip = self.branch('feature', {'tools/kit_hooks.py': 'raise SystemExit("broken")\n'})
        logs = os.path.join(self.tmp, 'merged')
        out = self.lander(BRANCH, self.main, tip, log_dir=logs).run()
        self.assertEqual(out.code, G.OK, out.lines)
        found = [os.path.join(d, f) for d, _, fs in os.walk(logs) for f in fs if f == 'strict.log']
        self.assertEqual(len(found), 1, found)
        with open(found[0], encoding='utf-8') as f:
            self.assertEqual(f.read(), 'main check\n')

    def test_a_broken_module_on_main_is_refused_and_undone(self):
        self.repo.write('tools/kit_hooks.py', 'raise SystemExit("broken")\n')
        self.main = self.repo.commit('break it', 'tools/kit_hooks.py')
        self.repo.git('push', '-q', 'origin', 'main')
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('failed to load: SystemExit: broken', out.lines[0])
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.REFUSED)


class NoStepSelected(LandCase):
    # The merged gate selects every step that is not manual (tests/gate/test_land_select.py). Two
    # steps that each drop the other (skip_if_selected) leave it nothing, as all-manual steps do:
    # the base is refused before the merge (Lander.no_full_step), and the merged gate itself, met
    # anyway, refuses with the advice for --full (runner.NO_STEP_FULL), not the one for paths.
    GATE = {'steps': [{'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE,
                       'pass': '^ok$', 'skip_if_selected': ['other']},
                      {'name': 'other', 'always': True, 'cmd': fixtures.RED_AWARE,
                       'pass': '^ok$', 'skip_if_selected': ['unit']}]}

    def test_a_landing_with_no_step_selected_is_refused_before_the_merge(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('leaves no step for --full to select', out.lines[0])
        self.assertIn("is dropped by another selected step's skip_if_selected", out.lines[0])
        self.assertIn('nothing was merged', out.lines[0])
        self.assertFalse(out.facts.get('undone'))         # no merge was made to undo
        self.assert_untouched(out, G.REFUSED)             # no record, nothing pushed
        # the landing's one turn, booked and given back; no gate ran, so none booked another
        self.assertEqual([c.get('priority') for c in self.book_calls], ['land'], self.book_calls)
        self.assertEqual([r['slug'] for r in self.store.history_records(1)], ['kit-feature'])

    def test_the_merged_gate_refuses_with_the_full_advice(self):
        out = runner.merged_gate(self.repo.root, kconfig.load(self.repo.root), self.main,
                                 log_dir=self.logs, book_kw=self.book_kw)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(out.lines[0], runner.NO_STEP_FULL)   # then the gate's source label
        self.assertEqual(self.book_calls, [])             # refused before it booked


class QueueTime(LandCase):
    """A direct landing's result record says when it joined the landing queue, as a submitted
    landing's does (the submission): started_at is taken before book_turn waits for its turn."""

    def test_started_at_is_before_the_turn_was_booked(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        real_utc, real_book = gate_ok.utc, land.lockrun.book
        late = [datetime.timedelta(0)]
        stamps = {}

        def utc(when=None):         # the real clock, an hour on once the landing books
            return real_utc(when or datetime.datetime.now(datetime.timezone.utc) + late[0])

        def book(*a, **kw):
            stamps['booking'] = utc()
            late[0] = datetime.timedelta(hours=1)
            stamps['booked'] = utc()
            return real_book(*a, **kw)
        lander = self.lander(BRANCH, self.main, tip, book=book)
        with mock.patch.object(gate_ok, 'utc', utc):
            out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNotNone(rec, why)
        self.assertLessEqual(rec['started_at'], stamps['booking'], (rec, stamps))
        self.assertGreaterEqual(rec['finished_at'], stamps['booked'], (rec, stamps))


class GateOnceLedger(LandCase):
    """A base that sets "landing".gate_once: the land record and its land ledger line say so,
    and say whether once_escalate made the merged gate --full (docs/gate.md "Result record")."""
    KIT = dict(LandCase.KIT, landing={'gate_once': True})

    def ledger(self):
        with open(result.ledger_path(self.repo.root), encoding='utf-8') as f:
            return [json.loads(l) for l in f.read().splitlines()]

    def once_of(self, rec):
        return rec['gate_once'], rec['escalated']

    def test_an_escalated_landing_records_escalated_true(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(any(l.startswith('GATE ONCE: ') for l in said), said)
        rec = result.read(self.repo.root, BRANCH, 'land')[0]
        self.assertEqual(self.once_of(rec), (True, True))
        line, = self.ledger()
        self.assertEqual((line['verdict'], line['tip'], line['run_id']),
                         ('pass', tip, rec['run_id']))
        self.assertEqual(self.once_of(line), (True, True))

    def test_a_red_escalated_landing_records_escalated_true(self):
        _, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        line, = self.ledger()
        self.assertEqual((line['verdict'], line['code']), ('fail', G.FAILED))
        self.assertEqual(self.once_of(line), (True, True))

    def test_a_full_landing_is_not_escalated(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []
        out = self.lander(BRANCH, self.main, tip, full=True, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(any(l.startswith('GATE ONCE: ') for l in said), said)
        line, = self.ledger()
        self.assertEqual(self.once_of(line), (True, False))
        self.assertEqual(self.once_of(result.read(self.repo.root, BRANCH, 'land')[0]),
                         (True, False))


class TakeTurns(LandCase):
    def test_two_landings_into_one_repo_serialise(self):
        _, tip_a = self.branch('feature', {'a.py': 'a\n'})
        _, tip_b = self.branch('other', {'b.py': 'b\n'})
        inside, go = threading.Event(), threading.Event()

        def held_gate(lander):
            inside.set()
            self.assertTrue(go.wait(HANG_S))
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      log_dir=self.logs, book_kw=self.book_kw, slot=lander.slot)
        results = {}
        a = threading.Thread(target=lambda: results.__setitem__(
            'a', self.lander(BRANCH, self.main, tip_a, merged_gate=held_gate).run()))
        a.start()
        self.addCleanup(a.join, HANG_S)
        self.addCleanup(go.set)
        self.assertTrue(inside.wait(HANG_S))

        # B at once: not given its turn, nothing changed - by the machine lock's landing queue
        # where it books one (fixtures.HAS_RESOURCE), else by the landing lock file
        busy = self.lander('kit/other', self.main, tip_b).run()
        if fixtures.HAS_RESOURCE:
            self.assertNotIn(busy.code, (G.OK, G.BUSY), busy.lines)
            self.assertTrue(busy.lines[0].startswith('NOT RUN: the machine lock did not give the '
                                                     'landing its turn'), busy.lines)
        else:
            self.assertEqual(busy.code, G.BUSY, busy.lines)
        self.assertIn('nothing was changed', busy.lines[0])

        # B waiting: it takes its turn only after A has landed and released, and lands
        said = []
        b = threading.Thread(target=lambda: results.__setitem__('b', self.lander(
            'kit/other', self.main, tip_b, wait_s=HANG_S, poll_s=0.05, say=said.append).run()))
        b.start()
        self.addCleanup(b.join, HANG_S)
        end = time.monotonic() + HANG_S

        def waiting():      # in the machine lock's queue, or at the landing lock file
            return ([s for s in said if s.startswith('waiting up to')]
                    or [e for e in lockqueue.load(self.store)['entries']
                        if e.get('slug') == 'kit-other' and e.get('state') == 'waiting'])
        while not waiting():
            self.assertLess(time.monotonic(), end, said)
            time.sleep(0.02)
        self.assertNotIn('b', results)
        go.set()
        for t in (a, b):
            t.join(HANG_S)
            self.assertFalse(t.is_alive())
        self.assertEqual(results['a'].code, G.OK, results['a'].lines)
        # B waited, so it follows main forward from its --expect-main to A's merge and lands on it
        self.assertEqual(results['b'].code, G.OK, results['b'].lines)
        self.assertEqual(self.repo.git('rev-parse', results['b'].merge + '^1'), results['a'].merge)
        self.assertEqual(self.remote_main(), results['b'].merge)
        self.assertIsNone(self.lock().read())

    def child(self, branch, tip, **extra):
        self.gate_tips(tip)
        spec = dict(repo=self.repo.root, branch=branch, expect_main=self.main, expect_tip=tip,
                    logs=self.logs, store={'root': self.root, 'kinds_path': self.kinds_path,
                                           'hw': self.HW},
                    kit=os.path.dirname(result.machine_root()), **extra)
        return [sys.executable, '-m', 'tests.gate.land_child', json.dumps(spec)]

    def test_two_landing_processes_into_one_repo_take_turns(self):
        _, tip_a = self.branch('feature', {'a.py': 'a\n'})
        _, tip_b = self.branch('other', {'b.py': 'b\n'})
        # A holds the landing lock in its merged gate: it says INSIDE, then waits for a line on
        # its stdin (pipes, not files: the signal must not depend on the two sharing a view)
        a = subprocess.Popen(self.child(BRANCH, tip_a, hold=True), cwd=KIT_ROOT, env=self.env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: a.poll() is None and (a.kill(), a.wait()))
        self.addCleanup(a.stdout.close)
        self.addCleanup(a.stdin.close)
        first = a.stdout.readline()
        if first != 'INSIDE\n':         # read the rest only now: A exits after a failure
            self.fail(first + a.stdout.read())
        self.assertEqual(self.lock().read()['pid'], a.pid)     # A, another process, holds it

        b = subprocess.run(self.child('kit/other', tip_b, wait=0), cwd=KIT_ROOT, env=self.env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=HANG_S, text=True)
        if fixtures.HAS_RESOURCE:       # the machine lock's landing queue keeps B out
            self.assertNotIn(b.returncode, (G.OK, G.BUSY), b.stdout)
            self.assertIn('NOT RUN: the machine lock did not give the landing its turn', b.stdout)
        else:                           # the landing lock file does
            self.assertEqual(b.returncode, G.BUSY, b.stdout)
            self.assertIn('BUSY: landing lock', b.stdout)
            self.assertIn('pid %d' % a.pid, b.stdout)
        self.assertIn('nothing was changed', b.stdout)

        out_a = a.communicate('go\n', timeout=HANG_S)[0]
        self.assertEqual(a.returncode, G.OK, out_a)
        self.assertIn('LANDED kit/feature', out_a)
        self.assertEqual(self.repo.git('rev-parse', self.remote_main() + '^2'), tip_a)
        self.assertIsNone(self.lock().read())

    def test_a_branch_gate_does_not_wait_for_the_landing_lock(self):
        wt, tip = self.branch('feature', {'a.py': 'a\n'})
        rec = self.lock().take(['kit/someone'], pid=os.getpid())
        self.addCleanup(self.lock().release, rec['token'])
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))


if __name__ == '__main__':
    unittest.main()
