"""kitlib.hubsite.migrate: the mirror folder and an export copied into the hub store. Every folder
is a scratch one under a temp folder; a server, where one runs, is on port 0."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import migrate
from kitlib.hubsite.migrate import Busy, Conflict, Kept, Left, MigrateError, NotADocument
from kitlib.hubsite.store import LOCK_NAME, META_NAME, HubError, Store, StoreBusy, normal_text
from tests.hubsite import test_hub_cli as cli

AT = '2026-01-02T03:04:05+00:00'
LATER = '2026-02-02T03:04:05+00:00'


def question(**kw):
    d = {'status': 'open', 'title': 'which way', 'repo': 'pm', 'asked_at': AT,
         'options': [{'label': 'a', 'description': 'b'}], 'recommended': 0}
    d.update(kw)
    return d


def notice(run, phase):
    return {'title': 'machine lock %s' % phase, 'text': 'the lock window %s' % phase, 'at': AT,
            'repo': 'all', 'from': 'machine lock', 'kind': 'lock-window', 'run': run,
            'phase': phase}


# The old hub's data folder, parked/ included.
MIRROR = {
    'questions': {
        'Q-001': question(),
        'Q-002': question(status='answered', answer={'choice': 0, 'at': AT}),
        'Q-007': question(status='logged', repo='kit'),
    },
    'inbox': {
        'M-001': {'text': 'the answer is in', 'ref': 'Q-001', 'at': AT, 'read': False},
        'L-run1-start': notice('run1', 'start'),
        'L-run1-end': notice('run1', 'end'),
    },
    'todos': {
        'T-001': {'status': 'open', 'title': 'run it', 'from': 'pm', 'commands': ['python x.py']},
        'T-002': {'status': 'done', 'title': 'ran it', 'from': 'you', 'done_at': AT,
                  'pm_ack': True},
    },
    'to_pm': {'P-001': {'status': 'done', 'kind': 'question', 'text': 'why', 'reply': 'because'}},
    'stories': {
        'S-001': {'status': 'added', 'repo': 'pm', 'title': 's', 'story': 'as an owner'},
        'story-abc': {'status': 'declined', 'repo': 'pm', 'title': 'u', 'story': 'as an agent'},
    },
    'pages': {'guide': {'title': 'Guide'}},
    'page_html': {'guide': {'html': '<p>read me</p>'}},
    'parked': {'one': {'title': 'parked a', 'q': 'Q-001'}, 'two': {'title': 'parked b'}},
}
# An export of one account's copy: wrapped documents, and the collections a tool refills.
EXPORT = {
    'questions': {'Q-001': question(), 'Q-009': question(title='only in the export')},
    'repos': {'kit': {'name': 'kit', 'added_at': AT, 'aliases': ['k']}},
    'backlog': {'kit': {'items': []}},
    'completed': {'kit': {'items': []}},
    'usage': {'now': {'accounts': {}}},
    'agents': {'worker': {'model': 'm'}, 'reviewer': {'model': 'm'}},
}
IMPORTED = ('questions', 'inbox', 'todos', 'to_pm', 'stories', 'pages', 'page_html', 'repos')
MIRROR_RELS = sorted('%s/%s' % (c, i) for c in MIRROR if c != 'parked' for i in MIRROR[c])
EXPORT_ONLY = ['questions/Q-009', 'repos/kit']


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(obj if isinstance(obj, str) else normal_text(obj))


def fill(folder, docs, wrapped=False):
    for coll, by_id in docs.items():
        for doc_id, fields in by_id.items():
            obj = {'id': doc_id, 'version': 4, 'data': fields} if wrapped else fields
            write(os.path.join(folder, coll, doc_id + '.json'), obj)
    return folder


def tree(folder):
    """{relative path: (bytes, mtime)} of every file under folder."""
    out = {}
    for base, _dirs, names in os.walk(folder):
        for n in names:
            path = os.path.join(base, n)
            with open(path, 'rb') as f:
                out[os.path.relpath(path, folder).replace(os.sep, '/')] = (
                    f.read(), os.stat(path).st_mtime_ns)
    return out


def on_disk(root, coll, doc_id):
    """The fields of root's coll/doc_id.json as its file holds them, or None: what migrate wrote,
    not what the store reads (the store reads a text `recommended` as its number)."""
    try:
        with open(os.path.join(root, coll, doc_id + '.json'), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def docs_of(folder):
    """The document files under folder: tree() without the store's own lock file."""
    return {k: v for k, v in tree(folder).items() if k != LOCK_NAME}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kit-hubmigrate-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mirror = fill(os.path.join(self.tmp, 'mirror'), MIRROR)
        self.export = fill(os.path.join(self.tmp, 'export'), EXPORT, wrapped=True)
        self.root = os.path.join(self.tmp, 'store')

    def run_both(self, **kw):
        return migrate.migrate(self.root, [self.mirror, self.export], **kw)

    def stored(self, coll, doc_id):
        return on_disk(self.root, coll, doc_id)


