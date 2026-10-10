"""Records and scopes: the scope language and collision (ported from an earlier
tools/test_backlog.py ScopeGrammar, ScopeIntersection, UncertaintyCollides), the header schema, and
Per-repository-format records round-tripping through parse and render."""
import unittest

from kitlib.backlog import record as rmod
from kitlib.backlog import scope as sc
from kitlib.backlog import store as smod

from tests.backlog.helpers import messages, parse, record_text


class ScopeGrammar(unittest.TestCase):

    def ok(self, member):
        self.assertIsNone(sc.scope_member_error(member), member)

    def bad(self, member):
        self.assertIsNotNone(sc.scope_member_error(member), '%r was accepted' % (member,))

    def test_the_three_legal_shapes(self):
        for m in ('none', 'routegraph/src/graph/PathSearch.java', 'mesh/Mesh.Control', 'link/**'):
            self.ok(m)

    def test_general_globs_are_parse_errors(self):
        for m in ('*.java', '**/Foo.java', 'src/*/main', 'a/[bc]/d', 'a/{b,c}/d', 'a/?.java', '**',
                  'a/**/b'):
            self.bad(m)

    def test_a_comma_separated_list_is_a_parse_error(self):
        self.bad('routegraph/**, link/**')

    def test_paths_are_repo_root_relative_forward_slash(self):
        for m in ('/link/**', 'C:/src/x/link/**', 'link\\src', '../link/**',
                  'link/../worker/**', 'link//src', 'link/', ' link/**', 'link/** ',
                  ''):
            self.bad(m)

    def test_a_value_that_casefolds_into_the_sentinel_is_an_error(self):
        for m in ('NONE', 'None', 'nOnE'):
            self.bad(m)
        self.ok('none/**')
        self.ok('tools/none')


class ScopeIntersection(unittest.TestCase):
    SEARCH = 'routegraph/src/main/java/graph/PathSearch.java'

    def test_glob_over_a_file_beneath_it_collides(self):
        self.assertTrue(sc.members_collide('routegraph/**', self.SEARCH))
        self.assertTrue(sc.members_collide(self.SEARCH, 'routegraph/**'))

    def test_nested_globs_and_a_directory_over_a_file_collide(self):
        self.assertTrue(sc.members_collide('a/**', 'a/b/**'))
        self.assertTrue(sc.members_collide('mesh/Mesh.Control', 'mesh/Mesh.Control/Program.cs'))

    def test_siblings_and_string_prefixes_are_clear(self):
        self.assertFalse(sc.members_collide('link/**', 'worker/**'))
        self.assertFalse(sc.members_collide('Foo.java', 'Foobar.java'))
        self.assertFalse(sc.members_collide('link/**', 'link-tools/**'))

    def test_none_collides_with_nothing_including_itself(self):
        self.assertFalse(sc.members_collide('none', 'link/**'))
        self.assertFalse(sc.members_collide('none', 'none'))

    def test_case_is_folded(self):
        self.assertTrue(sc.members_collide('Link/**', 'link/**'))

    def test_collision_is_symmetric_and_reflexive(self):
        ms = ['link/**', 'worker/**', 'mesh/**', 'mesh/Mesh.Control/**', self.SEARCH, 'Foo.java',
              'Foobar.java', 'none', 'tools/**']
        for a in ms:
            if a != 'none':
                self.assertTrue(sc.members_collide(a, a), a)
            for b in ms:
                self.assertEqual(sc.members_collide(a, b), sc.members_collide(b, a), (a, b))

    def test_an_item_collides_if_any_member_pair_does(self):
        a, b = ['docs/**', 'routegraph/**'], ['worker/**', 'docs/ops/gotchas.md']
        self.assertTrue(sc.scopes_collide(a, b))
        self.assertEqual(sc.colliding_pairs(a, b), [('docs/**', 'docs/ops/gotchas.md')])
        self.assertFalse(sc.scopes_collide(['none'], ['link/**']))


class UncertaintyCollides(unittest.TestCase):
    UNDECIDABLE = ('*.java', '**/Foo.java', 'a/{b,c}', '', '/link/**', 'link\\src',
                   '../link/**', 'routegraph/**, link/**', '**')

    def test_no_undecidable_input_is_ever_reported_clear(self):
        for m in self.UNDECIDABLE:
            for other in ('link/**', 'none', m):
                with self.assertRaises(sc.UndecidableScope, msg=(m, other)):
                    sc.members_collide(m, other)
                with self.assertRaises(sc.UndecidableScope, msg=(other, m)):
                    sc.members_collide(other, m)

    def test_a_bad_member_raises_even_after_a_hit(self):
        with self.assertRaises(sc.UndecidableScope):
            sc.scopes_collide(['link/**', '*.java'], ['link/**'])

    def test_an_empty_scope_set_raises(self):
        for a, b in (([], ['x/**']), (['x/**'], []), ([], [])):
            with self.assertRaises(sc.UndecidableScope):
                sc.scopes_collide(a, b)


