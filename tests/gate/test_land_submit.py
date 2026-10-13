"""kitlib.gate.submit (land.py --submit, gate.py land --submit): the one SUBMITTED line with its
wait-result command, the running land records written before the detached start, and the detached
landing (Lander(submitted=...)) finishing those same records whatever it ends with, refusals and
the machine lock's included. pin.lease_child: the lease a pinned tool's detached child holds. On a
scratch repository with a bare remote and the scratch lock store (tests/gate/fixtures.py); the
detached start is a fake, except in RealDetach, which starts tools/land.py and tools/gate.py
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
from kitlib.lock.store import LockError
from kitlib.proc import process
from tests import gitfixture
from tests.gate.test_land import BRANCH, HANG_S, KIT_ROOT, LandCase

LAND_PY = os.path.join(KIT_ROOT, 'tools', 'land.py')
GATE_PY = os.path.join(KIT_ROOT, 'tools', 'gate.py')


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
        args = ['-C', self.repo.root] + sum((['--expect-tip', t] for t in tips), []) + branches
        out = submit.submit(self.repo.root, LAND_PY, args, branches, tips,
                            environ=dict(environ or {}), detach_fn=detach, kit=kit, **kw)
        return out, detach

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
                                     'kit/feature --tip %s --kind land' % (
                                         tip, os.path.join(r'C:\kit', 'tools', 'gate.py'), tip)])
        payload, output = submit.files(self.repo.root, [BRANCH])
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
        self.assertEqual(argv[:3], [sys.executable, '-B', LAND_PY])
        self.assertEqual(argv[3:], ['-C', self.repo.root, '--expect-tip', tip, BRANCH,
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
                                     'wait-result kit/one --tip %s --kind land' % (
                                         t1, t2, os.path.join(r'C:\the kit', 'tools', 'gate.py'),
                                         t1)])
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

    def test_the_detach_reads_the_start_scripts_answer(self):
        def ran(stdout, rc=0):
            def run(argv, **kw):
                self.assertEqual(json.loads(kw['input'])['cwd'], 'C:\\cwd')
                return subprocess.CompletedProcess(argv, rc, stdout, 'err text')
            return run
        self.assertEqual(submit.detach(['x'], 'C:\\cwd', {}, run=ran('0 4242\n')), (4242, None))
        for stdout, rc in (('8 0\n', 0), ('', 1), ('0 notapid', 0)):
            pid, why = submit.detach(['x'], 'C:\\cwd', {}, run=ran(stdout, rc))
            self.assertIsNone(pid)
            self.assertIn('the detached start got', why)

        def broken(argv, **kw):
            raise OSError('no powershell')
        self.assertEqual(submit.detach(['x'], 'C:\\cwd', {}, run=broken),
                         (None, 'the detached start failed: no powershell'))

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
        sub = submit.restore(self.repo.root, submit.files(self.repo.root, branches)[0])
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


@unittest.skipUnless(os.name == 'nt', 'the detached start is Win32_Process.Create')
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
        self.assertTrue(lines[0].endswith(' wait-result kit/feature --tip %s --kind land' % tip),
                        lines)
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