class ImportCase(Case):
    def test_every_mirrored_question_is_in_the_store_with_its_id_and_validate_is_clean(self):
        r = self.run_both()
        with Store.open(self.root) as s:
            got = s.docs('questions')
        self.assertEqual(sorted(got), ['Q-001', 'Q-002', 'Q-007', 'Q-009'])
        for doc_id, fields in MIRROR['questions'].items():
            self.assertEqual(got[doc_id], fields, doc_id)
        self.assertEqual(r.findings, [])
        self.assertEqual(migrate.validate(self.root), [])
        self.assertEqual(migrate.validate_lines([]), ['VALIDATE clean: no finding'])

    def test_each_kind_of_document_is_copied_whole_and_counted(self):
        r = self.run_both()
        self.assertEqual(r.added, sorted(MIRROR_RELS + EXPORT_ONLY))
        self.assertEqual((r.same, r.kept, r.conflicts, r.skipped), ([], [], [], []))
        self.assertTrue(r.changed)
        self.assertTrue(r.complete)
        with Store.open(self.root) as s:
            for coll in IMPORTED:
                want = dict(MIRROR.get(coll, {}))
                for doc_id, fields in EXPORT.get(coll, {}).items():
                    want.setdefault(doc_id, fields)
                self.assertEqual(s.docs(coll), want, coll)
        # a wrapped export document is stored as its fields: no wrapper, no id, no version
        self.assertEqual(self.stored('repos', 'kit'), EXPORT['repos']['kit'])
        self.assertEqual(self.stored('stories', 'story-abc'), MIRROR['stories']['story-abc'])

    def test_the_lock_window_notices_are_imported_and_stay_in_the_source(self):
        before = tree(self.mirror)
        r = self.run_both()
        self.assertEqual(r.lock_windows, ['inbox/L-run1-end', 'inbox/L-run1-start'])
        self.assertEqual(self.stored('inbox', 'L-run1-start'), notice('run1', 'start'))
        self.assertEqual(self.stored('inbox', 'L-run1-end'), notice('run1', 'end'))
        self.assertEqual(tree(self.mirror), before)
        self.assertIn('inbox/L-run1-start.json', before)
        self.assertIn('inbox/: 2 lock-window notices in the store, kept as they are in the source',
                      migrate.report_lines(r))

    def test_no_source_file_is_changed_by_a_run_or_by_a_second_one(self):
        before = tree(self.mirror), tree(self.export)
        self.run_both()
        self.assertEqual((tree(self.mirror), tree(self.export)), before)
        self.run_both()
        self.assertEqual((tree(self.mirror), tree(self.export)), before)

    def test_a_folder_the_store_has_no_collection_for_is_left_alone_and_counted(self):
        r = self.run_both()
        self.assertEqual(r.left, sorted([
            Left(self.mirror, 'parked', 2), Left(self.export, 'agents', 2),
            Left(self.export, 'backlog', 1), Left(self.export, 'completed', 1),
            Left(self.export, 'usage', 1)]))
        for name in ('parked', 'agents', 'backlog', 'completed', 'usage'):
            self.assertFalse(os.path.exists(os.path.join(self.root, name)), name)
        self.assertTrue(r.complete, 'a left folder is reported, it does not fail the run')
        self.assertIn('LEFT ALONE parked/ in %s: 2 documents; the store has no parked/ collection'
                      % self.mirror, migrate.report_lines(r))

    def test_a_document_a_new_write_would_refuse_is_still_copied_whole_and_reported(self):
        odd = {'status': 'open', 'title': 'a hand step', 'from': 'pm'}       # no commands
        wrong = question(status='shelved')
        write(os.path.join(self.mirror, 'todos', 'T-003.json'), odd)
        write(os.path.join(self.mirror, 'questions', 'Q-003.json'), wrong)
        write(os.path.join(self.mirror, 'inbox', 'M-002.json'), {'at': AT})   # no text
        r = self.run_both()
        self.assertEqual(self.stored('todos', 'T-003'), odd)
        self.assertEqual(self.stored('questions', 'Q-003'), wrong)
        self.assertEqual(self.stored('inbox', 'M-002'), {'at': AT})
        self.assertTrue(r.complete)
        self.assertEqual(sorted((f.rel, f.code) for f in r.findings), [
            ('inbox/M-002', 'schema-field'), ('questions/Q-003', 'status-unknown'),
            ('todos/T-003', 'handstep-commands')])
        self.assertEqual(migrate.validate(self.root), r.findings)
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE 2 error(s), 1 warning(s)', lines)
        # the one document the store's own check refuses to write again is said so, plainly
        self.assertEqual([x for x in lines if 'the api will refuse' in x], [
            'VALIDATE inbox/M-002: the api will refuse every update of this document until it is '
            'fixed; fix its file by hand with the server stopped (no api route repairs it)'])
        with Store.open(self.root) as s:
            with self.assertRaises(HubError):
                s.update('inbox', 'M-002', {'read': True})
            s.update('questions', 'Q-003', {'title': 'a finding it already had does not block'})
        self.assertTrue(any(x.startswith('VALIDATE ERROR questions/Q-003 status') for x in lines))

    def test_the_next_id_the_store_gives_is_above_every_imported_one(self):
        self.run_both()
        with Store.open(self.root) as s:
            self.assertEqual(s.add('questions', question()).id, 'Q-010')
            self.assertEqual(s.add('stories', MIRROR['stories']['S-001']).id, 'S-002')

    def test_an_imported_document_is_at_version_0_until_the_store_writes_it(self):
        self.run_both()
        with Store.open(self.root) as s:
            self.assertNotIn('version', s.get('questions', 'Q-001'))
            got = s.update('questions', 'Q-001', {'title': 'changed'}, if_version=0)
            self.assertEqual(got.version, 1)


class IdempotentCase(Case):
    def test_a_second_run_changes_nothing_and_says_so(self):
        first = self.run_both()
        before = docs_of(self.root)     # the lock file is taken and released, as at any open
        second = self.run_both()
        self.assertEqual(docs_of(self.root), before)
        self.assertEqual(second.added, [])
        self.assertEqual(second.same, first.added)
        self.assertFalse(second.changed)
        self.assertTrue(second.complete)
        self.assertEqual(second.lock_windows, first.lock_windows)
        self.assertEqual(migrate.report_lines(first)[-1],
                         'imported 15 documents; no source was changed')
        self.assertEqual(migrate.report_lines(second)[-1],
                         'nothing changed: the store already holds every document that can be '
                         'imported; nothing was written')
        self.assertFalse(os.path.exists(os.path.join(self.root, META_NAME)),
                         'a run writes documents only, never the revision file')

    def test_a_document_the_store_wrote_since_is_kept_and_reported(self):
        self.run_both()
        with Store.open(self.root) as s:
            s.update('questions', 'Q-001', {'status': 'answered', 'answer': {'choice': 0}})
            s.update('inbox', 'M-001', {'read': True})
        before = docs_of(self.root)
        r = self.run_both()
        self.assertEqual(docs_of(self.root), before)
        self.assertEqual(r.kept, [Kept('inbox/M-001', [self.mirror]),
                                  Kept('questions/Q-001', [self.mirror, self.export])])
        self.assertEqual(r.added, [])
        self.assertTrue(r.complete, 'the store being newer is not a failure')
        self.assertEqual(self.stored('questions', 'Q-001')['status'], 'answered')
        self.assertIn('KEPT inbox/M-001: the store holds other fields than %s; the store is '
                      'unchanged' % self.mirror, migrate.report_lines(r))

    def test_a_document_the_store_only_stamped_is_the_same_document(self):
        self.run_both()
        with Store.open(self.root) as s:
            s.put('pages', 'guide', MIRROR['pages']['guide'])      # version 1, updated_at set
        r = self.run_both()
        self.assertEqual(r.kept, [])
        self.assertIn('pages/guide', r.same)

    def test_a_document_added_to_a_source_later_is_the_only_one_a_new_run_writes(self):
        self.run_both()
        write(os.path.join(self.mirror, 'questions', 'Q-012.json'), question(title='late'))
        r = self.run_both()
        self.assertEqual(r.added, ['questions/Q-012'])
        self.assertEqual(self.stored('questions', 'Q-012'), question(title='late'))

    def test_the_mirror_folder_served_in_place_is_already_the_store(self):
        before = tree(self.mirror)
        r = migrate.migrate(self.mirror, [self.mirror])
        self.assertEqual(docs_of(self.mirror), before)
        self.assertEqual((r.added, r.kept, r.conflicts, r.skipped), ([], [], [], []))
        self.assertEqual(r.same, MIRROR_RELS)
        self.assertEqual(r.left, [Left(self.mirror, 'parked', 2)])
        self.assertFalse(r.changed)
        self.assertFalse(os.path.exists(os.path.join(self.mirror, META_NAME)),
                         'a folder served in place gets no revision file from migrate')


