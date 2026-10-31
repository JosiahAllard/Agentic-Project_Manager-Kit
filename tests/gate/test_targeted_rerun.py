"""kitlib.gate.rerun: the targeted rerun of a red `gate.py --full` (docs/gate.md "Targeted
rerun"). A red --full run of a clean tree writes a red run; `gate.py --rerun` reruns its failing
test classes at the tip (each paired with a control at the base, which is information only) and
the steps it never reached, under one gate slot of the branch, and says FLAKE or REAL. A landing
takes a tip with no full gate-OK record on a FLAKE rerun of its latest red run, and never lands
a tip a test failed on when that test failed twice over the branch's commits, whatever record it
has. Every missing, unreadable or altered record refuses."""
import json
import os
import re
import unittest

from kitlib import gate as G
from kitlib.backlog import plan as pmod
from kitlib.gate import rerun, runner
from kitlib.records import gate_ok
from tests.gate import fixtures, test_land

BRANCH = test_land.BRANCH
ID = 'flaky_mod.Case.test_flaky'

# test_flaky fails when the file FLAKY_STATE names says `red`, or says `once` (it then says
# `done`, so the next run passes), or when the tree it runs in has no MARK (the base, in the
# test that commits flaky_mod.py on main). FLAKY_STATE saying `import` breaks the module's import.
FLAKY = '''import os
import unittest


def state():
    try:
        with open(os.environ['FLAKY_STATE'], encoding='utf-8') as f:
            return f.read().strip()
    except (KeyError, OSError):
        return ''


if state() == 'import':
    raise ImportError('flaky_mod is told not to import')


class Case(unittest.TestCase):
    def test_flaky(self):
        now = state()
        if now == 'once':
            with open(os.environ['FLAKY_STATE'], 'w', encoding='utf-8') as f:
                f.write('done')
        self.assertIn(now, ('', 'done'))
        self.assertTrue(os.path.exists('MARK'), 'no MARK in this tree')

    def test_steady(self):
        pass
'''
# A step that prints ok and exits 1 when the file AFTER_STATE names exists.
AFTER = ['{python}', '-c', 'import os, sys; print("ok"); '
         'p = os.environ.get("AFTER_STATE"); sys.exit(1 if p and os.path.exists(p) else 0)']
GATE = {'steps': [
    {'name': 'unit', 'always': True, 'cmd': ['{python}', '-m', 'unittest', '-v', 'flaky_mod'],
     'pass': '^OK'},
    {'name': 'after', 'always': True, 'cmd': AFTER, 'pass': '^ok$'}]}

# A repository gate's gradle step log, red on one test (2026-10-28), cut down and its worktree
# path dropped: the class is a simple name, the method a display name, and no passing test shows.
GRADLE_LOG = '''gate_tiers gradle: 31 changed path(s) against b857e2a660 (--all)
gate_tiers: --- gradle-build: gradlew.bat build --offline --max-workers=4

> Task :app-api:test

ApiLayeringTest > api.errand <-> api.movement import files stay at or under their ceilings FAILED
    org.opentest4j.AssertionFailedError at ApiLayeringTest.java:77

> Task :worker:assemble


> Task :app-api:test

6000 tests completed, 1 failed

> Task :app-api:test FAILED
> Task :worldmap:test

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':app-api:test'.
> There were failing tests. See the report at: file:///build/reports/tests/test/index.html

BUILD FAILED in 9m 27s
81 actionable tasks: 47 executed, 34 up-to-date
gate_tiers: --- gradle-build exited 1
    test tasks: 7 ran, 1 UP-TO-DATE, 1 FAILED
      FAILED     :app-api:test
gate_tiers gradle: FAILED - gradle-build: exit 1
'''
GRADLE_GATE = {'steps': [
    {'name': 'gradle', 'always': True, 'pass': '^gate_tiers gradle: OK',
     'cmd': ['{python}', '-c', 'import sys; print(open("gradle_report.txt").read()); sys.exit(1)']}]}
COVERS = 'gate.py --rerun covers kit_tests and unittest reports only, and this log is neither'


