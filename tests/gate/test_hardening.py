"""The gate's second review round: the pushed commit is the commit the merged gate passed, a
SystemExit or Ctrl+C never leaves a merge or a lock behind silently, a gate where nothing ran does
not pass, a partial gate writes no record, config errors before a merge are refusals, the record
names the commit its gate config came from, a lock usage error is worded USAGE, a requires probe
that times out ends its whole tree, a release failure after a verified push is still LANDED, and
--measure never writes the live table under another name.

Scratch repositories with a bare remote in temp and the scratch lock store only."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.config.types import ConfigError
from kitlib.gate import land, runner, steps
from kitlib.git import cmd
from kitlib.lock import run as lockrun
from kitlib.proc import process
from kitlib.records import gate_ok, landing
from tests import gitfixture
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, LandCase

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')

# A step that does what the file MODE in the tree it runs in says, then prints ok; it exits 1
# when the mode ends with "red".
MODE_STEP = '''import os, subprocess, sys
m = open('MODE').read().strip() if os.path.exists('MODE') else ''
if m == 'edit':
    with open('README.md', 'a') as f:
        f.write('changed by the gate\\n')
elif m == 'commit':
    with open('c.txt', 'w') as f:
        f.write('c\\n')
    subprocess.check_call(['git', 'add', 'c.txt'])
    subprocess.check_call(['git', '-c', 'user.name=gate', '-c', 'user.email=gate@example.invalid',
                           'commit', '-q', '-m', 'made by the gate'])
elif m.startswith('untracked'):
    with open('left.txt', 'w') as f:
        f.write('x\\n')
print('ok')
sys.exit(1 if m.endswith('red') else 0)
'''

QUITS = (
    'import sys\n'
    'def quits(ctx):\n'
    '    ctx["log"].write("about to exit\\n")\n'
    '    sys.exit(0)\n'
    'GATE_CHECKS = {"quits": quits}\n')

MISSING = 'kit-no-such-command-for-tests'


def joined(out):
    return '\n'.join(out.lines)


class _CommittedTools(LandCase):
    """LandCase with files committed on main (and pushed) before each test."""
    FILES = {}

    def setUp(self):
        LandCase.setUp(self)
        if self.FILES:
            for rel, text in self.FILES.items():
                self.repo.write(rel, text)
            self.main = self.repo.commit('tools', *self.FILES)
            self.repo.git('push', '-q', 'origin', 'main')


# ---------------------------------------------------------------- 1 the pushed commit is the gated one

class GatedIsPushed(_CommittedTools):
    GATE = {'steps': [{'name': 'mode', 'always': True, 'cmd': ['{python}', 'tools/mode_step.py'],
                       'pass': '^ok$'}]}
    FILES = {'tools/mode_step.py': MODE_STEP}

    def land_mode(self, mode):
        _, tip = self.branch('feature', {'MODE': mode + '\n'})
        return self.lander(BRANCH, self.main, tip).run()

    def assert_nothing_pushed(self, out):
        self.assertEqual(self.remote_main(), self.main, out.lines)
        self.assertEqual(self.records(), [], out.lines)

    def test_a_step_that_edits_a_tracked_file_pushes_nothing(self):
        out = self.land_mode('edit')
        self.assertEqual(out.code, G.STUCK, out.lines)      # its edit is never thrown away
        self.assertIn('README.md', joined(out))
        self.assert_nothing_pushed(out)
        self.assertEqual(self.lock().read()['phase'], 'stuck')
        text = joined(out)                                  # what is left, and how to clear it
        self.assertIn('tracked changes: M README.md', text)
        self.assertIn('reset --keep %s' % self.main, text)
        self.assertIn('.break_lock()', text)

    def test_a_step_that_commits_pushes_nothing(self):
        out = self.land_mode('commit')
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertIn('HEAD moved', joined(out))
        self.assert_nothing_pushed(out)
        self.assertNotEqual(self.main_now(), self.main)       # left for a person, not guessed at

    def test_an_untracked_file_left_by_a_green_gate_undoes_and_pushes_nothing(self):
        out = self.land_mode('untracked')
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.undone)
        self.assertIn('left.txt', joined(out))
        self.assertIn('needs cleaning', joined(out))
        self.assert_nothing_pushed(out)
        self.assertEqual(self.main_now(), self.main)
        self.assertIsNone(self.lock().read())
        self.assertTrue(os.path.exists(self.repo.path('left.txt')))   # kept, and reported

    def test_an_untracked_file_left_by_a_red_gate_is_undone_not_stuck(self):
        out = self.land_mode('untracked-red')
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.undone)
        self.assertIn('left.txt', joined(out))
        self.assert_nothing_pushed(out)
        self.assertEqual(self.main_now(), self.main)
        self.assertIsNone(self.lock().read())

    def test_the_undo_has_the_merge_timeout(self):
        seen = []
        real = cmd.call

        def spy(args, cwd, stdin=None, timeout=cmd._DEFAULT, env=None):
            seen.append((list(args), timeout))
            return real(args, cwd, stdin=stdin, timeout=timeout, env=env)
        _, tip = self.branch('feature', {'MODE': 'red\n'})
        with mock.patch.object(cmd, 'call', spy):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.undone, out.lines)
        resets = [t for a, t in seen if a[:1] == ['reset']]
        self.assertEqual(resets, [land.MERGE_TIMEOUT])


# ---------------------------------------------------------------- 2 SystemExit and Ctrl+C

class ChecksThatExit(_CommittedTools):
    KIT = dict(LandCase.KIT, hook={'module': 'kit_hooks'})
    GATE = {'steps': [{'name': 'quits', 'always': True, 'check': 'quits'}]}
    FILES = {'tools/kit_hooks.py': QUITS}

    def assert_logged_exit(self, logs):
        found = [os.path.join(d, f) for d, _, fs in os.walk(logs) for f in fs if f == 'quits.log']
        self.assertEqual(len(found), 1, found)
        with open(found[0], encoding='utf-8') as f:
            text = f.read()
        self.assertIn('SystemExit', text)
        self.assertIn('code 0', text)

    def test_sys_exit_0_in_a_branch_gate_check_is_a_failed_check(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[0].startswith('GATE FAILED: quits'), out.lines)
        self.assertIsNone(out.record)
        self.assert_logged_exit(self.logs)

    def test_sys_exit_0_in_a_landing_check_is_red_and_undone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.verdict.startswith('GATE FAILED: quits'), out.lines)
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.FAILED)
        self.assert_logged_exit(self.logs)

    def test_a_hook_module_that_exits_on_import_is_refused_in_a_branch_gate(self):
        wt, tip = self.branch('feature', {'tools/kit_hooks.py': 'raise SystemExit(0)\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('SystemExit', out.lines[0])
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))


class CtrlC(LandCase):
    def test_ctrl_c_before_the_merge_releases_the_lock(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []
        lander = self.lander(BRANCH, self.main, tip, say=said.append)
        with mock.patch.object(land.Lander, 'locked_checks', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lander.run()
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.main_now(), self.main)
        self.assertTrue(any('nothing was changed' in s for s in said), said)

    def test_ctrl_c_during_the_gate_undoes_the_merge_and_releases(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []

        def interrupted(lander):
            raise KeyboardInterrupt
        lander = self.lander(BRANCH, self.main, tip, say=said.append, merged_gate=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            lander.run()
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertTrue(any('undone' in s for s in said), said)

    def test_an_error_during_the_gate_is_undone_not_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def broken(lander):
            raise RuntimeError('gate bug')
        out = self.lander(BRANCH, self.main, tip, merged_gate=broken).run()
        self.assertIn('RuntimeError: gate bug', joined(out))
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.FAILED)


# ---------------------------------------------------------------- 3 a gate where nothing ran

SKIPPED = {'steps': [{'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$',
                      'requires': [MISSING]}]}


class NothingRan(fixtures.GateRepoCase):
    GATE = SKIPPED

    def test_every_step_skipped_is_refused_without_a_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: no gate step ran'), out.lines)
        self.assertIn('unit', out.lines[0])
        self.assertIn(MISSING, out.lines[0])
        self.assertIsNone(out.record)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))


class SomeSkipped(fixtures.GateRepoCase):
    GATE = {'steps': [
        {'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'},
        {'name': 'probe', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$',
         'requires': [['{python}', '-c', 'import sys; sys.exit(3)']]}]}

    def test_a_skipped_step_beside_a_passing_one_still_records(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (skipped: probe)')
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        rec, why = gate_ok.verify(wt, tip)
        self.assertIsNone(why)
        self.assertEqual([(s['name'], s['result']) for s in rec['steps']],
                         [('unit', 'pass'), ('probe', 'skip')])


class StrictRequires(fixtures.GateRepoCase):
    GATE = {'steps': [dict(SKIPPED['steps'][0], requires_strict=True)]}

    def test_a_missing_strict_requirement_fails(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertIn(MISSING, out.lines[0])
        self.assertIsNone(out.record)


class LandingNothingRan(LandCase):
    GATE = SKIPPED

    def test_a_landing_where_nothing_ran_is_refused_and_undone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.lines[0].startswith('REFUSED: no gate step ran'), out.lines)
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.REFUSED)


# ---------------------------------------------------------------- 4 --base

class PartialBase(fixtures.GateRepoCase):
    def test_a_non_default_base_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, base=self.repo.initial, book_kw=self.book_kw,
                                 log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.record)
        self.assertIn('partial', out.no_record)
        self.assertIn('no gate-OK record', joined(out))
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))

    def test_the_default_base_named_explicitly_still_records(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, base='origin/main', book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))


# ---------------------------------------------------------------- 5 config errors before a merge

class BadLockFile(LandCase):
    KIT = dict(LandCase.KIT, landing=dict(LandCase.KIT['landing'], lock_file='elsewhere.lock'))

    def test_a_bad_lock_file_is_refused_with_nothing_changed(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('lock_file', out.lines[0])
        self.assertIn('nothing was changed', out.lines[0])
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)


class ConfigErrorUnderTheLock(LandCase):
    def test_a_config_error_in_the_locked_checks_is_refused_and_released(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(land.Lander, 'locked_checks', side_effect=ConfigError('bad lanes')):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('bad lanes', out.lines[0])
        self.assertIn('nothing was changed', out.lines[0])
        self.assertIsNone(self.lock().read())


# ---------------------------------------------------------------- 6 config_rev

class ConfigRev(LandCase):
    # The fixture identity is the owner: a branch that changes .kit.json lands only as his
    # (kitlib.guard.ownercommits).
    KIT = dict(LandCase.KIT, owner={'email': gitfixture.EMAIL})

    def test_a_branch_gate_record_names_head_as_its_config_rev(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = runner.verify_record(wt, tip)
        self.assertIsNone(why)
        self.assertEqual(rec['config_rev'], tip)

    def test_a_landing_record_names_expect_main_as_its_config_rev(self):
        gate = {'steps': [dict(fixtures.GATE['steps'][0], name='other')]}
        wt, tip = self.branch('feature', {'.kit.json': json.dumps(dict(self.KIT, gate=gate))})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = runner.verify_record(self.repo.root, out.merge)
        self.assertIsNone(why)                       # main's config, not the merged tree's
        self.assertEqual(rec['config_rev'], self.main)
        self.assertEqual([s['name'] for s in rec['steps']], ['unit'])

    def test_a_record_whose_config_rev_has_another_config_does_not_verify(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        rec, _ = gate_ok.read(wt, tip)
        rec['config_rev'] = self.repo.initial               # before .kit.json existed
        gate_ok.write(wt, rec)
        _, why = runner.verify_record(wt, tip)
        self.assertIn('another gate config', why)
        rec['config_rev'] = 'f' * 40                        # not in the commit's history
        gate_ok.write(wt, rec)
        _, why = runner.verify_record(wt, tip)
        self.assertIsNotNone(why)
        self.assertIsNone(gate_ok.verify(wt, tip)[1])       # the old reading is unchanged


# ---------------------------------------------------------------- 7 lock usage wording

class UnknownKind(fixtures.GateRepoCase):
    GATE = dict(fixtures.GATE, kind='nosuch-kind')

    def test_an_unknown_gate_kind_is_usage_not_refused(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.USAGE, out.lines)
        self.assertTrue(out.lines[0].startswith('USAGE:'), out.lines)
        self.assertIn('nosuch-kind', out.lines[0])
        self.assertNotIn('refused', out.lines[0].lower())


# ---------------------------------------------------------------- 8 smaller fixes

GRANDCHILD = (
    'import subprocess, sys, time\n'
    'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
    'open(sys.argv[1], "w").write(str(c.pid))\n'
    'time.sleep(120)\n')


def exited_within(pid, seconds):
    """True once pid has exited, False if it is still running after `seconds`. TerminateProcess
    is asynchronous: a process it just ended can be opened and is not yet signalled for a few
    tens of ms on a loaded machine, so the check waits (a missed grandchild sleeps 120 s)."""
    ctime = process.proc_ctime(pid)
    deadline = time.monotonic() + seconds
    while ctime is not None and process.alive(pid, ctime):
        if time.monotonic() >= deadline:
            process.terminate_verified(pid, ctime)      # do not leave it running
            return False
        time.sleep(0.05)
    return True


class ProbeTimeout(fixtures.GateRepoCase):
    def test_a_probe_that_times_out_ends_its_whole_tree(self):
        pid_file = os.path.join(self.tmp, 'grandchild.pid')
        step = types.SimpleNamespace(cwd=self.tmp, requires=[
            [sys.executable, '-c', GRANDCHILD, pid_file]])
        with mock.patch.object(runner, 'PROBE_TIMEOUT', 5):
            slot = lockrun.book('engine-test', 'kit-tests', 'probe', **self.book_kw)
            with slot:
                missing = runner.unmet(step, slot.launcher)
                with open(pid_file) as f:
                    grandchild = int(f.read())
                alive = exited_within(grandchild, 5) is False
                slot.release(0, 'done')
        self.assertEqual(len(missing), 1)
        self.assertFalse(alive, 'the probe\'s child outlived the probe timeout')


class ReleaseAfterPush(LandCase):
    def test_a_release_failure_after_a_verified_push_is_landed_with_a_warning(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(landing.LandingLock, 'release',
                               side_effect=landing.LockError('disk said no')):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(any(l.startswith('LANDED kit/feature') for l in out.lines), out.lines)
        self.assertTrue(any(l.startswith('WARNING:') and 'disk said no' in l for l in out.lines),
                        out.lines)
        self.assertEqual(self.remote_main(), out.merge)


def entry_module():
    spec = importlib.util.spec_from_file_location('kit_gate_entry_for_tests', GATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EntrySystemExit(unittest.TestCase):
    def test_a_system_exit_that_escapes_the_library_is_a_failed_gate(self):
        entry = entry_module()
        printed = []
        with mock.patch.object(entry.runner, 'branch_gate', side_effect=SystemExit(0)), \
                mock.patch.object(entry, 'Store', lambda say=None: None), \
                mock.patch('builtins.print', lambda *a, **k: printed.append(' '.join(map(str, a)))):
            code = entry.main(['-C', '.'])
        self.assertEqual(code, G.FAILED)
        self.assertTrue(printed and printed[0].startswith('GATE FAILED:'), printed)


class Measure(fixtures.GateRepoCase):
    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        self.entry = entry_module()
        self.live = os.path.join(self.tmp, 'kinds.json')
        with open(self.live, 'w', encoding='utf-8') as f:
            f.write('{"kinds": {}}\n')

    def measure(self, out):
        args = types.SimpleNamespace(out=out, days=30)
        with mock.patch.object(self.entry.policy, 'KINDS_FILE', self.live), \
                mock.patch.object(self.entry, 'Store', lambda say=None: self.store):
            return self.entry.do_measure(args)

    def test_the_live_table_under_another_name_is_refused(self):
        link = os.path.join(self.tmp, 'other-name.json')
        os.link(self.live, link)
        self.assertEqual(self.measure(link), G.REFUSED)
        with open(self.live, encoding='utf-8') as f:
            self.assertEqual(f.read(), '{"kinds": {}}\n')

    def test_a_write_that_fails_is_refused_not_a_traceback(self):
        folder = os.path.join(self.tmp, 'a-folder')
        os.makedirs(folder)
        self.assertEqual(self.measure(folder), G.REFUSED)

    def test_the_proposal_names_a_whole_number_of_cores_per_kind(self):
        # --measure passes the layout's agent_cap, so every proposed kind is an integer
        # 1..agent_cap; the live table stays as it was.
        from kitlib.lock.store import iso, utc_now
        live = json.dumps({'kinds': {
            'engine-test': {'cpu': 'medium', 'gpu': 0, 'warn_min': 30},
            'cpu-eval': {'cpu': 'heavy', 'gpu': 0, 'warn_min': 120}}})
        with open(self.live, 'w', encoding='utf-8') as f:
            f.write(live)
        now = iso(utc_now())
        for i in range(3):
            self.store.history_append({'id': 'r-%d' % i, 'kind': 'engine-test', 'how': 'done',
                                       'since': now, 'held_since': now, 'ended': now,
                                       'peak_cpu_cores': 0.9, 'cores': [0, 1, 2, 3, 4, 5]})
        out = os.path.join(self.tmp, 'proposal.json')
        self.assertEqual(self.measure(out), G.OK)
        with open(out, encoding='utf-8') as f:
            kinds = json.load(f)['kinds']
        self.assertEqual({k: e['cpu'] for k, e in kinds.items()},
                         {'engine-test': 1, 'cpu-eval': self.store_layout()['agent_cap']})
        with open(self.live, encoding='utf-8') as f:
            self.assertEqual(f.read(), live)

    def test_the_proposal_carries_the_live_tables_aliases(self):
        aliases = {'old': {'to': 'build', 'why': 'a retired name'}}
        with open(self.live, 'w', encoding='utf-8') as f:
            json.dump({'kinds': {'build': {'cpu': 'medium', 'gpu': 0, 'warn_min': 60}},
                       'aliases': aliases}, f)
        out = os.path.join(self.tmp, 'proposal.json')
        self.assertEqual(self.measure(out), G.OK)
        with open(out, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['aliases'], aliases)

    def store_layout(self):
        from kitlib.lock import layout as lay
        return lay.in_force(self.store)['layout']


if __name__ == '__main__':
    unittest.main()