class FieldsOfTest(unittest.TestCase):

    def test_wrapped_document_gives_its_data(self):
        obj = {'id': 'q1', 'version': 3, 'data': {'text': 'x', 'state': 'open'}}
        self.assertEqual(migrate.fields_of('questions', obj), {'text': 'x', 'state': 'open'})

    def test_a_data_object_alone_is_a_wrapped_document(self):
        self.assertEqual(migrate.fields_of('todos', {'data': {'text': 'x'}}), {'text': 'x'})

    def test_bare_document_loses_id_and_version(self):
        obj = {'id': 'q1', 'version': 3, 'text': 'x', 'data': 'not an object'}
        self.assertEqual(migrate.fields_of('questions', obj),
                         {'text': 'x', 'data': 'not an object'})

    def test_a_data_object_beside_other_keys_is_refused(self):
        for obj in ({'id': 'q1', 'version': 3, 'updated_at': 't', 'data': {'text': 'x'}},
                    {'text': 'x', 'data': {'kept': True}}):
            with self.assertRaises(NotADocument, msg=repr(obj)) as cm:
                migrate.fields_of('questions', obj)
            self.assertIn('kitlib.hubsite.migrate.WRAPPER_KEYS', str(cm.exception))

    def test_to_pm_loses_its_id_like_every_collection(self):
        self.assertEqual(migrate.fields_of('to_pm', {'id': 'm7', 'version': 2, 'text': 'hi'}),
                         {'text': 'hi'})
        self.assertEqual(
            migrate.fields_of('to_pm', {'version': 2, 'data': {'id': 'm7', 'text': 'hi'}}),
            {'text': 'hi'})

    def test_not_an_object_is_refused(self):
        with self.assertRaises(NotADocument):
            migrate.fields_of('todos', ['x'])


class DryRunCase(Case):
    def test_a_dry_run_writes_nothing_and_says_what_a_run_would_do(self):
        r = self.run_both(dry_run=True)
        self.assertFalse(os.path.exists(self.root), 'a dry run made the store folder')
        self.assertEqual(r.added, sorted(MIRROR_RELS + EXPORT_ONLY))
        self.assertFalse(r.changed)
        self.assertEqual(r.findings, [])
        lines = migrate.report_lines(r)
        self.assertIn('questions/: would import 4, already in the store 0, store differs and is '
                      'kept 0, conflict 0', lines)
        self.assertEqual(lines[-1], 'dry run: 15 documents would be imported; nothing was written')
        self.assertEqual(self.run_both().added, r.added)

    def test_a_dry_run_over_a_store_writes_nothing_there(self):
        self.run_both()
        write(os.path.join(self.mirror, 'questions', 'Q-012.json'), question())
        before = docs_of(self.root)     # the lock file is taken and released, as at any open
        r = self.run_both(dry_run=True)
        self.assertEqual(docs_of(self.root), before)
        self.assertEqual(r.added, ['questions/Q-012'])

    def test_a_dry_run_reports_what_the_store_would_hold_wrong(self):
        write(os.path.join(self.mirror, 'questions', 'Q-003.json'), question(status='shelved'))
        r = self.run_both(dry_run=True)
        self.assertEqual([(f.rel, f.code) for f in r.findings],
                         [('questions/Q-003', 'status-unknown')])


class LockCase(Case):
    def test_a_run_is_refused_while_the_store_is_held_and_writes_nothing(self):
        with Store.open(self.root):
            before = tree(self.root)
            with self.assertRaises(Busy) as cm:
                self.run_both()
            self.assertIn('a hub server has this store open; stop it first', str(cm.exception))
            with self.assertRaises(Busy):
                self.run_both(dry_run=True)
            self.assertEqual(tree(self.root), before)
            self.assertEqual(sorted(os.listdir(self.root)), [LOCK_NAME])
        self.assertEqual(len(self.run_both().added), 15, 'the lock is free again: the run goes')

    def test_the_lock_is_released_after_a_run_and_after_a_refused_one(self):
        self.run_both()
        Store.open(self.root).close()
        with self.assertRaises(MigrateError):
            migrate.migrate(self.root, [os.path.join(self.tmp, 'no-such-folder')])
        Store.open(self.root).close()

    def test_validate_reads_beside_a_holder_and_makes_nothing(self):
        self.run_both()
        with Store.open(self.root):
            before = tree(self.root)
            self.assertEqual(migrate.validate(self.root), [])
            self.assertEqual(tree(self.root), before)
        missing = os.path.join(self.tmp, 'no-store')
        with self.assertRaises(MigrateError):
            migrate.validate(missing)
        self.assertFalse(os.path.exists(missing))


