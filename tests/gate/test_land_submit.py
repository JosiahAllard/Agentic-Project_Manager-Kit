"""kitlib.gate.submit (land.py --submit, gate.py land --submit): the SUBMITTED line with its
wait-result command, after a REVIEW line for each verdict queued at a tip submitted, the running land records written before the detached start, and the detached
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
import threading
import time
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.allowrules import rules
from kitlib.gate import pin, result, submit, verdicts
from kitlib.git import repo
from kitlib.lock import detach as lockdetach
from kitlib.lock.store import LockError
from kitlib.proc import process
from tests import gitfixture, testutil
from tests.gate.test_land import BRANCH, HANG_S, KIT_ROOT, LandCase
from tests.home import machine_config
from tests.lock.test_detach_cli import console_windows_of
from tests.platforms import windows_only

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
        kw.setdefault('landing_of', lambda carrier: None)   # no python.exe under a fake carrier
        tool = kw.pop('tool', LAND_PY)
        args = ['-C', self.repo.root] + sum((['--expect-tip', t] for t in tips), []) + branches
        out = submit.submit(self.repo.root, tool, args, branches, tips,
                            environ=dict(environ or {}), detach_fn=detach, kit=kit, **kw)
        return out, detach

    def restored(self, out, detach, branches, tips, expect_main=None):
        """submit.restore as the detached landing of `out` runs it: with its own arguments and
        the token the (fake) detached start got in its environment."""
        token = detach.calls[-1][2].get(submit.TOKEN_ENV)
        return submit.restore(self.repo.root, out.facts['payload'], branches, tips,
                              expect_main=expect_main, environ={submit.TOKEN_ENV: token})

    def dead_pid(self):
        p = testutil.dead_process()
        self.addCleanup(p.wait)             # holds the Popen, so Windows cannot reuse the pid
        return p.pid

    def main_top(self):
        """The wait command's -C: the main checkout as git names it, quoted when it has a space."""
        top = repo.main_checkout_toplevel(self.repo.root)
        self.assertIsNotNone(top)
        return '"%s"' % top if ' ' in top else top

    def record(self, branch):
        rec, why = result.read(self.repo.root, branch, 'land')
        self.assertIsNotNone(rec, why)
        return rec

    REVIEW = {'verdict': 'LAND', 'risk': 'High', 'reviewer': 'pm-kit:reviewer',
              'reviewer_model': 'opus', 'reviewer_id': 'agent-r1',
              'author': 'Kit Test / strong-worker', 'session': 'coord-1', 'records': []}

    def common(self):
        common, why = repo.common_dir(self.repo.root)
        self.assertIsNotNone(common, why)
        return common

    def queue(self, branch, tip, **kw):
        verdicts.add(self.common(), dict(self.REVIEW, branch=branch, tip=tip,
                                         range='%s..%s' % (self.main[:10], tip), **kw))


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

    def test_a_batch_names_every_branch_and_waits_on_the_last(self):
        """The last branch's record is the batch's last written (TheDetachedLanding
        test_a_batch_finishes_the_record_its_wait_names_last)."""
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, _ = self.submit(['kit/one', 'kit/two'], [t1, t2], kit=r'C:\the kit')
        self.assertEqual(out.lines, ['SUBMITTED kit/one+kit/two tip %s,%s wait: python "%s" '
                                     'wait-result kit/two --tip %s --kind land -C %s' % (
                                         t1, t2, os.path.join(r'C:\the kit', 'tools', 'gate.py'),
                                         t2, self.main_top())])
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

    def test_on_linux_the_command_is_the_python_and_the_tool_and_the_start_its_backend(self):
        """kitlib.platform.linux.detach: no pythonw, no carrier; the start is its trampoline."""
        from kitlib.platform.linux import detach as ld
        tool = os.path.join(self.tmp, 'export', 'tools', 'land.py')
        with mock.patch('kitlib.platform.NAME', 'linux'):
            argv = submit.command(tool, ['-C', 'r', 'b'], python='/usr/bin/python3')
            self.assertEqual(argv, [os.path.abspath('/usr/bin/python3'), '-B', tool,
                                    '-C', 'r', 'b'])
            self.assertEqual(submit.detach(argv, '/w', {}),
                             (None, 'the detached start: python is not a file: %s' % argv[0]))
            with mock.patch.object(ld, 'create', return_value=(4242, None)) as made:
                ok = submit.command(LAND_PY, ['b'], python=sys.executable)
                self.assertEqual(submit.detach(ok, '/w', {'A': '1'}), (4242, None))
            self.assertEqual(made.call_args[0][0], ok)
            self.assertEqual(made.call_args[0][2]['A'], '1')

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
        """At the path submit names it: elsewhere it is untrusted (test_land_restore_trust).
        An older kit's schema is one too: abandon finishes its records, and it is resubmitted."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, detach = self.submit([BRANCH], [tip])
        bad = out.facts['payload']
        for text in (None, 'not json',
                     '{"schema": %d, "runs": [{"branch": "kit/x"}]}' % submit.SCHEMA,
                     '{"schema": 1, "runs": []}'):
            if text is None:
                os.remove(bad)
            else:
                with open(bad, 'w', encoding='utf-8') as f:
                    f.write(text)
            with self.assertRaises(submit.SubmitError):
                self.restored(out, detach, [BRANCH], [tip])


class OldFolder(SubmitCase):
    """The result folder is out of the git common dir (docs/gate.md "Result record"): an older
    kit's land record in the old folder (<git common dir>/kit-gate-result) still stops a
    submission, and the submission never writes it (only result.finish_dead writes there, to
    finish a dead old-folder record in place); what a landing trusts (the records a submission
    replaces, the claim) reads the new folder only; with no result folder (no dev_drive) a
    submission refuses and writes nothing anywhere."""

    def plant(self, rec):
        path = result.legacy_path(self.repo.root, rec['branch'], rec['kind'])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        return path

    def test_an_old_folder_landing_that_lives_refuses_a_submit(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        old = result.build(BRANCH, tip, self.main, 'land', None, verdict=result.RUNNING,
                           pid=os.getpid(), ctime=process.own_ctime())
        path = self.plant(old)
        out, detach = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith(
            'REFUSED: a landing of kit/feature is already running (run %s, pid %d'
            % (old['run_id'], os.getpid())), out.lines)
        self.assertEqual(detach.calls, [])
        self.assertEqual(result.read_path(path), (old, None))
        self.assertEqual(result.read(self.repo.root, BRANCH, 'land', legacy=False), (None, 'none'))

    def test_an_old_folder_landed_tip_refuses_a_resubmit(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        old = result.build(BRANCH, tip, self.main, 'land', G.OK, summary='LANDED kit/feature')
        path = self.plant(old)
        out, detach = self.submit([BRANCH], [tip])
        self.assertEqual(out.lines, ['REFUSED: kit/feature at %s already landed (run %s, LANDED '
                                     'kit/feature); nothing was changed' % (tip[:10], old['run_id'])])
        self.assertEqual(detach.calls, [])
        self.assertEqual(result.read_path(path), (old, None))

    def test_an_old_folder_record_is_not_one_the_submission_replaces(self):
        """The records a submission replaces (submit.py `replaced`, which restore puts back) are
        the new folder's only: an old-folder land record of the branch, which read() would take
        when the new folder has none, is not carried into the payload."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        old = result.build(BRANCH, tip, self.main, 'land', G.FAILED, summary='NOT LANDED: red')
        path = self.plant(old)
        self.assertEqual(result.read(self.repo.root, BRANCH, 'land'), (old, None))
        out, _ = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.OK, out.lines)
        with open(out.facts['payload'], encoding='utf-8') as f:
            self.assertEqual(json.load(f)['replaced'], [])
        self.assertEqual(result.read_path(path), (old, None))

    def test_the_claim_never_takes_an_old_folder_record_as_unclaimed(self):
        """claim() watches the new folder only (submit.py `_unclaimed`): a landing that claimed
        its new-folder record is claimed, even with an unclaimed `running` record of the same
        run in the old folder that started later (one read() would take). With the landing's
        process gone, the old record would otherwise make the claim report it died."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = os.path.join(tmp.name, 'out.txt')     # a claim that thinks the landing died notes it
        rr = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
        rr.pid, rr.ctime = None, None       # as submit writes it: not claimed yet
        rr.start(log=out)
        done = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
        done.run_id, done.started_at = rr.run_id, rr.started_at
        done.start(log=out)                 # the landing claimed it: its pid is in the record
        old = result.build(BRANCH, tip, self.main, 'land', None, verdict=result.RUNNING,
                           run_id=rr.run_id, started_at='2999-01-01T00:00:00Z')
        path = self.plant(old)
        self.assertEqual(result.read(self.repo.root, BRANCH, 'land'), (old, None))
        self.assertIsNone(submit.claim(self.repo.root, [rr], 4242, out, wait_s=0,
                                       clock=lambda: 0.0, sleep=lambda s: None,
                                       alive=lambda pid, ctime: False))
        self.assertFalse(os.path.exists(out))
        rec, _ = result.read(self.repo.root, BRANCH, 'land', legacy=False)
        self.assertEqual((rec['verdict'], rec['run_id'], rec['pid']),
                         ('running', rr.run_id, os.getpid()))
        self.assertEqual(result.read_path(path), (old, None))

    def test_with_no_result_folder_a_submit_refuses_and_writes_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self._kit_dev_drive = None
        machine_config(None, {}, case=self)             # the machine config sets no dev_drive
        out, detach = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(len(out.lines), 1, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: the machine config sets no dev_drive'),
                        out.lines)
        self.assertTrue(out.lines[0].endswith('; nothing was changed'), out.lines)
        self.assertEqual(detach.calls, [])
        self.assertFalse(os.path.exists(result.legacy_dir(self.repo.root)))
        self.assertEqual(os.listdir(self.result_drive), [])


class ReviewLines(SubmitCase):
    """A REVIEW line, before the SUBMITTED line, for each branch with a verdict queued at the tip
    submitted (docs/gate.md "Submitting a landing"): the submission shows the review on record."""

    def test_a_verdict_queued_at_the_tip_is_said_before_the_submitted_line(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        out, _ = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.lines[0], 'REVIEW kit/feature %s: LAND by pm-kit:reviewer/opus '
                                       '(agent-r1)' % tip)
        self.assertEqual(len(out.lines), 2, out.lines)
        self.assertTrue(out.lines[1].startswith('SUBMITTED kit/feature tip %s wait: ' % tip),
                        out.lines)

    def test_a_carried_verdict_names_the_commit_reviewed(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, reviewed)
        self.repo.write('m.py', 'm\n')
        self.main = self.repo.commit('main edit', 'm.py')
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt)
        tip = self.repo.head(wt)
        verdicts.carry(self.common(), self.repo.root, BRANCH, self.main)
        out, _ = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.lines[0], 'REVIEW kit/feature %s: LAND by pm-kit:reviewer/opus '
                                       '(agent-r1), carried from %s over 1 main merge'
                         % (tip, reviewed))
        self.assertTrue(out.lines[1].startswith('SUBMITTED kit/feature tip %s wait: ' % tip),
                        out.lines)

    def test_a_verdict_at_another_tip_has_no_line(self):
        wt, old = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, old)
        self.repo.write('b.py', 'b\n', root=wt)
        tip = self.repo.commit('more', 'b.py', cwd=wt)
        out, _ = self.submit([BRANCH], [tip])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len(out.lines), 1, out.lines)
        self.assertTrue(out.lines[0].startswith('SUBMITTED '), out.lines)


class NamedReview(SubmitCase):
    """A submission names the review each branch lands on, --reviewed-by <reviewer id> before
    --submit (submit.named_review; docs/gate.md "Submitting a landing"), so the review shows on
    the command line the host's permission check reads. Missing or miscounted: USAGE with an
    Instead: line, the command in the land permission rule's form with the queued ids; a wrong id,
    or no usable verdict at the tip: REFUSED, no Instead: line. Nothing written either way."""
    AGAIN = staticmethod(lambda ids: 'again ' + ' '.join(ids))

    def named(self, branches, tips, given, **kw):
        return self.submit(branches, tips, reviewed_by=given, again=self.AGAIN, **kw)

    def assert_nothing_written(self, out, detach, branches):
        self.assertEqual(detach.calls, [], out.lines)
        for b in branches:
            rec, why = result.read(self.repo.root, b, 'land')
            self.assertIsNone(rec, (b, rec, why))

    def test_the_queued_reviewer_named_submits(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        for given in ('Agent-r1', ' agent-r1 '):        # case and spaces aside
            self.assertIsNone(submit.named_review(self.repo.root, [BRANCH], [tip], [given]))
        out, detach = self.named([BRANCH], [tip], ['agent-r1'])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[-1].startswith('SUBMITTED '), out.lines)
        self.assertEqual(len(detach.calls), 1)
        self.assertNotIn(submit.REVIEWED_BY, detach.calls[0][0])    # not the detached child's

    def test_any_id_the_entry_lists_names_it(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip, reviewer_id='agent-r1 / agent-r2')
        out, _ = self.named([BRANCH], [tip], ['agent-r2'])
        self.assertEqual(out.code, G.OK, out.lines)

    def test_no_reviewer_named_is_usage_with_the_command_to_run(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip, reviewer_id='agent-r1 / agent-r2')
        out, detach = self.named([BRANCH], [tip], [])
        self.assertEqual(out.code, G.USAGE, out.lines)
        self.assertTrue(out.lines[0].startswith('USAGE: a submission names the review each '
                                                'branch lands on: one --reviewed-by'), out.lines)
        self.assertIn('(0 given for 1 branch)', out.lines[0])
        self.assertEqual(out.lines[1:], ['Instead: again agent-r1'])
        self.assert_nothing_written(out, detach, [BRANCH])

    def test_a_wrong_id_is_refused_without_a_command(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        out, detach = self.named([BRANCH], [tip], ['agent-r9'])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(out.lines, [
            'REFUSED: --reviewed-by agent-r9 for kit/feature is not the reviewer of its verdict '
            'queued at %s: LAND by pm-kit:reviewer/opus (agent-r1); name the review that verdict '
            'records, or queue the one you mean (tools/verdict.py add); nothing was changed'
            % tip])
        self.assert_nothing_written(out, detach, [BRANCH])

    def test_no_usable_verdict_at_the_tip_is_refused(self):
        wt, old = self.branch('feature', {'a.py': 'a\n'})
        out, detach = self.named([BRANCH], [old], ['agent-r1'])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('kit/feature lands only with a verdict queued at its tip, and none is '
                      'queued for it', out.lines[0])
        self.assert_nothing_written(out, detach, [BRANCH])
        out, detach = self.named([BRANCH], [old], [])     # no Instead: with nothing queued
        self.assertEqual((out.code, len(out.lines)), (G.REFUSED, 1), out.lines)
        self.queue(BRANCH, old)
        self.repo.write('b.py', 'b\n', root=wt)
        tip = self.repo.commit('more', 'b.py', cwd=wt)
        out, detach = self.named([BRANCH], [tip], ['agent-r1'])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('the verdict of kit/feature was given at %s, and the submission names %s'
                      % (old, tip), out.lines[0])
        self.assert_nothing_written(out, detach, [BRANCH])
        self.queue(BRANCH, tip)
        self.assertTrue(verdicts.mark_failed(self.common(), BRANCH, 'red gate'))
        out, detach = self.named([BRANCH], [tip], ['agent-r1'])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('the verdict of kit/feature is marked failed (red gate)', out.lines[0])
        self.assert_nothing_written(out, detach, [BRANCH])

    def test_an_entry_that_cannot_be_used_is_refused(self):
        """A queue file holding another branch's entry (verdicts.OtherBranch) or no entry at all
        (verdicts.VerdictError) is REFUSED by name, whatever the ids, with nothing written."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        common = self.common()
        with open(verdicts.entry_path(common, BRANCH), encoding='utf-8') as f:
            held = f.read()
        with open(verdicts.entry_path(common, 'kit/other'), 'w', encoding='utf-8') as f:
            f.write(held)
        out = submit.named_review(self.repo.root, ['kit/other'], [tip], ['agent-r1'])
        self.assertEqual((out.code, len(out.lines)), (G.REFUSED, 1), out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: the verdict entry of kit/other cannot '
                                                'be used: '), out.lines)
        self.assertIn('holds the entry of kit/feature, not kit/other', out.lines[0])
        with open(verdicts.entry_path(common, BRANCH), 'w', encoding='utf-8') as f:
            f.write('{not json')
        for given in (['agent-r1'], []):
            out, detach = self.named([BRANCH], [tip], given)
            self.assertEqual((out.code, len(out.lines)), (G.REFUSED, 1), out.lines)
            self.assertTrue(out.lines[0].startswith('REFUSED: the verdict entry of kit/feature '
                                                    'cannot be used: '), out.lines)
            self.assertIn('is not a verdict entry', out.lines[0])
            self.assertTrue(out.lines[0].endswith('; nothing was changed'), out.lines)
            self.assert_nothing_written(out, detach, [BRANCH])

    def test_a_queue_that_cannot_be_found_is_refused(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        with mock.patch.object(submit.repo, 'common_dir', return_value=(None, 'git said no')):
            out = submit.named_review(self.repo.root, [BRANCH], [tip], ['agent-r1'])
        self.assertEqual((out.code, out.lines), (G.REFUSED, [
            'REFUSED: the verdict queue cannot be found: git said no; nothing was changed']))

    def test_a_carried_verdict_is_named_at_the_head(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, reviewed)
        self.repo.write('m.py', 'm\n')
        self.main = self.repo.commit('main edit', 'm.py')
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt)
        tip = self.repo.head(wt)
        verdicts.carry(self.common(), self.repo.root, BRANCH, self.main)
        out, _ = self.named([BRANCH], [tip], ['agent-r1'])
        self.assertEqual(out.code, G.OK, out.lines)

    def test_a_batch_names_one_reviewer_per_branch_in_order(self):
        _, ta = self.branch('a', {'a.py': 'a\n'})
        _, tb = self.branch('b', {'b.py': 'b\n'})
        self.queue('kit/a', ta)
        self.queue('kit/b', tb, reviewer_id='agent-r2')
        out, detach = self.named(['kit/a', 'kit/b'], [ta, tb], ['agent-r1'])
        self.assertEqual(out.code, G.USAGE, out.lines)
        self.assertIn('(1 given for 2 branches)', out.lines[0])
        self.assertEqual(out.lines[1:], ['Instead: again agent-r1 agent-r2'])
        self.assert_nothing_written(out, detach, ['kit/a', 'kit/b'])
        out, detach = self.named(['kit/a', 'kit/b'], [ta, tb], ['agent-r2', 'agent-r1'])
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('--reviewed-by agent-r2 for kit/a is not the reviewer', out.lines[0])
        self.assert_nothing_written(out, detach, ['kit/a', 'kit/b'])
        out, _ = self.named(['kit/a', 'kit/b'], [ta, tb], ['agent-r1', 'agent-r2'])
        self.assertEqual(out.code, G.OK, out.lines)

    def test_the_instead_command_is_one_the_land_permission_rule_allows(self):
        """submit.again_line in the rule's form: forward slashes (the rules deny a backslash),
        -C first, --submit last; allowed by the rule lines tools/allow_rules.py prints."""
        tip = 'a' * 40
        kit, root = r'Z:\pm-kit\plugin\0123456789ab', 'Y:/repo'
        line = submit.again_line('land.py', ['-C', root, BRANCH, '--expect-tip', tip],
                                 ['aad31bc6c5e0a88a3'], kit=kit)
        self.assertEqual(line, 'python Z:/pm-kit/plugin/0123456789ab/tools/land.py -C Y:/repo '
                               'kit/feature --expect-tip %s --reviewed-by aad31bc6c5e0a88a3 '
                               '--submit' % tip)
        lines = {'allow': rules.allow_lines('Z:/pm-kit/plugin/0123456789ab', root),
                 'deny': rules.deny_lines('Z:/pm-kit/plugin/0123456789ab', root)}
        self.assertEqual(rules.decide(lines, 'PowerShell', line), 'allow')
        for extra, said in ((['--expect-main', tip, '--summary', 'one', '--wait', '5', '--full',
                              '--fresh', '--keep-branch'], 'allow'),
                            (['--summary', 'two words'], 'deny'),     # quoted: the normal prompt
                            (['--log-dir', 'Y:/logs'], 'deny')):
            line = submit.again_line('land.py', ['-C', root, BRANCH, '--expect-tip', tip] + extra,
                                     ['aad31bc6c5e0a88a3'], kit=kit)
            self.assertEqual(rules.decide(lines, 'PowerShell', line), said, line)
        line = submit.again_line('gate.py', ['land', BRANCH, '-C', root, '--expect-tip', tip],
                                 ['aad31bc6c5e0a88a3'], kit=kit)
        self.assertIsNone(rules.decide(lines, 'PowerShell', line))    # no rule: the normal prompt

    def run_tool(self, *args):
        return subprocess.run([sys.executable] + list(args), capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, cwd=KIT_ROOT,
                              env=dict(os.environ, KIT_PINNED='live'))

    def test_the_tools_print_the_command_with_the_queued_reviewer(self):
        """tools/land.py and tools/gate.py land: a bare --submit exits 2 with the Instead: line,
        the caller's own submission with the queued reviewer named; nothing written."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        root = os.path.abspath(self.repo.root).replace('\\', '/')
        kit = KIT_ROOT.replace('\\', '/')
        for tool, argv in (('land.py', [LAND_PY, '-C', self.repo.root, BRANCH]),
                           ('gate.py', [GATE_PY, 'land', BRANCH, '-C', self.repo.root])):
            with self.subTest(tool=tool):
                r = self.run_tool(*(argv + ['--expect-tip', tip, '--full', '--submit']))
                self.assertEqual(r.returncode, G.USAGE, (r.stdout, r.stderr))
                lines = r.stdout.splitlines()
                self.assertTrue(lines[0].startswith('USAGE: a submission names the review'),
                                lines)
                head = ('-C %s %s' % (root, BRANCH) if tool == 'land.py'
                        else 'land %s -C %s' % (BRANCH, root))
                self.assertEqual(lines[1:], [
                    'Instead: python %s/tools/%s %s --expect-tip %s --full --reviewed-by '
                    'agent-r1 --submit' % (kit, tool, head, tip)])
                rec, why = result.read(self.repo.root, BRANCH, 'land')
                self.assertIsNone(rec, (rec, why))

    def test_the_command_keeps_the_submissions_other_arguments_in_order(self):
        """--expect-main, --summary (double-quoted for its space) and --wait come back after
        --expect-tip and before --reviewed-by, in each tool's own argument order."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        root = os.path.abspath(self.repo.root).replace('\\', '/')
        kit = KIT_ROOT.replace('\\', '/')
        rest = ['--expect-main', self.main, '--summary', 'two words', '--wait', '5']
        for tool, argv, head in (
                ('land.py', [LAND_PY, '-C', self.repo.root, BRANCH, '--expect-tip', tip],
                 '-C %s %s --expect-tip %s' % (root, BRANCH, tip)),
                ('gate.py', [GATE_PY, 'land', BRANCH, '-C', self.repo.root, '--expect-tip', tip],
                 'land %s -C %s --expect-tip %s' % (BRANCH, root, tip))):
            with self.subTest(tool=tool):
                r = self.run_tool(*(argv + rest + ['--submit']))
                self.assertEqual(r.returncode, G.USAGE, (r.stdout, r.stderr))
                self.assertEqual(r.stdout.splitlines()[1:], [
                    'Instead: python %s/tools/%s %s --expect-main %s --summary "two words" '
                    '--wait 5 --reviewed-by agent-r1 --submit' % (kit, tool, head, self.main)])

    def test_the_tools_take_reviewed_by_only_with_submit(self):
        tip = 'b' * 40
        for argv in ([LAND_PY, 'kit/x', '--expect-tip', tip, '--reviewed-by', 'r'],
                     [GATE_PY, 'land', 'kit/x', '--expect-tip', tip, '--reviewed-by', 'r']):
            with self.subTest(argv=argv[0]):
                r = self.run_tool(*argv)
                self.assertEqual(r.returncode, G.USAGE, (r.stdout, r.stderr))
                self.assertIn('USAGE: --reviewed-by goes with --submit', r.stderr)


