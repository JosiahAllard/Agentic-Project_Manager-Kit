"""The java-imports detector and its ratchets (docs/module-map.md "Detectors")."""
import unittest

from kitlib.modulemap import check as C
from kitlib.modulemap.detectors import java_imports
from kitlib.modulemap.load import ModuleMap

from tests.modulemap._fixtures import Tree

BASE = 'shared/api/src/main/java/org/r/'
LAYER = {'id': 'api-internal', 'from': 'api', 'to': 'api', 'goal': 0, 'detector': 'java-imports',
         'files': ['shared/api/src/main/java/org/r/api/*'], 'imports': ['org.r.internal']}


def mapped(*ratchets, **extra):
    data = {'modules': {'api': {'paths': ['shared/api']}, 'proto': {'paths': ['proto']}},
            'ratchets': list(ratchets)}
    data.update(extra)
    return ModuleMap(data)


class JavaImportsTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree({
            BASE + 'api/A.java': 'package org.r.api;\nimport org.r.internal.X;\n'
                                 'import org.r.internal.Y;\n',
            BASE + 'api/B.java': 'package org.r.api;\n\nimport static org.r.internal.Z.go;\n',
            BASE + 'api/C.java': 'package org.r.api;\nimport org.r.internalish.Q;\n'
                                 '// org.r.internal.X in a comment\n',
            BASE + 'api/sub/D.java': 'import org.r.internal.*;\n',
            BASE + 'internal/X.java': 'import org.r.api.A;\n',
            BASE + 'api/notes.txt': 'import org.r.internal.X;\n',
        })
        self.addCleanup(self.t.cleanup)

    def found(self, mm):
        return [(c.path.rsplit('/', 1)[-1], c.line, c.target, c.ratchet)
                for c in java_imports.detect(self.t.files(), mm, self.t.read())]

    def test_each_file_with_a_matching_import_counts_once(self):
        self.assertEqual(self.found(mapped(LAYER)), [
            ('A.java', 2, 'import org.r.internal.X;', 'api-internal'),
            ('B.java', 3, 'import static org.r.internal.Z.go;', 'api-internal'),
            ('D.java', 1, 'import org.r.internal.*;', 'api-internal')])

    def test_a_prefix_matches_a_whole_name_only(self):
        mm = mapped(dict(LAYER, imports=['org.r.internal.']))
        self.assertNotIn('C.java', [f for f, *_ in self.found(mm)])
        self.assertEqual(len(self.found(mm)), 3)

    def test_two_ratchets_with_the_same_ends_count_apart(self):
        other = dict(LAYER, id='internal-api', files=[BASE + 'internal/*'], imports=['org.r.api'])
        s = C.sort_crossings(mapped(LAYER, other),
                             C.scan(mapped(LAYER, other), self.t.files(), self.t.read()))
        self.assertEqual(len(s.ratcheted['api-internal']), 3)
        self.assertEqual(len(s.ratcheted['internal-api']), 1)
        self.assertEqual(s.refused, [])

    def test_the_check_holds_the_count_to_the_ceiling_even_into_messaging(self):
        r = dict(LAYER, to='proto')
        mm = mapped(r, messaging=['proto'])
        out = C.check(mm, {'api-internal': 2}, self.t.files(), self.t.read())
        self.assertEqual(out.code, C.FAIL, out.lines)
        self.assertIn('ratchet api-internal  api -> proto  count 3 / ceiling 2 / goal 0  '
                      'OVER CEILING', out.lines)
        self.assertEqual(C.check(mm, {'api-internal': 3}, self.t.files(), self.t.read()).code, C.OK)


if __name__ == '__main__':
    unittest.main()