def unittest_log(*blocks, summary='FAILED (failures=1)', ran=2):
    return '\n'.join(list(blocks) + ['-' * 70, 'Ran %d tests in 0.1s' % ran, '', summary, ''])


def fail_block(kind, method, paren):
    return '\n'.join(['=' * 70, '%s: %s (%s)' % (kind, method, paren), '-' * 70,
                      'Traceback (most recent call last):', 'AssertionError: boom'])


class ParseLog(unittest.TestCase):
    """parse_log: the failing ids of a kit_tests or unittest log, with the mode each failed in;
    anything it cannot account for is uncovered."""

    def kit_tests(self):
        return '\n'.join([
            'kit_tests: --both-tiers: tier fake git (KIT_FAKE_GIT=1)',
            '==== tests.gate.test_a (3 tests, 2s, FAILED) ====',
            'test_x (tests.gate.test_a.Case.test_x) ... FAIL',
            unittest_log(fail_block('FAIL', 'test_x', 'tests.gate.test_a.Case.test_x'), ran=3),
            'kit_tests [fake git]: FAILED, exit 1 in 2s (x); failed: tests.gate.test_a (FAILED)',
            'kit_tests: tier fake git: FAILED, exit 1',
            'kit_tests: --both-tiers: tier real git (KIT_FAKE_GIT=0)',
            '==== tests.gate.test_b (1 test, 1s, FAILED) ====',
            unittest_log(fail_block('ERROR', 'test_y', 'tests.gate.test_b.Other'),
                         summary='FAILED (errors=1)', ran=1),
            'kit_tests [real git]: FAILED, exit 1 in 1s (x); failed: tests.gate.test_b (FAILED)',
            'kit_tests: tier real git: FAILED, exit 1',
            'kit_tests: FAILED, exit 1: red tier(s): fake git, real git'])

    def test_kit_tests_failures_carry_the_mode_of_their_tier(self):
        failures, uncovered = rerun.parse_log(self.kit_tests())
        self.assertEqual(uncovered, [])
        self.assertEqual(failures, [
            {'id': 'tests.gate.test_a.Case.test_x', 'cls': 'tests.gate.test_a.Case',
             'env': {'KIT_FAKE_GIT': '1'}},
            {'id': 'tests.gate.test_b.Other.test_y', 'cls': 'tests.gate.test_b.Other',
             'env': {'KIT_FAKE_GIT': '0'}}])

    def test_a_timed_out_unit_is_uncovered(self):
        text = self.kit_tests().replace('(1 test, 1s, FAILED)', '(1 test, 1s, TIMEOUT)')
        _, uncovered = rerun.parse_log(text)
        self.assertTrue(any('TIMEOUT' in u for u in uncovered), uncovered)

    def test_a_unit_listed_as_failed_with_no_report_is_uncovered(self):
        text = self.kit_tests().replace('==== tests.gate.test_b', '==== tests.gate.test_c')
        _, uncovered = rerun.parse_log(text)
        self.assertTrue(any('no report' in u for u in uncovered), uncovered)

    def test_an_interrupted_run_is_uncovered(self):
        text = self.kit_tests() + '\nkit_tests: INTERRUPTED, exit 130 in tier real git'
        _, uncovered = rerun.parse_log(text)
        self.assertTrue(any('INTERRUPTED' in u for u in uncovered), uncovered)

    def test_a_plain_unittest_log(self):
        text = unittest_log(fail_block('FAIL', 'test_x', 'pkg.mod.Case.test_x'))
        self.assertEqual(rerun.parse_log(text), (
            [{'id': 'pkg.mod.Case.test_x', 'cls': 'pkg.mod.Case', 'env': {}}], []))

    def test_a_fixture_failure_reruns_its_class(self):
        text = unittest_log(fail_block('ERROR', 'setUpClass', 'pkg.mod.Case'),
                            summary='FAILED (errors=1)')
        self.assertEqual(rerun.parse_log(text)[0], [
            {'id': 'pkg.mod.Case.setUpClass', 'cls': 'pkg.mod.Case', 'env': {}}])

    def test_lines_that_do_not_add_up_to_the_summary_are_uncovered(self):
        text = unittest_log(fail_block('FAIL', 'test_x', 'pkg.mod.Case.test_x'),
                            summary='FAILED (failures=2)')
        self.assertNotEqual(rerun.parse_log(text)[1], [])

    def test_a_test_that_could_not_load_is_uncovered(self):
        text = unittest_log(fail_block('ERROR', 'pkg', 'unittest.loader._FailedTest.pkg'),
                            summary='FAILED (errors=1)')
        failures, uncovered = rerun.parse_log(text)
        self.assertEqual(failures, [])
        self.assertTrue(any('could not be loaded' in u for u in uncovered), uncovered)

    def test_an_unexpected_success_is_uncovered(self):
        text = unittest_log(fail_block('FAIL', 'test_x', 'pkg.mod.Case.test_x'),
                            summary='FAILED (failures=1, unexpected successes=1)')
        self.assertTrue(any('unexpected' in u for u in rerun.parse_log(text)[1]))

    def test_any_other_log_is_uncovered(self):
        self.assertEqual(rerun.parse_log('lint: 3 problems\n'),
                         ([], [COVERS + ' (a gradle or dotnet log, say)']))

    def test_a_gradle_log_gives_no_failing_id_and_says_what_the_rerun_covers(self):
        failures, uncovered = rerun.parse_log(GRADLE_LOG)
        self.assertEqual(failures, [])
        self.assertEqual(len(uncovered), 1, uncovered)
        self.assertIn(COVERS, uncovered[0])

    def test_a_step_failing_on_more_than_its_exit_is_uncovered(self):
        entry = rerun.cover_step({'name': 'unit', 'status': 'FAIL', 'log': None,
                                  'reasons': ['exit 1', 'matched fail pattern']}, '')
        self.assertTrue(any('matched fail pattern' in u for u in entry['uncovered']))
        self.assertTrue(any('cannot be read' in u for u in entry['uncovered']))


