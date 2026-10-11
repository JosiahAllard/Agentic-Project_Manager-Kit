"""kitlib.hubsite.schema: required fields, types and ids."""
import unittest

from kitlib.hubsite import schema


def codes(findings):
    return [(f.field, f.code) for f in findings]


class CheckTest(unittest.TestCase):
    def test_a_complete_question_passes(self):
        self.assertEqual(schema.check('questions', {'status': 'open', 'title': 't', 'urgent': False,
                                                    'recommended': 0, 'refs': []}), [])

    def test_a_missing_or_blank_required_field_is_an_error(self):
        self.assertEqual(codes(schema.check('stories', {'status': 'submitted', 'repo': 'pm',
                                                        'title': ' '})),
                         [('title', 'schema-field'), ('story', 'schema-field')])

    def test_a_wrong_type_is_an_error_and_a_bool_is_not_an_int(self):
        f = schema.check('questions', {'status': 'open', 'title': 't', 'recommended': True,
                                       'urgent': 'yes'})
        self.assertEqual(codes(f), [('recommended', 'schema-field'), ('urgent', 'schema-field')])

    def test_null_optional_fields_pass(self):
        self.assertEqual(schema.check('to_pm', {'status': 'queued', 'kind': 'fyi', 'text': 'x',
                                                'sent_at': None, 'reply': None}), [])

    def test_an_unknown_to_pm_kind_is_an_error(self):
        self.assertEqual(codes(schema.check('to_pm', {'status': 'queued', 'kind': 'rant',
                                                      'text': 'x'})), [('kind', 'schema-field')])

    def test_an_unknown_collection_is_an_error(self):
        self.assertEqual(len(schema.check('parked', {})), 1)

    def test_story_ids_come_from_hubvalidate(self):
        self.assertEqual(schema.ID_PREFIX['stories'], 'S')
        self.assertEqual(schema.ID_PREFIX['questions'], 'Q')


class IdTest(unittest.TestCase):
    def test_ids(self):
        for ok in ('Q-001', 'S-12', 'alpha', 'page.v2'):
            self.assertIsNone(schema.check_id(ok), ok)
        for bad in ('', '../x', 'a/b', '.hidden', 'CON', 'nul.txt', 'com1', 'x.', 7):
            self.assertIsNotNone(schema.check_id(bad), bad)


if __name__ == '__main__':
    unittest.main()
