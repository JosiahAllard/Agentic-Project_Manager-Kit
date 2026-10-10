"""The risk map's glob forms: brace globs, mid-name `*`, `**`, bare file names, plain prefixes."""
import unittest

from kitlib import reviewpack


def rules(*bodies):
    lines = ['- %s x | %s\n' % (lv, body) for lv, body in bodies]
    return reviewpack.rules_from_lines(lines, lambda tok: False)


def lv(path, *bodies):
    return reviewpack.level(path, rules(*bodies))


class ParsingKeepsGlobs(unittest.TestCase):
    def test_each_form_is_kept_whole(self):
        self.assertEqual(
            rules(('High', 'ui/*.gdshader, src/{a,b}/x.py, **/*.tscn, Wire.java, net/')),
            [('High', 'ui/*.gdshader'), ('High', 'src/{a,b}/x.py'), ('High', '**/*.tscn'),
             ('High', 'Wire.java'), ('High', 'net')])

    def test_a_comma_inside_braces_does_not_split_the_rule(self):
        self.assertEqual(rules(('Low', 'docs/*.{md,txt}, a/b.py')),
                         [('Low', 'docs/*.{md,txt}'), ('Low', 'a/b.py')])


class Matching(unittest.TestCase):
    def test_a_star_extension_under_a_dir(self):
        self.assertEqual(lv('ui/x.gdshader', ('Medium', 'ui/*.gdshader')), 'Medium')
        self.assertEqual(lv('ui/x.gd', ('Medium', 'ui/*.gdshader')), 'unlisted (= High)')
        self.assertEqual(lv('ui/sub/x.gdshader', ('Medium', 'ui/*.gdshader')),
                         'unlisted (= High)')

    def test_a_mid_name_star(self):
        self.assertEqual(lv('src/NetWire.java', ('High', 'src/Net*.java')), 'High')
        self.assertEqual(lv('src/Other.java', ('High', 'src/Net*.java')), 'unlisted (= High)')

    def test_brace_globs(self):
        body = ('Low', 'docs/*.{md,txt}')
        self.assertEqual(lv('docs/a.txt', body), 'Low')
        self.assertEqual(lv('docs/a.md', body), 'Low')
        self.assertEqual(lv('docs/a.py', body),'unlisted (= High)')
        self.assertEqual(lv('b/x.py', ('Medium', '{a,b}/x.py')), 'Medium')

    def test_double_star_crosses_directories(self):
        self.assertEqual(lv('a/b/c/x.tscn', ('Medium', '**/*.tscn')), 'Medium')
        self.assertEqual(lv('x.tscn', ('Medium', '**/*.tscn')), 'Medium')
        self.assertEqual(lv('a/b/c.py', ('Low', 'a/**')), 'Low')

    def test_equally_specific_rules_resolve_to_the_higher_level_in_either_order(self):
        for order in ([('Low', 'a/b.py'), ('High', 'a/b.py')],
                      [('High', 'a/b.py'), ('Low', 'a/b.py')]):
            self.assertEqual(lv('a/b.py', *order), 'High')

    def test_a_plain_bare_name_is_a_root_prefix_only(self):
        self.assertEqual(lv('project.godot', ('High', 'project.godot')), 'High')
        self.assertEqual(lv('deep/project.godot', ('Low', 'project.godot')),
                         'unlisted (= High)')
        self.assertEqual(lv('net/config.json', ('High', 'net/'), ('Low', 'config.json')), 'High')

    def test_a_bare_glob_matches_at_any_depth(self):
        self.assertEqual(lv('a/b/x.gdshader', ('Medium', '*.gdshader')), 'Medium')
        self.assertEqual(lv('a/b/x.gd', ('Medium', '*.gdshader')), 'unlisted (= High)')

    def test_a_plain_prefix_still_matches_as_a_prefix(self):
        self.assertEqual(lv('net/x.py', ('High', 'net/')), 'High')
        self.assertEqual(lv('network/x.py', ('High', 'net/')), 'High')

    def test_the_more_specific_rule_wins_across_forms(self):
        body = [('High', 'ui/'), ('Low', 'ui/*.md')]
        self.assertEqual(reviewpack.level('ui/a.md', rules(*body)), 'Low')
        self.assertEqual(reviewpack.level('ui/a.gd', rules(*body)), 'High')


if __name__ == '__main__':
    unittest.main()