class JudgeClass(unittest.TestCase):
    """judge_class: an id passes only in a whole run (exit 0, OK, at least one test ran) that
    shows it run."""

    def run_log(self, *progress, summary='OK', ran=1):
        return '\n'.join(list(progress) + ['', '-' * 70, 'Ran %d test in 0.0s' % ran, '',
                                           summary, ''])

    def test_a_whole_run_passes_the_id(self):
        text = self.run_log('test_a (m.C.test_a) ... ok')
        self.assertEqual(rerun.judge_class(text, 0, ['m.C.test_a']), (True, {'m.C.test_a': 'ok'}))

    def test_a_nonzero_exit_fails(self):
        text = self.run_log('test_a (m.C.test_a) ... ok')
        self.assertEqual(rerun.judge_class(text, 1, ['m.C.test_a']),
                         (False, {'m.C.test_a': 'run not ok'}))
        self.assertFalse(rerun.judge_class(text, None, ['m.C.test_a'])[0])

    def test_a_skipped_id_does_not_pass(self):
        text = self.run_log("test_a (m.C.test_a) ... skipped 'no'", summary='OK (skipped=1)')
        self.assertFalse(rerun.judge_class(text, 0, ['m.C.test_a'])[0])

    def test_an_id_the_run_did_not_show_does_not_pass(self):
        text = self.run_log('test_b (m.C.test_b) ... ok')
        self.assertEqual(rerun.judge_class(text, 0, ['m.C.test_a']),
                         (False, {'m.C.test_a': 'not run'}))

    def test_a_run_of_no_test_does_not_pass(self):
        self.assertFalse(rerun.judge_class(self.run_log(ran=0), 0, ['m.C.test_a'])[0])

    def test_a_docstring_line_carries_the_status(self):
        text = self.run_log('test_a (m.C.test_a)', 'What it checks. ... ok')
        self.assertTrue(rerun.judge_class(text, 0, ['m.C.test_a'])[0])

    def test_a_fixture_passes_when_a_test_of_its_class_passes(self):
        text = self.run_log('test_a (m.C.test_a) ... ok')
        self.assertTrue(rerun.judge_class(text, 0, ['m.C.setUpClass'])[0])
        self.assertFalse(rerun.judge_class(text, 0, ['m.D.setUpClass'])[0])


