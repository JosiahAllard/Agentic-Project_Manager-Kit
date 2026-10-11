"""kitlib.gate.steps: selection by what changed, and judging a step by its log."""
import unittest

from kitlib.gate import steps as S

ROOT = 'C:/repo'


def gate_of(*specs, **top):
    g = {'steps': list(specs)}
    g.update(top)
    return g


def cmd(name, **kw):
    d = {'name': name, 'cmd': ['{python}', '-c', 'pass']}
    d.update(kw)
    return d


def pick(gate, changed, **kw):
    built = S.build(gate, ROOT, 'base', 'tools')
    chosen, why = S.select(gate, built, changed, **kw)
    return [s.name for s in chosen], why


class Build(unittest.TestCase):
    def test_placeholders_and_merged_args(self):
        g = gate_of(cmd('a', cmd=['{python}', '{root}/x', '{base}'], merged_args=['--merged']))
        (a,) = S.build(g, ROOT, 'abc', 'tools')
        self.assertEqual(a.cmd[1:], [ROOT + '/x', 'abc'])
        (m,) = S.build(g, ROOT, 'abc', 'tools', merged=True)
        self.assertEqual(m.cmd[-1], '--merged')

    def test_full_cmd_replaces_cmd_only_in_a_full_run(self):
        g = gate_of(cmd('t', cmd=['{python}', 'kt.py', '--select'],
                        full_cmd=['{python}', 'kt.py', '--full', '{root}'], merged_args=['--merged']))
        (a,) = S.build(g, ROOT, 'b', 't')
        self.assertEqual(a.cmd[1:], ['kt.py', '--select'])
        (m,) = S.build(g, ROOT, 'b', 't', merged=True)
        self.assertEqual(m.cmd[1:], ['kt.py', '--select', '--merged'])
        (f,) = S.build(g, ROOT, 'b', 't', full=True)
        self.assertEqual(f.cmd[1:], ['kt.py', '--full', ROOT])
        (fm,) = S.build(g, ROOT, 'b', 't', merged=True, full=True)
        self.assertEqual(fm.cmd[1:], ['kt.py', '--full', ROOT, '--merged'])
        (plain,) = S.build(gate_of(cmd('s')), ROOT, 'b', 't', full=True)     # no full_cmd: cmd
        self.assertEqual(plain.cmd[1:], ['-c', 'pass'])

    def test_daily_cmd_replaces_full_cmd_and_cmd_only_in_a_daily_run(self):
        g = gate_of(cmd('t', cmd=['{python}', 'kt.py', '--select'],
                        full_cmd=['{python}', 'kt.py', '--full'],
                        daily_cmd=['{python}', 'kt.py', '--full', '--both-tiers', '{root}']))
        (a,) = S.build(g, ROOT, 'b', 't')
        self.assertEqual(a.cmd[1:], ['kt.py', '--select'])
        (f,) = S.build(g, ROOT, 'b', 't', full=True)
        self.assertEqual(f.cmd[1:], ['kt.py', '--full'])
        (d,) = S.build(g, ROOT, 'b', 't', full=True, daily=True)
        self.assertEqual(d.cmd[1:], ['kt.py', '--full', '--both-tiers', ROOT])
        (dn,) = S.build(g, ROOT, 'b', 't', daily=True)          # daily implies the full argv chain
        self.assertEqual(dn.cmd, d.cmd)
        # No daily_cmd: full_cmd; neither: cmd.
        (df,) = S.build(gate_of(cmd('t', cmd=['{python}', 'kt.py', '--select'],
                                    full_cmd=['{python}', 'kt.py', '--full'])),
                        ROOT, 'b', 't', full=True, daily=True)
        self.assertEqual(df.cmd[1:], ['kt.py', '--full'])
        (dc,) = S.build(gate_of(cmd('s')), ROOT, 'b', 't', full=True, daily=True)
        self.assertEqual(dc.cmd[1:], ['-c', 'pass'])

    def test_reserved_names_refuse(self):
        for name in S.RESERVED:
            with self.assertRaises(S.GateConfigError):
                S.build(gate_of(cmd(name)), ROOT, 'b', 't')

    def test_unported_settings_refuse_rather_than_being_ignored(self):
        for top in ({'python_tests': 'pytests'}, {'python_tests_floor': 10}, {'trailers': True}):
            with self.assertRaises(S.GateConfigError, msg=top):
                S.build(gate_of(cmd('a'), **top), ROOT, 'b', 't')
        with self.assertRaises(S.GateConfigError):
            S.build(gate_of(cmd('a', kill_retry=True)), ROOT, 'b', 't')
        self.assertEqual(S.unsupported(gate_of(cmd('a'), python_tests='discover')), [])

    def test_the_pid_hold_flag_refuses_wherever_a_gate_could_pass_it(self):
        hold = S.PID_HOLD_FLAG
        kt = ['{python}', 'tools/kit_tests.py', '--select']
        for g in (gate_of(cmd('a', cmd=kt + [hold])),
                  gate_of(cmd('a', cmd=kt, full_cmd=kt + [hold])),
                  gate_of(cmd('a', cmd=kt, daily_cmd=kt + [hold])),
                  gate_of(cmd('a', cmd=kt, merged_args=[hold])),
                  gate_of(cmd('a'), setup=['{python}', 'tools/pytests.py', '--all', hold])):
            with self.assertRaises(S.GateConfigError, msg=g) as cm:
                S.build(g, ROOT, 'b', 't')
            self.assertIn(S.PID_HOLD_WHY, str(cm.exception))
        with self.assertRaises(S.GateConfigError) as cm:      # a landing lane's merged_args
            S.build(gate_of(cmd('a', cmd=kt)), ROOT, 'b', 't', merged=True, extra=[hold])
        self.assertIn('landing lane', str(cm.exception))
        self.assertEqual(S.unsupported(gate_of(cmd('a', cmd=kt + ['--pid-holder']))), [])


