"""kitlib.gate.steps: selection by what changed, and judging a step by its log."""
import builtins
import unittest
from unittest import mock

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


class Smoke(unittest.TestCase):
    """"gate".smoke_steps: added in the default (diff) mode only, manual ones included."""
    G = gate_of(cmd('py', globs=['*.py']), cmd('boot', manual=True), cmd('proto', globs=['p/*']),
                cmd('quick', globs=['*.py'], skip_if_selected=['proto']),
                smoke_steps=['boot', 'quick'])

    def test_the_default_mode_adds_the_smoke_steps_whatever_changed(self):
        names, why = pick(self.G, ['readme.md'])
        self.assertEqual(sorted(names), ['boot', 'quick'])
        self.assertEqual(why, {'boot': 'smoke', 'quick': 'smoke'})
        names, why = pick(self.G, ['a.py'])
        self.assertEqual(sorted(names), ['boot', 'py', 'quick'])
        self.assertEqual(why['quick'], 'changed a.py')        # selected anyway: its own reason

    def test_a_smoke_step_is_never_dropped_by_skip_if_selected(self):
        names, why = pick(self.G, ['a.py', 'p/x'])
        self.assertIn('quick', names)
        self.assertEqual(why['quick'], 'smoke')

    def test_full_only_and_docs_add_no_smoke(self):
        self.assertEqual(sorted(pick(self.G, [], full=True)[0]), ['proto', 'py'])
        self.assertEqual(pick(self.G, [], only=['py'])[0], ['py'])
        g = dict(self.G, docs_only={'globs': ['*.md'], 'steps': ['proto']})
        self.assertEqual(pick(g, ['a.md'], docs=True)[0], ['proto'])

    def test_unset_changes_nothing(self):
        g = dict(self.G)
        del g['smoke_steps']
        self.assertEqual(pick(g, ['readme.md'])[0], [])

    def test_an_unknown_smoke_or_docs_step_name_refuses_in_build(self):
        for top in ({'smoke_steps': ['nosuch']},
                    {'docs_only': {'globs': ['*.md'], 'steps': ['nosuch']}}):
            with self.assertRaises(S.GateConfigError, msg=top) as cm:
                S.build(gate_of(cmd('a'), **top), ROOT, 'b', 't')
            self.assertIn('nosuch', str(cm.exception))
            self.assertNotIsInstance(cm.exception, S.UnknownStep)

    def test_a_docs_only_section_with_empty_or_missing_steps_refuses_in_build(self):
        # a docs-only change skips the tests but keeps the scrub; without this a docs-only
        # gate would run nothing and still write a record
        for spec in ({'globs': ['*.md'], 'steps': []}, {'globs': ['*.md']}):
            with self.assertRaises(S.GateConfigError, msg=spec) as cm:
                S.build(gate_of(cmd('scrub'), docs_only=spec), ROOT, 'b', 't')
            self.assertIn('"docs_only".steps is empty', str(cm.exception))
            self.assertIn('scrub step', str(cm.exception))
        S.build(gate_of(cmd('scrub'), docs_only={'globs': ['*.md'], 'steps': ['scrub']}),
                ROOT, 'b', 't')


class DocsOnly(unittest.TestCase):
    G = gate_of(cmd('scrub', globs=['*']), cmd('suite', always=True),
                docs_only={'globs': ['docs/*.md', '*.txt'], 'exclude': ['docs/ops/*'],
                           'steps': ['scrub']})

    def test_every_path_must_match_its_globs_and_none_its_exclude(self):
        self.assertTrue(S.docs_only(self.G, ['docs/a.md', 'DOCS/B.MD', 'x/y.txt']))
        self.assertFalse(S.docs_only(self.G, ['docs/a.md', 'kitlib/a.py']))
        self.assertFalse(S.docs_only(self.G, ['docs/ops/a.md']))

    def test_no_change_unset_kit_json_and_barred_paths_are_never_docs_only(self):
        self.assertFalse(S.docs_only(self.G, []))
        self.assertFalse(S.docs_only(gate_of(cmd('a')), ['docs/a.md']))
        g = dict(self.G, docs_only={'globs': ['*'], 'steps': ['scrub']})
        self.assertTrue(S.docs_only(g, ['docs/a.md']))
        self.assertFalse(S.docs_only(g, ['docs/a.md', '.kit.json']))
        self.assertFalse(S.docs_only(g, ['docs/a.md', '.KIT.JSON']))
        self.assertFalse(S.docs_only(g, ['docs/a.md', 'tools/x.py'], barred=['tools\\x.py']))

    def test_docs_mode_runs_exactly_the_docs_steps_with_setup(self):
        names, why = pick(self.G, ['docs/a.md'], docs=True)
        self.assertEqual(names, ['scrub'])
        self.assertEqual(why, {'scrub': 'docs-only'})
        g = dict(self.G, setup=['{python}', '-c', 'pass'])
        self.assertEqual(pick(g, ['docs/a.md'], docs=True)[0], ['setup', 'scrub'])


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

    def test_zero_tests_fails_a_step_whose_bare_pass_pattern_matched(self):
        # a bare pass string also matches the zero-count line, so the gate's zero_tests rule is
        # what keeps that run from passing
        st = self.step(**{'pass': 'UNIT TESTS OK'})
        g = {'zero_tests': [r'\bUNIT TESTS OK 0\b']}
        status, why = S.judge(st, 'UNIT TESTS OK 0\n', 0, g)
        self.assertEqual(status, 'FAIL')
        self.assertEqual(why, ["reported zero tests run ('UNIT TESTS OK 0')"])
        self.assertEqual(S.judge(st, 'UNIT TESTS OK 12\n', 0, g), ('PASS', []))

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


