"""kitlib.roster and tools/agent_roster.py: docs/ROSTER.md against the agent files' frontmatter.

The check runs on the real claude/agents/ folder; --write runs on a temp copy and must leave every byte
outside the changed lines alone (CRLF files included).
"""
import importlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import reviewpack, roster
from tests.docs.test_cli import repo_with_docs
from tests.gitfixture import fixture_env

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENTS = os.path.join(KIT_ROOT, 'claude', 'agents')
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import agent_roster as cli  # noqa: E402


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def write_file(path, data):
    with open(path, 'wb') as f:
        f.write(data)


class RealAgentsTest(unittest.TestCase):
    def test_roster_matches_frontmatter(self):
        self.assertEqual(roster.check(AGENTS), [])

    def test_cli_default_is_check(self):
        out = io.StringIO()
        self.assertEqual(cli.main([], out), 0, out.getvalue())


class TempCopyTest(unittest.TestCase):
    def setUp(self):
        top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, top, True)
        self.top = top
        self.dir = os.path.join(top, 'claude', 'agents')    # the kit layout: docs/ two levels up
        shutil.copytree(AGENTS, self.dir)
        os.mkdir(os.path.join(top, 'docs'))
        self.roster = os.path.join(top, 'docs', 'ROSTER.md')
        shutil.copy(os.path.join(KIT_ROOT, 'docs', 'ROSTER.md'), self.roster)

    def edit_row(self, agent, old, new):
        text = read(self.roster).decode('utf-8')
        lines = text.split('\n')
        i = next(i for i, l in enumerate(lines) if l.startswith('| %s |' % agent))
        self.assertIn(old, lines[i])
        lines[i] = lines[i].replace(old, new, 1)
        write_file(self.roster, '\n'.join(lines).encode('utf-8'))

    def test_model_and_effort_mismatch_listed(self):
        self.edit_row('worker', '| sonnet | medium |', '| opus | high |')
        got = roster.check(self.dir)
        self.assertEqual(len(got), 2, got)
        out = io.StringIO()
        self.assertEqual(cli.main(['--check', '--dir', self.dir], out), 1)
        self.assertIn('worker: model is opus', out.getvalue())

    def test_missing_on_either_side(self):
        os.remove(os.path.join(self.dir, 'lander.md'))
        write_file(self.roster, read(self.roster) + b'| ghost | opus | low | x |\n')
        got = '\n'.join(roster.check(self.dir))
        self.assertIn('lander: row in ROSTER.md has no agent file', got)
        self.assertIn('ghost: row in ROSTER.md has no agent file', got)
        write_file(os.path.join(self.dir, 'extra.md'), b'---\nname: extra\nmodel: opus\n---\nbody\n')
        self.assertIn('extra: agent file has no row in ROSTER.md', '\n'.join(roster.check(self.dir)))

    def test_write_changes_only_the_two_lines(self):
        before = {n: read(os.path.join(self.dir, n)) for n in os.listdir(self.dir)}
        self.edit_row('worker', '| sonnet | medium |', '| opus | high |')
        self.edit_row('coordinator', '| opus | medium |', '| sonnet | low |')   # CRLF file
        self.assertEqual(roster.write(self.dir), ['coordinator', 'worker'])
        self.assertEqual(roster.check(self.dir), [])
        for n, old in before.items():
            new = read(os.path.join(self.dir, n))
            if n in ('worker.md', 'coordinator.md'):
                nl = b'\r\n' if b'\r\n' in old else b'\n'
                a, b = old.split(nl), new.split(nl)
                self.assertEqual(len(a), len(b), n)
                diff = [i for i in range(len(a)) if a[i] != b[i]]
                self.assertEqual(len(diff), 2, n)
                for i in diff:
                    self.assertTrue(a[i].startswith((b'model:', b'effort:')), a[i])
            else:
                self.assertEqual(new, old, n)

    def test_write_adds_and_removes_effort_line(self):
        self.edit_row('test-runner', '| haiku | - |', '| haiku | low |')
        self.edit_row('lander', '| haiku | medium |', '| haiku | - |')
        roster.write(self.dir)
        self.assertEqual(roster.check(self.dir), [])
        self.assertIn(b'model: haiku\neffort: low\n', read(os.path.join(self.dir, 'test-runner.md')))
        self.assertNotIn(b'effort:', read(os.path.join(self.dir, 'lander.md')).split(b'---')[1])

    def test_removing_effort_spares_effort_text_in_description(self):
        path = os.path.join(self.dir, 'lander.md')
        text = read(path).decode('utf-8')
        text = text.replace('description: ', 'description: Mind the effort: it is a word here. ', 1)
        write_file(path, text.encode('utf-8'))
        self.edit_row('lander', '| haiku | medium |', '| haiku | - |')
        roster.write(self.dir)
        want = text.replace('effort: medium' + chr(10), '', 1)
        self.assertEqual(read(path).decode('utf-8'), want)

    def test_write_leaves_a_frontmatter_map_alone(self):
        # opus-worker.md carries a map (`experimental:` over an indented `cacheTtl: 1h`): the
        # indented line is no key of its own to check, and a rewrite of effort: must not move it.
        path = os.path.join(self.dir, 'opus-worker.md')
        old = read(path)
        nl = b'\r\n' if b'\r\n' in old else b'\n'
        block = nl + b'experimental:' + nl + b'  cacheTtl: 1h' + nl + b'---' + nl
        self.assertIn(b'effort: medium' + block, old)
        self.assertEqual(roster.read_frontmatter(old).get('experimental'), '')
        self.assertNotIn('cacheTtl', roster.read_frontmatter(old))
        self.edit_row('opus-worker', '| opus | medium |', '| opus | high |')
        self.assertEqual(roster.write(self.dir), ['opus-worker'])
        self.assertEqual(read(path), old.replace(b'effort: medium', b'effort: high', 1))
        self.edit_row('opus-worker', '| opus | high |', '| opus | - |')
        self.assertEqual(roster.write(self.dir), ['opus-worker'])
        self.assertEqual(read(path), old.replace(nl + b'effort: medium', b'', 1))
        self.assertEqual(roster.check(self.dir), [])

    def test_write_is_a_no_op_when_agreeing(self):
        self.assertEqual(roster.write(self.dir), [])

    def test_roster_found_one_level_up_from_claude_agents(self):
        self.assertEqual(roster.roster_path(self.dir), self.roster)

    def test_roster_beside_a_flat_agents_folder_wins(self):
        flat = os.path.join(self.top, 'claude', 'docs')
        os.mkdir(flat)
        shutil.copy(self.roster, os.path.join(flat, 'ROSTER.md'))
        self.assertEqual(roster.roster_path(self.dir), os.path.join(flat, 'ROSTER.md'))