class AddLine(unittest.TestCase):
    def test_the_summary_stays_a_title_however_many_long_tests_failed(self):
        long = 'tests.gate.test_' + 'x' * 70
        classes = [{'cls': '%s.Case%d%s' % (long, n, 'Y' * 80),
                    'ids': ['%s.Case%d.test_%s' % (long, n, 'z' * 50)]} for n in range(5)]
        add = rerun.add_line(os.getcwd(), BRANCH, 'a' * 40, classes)
        summary = re.search(r'--summary "([^"]*)"', add).group(1)
        self.assertLessEqual(len(summary), pmod.SUMMARY_MAX, summary)
        self.assertIn('and 4 more', summary)
        why = re.search(r'--why-not-folded "([^"]*)"', add).group(1)
        for c in classes:
            self.assertIn(c['ids'][0], why)


class RerunCase(test_land.LandCase):
    GATE = GATE
    GATE_TIPS = False       # every tip here is gated, or rerun, by the test itself

    def setUp(self):
        test_land.LandCase.setUp(self)
        self.state = os.path.join(self.tmp, 'flaky-state')
        self.after = os.path.join(self.tmp, 'after-red')
        self.env['FLAKY_STATE'] = self.state        # the slot's environment (book_kw env)
        self.env['AFTER_STATE'] = self.after

    def set_state(self, text):
        with open(self.state, 'w', encoding='utf-8') as f:
            f.write(text)

    def work(self, state):
        self.set_state(state)
        return self.branch('feature', {'flaky_mod.py': FLAKY, 'MARK': 'here\n'})

    def red(self, wt):
        out = runner.branch_gate(wt, full=True, book_kw=self.book_kw,
                                 log_dir=os.path.join(self.tmp, 'own'))
        self.assertEqual(out.code, G.FAILED, out.lines)
        return out

    def rerun(self, wt):
        lines = []
        out = rerun.rerun_gate(wt, log_dir=os.path.join(self.tmp, 'rerun'), say=lines.append,
                               book_kw=self.book_kw)
        out.said = lines
        return out

    def loaded(self, tip):
        return rerun.load(self.repo.root, tip)