class ZeroCountPasses(unittest.TestCase):
    """zero_count_passes: the pass patterns that would also match a zero-test run."""

    def warned(self, passes, **top):
        return S.zero_count_passes(gate_of(cmd('t', **{'pass': passes}), **top))

    def test_a_bare_pass_string_warns_under_a_zero_tests_pattern_that_extends_it(self):
        (line,) = self.warned('UNIT TESTS OK', zero_tests=[r'\bUNIT TESTS OK 0\b'])
        self.assertIn("step t \"pass\" 'UNIT TESTS OK' accepts a zero-test run", line)
        self.assertIn("'UNIT TESTS OK 0'", line)

    def test_a_floored_count_does_not_warn(self):
        self.assertEqual(self.warned(r'\bUNIT TESTS OK [1-9]\d*\b',
                                     zero_tests=r'\bUNIT TESTS OK 0\b'), [])
        self.assertEqual(self.warned([r'(?m)^Ran [1-9][0-9]* tests? in', r'(?m)^OK\b']), [])

    def test_a_count_that_may_be_zero_warns_without_zero_tests(self):
        self.assertEqual(len(self.warned(r'(?m)^Ran \d+ tests? in')), 1)    # the Ran 0 tests line
        (line,) = self.warned(r'Passed: \d+')                                 # its own count
        self.assertIn("'Passed: 0'", line)

    def test_a_literal_zero_or_a_class_that_takes_letters_is_no_count(self):
        self.assertEqual(self.warned(r'\b0 failed\b'), [])
        self.assertEqual(self.warned(r'(?m)^engine_run: dir=\S'), [])     # a game repo's step
        self.assertEqual(self.warned(r'done: .+'), [])

    def test_a_bare_pass_string_with_no_zero_line_it_matches_does_not_warn(self):
        self.assertEqual(self.warned('kit_tests: OK'), [])
        self.assertEqual(self.warned('^OK$', zero_tests=r'^OK 0$'), [])

    def test_the_ran_zero_tests_line_alone_warns(self):
        # no zero_tests, and no count of its own: only the built-in Ran 0 tests line matches
        (line,) = self.warned(r'(?m)^Ran .+ in ')
        self.assertIn("'Ran 0 tests in 0.000s'", line)

    def test_a_malformed_regex_gives_no_line_and_never_raises(self):
        self.assertEqual(self.warned('('), [])                            # as a pass pattern
        self.assertEqual(self.warned('UNIT TESTS OK', zero_tests='('), [])  # as a zero_tests one

    def test_a_pattern_it_cannot_read_gives_no_line(self):
        self.assertEqual(self.warned(r'(?P<a>x)(?(a)y|z) OK'), [])
        self.assertEqual(S.zero_count_passes({}), [])

    def test_a_count_that_may_be_empty_warns(self):
        (line,) = self.warned(r'Passed: \d*')                 # matches 'Passed: 0' too
        self.assertIn("'Passed: 0'", line)
        self.assertEqual(self.warned(r'Passed: [1-9]\d*'), [])
        self.assertEqual(self.warned(r'(?m)^Ran [1-9]\d* tests? in'), [])

    def without(self, *blocked):
        """builtins.__import__ that refuses kitlib.gate.steps the modules `blocked`: a module
        name, or 're' to refuse `from re import _parser` (before Python 3.11 the stdlib regex
        parser was only sre_parse, and a later Python may drop both). Only steps' own imports:
        sre_parse itself reads re._parser on 3.11 on."""
        real = builtins.__import__

        def imp(name, globals=None, locals=None, fromlist=(), level=0):
            if (name in blocked and (globals or {}).get('__name__') == S.__name__
                    and (name != 're' or {'_parser', '_constants'} & set(fromlist or ()))):
                raise ImportError('blocked: %s' % name)
            return real(name, globals, locals, fromlist, level)
        return mock.patch.object(builtins, '__import__', imp)

    def test_the_older_parser_modules_serve_when_re_has_none(self):
        with self.without('re'):
            (line,) = self.warned(r'Passed: \d+')
        self.assertIn("'Passed: 0'", line)

    def test_no_regex_parser_gives_no_line_and_never_raises(self):
        with self.without('re', 'sre_parse', 'sre_constants'):
            self.assertEqual(self.warned('UNIT TESTS OK', zero_tests=r'\bUNIT TESTS OK 0\b'), [])
            self.assertEqual(self.warned(r'(?m)^Ran \d+ tests? in'), [])


if __name__ == '__main__':
    unittest.main()
