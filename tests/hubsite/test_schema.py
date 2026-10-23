"""kitlib.hubsite.schema: required fields, types and ids."""
import datetime
import unittest

from kitlib.hubsite import approvals, schema


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
        self.assertEqual(schema.ID_PREFIX['approvals'], 'A')

    def test_an_approval_request_passes_and_needs_its_head(self):
        doc = approvals.new_doc('https://github.com/o/r', 't', 'a' * 40,
                                [{'path': 'x.md', 'base_blob': None, 'content': 'x\n'}],
                                datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc))
        self.assertEqual(schema.check('approvals', doc), [])
        doc.pop('head')
        self.assertEqual(codes(schema.check('approvals', doc)), [('head', 'schema-field')])
        doc.pop('files')
        self.assertEqual(codes(schema.check('approvals', doc)),
                         [('head', 'schema-field'), ('files', 'schema-field')])

    OP = {'status': 'pending', 'kind': 'op', 'op': 'D-001', 'repo': '*', 'title': 't',
          'line': 'tool.exe --x 1', 'args_sha256': 'a' * 64, 'what': 'w',
          'created_at': '2026-01-02T00:00:00+00:00'}

    def test_an_op_approval_passes_without_head_or_files_and_needs_its_own_fields(self):
        self.assertEqual(schema.check('approvals', dict(self.OP)), [])
        for field in ('op', 'line', 'args_sha256', 'repo', 'title', 'created_at'):
            doc = dict(self.OP)
            doc.pop(field)
            self.assertEqual(codes(schema.check('approvals', doc)), [(field, 'schema-field')],
                             field)

    def test_an_approval_with_no_kind_still_needs_head_and_files(self):
        doc = dict(self.OP)
        doc.pop('kind')
        self.assertEqual(codes(schema.check('approvals', doc)),
                         [('head', 'schema-field'), ('files', 'schema-field')])

    def test_an_unknown_approval_kind_is_an_error(self):
        for kind in ('files', 'OP', '', ' op'):
            self.assertEqual(codes(schema.check('approvals', dict(self.OP, kind=kind))),
                             [('kind', 'schema-field')], kind)
        self.assertEqual(codes(schema.check('approvals', dict(self.OP, kind=['op']))),
                         [('kind', 'schema-field')])


class IdTest(unittest.TestCase):
    def test_ids(self):
        for ok in ('Q-001', 'S-12', 'alpha', 'page.v2'):
            self.assertIsNone(schema.check_id(ok), ok)
        for bad in ('', '../x', 'a/b', '.hidden', 'CON', 'nul.txt', 'com1', 'x.', 7):
            self.assertIsNotNone(schema.check_id(bad), bad)


if __name__ == '__main__':
    unittest.main()