class RedRun(RerunCase):
    def test_a_red_full_run_records_its_failing_ids_and_the_steps_it_never_reached(self):
        wt, tip = self.work('red')
        out = self.red(wt)
        self.assertIsNone(gate_ok.read(self.repo.root, tip)[0], out.lines)
        reds, reruns = self.loaded(tip)
        self.assertEqual(len(reds), 1)
        self.assertEqual(reruns, [])
        red = reds[0]
        self.assertEqual((red['commit'], red['config_rev'], red['full']), (tip, tip, True))
        self.assertEqual(red['base'], self.main)
        steps = {s['name']: s for s in red['steps']}
        self.assertEqual(steps['unit']['status'], 'FAIL')
        self.assertEqual([f['id'] for f in steps['unit']['failures']], [ID])
        self.assertEqual(steps['unit']['uncovered'], [])
        self.assertEqual(steps['after']['status'], 'NOT RUN')

    def test_an_only_run_writes_no_red_run(self):
        wt, tip = self.work('red')
        out = runner.branch_gate(wt, only=['unit'], book_kw=self.book_kw,
                                 log_dir=os.path.join(self.tmp, 'own'))
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.loaded(tip), ([], []))

    def test_a_default_mode_run_writes_no_red_run(self):
        wt, tip = self.work('red')
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=os.path.join(self.tmp, 'own'))
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.loaded(tip), ([], []))

    def test_a_dirty_tree_writes_no_red_run(self):
        wt, tip = self.work('red')
        self.repo.write('loose.txt', 'x\n', root=wt)
        lines = []
        out = runner.branch_gate(wt, full=True, book_kw=self.book_kw, say=lines.append,
                                 log_dir=os.path.join(self.tmp, 'own'))
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.loaded(tip), ([], []))
        self.assertTrue(any('no red-run record' in s for s in lines), lines)

    def test_a_rerun_with_no_red_run_is_refused(self):
        wt, tip = self.work('done')
        out = self.rerun(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('no red gate.py --full run', out.lines[0])
        self.assertEqual(self.book_calls, [])


class Flake(RerunCase):
    def test_a_flake_passes_its_rerun_and_the_tip_lands_on_it(self):
        wt, tip = self.work('once')
        self.red(wt)
        n = len(self.book_calls)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.OK, out.lines + out.said)
        self.assertTrue(out.lines[0].startswith('RERUN FLAKE'), out.lines)
        add = out.lines[-1].strip()
        # the id it proposes is FLAKE- and the failing test's class name
        self.assertIn(' add FLAKE-%s ' % ID.split('.')[-2].upper(), add)
        self.assertIn('--scope flaky_mod.py', add)
        self.assertIn('--found-on %s' % BRANCH, add)
        # the summary is a title `backlog.py add` takes; the test id goes in the reason
        summary = re.search(r'--summary "([^"]*)"', add).group(1)
        self.assertLessEqual(len(summary), pmod.SUMMARY_MAX, summary)
        self.assertIn(ID, re.search(r'--why-not-folded "([^"]*)"', add).group(1))
        self.assertIn('report this line to your coordinator', out.lines[-2])
        # one gate slot of the branch: it counts toward the branch gate budget
        calls = self.book_calls[n:]
        self.assertEqual(len(calls), 1, calls)
        self.assertIs(calls[0]['gate'], True)
        self.assertEqual(calls[0]['branch'], BRANCH)
        reds, reruns = self.loaded(tip)
        self.assertEqual([r['verdict'] for r in reruns], ['FLAKE'])
        self.assertEqual(reruns[0]['red'], reds[0]['hash'])
        self.assertEqual(reruns[0]['classes'][0]['control'], 'absent')
        self.assertEqual([s['name'] for s in reruns[0]['steps'] if s['name'] != 'windows'],
                         ['after'])
        self.assertIsNone(rerun.verify_flake(self.repo.root, tip))
        self.assertIsNone(rerun.twice_refusal(self.repo.root, tip, self.main))
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_a_red_run_gets_one_rerun(self):
        wt, tip = self.work('once')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.OK)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('rerun already', out.lines[0])

    def test_a_control_red_at_the_base_is_said_and_never_the_verdict(self):
        self.repo.write('flaky_mod.py', FLAKY)          # on main, with no MARK
        self.main = self.repo.commit('flaky on main', 'flaky_mod.py')
        self.repo.git('push', '-q', 'origin', 'main')
        wt, tip = self.work('once')
        self.red(wt)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.OK, out.lines + out.said)
        self.assertTrue(any(line.startswith('also red at base') for line in out.lines),
                        out.lines)
        self.assertTrue(any('(also red at base)' in s for s in out.said), out.said)
        self.assertEqual(self.loaded(tip)[1][0]['classes'][0]['control'], 'red')

    def test_a_tampered_record_refuses_the_landing(self):
        wt, tip = self.work('once')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.OK)
        folder = rerun.commit_dir(self.repo.root, tip)
        name = [n for n in os.listdir(folder) if n.startswith('rerun-')][0]
        path = os.path.join(folder, name)
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        rec['classes'][0]['ids'][ID] = 'ok'
        rec['verdict'] = 'FLAKE'
        rec['log_dir'] = 'elsewhere'                    # any change breaks the hash
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        self.assertIn('does not match its hash', rerun.verify_flake(self.repo.root, tip))
        self.assertIn('cannot be read', rerun.twice_refusal(self.repo.root, tip, self.main))
        fixtures.gate_full(self.repo.root, tip)         # even a full gate-OK record
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('%s tip %s does not land' % (BRANCH, tip[:10]), out.lines[0])
        self.assertEqual(self.main_now(), self.main)

    def test_a_stray_file_in_the_record_folder_refuses(self):
        wt, tip = self.work('once')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.OK)
        with open(os.path.join(rerun.commit_dir(self.repo.root, tip), 'red-1-0000000g.json'),
                  'w', encoding='utf-8') as f:
            f.write('{}')
        self.assertIsNotNone(rerun.verify_flake(self.repo.root, tip))
        self.assertIsNotNone(rerun.twice_refusal(self.repo.root, tip, self.main))


