"""kitlib.roster, tools/agent_roster.py and the roster render: docs/ROSTER.md's tables are rendered
from content/roles and each host's tier -> model map (decision 17, docs/multi-host-plan.md section 7).

The check runs on the real tree; the edits run on a temp copy and must leave every byte outside the
rendered regions alone.
"""
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

from kitlib import content, roster
from kitlib.host import registry
from kitlib.host.content import ContentError

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import agent_roster as cli  # noqa: E402

MAPS = {h: dict(registry.host(h).tiers) for h in registry.names()}     # hosts.json, the one tier map


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def write_file(path, data):
    with open(path, 'wb') as f:
        f.write(data)


def tables(text):
    """{first header cell: [row cells, ...]} of every markdown table in `text`."""
    out, cur = {}, None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            cur = None
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if cur is None:
            cur = out.setdefault(cells[0], [])
            cur.append(cells)
        elif not all(set(c) <= set('-: ') for c in cells):
            cur.append(cells)
    return out


class RealRosterTest(unittest.TestCase):
    def test_roster_is_what_the_render_makes(self):
        self.assertEqual(content.drift(KIT_ROOT, [], tiers=MAPS), [])

    def test_cli_default_is_check(self):
        out = io.StringIO()
        self.assertEqual(cli.main([], out), 0, out.getvalue())

    def test_one_row_per_role_file_and_one_column_per_host(self):
        t = tables(read(os.path.join(KIT_ROOT, 'docs', 'ROSTER.md')).decode('utf-8'))
        rows = t['role'][1:]
        self.assertEqual([r[0] for r in rows], [r.name for r in content.roster(KIT_ROOT)])
        self.assertEqual(t['tier'][0][1:], sorted(registry.names()))

    def test_every_rendered_model_is_the_roster_tiers(self):
        """The model an agent file names is the role's tier through the host's tier table."""
        t = tables(read(os.path.join(KIT_ROOT, 'docs', 'ROSTER.md')).decode('utf-8'))
        hosts = t['tier'][0][1:]
        models = {row[0]: dict(zip(hosts, row[1:])) for row in t['tier'][1:]}
        for name, tier, effort, _ in t['role'][1:]:
            fm = roster_frontmatter(read(os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'agents', name + '.md')))
            with self.subTest(role=name):
                self.assertEqual(fm['model'], models[tier]['claude'])
                self.assertEqual(fm.get('effort', roster.NONE), effort)


def roster_frontmatter(data):
    text = data.decode('utf-8').replace('\r\n', '\n')
    head = text.split('\n---\n', 1)[0]
    return dict((k.strip(), v.strip()) for k, _, v in (l.partition(':') for l in head.split('\n')[1:])
                if not k.startswith(' '))


class TempCopyTest(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.top, True)
        shutil.copytree(os.path.join(KIT_ROOT, 'content', 'roles'), os.path.join(self.top, 'content', 'roles'))
        os.makedirs(os.path.join(self.top, 'content', 'skills'))
        os.makedirs(os.path.join(self.top, 'docs'))
        shutil.copyfile(os.path.join(KIT_ROOT, 'docs', 'ROSTER.md'), os.path.join(self.top, 'docs', 'ROSTER.md'))
        self.dir = os.path.join(self.top, 'content', 'roles')
        self.roster = os.path.join(self.top, 'docs', 'ROSTER.md')

    def check(self):
        out = io.StringIO()
        return cli.main(['--check', '-C', self.top], out), out.getvalue()

    def rows(self):
        return {r[0]: r[1:] for r in tables(read(self.roster).decode('utf-8'))['role'][1:]}

    def edit_role(self, name, old, new):
        path = os.path.join(self.dir, name + '.md')
        data = read(path)
        self.assertIn(old, data)
        write_file(path, data.replace(old, new, 1))

    def test_a_role_file_edit_is_drift_and_write_renders_its_row(self):
        self.edit_role('worker', b'tier: standard', b'tier: strong')
        self.edit_role('worker', b'effort: medium', b'effort: high')
        self.assertEqual(self.check(), (1, 'differs: docs/ROSTER.md; run python tools/render_content.py --write\n'))
        before = read(self.roster)
        self.assertEqual(cli.main(['--write', '-C', self.top], io.StringIO()), 0)
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.rows()['worker'][:2], ['strong', 'high'])
        a, b = before.split(b'\n'), read(self.roster).split(b'\n')
        self.assertEqual([x for x, y in zip(a, b) if x != y], [l for l in a if l.startswith(b'| worker |')])

    def test_one_file_adds_or_removes_a_role(self):
        write_file(os.path.join(self.dir, 'extra.md'),
                   b'---\nname: extra\ndescription: Does | extra things.\ntier: light\nsandbox: read-only\n'
                   b'capabilities: [read]\n---\nbody\n')
        os.remove(os.path.join(self.dir, 'lander.md'))
        cli.main(['--write', '-C', self.top], io.StringIO())
        self.assertNotIn('lander', self.rows())
        # no summary: the description, its | escaped
        self.assertIn('| extra | light | - | Does \\| extra things. |', read(self.roster).decode('utf-8'))

    def test_a_hand_edit_of_a_table_is_put_back_and_the_prose_is_kept(self):
        text = read(self.roster).decode('utf-8')
        edited = text.replace('| worker | standard |', '| worker | deep |', 1).replace(
            '# Agent roster\n', '# Agent roster\n\nA new owner paragraph.\n', 1)
        write_file(self.roster, edited.encode('utf-8'))
        self.assertEqual(self.check()[0], 1)
        cli.main(['--write', '-C', self.top], io.StringIO())
        self.assertEqual(read(self.roster).decode('utf-8'),
                         text.replace('# Agent roster\n', '# Agent roster\n\nA new owner paragraph.\n', 1))

    def test_the_tier_table_follows_the_host_maps(self):
        maps = dict(MAPS, claude=dict(MAPS['claude'], deep='sonnet'), other={'light': 'x'})
        text = roster.render(read(self.roster).decode('utf-8'), _content(self.top), maps)
        t = tables(text)['tier']
        self.assertEqual(t[0], ['tier', 'claude', 'codex', 'other'])
        self.assertEqual(t[4], ['deep', 'sonnet', MAPS['codex']['deep'], '-'])

    def test_missing_markers_are_usage(self):
        write_file(self.roster, read(self.roster).replace(b'<!-- rendered: roles -->', b'', 1))
        out = io.StringIO()
        self.assertEqual(cli.main(['-C', self.top], out), 2)
        self.assertIn('agent_roster: docs/ROSTER.md: needs exactly one <!-- rendered: roles -->', out.getvalue())
        with self.assertRaises(ContentError):
            roster.render('no regions\n', _content(self.top), MAPS)
        text = read(self.roster).decode('utf-8')
        twice = text.replace('<!-- rendered: roles -->\n', '<!-- rendered: roles -->\n<!-- rendered: roles -->\n', 1)
        with self.assertRaises(ContentError):        # a repeated start marker, though only one region closes
            roster.render(twice, _content(self.top), MAPS)

    def test_crlf_is_kept(self):
        write_file(self.roster, read(self.roster).replace(b'\n', b'\r\n'))
        self.edit_role('records', b'effort: low', b'effort: medium')
        cli.main(['--write', '-C', self.top], io.StringIO())
        data = read(self.roster)
        self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
        self.assertIn(b'| records | standard | medium |', data)

    def test_an_unreadable_roster_is_usage(self):
        os.remove(self.roster)
        out = io.StringIO()
        self.assertEqual(cli.main(['-C', self.top], out), 2)
        self.assertIn('agent_roster: docs/ROSTER.md cannot be read', out.getvalue())