class Schema(unittest.TestCase):

    def test_the_minimal_record_is_valid(self):
        self.assertEqual(messages(), [])

    def test_an_unknown_key_and_a_cut_key_are_findings(self):
        for key in ('keys', 'class', 'small_fix_by', 'migrated', 'artefact', 'anchor', 'hold_spent',
                    'value'):
            self.assertTrue(any('unknown key `%s`' % key in m for m in messages(**{key: 'x'})), key)

    def test_kept_keys_are_accepted(self):
        self.assertEqual(messages(files='tools/x.py', files_at='abc123', pin='3'), [])

    def test_required_keys_are_never_defaulted(self):
        for key in ('area', 'priority', 'state', 'scope', 'summary'):
            self.assertTrue(any('`%s` is missing' % key in m for m in messages(drop=(key,))), key)

    def test_single_valued_keys_repeat_is_a_finding(self):
        self.assertTrue(any('single-valued' in m for m in messages(effort=['s', 'm'])))

    def test_cross_field_rules(self):
        self.assertTrue(messages(priority='2'))                       # band with priority_by -
        self.assertTrue(messages(priority='2', priority_by='pm'))      # pm without its ruling
        self.assertTrue(messages(state='blocked'))                    # blocked on nothing
        self.assertTrue(messages(state='pulled'))                     # no pulled_at / pulled_by
        self.assertTrue(messages(blocked_on='owner', state='blocked'))  # no owner_ask
        self.assertEqual(messages(blocked_on='owner', state='blocked', owner_ask='may we?'), [])
        self.assertTrue(messages(scope=['none', 'x/**']))

    def test_the_blocked_on_closed_set(self):
        rules = rmod.Rules('upper', ('L1',))
        self.assertEqual(messages(rules=rules, state='blocked', blocked_on='phase:L1'), [])
        self.assertTrue(messages(rules=rules, state='blocked', blocked_on='phase:L9'))
        self.assertTrue(messages(rules=rules, state='blocked', blocked_on='whenever'))

    def test_the_filename_is_the_id(self):
        r = rmod.parse('backlog/BAR.md', record_text(), 'BAR')
        self.assertTrue(any(f.check == rmod.ID_FILENAME for f in rmod.validate(r, rmod.Rules())))

    def test_structural_problems_make_a_record_unreadable(self):
        for text in ('', '\nid: FOO\n', 'id FOO\n', 'Id: FOO\n'):
            self.assertFalse(rmod.parse('backlog/FOO.md', text, 'FOO').readable, repr(text))
        self.assertFalse(smod.parse_blob('FOO', b'\xff\xfe', 'backlog/FOO.md').readable)


REPO_RECORD = '''id: EX-STORE-STEP
area: artifacts
priority: 1
priority_by: owner
priority_ruling: R-9001
priority_per: R-9002
effort: s
state: blocked
blocked_on: item:EX-OBTAIN
blocked_on: live-client
scope: artifacts/plans/definitions/EX-STORE-PLAN/**
scope: artifacts/scripts/scripts/src/main/java/org/example/scripts/shop/**
files: artifacts/scripts/Shop.java
files_at: 0123456789abcdef
requires: item:EX-OBTAIN
requires: the intro step lands first
pin: 7
session: gamma
ref: PM R-9001, R-9002; docs/reference/API_CALLS.md section 7
summary: setup store step as obtain(coins) + buy(tool) + pickUp(item) (PM R-9002)

## Why this is here

Task 15 of docs/reference/API_CALLS.md section 7 (PM R-9001).

## The step

- A store plan step before the crafting step.
'''


class RepoFormat(unittest.TestCase):

    def test_a_repo_record_parses_validates_and_round_trips(self):
        rules = rmod.Rules('upper', ('L0', 'L1'))
        rec = smod.parse_blob('EX-STORE-STEP', REPO_RECORD.encode('utf-8'),
                              'backlog/EX-STORE-STEP.md')
        self.assertTrue(rec.readable)
        self.assertEqual(rmod.validate(rec, rules), [])
        self.assertEqual(rec.all('blocked_on'), ['item:EX-OBTAIN', 'live-client'])
        self.assertEqual(rmod.render(rec.pairs(), rec.body), REPO_RECORD)

    def test_crlf_and_a_bom_still_parse(self):
        data = b'\xef\xbb\xbf' + REPO_RECORD.replace('\n', '\r\n').encode('utf-8')
        rec = smod.parse_blob('EX-STORE-STEP', data, 'x')
        self.assertTrue(rec.readable, rec.structural)
        self.assertEqual(rec.get('id'), 'EX-STORE-STEP')

    def test_header_order_is_kept_on_a_change(self):
        rec = parse(summary='first', area='xx')
        pairs = rmod.apply_changes(rec.pairs(), [('effort', ['m']), ('pin', ['2'])])
        keys = [k for k, _ in pairs]
        self.assertEqual(keys[:5], ['id', 'area', 'priority', 'priority_by', 'effort'])
        self.assertLess(keys.index('pin'), keys.index('summary'))


if __name__ == '__main__':
    unittest.main()
