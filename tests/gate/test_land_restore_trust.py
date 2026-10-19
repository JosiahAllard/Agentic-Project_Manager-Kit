"""kitlib.gate.submit.restore trusts only the payload a submission wrote (docs/gate.md "Submitting
a landing"): `land.py --submitted <payload>` and `gate.py land --submitted <payload>` run a landing
only from the payload file submit() named for those branches and that run_id, read with the token
submit() gave the detached landing in its environment, naming the landing's own branches, tips,
main, run_id and output, while each branch's land record is still that run's and unclaimed by
another process. Anything else is REFUSED on stderr, exit 4, with no record written and no
landing; the refusal line is appended to the submission's output only when the payload is at the
path submit named (the detached landing's stderr is a hidden console). The payloads here are agent-written files; the tools run from a linked
worktree, so a landing that got past restore refuses (not a main checkout) before it books."""
import hashlib
import json
import os
import subprocess
import sys

from kitlib import gate as G
from kitlib.gate import result, submit
from tests.gate.test_land import BRANCH, HANG_S, KIT_ROOT
from tests.gate.test_land_submit import GATE_PY, LAND_PY, SubmitCase

TOOLS = ([LAND_PY], [GATE_PY, 'land'])
# The environment variable that carries the token to the detached landing (submit.TOKEN_ENV)
TOKEN_ENV = 'KIT_LAND_SUBMIT_TOKEN'