def _content(root):
    return content.Content(roles=content.load_roles(root), skills=())


class ReviewerRuleTest(unittest.TestCase):
    """The "Reviewer model" rule in the dispatch skill's roster.md is prose a coordinator follows by
    matching strings: the builder roles its rows name. Each row names exactly its builders, each a
    role, matched as whole words."""

    RULE_FILE = os.path.join(KIT_ROOT, 'content', 'skills', 'coordinator-dispatch', 'roster.md')

    def rule(self):
        text = read(self.RULE_FILE).decode('utf-8').replace('\r\n', '\n')
        m = re.search(r'^- \*\*Reviewer model\*\*.*?(?=^- )', text, re.S | re.M)
        self.assertIsNotNone(m, 'no "Reviewer model" bullet in roster.md')
        return re.sub(r'\s+', ' ', m.group(0))       # a wrapped line is still one sentence

    STRONG_BUILDERS = {'strong-worker', 'senior-worker', 'deep-worker'}
    LIGHT_BUILDERS = {'worker', 'weak-worker'}

    def test_rule_names_only_what_the_code_prints_and_defines(self):
        rule = self.rule()
        roles = {r.name: r for r in content.load_roles(KIT_ROOT)}
        self.assertIn('light tier', rule)

        def named(text):        # whole words: 'worker' must not match inside 'strong-worker'
            return {n for n in roles if re.search(r'(?<![\w-])%s(?![\w-])' % re.escape(n), text)}

        self.assertIn('The first row that fits:', rule)
        rows_text = rule.split('The first row that fits:', 1)[1].split('A light reviewer', 1)[0]
        rows = [r for r in re.split(r'\s(?=\d\. )', rows_text.strip()) if r]
        self.assertEqual(len(rows), 3, rows)
        # every builder the rows name is a role, and every role they name is one of these builders
        self.assertEqual(named(rows_text), self.STRONG_BUILDERS | self.LIGHT_BUILDERS)
        self.assertEqual(named(rows[0]), self.STRONG_BUILDERS)
        self.assertIn('raise to strong', rows[0])
        self.assertEqual(named(rows[1]), self.LIGHT_BUILDERS)
        self.assertIn('light as pinned', rows[1])
        self.assertTrue(rows[2].startswith('3. Otherwise'), rows[2])
        self.assertEqual(named(rows[2]), set())
        # the rows split builders by their pinned tier
        for name in self.STRONG_BUILDERS:
            self.assertIn(roles[name].tier, ('strong', 'deep'), name)
        for name in self.LIGHT_BUILDERS:
            self.assertIn(roles[name].tier, ('light', 'standard'), name)

    def test_rule_names_the_read_type_at_its_pinned_tier(self):
        """The rule raises the analyst (the read type a review goes to) from the tier its role file
        pins: the rule says `light tier` and the file pins light."""
        rule = self.rule()
        roles = {r.name: r for r in content.load_roles(KIT_ROOT)}
        self.assertIn('analyst', roles)
        self.assertRegex(rule, r'(?<![\w-])analyst(?![\w-])')
        self.assertEqual(roles['analyst'].tier, 'light')
        self.assertIn('%s tier' % roles['analyst'].tier, rule)

    def test_agent_definitions_are_a_strict_landing_path(self):
        from kitlib.gate import strict_defaults
        self.assertIn('hosts/*/shell/agents/**', strict_defaults.STRICT_DEFAULTS)


if __name__ == '__main__':
    unittest.main()
