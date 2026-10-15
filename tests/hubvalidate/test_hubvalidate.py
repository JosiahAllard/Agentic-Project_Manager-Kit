"""kitlib.hubvalidate: the findings over a corpus of hub documents."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import hubvalidate as V


def tmpdir(test):
    d = tempfile.mkdtemp(prefix='kit-hubvalidate-')
    test.addCleanup(shutil.rmtree, d, True)
    return d


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f)


def codes(findings, level=None):
    return [(f.rel, f.code) for f in findings if level is None or f.level == level]


def question(**kw):
    d = {'status': 'open', 'repo': 'pm', 'title': 't', 'options': [{'label': 'a', 'description': 'b'}],
         'recommended': 0}
    d.update(kw)
    return d


def todo(**kw):
    d = {'status': 'open', 'title': 't', 'commands': ['dir'], 'repo': 'pm'}
    d.update(kw)
    return d


class DoneWhenTest(unittest.TestCase):
    def test_a_string_commands_is_an_error(self):
        f = V.validate({'todos': {'T-1': todo(commands='Get-ChildItem')}})
        self.assertEqual(codes(f, V.ERROR), [('todos/T-1', 'commands-type')])

    def test_a_ref_to_a_missing_q_id_is_flagged(self):
        docs = {'questions': {'Q-906': question()},
                'todos': {'T-q910-patch': todo(refs=['Q-910'])}}
        self.assertEqual(codes(V.validate(docs)), [('todos/T-q910-patch', 'ref-dangling')])
        docs['questions']['Q-910'] = question()
        self.assertEqual(V.validate(docs), [])

    def test_a_ref_into_a_collection_that_was_not_read_is_not_judged(self):
        self.assertEqual(V.validate({'todos': {'T-1': todo(refs=['Q-910'])}}), [])

    def test_a_ref_to_a_missing_story_id_is_flagged(self):
        docs = {'stories': {'S-001': {'status': 'submitted', 'repo': 'pm', 'title': 't'}},
                'todos': {'T-1': todo(refs=['S-002'])}}
        self.assertEqual(codes(V.validate(docs)), [('todos/T-1', 'ref-dangling')])
        docs['todos']['T-1']['refs'] = ['S-001']
        self.assertEqual(V.validate(docs), [])

    def test_an_unaliased_repo_tag_is_flagged_until_aliased(self):
        docs = {'questions': {'Q-1': question(repo='pm-kit')},
                'repos': {'pm': {'name': 'pm', 'added_at': 'x'}}}
        self.assertEqual(codes(V.validate(docs)), [('questions/Q-1', 'repo-alias')])
        docs['repos']['pm']['aliases'] = ['pm-kit']
        self.assertEqual(V.validate(docs), [])
        docs['questions']['Q-1']['repo'] = 'PM-KIT'
        self.assertEqual(V.validate(docs), [])

    def test_no_repos_collection_skips_the_alias_check_with_a_note(self):
        f = V.validate({'questions': {'Q-1': question(repo='pm-kit')}})
        self.assertEqual([(x.level, x.code) for x in f], [(V.NOTE, 'repo-alias')])
        self.assertEqual(V.validate({'questions': {'Q-1': question(repo='all')}}), [])


    def test_configured_repo_names_are_known_without_a_stored_repos_doc(self):
        docs = {'questions': {'Q-1': question(repo='gamerepo')},
                'repos': {'pm': {'name': 'pm', 'added_at': 'x'}}}
        self.assertEqual(codes(V.validate(docs)), [('questions/Q-1', 'repo-alias')])
        self.assertEqual(V.validate(docs, known_repos=['Gamerepo']), [])
        self.assertEqual(codes(V.validate(docs, known_repos=['other'])),
                         [('questions/Q-1', 'repo-alias')])

    def test_configured_repo_names_also_cover_a_store_with_no_repos_collection(self):
        docs = {'questions': {'Q-1': question(repo='gamerepo')}}
        self.assertEqual(V.validate(docs, known_repos=['gamerepo']), [])
        self.assertEqual(codes(V.validate(docs, known_repos=['other'])),
                         [('questions/Q-1', 'repo-alias')])

    def test_configured_repo_names_given_as_a_callable_are_asked_only_for_a_checked_tag(self):
        asked = []

        def names():
            asked.append(1)
            return ['gamerepo']

        docs = {'questions': {'Q-1': question(repo='gamerepo'), 'Q-2': question(repo='ghost')},
                'todos': {'T-1': {'status': 'open', 'title': 't', 'from': 'you'}},
                'repos': {}}
        self.assertEqual(V.validate(docs, only={'todos/T-1'}, known_repos=names), [])
        self.assertEqual(asked, [], 'no checked document has a repo tag')
        self.assertEqual(V.validate(docs, only={'questions/Q-1'}, known_repos=names), [])
        self.assertEqual(codes(V.validate(docs, known_repos=names)),
                         [('questions/Q-2', 'repo-alias')])
        self.assertEqual(len(asked), 2)


class CheckTest(unittest.TestCase):
    def test_commands_must_be_a_list_of_strings(self):
        for bad in ({'a': 1}, [1], [''], None):
            f = V.validate({'todos': {'T-1': todo(commands=bad)}})
            self.assertIn(('todos/T-1', 'commands-type'), codes(f, V.ERROR), msg=bad)

    def test_statuses(self):
        f = V.validate({'questions': {'Q-1': question(status='done'), 'Q-2': question(status=None)},
                        'stories': {'S': {'status': 'added'}}, 'to_pm': {'P-001': {'status': 'sent'}}})
        self.assertEqual(codes(f), [('questions/Q-1', 'status-unknown'),
                                    ('questions/Q-2', 'status-missing')])
        self.assertEqual(f[0].level, V.ERROR)
        self.assertEqual(f[1].level, V.WARN)

    def test_options_and_recommended(self):
        bad = [question(options='x'), question(options=[{'description': 'no label'}]),
               question(recommended=5), question(recommended=True), question(recommended='0')]
        for i, d in enumerate(bad):
            self.assertIn(('questions/Q-1', 'options-shape'),
                          codes(V.validate({'questions': {'Q-1': d}}), V.ERROR), msg=i)
        self.assertEqual(codes(V.validate({'questions': {'Q-1': question(refs='R-1')}}), V.WARN),
                         [('questions/Q-1', 'options-shape')])
        self.assertEqual(V.validate({'questions': {'Q-1': question(refs=['R-1'])}}), [])

    def test_a_hand_step_todo_without_commands_warns(self):
        for d in (todo(commands=[]), {'status': 'open', 'repo': 'pm'}):
            self.assertEqual(codes(V.validate({'todos': {'T-1': d}})), [('todos/T-1', 'handstep-commands')])
        self.assertEqual(V.validate({'todos': {'T-1': todo(commands=[], **{'from': 'you'})}}), [])
        self.assertEqual(V.validate({'todos': {'T-1': todo(commands=[], status='done')}}), [])

    def test_inbox_and_parked_refs(self):
        docs = {'questions': {}, 'todos': {}, 'inbox': {'M-1': {'ref': 'Q-5', 'refs': ['R-1']}},
                'parked': {'x': {'q': 'Q-6', 'repo': 'pm'}}}
        self.assertEqual(codes(V.validate(docs)), [('inbox/M-1', 'ref-dangling'),
                                                   ('parked/x', 'ref-dangling')])

    def test_pages(self):
        docs = {'pages': {'p1': {'title': 'ok'}, 'p2': {}}, 'page_html': {'p1': {'html': '<p>'},
                                                                           'p3': {'html': 1}}}
        self.assertEqual(codes(V.validate(docs)), [('page_html/p3', 'pages-schema'),
                                                   ('page_html/p3', 'pages-schema'),
                                                   ('pages/p2', 'pages-schema')])

    def test_a_clean_corpus_and_a_non_object(self):
        self.assertEqual(V.validate({'questions': {'Q-1': question()}, 'todos': {'T-1': todo()}}), [])
        self.assertEqual(codes(V.validate({'todos': {'T-1': 'x'}})), [('todos/T-1', 'not-object')])

    def test_a_non_object_pages_document_is_not_object_not_a_crash(self):
        f = V.validate({'pages': {'p': 'x'}, 'page_html': {'p': 3}})
        self.assertEqual(codes(f), [('page_html/p', 'not-object'), ('pages/p', 'not-object')])

    def test_only_limits_findings_not_the_judging(self):
        docs = {'questions': {'Q-1': question(status='bad')}, 'todos': {'T-1': todo(refs=['Q-9'])}}
        self.assertEqual(codes(V.validate(docs, only={'todos/T-1'})), [('todos/T-1', 'ref-dangling')])

    def test_summary_and_line(self):
        f = V.validate({'todos': {'T-1': todo(commands='x', refs=['Q-1'])}, 'questions': {}})
        self.assertEqual(V.summary(f), '1 error(s), 1 warning(s)')
        self.assertIn('ERROR todos/T-1 commands:', V.format_line(f[0]))


class BatchTest(unittest.TestCase):
    def test_batch_docs_reads_data_and_files_and_skips_updates(self):
        d = tmpdir(self)
        write_json(os.path.join(d, 'a.json'), {'status': 'open'})
        write_json(os.path.join(d, 'w.json'), {'data': {'status': 'done'}, 'id': 'x'})
        docs, bad = V.batch_docs([
            {'op': 'set', 'collection': 'todos', 'doc_id': 'T-1', 'data': {'status': 'open'}},
            {'op': 'set', 'collection': 'todos', 'doc_id': 'T-2', 'file_path': 'a.json'},
            {'op': 'set', 'collection': 'todos', 'doc_id': 'T-3', 'file_path': 'w.json'},
            {'op': 'set', 'collection': 'todos', 'doc_id': 'T-4', 'file_path': 'gone.json'},
            {'op': 'update', 'collection': 'todos', 'doc_id': 'T-5', 'data': {}}], d)
        self.assertEqual(sorted(docs['todos']), ['T-1', 'T-2', 'T-3'])
        self.assertEqual(docs['todos']['T-3'], {'status': 'done'})
        self.assertEqual(codes(bad), [('todos/T-4', 'unreadable')])


if __name__ == '__main__':
    unittest.main()
