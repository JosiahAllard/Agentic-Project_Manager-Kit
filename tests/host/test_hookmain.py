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


class CommandLine(unittest.TestCase):
    def test_the_one_hook_command_line_is_exec_form(self):
        line = hookmain.command_line(os.path.join('k', 'hook.py'))
        self.assertEqual(line['args'], ['k/hook.py'])
        self.assertTrue(os.path.isfile(line['command']))
        if os.name == 'nt':
            self.assertEqual(os.path.basename(line['command']).lower(), 'pythonw.exe')
        # an interpreter with no pythonw beside it is used itself
        self.assertEqual(hookmain.windowless(sys.executable + '-none'), sys.executable + '-none')

    @unittest.skipUnless(os.name == 'nt', 'pythonw.exe is a Windows interpreter')
    def test_an_existing_interpreter_of_any_python_name_gets_the_pythonw_beside_it(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('python3.exe', 'python3.14.exe', 'pythonw.exe'):
                open(os.path.join(d, name), 'w').close()
            want = os.path.join(d, 'pythonw.exe')
            self.assertEqual(hookmain.windowless(os.path.join(d, 'python3.exe')), want)
            self.assertEqual(hookmain.windowless(os.path.join(d, 'python3.14.exe')), want)
            gone = os.path.join(d, 'python9.exe')      # no such file: used as given
            self.assertEqual(hookmain.windowless(gone), gone)

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

        def fake_run(adapter, event, stdin, stdout, environ, entry, deadline, started, args):
            got.update(event=event, text=stdin.read(), entry=entry, deadline=deadline, started=started, args=args)
            return 7
        with mock.patch.object(hookmain, 'run', fake_run):
            code = hookmain.dispatch(a, 'PreToolUse', 'C:/k', '{"kind": "shell"}', io.StringIO(),
                                     'C:/k/boot/hook.py', ['h', 'PreToolUse'], started=1.5)
        self.assertEqual(code, 7)
        self.assertEqual(got, {'event': 'PreToolUse', 'text': '{"kind": "shell"}', 'entry': 'C:/k/boot/hook.py',
                               'deadline': 5, 'started': 1.5, 'args': ['h', 'PreToolUse']})
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


if __name__ == '__main__':
    unittest.main()
