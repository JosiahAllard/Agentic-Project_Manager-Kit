"""Bare and plugin-prefixed agent names get the same decisions (docs/guard.md "Agent names").

Every other tests/guard test runs again with the prefix knob on: each subagent's agent_type is
qualified (kitlib.agentname.qualified, through world.AGENT_TYPE) and the PM_KIT_ROLE mark every
guard context is built with gets the prefix. The whole set must pass, so every row's decision
equals its bare twin. Sentinels show the guard really read prefixed names; without the knob they
stay at zero. A main session (no agent_id) is untouched by an agent_type it carries."""
import io
import os
import unittest
from unittest import mock

from kitlib import agentname
from kitlib.guard import context, decide

from . import world as W
from .world import bash, event, ps

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(HERE))
P = agentname.PREFIX


def tests_in(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests_in(item)
        else:
            yield item


def twin(d):
    return None if d is None else (d.kind, d.rule, d.reason)


class Knob(object):
    """The prefix knob: patches world.AGENT_TYPE and context.Ctx, and counts what the guard saw."""

    def __init__(self):
        self.qualified = self.marked = 0           # names the knob changed
        self.seen_type = self.seen_mark = 0        # prefixed names the guard's normaliser was given
        self.real_init = context.Ctx.__init__
        self.real_bare, self.real_bare_mark = agentname.bare, agentname.bare_mark

    def agent_type(self, kind):
        if not isinstance(kind, str):
            return kind
        self.qualified += 1
        return agentname.qualified(kind)

    def init(self, ctx, *args, **kw):
        self.real_init(ctx, *args, **kw)
        value = ctx.environ.get(context.ROLE_ENV)
        if isinstance(value, str) and value.strip():   # a blank mark is unmarked; it stays unmarked
            ctx.environ[context.ROLE_ENV] = P + value
            self.marked += 1

    def bare(self, name):
        if isinstance(name, str) and name.startswith(P):
            self.seen_type += 1
        return self.real_bare(name)

    def bare_mark(self, mark):
        if isinstance(mark, str) and mark.strip().lower().startswith(P.lower()):
            self.seen_mark += 1
        return self.real_bare_mark(mark)

    def patches(self):
        def init(ctx, *args, **kw):                    # a plain function: Ctx passes itself in
            self.init(ctx, *args, **kw)
        return (mock.patch.object(W, 'AGENT_TYPE', self.agent_type),
                mock.patch.object(context.Ctx, '__init__', init),
                mock.patch.object(agentname, 'bare', self.bare),
                mock.patch.object(agentname, 'bare_mark', self.bare_mark))


class PrefixParity(unittest.TestCase):

    def test_every_guard_test_passes_with_prefixed_names(self):
        loaded = unittest.defaultTestLoader.discover(HERE, pattern='test_*.py', top_level_dir=TOP)
        suite = unittest.TestSuite(t for t in tests_in(loaded) if not t.id().startswith(__name__ + '.'))
        knob = Knob()
        patches = knob.patches()
        for p in patches:
            p.start()
        try:
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual([t.id() for t, _ in result.failures + result.errors], [],
                         '\n'.join(tb for _, tb in (result.failures + result.errors)[:3]))
        self.assertEqual(result.testsRun, suite.countTestCases())
        self.assertGreater(result.testsRun, 100)
        # the observable that would be absent without the knob
        self.assertGreater(knob.qualified, 0)
        self.assertGreater(knob.marked, 0)
        self.assertGreater(knob.seen_type, 0)
        self.assertGreater(knob.seen_mark, 0)

    def test_the_knob_is_off_by_default(self):
        self.assertIsNone(W.AGENT_TYPE)
        self.assertEqual(event('Bash', {}, 'x', True, 'lander')['agent_type'], 'lander')


class Twins(unittest.TestCase):
    """A few rows decided both ways side by side, reasons included."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo', 'push_block': True},
                              'roles': {'lander': {'edit_main_checkout': True, 'why': 'test'}}})
        self.addCleanup(self.w.cleanup)
        m = W.fwd(self.w.main)
        self.rows = (('Bash', {'command': 'git -C "%s" merge --ff-only origin/main' % m}, self.w.wt),
                     ('Bash', {'command': 'git -C "%s" commit -m x' % m}, self.w.wt),
                     ('Bash', {'command': 'git merge --no-ff kit/wt'}, self.w.main),
                     ('Bash', {'command': 'git restore a.py'}, self.w.main),
                     ('Write', {'file_path': os.path.join(self.w.main, 'a.md'), 'content': ''}, self.w.main),
                     ('Write', {'file_path': os.path.join(self.w.plain, 'a.md'), 'content': ''}, self.w.wt),
                     ('PowerShell', {'command': 'Stop-Process -Name godot -Force'}, self.w.wt))

    def test_a_subagent_by_either_name(self):
        for kind in ('lander', 'worker', 'records', 'main', 'opus-worker'):
            for tool, data, cwd in self.rows:
                with self.subTest(kind, tool=tool, data=data):
                    bare = decide(event(tool, data, cwd, True, kind), self.w.ctx(environ={}))
                    prefixed = decide(event(tool, data, cwd, True, P + kind), self.w.ctx(environ={}))
                    self.assertEqual(twin(prefixed), twin(bare))

    def test_a_main_session_by_either_mark(self):
        for mark in ('coordinator', 'pm', 'deep-worker', 'consultant', 'plain', 'owner', 'Coordinator'):
            for tool, data, cwd in self.rows:
                with self.subTest(mark, tool=tool, data=data):
                    bare = decide(event(tool, data, cwd), self.w.ctx(environ={context.ROLE_ENV: mark}))
                    prefixed = decide(event(tool, data, cwd), self.w.ctx(environ={context.ROLE_ENV: P + mark}))
                    self.assertEqual(twin(prefixed), twin(bare))

    def test_a_main_session_ignores_an_agent_type(self):
        # no agent_id: the session is the main one whatever agent_type says
        for env in ({}, {context.ROLE_ENV: 'coordinator'}):
            for tool, data, cwd in self.rows:
                for kind in (P + 'coordinator', P + 'lander', 'lander'):
                    with self.subTest(kind, env=env, tool=tool):
                        plain = event(tool, data, cwd)
                        typed = dict(plain, agent_type=kind)
                        self.assertEqual(twin(decide(typed, self.w.ctx(environ=env))),
                                         twin(decide(plain, self.w.ctx(environ=env))))

    def test_the_rows_are_not_all_allowed(self):
        # the twins compare real decisions: some rows deny for a worker
        kinds = [twin(decide(event(t, d, c, True, 'worker'), self.w.ctx(environ={}))) for t, d, c in self.rows]
        self.assertTrue(any(k and k[0] == 'deny' for k in kinds), kinds)
        lander = [twin(decide(event(t, d, c, True, 'lander'), self.w.ctx(environ={}))) for t, d, c in self.rows]
        self.assertIn(None, lander)
        self.assertNotEqual(lander, kinds)
        self.assertIsNotNone(decide(bash('git restore a.py', self.w.main, True), self.w.ctx(environ={})))


if __name__ == '__main__':
    unittest.main()