class TrustCase(SubmitCase):

    def genuine(self, branches=None, files=None):
        """A real submission (the fake detached start, kept in self.started): (worktree, tips,
        its Outcome, the token its detached landing got)."""
        branches = branches or [BRANCH]
        wt, tips = None, []
        for b in branches:
            name = b.split('/', 1)[1]
            wt_b, tip = self.branch(name, files or {'%s.py' % name: '1\n'})
            wt = wt or wt_b
            tips.append(tip)
        out, detach = self.submit(branches, tips, expect_main=self.main)
        self.assertEqual(out.code, G.OK, out.lines)
        [self.started] = detach.calls
        return wt, tips, out, self.started[2].get(TOKEN_ENV)

    def snapshot(self, *paths):
        """{path: bytes or None}: the land records and files a refusal must leave as they were."""
        got = {}
        for p in paths:
            try:
                with open(p, 'rb') as f:
                    got[p] = f.read()
            except FileNotFoundError:
                got[p] = None
        return got

    def record_path(self, branch=BRANCH):
        return result.result_path(self.repo.root, branch, 'land')

    def run_tool(self, tool, wt, branches, tips, payload, token=None, expect_main=None):
        env = {k: v for k, v in self.env.items() if k != TOKEN_ENV}
        if token is not None:
            env[TOKEN_ENV] = token
        args = list(branches) + ['-C', wt]
        for t in tips:
            args += ['--expect-tip', t]
        if expect_main:
            args += ['--expect-main', expect_main]
        return subprocess.run([sys.executable] + tool + args + [submit.FLAG, payload],
                              capture_output=True, text=True, timeout=HANG_S, env=env,
                              stdin=subprocess.DEVNULL, cwd=KIT_ROOT)

    def assert_refused(self, r, payload, before, logged=None):
        """Exit 4 with the one REFUSED line on stderr, and every path in `before` as it was,
        except `logged` (the submission's output, for a payload at the path submit names): that
        line appended to it, so a detached landing's reason outlives its hidden console."""
        self.assertEqual(r.returncode, G.REFUSED, (r.stdout, r.stderr))
        self.assertEqual(r.stdout, '', r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith('REFUSED: %s %s: ' % (submit.FLAG, payload)), lines)
        self.assertTrue(lines[0].endswith('; nothing was changed'), lines)
        want = dict(before)
        if logged is not None:
            want[logged] = (before[logged] or b'') + (lines[0] + '\n').encode('utf-8')
        self.assertEqual(self.snapshot(*before), want)

    def forge(self, path, runs, output, token='agent', schema=None):
        """An agent-written payload at `path`, with the hash of a token the agent chose."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {'schema': submit.SCHEMA if schema is None else schema, 'output': output,
                'replaced': [], 'runs': runs,
                'token_sha256': hashlib.sha256(token.encode('ascii')).hexdigest()}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return path

    def runs_of(self, payload):
        with open(payload, encoding='utf-8') as f:
            return json.load(f)['runs']


class AgentWrittenPayloads(TrustCase):

    def test_a_payload_outside_the_result_folder_is_refused_however_well_it_names_a_run(self):
        """1: a temp-folder payload naming a live submission's run_id, branch and tip, an output
        of its own, and the hash of a token the agent holds: neither tool runs it."""
        wt, [tip], out, _ = self.genuine()
        runs = self.runs_of(out.facts['payload'])
        rid = runs[0]['run_id']
        stolen = os.path.join(self.tmp, 'forged', 'stolen.log')
        for tool in TOOLS:
            with self.subTest(tool=tool[-1]):
                payload = self.forge(os.path.join(self.tmp, 'forged', 'x.%s.land.submit' % rid),
                                     runs, stolen)
                before = self.snapshot(self.record_path(), out.facts['output'],
                                       out.facts['payload'], stolen)
                r = self.run_tool(tool, wt, [BRANCH], [tip], payload, token='agent',
                                  expect_main=self.main)
                self.assert_refused(r, payload, before)
                self.assertFalse(os.path.exists(stolen))

    def test_an_unreadable_payload_outside_the_result_folder_abandons_nothing(self):
        """2: a corrupt file named for a live submission's run_id finishes none of its records."""
        wt, [tip], out, token = self.genuine()
        rid = submit.run_id_of(out.facts['payload'])
        payload = os.path.join(self.tmp, 'x.%s.land.submit' % rid)
        with open(payload, 'w', encoding='utf-8') as f:
            f.write('not json')
        for tool in TOOLS:
            with self.subTest(tool=tool[-1]):
                before = self.snapshot(self.record_path(), out.facts['output'],
                                       out.facts['payload'])
                r = self.run_tool(tool, wt, [BRANCH], [tip], payload, token=token,
                                  expect_main=self.main)
                self.assert_refused(r, payload, before)
                self.assertEqual(self.record(BRANCH)['verdict'], result.RUNNING)

    def test_the_genuine_payload_with_no_token_is_refused(self):
        """3: the payload the submission wrote, run by anything but its detached landing."""
        wt, [tip], out, _ = self.genuine()
        payload = out.facts['payload']
        for tool in TOOLS:
            with self.subTest(tool=tool[-1]):
                before = self.snapshot(self.record_path(), out.facts['output'], payload)
                r = self.run_tool(tool, wt, [BRANCH], [tip], payload, token=None,
                                  expect_main=self.main)
                self.assert_refused(r, payload, before, logged=out.facts['output'])
                self.assertIn('no submission token', r.stderr)

    def test_the_genuine_payload_with_a_wrong_token_is_refused(self):
        """4"""
        wt, [tip], out, token = self.genuine()
        payload = out.facts['payload']
        for tool in TOOLS:
            for wrong in ('0' * 32, 'agent', (token or 'x')[:-1]):
                with self.subTest(tool=tool[-1], wrong=wrong):
                    before = self.snapshot(self.record_path(), out.facts['output'], payload)
                    r = self.run_tool(tool, wt, [BRANCH], [tip], payload, token=wrong,
                                      expect_main=self.main)
                    self.assert_refused(r, payload, before, logged=out.facts['output'])

    def test_the_right_token_with_another_landings_arguments_is_refused(self):
        """5: the payload's branches, tips and main are the landing's own arguments."""
        wt, [tip], out, token = self.genuine()
        _, t2 = self.branch('two', {'two.py': '2\n'})
        payload = out.facts['payload']
        for branches, tips, main in (([BRANCH], [self.repo.initial], self.main),   # another tip
                                     ([BRANCH], [tip], None),                      # no main
                                     ([BRANCH], [tip], self.repo.initial),         # another main
                                     ([BRANCH, 'kit/two'], [tip, t2], self.main),  # a batch
                                     (['kit/two'], [t2], self.main)):              # another branch
            with self.subTest(branches=branches, tips=tips, main=main):
                before = self.snapshot(self.record_path(), self.record_path('kit/two'),
                                       out.facts['output'], payload)
                r = self.run_tool([LAND_PY], wt, branches, tips, payload, token=token,
                                  expect_main=main)
                # kit/two alone names another payload path: refused before its log is known
                self.assert_refused(r, payload, before, logged=(
                    out.facts['output'] if branches[0] == BRANCH else None))

    def test_the_genuine_payload_rewritten_is_refused(self):
        """6: the token's hash kept, the output or a run_id changed."""
        wt, [tip], out, token = self.genuine()
        payload = out.facts['payload']
        with open(payload, encoding='utf-8') as f:
            data = json.load(f)
        elsewhere = os.path.join(self.tmp, 'elsewhere.log')
        other = dict(data['runs'][0], run_id='f' * 16)
        for change in ({'output': elsewhere}, {'output': None}, {'runs': [other]}):
            with self.subTest(change=sorted(change)):
                with open(payload, 'w', encoding='utf-8') as f:
                    json.dump(dict(data, **change), f)
                before = self.snapshot(self.record_path(), out.facts['output'], payload,
                                       elsewhere)
                r = self.run_tool([LAND_PY], wt, [BRANCH], [tip], payload, token=token,
                                  expect_main=self.main)
                self.assert_refused(r, payload, before, logged=out.facts['output'])
                self.assertFalse(os.path.exists(elsewhere))

    def test_a_record_another_process_claimed_is_refused(self):
        """7: the record is the run's, but a process that is neither the landing nor its carrier
        holds it."""
        wt, [tip], out, token = self.genuine()
        rec = self.record(BRANCH)
        rr = result.Run(self.repo.root, 'land', BRANCH, tip, base=rec['base'])
        rr.run_id, rr.started_at = rec['run_id'], rec['started_at']
        rr.pid, rr.ctime = self.dead_pid(), None
        rr.start(log=rec['log'])
        payload = out.facts['payload']
        before = self.snapshot(self.record_path(), out.facts['output'], payload)
        r = self.run_tool([LAND_PY], wt, [BRANCH], [tip], payload, token=token,
                          expect_main=self.main)
        self.assert_refused(r, payload, before, logged=out.facts['output'])

    def test_a_record_of_another_run_or_finished_is_refused(self):
        wt, [tip], out, token = self.genuine()
        payload = out.facts['payload']
        newer = result.Run(self.repo.root, 'land', BRANCH, tip, base=self.main)
        newer.finish(G.Outcome(G.FAILED, ['NOT LANDED: red']))
        before = self.snapshot(self.record_path(), out.facts['output'], payload)
        r = self.run_tool([LAND_PY], wt, [BRANCH], [tip], payload, token=token,
                          expect_main=self.main)
        self.assert_refused(r, payload, before, logged=out.facts['output'])

    def test_no_token_never_reads_the_payload_so_nothing_is_abandoned(self):
        """Without a token a payload at the submission's own path that is missing, not json or
        an older kit's schema is untrusted, not a SubmitError: abandon would finish the run's
        records refused for any caller. The records stay running; only the refusal is logged."""
        wt, [tip], out, _ = self.genuine()
        payload = out.facts['payload']
        with open(payload, encoding='utf-8') as f:
            data = json.load(f)
        old = dict(data, schema=1)
        old.pop('token_sha256')
        for name, text in (('missing', None), ('not json', 'not json'),
                           ('schema 1', json.dumps(old))):
            if text is None:
                if os.path.exists(payload):
                    os.remove(payload)
            else:
                with open(payload, 'w', encoding='utf-8') as f:
                    f.write(text)
            for tool in TOOLS:
                with self.subTest(payload=name, tool=tool[-1]):
                    before = self.snapshot(self.record_path(), out.facts['output'], payload)
                    with self.assertRaises(submit.Untrusted) as e:
                        submit.restore(self.repo.root, payload, [BRANCH], [tip],
                                       expect_main=self.main, environ={})
                    self.assertEqual(e.exception.log, out.facts['output'])
                    r = self.run_tool(tool, wt, [BRANCH], [tip], payload, token=None,
                                      expect_main=self.main)
                    self.assert_refused(r, payload, before, logged=out.facts['output'])
                    self.assertIn('no submission token', r.stderr)
                    self.assertEqual(self.record(BRANCH)['verdict'], result.RUNNING)


