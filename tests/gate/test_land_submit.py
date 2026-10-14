"""kitlib.gate.submit (land.py --submit, gate.py land --submit): the one SUBMITTED line with its
wait-result command, the running land records written before the detached start, and the detached
landing (Lander(submitted=...)) finishing those same records whatever it ends with, refusals and
the machine lock's included. pin.lease_child: the lease a pinned tool's detached child holds. On a
scratch repository with a bare remote and the scratch lock store (tests/gate/fixtures.py); the
detached start is a fake, except in RealCarrier, which starts a sleeping python through the
carrier and finds no console window, and RealDetach, which starts tools/land.py and tools/gate.py
detached on a landing they refuse at once."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import pin, result, submit
from kitlib.git import repo
from kitlib.lock import detach as lockdetach
from kitlib.lock.store import LockError
from kitlib.proc import process
from tests import gitfixture, testutil
from tests.gate.test_land import BRANCH, HANG_S, KIT_ROOT, LandCase
from tests.lock.test_detach_cli import console_windows_of

LAND_PY = os.path.join(KIT_ROOT, 'tools', 'land.py')
GATE_PY = os.path.join(KIT_ROOT, 'tools', 'gate.py')
CARRIER = os.path.join(KIT_ROOT, 'tools', 'no_window.py')
PYTHONW = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
CONSOLE_PY = os.path.join(os.path.dirname(sys.executable), 'python.exe')


class Detach(object):
    """A fake submit.detach: records each start; (pid, None), or (None, why) when pid is None."""

    def __init__(self, pid=None, why=None):
        self.pid, self.why, self.calls = pid, why, []

    def __call__(self, argv, cwd, env):
        self.calls.append((list(argv), cwd, dict(env)))
        return (self.pid, None) if self.pid is not None else (None, self.why)


class SubmitCase(LandCase):
    def submit(self, branches, tips, detach=None, environ=None, kit=r'C:\kit', **kw):
        detach = detach or Detach(pid=os.getpid())
        kw.setdefault('claim_wait_s', 0)        # the fake child never records itself
        args = ['-C', self.repo.root] + sum((['--expect-tip', t] for t in tips), []) + branches
        out = submit.submit(self.repo.root, LAND_PY, args, branches, tips,
                            environ=dict(environ or {}), detach_fn=detach, kit=kit, **kw)
        return out, detach

    def main_top(self):
        """The wait command's -C: the main checkout as git names it, quoted when it has a space."""
        top = repo.main_checkout_toplevel(self.repo.root)
        self.assertIsNotNone(top)
        return '"%s"' % top if ' ' in top else top

    def record(self, branch):
        rec, why = result.read(self.repo.root, branch, 'land')
        self.assertIsNotNone(rec, why)
        return rec