class TheDetachedLanding(SubmitCase):
    """What the detached tool runs: the restored submission, landed; its outcome, whatever it is,
    finishes the records the submission wrote running (the same run_id)."""

    def detached(self, branches, tips, **kw):
        out, detach = self.submit(branches, tips, expect_main=self.main)
        self.assertEqual(out.code, G.OK, out.lines)
        running = {b: self.record(b)['run_id'] for b in branches}
        sub = self.restored(out, detach, branches, tips, expect_main=self.main)
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

    def test_a_batch_finishes_the_record_its_wait_names_last(self):
        """A batch has one outcome, written into each branch's record in the batch's order: the
        record the SUBMITTED line waits on is final only once every branch's record is, so the
        wait it prints ends after the whole batch's."""
        branches = ['kit/one', 'kit/two']
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        line = submit.wait_line(branches, [t1, t2], self.repo.root, kit=r'C:\kit')
        waited = line.split(' wait-result ', 1)[1].split()[0]
        lander, running = self.detached(branches, [t1, t2])
        write, seen = result.write, []

        def spy(root, rec, when=None):
            path = write(root, rec, when=when)
            if rec['branch'] == waited and rec['verdict'] != result.RUNNING:
                seen.append([self.record(b)['verdict'] for b in branches])
            return path
        with mock.patch.object(result, 'write', spy):
            out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(seen, [['pass', 'pass']])
        for b in branches:
            self.assert_finished(b, running[b], 'pass', G.OK)

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
        now, slept, started = [0.0], [], []

        def detach(argv, cwd, env):
            started.append((argv[-1], env[submit.TOKEN_ENV]))
            return 4242, None

        def sleep(s):                       # the detached landing starts: restore claims
            slept.append(s)
            now[0] += s
            if len(slept) == 2:
                payload, token = started[0]
                submit.restore(self.repo.root, payload, [BRANCH], [tip],
                               environ={submit.TOKEN_ENV: token})
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

    def test_a_record_finished_or_claimed_before_the_timeout_write_is_kept(self):
        """claim() reads the records, then checks the landing is alive, then times out: a landing
        that finished its record (or claimed it) in that window keeps it, not `running` again
        with the submitter's pid over it."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        for code in (G.OK, None):           # G.OK: the landing finished it; None: it claimed it
            with self.subTest(code=code):
                rr = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
                rr.pid, rr.ctime = None, None   # as submit writes it: not claimed yet
                rr.start(log='out.txt')

                def landing_moves(pid, ctime):  # between claim's read and its timeout write
                    done = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
                    done.run_id, done.started_at = rr.run_id, rr.started_at
                    if code is None:
                        done.start(log='out.txt')
                    else:
                        done.finish(G.Outcome(code, ['LANDED kit/feature']))
                    return True
                self.assertIsNone(submit.claim(self.repo.root, [rr], 4242, 'out.txt', wait_s=0,
                                               clock=lambda: 0.0, sleep=lambda s: None,
                                               alive=landing_moves))
                rec = self.record(BRANCH)
                self.assertEqual((rec['verdict'], rec['run_id'], rec['pid']),
                                 ('running' if code is None else 'pass', rr.run_id,
                                  os.getpid()))

    def test_a_landing_that_finishes_while_the_timeout_write_is_under_way_keeps_its_record(self):
        """The window inside the timeout write itself: the landing finishes its record after
        claim() read it (unclaimed) and before claim() replaces it. The record's lock holds the
        landing's write until claim()'s is done, so the landing's `pass` is the record left, not
        claim()'s `running` over it."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        rr = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
        rr.pid, rr.ctime = None, None       # as submit writes it: not claimed yet
        rr.start(log='out.txt')
        done = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
        done.run_id, done.started_at = rr.run_id, rr.started_at
        timed_out, landing = [], []
        read = result.read_path

        def alive(pid, ctime):              # the last check before the timeout write
            timed_out.append(True)
            return True

        def read_then_land(path):           # claim()'s last read of the record: then the landing
            rec = read(path)
            if timed_out and not landing and threading.current_thread() is main:
                t = threading.Thread(target=done.finish,
                                     args=(G.Outcome(G.OK, ['LANDED kit/feature']),))
                landing.append(t)
                t.start()
                t.join(1.0)                 # it writes now, or waits on the record's lock
            return rec
        main = threading.current_thread()
        with mock.patch.object(result, 'read_path', read_then_land):
            self.assertIsNone(submit.claim(self.repo.root, [rr], 4242, 'out.txt', wait_s=0,
                                           clock=lambda: 0.0, sleep=lambda s: None, alive=alive))
        self.assertEqual(len(landing), 1)
        landing[0].join(30)
        self.assertFalse(landing[0].is_alive())
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['run_id'], rec['pid']),
                         ('pass', rr.run_id, os.getpid()))

    def test_the_claim_watches_and_records_the_landing_not_its_carrier(self):
        """A carrier that is gone (killed) while the landing it started runs: the claim neither
        finishes the records `error` nor records the carrier's pid, but the landing's."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        carrier, asked = self.dead_pid(), []

        def landing_of(pid):
            asked.append(pid)
            return os.getpid()              # the landing: this process, alive
        out, _ = self.submit([BRANCH], [tip], detach=Detach(pid=carrier), landing_of=landing_of)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(asked, [carrier])
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['pid'], rec['ctime']),
                         ('running', os.getpid(), process.own_ctime()))

    def test_a_landing_lookup_that_fails_leaves_the_carriers_pid(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})

        def boom(pid):
            raise OSError('no snapshot')
        with mock.patch.object(process, 'alive', return_value=True):
            out, _ = self.submit([BRANCH], [tip], detach=Detach(pid=4242), landing_of=boom)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.record(BRANCH)['pid'], 4242)

    def test_a_pinned_export_stays_leased_while_the_landing_lives_after_its_carrier_ends(self):
        """pin.prune keeps an export while any live lease holds it: the carrier's lease reads dead
        once the carrier is gone, so the landing's own lease must hold the export it runs from."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        sha = 'ab' * 20
        pins = os.path.join(self.tmp, pin.PINS)
        tool = os.path.join(pins, sha[:12], 'tools', 'land.py')
        carrier = self.dead_pid()
        out, _ = self.submit([BRANCH], [tip], detach=Detach(pid=carrier), tool=tool,
                             environ={pin.PINNED_ENV: sha}, landing_of=lambda c: os.getpid())
        self.assertEqual(out.code, G.OK, out.lines)
        pin.prune(pins)                     # what the next pin does under its lock
        leases = os.path.join(pins, pin.LEASES)
        mine = '%s-%d.json' % (sha[:12], os.getpid())
        self.assertEqual(os.listdir(leases), [mine])        # the carrier's, if written, pruned
        data = pin.read_lease(os.path.join(leases, mine))
        self.assertEqual((data['sha'], data['pid'], data['ctime']),
                         (sha, os.getpid(), process.own_ctime()))
        self.assertTrue(pin.lease_live(data))

    def test_by_default_the_landing_is_the_one_lockdetach_wrapper_pid_finds(self):
        """No landing_of: submit asks kitlib.lock.detach.wrapper_pid for the carrier's landing,
        leases the pinned export to that pid and records it (and its ctime) in the claim."""
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        sha = 'cd' * 20
        pins = os.path.join(self.tmp, pin.PINS)
        tool = os.path.join(pins, sha[:12], 'tools', 'land.py')
        carrier, asked = self.dead_pid(), []

        def wrapper_pid(pid):
            asked.append(pid)
            return os.getpid()              # the landing: this process, alive
        with mock.patch.object(lockdetach, 'wrapper_pid', wrapper_pid):
            out, _ = self.submit([BRANCH], [tip], detach=Detach(pid=carrier), tool=tool,
                                 environ={pin.PINNED_ENV: sha}, landing_of=None)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(asked, [carrier])
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['pid'], rec['ctime']),
                         ('running', os.getpid(), process.own_ctime()))
        leases = os.path.join(pins, pin.LEASES)
        mine = '%s-%d.json' % (sha[:12], os.getpid())
        self.assertIn(mine, os.listdir(leases))
        data = pin.read_lease(os.path.join(leases, mine))
        self.assertEqual((data['sha'], data['pid'], data['ctime']),
                         (sha, os.getpid(), process.own_ctime()))

    def unreadable(self, branches, tips):
        """A submission whose payload is not json: (its Outcome, the token its landing got)."""
        out, detach = self.submit(branches, tips)
        self.assertEqual(out.code, G.OK, out.lines)
        with open(out.facts['payload'], 'w', encoding='utf-8') as f:
            f.write('not json')
        return out, detach.calls[-1][2][submit.TOKEN_ENV]

    def test_a_landing_that_cannot_read_its_payload_finishes_the_records_refused(self):
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, token = self.unreadable(['kit/one', 'kit/two'], [t1, t2])
        rid = submit.run_id_of(out.facts['payload'])
        with self.assertRaises(submit.SubmitError) as e:
            submit.restore(self.repo.root, out.facts['payload'], ['kit/one', 'kit/two'], [t1, t2],
                           environ={submit.TOKEN_ENV: token})
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
            out, token = self.unreadable([BRANCH], [tip])
            r = subprocess.run([sys.executable] + tool + [BRANCH, '-C', self.repo.root,
                                                          '--expect-tip', tip, submit.FLAG,
                                                          out.facts['payload']],
                               capture_output=True, text=True, timeout=HANG_S,
                               env=dict(self.env, **{submit.TOKEN_ENV: token}),
                               stdin=subprocess.DEVNULL, cwd=KIT_ROOT)
            self.assertEqual(r.returncode, G.REFUSED, (r.stdout, r.stderr))
            self.assertIn('REFUSED: cannot read the submission', r.stdout)
            rec = self.record(BRANCH)
            self.assertEqual((rec['verdict'], rec['run_id']),
                             ('refused', submit.run_id_of(out.facts['payload'])))