class NotImportedCase(Case):
    def test_two_sources_that_differ_are_imported_from_neither_and_both_are_named(self):
        write(os.path.join(self.export, 'questions', 'Q-002.json'),
              {'id': 'Q-002', 'data': question(status='logged', asked_at=LATER)})
        before = tree(self.mirror), tree(self.export)
        r = self.run_both()
        self.assertEqual(r.conflicts,
                         [Conflict('questions/Q-002', [self.mirror, self.export], self.export,
                                   False)])
        self.assertIsNone(self.stored('questions', 'Q-002'))
        self.assertFalse(r.complete)
        self.assertEqual(len(r.added), 14, 'every other document is imported')
        self.assertEqual((tree(self.mirror), tree(self.export)), before)
        lines = migrate.report_lines(r)
        self.assertIn('CONFLICT questions/Q-002: %s and %s hold different fields; imported from '
                      'neither (the later by its time stamps: %s). To resolve without editing a '
                      'source: run again with the chosen source alone'
                      % (self.mirror, self.export, self.export), lines)
        # the line's own advice: the chosen source alone imports its copy
        alone = migrate.migrate(self.root, [self.export])
        self.assertEqual((alone.added, alone.conflicts), (['questions/Q-002'], []))
        self.assertEqual(self.stored('questions', 'Q-002')['status'], 'logged')

    def test_a_conflict_resolved_at_a_source_is_imported_by_the_next_run(self):
        write(os.path.join(self.export, 'questions', 'Q-002.json'),
              {'id': 'Q-002', 'data': question(status='logged', asked_at=LATER)})
        r = self.run_both()
        self.assertFalse(r.complete)
        self.assertIn('NOT COMPLETE: 1 document not imported (CONFLICT and SKIPPED above); the '
                      'sources are unchanged', migrate.report_lines(r))
        # resolved at a source, the next run imports it
        write(os.path.join(self.export, 'questions', 'Q-002.json'), MIRROR['questions']['Q-002'])
        again = self.run_both()
        self.assertEqual((again.added, again.conflicts), (['questions/Q-002'], []))

    def test_a_conflict_with_no_time_to_tell_names_no_later_source(self):
        write(os.path.join(self.export, 'pages', 'guide.json'), {'title': 'Another guide'})
        r = self.run_both()
        self.assertEqual(r.conflicts,
                         [Conflict('pages/guide', [self.mirror, self.export], None, False)])
        self.assertTrue(any('the time stamps do not tell which is later' in x
                            for x in migrate.report_lines(r)))

    def test_a_file_that_is_no_document_or_has_an_id_the_store_cannot_use_is_skipped(self):
        write(os.path.join(self.mirror, 'questions', 'Q-004.json'), '{ not json')
        write(os.path.join(self.mirror, 'questions', 'Q-005.json'), '[1, 2]\n')
        write(os.path.join(self.mirror, 'questions', 'two words.json'), question())
        write(os.path.join(self.export, 'questions', 'q-007.json'), question(status='logged'))
        write(os.path.join(self.export, 'questions', 'Q-006.json'),
              {'data': question(), 'surprise': 1})
        before = tree(self.mirror), tree(self.export)
        r = self.run_both()
        self.assertEqual([(s.source, s.rel) for s in r.skipped], sorted([
            (self.mirror, 'questions/Q-004'), (self.mirror, 'questions/Q-005'),
            (self.mirror, 'questions/two words'), (self.export, 'questions/Q-006'),
            (self.export, 'questions/q-007')]))
        self.assertIn('differs only in case from questions/Q-007',
                      [s.why for s in r.skipped if s.rel == 'questions/q-007'][0])
        self.assertFalse(r.complete)
        self.assertEqual(len(r.added), 15, 'the rest is imported')
        self.assertEqual((tree(self.mirror), tree(self.export)), before)
        with Store.open(self.root) as s:
            self.assertEqual(sorted(s.docs('questions')), ['Q-001', 'Q-002', 'Q-007', 'Q-009'])

    def test_a_source_that_is_no_folder_refuses_the_run_before_anything_is_written(self):
        for sources in ([], [self.mirror, os.path.join(self.tmp, 'absent')]):
            with self.assertRaises(MigrateError):
                migrate.migrate(self.root, sources)
            self.assertFalse(os.path.exists(self.root), sources)

    def test_a_store_document_is_never_written_over(self):
        path = os.path.join(self.root, 'questions', 'Q-001.json')
        write(path, {'status': 'open', 'title': 'the store was first'})
        text = tree(self.root)
        with self.assertRaises(OSError):
            migrate._create(path, 'other text\n')
        self.assertEqual(tree(self.root), text, 'the file is as it was and no temp file is left')
        r = self.run_both()
        self.assertEqual(r.kept, [])
        self.assertEqual(r.conflicts,
                         [Conflict('questions/Q-001', [self.mirror, self.export], None, True)])
        self.assertEqual(self.stored('questions', 'Q-001')['title'], 'the store was first')


class StoreConflictCase(Case):
    """A store document no server has written (no `version`) that a source differs from: one
    source against another, so the run is not complete. The default layout is the case that
    matters: the store folder is the mirror folder."""

    def newer_in_the_export(self):
        write(os.path.join(self.export, 'questions', 'Q-002.json'),
              {'id': 'Q-002', 'data': question(status='logged', asked_at=LATER)})

    def test_the_mirror_served_in_place_and_a_newer_copy_in_a_second_source_is_a_conflict(self):
        self.newer_in_the_export()
        before = docs_of(self.mirror), tree(self.export)
        r = migrate.migrate(self.mirror, [self.mirror, self.export])
        self.assertEqual(r.conflicts,
                         [Conflict('questions/Q-002', [self.export], self.export, True)])
        self.assertEqual(r.kept, [])
        self.assertFalse(r.complete)
        self.assertEqual(r.added, EXPORT_ONLY, 'the rest of the second source is imported')
        after = docs_of(self.mirror)
        for rel in EXPORT_ONLY:
            del after[rel + '.json']
        self.assertEqual((after, tree(self.export)), before, 'no document was written over')
        lines = migrate.report_lines(r)
        self.assertIn(
            "CONFLICT questions/Q-002: the store's copy, which no server has written, and %s "
            "hold different fields; the store's copy is kept and nothing was imported (the later "
            "by its time stamps: %s). To keep the store's copy there is nothing to do; a run with "
            "that source reports this again. To take a source's copy: with the server stopped, "
            "move the file questions/Q-002.json out of the store folder (where the store folder "
            "is the old hub's data folder, that file is the old hub's own file), then run again "
            "with the chosen source alone" % (self.export, self.export), lines)
        self.assertFalse(any('run again without that source' in x for x in lines),
                         'leaving the source out would leave out its other documents too')
        self.assertIn('NOT COMPLETE: 1 document not imported (CONFLICT and SKIPPED above); the '
                      'sources are unchanged', lines)
        self.assertIn('questions/: imported 1, already in the store 2, store differs and is kept '
                      '0, conflict 1', lines)
        # a second run still says so: the conflict does not go quiet
        self.assertEqual(migrate.migrate(self.mirror, [self.mirror, self.export]).conflicts,
                         r.conflicts)
        # the line's own advice, taken: the file moved out, the chosen source alone
        os.replace(os.path.join(self.mirror, 'questions', 'Q-002.json'),
                   os.path.join(self.tmp, 'Q-002.aside.json'))
        done = migrate.migrate(self.mirror, [self.export])
        self.assertEqual((done.added, done.conflicts), (['questions/Q-002'], []))
        self.assertTrue(done.complete)

    def test_the_stores_copy_is_named_when_it_is_the_later_one(self):
        write(os.path.join(self.export, 'questions', 'Q-001.json'),
              question(title='an older wording', asked_at='2025-01-02T03:04:05+00:00'))
        r = migrate.migrate(self.mirror, [self.mirror, self.export])
        self.assertEqual(r.conflicts,
                         [Conflict('questions/Q-001', [self.export], self.mirror, True)])
        self.assertTrue(any("(the later by its time stamps: the store's copy)" in x
                            for x in migrate.report_lines(r)))

    def test_a_copy_of_an_earlier_run_that_a_source_now_differs_from_is_a_conflict(self):
        self.run_both()
        self.newer_in_the_export()
        r = self.run_both()
        self.assertEqual(r.conflicts,
                         [Conflict('questions/Q-002', [self.export], self.export, True)])
        self.assertFalse(r.complete)
        self.assertEqual(self.stored('questions', 'Q-002'), MIRROR['questions']['Q-002'])

    def test_a_document_a_server_wrote_is_kept_and_the_run_is_complete(self):
        self.run_both()
        with Store.open(self.root) as s:
            s.update('questions', 'Q-002', {'title': 'the server wrote this'})
        self.newer_in_the_export()
        r = self.run_both()
        self.assertEqual(r.conflicts, [])
        self.assertEqual(r.kept, [Kept('questions/Q-002', [self.mirror, self.export])])
        self.assertTrue(r.complete)
        self.assertEqual(self.stored('questions', 'Q-002')['title'], 'the server wrote this')

    def test_a_lock_window_notice_in_a_conflict_stays_in_the_store_and_is_counted(self):
        write(os.path.join(self.export, 'inbox', 'L-run1-end.json'),
              dict(notice('run1', 'end'), text='another wording'))
        r = migrate.migrate(self.mirror, [self.mirror, self.export])
        self.assertEqual([c.rel for c in r.conflicts], ['inbox/L-run1-end'])
        self.assertEqual(r.lock_windows, ['inbox/L-run1-end', 'inbox/L-run1-start'])
        self.assertEqual(self.stored_in(self.mirror, 'inbox', 'L-run1-end'), notice('run1', 'end'))

    def stored_in(self, root, coll, doc_id):
        return on_disk(root, coll, doc_id)


