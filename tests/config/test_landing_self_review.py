"""kitlib.config.schema: "landing".self_review_max_lines, the most changed lines a verdict the
queueing coordinator reviewed itself may cover (kitlib.gate.verdicts.self_review_size_why):
unset by default, a whole number from 1 to 100000 when set."""
import unittest

from kitlib import config
from kitlib.config import schema
from tests.config.helpers import TempTree


class SelfReviewMaxLines(unittest.TestCase):

    def load(self, kit=None):
        t = TempTree(kit)
        self.addCleanup(t.cleanup)
        return config.load(t.root)

    def refused(self, kit):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(kit)
        return str(cm.exception)

    def test_unset_by_default(self):
        self.assertIsNone(self.load().section('landing')['self_review_max_lines'])

    def test_a_whole_number_loads(self):
        cfg = self.load({'landing': {'self_review_max_lines': 150}})
        self.assertEqual(cfg.section('landing')['self_review_max_lines'], 150)

    def test_zero_a_string_or_a_bool_is_a_config_error(self):
        for bad in (0, -1, 100001, '150', True, 1.5):
            with self.subTest(value=bad):
                self.assertIn('"landing".self_review_max_lines',
                              self.refused({'landing': {'self_review_max_lines': bad}}))

    def test_the_doc_line_names_it(self):
        self.assertTrue([l for l in schema.doc_lines()
                         if l.startswith('- `landing.self_review_max_lines` ')])


if __name__ == '__main__':
    unittest.main()
