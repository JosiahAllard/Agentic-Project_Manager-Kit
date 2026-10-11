"""kitlib.agentname (docs/guard.md "Agent names"): an agent bundled in the kit plugin is named
`<plugin>:<name>`; the agent type loses exactly one case-sensitive prefix, a session mark loses
every prefix case ignored, and nothing else is stripped."""
import unittest

from kitlib import agentname, lockmcp

P = agentname.PREFIX


class Prefix(unittest.TestCase):
    def test_the_prefix_is_the_plugin_name(self):
        self.assertEqual(P, lockmcp.CLAUDE_PLUGIN_NAME + ':')


class Bare(unittest.TestCase):
    def test_bare_and_prefixed_name_the_same_agent(self):
        self.assertEqual(agentname.bare('lander'), 'lander')
        self.assertEqual(agentname.bare(P + 'lander'), 'lander')
        self.assertEqual(agentname.bare(P + 'deep-worker'), 'deep-worker')

    def test_nothing_but_one_exact_prefix_is_stripped(self):
        for name, out in (('other:lander', 'other:lander'),
                          (P + P + 'lander', P + 'lander'),
                          (P.upper() + 'lander', P.upper() + 'lander'),
                          (P.rstrip(':') + 'lander', P.rstrip(':') + 'lander'),
                          ('x' + P + 'lander', 'x' + P + 'lander'),
                          (P[1:] + 'lander', P[1:] + 'lander'),
                          (' ' + P + 'lander', ' ' + P + 'lander'),
                          (P, '')):
            with self.subTest(name):
                self.assertEqual(agentname.bare(name), out)

    def test_a_missing_or_odd_value_passes_through(self):
        for value in (None, '', 3, ['lander']):
            self.assertEqual(agentname.bare(value), value)

    def test_qualified(self):
        self.assertEqual(agentname.qualified('worker'), P + 'worker')
        self.assertEqual(agentname.qualified(P + 'worker'), P + 'worker')


class BareMark(unittest.TestCase):
    def test_every_prefix_goes_case_ignored(self):
        for mark, out in ((P + 'coordinator', 'coordinator'),
                          (P.upper() + 'Coordinator', 'Coordinator'),
                          (P + P + 'owner', 'owner'),
                          (' %s pm ' % P, 'pm'),
                          (P + ' ' + P.title() + 'deep-worker', 'deep-worker'),
                          ('coordinator', 'coordinator')):
            with self.subTest(mark):
                self.assertEqual(agentname.bare_mark(mark), out)

    def test_another_plugin_keeps_its_prefix(self):
        self.assertEqual(agentname.bare_mark('other:pm'), 'other:pm')
        self.assertEqual(agentname.bare_mark('x' + P + 'pm'), 'x' + P + 'pm')

    def test_a_mark_that_is_all_prefix_is_never_emptied(self):
        # an empty mark would read as unmarked, the owner's own session
        for mark in (P, P + P, ' ' + P.upper() + ' '):
            with self.subTest(mark):
                self.assertEqual(agentname.bare_mark(mark), mark)

    def test_a_missing_value_passes_through(self):
        self.assertIsNone(agentname.bare_mark(None))


if __name__ == '__main__':
    unittest.main()
