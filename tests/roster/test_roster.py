"""kitlib.roster and tools/agent_roster.py: docs/ROSTER.md against content/roles and each host's
tier -> model map.

The check runs on the real content/roles/; --write runs on a temp copy and must leave every byte
outside the changed lines alone (CRLF files included), then render the host files.
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

from hosts.claude.adapter import content as claude_content
from kitlib import content, reviewpack, roster
from kitlib.host import registry
from tests.docs.test_cli import repo_with_docs
from tests.gitfixture import fixture_env
from tests.home import no_machine_owner

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import agent_roster as cli  # noqa: E402

MAPS = {h: dict(registry.host(h).tiers) for h in ('claude', 'codex')}     # hosts.json, the one tier map


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def write_file(path, data):
    with open(path, 'wb') as f:
        f.write(data)


class RealRosterTest(unittest.TestCase):
    def test_roster_matches_the_roles_and_the_tier_maps(self):
        self.assertEqual(roster.check(KIT_ROOT, MAPS), [])

    def test_cli_default_is_check(self):
        out = io.StringIO()
        self.assertEqual(cli.main([], out), 0, out.getvalue())

    def test_every_rendered_model_is_the_roster_tiers(self):
        """The model an agent file names is the roster's tier through the host's tier table."""
        rows, models = roster.parse_roster(read(os.path.join(KIT_ROOT, 'docs', 'ROSTER.md')).decode('utf-8'))
        for name, (tier, effort) in rows.items():
            fm = roster.read_frontmatter(read(os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'agents', name + '.md')))
            with self.subTest(role=name):
                self.assertEqual(fm['model'], models['claude'][tier])
                self.assertEqual(fm.get('effort', roster.NONE), effort)


class TempCopyTest(unittest.TestCase):
    def setUp(self):
        top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, top, True)
        self.top = top
        for rel in ('content', 'docs', 'boot', '.claude-plugin', os.path.join('hosts', 'claude', 'shell', 'agents'),
                    os.path.join('hosts', 'claude', 'shell', 'skills')):
            shutil.copytree(os.path.join(KIT_ROOT, rel), os.path.join(top, rel))
        for rel in ('kit.json', os.path.join('hosts', 'claude', 'shell', 'hooks', 'hooks.json')):
            os.makedirs(os.path.dirname(os.path.join(top, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(KIT_ROOT, rel), os.path.join(top, rel))
        self.dir = os.path.join(top, 'content', 'roles')
        self.roster = os.path.join(top, 'docs', 'ROSTER.md')

    def edit_row(self, role, old, new):
        text = read(self.roster).decode('utf-8')
        lines = text.split('\n')
        i = next(i for i, l in enumerate(lines) if l.startswith('| %s |' % role))
        self.assertIn(old, lines[i])
        lines[i] = lines[i].replace(old, new, 1)
        write_file(self.roster, '\n'.join(lines).encode('utf-8'))

    def test_tier_and_effort_mismatch_listed(self):
        self.edit_row('worker', '| standard | medium |', '| strong | high |')
        got = roster.check(self.top, MAPS)
        self.assertEqual(len(got), 2, got)
        out = io.StringIO()
        self.assertEqual(cli.main(['--check', '-C', self.top], out), 1)
        self.assertIn('worker: tier is strong', out.getvalue())

    def test_an_unknown_tier_is_listed(self):
        self.edit_row('worker', '| standard |', '| huge |')
        self.assertIn('worker: tier huge in ROSTER.md is not one of light, standard, strong, deep',
                      roster.check(self.top, MAPS))

    def test_tier_table_against_the_host_map(self):
        self.assertEqual(roster.check(self.top, dict(MAPS, claude=dict(MAPS['claude'], deep='sonnet'))),
                         ['claude: tier deep runs opus in ROSTER.md, sonnet in the host renderer'])
        self.assertEqual(roster.check(self.top, dict(MAPS, other={'light': 'x'})),
                         ['other: no column in the ROSTER.md tier table'])
        self.assertEqual(roster.check(self.top, {k: v for k, v in MAPS.items() if k != 'claude'}),
                         ['claude: a column in the ROSTER.md tier table, but no such host renderer'])

    def test_missing_on_either_side(self):
        os.remove(os.path.join(self.dir, 'lander.md'))
        write_file(self.roster, read(self.roster).replace(b'| worker |', b'| ghost | strong | low | x |\n| worker |', 1))
        got = '\n'.join(roster.check(self.top, MAPS))
        self.assertIn('lander: row in ROSTER.md has no role file', got)
        self.assertIn('ghost: row in ROSTER.md has no role file', got)
        write_file(os.path.join(self.dir, 'extra.md'), b'---\nname: extra\ntier: strong\n---\nbody\n')
        self.assertIn('extra: role file has no row in ROSTER.md', '\n'.join(roster.check(self.top, MAPS)))

    def test_write_changes_only_the_two_lines_and_renders(self):
        before = {n: read(os.path.join(self.dir, n)) for n in os.listdir(self.dir)}
        self.edit_row('worker', '| standard | medium |', '| strong | high |')       # CRLF file
        self.edit_row('records', '| standard | low |', '| light | medium |')
        self.assertEqual(roster.write(self.top), ['records', 'worker'])
        self.assertEqual(roster.check(self.top, MAPS), [])
        for n, old in before.items():
            new = read(os.path.join(self.dir, n))
            if n in ('worker.md', 'records.md'):
                nl = b'\r\n' if b'\r\n' in old else b'\n'
                a, b = old.split(nl), new.split(nl)
                self.assertEqual(len(a), len(b), n)
                diff = [i for i in range(len(a)) if a[i] != b[i]]
                self.assertEqual(len(diff), 2, n)
                for i in diff:
                    self.assertTrue(a[i].startswith((b'tier:', b'effort:')), a[i])
            else:
                self.assertEqual(new, old, n)

    def test_cli_write_renders_the_host_files(self):
        self.edit_row('worker', '| standard | medium |', '| strong | high |')
        out = io.StringIO()
        self.assertEqual(cli.main(['--write', '-C', self.top], out), 0)
        self.assertIn('rendered hosts/claude/shell/agents/worker.md', out.getvalue())
        fm = roster.read_frontmatter(read(os.path.join(self.top, 'hosts', 'claude', 'shell', 'agents', 'worker.md')))
        self.assertEqual((fm['model'], fm['effort']), ('opus', 'high'))
        self.assertEqual(content.drift(self.top, [claude_content.renderer()]), [])

    def test_write_adds_and_removes_effort_line(self):
        self.edit_row('test-runner', '| light | - |', '| light | low |')
        self.edit_row('lander', '| light | medium |', '| light | - |')
        roster.write(self.top)
        self.assertEqual(roster.check(self.top, MAPS), [])
        self.assertIn(b'tier: light\neffort: low\n', read(os.path.join(self.dir, 'test-runner.md')))
        self.assertNotIn(b'effort:', read(os.path.join(self.dir, 'lander.md')).split(b'---')[1])

    def test_removing_effort_spares_effort_text_in_description(self):
        path = os.path.join(self.dir, 'lander.md')
        text = read(path).decode('utf-8')
        text = text.replace('description: ', 'description: Mind the effort it takes. ', 1)
        write_file(path, text.encode('utf-8'))
        self.edit_row('lander', '| light | medium |', '| light | - |')
        roster.write(self.top)
        want = text.replace('effort: medium' + chr(10), '', 1)
        self.assertEqual(read(path).decode('utf-8'), want)

    def test_write_leaves_other_keys_alone(self):
        # senior-worker.md carries `cache: long` after its capabilities: a rewrite of effort:
        # must not move or drop it.
        path = os.path.join(self.dir, 'senior-worker.md')
        old = read(path)
        nl = b'\r\n' if b'\r\n' in old else b'\n'
        self.assertIn(nl + b'cache: long' + nl + b'---' + nl, old)
        self.edit_row('senior-worker', '| strong | medium |', '| strong | high |')
        self.assertEqual(roster.write(self.top), ['senior-worker'])
        self.assertEqual(read(path), old.replace(b'effort: medium', b'effort: high', 1))
        self.edit_row('senior-worker', '| strong | high |', '| strong | - |')
        self.assertEqual(roster.write(self.top), ['senior-worker'])
        self.assertEqual(read(path), old.replace(nl + b'effort: medium', b'', 1))
        self.assertEqual(roster.check(self.top, MAPS), [])

    def test_write_is_a_no_op_when_agreeing(self):
        self.assertEqual(roster.write(self.top), [])

    def test_an_unreadable_roster_is_usage(self):
        os.remove(self.roster)
        out = io.StringIO()
        self.assertEqual(cli.main(['-C', self.top], out), 2)
        self.assertIn('agent_roster: cannot read', out.getvalue())


class ReviewerRuleTest(unittest.TestCase):
    """The "Reviewer model" rule in the dispatch skill's roster.md is prose a coordinator follows by
    matching strings: the group labels the review pack prints and the name of the strict-path
    tuple. Each one it names must still be what the code prints or defines."""

    RULE_FILE = os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'skills', 'coordinator-dispatch', 'roster.md')

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
        # no owner email in .kit.json: the owner rules read the machine config's, here none
        no_machine_owner(self)
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
        m = re.search(r'`(\w+)` in `<kit>/(kitlib/[\w/]+)\.py`', rule)     # a skill writes the kit's root <kit>
        self.assertIsNotNone(m, 'the rule names no strict-path tuple')
        self.assertEqual(m.group(1), 'STRICT_DEFAULTS')
        module = importlib.import_module(m.group(2).replace('/', '.'))
        self.assertTrue(getattr(module, m.group(1), None), '%s.%s' % (module.__name__, m.group(1)))
        self.assertIn('hosts/*/shell/agents/**', getattr(module, m.group(1)))


if __name__ == '__main__':
    unittest.main()