class TheLine(SubmitCase):
    def test_one_branch_prints_the_line_writes_running_and_starts_detached(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, detach = self.submit([BRANCH], [tip], expect_main=self.main,
                                  environ={'MACHINE_LOCK_RUN': 'r9', 'KEEP': 'me'})
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.lines, ['SUBMITTED kit/feature tip %s wait: python %s wait-result '
                                     'kit/feature --tip %s --kind land -C %s' % (
                                         tip, os.path.join(r'C:\kit', 'tools', 'gate.py'), tip,
                                         self.main_top())])
        payload, output = out.facts['payload'], out.facts['output']
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['tip'], rec['base'], rec['log']),
                         ('running', tip, self.main, output))
        with open(payload, encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual([(r['branch'], r['tip'], r['run_id'], r['started_at'])
                          for r in data['runs']],
                         [(BRANCH, tip, rec['run_id'], rec['started_at'])])
        self.assertEqual(data['output'], output)
        with open(output, encoding='utf-8') as f:
            self.assertTrue(f.read().startswith('=== landing kit/feature at %s submitted '
                                                % tip[:10]))
        [(argv, cwd, env)] = detach.calls
        # pythonw (no console) runs the carrier, which starts the console python with no window
        self.assertEqual(argv[:5], [PYTHONW, CARRIER, CONSOLE_PY, '-B', LAND_PY])
        self.assertEqual(argv[5:], ['-C', self.repo.root, '--expect-tip', tip, BRANCH,
                                    '--submitted', payload])
        self.assertEqual(cwd, self.repo.root)
        self.assertNotIn('MACHINE_LOCK_RUN', env)   # never a slot nested in the submitter's run
        self.assertIn('MACHINE_LOCK_ORIGIN', env)   # the submitter's ancestry: its role
        self.assertEqual((env['KEEP'], env['PYTHONIOENCODING']), ('me', 'utf-8'))
        self.assertEqual(self.book_calls, [])       # the submitter books nothing

    def test_a_batch_names_every_branch_and_waits_on_the_first(self):
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, _ = self.submit(['kit/one', 'kit/two'], [t1, t2], kit=r'C:\the kit')
        self.assertEqual(out.lines, ['SUBMITTED kit/one+kit/two tip %s,%s wait: python "%s" '
                                     'wait-result kit/one --tip %s --kind land -C %s' % (
                                         t1, t2, os.path.join(r'C:\the kit', 'tools', 'gate.py'),
                                         t1, self.main_top())])
        self.assertEqual([self.record(b)['verdict'] for b in ('kit/one', 'kit/two')],
                         ['running', 'running'])
        self.assertIsNone(self.record('kit/one')['base'])   # no --expect-main: none yet

    def test_usage_writes_nothing_and_starts_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        for branches, tips in (([BRANCH, 'kit/two'], [tip]), ([BRANCH], ['abc'])):
            out, detach = self.submit(branches, tips)
            self.assertEqual(out.code, G.USAGE, out.lines)
            self.assertTrue(out.lines[0].startswith('USAGE: '), out.lines)
            self.assertEqual(detach.calls, [])
        self.assertEqual(result.read(self.repo.root, BRANCH, 'land')[0], None)

    def test_a_start_that_fails_finishes_the_records_refused(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, _ = self.submit([BRANCH], [tip], detach=Detach(why='no powershell'))
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(out.lines, ['REFUSED: the landing could not be started: no powershell; '
                                     'nothing was changed'])
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['code'], rec['summary']),
                         ('refused', G.REFUSED, out.lines[0]))
        self.assertIsNotNone(rec['finished_at'])

    def test_the_command_is_the_carrier_beside_the_tool(self):
        """The machine lock's detach shape (kitlib.lock.detach.command), with no --detached-id:
        the pythonw and python.exe beside the interpreter, the carrier beside the tool."""
        tool = os.path.join(self.tmp, 'export', 'tools', 'land.py')
        py = os.path.join(self.tmp, 'py', 'pythonw.exe')
        self.assertEqual(submit.command(tool, ['-C', 'r', 'b'], python=py),
                         [py, os.path.join(self.tmp, 'export', 'tools', 'no_window.py'),
                          os.path.join(self.tmp, 'py', 'python.exe'), '-B', tool, '-C', 'r', 'b'])
        self.assertEqual(submit.command(LAND_PY, [])[:5],
                         [PYTHONW, CARRIER, CONSOLE_PY, '-B', LAND_PY])

    def test_the_detach_reads_the_start_scripts_answer(self):
        argv = submit.command(LAND_PY, ['b'])
        seen = []

        def ran(stdout, rc=0):
            def run(args, **kw):
                data = json.loads(kw['input'])
                self.assertEqual(data['cwd'], 'C:\\cwd')
                self.assertEqual(data['cmd'], subprocess.list2cmdline(argv))
                seen.append(args[-1])
                return subprocess.CompletedProcess(args, rc, stdout, 'err text')
            return run
        self.assertEqual(submit.detach(argv, 'C:\\cwd', {}, run=ran('0 4242\n')), (4242, None))
        # the WMI start of the rule's home, which sets no ShowWindow: nothing rests on it
        self.assertEqual(seen, [lockdetach.CREATE_PS])
        self.assertNotIn('ShowWindow', seen[0])
        for stdout, rc in (('8 0\n', 0), ('', 1), ('0 notapid', 0)):
            pid, why = submit.detach(argv, 'C:\\cwd', {}, run=ran(stdout, rc))
            self.assertIsNone(pid)
            self.assertIn('the detached start got', why)

        def broken(args, **kw):
            raise OSError('no powershell')
        self.assertEqual(submit.detach(argv, 'C:\\cwd', {}, run=broken),
                         (None, 'the detached start failed: no powershell'))

    def test_a_missing_pythonw_or_carrier_starts_nothing(self):
        def run(args, **kw):
            raise AssertionError('started')
        gone = os.path.join(self.tmp, 'nowhere', 'pythonw.exe')
        argv = submit.command(LAND_PY, ['b'])
        for i, what in ((0, 'pythonw.exe'), (1, 'the carrier'), (2, 'python.exe')):
            bad = list(argv)
            bad[i] = gone
            self.assertEqual(submit.detach(bad, 'C:\\cwd', {}, run=run),
                             (None, 'the detached start: %s is not a file: %s' % (what, gone)))

    def test_a_payload_that_cannot_be_read_is_a_submit_error(self):
        bad = os.path.join(self.tmp, 'bad.submit')
        for text in (None, 'not json', '{"runs": [{"branch": "kit/x"}]}'):
            if text is not None:
                with open(bad, 'w', encoding='utf-8') as f:
                    f.write(text)
            with self.assertRaises(submit.SubmitError):
                submit.restore(self.repo.root, bad)