class InProcess(TrustCase):

    def test_untrusted_is_not_a_submit_error(self):
        """A SubmitError is abandoned (the submission's records finished refused); an untrusted
        payload must change nothing, so the tools never take one for the other."""
        self.assertFalse(issubclass(submit.Untrusted, submit.SubmitError))
        self.assertEqual(submit.TOKEN_ENV, TOKEN_ENV)

    def test_the_token_is_in_the_detached_environment_only(self):
        _, [tip], out, token = self.genuine()
        self.assertRegex(token or '', r'^[0-9a-f]{32}$')
        with open(out.facts['payload'], encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn(token, text)
        data = json.loads(text)
        self.assertEqual(data['schema'], 2)
        self.assertEqual(data['token_sha256'],
                         hashlib.sha256(token.encode('ascii')).hexdigest())
        argv, _cwd, _env = self.started
        self.assertNotIn(token, ' '.join(argv))

    def test_restore_refuses_in_process_and_writes_nothing(self):
        _, [tip], out, token = self.genuine()
        payload = out.facts['payload']
        before = self.snapshot(self.record_path(), out.facts['output'], payload)
        for environ in ({}, {TOKEN_ENV: 'a' * 32}):
            with self.assertRaises(submit.Untrusted):
                submit.restore(self.repo.root, payload, [BRANCH], [tip], expect_main=self.main,
                               environ=environ)
        self.assertEqual(self.snapshot(*before), before)

    def test_restore_pops_the_token_so_nothing_the_landing_runs_inherits_it(self):
        _, [tip], out, token = self.genuine()
        environ = {TOKEN_ENV: token, 'KEEP': 'me'}
        submit.restore(self.repo.root, out.facts['payload'], [BRANCH], [tip],
                       expect_main=self.main, environ=environ)
        self.assertEqual(environ, {'KEEP': 'me'})

    def test_an_old_schema_payload_at_its_own_path_is_abandoned(self):
        """A payload an older kit's submit wrote, read by this restore: a SubmitError, so the tool
        finishes its records refused (abandon) and says to submit again."""
        _, [tip], out, token = self.genuine()
        payload = out.facts['payload']
        with open(payload, encoding='utf-8') as f:
            data = json.load(f)
        data['schema'] = 1
        data.pop('token_sha256')
        with open(payload, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        with self.assertRaises(submit.SubmitError) as e:
            submit.restore(self.repo.root, payload, [BRANCH], [tip], expect_main=self.main,
                           environ={TOKEN_ENV: token})
        self.assertIn('submit the landing again', str(e.exception))

    def test_the_round_trip_lands(self):
        """8: submit, then restore with the token its detached landing got, then the landing."""
        _, [tip], out, token = self.genuine()
        rid = submit.run_id_of(out.facts['payload'])
        sub = submit.restore(self.repo.root, out.facts['payload'], [BRANCH], [tip],
                             expect_main=self.main, environ={TOKEN_ENV: token})
        self.assertEqual([rr.run_id for rr in sub['runs']], [rid])
        self.assertEqual((self.record(BRANCH)['pid'], self.record(BRANCH)['run_id']),
                         (os.getpid(), rid))
        got = self.lander(BRANCH, self.main, tip, submitted=sub).run()
        self.assertEqual(got.code, G.OK, got.lines)
        rec = self.record(BRANCH)
        self.assertEqual((rec['run_id'], rec['verdict']), (rid, 'pass'))

    def test_a_batch_round_trip_restores_every_run(self):
        _, tips, out, token = self.genuine(['kit/one', 'kit/two'])
        sub = submit.restore(self.repo.root, out.facts['payload'], ['kit/one', 'kit/two'], tips,
                             expect_main=self.main, environ={TOKEN_ENV: token})
        rid = submit.run_id_of(out.facts['payload'])
        self.assertEqual([(rr.branch, rr.run_id) for rr in sub['runs']],
                         [('kit/one', rid), ('kit/two', rid)])