class WriteCase(Case):
    def test_a_document_that_is_not_utf8_text_is_skipped_and_the_rest_is_imported(self):
        write(os.path.join(self.mirror, 'questions', 'Q-004.json'),
              r'{"status": "open", "title": "half \ud800 pair"}')
        dry = self.run_both(dry_run=True)
        r = self.run_both()
        for got in (dry, r):
            self.assertEqual([(s.source, s.rel) for s in got.skipped],
                             [(self.mirror, 'questions/Q-004')])
            self.assertIn('cannot be written to the store: ', got.skipped[0].why)
            self.assertEqual(len(got.added), 15, 'every other document is imported')
            self.assertFalse(got.complete)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, 'questions'))),
                         ['Q-001.json', 'Q-002.json', 'Q-007.json', 'Q-009.json'])
        with self.assertRaises(ValueError):
            migrate._create(os.path.join(self.root, 'questions', 'Q-005.json'), '\ud800')
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'questions'))), 4,
                         'no temp file and no half document is left')

    def test_the_store_lock_is_held_while_documents_are_written(self):
        real, seen = migrate._create, []

        def create(path, text):
            with self.assertRaises(StoreBusy):
                Store.open(self.root)
            seen.append(path)
            real(path, text)

        with mock.patch.object(migrate, '_create', create):
            self.run_both()
        self.assertEqual(len(seen), 15)
        Store.open(self.root).close()

    def test_the_lock_is_released_when_the_run_dies_while_writing(self):
        with mock.patch.object(migrate, '_create', side_effect=RuntimeError('died')):
            with self.assertRaises(RuntimeError):
                self.run_both()
        Store.open(self.root).close()
        self.assertEqual(len(self.run_both().added), 15)

    def test_every_write_failing_is_not_called_nothing_changed(self):
        with mock.patch.object(migrate, '_create', side_effect=OSError('the disk is full')):
            r = self.run_both()
        self.assertEqual((r.added, len(r.skipped)), ([], 15))
        lines = migrate.report_lines(r)
        self.assertEqual(lines[-1],
                         'nothing was imported: see NOT COMPLETE above; nothing was written')
        self.assertFalse(any(x.startswith('nothing changed') for x in lines))

    def test_a_store_that_cannot_be_read_back_keeps_the_report(self):
        real, calls = migrate.store_docs, []

        def store_docs(root):
            calls.append(root)
            if len(calls) > 1:
                raise MigrateError('cannot read the hub store: gone')
            return real(root)

        with mock.patch.object(migrate, 'store_docs', store_docs):
            r = self.run_both()
        self.assertEqual((len(r.added), r.findings), (15, []))
        self.assertEqual(r.unchecked, 'cannot read the hub store: gone')
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE skipped: cannot read the hub store: gone', lines)
        self.assertNotIn('VALIDATE clean: no finding', lines)
        self.assertEqual(lines[-1], 'imported 15 documents; no source was changed')
        self.assertEqual(self.run_both().unchecked, '')


class SourceCase(Case):
    def test_a_source_with_no_document_for_the_store_is_said_so(self):
        only = fill(os.path.join(self.tmp, 'only-parked'), {'parked': MIRROR['parked']})
        r = migrate.migrate(self.root, [only])
        self.assertEqual(r.empty, [only])
        self.assertTrue(r.complete)
        lines = migrate.report_lines(r)
        self.assertIn('NO DOCUMENT in %s: it holds no document of a collection the store has'
                      % only, lines)
        self.assertEqual(lines[-1], 'nothing to import: no source holds a document of a '
                                    'collection the store has; nothing was written')
        both = migrate.report_lines(migrate.migrate(self.root, [only, self.mirror]))
        self.assertIn('NO DOCUMENT in %s: it holds no document of a collection the store has'
                      % only, both)
        self.assertEqual(both[-1], 'imported 13 documents; no source was changed')
        self.assertEqual(self.run_both().empty, [])

    def test_with_no_repos_document_the_tags_are_noted_once_not_warned_each(self):
        r = migrate.migrate(self.root, [self.mirror])     # Q-007 is tagged `kit`; no repos/
        self.assertEqual([(f.level, f.rel, f.code) for f in r.findings],
                         [('note', '', 'repo-alias')])
        self.assertEqual(migrate.validate(self.root), r.findings)
        self.assertIn('VALIDATE 0 error(s), 0 warning(s)', migrate.report_lines(r))
        # with a repos/ document the tags are judged: one the store does not know is a warning
        write(os.path.join(self.root, 'repos', 'other.json'), {'name': 'other', 'added_at': AT})
        self.assertEqual([(f.level, f.rel, f.code) for f in migrate.validate(self.root)],
                         [('warn', 'questions/Q-007', 'repo-alias')])

    def test_a_file_whose_own_keys_name_another_document_is_skipped(self):
        q = os.path.join(self.export, 'questions')
        write(os.path.join(q, 'Q-011.json'), {'id': 'Q-010', 'data': question()})
        write(os.path.join(q, 'Q-012.json'), {'doc_id': 'Q-013', 'data': question()})
        write(os.path.join(q, 'Q-014.json'), {'collection': 'todos', 'data': question()})
        write(os.path.join(q, 'Q-015.json'), dict(question(), id='Q-016'))
        write(os.path.join(q, 'Q-017.json'), {'id': 'Q-017', 'doc_id': 'Q-017',
                                              'collection': 'questions', 'data': question()})
        write(os.path.join(q, 'Q-018.json'), dict(question(), id='Q-018', collection='a field'))
        r = self.run_both()
        self.assertEqual([s.rel for s in r.skipped], [
            'questions/Q-011', 'questions/Q-012', 'questions/Q-014', 'questions/Q-015'])
        self.assertIn("its `id` is 'Q-010', not 'Q-011' as its path says", r.skipped[0].why)
        self.assertIn("its `collection` is 'todos', not 'questions'", r.skipped[2].why)
        self.assertFalse(r.complete)
        with Store.open(self.root) as s:
            got = s.docs('questions')
        self.assertEqual(sorted(got), ['Q-001', 'Q-002', 'Q-007', 'Q-009', 'Q-017', 'Q-018'])
        self.assertEqual(got['Q-017'], question())
        self.assertEqual(got['Q-018'], dict(question(), collection='a field'))

    def test_an_id_inside_the_wrapped_data_that_names_another_document_is_skipped(self):
        q = os.path.join(self.export, 'questions')
        write(os.path.join(q, 'Q-011.json'), {'data': dict(question(), id='Q-010')})
        write(os.path.join(q, 'Q-012.json'), {'id': 'Q-012', 'data': dict(question(), id='Q-012')})
        r = self.run_both()
        self.assertEqual([s.rel for s in r.skipped], ['questions/Q-011'])
        self.assertIn("its `data.id` is 'Q-010', not 'Q-011' as its path says", r.skipped[0].why)
        self.assertIsNone(self.stored('questions', 'Q-011'))
        self.assertEqual(self.stored('questions', 'Q-012'), question())

    def test_an_id_that_is_a_number_names_the_file_of_that_text(self):
        q = os.path.join(self.export, 'questions')
        write(os.path.join(q, '21.json'), dict(question(), id=21))
        write(os.path.join(q, '22.json'), {'id': 22, 'doc_id': 22, 'data': dict(question(), id=22)})
        write(os.path.join(q, '23.json'), dict(question(), id=24))
        write(os.path.join(q, '25.json'), dict(question(), id=True))
        r = self.run_both()
        self.assertEqual([s.rel for s in r.skipped], ['questions/23', 'questions/25'])
        self.assertIn("its `id` is 24, not '23' as its path says", r.skipped[0].why)
        self.assertEqual(self.stored('questions', '21'), question())
        self.assertEqual(self.stored('questions', '22'), question())


