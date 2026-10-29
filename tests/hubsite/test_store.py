"""kitlib.hubsite.store: the file store, its single-writer lock, ids, revision, events, pending and
validation on write, and a crash in the middle of a write."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.hubsite import approvals, store
from kitlib.hubsite.store import HubError, Store, StoreBusy
from kitlib.proc import spawn

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLOCK = lambda: '2026-01-02T03:04:05+00:00'      # noqa: E731

# A child that holds the store until told on stdin: `close` closes it, `die` exits without.
HOLDER = '''
import os, sys
from kitlib.hubsite import store
s = store.Store.open(sys.argv[1])
print('READY', flush=True)
if sys.stdin.readline().strip() == 'die':
    os._exit(0)
s.close()
'''
# A child that dies at its argv[2]-th os.replace in the middle of an add or update (argv[3]).
CRASH = '''
import os, sys
from kitlib.hubsite import store
s = store.Store.open(sys.argv[1])
n, real, calls = int(sys.argv[2]), os.replace, [0]
def fake(a, b):
    calls[0] += 1
    if calls[0] == n:
        os._exit(3)
    real(a, b)
os.replace = fake
if sys.argv[3] == 'add':
    s.add('questions', {'status': 'open', 'title': 'second'})
else:
    s.update('questions', 'Q-1000', {'title': 'changed'})
'''


def tmpdir(test):
    d = tempfile.mkdtemp(prefix='kit-hubsite-')
    test.addCleanup(shutil.rmtree, d, True)
    return d


def child_env():
    env = dict(os.environ)
    env['PYTHONPATH'] = KIT
    return env


def question(**kw):
    d = {'status': 'open', 'title': 't', 'repo': 'pm',
         'options': [{'label': 'a', 'description': 'b'}], 'recommended': 0}
    d.update(kw)
    return d


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def read_file(path):
    with open(path, encoding='utf-8', newline='') as f:
        return f.read()


def tmp_files(root):
    return [os.path.join(d, n) for d, _, names in os.walk(root) for n in names
            if n.endswith('.tmp')]


class StoreCase(unittest.TestCase):
    def open(self, root=None):
        self.root = root or getattr(self, 'root', None) or tmpdir(self)
        s = Store.open(self.root, clock=CLOCK)
        self.addCleanup(s.close)
        return s


class FormatTest(StoreCase):
    def test_a_document_file_is_the_mirror_text_plus_version(self):
        s = self.open()
        s.put('pages', 'home', {'title': 'Café'})
        self.assertEqual(read_file(os.path.join(self.root, 'pages', 'home.json')),
                         '{\n  "title": "Café",\n  "updated_at": "2026-01-02T03:04:05+00:00",\n'
                         '  "version": 1\n}\n')
        self.assertEqual(s.get('pages', 'home')['version'], 1)

    def test_a_mirror_folder_opens_unchanged_at_version_zero(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'questions', 'Q-041.json'),
                   '{\n  "status": "open",\n  "title": "old"\n}\n')
        write_file(os.path.join(root, 'parked', 'X.json'), '{"item": "X"}\n')
        s = self.open(root)
        self.assertEqual(s.docs('questions'), {'Q-041': {'status': 'open', 'title': 'old'}})
        self.assertEqual(s.revision, 0)
        self.assertEqual(s.update('questions', 'Q-041', {'title': 'new'}, if_version=0).version, 1)
        self.assertTrue(os.path.exists(os.path.join(root, 'parked', 'X.json')))

    def test_reopen_reads_what_was_written(self):
        s = self.open()
        s.add('questions', question())
        s.close()
        s = self.open()
        self.assertEqual(s.revision, 1)
        self.assertEqual(s.get('questions', 'Q-1000')['title'], 't')

    def test_an_unreadable_document_refuses_open(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'todos', 'T-001.json'), '[1]')
        with self.assertRaises(HubError) as cm:
            Store.open(root)
        self.assertEqual(cm.exception.code, 'store')
        os.remove(os.path.join(root, 'todos', 'T-001.json'))
        Store.open(root).close()        # the failed open released the lock


class IdTest(StoreCase):
    def test_each_prefix_counts_on_its_own(self):
        s = self.open()
        self.assertEqual(s.add('questions', question()).id, 'Q-1000')
        self.assertEqual(s.add('questions', question()).id, 'Q-1001')
        self.assertEqual(s.add('stories', {'status': 'submitted', 'repo': 'pm', 'title': 't',
                                           'story': 's'}).id, 'S-001')
        self.assertEqual(s.add('to_pm', {'status': 'queued', 'kind': 'fyi', 'text': 'x'}).id,
                         'P-001')

    def test_an_approval_is_an_a_id_with_a_known_status(self):
        s = self.open()
        doc = approvals.new_doc('https://github.com/o/r', 't', 'a' * 40,
                                [{'path': 'x.md', 'base_blob': None, 'content': 'x\n'}],
                                datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc))
        self.assertEqual(s.add('approvals', doc).id, 'A-001')
        with self.assertRaises(HubError) as cm:
            s.add('approvals', dict(doc, status='granted'))
        self.assertEqual(cm.exception.code, 'invalid')

    def test_the_counter_starts_above_ids_on_disk(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'questions', 'Q-1041.json'),
                   '{"status": "open", "title": "x"}')
        self.assertEqual(self.open(root).add('questions', question()).id, 'Q-1042')

    def test_a_new_question_id_starts_at_the_floor_and_old_ids_keep_theirs(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'questions', 'Q-957.json'), '{"status": "open", "title": "x"}')
        write_file(os.path.join(root, store.META_NAME), '{"revision": 3, "counters": {"Q": 957}}')
        s = self.open(root)
        self.assertEqual(s.add('questions', question()).id, 'Q-1000')
        self.assertEqual(sorted(s.docs('questions')), ['Q-1000', 'Q-957'])
        self.assertEqual(s.add('todos', {'status': 'open', 'title': 't', 'from': 'you'}).id,
                         'T-001', 'only questions have the floor')

    def test_an_empty_stores_first_question_is_at_the_floor(self):
        self.assertEqual(self.open().add('questions', question()).id, 'Q-1000')

    def test_deleting_the_newest_question_never_lowers_the_next_id(self):
        s = self.open()
        s.add('questions', question())
        newest = s.add('questions', question()).id
        s.delete('questions', newest)
        self.assertEqual(s.add('questions', question()).id, 'Q-1002')
        s.delete('questions', 'Q-1002')
        s.close()
        self.assertEqual(self.open().add('questions', question()).id, 'Q-1003')

    def test_a_lost_meta_never_gives_an_id_twice(self):
        s = self.open()
        s.add('questions', question())
        s.add('questions', question())
        s.close()
        os.remove(os.path.join(self.root, store.META_NAME))
        s = self.open()
        self.assertEqual(s.add('questions', question()).id, 'Q-1002')

    def test_a_put_of_a_server_id_raises_the_counter(self):
        s = self.open()
        s.add('questions', question())
        s.put('questions', 'Q-1005', question())
        self.assertEqual(s.add('questions', question()).id, 'Q-1006')
        s.close()
        self.assertIn('"Q": 1006', read_file(os.path.join(self.root, store.META_NAME)))

    def test_an_id_differing_only_in_case_is_refused(self):
        s = self.open()
        s.put('pages', 'alpha', {'title': 'first'})
        for call in (lambda: s.put('pages', 'Alpha', {'title': 'second'}, if_version=0),
                     lambda: s.put('pages', 'ALPHA', {'title': 'second'})):
            with self.assertRaises(HubError) as cm:
                call()
            self.assertEqual((cm.exception.code, cm.exception.field), ('invalid', 'id'))
            self.assertIn('case', cm.exception.message)
        self.assertEqual(s.get('pages', 'alpha')['title'], 'first')
        self.assertEqual(sorted(s.docs('pages')), ['alpha'])
        self.assertEqual(s.revision, 1)

    def test_an_unreadable_or_empty_meta_is_rebuilt_like_a_missing_one(self):
        s = self.open()
        s.add('questions', question())
        s.add('questions', question())
        s.close()
        for text in ('', '{"revision": 2, "coun'):
            write_file(os.path.join(self.root, store.META_NAME), text)
            s = Store.open(self.root, clock=CLOCK)
            try:
                self.assertEqual(s.revision, 0)
                self.assertEqual(s.add('questions', question()).id, 'Q-1002')
                s.delete('questions', 'Q-1002')
            finally:
                s.close()

    def test_ids_past_999_keep_counting(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'questions', 'Q-999.json'), '{"status": "open", "title": "x"}')
        self.assertEqual(self.open(root).add('questions', question()).id, 'Q-1000')

    def test_add_refuses_a_collection_without_server_ids_and_bad_ids_are_refused(self):
        s = self.open()
        with self.assertRaises(HubError) as cm:
            s.add('pages', {'title': 't'})
        self.assertEqual(cm.exception.code, 'invalid')
        for bad in ('../x', 'CON'):
            with self.assertRaises(HubError) as cm:
                s.put('pages', bad, {'title': 't'})
            self.assertEqual((cm.exception.code, cm.exception.field), ('invalid', 'id'))


class RevisionTest(StoreCase):
    def test_each_write_bumps_the_revision_and_the_version(self):
        s = self.open()
        r = s.put('pages', 'p', {'title': 'a'})
        self.assertEqual((r.version, r.revision), (1, 1))
        r = s.update('pages', 'p', {'note': 'n'})
        self.assertEqual((r.version, r.revision), (2, 2))
        self.assertEqual(s.get('pages', 'p')['title'], 'a')
        r = s.put('pages', 'p', {'title': 'b'})
        self.assertNotIn('note', s.get('pages', 'p'))
        r = s.delete('pages', 'p')
        self.assertEqual((r.version, r.revision), (0, 4))
        self.assertIsNone(s.get('pages', 'p'))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'pages', 'p.json')))

    def test_if_version_pins_a_write(self):
        s = self.open()
        s.put('pages', 'p', {'title': 'a'}, if_version=0)
        with self.assertRaises(HubError) as cm:
            s.put('pages', 'p', {'title': 'b'}, if_version=0)
        self.assertEqual(cm.exception.code, 'conflict')
        with self.assertRaises(HubError):
            s.update('pages', 'p', {'title': 'b'}, if_version=2)
        s.update('pages', 'p', {'title': 'b'}, if_version=1)
        with self.assertRaises(HubError) as cm:
            s.delete('pages', 'p', if_version=1)
        self.assertEqual(cm.exception.code, 'conflict')
        self.assertEqual(s.revision, 2)

    def test_the_caller_cannot_set_the_store_keys(self):
        s = self.open()
        s.put('pages', 'p', {'title': 'a', 'version': 40, 'id': 'zz', 'updated_at': 'x'})
        self.assertEqual(s.get('pages', 'p'), {'title': 'a', 'version': 1,
                                               'updated_at': CLOCK()})

    def test_the_caller_changing_its_fields_afterwards_changes_nothing(self):
        s = self.open()
        refs = ['T-001']
        fields = {'status': 'open', 'title': 't', 'from': 'you', 'refs': refs}
        s.add('todos', fields)
        refs.append('T-002')
        fields['title'] = 'changed'
        self.assertEqual(s.get('todos', 'T-001')['refs'], ['T-001'])
        self.assertEqual(s.get('todos', 'T-001')['title'], 't')

    def test_a_create_only_put_matches_a_mirror_document(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'pages', 'home.json'), '{"title": "old"}')
        s = self.open(root)
        r = s.put('pages', 'home', {'title': 'new'}, if_version=0)
        self.assertEqual(r.version, 1)
        with self.assertRaises(HubError) as cm:
            s.put('pages', 'home', {'title': 'again'}, if_version=0)
        self.assertEqual(cm.exception.code, 'conflict')

    def test_update_and_delete_of_a_missing_document_are_not_found(self):
        s = self.open()
        for call in (lambda: s.update('pages', 'p', {}), lambda: s.delete('pages', 'p')):
            with self.assertRaises(HubError) as cm:
                call()
            self.assertEqual(cm.exception.code, 'not_found')

    def test_bad_arguments_are_invalid(self):
        s = self.open()
        for call in (lambda: s.put('parked', 'x', {}), lambda: s.put('pages', 'p', ['x']),
                     lambda: s.put('pages', 'p', {'title': 'a'}, if_version=True)):
            with self.assertRaises(HubError) as cm:
                call()
            self.assertEqual(cm.exception.code, 'invalid')
        self.assertEqual(s.revision, 0)


class ValidateTest(StoreCase):
    def test_a_schema_error_refuses_and_writes_nothing(self):
        s = self.open()
        with self.assertRaises(HubError) as cm:
            s.add('stories', {'status': 'submitted', 'repo': 'pm', 'title': 't'})
        self.assertEqual((cm.exception.code, cm.exception.field), ('invalid', 'story'))
        self.assertEqual(s.revision, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, store.META_NAME)))

    def test_options_shape_commands_type_and_unknown_status_refuse(self):
        s = self.open()
        for coll, fields in (('questions', question(options='pick one')),
                             ('todos', {'status': 'open', 'title': 't', 'commands': 'dir'}),
                             ('questions', question(status='maybe'))):
            with self.assertRaises(HubError) as cm:
                s.add(coll, fields)
            self.assertEqual(cm.exception.code, 'invalid', fields)

    def test_an_open_pm_todo_without_commands_refuses_but_the_owners_passes(self):
        s = self.open()
        with self.assertRaises(HubError) as cm:
            s.add('todos', {'status': 'open', 'title': 't', 'from': 'pm'})
        self.assertIn('handstep-commands', cm.exception.message)
        self.assertEqual(s.add('todos', {'status': 'open', 'title': 't', 'from': 'you'}).id,
                         'T-001')

    def test_a_new_warning_comes_back(self):
        s = self.open()
        s.put('repos', 'alpha', {'name': 'alpha'})
        r = s.add('questions', question(repo='ghost'))
        self.assertEqual([f.code for f in r.warnings], ['repo-alias'])
        self.assertEqual(s.add('questions', question(repo='alpha')).warnings, [])

    def test_a_configured_repo_name_needs_no_repos_document(self):
        s = self.open()
        s.known_repos = lambda: ['Gamerepo']
        self.assertEqual(s.add('questions', question(repo='gamerepo')).warnings, [])
        r = s.add('questions', question(repo='ghost'))
        self.assertEqual([f.code for f in r.warnings], ['repo-alias'])

    def test_the_configured_names_are_asked_only_for_a_repo_tag_and_once_a_write(self):
        s = self.open()
        asked = []
        s.known_repos = lambda: asked.append(1) or ['Gamerepo']
        s.put('repos', 'alpha', {'name': 'alpha'})
        s.add('todos', {'status': 'open', 'title': 't', 'from': 'you'})
        s.add('questions', question(repo='all'))
        self.assertEqual(asked, [], 'a write with no repo tag asked for the names')
        q = s.add('questions', question(repo='gamerepo'))
        self.assertEqual(q.warnings, [])
        self.assertEqual(asked, [1], 'the check before and after the change asks once')
        # a stored repo tag elsewhere does not make another document's write ask
        s.add('todos', {'status': 'open', 'title': 't', 'from': 'you'})
        s.update('questions', q.id, {'title': 'changed'})
        self.assertEqual(len(asked), 2)

    def test_an_s_ref_is_checked(self):
        s = self.open()
        r = s.add('questions', question(refs=['S-004']))
        s.add('stories', {'status': 'submitted', 'repo': 'pm', 'title': 't', 'story': 's'})
        self.assertEqual([f.code for f in r.warnings], ['ref-dangling'])
        self.assertEqual(s.add('questions', question(refs=['S-001'])).warnings, [])

    def test_a_finding_the_document_already_had_never_blocks(self):
        root = tmpdir(self)
        write_file(os.path.join(root, 'todos', 'T-001.json'),
                   '{"status": "open", "title": "t", "from": "pm"}')
        s = self.open(root)
        self.assertEqual(s.update('todos', 'T-001', {'title': 'u'}).version, 1)

    def test_a_legacy_text_recommended_is_read_as_its_number_so_the_question_can_be_logged(self):
        root = tmpdir(self)
        two = [{'label': 'a', 'description': 'b'}, {'label': 'c', 'description': 'd'}]
        legacy = question(status='answered', answer={'choice': 0, 'at': '2'}, options=two,
                          recommended='1')
        write_file(os.path.join(root, 'questions', 'Q-001.json'), json.dumps(legacy))
        label = question(options=two, recommended='c')
        write_file(os.path.join(root, 'questions', 'Q-002.json'), json.dumps(label))
        s = self.open(root)
        self.assertEqual(s.get('questions', 'Q-001')['recommended'], 1)
        self.assertEqual(s.get('questions', 'Q-002')['recommended'], 1)
        self.assertEqual(s.update('questions', 'Q-001', {'status': 'logged'}).version, 1)
        with open(os.path.join(root, 'questions', 'Q-001.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['recommended'], 1)


class RingTest(StoreCase):
    def test_changes_since(self):
        s = self.open()
        for i in range(3):
            s.put('pages', 'p%d' % i, {'title': 't'})
        c = s.changes_since(1)
        self.assertEqual((c.revision, c.reset), (3, False))
        self.assertEqual([(e.rev, e.id, e.op) for e in c.events], [(2, 'p1', 'put'), (3, 'p2', 'put')])
        self.assertEqual(s.changes_since(3), store.Changes(3, [], False))
        s.delete('pages', 'p0')
        self.assertEqual(s.changes_since(3).events, [store.Event(4, 'pages', 'p0', 'delete', None)])

    def test_eviction_resets(self):
        with mock.patch.object(store, 'RING', 3):
            s = self.open()
        for i in range(5):
            s.put('pages', 'p%d' % i, {'title': 't'})
        self.assertEqual([e.rev for e in s.changes_since(2).events], [3, 4, 5])
        self.assertFalse(s.changes_since(2).reset)
        self.assertTrue(s.changes_since(1).reset)

    def test_after_reopen_older_revisions_reset(self):
        s = self.open()
        s.add('questions', question())
        s.add('questions', question())
        s.close()
        s = self.open()
        self.assertEqual(s.changes_since(1), store.Changes(2, [], True))
        self.assertEqual(s.changes_since(2), store.Changes(2, [], False))
        self.assertTrue(s.changes_since(9).reset)

    def test_wait_past_the_revision_returns_reset_at_once(self):
        s = self.open()
        s.put('pages', 'p', {'title': 't'})
        start = time.monotonic()
        c = s.wait(9, 10)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(c, store.Changes(1, [], True))

    def test_the_status_is_in_the_event(self):
        s = self.open()
        s.add('questions', question())
        self.assertEqual(s.changes_since(0).events[0].status, 'open')

    def test_wait_times_out_wakes_on_a_write_and_on_close(self):
        s = self.open()
        start = time.monotonic()
        self.assertEqual(s.wait(0, 0.05).events, [])
        self.assertLess(time.monotonic() - start, 5)
        t = threading.Timer(0.1, lambda: s.put('pages', 'p', {'title': 't'}))
        t.start()
        self.addCleanup(t.join)
        start = time.monotonic()
        c = s.wait(0, 20)
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual([e.id for e in c.events], ['p'])
        t2 = threading.Timer(0.1, s.close)
        t2.start()
        self.addCleanup(t2.join)
        start = time.monotonic()
        s.wait(1, 20)
        self.assertLess(time.monotonic() - start, 15)


class ReplaceRetryTest(StoreCase):
    """_replace and _remove keep the store's behaviour on the shared kitlib.fs.replace loop."""

    def setUp(self):
        self.root = tmpdir(self)

    def test_replace_waits_out_a_reader_for_REPLACE_S_then_raises_and_drops_the_temp(self):
        path = os.path.join(self.root, 'doc.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('old')
        with mock.patch.object(store, 'REPLACE_S', 0.2),                 mock.patch('os.replace', side_effect=PermissionError(13, 'denied')) as m:
            start = time.monotonic()
            with self.assertRaises(PermissionError):
                store._replace(path, 'new')
        self.assertGreaterEqual(time.monotonic() - start, 0.2)
        self.assertGreater(m.call_count, 2)
        self.assertEqual(sorted(os.listdir(self.root)), ['doc.json'])
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'old')

    def test_replace_succeeds_once_the_reader_lets_go(self):
        path = os.path.join(self.root, 'doc.json')
        real, calls = os.replace, []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, 'denied')
            real(a, b)
        with mock.patch('os.replace', side_effect=flaky):
            store._replace(path, 'new')
        self.assertEqual(len(calls), 3)
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'new')

    def test_replace_raises_another_error_at_once(self):
        path = os.path.join(self.root, 'doc.json')
        with mock.patch('os.replace', side_effect=OSError(5, 'io')) as m:
            with self.assertRaises(OSError):
                store._replace(path, 'new')
        self.assertEqual(m.call_count, 1)

    def test_remove_a_missing_file_is_success_and_a_held_one_is_raised_after_REPLACE_S(self):
        store._remove(os.path.join(self.root, 'nothing.json'))
        path = os.path.join(self.root, 'doc.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('x')
        with mock.patch.object(store, 'REPLACE_S', 0.2),                 mock.patch('os.remove', side_effect=PermissionError(13, 'denied')) as m:
            start = time.monotonic()
            with self.assertRaises(PermissionError):
                store._remove(path)
        self.assertGreaterEqual(time.monotonic() - start, 0.2)
        self.assertGreater(m.call_count, 2)


class PendingTest(StoreCase):
    def test_pending_items(self):
        docs = {
            'questions': {'Q-1': question(status='answered', answer={'choice': 0, 'at': '2'}),
                          'Q-2': question(status='logged'), 'Q-3': question()},
            'stories': {'S-1': {'status': 'submitted', 'title': 's', 'repo': 'pm',
                                'submitted_at': '1'},
                        'S-2': {'status': 'review', 'title': 's'}},
            'to_pm': {'P-1': {'status': 'sent', 'text': 'first\nsecond', 'sent_at': '3'},
                      'P-2': {'status': 'queued', 'text': 'x'}},
            'todos': {'T-1': {'status': 'done', 'title': 'd', 'done_at': '4'},
                      'T-2': {'status': 'open', 'title': 'mine', 'from': 'you', 'added_at': '5'},
                      'T-3': {'status': 'done', 'title': 'acked', 'pm_ack': True},
                      'T-4': {'status': 'open', 'title': 'pm', 'from': 'pm'}},
            'inbox': {'M-1': {'text': 'x', 'read': False}},
        }
        self.assertEqual(store.pending_items(docs), [
            store.Pending('story', 'S-1', 's', 'pm', '1'),
            store.Pending('question', 'Q-1', 't', 'pm', '2'),
            store.Pending('to_pm', 'P-1', 'first', '', '3'),
            store.Pending('todo', 'T-1', 'd', '', '4'),
            store.Pending('todo', 'T-2', 'mine', '', '5', 'you')])

    def test_a_pending_question_and_to_do_carry_their_stored_from(self):
        docs = {'questions': {'Q-1': question(status='answered', answer={'at': '1'},
                                              **{'from': 'kit'})},
                'todos': {'T-1': {'status': 'done', 'title': 'd', 'from': 'zeta',
                                  'done_at': '2'}}}
        self.assertEqual([p.sender for p in store.pending_items(docs)], ['kit', 'zeta'])

    def test_a_filed_question_is_pending_until_it_is_logged_or_done(self):
        docs = {'questions': {
            'Q-1': question(status='filed', answer={'at': '1'}, **{'from': 'kit'}),
            'Q-2': question(status='done', answer={'at': '2'}, filed_at='2'),
            'Q-3': question(status='logged', answer={'at': '3'}, filed_at='3')}}
        self.assertEqual(store.pending_items(docs), [
            store.Pending('question', 'Q-1', 't', 'pm', '1', 'kit', 'filed')])

    def test_the_store_lists_and_clears_pending(self):
        s = self.open()
        s.add('stories', {'status': 'submitted', 'repo': 'pm', 'title': 't', 'story': 's'})
        self.assertEqual([p.id for p in s.pending()], ['S-001'])
        s.update('stories', 'S-001', {'status': 'review'})
        self.assertEqual(s.pending(), [])


class LockTest(StoreCase):
    def holder(self, root):
        p = spawn.popen([sys.executable, '-c', HOLDER, root], cwd=KIT, env=child_env(),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is not None or (p.kill(), p.wait()))  # our own child
        self.assertEqual(p.stdout.readline().strip(), 'READY')
        return p

    def test_a_second_open_in_this_process_is_busy(self):
        self.open()
        with self.assertRaises(StoreBusy) as cm:
            Store.open(self.root)
        self.assertEqual(cm.exception.record.get('pid'), os.getpid())

    def test_another_process_holding_it_is_busy_until_it_closes(self):
        root = tmpdir(self)
        p = self.holder(root)
        with self.assertRaises(StoreBusy) as cm:
            Store.open(root)
        self.assertEqual(cm.exception.record.get('pid'), p.pid)
        self.assertEqual(cm.exception.code, 'store')
        p.communicate('close\n', timeout=60)
        s = self.open(root)
        self.assertEqual(s.took_over, {})

    def test_a_dead_holder_never_blocks_and_its_record_is_taken_over(self):
        root = tmpdir(self)
        p = self.holder(root)
        p.communicate('die\n', timeout=60)
        s = self.open(root)
        self.assertEqual(s.took_over.get('pid'), p.pid)

    def test_a_closed_store_refuses_calls(self):
        s = self.open()
        s.close()
        with self.assertRaises(HubError):
            s.get('pages', 'p')
        with self.assertRaises(HubError):
            s.put('pages', 'p', {'title': 't'})


class ContentionTest(StoreCase):
    def test_threads_adding_at_once_get_unique_contiguous_ids(self):
        s = self.open()
        ids, errors = [], []

        def work():
            try:
                for _ in range(25):
                    ids.append(s.add('questions', question()).id)
            except Exception as e:          # reported below
                errors.append(e)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ids), ['Q-%d' % n for n in range(1000, 1200)])
        self.assertEqual(s.revision, 200)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'questions'))), 200)

    def test_two_updates_pinned_to_one_version_one_wins(self):
        s = self.open()
        s.add('questions', question())
        barrier, results = threading.Barrier(2), []

        def work(title):
            barrier.wait()
            try:
                results.append(s.update('questions', 'Q-1000', {'title': title}, if_version=1))
            except HubError as e:
                results.append(e.code)
        threads = [threading.Thread(target=work, args=(t,)) for t in ('a', 'b')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(type(r).__name__ for r in results), ['Result', 'str'])
        self.assertIn('conflict', results)
        self.assertEqual(s.get('questions', 'Q-1000')['version'], 2)


