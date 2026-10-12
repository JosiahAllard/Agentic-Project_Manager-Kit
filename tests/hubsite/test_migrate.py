"""kitlib.hubsite.migrate: the mirror folder and an export copied into the hub store. Every folder
is a scratch one under a temp folder; a server, where one runs, is on port 0."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import hubmirror
from kitlib.hubsite import migrate
from kitlib.hubsite.migrate import Busy, Conflict, Kept, Left, MigrateError
from kitlib.hubsite.store import LOCK_NAME, META_NAME, Store
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


# The mirror folder: what kitlib.hubmirror keeps, parked/ included.
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
        f.write(obj if isinstance(obj, str) else hubmirror.normal_text(obj))


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
        with Store.open(self.root) as s:
            return s.get(coll, doc_id)


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
                         'the mirror must still be a mirror: hubmirror.save refuses a folder '
                         'that holds the revision file')
        self.assertEqual(hubmirror.save({'questions': MIRROR['questions']}, self.mirror), [])


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
                         [Conflict('questions/Q-002', [self.mirror, self.export], self.export)])
        self.assertIsNone(self.stored('questions', 'Q-002'))
        self.assertFalse(r.complete)
        self.assertEqual(len(r.added), 14, 'every other document is imported')
        self.assertEqual((tree(self.mirror), tree(self.export)), before)
        lines = migrate.report_lines(r)
        self.assertIn('CONFLICT questions/Q-002: %s and %s hold different fields; imported from '
                      'neither (the later by its time stamps: %s)'
                      % (self.mirror, self.export, self.export), lines)
        self.assertIn('NOT COMPLETE: 1 document not imported (CONFLICT and SKIPPED above); the '
                      'sources are unchanged', lines)
        # resolved at a source, the next run imports it
        write(os.path.join(self.export, 'questions', 'Q-002.json'), MIRROR['questions']['Q-002'])
        again = self.run_both()
        self.assertEqual((again.added, again.conflicts), (['questions/Q-002'], []))

    def test_a_conflict_with_no_time_to_tell_names_no_later_source(self):
        write(os.path.join(self.export, 'pages', 'guide.json'), {'title': 'Another guide'})
        r = self.run_both()
        self.assertEqual(r.conflicts, [Conflict('pages/guide', [self.mirror, self.export], None)])
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
        self.assertEqual(r.kept, [Kept('questions/Q-001', [self.mirror, self.export])])
        self.assertEqual(self.stored('questions', 'Q-001')['title'], 'the store was first')


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

    def test_no_store_folder_set_is_refused(self):
        code, out, err = self.run_cli('migrate', '--from', self.mirror)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: no store folder is set', err)


if __name__ == '__main__':
    unittest.main()