class RecommendedCase(Case):
    """A question's `recommended` that is a whole number written as text is imported as the
    number: the store refuses every update of a question that holds the text."""

    LINE = ("RECOMMENDED questions/Q-020: its `recommended` is the text '1'; %s as the number 1; "
            "nothing else in the document is changed")

    def setUp(self):
        Case.setUp(self)
        self.text = question(recommended='1', title='as text', options=[
            {'label': 'a', 'description': 'b'}, {'label': 'c', 'description': 'd'}])
        self.number = dict(self.text, recommended=1)
        self.path = os.path.join(self.mirror, 'questions', 'Q-020.json')
        write(self.path, self.text)

    def test_a_whole_number_written_as_text_is_imported_as_the_number_and_said_so(self):
        before = tree(self.mirror)
        r = self.run_both()
        self.assertEqual(r.turned, [migrate.Turned('questions/Q-020', '1', 1)])
        self.assertEqual(self.stored('questions', 'Q-020'), self.number)
        self.assertEqual(tree(self.mirror), before, 'the source keeps its text')
        self.assertEqual(r.findings, [])
        lines = migrate.report_lines(r)
        self.assertEqual([x for x in lines if x.startswith('RECOMMENDED')],
                         [self.LINE % 'imported'])
        self.assertFalse(any('the api will refuse' in x for x in lines))
        with Store.open(self.root) as s:            # the question can now be answered
            s.update('questions', 'Q-020', {'status': 'answered', 'answer': {'choice': 1}})

    def test_a_dry_run_says_it_would_and_writes_nothing(self):
        r = self.run_both(dry_run=True)
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(r.turned, [migrate.Turned('questions/Q-020', '1', 1)])
        self.assertEqual(r.findings, [], 'the findings are of what the store would hold')
        self.assertEqual([x for x in migrate.report_lines(r) if x.startswith('RECOMMENDED')],
                         [self.LINE % 'would be imported'])

    def test_text_that_is_not_a_whole_number_stays_and_is_reported(self):
        kept = {'Q-021': 'one', 'Q-022': '1.0', 'Q-023': '-1', 'Q-024': ' 1', 'Q-025': ''}
        for doc_id, text in kept.items():
            write(os.path.join(self.mirror, 'questions', doc_id + '.json'),
                  dict(self.text, recommended=text))
        # no other field is read as a number, and no other collection's `recommended` is
        write(os.path.join(self.mirror, 'questions', 'Q-026.json'), question(title='7'))
        write(os.path.join(self.mirror, 'todos', 'T-009.json'),
              dict(MIRROR['todos']['T-001'], recommended='1'))
        r = self.run_both()
        self.assertEqual([t.rel for t in r.turned], ['questions/Q-020'])
        for doc_id, text in kept.items():
            self.assertEqual(self.stored('questions', doc_id)['recommended'], text, doc_id)
        self.assertEqual(self.stored('questions', 'Q-026')['title'], '7')
        self.assertEqual(self.stored('todos', 'T-009')['recommended'], '1')
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE ERROR questions/Q-021 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertEqual(
            [x.split(':')[0] for x in lines if 'the api will refuse' in x],
            ['VALIDATE questions/%s' % i for i in sorted(kept)])

    def test_a_second_run_finds_the_number_and_changes_nothing(self):
        self.run_both()
        before = docs_of(self.root)
        r = self.run_both()
        self.assertEqual(docs_of(self.root), before)
        self.assertEqual((r.added, r.conflicts, r.kept, r.turned), ([], [], [], []))
        self.assertIn('questions/Q-020', r.same)
        lines = migrate.report_lines(r)
        self.assertTrue(lines[-1].startswith('nothing changed: '))
        self.assertFalse(any(x.startswith('RECOMMENDED') for x in lines))
        # after a server wrote it, still the same document: not KEPT
        with Store.open(self.root) as s:
            s.put('questions', 'Q-020', self.number)
        again = self.run_both()
        self.assertEqual((again.kept, again.conflicts), ([], []))

    def test_a_wrapped_document_is_turned_inside_its_data(self):
        os.remove(self.path)
        write(os.path.join(self.export, 'questions', 'Q-020.json'),
              {'id': 'Q-020', 'version': 3, 'data': self.text})
        r = self.run_both()
        self.assertEqual(r.turned, [migrate.Turned('questions/Q-020', '1', 1)])
        self.assertEqual(self.stored('questions', 'Q-020'), self.number)

    def test_the_text_and_the_number_in_two_sources_are_one_document(self):
        write(os.path.join(self.export, 'questions', 'Q-020.json'),
              {'id': 'Q-020', 'data': self.number})
        r = self.run_both()
        self.assertEqual(r.conflicts, [])
        self.assertEqual(self.stored('questions', 'Q-020'), self.number)

    def test_a_store_that_already_holds_the_text_keeps_it_and_is_reported(self):
        # a store migrated before, or the mirror folder served in place: never written over
        r = migrate.migrate(self.mirror, [self.mirror])
        self.assertEqual((r.added, r.conflicts, r.kept, r.turned), ([], [], [], []))
        self.assertIn('questions/Q-020', r.same)
        self.assertTrue(r.complete)
        self.assertEqual(on_disk(self.mirror, 'questions', 'Q-020'), self.text,
                         'the file is not rewritten')
        with Store.open(self.mirror) as s:     # the store reads the text as its number
            self.assertEqual(s.get('questions', 'Q-020'), self.number)
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE ERROR questions/Q-020 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertIn('VALIDATE questions/Q-020: the api will refuse every update of this '
                      'document until it is fixed; fix its file by hand with the server stopped '
                      '(no api route repairs it)', lines)
        self.assertTrue(lines[-1].startswith('nothing changed: '))