class Real(RerunCase):
    def assert_never_lands(self, tip):
        self.assertIsNotNone(rerun.verify_flake(self.repo.root, tip))
        why = rerun.twice_refusal(self.repo.root, tip, self.main)
        self.assertIn('%s failed on this tip and failed twice on its branch' % ID, why)
        fixtures.gate_full(self.repo.root, tip)         # a full gate-OK record does not help
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('%s tip %s does not land' % (BRANCH, tip[:10]), out.lines[0])
        self.assertIn('failed twice', out.lines[0])
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)

    def test_a_test_that_fails_its_rerun_is_real_and_never_lands(self):
        wt, tip = self.work('red')
        self.red(wt)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[0].startswith('RERUN REAL'), out.lines)
        self.assertNotIn('add FLAKE', '\n'.join(out.lines))
        self.assertEqual([r['verdict'] for r in self.loaded(tip)[1]], ['REAL'])
        self.assert_never_lands(tip)

    def test_two_red_runs_of_one_test_are_real_and_never_land(self):
        wt, tip = self.work('red')
        self.red(wt)
        self.red(wt)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[0].startswith('RERUN REAL: %s failed on this tip' % ID),
                        out.lines)
        self.assert_never_lands(tip)

    def test_a_step_the_red_run_never_reached_failing_is_real(self):
        wt, tip = self.work('once')
        self.red(wt)
        with open(self.after, 'w', encoding='utf-8') as f:
            f.write('red')
        out = self.rerun(wt)
        self.assertEqual(out.code, G.FAILED, out.lines + out.said)
        self.assertTrue(out.lines[0].startswith('RERUN REAL'), out.lines)
        self.assertIn('said REAL', rerun.verify_flake(self.repo.root, tip))
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('its targeted rerun does not land it', out.lines[0])

    def test_an_uncovered_red_run_is_not_rerun(self):
        wt, tip = self.work('import')
        self.red(wt)
        red = self.loaded(tip)[0][0]
        self.assertNotEqual(rerun.uncovered_of(red), [])
        n = len(self.book_calls)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('uncovered', out.lines[0])
        self.assertEqual(self.book_calls[n:], [])
        self.assertIn('uncovered', rerun.verify_flake(self.repo.root, tip))

    def test_a_dirty_tree_is_not_rerun(self):
        wt, tip = self.work('once')
        self.red(wt)
        self.repo.write('loose.txt', 'x\n', root=wt)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('not clean', out.lines[0])
        self.assertEqual(self.loaded(tip)[1], [])


class GradleStep(RerunCase):
    GATE = GRADLE_GATE

    def test_a_red_gradle_step_is_not_rerun_and_the_refusal_says_what_is_covered(self):
        wt, tip = self.branch('feature', {'gradle_report.txt': GRADLE_LOG})
        self.red(wt)
        step = self.loaded(tip)[0][0]['steps'][0]
        self.assertEqual((step['name'], step['failures']), ('gradle', []))
        n = len(self.book_calls)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('only a passing gate.py --full lands this tip', out.lines[0])
        self.assertIn('  gradle: ' + COVERS, '\n'.join(out.lines))
        self.assertEqual(self.book_calls[n:], [])
        self.assertIn(COVERS, rerun.verify_flake(self.repo.root, tip))