class Select(unittest.TestCase):
    G = gate_of(cmd('py', globs=['*.py'], exclude=['docs/*']),
                cmd('docs', globs=['docs/*']),
                cmd('slow', always=True, defer=True),
                cmd('lint', always=True, stage='first'),
                cmd('hand', manual=True, globs=['*']),
                cmd('quick', globs=['*.py'], skip_if_selected=['big']),
                cmd('big', globs=['big/*']))

    def test_diff_selects_by_glob_and_orders_stage_then_defer(self):
        names, why = pick(self.G, ['kitlib/A.PY'])
        self.assertEqual(names, ['lint', 'py', 'quick', 'slow'])
        self.assertEqual(why['py'], 'changed kitlib/A.PY')     # case-insensitive, * crosses /

    def test_exclude_and_manual(self):
        names, _ = pick(self.G, ['docs/x.py'])
        self.assertNotIn('py', names)
        self.assertIn('docs', names)
        self.assertNotIn('hand', names)

    def test_skip_if_selected(self):
        names, _ = pick(self.G, ['a.py', 'big/b'])
        self.assertIn('big', names)
        self.assertNotIn('quick', names)

    def test_full_runs_every_step_but_manual(self):
        names, _ = pick(self.G, [], full=True)
        self.assertEqual(sorted(names), ['big', 'docs', 'lint', 'py', 'slow'])   # quick skips for big

    def test_only_runs_exactly_the_named_steps_manual_included(self):
        names, why = pick(self.G, [], only=['hand', 'docs'])
        self.assertEqual(sorted(names), ['docs', 'hand'])
        self.assertEqual(why, {'hand': 'named', 'docs': 'named'})

    def test_only_unknown_name_is_unknown_step(self):
        with self.assertRaises(S.UnknownStep):
            pick(self.G, [], only=['nosuch'])

    def test_content_reads_the_changed_file(self):
        g = gate_of(cmd('c', content={'globs': ['*.txt'], 'regex': '^MARK$'}))
        self.assertEqual(pick(g, ['a.txt'], read=lambda p: 'x\nMARK\n')[0], ['c'])
        self.assertEqual(pick(g, ['a.txt'], read=lambda p: 'nothing')[0], [])
        self.assertEqual(pick(g, ['a.md'], read=lambda p: 'MARK')[0], [])

    def test_setup_runs_first_and_only_when_something_runs(self):
        g = gate_of(cmd('a', globs=['*.py']), setup=['{python}', '-c', 'pass'])
        self.assertEqual(pick(g, ['x.py'])[0], ['setup', 'a'])
        self.assertEqual(pick(g, ['x.md'])[0], [])


