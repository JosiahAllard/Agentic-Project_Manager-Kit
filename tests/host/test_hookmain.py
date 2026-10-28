"""kitlib.host.hookmain: the one hook runner, over a fake adapter and a fake guard decide.

What every host's hook gets from it: parse -> decide -> degrade -> render, a guard-error deny for
any failure on the way (the role tag read from the payload even when parse refuses it), a
deadline that denies rather than falls silent, and the one rendered hook command line."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.host import Action, Decision, HostCapabilities, hookmain
from tests.platforms import windows_only


def caps(supports_ask):
    return HostCapabilities(('PreToolUse',), supports_ask, True, True, True, 'x')


class FakeAdapter(object):
    """Payload {"kind": ..., "who": ...}; render prints `<verdict> <rule>` and exits 3 for a deny."""
    name = 'fake'
    profile = object()

    def __init__(self, supports_ask=True, parse_error=None):
        self.capabilities = caps(supports_ask)
        self.parse_error = parse_error

    def parse(self, event_name, payload):
        if self.parse_error:
            raise self.parse_error
        return Action(payload.get('kind', 'other'), agent=payload.get('who'))

    def agent(self, payload):
        return (payload or {}).get('who')

    def render(self, event_name, d):
        self.last = d
        return ('' if d.verdict == 'allow' else '%s %s\n' % (d.verdict, d.rule)), (3 if d.verdict == 'deny' else 0)


def run(adapter, payload, decide, **kw):
    out = io.StringIO()
    with mock.patch('kitlib.guard.decide', decide):
        code = hookmain.run(adapter, 'PreToolUse', io.StringIO(json.dumps(payload)), out,
                            environ={}, entry='C:/kit/hook.py', **kw)
    return code, out.getvalue()


class Run(unittest.TestCase):
    def test_the_verdict_is_rendered_by_the_adapter(self):
        a = FakeAdapter()
        self.assertEqual(run(a, {'kind': 'shell'}, lambda *x, **k: Decision('allow')), (0, ''))
        self.assertEqual(run(a, {'kind': 'shell'}, lambda *x, **k: Decision('deny', 'r', 'why')), (3, 'deny r\n'))

    def test_decide_gets_the_action_the_environment_and_the_adapters_profile(self):
        a, seen = FakeAdapter(), {}

        def decide(action, ctx=None, environ=None, profile=None):
            seen.update(action=action, environ=environ, profile=profile())
            return Decision('allow')
        run(a, {'kind': 'edit', 'who': 'a1'}, decide)
        self.assertEqual(seen['action'], Action('edit', agent='a1'))
        self.assertEqual((seen['environ'], seen['profile']), ({}, a.profile))

    def test_an_ask_is_lowered_only_on_a_host_without_ask(self):
        ask = lambda *x, **k: Decision('ask', 'r', 'why', 'cmd')       # noqa: E731
        self.assertEqual(run(FakeAdapter(True), {}, ask), (0, 'ask r\n'))
        a = FakeAdapter(False)
        self.assertEqual(run(a, {}, ask), (3, 'deny r\n'))
        self.assertEqual(a.last, Decision('deny', 'r', 'why', 'cmd'))

    def test_any_failure_denies_with_the_error_and_the_rerun_command(self):
        def boom(*x, **k):
            raise RuntimeError('boom')
        for payload, role in (({'kind': 'mcp'}, 'main'), ({'kind': 'edit', 'who': 'a1'}, 'subagent')):
            a = FakeAdapter()
            self.assertEqual(run(a, payload, boom), (3, 'deny guard-error\n'))
            self.assertIn('(RuntimeError: boom)', a.last.reason)
            self.assertTrue(a.last.reason.endswith(
                'Instead: python "C:/kit/hook.py" < event.json [kit guard: rule=guard-error role=%s]' % role))
            self.assertEqual(a.last.instead, 'python "C:/kit/hook.py" < event.json')

    def test_a_payload_parse_refuses_still_carries_its_role(self):
        a = FakeAdapter(parse_error=ValueError('bad shape'))
        self.assertEqual(run(a, {'who': 'a1'}, lambda *x, **k: Decision('allow')), (3, 'deny guard-error\n'))
        self.assertIn('ValueError: bad shape', a.last.reason)
        self.assertTrue(a.last.reason.endswith('role=subagent]'))

    def test_unreadable_json_denies(self):
        a, out = FakeAdapter(), io.StringIO()
        self.assertEqual(hookmain.run(a, 'PreToolUse', io.StringIO('{nope'), out, environ={}, entry='h.py'), 3)
        self.assertTrue(a.last.reason.endswith('role=main]'))

    def test_a_guard_still_deciding_at_the_deadline_denies(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(*x, **k):
            gate.wait(5)
            return Decision('allow')
        a = FakeAdapter()
        self.assertEqual(run(a, {'who': 'a1'}, slow, deadline=0.05), (3, 'deny guard-timeout\n'))
        self.assertTrue(a.last.reason.endswith('[kit guard: rule=guard-timeout role=subagent]'))
        # a decide inside the deadline is answered as usual, its errors as guard-error
        self.assertEqual(run(a, {}, lambda *x, **k: Decision('allow'), deadline=5), (0, ''))

        def boom(*x, **k):
            raise KeyError('k')
        self.assertEqual(run(a, {}, boom, deadline=5), (3, 'deny guard-error\n'))

    def test_a_write_that_fails_is_raised_to_the_entry(self):
        class Closed(io.StringIO):
            def write(self, text):
                raise OSError('no stdout')
        with mock.patch('kitlib.guard.decide', lambda *x, **k: Decision('deny', 'r', 'why')):
            with self.assertRaises(OSError):
                hookmain.run(FakeAdapter(), 'PreToolUse', io.StringIO('{}'), Closed(), environ={}, entry='h')


class Unready(unittest.TestCase):
    """On a platform kitlib.guard does not declare ready (READY_PLATFORMS), every guarded call is
    the guard-error deny and decide never runs (docs/linux-support-plan.md section 3). Linux is
    ready since slice 8 (tests/guard/test_linux_paths.py), so a platform with no backend stands in."""

    def on(self, name):
        patch = mock.patch('kitlib.platform.NAME', name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_on_an_unready_platform_every_guarded_call_is_denied_before_decide(self):
        from kitlib import guard
        self.assertNotIn('unsupported', guard.READY_PLATFORMS)
        self.on('unsupported')
        called = []

        def decide(*x, **k):
            called.append(x)
            return Decision('allow')
        for payload, role in (({'kind': 'shell'}, 'main'), ({'kind': 'edit', 'who': 'a1'}, 'subagent')):
            a = FakeAdapter()
            self.assertEqual(run(a, payload, decide), (3, 'deny guard-error\n'))
            self.assertIn('PlatformUnsupported', a.last.reason)
            self.assertIn('not declared ready on this platform (unsupported; ready: windows, linux)', a.last.reason)
            self.assertTrue(a.last.reason.endswith('role=%s]' % role))
        self.assertEqual(called, [])

    def test_on_linux_the_guard_decides(self):
        self.on('linux')
        self.assertEqual(run(FakeAdapter(), {}, lambda *x, **k: Decision('allow')), (0, ''))

    def test_on_windows_the_guard_decides_as_before(self):
        self.on('windows')
        self.assertEqual(run(FakeAdapter(), {}, lambda *x, **k: Decision('allow')), (0, ''))


class Several(unittest.TestCase):
    """A call parsed into several Actions (one patch over several files): each is judged."""

    class Multi(FakeAdapter):
        def __init__(self, paths, screen=None, **kw):
            super().__init__(**kw)
            self.paths, self._screen = paths, screen

        def parse(self, event_name, payload):
            return tuple(Action('edit', path=p) for p in self.paths)

        def screen(self, action, environ):
            return self._screen(action) if self._screen else None

    def by_path(self, verdicts):
        seen = []

        def decide(action, ctx=None, environ=None, profile=None):
            seen.append(action.path)
            v = verdicts.get(action.path, 'allow')
            return Decision(v, 'r-' + action.path, 'about ' + action.path)
        return decide, seen

    def test_every_action_is_judged_and_all_allowed_allows(self):
        decide, seen = self.by_path({})
        self.assertEqual(run(self.Multi(('a', 'b', 'c')), {}, decide), (0, ''))
        self.assertEqual(seen, ['a', 'b', 'c'])

    def test_any_deny_denies_and_stops(self):
        decide, seen = self.by_path({'b': 'deny', 'c': 'deny'})
        a = self.Multi(('a', 'b', 'c'))
        self.assertEqual(run(a, {}, decide), (3, 'deny r-b\n'))
        self.assertEqual(seen, ['a', 'b'])

    def test_a_deny_outranks_an_earlier_ask(self):
        decide, _ = self.by_path({'a': 'ask', 'b': 'deny'})
        self.assertEqual(run(self.Multi(('a', 'b')), {}, decide), (3, 'deny r-b\n'))
        decide, _ = self.by_path({'b': 'ask'})
        self.assertEqual(run(self.Multi(('a', 'b')), {}, decide), (0, 'ask r-b\n'))
        self.assertEqual(run(self.Multi(('a', 'b'), supports_ask=False), {}, decide), (3, 'deny r-b\n'))

    def test_no_action_is_refused(self):
        a = self.Multi(())
        self.assertEqual(run(a, {}, lambda *x, **k: Decision('allow')), (3, 'deny guard-error\n'))
        self.assertIn('no action', a.last.reason)

    def test_the_hosts_screen_judges_before_the_guard(self):
        decide, seen = self.by_path({})
        a = self.Multi(('a', 'b'), screen=lambda act: Decision('deny', 'host-rule', 'no') if act.path == 'b' else None)
        self.assertEqual(run(a, {}, decide), (3, 'deny host-rule\n'))
        self.assertEqual(seen, ['a'], 'a screened deny never reaches the guard')
        allow = self.Multi(('a',), screen=lambda act: Decision('allow'))
        self.assertEqual(run(allow, {}, decide), (0, ''))
        self.assertEqual(seen, ['a', 'a'], 'a screen allow still meets the guard')


class NoIdentity(unittest.TestCase):
    """A host whose hooks carry no subagent identity: a marked session's call is judged as an
    agent of its mark's role; the owner's unmarked session and a host with identity are not."""

    def adapter(self, identity):
        a = FakeAdapter()
        a.capabilities = HostCapabilities(('PreToolUse',), True, identity, True, True, 'x')
        return a

    def seen(self, adapter, payload, environ):
        got = []

        def decide(action, ctx=None, environ=None, profile=None):
            got.append(action)
            return Decision('allow')
        with mock.patch('kitlib.guard.decide', decide):
            hookmain.run(adapter, 'PreToolUse', io.StringIO(json.dumps(payload)), io.StringIO(),
                         environ=environ, entry='h.py')
        return got[0]

    def test_a_marked_call_is_an_unidentified_agent_of_its_role(self):
        for kind in ('edit', 'shell', 'mcp'):
            a = self.seen(self.adapter(False), {'kind': kind}, {'PM_KIT_ROLE': 'worker'})
            self.assertEqual((a.agent, a.agent_role), (hookmain.UNIDENTIFIED, 'worker'), kind)

    def test_the_owner_another_kind_and_a_host_with_identity_are_unchanged(self):
        self.assertIsNone(self.seen(self.adapter(False), {'kind': 'edit'}, {}).agent)
        self.assertIsNone(self.seen(self.adapter(False), {'kind': 'other'}, {'PM_KIT_ROLE': 'pm'}).agent)
        self.assertIsNone(self.seen(self.adapter(True), {'kind': 'edit'}, {'PM_KIT_ROLE': 'pm'}).agent)
        self.assertEqual(self.seen(self.adapter(True), {'kind': 'edit', 'who': 'a1'}, {}).agent, 'a1')

    def test_the_deadline_is_one_budget_for_every_action(self):
        class Many(FakeAdapter):
            def parse(self, event_name, payload):
                return tuple(Action('edit', path=str(i)) for i in range(5))
        budgets = []

        def fake(guard, action, environ, profile, deadline):
            budgets.append(deadline)
            time.sleep(0.03)
            return Decision('allow')
        with mock.patch.object(hookmain, '_decide', fake):
            code = hookmain.run(Many(), 'PreToolUse', io.StringIO('{}'), io.StringIO(), environ={},
                                entry='h.py', deadline=1.0)
        self.assertEqual(code, 0)
        self.assertTrue(all(b < a for a, b in zip(budgets, budgets[1:])), budgets)
        self.assertLessEqual(budgets[0], 1.0)

    def test_a_budget_started_before_the_hook_counts_that_time_and_is_named_whole(self):
        """`started` (a time.monotonic() reading the entry took before its own work, a boot) starts
        the budget early; the timeout reason names the whole budget, not what was left of it."""
        budgets = []

        def fake(guard, action, environ, profile, deadline):
            budgets.append(deadline)
            return None                                 # still deciding
        a = FakeAdapter()
        with mock.patch.object(hookmain, '_decide', fake):
            code = hookmain.run(a, 'PreToolUse', io.StringIO('{}'), io.StringIO(), environ={}, entry='h.py',
                                deadline=7, started=time.monotonic() - 3)
        self.assertEqual(code, 3)
        self.assertLessEqual(budgets[0], 4.0)
        self.assertGreater(budgets[0], 3.5)
        self.assertIn('within 7 s', a.last.reason)


class RefusalLog(unittest.TestCase):
    """Every deny run() answers is logged after the answer is written: one line in
    <pm_data>/audit/refusals.jsonl (kitlib.guard.refusal_log; docs/guard-v2.md "Refusal log"),
    whatever made it (the guard, a guard error, the deadline, the host's screen). Nothing else is
    logged, and the log never changes the answer or the exit code."""

    def setUp(self):
        from tests.gitfixture import rmtree
        from tests.home import machine_config, temp_home
        home = temp_home(self)
        self.data = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(rmtree, self.data)
        machine_config(home, {'pm_data': {'path': self.data}})
        self.log = os.path.join(self.data, 'audit', 'refusals.jsonl')

    def rows(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as fh:
            return [json.loads(line) for line in fh]

    def test_a_deny_writes_one_line_with_its_rule_and_role(self):
        deny = lambda *x, **k: Decision('deny', 'some-rule', 'why: rm -rf C:/x', 'do this')   # noqa: E731
        self.assertEqual(run(FakeAdapter(), {'kind': 'shell', 'who': 'a1'}, deny), (3, 'deny some-rule\n'))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['host'], rows[0]['rule'], rows[0]['role'], rows[0]['agent'], rows[0]['kind'],
                          rows[0]['instead_given']), ('fake', 'some-rule', 'subagent', 'a1', 'shell', True))
        self.assertNotIn('rm -rf', json.dumps(rows[0]))

    def test_allow_ask_and_a_warning_write_nothing(self):
        for d in (Decision('allow'), Decision('ask', 'r', 'why', 'cmd'), Decision('allow', 'w', 'a warning')):
            with self.subTest(d=d):
                run(FakeAdapter(), {'kind': 'shell'}, lambda *x, **k: d)
        self.assertEqual(self.rows(), [])

    def test_a_guard_error_and_a_timeout_are_logged(self):
        def boom(*x, **k):
            raise RuntimeError('boom')
        self.assertEqual(run(FakeAdapter(), {'kind': 'mcp'}, boom), (3, 'deny guard-error\n'))
        self.assertEqual(run(FakeAdapter(parse_error=ValueError('bad')), {}, boom), (3, 'deny guard-error\n'))
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(*x, **k):
            gate.wait(5)
            return Decision('allow')
        self.assertEqual(run(FakeAdapter(), {'who': 'a1'}, slow, deadline=0.05), (3, 'deny guard-timeout\n'))
        self.assertEqual([(r['rule'], r['role']) for r in self.rows()],
                         [('guard-error', 'main'), ('guard-error', 'main'), ('guard-timeout', 'subagent')])

    def test_a_host_screen_deny_is_logged_once_for_a_call_of_several_actions(self):
        a = Several.Multi(('a', 'b', 'c'), screen=lambda act: Decision('deny', 'host-rule', 'no') if act.path != 'a' else None)
        self.assertEqual(run(a, {}, lambda *x, **k: Decision('allow')), (3, 'deny host-rule\n'))
        self.assertEqual([(r['rule'], r['kind']) for r in self.rows()], [('host-rule', 'edit')])

    def test_a_log_that_fails_leaves_the_answer_unchanged(self):
        deny = lambda *x, **k: Decision('deny', 'r', 'why')                            # noqa: E731
        with mock.patch('kitlib.guard.refusal_log.append', side_effect=OSError('disk full')):
            self.assertEqual(run(FakeAdapter(), {}, deny), (3, 'deny r\n'))
        with mock.patch('kitlib.guard.refusal_log.record', side_effect=RuntimeError('broken')):
            self.assertEqual(run(FakeAdapter(), {}, deny), (3, 'deny r\n'))
        self.assertEqual(self.rows(), [])

    def test_the_line_is_written_after_the_answer(self):
        order = []

        class Out(io.StringIO):
            def write(self, text):
                order.append('answer')
                return super().write(text)
        with mock.patch('kitlib.guard.decide', lambda *x, **k: Decision('deny', 'r', 'why')), \
                mock.patch('kitlib.guard.refusal_log.record', side_effect=lambda *a, **k: order.append('log')):
            hookmain.run(FakeAdapter(), 'PreToolUse', io.StringIO('{}'), Out(), environ={}, entry='h')
        self.assertEqual(order, ['answer', 'log'])

    def test_a_marked_call_on_a_host_without_identity_logs_the_role_the_guard_judged(self):
        a = FakeAdapter()
        a.capabilities = HostCapabilities(('PreToolUse',), True, False, True, True, 'x')
        tags = []

        def deny(action, ctx=None, environ=None, profile=None):
            role = 'subagent' if action.agent else 'main'
            tags.append(role)
            return Decision('deny', 'r', 'why [kit guard: rule=r role=%s]' % role)
        with mock.patch('kitlib.guard.decide', deny):
            code = hookmain.run(a, 'PreToolUse', io.StringIO('{"kind": "edit"}'), io.StringIO(),
                                environ={'PM_KIT_ROLE': 'worker'}, entry='h.py')
        self.assertEqual((code, tags), (3, ['subagent']))
        self.assertEqual([(r['role'], r['agent'], r['agent_role']) for r in self.rows()],
                         [('subagent', hookmain.UNIDENTIFIED, 'worker')])

    def test_a_hung_git_cannot_hold_the_deny_up(self):
        from kitlib.guard import refusal_log
        started = []

        def hung(*a, **k):
            started.append(a)
            time.sleep(5)
            raise OSError('hung')
        deny = lambda *x, **k: Decision('deny', 'r', 'why')                            # noqa: E731
        with mock.patch('kitlib.git.cmd.call', side_effect=hung), \
                mock.patch('subprocess.Popen', side_effect=hung), mock.patch('subprocess.run', side_effect=hung):
            began = time.monotonic()
            self.assertEqual(run(FakeAdapter(), {'kind': 'shell'}, deny), (3, 'deny r\n'))
            took = time.monotonic() - began
        self.assertEqual((len(self.rows()), started), (1, []))
        self.assertLess(took, refusal_log.BOUND)


class CommandLine(unittest.TestCase):
    def test_the_one_hook_command_line_is_exec_form(self):
        line = hookmain.command_line(os.path.join('k', 'hook.py'))
        self.assertEqual(line['args'], ['k/hook.py'])
        self.assertTrue(os.path.isfile(line['command']))
        if os.name == 'nt':
            self.assertEqual(os.path.basename(line['command']).lower(), 'pythonw.exe')
        # an interpreter with no pythonw beside it is used itself
        self.assertEqual(hookmain.windowless(sys.executable + '-none'), sys.executable + '-none')

    @windows_only('pythonw.exe is a Windows interpreter')
    def test_an_existing_interpreter_of_any_python_name_gets_the_pythonw_beside_it(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('python3.exe', 'python3.14.exe', 'pythonw.exe'):
                open(os.path.join(d, name), 'w').close()
            want = os.path.join(d, 'pythonw.exe')
            self.assertEqual(hookmain.windowless(os.path.join(d, 'python3.exe')), want)
            self.assertEqual(hookmain.windowless(os.path.join(d, 'python3.14.exe')), want)
            gone = os.path.join(d, 'python9.exe')      # no such file: used as given
            self.assertEqual(hookmain.windowless(gone), gone)

    def test_a_manifest_command_on_windows_is_pythonw_in_exec_form(self):
        line = hookmain.manifest_command('${ROOT}/boot/hook.py', 'windows', 'h', 'PreToolUse')
        self.assertEqual(line, {'command': 'pythonw', 'args': ['${ROOT}/boot/hook.py', 'h', 'PreToolUse']})
        self.assertEqual(hookmain.manifest_command('a.py', 'windows', blocking=True),
                         {'command': 'pythonw', 'args': ['a.py']})

    def test_a_manifest_command_on_linux_runs_python3_under_sh_and_fails_closed(self):
        tool = hookmain.manifest_command('${ROOT}/boot/hook.py', 'linux', 'h', 'PreToolUse', blocking=True)
        self.assertEqual(tool, {'command': '/bin/sh', 'args': [
            '-c', 'python3 "$@" || exit 2', 'kit-hook', '${ROOT}/boot/hook.py', 'h', 'PreToolUse']})
        start = hookmain.manifest_command('${ROOT}/boot/hook.py', 'linux', 'h', 'SessionStart')
        self.assertEqual(start['command'], '/bin/sh')
        self.assertEqual(start['args'][0], '-c')
        self.assertTrue(start['args'][1].startswith('python3 "$@" || echo "PM kit: the kit did not load: '),
                        start['args'][1])
        self.assertNotIn('exit 2', start['args'][1])
        self.assertEqual(start['args'][2:], ['kit-hook', '${ROOT}/boot/hook.py', 'h', 'SessionStart'])

    def test_a_manifest_command_for_a_platform_with_no_interpreter_is_refused(self):
        with self.assertRaises(ValueError):
            hookmain.manifest_command('a.py', 'macos')

    def test_the_mcp_interpreter_is_python3_on_linux_and_refused_for_an_unknown_platform(self):
        """Stock Ubuntu has no `python`, only python3; Windows keeps the `python` its manifests name."""
        self.assertEqual(hookmain.mcp_interpreter('windows'), 'python')
        self.assertEqual(hookmain.mcp_interpreter('linux'), 'python3')
        with self.assertRaises(ValueError):
            hookmain.mcp_interpreter('macos')

    def test_the_settings_entry_wraps_it(self):
        entry = hookmain.settings_entry('PreToolUse', 'Bash', 'h.py', executable='py')
        self.assertEqual(entry, {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'py', 'args': ['h.py']}]}]})
        timed = hookmain.settings_entry('PreToolUse', '*', 'h.py', executable='py', timeout=10)
        self.assertEqual(timed['PreToolUse'][0]['hooks'][0]['timeout'], 10)
        self.assertEqual(list(entry['PreToolUse'][0]['hooks'][0]), ['type', 'command', 'args'])
        named = hookmain.settings_entry('PreToolUse', 'Bash', 'k/boot/hook.py', executable='py',
                                        args=('h', 'PreToolUse'))
        self.assertEqual(named['PreToolUse'][0]['hooks'][0]['args'], ['k/boot/hook.py', 'h', 'PreToolUse'])


class Dispatch(unittest.TestCase):
    """dispatch: boot/hook.py's work once the kit has booted."""

    class Starts(FakeAdapter):
        CONTEXT_EVENTS = ('Begin',)
        DEADLINE = 5

        def context(self, event_name, text):
            return ('[%s] %s\n' % (event_name, text) if text else ''), 0

    def test_a_start_event_writes_the_kit_header_through_context(self):
        from kitlib.host import header
        out = io.StringIO()
        self.assertEqual(hookmain.dispatch(self.Starts(), 'Begin', 'C:/k', '{not json', out), 0)
        self.assertEqual(out.getvalue(), '[Begin] %s\n' % header.text('C:/k'))
        with self.assertRaises(ValueError):
            hookmain.dispatch(self.Starts(), 'Begin', 'C:/k', '{}', io.StringIO(), part='pm')

    def test_start_text_gets_the_payload_and_the_part(self):
        a, seen = self.Starts(), []
        a.start_text = lambda *args: seen.append(args) or 'checks'
        out = io.StringIO()
        hookmain.dispatch(a, 'Begin', 'C:/k', '{"cwd": "C:/r"}', out, part='pm', environ={'X': '1'})
        self.assertEqual(seen, [('Begin', 'C:/k', {'cwd': 'C:/r'}, {'X': '1'}, 'pm')])
        self.assertEqual(out.getvalue(), '[Begin] checks\n')
        hookmain.dispatch(a, 'Begin', 'C:/k', '[]', io.StringIO())
        self.assertEqual(seen[-1][2], {}, 'a payload that is not an object reads as none')

    def test_the_tool_event_runs_the_guard_with_the_adapters_deadline_and_the_command_line(self):
        a, got = self.Starts(), {}

        def fake_run(adapter, event, stdin, stdout, environ, entry, deadline, started, args, kit):
            got.update(event=event, text=stdin.read(), entry=entry, deadline=deadline, started=started, args=args,
                       kit=kit)
            return 7
        with mock.patch.object(hookmain, 'run', fake_run):
            code = hookmain.dispatch(a, 'PreToolUse', 'C:/k', '{"kind": "shell"}', io.StringIO(),
                                     'C:/k/boot/hook.py', ['h', 'PreToolUse'], started=1.5)
        self.assertEqual(code, 7)
        self.assertEqual(got, {'event': 'PreToolUse', 'text': '{"kind": "shell"}', 'entry': 'C:/k/boot/hook.py',
                               'deadline': 5, 'started': 1.5, 'args': ['h', 'PreToolUse'], 'kit': 'C:/k'})
        with self.assertRaises(ValueError):
            hookmain.dispatch(a, 'PreToolUse', 'C:/k', '{}', io.StringIO(), part='pm')

    def test_the_reproduce_line_names_the_entrys_arguments(self):
        def boom(*a, **k):
            raise RuntimeError('x')
        out = io.StringIO()
        with mock.patch('kitlib.guard.decide', boom):
            hookmain.run(FakeAdapter(), 'PreToolUse', io.StringIO('{}'), out, environ={},
                         entry='C:/k/boot/hook.py', args=('h', 'PreToolUse'))
        self.assertEqual(out.getvalue(), 'deny guard-error\n')
        d = hookmain.failure(RuntimeError('x'), 'main', 'C:/k/boot/hook.py', ('h', 'PreToolUse'))
        self.assertEqual(d.instead, 'python "C:/k/boot/hook.py" h PreToolUse < event.json')


class HeaderFollow(unittest.TestCase):
    """The header-update line (kitlib.host.headerfollow): noted at the header part of a start
    event, told with the verdict of a later tool event."""

    class Tells(Dispatch.Starts):
        def tool_context(self, event_name, text, rendered):
            return rendered + '<ctx %s> %s\n' % (event_name, text)

    def test_the_header_part_notes_the_kit_with_the_payloads_agent(self):
        seen = []
        with mock.patch('kitlib.host.headerfollow.note', lambda *a: seen.append(a)):
            hookmain.dispatch(self.Tells(), 'Begin', 'C:/k', '{"session_id": "s", "who": "a1"}', io.StringIO())
            a = self.Tells()
            a.start_text = lambda *args: 'checks'
            hookmain.dispatch(a, 'Begin', 'C:/k', '{"session_id": "s"}', io.StringIO(), part='pm')
        self.assertEqual(seen, [('C:/k', {'session_id': 's', 'who': 'a1'}, 'a1', 'Begin')])

    def tool(self, adapter, verdict, line='LINE', reason=None, boom=False):
        """(exit code, stdout, the calls to headerfollow.line as (write flag, ...))."""
        out, seen = io.StringIO(), []
        reason = ('' if verdict == 'allow' else 'why') if reason is None else reason

        def told(kit, payload, agent=None, environ=None, current=None, write=True):
            seen.append((write, kit, payload, agent, environ))
            if boom:
                raise RuntimeError('record unreadable')
            return line
        with mock.patch('kitlib.guard.decide', lambda *x, **k: Decision(verdict, 'r', reason)), \
                mock.patch('kitlib.host.headerfollow.line', told):
            code = hookmain.run(adapter, 'PreToolUse', io.StringIO('{"session_id": "s", "who": "a1"}'), out,
                                environ={'X': '1'}, entry='C:/k/boot/hook.py', kit='C:/k')
        return code, out.getvalue(), seen

    def test_an_allow_tells_the_line_through_tool_context_and_settles_the_record_after(self):
        code, out, seen = self.tool(self.Tells(), 'allow')
        self.assertEqual((code, out), (0, '<ctx PreToolUse> LINE\n'))
        args = ('C:/k', {'session_id': 's', 'who': 'a1'}, 'a1', {'X': '1'})
        self.assertEqual(seen, [(False,) + args, (True,) + args])
        self.assertEqual(self.tool(self.Tells(), 'allow', None)[:2], (0, ''))

    def test_a_deny_and_an_ask_keep_their_reason_and_get_the_line_as_context(self):
        for verdict, code in (('deny', 3), ('ask', 0)):
            a = self.Tells()
            got = self.tool(a, verdict)
            self.assertEqual(got[:2], (code, '%s r\n<ctx PreToolUse> LINE\n' % verdict), verdict)
            self.assertEqual(a.last.reason, 'why', verdict)
            self.assertEqual([w for w, *_ in got[2]], [False, True], verdict)

    def test_a_warned_allow_keeps_its_reason_and_gets_the_line_as_context(self):
        a = self.Tells()
        self.assertEqual(self.tool(a, 'allow', reason='warn')[:2], (0, '<ctx PreToolUse> LINE\n'))
        self.assertEqual(a.last.reason, 'warn')

    def test_a_raising_headerfollow_changes_neither_the_decision_nor_the_output(self):
        for verdict in ('allow', 'ask', 'deny'):
            a, base = self.Tells(), self.Tells()
            got = self.tool(a, verdict, boom=True)
            want = self.tool(base, verdict, line=None)
            self.assertEqual(got[:2], want[:2], verdict)
            self.assertEqual(a.last, base.last, verdict)
            self.assertTrue(got[2], verdict)            # it was reached, and swallowed

    def test_a_raising_tool_context_changes_nothing_and_leaves_the_record_unspent(self):
        class Bad(self.Tells):
            def tool_context(self, event_name, text, rendered):
                raise RuntimeError('x')
        for verdict in ('allow', 'deny'):
            code, out, seen = self.tool(Bad(), verdict)
            self.assertEqual(out, '' if verdict == 'allow' else 'deny r\n')
            self.assertEqual([w for w, *_ in seen], [False], verdict)

    def test_an_adapter_without_tool_context_reads_no_record_for_any_verdict(self):
        """Codex prints nothing for an allow and has no context channel: its record is not spent
        into a reason nobody sees, nor appended to a deny's tag."""
        for verdict, reason, want in (('allow', '', ''), ('allow', 'warn', ''), ('ask', 'why', 'ask r\n'),
                                      ('deny', 'why', 'deny r\n')):
            a = FakeAdapter()
            code, out, seen = self.tool(a, verdict, reason=reason)
            self.assertEqual((out, seen), (want, []), (verdict, reason))
            self.assertEqual(a.last.reason, reason)

    def test_a_raising_note_does_not_fail_a_start_event(self):
        out = io.StringIO()
        with mock.patch('kitlib.host.headerfollow.note', side_effect=RuntimeError('disk')):
            code = hookmain.dispatch(self.Tells(), 'Begin', 'C:/k', '{"session_id": "s"}', out)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue())

    def test_no_kit_no_line(self):
        out = io.StringIO()
        with mock.patch('kitlib.guard.decide', lambda *x, **k: Decision('allow')), \
                mock.patch('kitlib.host.headerfollow.line', side_effect=AssertionError('read')):
            hookmain.run(self.Tells(), 'PreToolUse', io.StringIO('{"session_id": "s"}'), out, environ={})
        self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