class Branch(RerunCase):
    """The twice rule counts a test's failures over every commit of the branch since its merge
    base, for a tip that has a red run of its own; a tip without one (a green --full) lands."""

    def next_commit(self, wt, state):
        self.set_state(state)
        self.repo.write('more.txt', 'more\n', root=wt)
        return self.repo.commit('more work', 'more.txt', cwd=wt)

    def assert_real_on_the_tip(self, wt, tip):
        n = len(self.book_calls)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.FAILED, out.lines + out.said)
        self.assertTrue(out.lines[0].startswith(
            'RERUN REAL: %s failed on this tip and failed twice on its branch' % ID), out.lines)
        self.assertEqual(self.book_calls[n:], [])       # nothing was rerun or booked
        fixtures.gate_full(self.repo.root, tip)         # a full gate-OK record does not help
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('%s tip %s does not land' % (BRANCH, tip[:10]), out.lines[0])
        self.assertIn('failed twice on its branch', out.lines[0])
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)

    def test_a_test_real_on_an_earlier_commit_that_fails_once_on_the_tip_never_lands(self):
        wt, _ = self.work('red')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.FAILED)  # REAL on the first commit
        tip = self.next_commit(wt, 'once')              # on the tip it fails once, then passes
        self.red(wt)
        self.assert_real_on_the_tip(wt, tip)

    def test_a_flake_on_an_earlier_commit_that_fails_again_on_the_tip_never_lands(self):
        wt, _ = self.work('once')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.OK)      # FLAKE on the first commit
        tip = self.next_commit(wt, 'once')
        self.red(wt)
        self.assert_real_on_the_tip(wt, tip)

    def test_a_green_full_run_of_a_fix_after_a_real_failure_lands(self):
        wt, _ = self.work('red')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.FAILED)
        tip = self.next_commit(wt, 'done')
        out = runner.branch_gate(wt, full=True, book_kw=self.book_kw,
                                 log_dir=os.path.join(self.tmp, 'own'))
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.loaded(tip), ([], []))
        self.assertIsNone(rerun.twice_refusal(self.repo.root, tip, self.main))
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_only_a_test_that_failed_on_the_tip_counts(self):
        wt, _ = self.work('red')
        self.red(wt)
        self.assertEqual(self.rerun(wt).code, G.FAILED)  # ID failed twice on the first commit
        tip = self.next_commit(wt, 'done')
        other = {'id': 'other_mod.Case.test_x', 'cls': 'other_mod.Case', 'env': {}}
        rerun._write(self.repo.root, {'kind': rerun.RED, 'commit': tip, 'steps': [
            {'name': 'unit', 'status': 'FAIL', 'failures': [other]}]})
        reds, reruns = self.loaded(tip)
        self.assertEqual(rerun.branch_twice(self.repo.root, tip, self.main, reds, reruns),
                         ([], None))
        self.assertIsNone(rerun.twice_refusal(self.repo.root, tip, self.main))
        rerun._write(self.repo.root, {'kind': rerun.RED, 'commit': tip, 'steps': [
            {'name': 'unit', 'status': 'FAIL', 'failures': [dict(other, id=ID)]}]})
        reds, reruns = self.loaded(tip)
        self.assertEqual(rerun.branch_twice(self.repo.root, tip, self.main, reds, reruns),
                         ([ID], None))

    def test_an_unreadable_record_of_an_earlier_commit_refuses(self):
        wt, first = self.work('once')
        self.red(wt)
        tip = self.next_commit(wt, 'red')
        self.red(wt)
        with open(os.path.join(rerun.commit_dir(self.repo.root, first), 'red-1-0000000g.json'),
                  'w', encoding='utf-8') as f:
            f.write('{}')
        why = rerun.twice_refusal(self.repo.root, tip, self.main)
        self.assertIn('the branch commit %s cannot be read' % first[:10], why)
        out = self.rerun(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('the branch commit %s cannot be read' % first[:10], out.lines[0])

    def test_an_unreadable_branch_record_refuses_a_tip_with_no_failure_of_its_own(self):
        wt, first = self.work('done')
        tip = self.next_commit(wt, 'done')
        rerun._write(self.repo.root, {'kind': rerun.RED, 'commit': tip, 'steps': [
            {'name': 'unit', 'status': 'FAIL', 'failures': []}]})
        reds, reruns = self.loaded(tip)
        self.assertEqual(rerun.failure_counts(reds, reruns), {})
        self.assertEqual(rerun.branch_twice(self.repo.root, tip, self.main, reds, reruns),
                         ([], None))
        os.makedirs(rerun.commit_dir(self.repo.root, first), exist_ok=True)
        with open(os.path.join(rerun.commit_dir(self.repo.root, first), 'red-1-0000000g.json'),
                  'w', encoding='utf-8') as f:
            f.write('{}')
        ids, why = rerun.branch_twice(self.repo.root, tip, self.main, reds, reruns)
        self.assertIsNone(ids)
        self.assertIn('the branch commit %s cannot be read' % first[:10], why)
        self.assertIn('the branch commit %s cannot be read' % first[:10],
                      rerun.twice_refusal(self.repo.root, tip, self.main))