class Judge(unittest.TestCase):
    def step(self, **kw):
        spec = cmd('s', **kw)
        return S.build(gate_of(spec), ROOT, 'b', 't')[0]

    def test_pass(self):
        self.assertEqual(S.judge(self.step(), 'fine\n', 0, {}), ('PASS', []))

    def test_exit_and_ok_exits(self):
        self.assertEqual(S.judge(self.step(), 'x', 1, {})[0], 'FAIL')
        self.assertEqual(S.judge(self.step(ok_exits=[0, 1]), 'x', 1, {})[0], 'PASS')

    def test_empty_log_is_a_run_that_never_happened(self):
        status, why = S.judge(self.step(), '  \n', 0, {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('empty log', why[0])

    def test_pass_and_fail_patterns(self):
        st = self.step(**{'pass': '^OK$', 'fail': 'BOOM'})
        self.assertEqual(S.judge(st, 'OK\n', 0, {})[0], 'PASS')
        self.assertEqual(S.judge(st, 'ok\n', 0, {})[0], 'FAIL')
        self.assertEqual(S.judge(st, 'OK\nBOOM\n', 0, {})[0], 'FAIL')
        top = S.build(gate_of(cmd('s'), fail='TOPFAIL'), ROOT, 'b', 't')[0]
        self.assertEqual(S.judge(top, 'TOPFAIL\n', 0, {})[0], 'FAIL')

    def test_ran_zero_tests_fails(self):
        self.assertEqual(S.judge(self.step(), 'Ran 0 tests in 0.0s\nOK\n', 0, {})[0], 'FAIL')
        self.assertEqual(S.judge(self.step(), 'Ran 3 tests in 0.1s\nOK\n', 0, {})[0], 'PASS')

    def test_zero_tests_pattern(self):
        g = {'zero_tests': r'No test is available'}
        self.assertEqual(S.judge(self.step(), 'No test is available\n', 0, g)[0], 'FAIL')

    def test_errors_policy_windows_and_excuses(self):
        g = {'errors': {'match': 'ERROR', 'excuse': 'ERROR expected', 'expect': ['BEGIN', 'END']}}
        st = self.step()
        self.assertEqual(S.judge(st, 'ERROR x\n', 0, g)[0], 'FAIL')
        self.assertEqual(S.judge(st, 'ERROR expected\n', 0, g)[0], 'PASS')
        self.assertEqual(S.judge(st, 'BEGIN\nERROR x\nEND\n', 0, g)[0], 'PASS')
        status, why = S.judge(st, 'BEGIN\nok\n', 0, g)
        self.assertEqual(status, 'FAIL')
        self.assertIn('never closes', why[0])

    def test_warn_step_and_warn_lines(self):
        self.assertEqual(S.judge(self.step(warn=True), 'x', 1, {})[0], 'WARN')
        status, why = S.judge(self.step(), 'deprecated thing\n', 0, {'warn_lines': 'deprecated'})
        self.assertEqual(status, 'WARN')
        self.assertIn('deprecated thing', why[0])

    def test_check_step_may_have_an_empty_log(self):
        st = S.build(gate_of({'name': 'c', 'check': 'x'}), ROOT, 'b', 't')[0]
        self.assertEqual(S.judge(st, '', 0, {})[0], 'PASS')


if __name__ == '__main__':
    unittest.main()
