"""kitlib.hubvalidate, claude/hub/hub_validate.py, and the validation lines of hub_mirror save."""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import hubmirror, hubvalidate as V

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


class CliTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'hub_validate_tool', os.path.join(KIT, 'claude', 'hub', 'hub_validate.py'))
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.d = tmpdir(self)

    def run_tool(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            try:
                code = self.tool.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def hub(self, **colls):
        for c, docs in colls.items():
            for i, d in docs.items():
                write_json(os.path.join(self.d, 'hub', c, i + '.json'), d)
        return os.path.join(self.d, 'hub')

    def test_usage_errors_exit_2(self):
        for argv in ([], ['--dir', 'x', '--batch', 'y'], ['--dir', 'x', '--against', 'y']):
            self.assertEqual(self.run_tool(argv)[0], 2, msg=argv)
        self.assertEqual(self.run_tool(['--dir', os.path.join(self.d, 'none')])[0], 2)
        self.assertEqual(self.run_tool(['--batch', os.path.join(self.d, 'none.json')])[0], 2)

    def test_dir_clean_warn_strict_and_error(self):
        clean = self.hub(todos={'T-1': todo()})
        self.assertEqual(self.run_tool(['--dir', clean])[0], 0)
        shutil.rmtree(clean)
        warn = self.hub(todos={'T-1': todo(commands=[])})
        code, out, _ = self.run_tool(['--dir', warn])
        self.assertEqual(code, 0)
        self.assertIn('handstep-commands', out)
        self.assertEqual(self.run_tool(['--dir', warn, '--strict'])[0], 1)
        shutil.rmtree(warn)
        bad = self.hub(todos={'T-1': todo(commands='dir')})
        code, out, _ = self.run_tool(['--dir', bad])
        self.assertEqual(code, 1)
        self.assertIn('1 error(s), 0 warning(s)', out)

    def test_dir_judges_repo_tags_against_repos(self):
        h = self.hub(todos={'T-1': todo(repo='old')}, repos={'pm': {'name': 'pm', 'aliases': ['old']}})
        self.assertEqual(self.run_tool(['--dir', h, '--strict'])[0], 0)

    def test_batch_with_a_non_object_pages_document_is_an_error_not_a_traceback(self):
        batch = os.path.join(self.d, 'b.json')
        write_json(batch, [{'op': 'set', 'collection': 'pages', 'doc_id': 'p1', 'data': 'text'},
                           {'op': 'set', 'collection': 'page_html', 'doc_id': 'p1', 'data': 'text'}])
        code, out, _ = self.run_tool(['--batch', batch])
        self.assertEqual(code, 1)
        self.assertIn('ERROR pages/p1', out)
        self.assertIn('ERROR page_html/p1', out)

    def test_batch_without_against_does_not_judge_page_html_ids(self):
        batch = os.path.join(self.d, 'b.json')
        write_json(batch, [{'op': 'set', 'collection': 'pages', 'doc_id': 'p1', 'data': {'title': 't'}},
                           {'op': 'set', 'collection': 'page_html', 'doc_id': 'p2',
                            'data': {'html': '<p>'}}])
        code, out, _ = self.run_tool(['--batch', batch, '--strict'])
        self.assertEqual(code, 0, out)
        self.assertIn('page_html ids not judged', out)
        against = self.hub(pages={'p1': {'title': 't'}})
        code, out, _ = self.run_tool(['--batch', batch, '--against', against, '--strict'])
        self.assertEqual(code, 1)
        self.assertIn('no pages/p2', out)

    def test_batch_with_and_without_against(self):
        batch = os.path.join(self.d, 'b.json')
        write_json(batch, [{'op': 'set', 'collection': 'todos', 'doc_id': 'T-1',
                            'data': todo(refs=['Q-9'])}])
        code, out, _ = self.run_tool(['--batch', batch, '--strict'])
        self.assertEqual(code, 0)
        self.assertNotIn('ref-dangling: ', out.replace('[ref-dangling]', ''))
        self.assertIn('not judged: no --against', out)
        against = self.hub(questions={'Q-1': question()})
        code, out, _ = self.run_tool(['--batch', batch, '--against', against, '--strict'])
        self.assertEqual(code, 1)
        self.assertIn('todos/T-1 refs', out)


class SaveTest(unittest.TestCase):
    """hub_mirror save prints the findings (read_source notes) and saves anyway."""

    def setUp(self):
        self.d = tmpdir(self)
        self.src = os.path.join(self.d, 'src')
        write_json(os.path.join(self.src, 'todos', 'T-1.json'), todo(commands='Get-ChildItem'))

    def test_read_source_notes_the_findings(self):
        _, notes = hubmirror.read_source(self.src)
        self.assertTrue(any(n.startswith('VALIDATE ERROR todos/T-1 commands') for n in notes), notes)
        self.assertIn('VALIDATE 1 error(s), 0 warning(s) (saved anyway)', notes)

    def test_a_crashing_check_never_aborts_the_save(self):
        spec = importlib.util.spec_from_file_location('hub_mirror_tool',
                                                      os.path.join(KIT, 'tools', 'hub_mirror.py'))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        mirror = os.path.join(self.d, 'mirror')
        out = io.StringIO()
        with mock.patch('sys.stdout', out), \
                mock.patch.object(hubmirror.hubvalidate, 'validate', side_effect=KeyError('boom')), \
                mock.patch.object(tool, '_settings', return_value=(None, {'hub_mirror': mirror})):
            code = tool.main(['save', '--from', self.src])
        self.assertEqual(code, 0)
        self.assertIn('VALIDATE skipped:', out.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(mirror, 'todos', 'T-1.json')))

    def test_a_non_object_pages_document_is_not_object_not_a_crash(self):
        f = V.validate({'pages': {'p': 'x'}, 'page_html': {'p': 3}})
        self.assertEqual(codes(f), [('page_html/p', 'not-object'), ('pages/p', 'not-object')])

    def test_clean_source_adds_no_notes(self):
        write_json(os.path.join(self.src, 'todos', 'T-1.json'), todo())
        self.assertEqual(hubmirror.read_source(self.src)[1], [])

    def test_repos_folder_is_read_for_aliases_but_not_mirrored(self):
        write_json(os.path.join(self.src, 'todos', 'T-1.json'), todo(repo='old'))
        write_json(os.path.join(self.src, 'repos', 'pm.json'), {'name': 'pm', 'aliases': ['old']})
        found, notes = hubmirror.read_source(self.src)
        self.assertEqual(sorted(found), ['todos'])
        self.assertEqual([n for n in notes if n.startswith('VALIDATE')], [])

    def test_the_tool_save_prints_findings_and_still_writes_exit_0(self):
        spec = importlib.util.spec_from_file_location('hub_mirror_tool',
                                                      os.path.join(KIT, 'tools', 'hub_mirror.py'))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        mirror = os.path.join(self.d, 'mirror')
        out = io.StringIO()
        with mock.patch('sys.stdout', out), \
                mock.patch.object(tool, '_settings', return_value=(None, {'hub_mirror': mirror})):
            code = tool.main(['save', '--from', self.src])
        self.assertEqual(code, 0)
        self.assertIn('VALIDATE ERROR todos/T-1 commands', out.getvalue())
        self.assertIn('added todos/T-1.json', out.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(mirror, 'todos', 'T-1.json')))


if __name__ == '__main__':
    unittest.main()
