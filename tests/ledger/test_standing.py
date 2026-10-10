"""kitlib.ledger.standing and `ledger.py standing` / `ledger.py check` over STANDING.md beside the
ledger (docs/ledger.md "Standing digest"). Ids are built by rid(n) (tests/scrub)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib.ledger import standing

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')


def rid(n):
    return 'R-%04d' % n


def entry(n, retired=False):
    body = 'Retired: spent - done (see -)\n' if retired else (
        'Q: q\nA: a\nDecides: Rule %d.\nSource: s\nSupersedes: -\n' % n)
    return '## %s | 2026-10-0%d 08:00Z | alpha | selected\n%s\n' % (rid(n), n, body)


LEDGER = '# Rulings ledger\n\n' + entry(1) + entry(2, retired=True) + entry(3)

DIGEST = """# Standing rulings

## alpha

### Landing
- %s: land one branch at a time.
- %s: the gate runs before every landing.

## all
- %s: Commands go inline, never in files.
""" % (rid(1), rid(3), rid(3))


class ProblemsTest(unittest.TestCase):
    def test_live_citations_pass_and_no_digest_is_no_finding(self):
        self.assertEqual(standing.problems(LEDGER, DIGEST), [])
        self.assertEqual(standing.problems(LEDGER, None), [])

    def test_retired_and_missing_citations_are_named_with_their_line(self):
        digest = DIGEST + '- %s: old rule.\n- %s: unknown rule.\n' % (rid(2), rid(9))
        errs = standing.problems(LEDGER, digest.replace('\n', '\r\n'))
        self.assertEqual(len(errs), 2, errs)
        self.assertIn('STANDING.md:11 cites %s, which is retired' % rid(2), errs[0])
        self.assertIn('STANDING.md:12 cites %s, which is not in the ledger' % rid(9), errs[1])


class TopicTest(unittest.TestCase):
    def test_a_word_in_a_rule_line_brings_its_headings_once(self):
        self.assertEqual(standing.topic(DIGEST, 'GATE'),
                         ['## alpha', '### Landing', '- %s: the gate runs before every landing.' % rid(3)])

    def test_a_word_in_a_heading_brings_every_rule_under_it(self):
        got = standing.topic(DIGEST, 'landing')
        self.assertEqual(got[:2], ['## alpha', '### Landing'])
        self.assertEqual(len(got), 4)
        self.assertEqual(standing.topic(DIGEST, 'all'), ['## all', '- %s: Commands go inline, never in files.' % rid(3)])

    def test_the_title_is_no_heading_and_no_match_is_empty(self):
        self.assertEqual(standing.topic(DIGEST, 'rulings'), [])
        self.assertEqual(standing.topic(DIGEST, 'nowhere'), [])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        folder = os.path.join(self.home, 'rulings')
        os.makedirs(folder)
        self.ledger = os.path.join(folder, 'LEDGER.md')
        self.digest = os.path.join(folder, 'STANDING.md')
        self.put(self.ledger, LEDGER)
        os.makedirs(os.path.join(self.home, '.claude-pm-kit'))
        self.put(os.path.join(self.home, '.claude-pm-kit', 'config.json'),
                 json.dumps({'ledger_path': self.ledger}))

    def put(self, path, text):
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def run_tool(self, *args):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home, CLAUDE_AGENT_MODEL='')
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60, cwd=self.home)

    def test_standing_prints_the_topic_and_exits_1_on_no_match_or_no_digest(self):
        r = self.run_tool('standing', 'gate')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('no standing digest', r.stderr)
        self.put(self.digest, DIGEST)
        r = self.run_tool('standing', 'gate')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], '- %s: the gate runs before every landing.' % rid(3))
        self.assertEqual(self.run_tool('standing', 'nowhere').returncode, 1)
        self.assertEqual(self.run_tool('standing').returncode, 2)

    def test_check_fails_on_a_retired_citation_and_keeps_the_base_reason(self):
        self.put(self.digest, DIGEST)
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.put(self.digest, DIGEST + '- %s: old.\n' % rid(2))
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('FAIL STANDING.md:11 cites %s, which is retired' % rid(2), r.stdout)
        # the base revision's own reason is still printed, not the digest's
        line = [x for x in r.stdout.splitlines() if x.startswith('append-only NOT CHECKED')][0]
        self.assertNotEqual(line.rstrip(), 'append-only NOT CHECKED: cannot read the ledger at none ()')


if __name__ == '__main__':
    unittest.main()