class TheDetachedLanding(SubmitCase):
    """What the detached tool runs: the restored submission, landed; its outcome, whatever it is,
    finishes the records the submission wrote running (the same run_id)."""

    def detached(self, branches, tips, **kw):
        out, _ = self.submit(branches, tips, expect_main=self.main)
        self.assertEqual(out.code, G.OK, out.lines)
        running = {b: self.record(b)['run_id'] for b in branches}
        sub = submit.restore(self.repo.root, out.facts['payload'])
        self.assertEqual([rr.run_id for rr in sub['runs']], [running[b] for b in branches])
        lander = self.lander(branches if len(branches) > 1 else branches[0], self.main,
                             tips if len(tips) > 1 else tips[0], submitted=sub, **kw)
        return lander, running

    def assert_finished(self, branch, run_id, verdict, code):
        rec = self.record(branch)
        self.assertEqual((rec['run_id'], rec['verdict'], rec['code']), (run_id, verdict, code))
        self.assertIsNotNone(rec['finished_at'])
        return rec

    def test_a_landing_that_lands_finishes_pass(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        lander, running = self.detached([BRANCH], [tip])
        out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual([c.get('detached') for c in self.book_calls], [True])
        rec = self.assert_finished(BRANCH, running[BRANCH], 'pass', G.OK)
        self.assertTrue(rec['summary'].startswith('LANDED kit/feature'), rec)
        self.assertEqual(rec['base'], self.main)

    def test_the_machine_locks_refusal_finishes_not_run(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        calls = []

        def busy(kind, repo, slug, **kw):
            calls.append(kw)
            raise LockError(6, 'the queue is full')
        lander, running = self.detached([BRANCH], [tip], book=busy)
        out = lander.run()
        self.assertEqual(out.code, 6, out.lines)
        self.assertEqual([c.get('detached') for c in calls], [True])
        rec = self.assert_finished(BRANCH, running[BRANCH], 'not-run', 6)
        self.assertEqual(rec['summary'], 'NOT RUN: the machine lock did not give the landing its '
                                         'turn: the queue is full; nothing was changed')

    def test_a_refusal_finishes_refused(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        lander, running = self.detached([BRANCH], [self.repo.initial])     # not its tip
        out = lander.run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        rec = self.assert_finished(BRANCH, running[BRANCH], 'refused', G.REFUSED)
        self.assertIn('kit/feature is at %s' % tip[:10], rec['summary'])
        self.assertEqual(self.remote_main(), self.main)

    def test_a_conflict_finishes_fail_with_the_conflict_line(self):
        _, tip = self.branch('feature', {'README.md': 'branch\n'})
        self.repo.write('README.md', 'main\n')
        moved = self.repo.commit('main edit', 'README.md')
        self.repo.git('push', '-q', 'origin', 'main')
        lander, running = self.detached([BRANCH], [tip])
        out = lander.run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        rec = self.assert_finished(BRANCH, running[BRANCH], 'fail', G.FAILED)
        self.assertEqual(rec['summary'], 'CONFLICT kit/feature with main %s: the author merges '
                                         'main' % moved)

    def test_an_exception_finishes_error(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        lander, running = self.detached([BRANCH], [tip])

        def boom():
            raise RuntimeError('boom')
        lander.preconditions = boom
        with self.assertRaises(RuntimeError):
            lander.run()
        rec = self.assert_finished(BRANCH, running[BRANCH], 'error', None)
        self.assertIn('RuntimeError: boom', rec['summary'])

    def test_its_own_running_records_are_not_prior_records_and_what_they_replaced_is(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        before = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
        before.finish(G.Outcome(G.FAILED, ['NOT LANDED: red']))
        lander, running = self.detached([BRANCH], [tip])
        ids = [r['run_id'] for r in lander.prior_records()]
        self.assertIn(before.run_id, ids)
        self.assertNotIn(running[BRANCH], ids)


class Resubmit(SubmitCase):
    """One submission never overwrites another's files or records: the payload and log are named
    by the run_id, a live running landing or a landed tip refuses the submission at once, and a
    landing whose process died is finished, never left running."""

    def dead_pid(self):
        p = testutil.dead_process()
        self.addCleanup(p.wait)             # holds the Popen, so Windows cannot reuse the pid
        return p.pid

    def test_each_submission_names_its_payload_and_log_by_its_run_id(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        first, _ = self.submit([BRANCH], [tip], detach=Detach(pid=self.dead_pid()))
        self.assertEqual(first.code, G.REFUSED, first.lines)     # its landing died: not running
        second, _ = self.submit([BRANCH], [tip])
        self.assertEqual(second.code, G.OK, second.lines)
        rid = self.record(BRANCH)['run_id']
        p1, p2 = first.facts['payload'], second.facts['payload']
        self.assertEqual(os.path.basename(p2), '%s.%s.land.submit' % (result.file_stem(BRANCH), rid))
        self.assertEqual(os.path.basename(second.facts['output']),
                         '%s.%s.land.log' % (result.file_stem(BRANCH), rid))
        self.assertNotEqual(p1, p2)
        self.assertNotEqual(first.facts['output'], second.facts['output'])
        for path in (p1, p2):
            with open(path, encoding='utf-8') as f:
                self.assertEqual(json.load(f)['runs'][0]['run_id'], submit.run_id_of(path))
        self.assertEqual(submit.run_id_of(p2), rid)

    def test_a_batch_shares_one_run_id(self):
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, _ = self.submit(['kit/one', 'kit/two'], [t1, t2])
        rid = submit.run_id_of(out.facts['payload'])
        self.assertEqual([self.record(b)['run_id'] for b in ('kit/one', 'kit/two')], [rid, rid])

    def test_a_live_running_landing_refuses_a_submit_and_changes_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, _ = self.submit([BRANCH], [tip])           # its landing is this process: live
        self.assertEqual(out.code, G.OK, out.lines)
        running = self.record(BRANCH)
        for branches, tips in (([BRANCH], [tip]), (['kit/two', BRANCH], [t2, tip])):
            again, detach = self.submit(branches, tips)
            self.assertEqual(again.code, G.REFUSED, again.lines)
            self.assertTrue(again.lines[0].startswith(
                'REFUSED: a landing of kit/feature is already running (run %s, pid %d'
                % (running['run_id'], os.getpid())), again.lines)
            self.assertTrue(again.lines[0].endswith('; nothing was changed'), again.lines)
            self.assertEqual(detach.calls, [])
            self.assertEqual(self.record(BRANCH), running)
        self.assertEqual(result.read(self.repo.root, 'kit/two', 'land'), (None, 'none'))

    def test_a_running_landing_whose_process_died_does_not_refuse_a_submit(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        gone = result.Run(self.repo.root, 'land', BRANCH, tip)
        gone.pid = self.dead_pid()
        gone.start()
        out, detach = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len(detach.calls), 1)
        self.assertNotEqual(self.record(BRANCH)['run_id'], gone.run_id)

    def test_a_tip_that_landed_refuses_a_resubmit_and_keeps_its_record(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        for code in (G.OK, G.LANDED_UNGATED):
            done = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
            done.finish(G.Outcome(code, ['LANDED kit/feature']))
            landed = self.record(BRANCH)
            out, detach = self.submit([BRANCH], [tip])
            self.assertEqual(out.code, G.REFUSED, out.lines)
            self.assertEqual(out.lines, ['REFUSED: kit/feature at %s already landed (run %s, '
                                         'LANDED kit/feature); nothing was changed' % (
                                             tip[:10], done.run_id)])
            self.assertEqual(detach.calls, [])
            self.assertEqual(self.record(BRANCH), landed)
        other = result.Run(self.repo.root, 'land', BRANCH, 'b' * 40)   # landed at another tip
        other.finish(G.Outcome(G.OK, ['LANDED kit/feature']))
        self.assertEqual(self.submit([BRANCH], [tip])[0].code, G.OK)

    def test_a_landing_that_died_before_it_recorded_itself_is_finished_error(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        pid = self.dead_pid()
        out, _ = self.submit([BRANCH], [tip], detach=Detach(pid=pid), claim_wait_s=5)
        why = 'the landing process died before it started: pid %d ended without recording ' \
              'itself' % pid
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(out.lines, ['REFUSED: %s; nothing was changed; its output: %s' % (
            why, out.facts['output'])])
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['run_id'], rec['pid']),
                         ('error', submit.run_id_of(out.facts['payload']), None))
        self.assertEqual(rec['summary'], '%s; its output: %s' % (why, out.facts['output']))
        with open(out.facts['output'], encoding='utf-8') as f:
            self.assertIn('ERROR: %s' % why, f.read())

    def test_the_submitter_waits_for_the_landing_to_record_itself(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        now, slept, payload = [0.0], [], []

        def detach(argv, cwd, env):
            payload.append(argv[-1])
            return 4242, None

        def sleep(s):                       # the detached landing starts: restore claims
            slept.append(s)
            now[0] += s
            if len(slept) == 2:
                submit.restore(self.repo.root, payload[0])
        with mock.patch.object(process, 'alive', return_value=True):
            out, _ = self.submit([BRANCH], [tip], detach=detach, claim_wait_s=5,
                                 clock=lambda: now[0], sleep=sleep)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len(slept), 2)                     # no longer than until the claim
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['pid'], rec['ctime']),
                         ('running', os.getpid(), process.own_ctime()))   # the claimer's

    def test_a_landing_still_starting_gets_its_pid_and_its_death_is_seen(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        pid = self.dead_pid()
        with mock.patch.object(process, 'alive', return_value=True):  # alive while submitting
            out, _ = self.submit([BRANCH], [tip], detach=Detach(pid=pid))
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual((self.record(BRANCH)['verdict'], self.record(BRANCH)['pid']),
                         ('running', pid))
        rec, why = result.wait(self.repo.root, BRANCH, tip, kind='land', timeout_s=0)
        self.assertIsNone(why)
        self.assertEqual((rec['verdict'], rec['summary']), (
            'error', 'the landing process died: pid %d ended without finishing its record' % pid))

    def unreadable(self, branches, tips):
        out, _ = self.submit(branches, tips)
        self.assertEqual(out.code, G.OK, out.lines)
        with open(out.facts['payload'], 'w', encoding='utf-8') as f:
            f.write('not json')
        return out

    def test_a_landing_that_cannot_read_its_payload_finishes_the_records_refused(self):
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out = self.unreadable(['kit/one', 'kit/two'], [t1, t2])
        rid = submit.run_id_of(out.facts['payload'])
        with self.assertRaises(submit.SubmitError) as e:
            submit.restore(self.repo.root, out.facts['payload'])
        gave = submit.abandon(self.repo.root, out.facts['payload'], str(e.exception))
        line = 'REFUSED: %s; nothing was changed' % e.exception
        self.assertEqual((gave.code, gave.lines[-1]), (G.REFUSED, line))
        for b in ('kit/one', 'kit/two'):
            rec = self.record(b)
            self.assertEqual((rec['verdict'], rec['code'], rec['run_id'], rec['summary']),
                             ('refused', G.REFUSED, rid, line))
        with open(out.facts['output'], encoding='utf-8') as f:
            self.assertIn(line, f.read())

    def test_the_tools_finish_the_records_of_a_payload_they_cannot_read(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        for tool in ([LAND_PY], [GATE_PY, 'land']):
            out = self.unreadable([BRANCH], [tip])
            r = subprocess.run([sys.executable] + tool + [BRANCH, '-C', self.repo.root,
                                                          '--expect-tip', tip, submit.FLAG,
                                                          out.facts['payload']],
                               capture_output=True, text=True, timeout=HANG_S, env=self.env,
                               stdin=subprocess.DEVNULL, cwd=KIT_ROOT)
            self.assertEqual(r.returncode, G.REFUSED, (r.stdout, r.stderr))
            self.assertIn('REFUSED: cannot read the submission', r.stdout)
            rec = self.record(BRANCH)
            self.assertEqual((rec['verdict'], rec['run_id']),
                             ('refused', submit.run_id_of(out.facts['payload'])))


class LeaseChild(unittest.TestCase):
    """pin.lease_child: a pinned tool's detached child holds the export it runs from."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, self.tmp)
        self.sha = 'ab' * 20
        self.tool = os.path.join(self.tmp, pin.PINS, self.sha[:12], 'tools', 'land.py')
        self.leases = os.path.join(self.tmp, pin.PINS, pin.LEASES)

    def test_a_child_of_a_pinned_tool_gets_a_lease_its_prune_respects(self):
        self.assertTrue(pin.lease_child(self.tool, os.getpid(), {pin.PINNED_ENV: self.sha}))
        data = pin.read_lease(os.path.join(self.leases, '%s-%d.json' % (self.sha[:12],
                                                                        os.getpid())))
        self.assertEqual((data['sha'], data['pid'], data['ctime']),
                         (self.sha, os.getpid(), process.own_ctime()))
        self.assertTrue(pin.lease_live(data))

    def test_nothing_to_lease(self):
        live = os.path.join(KIT_ROOT, 'tools', 'land.py')
        for tool, env in ((self.tool, {}), (self.tool, {pin.PINNED_ENV: pin.LIVE}),
                          (live, {pin.PINNED_ENV: self.sha}),
                          (self.tool, {pin.PINNED_ENV: 'cd' * 20})):
            self.assertFalse(pin.lease_child(tool, os.getpid(), env), (tool, env))
        with mock.patch.object(process, 'proc_ctime', return_value=None):     # the pid is gone
            self.assertFalse(pin.lease_child(self.tool, 4, {pin.PINNED_ENV: self.sha}))
        self.assertFalse(os.path.exists(self.leases))


@unittest.skipUnless(os.name == 'nt', 'the detached start is Windows only')
class RealCarrier(unittest.TestCase):
    """submit.detach for real, on the command's pythonw, carrier and python.exe running a 2 s
    sleep: the process WMI starts is pythonw.exe, the python.exe that runs the program is its
    child, and no process of the tree owns a console window, hidden or shown (a console python.exe
    WMI starts itself has a ConsoleWindowClass window, hidden only by ShowWindow)."""

    def test_the_detached_program_runs_under_the_carrier_with_no_console_window(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, tmp)
        said = os.path.join(tmp, 'said.txt')
        code = ('import os, sys, time; f = open(%r, "w"); f.write("%%d %%s" %% (os.getppid(), '
                'sys.executable)); f.close(); time.sleep(2)' % said)
        argv = submit.command(LAND_PY, [])[:3] + ['-c', code]
        pid, why = submit.detach(argv, tmp, dict(os.environ))
        self.assertIsNotNone(pid, why)
        ctime = process.proc_ctime(pid)
        self.assertIsNotNone(ctime, 'the carrier ended at once')
        names = dict((p, n) for p, _pp, n in process.snapshot())
        self.assertEqual(names.get(pid, '').lower(), 'pythonw.exe')
        seen, end = [], time.monotonic() + HANG_S
        while process.alive(pid, ctime) and time.monotonic() < end:
            tree = {pid} | {p for p, _c in process.descendants(pid, ctime)}
            seen += console_windows_of(tree)
            time.sleep(0.05)
        self.assertFalse(process.alive(pid, ctime), 'the carrier did not end')
        with open(said, encoding='utf-8') as f:
            parent, exe = f.read().split(' ', 1)
        self.assertEqual(int(parent), pid)                      # the carrier started it
        self.assertEqual(os.path.basename(exe).lower(), 'python.exe')
        self.assertEqual(seen, [], 'a console window belonged to the detached start')


@unittest.skipUnless(os.name == 'nt', 'the detached start is Windows only')
class RealDetach(SubmitCase):
    """tools/land.py --submit and tools/gate.py land --submit, run for real on a landing from a
    linked worktree: each prints its one SUBMITTED line and exits 0, and the detached landing it
    started refuses (not a main checkout) before it books anything, finishing the record."""

    def run_submit(self, argv):
        wt, tip = self.branch('feature', {'a.py': 'a\n'})
        r = subprocess.run([sys.executable] + argv + [BRANCH, '-C', wt, '--expect-tip', tip,
                                                      '--submit'],
                           capture_output=True, text=True, timeout=HANG_S, env=self.env,
                           stdin=subprocess.DEVNULL, cwd=KIT_ROOT)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertTrue(lines[0].startswith('SUBMITTED kit/feature tip %s wait: python ' % tip),
                        lines)
        self.assertTrue(lines[0].endswith(' wait-result kit/feature --tip %s --kind land -C %s'
                                          % (tip, self.main_top())), lines)  # not the worktree
        rec, why = result.wait(wt, BRANCH, tip, kind='land', timeout_s=HANG_S, poll_s=0.1)
        self.assertIsNone(why)
        self.until_closed(rec['log'])
        self.assertEqual(rec['verdict'], 'refused', rec)
        self.assertIn('not a main checkout', rec['summary'])
        with open(rec['log'], encoding='utf-8') as f:
            self.assertIn('not a main checkout', f.read())  # the detached tool's own output

    def until_closed(self, log):
        """The detached tool holds its output file open until it ends (an open file cannot be
        renamed on Windows): wait for that, so the cleanup finds no process in the folder."""
        end = time.monotonic() + HANG_S
        while True:
            try:
                os.rename(log, log + '.closed')
                os.rename(log + '.closed', log)
                break
            except OSError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.1)
        time.sleep(0.5)     # the process ends just after its output closes

    def test_land_py_submit(self):
        self.run_submit([LAND_PY])

    def test_gate_py_land_submit(self):
        self.run_submit([GATE_PY, 'land'])


if __name__ == '__main__':
    unittest.main()
