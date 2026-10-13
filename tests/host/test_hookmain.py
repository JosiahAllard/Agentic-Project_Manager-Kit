"""kitlib.host.hookmain: the one hook runner, over a fake adapter and a fake guard decide.

What every host's hook gets from it: parse -> decide -> degrade -> render, a guard-error deny for
any failure on the way (the role tag read from the payload even when parse refuses it), a
deadline that denies rather than falls silent, and the one rendered hook command line."""
import io
import json
import os
import sys
import threading
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


class CommandLine(unittest.TestCase):
    def test_the_one_hook_command_line_is_exec_form(self):
        line = hookmain.command_line(os.path.join('k', 'hook.py'))
        self.assertEqual(line['args'], ['k/hook.py'])
        self.assertTrue(os.path.isfile(line['command']))
        if os.name == 'nt':
            self.assertEqual(os.path.basename(line['command']).lower(), 'pythonw.exe')
        # an interpreter with no pythonw beside it is used itself
        self.assertEqual(hookmain.windowless(sys.executable + '-none'), sys.executable + '-none')

    def test_the_settings_entry_wraps_it(self):
        entry = hookmain.settings_entry('PreToolUse', 'Bash', 'h.py', executable='py')
        self.assertEqual(entry, {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'py', 'args': ['h.py']}]}]})
        timed = hookmain.settings_entry('PreToolUse', '*', 'h.py', executable='py', timeout=10)
        self.assertEqual(timed['PreToolUse'][0]['hooks'][0]['timeout'], 10)
        self.assertEqual(list(entry['PreToolUse'][0]['hooks'][0]), ['type', 'command', 'args'])


if __name__ == '__main__':
    unittest.main()