def labelled(recommended, labels=('Paint it', 'Leave it: as found', 'Ask again'), **kw):
    """A question as the earlier hub's writer left it: `recommended` holds an option's label."""
    return question(recommended=recommended, title='which one', options=[
        {'description': 'what %s means' % x, 'label': x} for x in labels], **kw)


class LabelCase(Case):
    """A question's `recommended` that is exactly one option's label is imported as that
    option's position, counted from 0."""

    LINE = ("RECOMMENDED questions/Q-030: its `recommended` is the text 'Leave it: as found', "
            "the label of the option at position 1 (the first is 0); %s as the number 1; "
            "nothing else in the document is changed")

    def setUp(self):
        Case.setUp(self)
        self.text = labelled('Leave it: as found')
        self.number = dict(self.text, recommended=1)
        self.put('Q-030', self.text)

    def put(self, doc_id, fields):
        write(os.path.join(self.mirror, 'questions', doc_id + '.json'), fields)

    def test_a_label_is_imported_as_its_options_position_and_said_so(self):
        self.put('Q-031', labelled('Paint it', labels=('Paint it', 'Leave it')))    # the first
        self.put('Q-032', labelled('Ask again'))                                    # the last
        before = tree(self.mirror)
        r = self.run_both()
        self.assertEqual(r.turned, [
            migrate.Turned('questions/Q-030', 'Leave it: as found', 1, True),
            migrate.Turned('questions/Q-031', 'Paint it', 0, True),
            migrate.Turned('questions/Q-032', 'Ask again', 2, True)])
        self.assertEqual(self.stored('questions', 'Q-030'), self.number)
        self.assertEqual(self.stored('questions', 'Q-031')['recommended'], 0)
        self.assertEqual(self.stored('questions', 'Q-032')['recommended'], 2)
        self.assertEqual(tree(self.mirror), before, 'the source keeps its text')
        self.assertEqual(r.findings, [])
        lines = migrate.report_lines(r)
        self.assertEqual([x for x in lines if x.startswith('RECOMMENDED')][0],
                         self.LINE % 'imported')
        self.assertFalse(any('the api will refuse' in x for x in lines))
        with Store.open(self.root) as s:            # the question can now be answered
            s.update('questions', 'Q-030', {'status': 'answered', 'answer': {'choice': 1}})

    def test_a_dry_run_says_it_would_and_writes_nothing(self):
        r = self.run_both(dry_run=True)
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(r.findings, [], 'the findings are of what the store would hold')
        self.assertEqual([x for x in migrate.report_lines(r) if x.startswith('RECOMMENDED')],
                         [self.LINE % 'would be imported'])

    def test_text_that_is_not_exactly_one_options_label_stays_and_is_reported(self):
        kept = {
            'Q-033': labelled('Sell it'),                                   # no option's label
            'Q-034': labelled('Paint it', labels=('Paint it', 'Leave it', 'Paint it')),   # two
            'Q-035': labelled('paint it'),                                  # another case
            'Q-036': labelled('Paint it '),                                 # a space more
            'Q-037': question(recommended='Paint it', options=['Paint it', 'Leave it']),
            'Q-038': labelled('0', labels=('Paint it', '0')),   # the number 0, or the option at 1
            # the number 2, or one of the two options that are labelled 2: it does not say
            'Q-040': labelled('2', labels=('2', '2', 'Paint it')),
            'Q-043': labelled('1', labels=('Paint it', '1', '1')),  # even with one of them at 1
        }
        for doc_id, fields in kept.items():
            self.put(doc_id, fields)
        self.put('Q-039', labelled('1', labels=('Paint it', '1')))      # both say 1
        r = self.run_both()
        self.assertEqual([(t.rel, t.new) for t in r.turned], [
            ('questions/Q-030', 1), ('questions/Q-039', 1)])
        for doc_id, fields in kept.items():
            self.assertEqual(self.stored('questions', doc_id), fields, doc_id)
        self.assertTrue(r.complete)
        lines = migrate.report_lines(r)
        self.assertIn(
            "RECOMMENDED questions/Q-039: its `recommended` is the text '1', the label of the "
            "option at position 1 (the first is 0); imported as the number 1; nothing else in "
            "the document is changed", lines)
        self.assertIn("VALIDATE ERROR questions/Q-033 recommended: recommended must be an index "
                      "into options (3 option(s)), not 'Sell it' [options-shape]", lines)
        self.assertIn('VALIDATE ERROR questions/Q-033 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertEqual(
            [x.split(':')[0] for x in lines if 'the api will refuse' in x],
            ['VALIDATE questions/%s' % i for i in sorted(kept)])

    def test_digits_too_many_to_be_a_position_stay_as_text_and_the_run_goes_on(self):
        endless = '9' * 5000                    # more digits than int() reads
        kept = {
            'Q-044': labelled('1234567890'),                                # ten digits
            'Q-045': labelled(endless),
            'Q-046': labelled('1234567890', labels=('Paint it', '1234567890')),  # a label too
        }
        for doc_id, fields in kept.items():
            self.put(doc_id, fields)
        self.put('Q-047', labelled('000000001'))                            # nine: the number 1
        r = self.run_both()
        self.assertEqual([(t.rel, t.new) for t in r.turned], [
            ('questions/Q-030', 1), ('questions/Q-047', 1)])
        for doc_id, fields in kept.items():
            self.assertEqual(self.stored('questions', doc_id), fields, doc_id)
        self.assertLessEqual({'questions/Q-0%d' % n for n in (30, 44, 45, 46, 47)}, set(r.added))
        self.assertTrue(r.complete)
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE ERROR questions/Q-044 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertEqual(
            [x.split(':')[0] for x in lines if 'the api will refuse' in x],
            ['VALIDATE questions/%s' % i for i in sorted(kept)])

    def test_the_empty_text_is_never_an_options_label(self):
        fields = labelled('', labels=('', 'Paint it'))
        self.put('Q-048', fields)
        r = self.run_both()
        self.assertEqual([t.rel for t in r.turned], ['questions/Q-030'])
        self.assertEqual(self.stored('questions', 'Q-048'), fields)
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE ERROR questions/Q-048 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertFalse(any(x.startswith('RECOMMENDED questions/Q-048') for x in lines))

    def test_a_number_past_the_last_option_is_imported_as_that_number_and_reported(self):
        self.put('Q-049', labelled('7'))
        r = self.run_both()
        self.assertIn(migrate.Turned('questions/Q-049', '7', 7, False), r.turned)
        self.assertEqual(self.stored('questions', 'Q-049'), labelled(7))
        lines = migrate.report_lines(r)
        self.assertIn("RECOMMENDED questions/Q-049: its `recommended` is the text '7'; imported "
                      "as the number 7; nothing else in the document is changed", lines)
        self.assertIn('VALIDATE ERROR questions/Q-049 recommended: recommended must be an index '
                      'into options (3 option(s)), not 7 [options-shape]', lines)

    def test_an_answered_question_keeps_its_answer_as_it_is(self):
        answer = {'at': AT, 'choice': 0, 'label': 'Paint it', 'note': ''}
        fields = labelled('Paint it', status='answered', answer=answer)
        self.put('Q-041', fields)
        self.run_both()
        self.assertEqual(self.stored('questions', 'Q-041'), dict(fields, recommended=0))
        self.assertEqual(self.stored('questions', 'Q-041')['answer'], answer)

    def test_a_second_run_finds_the_number_and_changes_nothing(self):
        self.run_both()
        before = docs_of(self.root)
        r = self.run_both()
        self.assertEqual(docs_of(self.root), before)
        self.assertEqual((r.added, r.conflicts, r.kept, r.turned), ([], [], [], []))
        self.assertIn('questions/Q-030', r.same)
        self.assertEqual(self.stored('questions', 'Q-030'), self.number,
                         'the store holds the number and the source the label: one document')
        lines = migrate.report_lines(r)
        self.assertTrue(lines[-1].startswith('nothing changed: '))
        self.assertFalse(any(x.startswith('RECOMMENDED') for x in lines))

    def test_a_store_that_already_holds_the_label_keeps_it_and_is_reported(self):
        r = migrate.migrate(self.mirror, [self.mirror])
        self.assertEqual((r.added, r.conflicts, r.kept, r.turned), ([], [], [], []))
        self.assertIn('questions/Q-030', r.same)
        self.assertTrue(r.complete)
        self.assertEqual(on_disk(self.mirror, 'questions', 'Q-030'), self.text,
                         'the file is not rewritten')
        with Store.open(self.mirror) as s:     # the store reads the label as its position
            self.assertEqual(s.get('questions', 'Q-030'), self.number)
        lines = migrate.report_lines(r)
        self.assertIn('VALIDATE questions/Q-030: the api will refuse every update of this '
                      'document until it is fixed; fix its file by hand with the server stopped '
                      '(no api route repairs it)', lines)
        self.assertTrue(lines[-1].startswith('nothing changed: '))
        # a second source that holds the number is the same document: no conflict
        fill(self.export, {'questions': {'Q-030': self.number}}, wrapped=True)
        again = migrate.migrate(self.mirror, [self.mirror, self.export])
        self.assertEqual(again.conflicts, [])