class CrashTest(StoreCase):
    def crash(self, n, op):
        root = self.root
        p = spawn.run([sys.executable, '-c', CRASH, root, str(n), op], cwd=KIT,
                      env=child_env(), timeout=60)
        self.assertEqual(p.returncode, 3)
        return p

    def setUp(self):
        self.root = tmpdir(self)
        s = Store.open(self.root, clock=CLOCK)
        s.add('questions', question())
        s.close()
        self.q1 = read_file(os.path.join(self.root, 'questions', 'Q-1000.json'))

    def test_a_crash_before_meta_changes_nothing(self):
        self.crash(1, 'add')
        self.assertTrue(tmp_files(self.root))
        s = self.open()
        self.assertEqual(tmp_files(self.root), [])
        self.assertEqual(s.revision, 1)
        self.assertEqual(sorted(s.docs('questions')), ['Q-1000'])
        self.assertTrue(s.took_over.get('pid'))
        self.assertEqual(s.add('questions', question()).id, 'Q-1001')

    def test_a_crash_between_meta_and_document_skips_a_revision_and_an_id(self):
        self.crash(2, 'add')
        s = self.open()
        self.assertEqual(tmp_files(self.root), [])
        self.assertEqual(s.revision, 2)
        self.assertEqual(sorted(s.docs('questions')), ['Q-1000'])
        r = s.add('questions', question())
        self.assertEqual((r.id, r.revision), ('Q-1002', 3))

    def test_a_crash_mid_update_keeps_the_old_document(self):
        self.crash(2, 'update')
        s = self.open()
        self.assertEqual(read_file(os.path.join(self.root, 'questions', 'Q-1000.json')), self.q1)
        self.assertEqual(s.get('questions', 'Q-1000')['version'], 1)
        self.assertEqual(s.update('questions', 'Q-1000', {'title': 'x'}, if_version=1).version, 2)


if __name__ == '__main__':
    unittest.main()