class ReviewerRuleTest(unittest.TestCase):
    """The "Reviewer model" rule in the dispatch skill's roster.md is prose a coordinator follows by
    matching strings: the group labels the review pack prints and the name of the strict-path
    tuple. Each one it names must still be what the code prints or defines."""

    RULE_FILE = os.path.join(KIT_ROOT, 'claude', 'skills', 'coordinator-dispatch', 'roster.md')

    def rule(self):
        text = read(self.RULE_FILE).decode('utf-8').replace('\r\n', '\n')
        m = re.search(r'^- \*\*Reviewer model\*\*.*?(?=^- )', text, re.S | re.M)
        self.assertIsNotNone(m, 'no "Reviewer model" bullet in roster.md')
        return re.sub(r'\s+', ' ', m.group(0))       # a wrapped line is still one sentence

    def printed_groups(self):
        """The group lines a real pack prints under its `files` line, in order: a throwaway repo
        whose branch changes one file at each risk level and one the risk map does not list."""
        repo = repo_with_docs()
        self.addCleanup(repo.cleanup)
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        repo.write('docs/ops/risk-map.md', '- High | hi/\n- Medium | med/\n- Low | low/\n')
        repo.git('add', '-A')
        repo.git('commit', '-q', '-m', 'risk map')
        repo.git('checkout', '-q', '-b', 'phase/x')
        for folder in ('hi', 'med', 'low', 'other'):
            repo.write('%s/a.py' % folder, 'x = 1\n')
        repo.git('add', '-A')
        repo.git('commit', '-q', '-m', 'one file per level')
        lines = reviewpack.pack(repo.root, 'phase/x', base='main').lines
        start = next(i for i, l in enumerate(lines) if l.startswith('files '))
        groups = []
        for line in lines[start + 1:]:
            if not line.startswith('  '):
                break
            m = re.match(r'  (\S.*) \(\d+\)$', line)
            if m:
                groups.append(m.group(1))
        return groups

    def test_rule_names_only_what_the_code_prints_and_defines(self):
        rule = self.rule()
        printed = self.printed_groups()
        self.assertEqual(len(printed), 4, printed)      # the fixture reached every level
        # the list of groups the rule tells the coordinator to read: `<label> (N)`
        listed = re.findall(r'`([^`]+) \(N\)`', rule)
        self.assertEqual(listed, printed)
        # the labels its rows turn on: `<label>` group, `<a>` or `<b>` group
        in_rows = []
        for run in re.findall(r'((?:`[^`]+`(?:,| or| and)? )+)groups?\b', rule):
            in_rows.extend(re.findall(r'`([^`]+)`', run))
        self.assertTrue(in_rows, 'the rule names no group in its rows')
        self.assertEqual(sorted(set(in_rows) - set(printed)), [])
        self.assertEqual(sorted(set(printed) - set(in_rows)), [], 'a printed group no row covers')
        # the strict landing paths: a name in a module, both as the rule spells them
        m = re.search(r'`(\w+)` in `\$\{CLAUDE_PLUGIN_ROOT\}/(kitlib/[\w/]+)\.py`', rule)
        self.assertIsNotNone(m, 'the rule names no strict-path tuple')
        self.assertEqual(m.group(1), 'STRICT_DEFAULTS')
        module = importlib.import_module(m.group(2).replace('/', '.'))
        self.assertTrue(getattr(module, m.group(1), None), '%s.%s' % (module.__name__, m.group(1)))
        self.assertIn('claude/agents/**', getattr(module, m.group(1)))


if __name__ == '__main__':
    unittest.main()