class MigrateCliCase(cli.CliCase):
    def setUp(self):
        cli.CliCase.setUp(self)
        self.mirror = fill(os.path.join(self.home, 'mirror'), MIRROR)
        self.export = fill(os.path.join(self.home, 'export'), EXPORT, wrapped=True)

    def migrate_cli(self, *more):
        return self.run_cli('migrate', '--from', self.mirror, '--from', self.export,
                            '--store', self.store_dir, *more)

    def test_migrate_prints_the_report_and_a_second_run_says_nothing_changed(self):
        code, out, err = self.migrate_cli()
        self.assertEqual((code, err), (0, ''))
        lines = out.splitlines()
        self.assertEqual(lines[0], 'hub store: %s' % self.store_dir)
        self.assertIn('source: %s' % self.mirror, lines)
        self.assertIn('questions/: imported 4, already in the store 0, store differs and is kept '
                      '0, conflict 0', lines)
        self.assertIn('VALIDATE clean: no finding', lines)
        self.assertEqual(lines[-1], 'imported 15 documents; no source was changed')
        code, out, err = self.migrate_cli()
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(out.splitlines()[-1].startswith('nothing changed: '))
        with Store.open(self.store_dir) as s:
            self.assertEqual(sorted(s.docs('questions')), ['Q-001', 'Q-002', 'Q-007', 'Q-009'])

    def test_migrate_is_refused_while_a_server_holds_the_store(self):
        self.server()
        self.assertTrue(self.locked())
        code, out, err = self.migrate_cli()
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: ', err)
        self.assertIn('a hub server has this store open', err)
        self.assertFalse(os.path.exists(os.path.join(self.store_dir, 'questions')))

    def test_a_dry_run_writes_nothing(self):
        code, out, err = self.migrate_cli('--dry-run')
        self.assertEqual((code, err), (0, ''))
        self.assertFalse(os.path.exists(self.store_dir))
        self.assertIn('(dry run: nothing is written)', out.splitlines()[0])

    def test_a_document_left_out_is_exit_1_with_the_report(self):
        write(os.path.join(self.export, 'pages', 'guide.json'), {'title': 'Another guide'})
        code, out, err = self.migrate_cli()
        self.assertEqual((code, err), (1, ''))
        self.assertIn('NOT COMPLETE: 1 document not imported', out)

    def test_usage_errors_and_a_missing_source(self):
        for argv in (('migrate',), ('migrate', '--from', ''),
                     ('migrate', '--from', self.mirror, '--store', '')):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn('USAGE: ', err)
        code, out, err = self.run_cli('migrate', '--from', os.path.join(self.home, 'absent'),
                                      '--store', self.store_dir)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: ', err)
        self.assertIn('is not a folder', err)
        self.assertIn('USAGE: argument --from: --from needs a folder, not an empty value',
                      self.run_cli('migrate', '--from', '')[2])
        self.assertIn('--store needs a folder, not an empty value',
                      self.run_cli('migrate', '--from', self.mirror, '--store', '')[2])

    def test_the_default_layout_with_a_newer_copy_in_an_export_is_exit_1(self):
        write(os.path.join(self.export, 'questions', 'Q-002.json'),
              {'id': 'Q-002', 'data': question(status='logged', asked_at=LATER)})
        code, out, err = self.run_cli('migrate', '--from', self.mirror, '--from', self.export,
                                      '--store', self.mirror)
        self.assertEqual((code, err), (1, ''))
        self.assertIn("CONFLICT questions/Q-002: the store's copy, which no server has written",
                      out)
        self.assertIn('NOT COMPLETE: 1 document not imported', out)

    def test_no_store_folder_set_is_refused(self):
        code, out, err = self.run_cli('migrate', '--from', self.mirror)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: no store folder is set', err)


if __name__ == '__main__':
    unittest.main()