class Prune(SubmitCase):
    """submit.prune, run by each submission: a payload and log older than KEEP_S whose run no
    land record still has running (queued or landing) are deleted; nothing else is."""

    def finished(self, branch='feature'):
        """A submission whose landing died before it recorded itself: its records are final."""
        _, tip = self.branch(branch, {'%s.py' % branch: '1\n'})
        out, _ = self.submit(['kit/%s' % branch], [tip], detach=Detach(pid=self.dead_pid()))
        self.assertEqual(out.code, G.REFUSED, out.lines)
        return out.facts['payload'], out.facts['output']

    def age(self, *paths, by=None):
        """Set the files' modified time `by` seconds back (default: an hour past KEEP_S)."""
        then = time.time() - (submit.KEEP_S + 3600 if by is None else by)
        for p in paths:
            os.utime(p, (then, then))

    def test_old_files_of_a_finished_landing_go_and_young_ones_stay(self):
        old = self.finished('one')
        young = self.finished('two')
        self.age(*old)
        self.age(*young, by=submit.KEEP_S - 3600)
        self.assertEqual(sorted(submit.prune(self.repo.root)), sorted(old))
        self.assertEqual([os.path.exists(p) for p in old + young], [False, False, True, True])

    def test_a_landing_still_running_keeps_its_files_however_old(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, _ = self.submit([BRANCH], [tip])       # its landing is this process: running
        self.assertEqual(out.code, G.OK, out.lines)
        files = (out.facts['payload'], out.facts['output'])
        self.age(*files)
        self.assertEqual(submit.prune(self.repo.root), [])
        self.assertTrue(all(os.path.exists(p) for p in files))

    def test_a_batch_running_under_another_branchs_record_keeps_its_files(self):
        """The files are named by the batch's first branch; the run is running while any of its
        branches' records is."""
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, _ = self.submit(['kit/one', 'kit/two'], [t1, t2])
        self.assertEqual(out.code, G.OK, out.lines)
        first = self.record('kit/one')
        result.Run(self.repo.root, 'land', 'kit/one', t1).finish(
            G.Outcome(G.FAILED, ['NOT LANDED: red']))      # kit/one's record: another run's
        self.assertNotEqual(self.record('kit/one')['run_id'], first['run_id'])
        files = (out.facts['payload'], out.facts['output'])
        self.age(*files)
        self.assertEqual(submit.prune(self.repo.root), [])
        self.assertTrue(all(os.path.exists(p) for p in files))

    def test_a_record_that_cannot_be_read_prunes_nothing(self):
        old = self.finished()
        self.age(*old)
        with open(result.result_path(self.repo.root, 'kit/other', 'land'), 'w',
                  encoding='utf-8') as f:
            f.write('not json')
        self.assertEqual(submit.prune(self.repo.root), [])
        self.assertTrue(all(os.path.exists(p) for p in old))

    def test_a_file_in_use_is_left_and_the_rest_go(self):
        payload, output = self.finished()
        self.age(payload, output)
        with open(output, encoding='utf-8'):        # open: Windows refuses its delete
            self.assertEqual(submit.prune(self.repo.root), [payload])
        self.assertEqual((os.path.exists(payload), os.path.exists(output)), (False, True))

    def test_only_the_submissions_files_are_pruned(self):
        payload, output = self.finished()
        folder = os.path.dirname(payload)
        others = [os.path.join(folder, n) for n in (
            'notes.land.log', 'x.%s.land.json' % ('a' * 16), 'x.%s.gate.log' % ('a' * 16))]
        for p in others:
            with open(p, 'w', encoding='utf-8') as f:
                f.write('{}')
        keep = others + [result.result_path(self.repo.root, 'kit/feature', 'land')]
        self.age(payload, output, *keep)
        self.assertEqual(sorted(submit.prune(self.repo.root)), sorted([payload, output]))
        self.assertTrue(all(os.path.exists(p) for p in keep))

    def test_a_submission_prunes_old_finished_files(self):
        old = self.finished('one')
        self.age(*old)
        _, tip = self.branch('two', {'two.py': '2\n'})
        out, _ = self.submit(['kit/two'], [tip])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual([os.path.exists(p) for p in old], [False, False])
        self.assertTrue(os.path.exists(out.facts['payload']))
        self.assertTrue(os.path.exists(out.facts['output']))


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


@windows_only('the detached start is Windows only')
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


@windows_only('the detached start is Windows only')
class RealDetach(SubmitCase):
    """tools/land.py --submit and tools/gate.py land --submit, run for real on a landing from a
    linked worktree, naming its queued reviewer: each prints its REVIEW line, then its SUBMITTED
    line, and exits 0, and the detached landing it
    started refuses (not a main checkout) before it books anything, finishing the record."""

    def run_submit(self, argv):
        wt, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        r = subprocess.run([sys.executable] + argv + [BRANCH, '-C', wt, '--expect-tip', tip,
                                                      '--reviewed-by', 'agent-r1', '--submit'],
                           capture_output=True, text=True, timeout=HANG_S, env=self.env,
                           stdin=subprocess.DEVNULL, cwd=KIT_ROOT)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertEqual(lines[0], 'REVIEW kit/feature %s: LAND by pm-kit:reviewer/opus '
                                   '(agent-r1)' % tip)
        lines = lines[1:]
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
        renamed on Windows): wait for that, then for the tool and its carrier (cwd: the scratch
        repository; the carrier ends after the tool) to end, so the cleanup finds no process in
        the folder. Both are bounded by HANG_S; a process still there after it fails the test."""
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
        payload = log[:-len(submit.OUTPUT_EXT)] + submit.PAYLOAD_EXT
        left = self.submission_processes(payload)
        while left and time.monotonic() < end:
            time.sleep(0.05)
            left = [(p, c) for p, c in left if process.alive(p, c)]
        self.assertEqual(left, [], 'the detached landing or its carrier did not end')

    @staticmethod
    def submission_processes(payload):
        """[(pid, ctime)] of the pythonw.exe and python.exe processes whose command line names
        `payload`: the carrier and the landing it started (submit.command), while they run."""
        want, out = os.path.normcase(payload), []
        for pid, _ppid, name in process.snapshot():
            if name.lower() not in ('pythonw.exe', 'python.exe'):
                continue
            ctime = process.proc_ctime(pid)
            line = process.command_line(pid, ctime) if ctime is not None else None
            if line and want in os.path.normcase(line):
                out.append((pid, ctime))
        return out

    def test_land_py_submit(self):
        self.run_submit([LAND_PY])

    def test_gate_py_land_submit(self):
        self.run_submit([GATE_PY, 'land'])


if __name__ == '__main__':
    unittest.main()
