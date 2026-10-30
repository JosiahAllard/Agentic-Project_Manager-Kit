"""kitlib.records.gate_ok: the gate-OK record written and verified against real throwaway repos."""
import contextlib
import hashlib
import json
import os
import time
import unittest
from unittest import mock

from kitlib.records import gate_ok
from tests import gitfixture
from tests.home import result_drive

GATE = {'steps': [{'name': 'unit', 'run': ['python', '-m', 'unittest']}], 'fail': ['FAILED']}
KIT = {'version': '0.1', 'commit': 'a' * 40}
STEPS = [{'name': 'unit', 'result': 'pass', 'seconds': 1.5}, {'name': 'docs'}]
AUTHOR = '%s <%s>' % (gitfixture.NAME, gitfixture.EMAIL)


class FakeClock(object):
    """time.monotonic and time.sleep for the `with` of patched(): a sleep is recorded and moves
    the clock on at once, so a retry loop's tries and pauses are counted exactly."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(time, 'monotonic', self.monotonic))
        stack.enter_context(mock.patch.object(time, 'sleep', self.sleep))
        return stack


class GateOkCase(unittest.TestCase):

    def setUp(self):
        self.drive = result_drive(self)     # the record folder is under its dev_drive
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.head = self.repo.initial

    def passed(self, rev=None, root=None, **kw):
        return gate_ok.record_pass(root or self.repo.root, rev or self.head, kw.pop('gate', GATE), KIT,
                                   'kit-test', '2026-10-06T10:00:00Z', STEPS, **kw)

    def rewrite(self, path, **changes):
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        rec.update(changes)
        for k in [k for k, v in changes.items() if v is gate_ok]:
            del rec[k]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)


class WriteTest(GateOkCase):

    def test_a_pass_writes_every_key_from_git_and_the_caller(self):
        path, rec = self.passed('HEAD', base='main')
        self.assertEqual(sorted(rec), sorted(gate_ok.KEYS + gate_ok.ADDED_KEYS))
        self.assertIsNone(rec['config_rev'])        # no config_rev given
        tree = self.repo.git('rev-parse', self.head + '^{tree}')
        self.assertEqual((rec['schema'], rec['verdict'], rec['commit'], rec['tree'], rec['base']),
                         (1, 'OK', self.head, tree, self.head))
        self.assertEqual((rec['author'], rec['repo'], rec['kit']), (AUTHOR, 'kit-test', KIT))
        self.assertEqual(rec['gate_config'], gate_ok.config_hash(GATE))
        self.assertEqual(rec['steps'], [{'name': 'unit', 'result': 'pass', 'seconds': 1.5},
                                        {'name': 'docs', 'result': 'pass', 'seconds': None}])
        self.assertEqual(rec['started_utc'], '2026-10-06T10:00:00Z')
        self.assertRegex(rec['ended_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), rec)

    def test_the_source_is_written_and_an_older_record_reads_as_gate_json(self):
        path, rec = self.passed(source='auto')
        self.assertEqual(rec['source'], 'auto')
        self.assertEqual(gate_ok.source_of(rec), 'auto')
        self.assertIsNone(gate_ok.verify(self.repo.root, self.head, GATE)[1])
        self.rewrite(path, source=gate_ok)              # a record from before the key
        rec, why = gate_ok.verify(self.repo.root, self.head, GATE)
        self.assertIsNone(why)
        self.assertNotIn('source', rec)
        self.assertEqual(gate_ok.source_of(rec), 'gate.json')

    def test_input_keys_are_written_as_given_and_a_malformed_set_is_refused(self):
        self.assertIsNone(self.passed()[1]['input_keys'])
        keys = {'schema': 1, 'steps': {'unit': {'argv': 'a' * 64, 'kit': '0' * 64}}}
        path, rec = self.passed(input_keys=keys)
        self.assertEqual(rec['input_keys'], keys)
        self.assertIsNone(gate_ok.verify(self.repo.root, self.head, GATE)[1])
        for bad in ({'schema': 1}, {'schema': True, 'steps': {}}, {'schema': '1', 'steps': {}},
                    {'schema': 1, 'steps': {}, 'more': 1}, {'schema': 1, 'steps': {'unit': {}}},
                    {'schema': 1, 'steps': {'unit': {'argv': 'A' * 64}}},
                    {'schema': 1, 'steps': {'unit': {'argv': 'a' * 63}}},
                    {'schema': 1, 'steps': {'': {'argv': 'a' * 64}}}, ['schema']):
            with self.assertRaises(gate_ok.RecordError, msg=bad):
                self.passed(input_keys=bad)

    def test_the_record_is_in_the_drive_folder_with_no_git_copy_whichever_checkout_writes_it(self):
        wt = self.repo.worktree('a')
        path, rec = self.passed(root=wt)
        self.assertEqual(os.path.dirname(path), gate_ok.record_dir(self.repo.root))
        self.assertEqual(gate_ok.record_dir(wt), gate_ok.record_dir(self.repo.root))
        self.assertTrue(os.path.normcase(path).startswith(os.path.normcase(
            os.path.join(self.drive, 'pm-kit', 'kit-gate-ok') + os.sep)))
        self.assertEqual(os.path.basename(path), self.head + '.json')
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), rec)
        self.assertFalse(os.path.exists(os.path.join(self.repo.common_dir(), 'kit-gate-ok')))
        self.assertIsNotNone(gate_ok.verify(self.repo.root, self.head, GATE)[0])

    def test_a_write_leaves_no_temp_file_and_a_second_pass_replaces_the_first(self):
        path, first = self.passed()
        path2, second = self.passed(gate={'other': True})
        self.assertEqual(path, path2)
        self.assertEqual(os.listdir(os.path.dirname(path)), [self.head + '.json'])
        self.assertEqual(gate_ok.read(self.repo.root, self.head)[0]['gate_config'], second['gate_config'])
        self.assertNotEqual(first['gate_config'], second['gate_config'])

    def test_a_replace_held_open_is_tried_five_times_0_2_s_apart_then_a_record_error(self):
        # The equivalence test of _write_file's move onto kitlib.fs.replace: the loop it replaced
        # tried RETRIES (5) times, sleeping RETRY_SLEEP (0.2 s) between, then raised the
        # PermissionError (a RecordError here), the temp file removed. With a dev_drive the record
        # folder's write is the only one: no .git copy is written. The clock is faked, so the
        # count is exact.
        folder_rec = os.path.normcase(gate_ok.record_path(self.repo.root, self.head))
        copy = gate_ok.legacy_path(self.repo.root, self.head)
        for fails, tries, written in ((2, 3, True), (4, 5, True), (5, 5, False), (9, 5, False)):
            with self.subTest(fails=fails):
                for p in (folder_rec, copy):
                    if os.path.exists(p):
                        os.remove(p)
                real, calls, others, clock = os.replace, [], [], FakeClock()

                def flaky(a, b):
                    if os.path.normcase(b) != folder_rec:
                        others.append(b)
                        return real(a, b)
                    calls.append(b)
                    if len(calls) <= fails:
                        raise PermissionError('held open')
                    return real(a, b)
                with clock.patched(), mock.patch.object(gate_ok.os, 'replace', flaky):
                    if written:
                        self.passed()
                    else:
                        with self.assertRaises(gate_ok.RecordError) as cm:
                            self.passed()
                self.assertEqual(len(calls), tries)
                self.assertEqual(clock.sleeps, [0.2] * (tries - 1))
                self.assertEqual((os.path.exists(folder_rec), os.path.exists(copy), len(others)),
                                 (written, False, 0))
                if not written:
                    self.assertIn('held open', str(cm.exception))
                for folder in (os.path.dirname(folder_rec),):
                    self.assertEqual([n for n in os.listdir(folder) if n.endswith('.tmp')], [])

    def test_the_replace_goes_through_kitlib_fs_replace_with_the_old_bounds(self):
        real, seen = gate_ok.fsreplace.replace, []

        def spy(src, dst, wait=None, pause=None):
            seen.append((os.path.normcase(dst), wait, pause))
            return real(src, dst, wait=wait, pause=pause)
        with mock.patch.object(gate_ok.fsreplace, 'replace', spy):
            path, _ = self.passed()
        bounds = (gate_ok.REPLACE_WAIT, 0.2)
        self.assertEqual(seen, [(os.path.normcase(path),) + bounds])
        self.assertAlmostEqual(gate_ok.REPLACE_WAIT, 0.7)

    def test_a_replace_refused_otherwise_is_not_retried(self):
        calls, clock = [], FakeClock()

        def refused(a, b):
            calls.append(b)
            raise OSError('disk said no')
        with clock.patched(), mock.patch.object(gate_ok.os, 'replace', refused):
            with self.assertRaises(gate_ok.RecordError) as cm:
                self.passed()
        self.assertIn('disk said no', str(cm.exception))
        self.assertEqual((len(calls), clock.sleeps), (1, []))

    def test_a_write_that_fails_part_way_leaves_no_temp_file(self):
        rec = gate_ok.build(self.repo.root, self.head, GATE, KIT, 'kit-test', 'x', STEPS)
        with mock.patch.object(gate_ok.json, 'dumps', side_effect=OSError('disk full')):
            with self.assertRaises(gate_ok.RecordError) as cm:
                gate_ok.write(self.repo.root, rec)
        self.assertIn('disk full', str(cm.exception))
        for folder in (gate_ok.legacy_dir(self.repo.root), gate_ok.machine_dir()):
            for _dirpath, _dirs, files in os.walk(folder):
                self.assertEqual(files, [], folder)

    def test_what_git_cannot_answer_or_the_caller_gets_wrong_refuses(self):
        for kw in ({'rev': 'no-such-branch'}, {'base': 'no-such-branch'}):
            with self.assertRaises(gate_ok.RecordError):
                self.passed(**kw)
        root = self.repo.root
        for kit, steps in (({}, STEPS), (None, STEPS), (KIT, [{'result': 'pass'}]), (KIT, ['unit']),
                           (KIT, [{'name': 'unit', 'seconds': True}]),
                           (KIT, [{'name': 'unit', 'seconds': '3'}])):
            with self.assertRaises(gate_ok.RecordError):
                gate_ok.build(root, self.head, GATE, kit, 'kit-test', 'x', steps)
        with self.assertRaises(gate_ok.RecordError):
            gate_ok.write(root, {'commit': 'HEAD'})
        self.assertFalse(os.path.exists(os.path.join(self.repo.common_dir(), 'kit-gate-ok')))
        self.assertFalse(os.path.exists(gate_ok.machine_dir()))

    def test_a_record_is_named_only_by_a_full_hash(self):
        for name in ('HEAD', 'main', self.head[:12], '', None, '../' + self.head, self.head + chr(10)):
            with self.assertRaises(gate_ok.RecordError):
                gate_ok.record_path(self.repo.root, name)

    def test_outside_a_repository_there_is_no_record_dir(self):
        with self.assertRaises(gate_ok.RecordError):
            gate_ok.record_dir(os.path.dirname(self.repo.root))


class VerifyTest(GateOkCase):

    def test_a_fresh_record_verifies_by_hash_or_by_name(self):
        _, rec = self.passed()
        self.assertEqual(gate_ok.verify(self.repo.root, self.head, GATE), (rec, None))
        self.assertEqual(gate_ok.verify(self.repo.root, 'main')[0], rec)

    def test_no_record_and_an_unknown_revision_refuse(self):
        rec, why = gate_ok.verify(self.repo.root, self.head)
        self.assertIsNone(rec)
        self.assertIn('no gate-OK record for %s' % self.head, why)
        self.assertIn('cannot resolve', gate_ok.verify(self.repo.root, 'no-such-branch')[1])
        self.assertIn('cannot resolve', gate_ok.verify(self.repo.root, None)[1])

    def test_a_new_commit_is_not_covered_by_its_parents_record(self):
        self.passed()
        self.repo.write('x.txt', 'x\n')
        new = self.repo.commit('more', 'x.txt')
        self.assertIn('no gate-OK record for %s' % new, gate_ok.verify(self.repo.root, new)[1])

    def test_an_amended_author_is_a_new_commit_with_no_record(self):
        self.passed()
        self.repo.git('commit', '-q', '--amend', '--no-edit', '--author', 'Other <agent@example.invalid>')
        amended = self.repo.git('rev-parse', 'HEAD')
        self.assertNotEqual(amended, self.head)
        self.assertIsNone(gate_ok.verify(self.repo.root, amended)[0])

    def test_another_gate_config_refuses_and_key_order_does_not_matter(self):
        self.passed()
        reordered = {'fail': ['FAILED'], 'steps': [{'run': ['python', '-m', 'unittest'], 'name': 'unit'}]}
        self.assertIsNotNone(gate_ok.verify(self.repo.root, self.head, reordered)[0])
        rec, why = gate_ok.verify(self.repo.root, self.head, dict(GATE, fail=['FAILED', 'ERROR']))
        self.assertIsNone(rec)
        self.assertIn('another gate config', why)

    def test_every_field_git_can_answer_is_checked(self):
        path, rec = self.passed()
        self.repo.write('x.txt', 'x\n')
        other = self.repo.commit('more', 'x.txt')
        other_tree = self.repo.git('rev-parse', other + '^{tree}')
        cases = (({'tree': other_tree}, 'names tree'),
                 ({'author': 'Other <agent@example.invalid>'}, 'names author'),
                 ({'commit': other}, 'names commit'),
                 ({'schema': 2}, 'is schema 2'),
                 ({'verdict': 'FAIL'}, "says 'FAIL'"),
                 ({'steps': gate_ok}, 'lacks steps'))
        for change, why in cases:
            self.passed()
            self.rewrite(path, **change)
            got, msg = gate_ok.verify(self.repo.root, self.head)
            self.assertIsNone(got, change)
            self.assertIn(why, msg)

    def test_a_record_copied_to_another_commits_name_refuses(self):
        path, _ = self.passed()
        self.repo.write('x.txt', 'x\n')
        other = self.repo.commit('more', 'x.txt')
        with open(path, encoding='utf-8') as f:
            body = f.read()
        with open(os.path.join(os.path.dirname(path), other + '.json'), 'w', encoding='utf-8') as f:
            f.write(body)
        self.assertIn('filed under %s names commit %s' % (other, self.head),
                      gate_ok.verify(self.repo.root, other)[1])

    def test_a_file_that_is_not_a_record_refuses(self):
        path, _ = self.passed()
        for body in ('{', '[]', '"OK"'):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(body)
            rec, why = gate_ok.verify(self.repo.root, self.head)
            self.assertIsNone(rec)
            self.assertIn('is not a gate-OK record', why)


class LegacyTest(GateOkCase):
    """The .git copy (legacy_path): with a dev_drive it is neither written nor read; with none
    (a load node set up without one) it is the only record, written and read as before."""

    def git_copy(self, rev=None, **kw):
        """A record written only to the .git folder, as a gate with no dev_drive writes it."""
        rec = gate_ok.build(self.repo.root, rev or self.head, kw.pop('gate', GATE), KIT, 'kit-test',
                            '2026-10-06T10:00:00Z', STEPS, **kw)
        path = gate_ok.legacy_path(self.repo.root, rec['commit'])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(rec, sort_keys=True, indent=1) + '\n')
        return path, rec

    def test_with_a_dev_drive_a_git_copy_alone_is_not_read(self):
        self.git_copy(full=True)
        self.assertFalse(os.path.exists(gate_ok.record_path(self.repo.root, self.head)))
        self.assertEqual(gate_ok.read(self.repo.root, self.head), (None, 'none'))
        self.assertIsNone(gate_ok.verify(self.repo.root, self.head)[0])
        self.assertIsNone(gate_ok.verify_full(self.repo.root, self.head)[0])

    def test_with_a_dev_drive_a_git_copy_beside_the_record_is_ignored(self):
        path, rec = self.passed()
        self.git_copy(full=True, mode='full')
        self.assertEqual(gate_ok.verify(self.repo.root, self.head), (rec, None))
        self.assertIn('without --full', gate_ok.verify_full(self.repo.root, self.head)[1])
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{')
        got, why = gate_ok.verify(self.repo.root, self.head)
        self.assertIsNone(got)
        self.assertIn('is not a gate-OK record', why)       # a bad record is not passed over

    def test_with_no_dev_drive_only_the_git_copy_is_written_and_it_is_read(self):
        with mock.patch('kitlib.config.user.dev_drive', return_value=None):
            with self.assertRaises(gate_ok.NoRecordFolder) as cm:
                self.passed()
            self.assertIn('dev_drive', str(cm.exception))
            with open(gate_ok.legacy_path(self.repo.root, self.head), encoding='utf-8') as f:
                rec = json.load(f)
            self.assertEqual(rec['commit'], self.head)
            self.assertEqual(gate_ok.verify(self.repo.root, self.head), (rec, None))
        self.assertFalse(os.path.exists(os.path.join(self.drive, 'pm-kit')))

    def blocked(self, folder):
        """A file where `folder` should be, so nothing can be written in it."""
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        with open(folder, 'w', encoding='utf-8') as f:
            f.write('not a folder\n')

    def test_a_failed_folder_write_leaves_no_git_copy_to_read(self):
        self.blocked(gate_ok.record_dir(self.repo.root))
        with self.assertRaises(gate_ok.RecordError) as cm:
            self.passed()
        self.assertNotIsInstance(cm.exception, gate_ok.NoRecordFolder)
        self.assertFalse(os.path.exists(gate_ok.legacy_path(self.repo.root, self.head)))
        self.assertEqual(gate_ok.read(self.repo.root, self.head), (None, 'none'))

    def test_with_a_dev_drive_a_blocked_git_folder_does_not_stop_the_write(self):
        self.blocked(gate_ok.legacy_dir(self.repo.root))
        path, rec = self.passed()
        self.assertEqual(gate_ok.read(self.repo.root, self.head), (rec, None))

    def test_with_no_record_anywhere_the_answer_is_none(self):
        self.assertEqual(gate_ok.read(self.repo.root, self.head), (None, 'none'))
        with mock.patch('kitlib.config.user.dev_drive', return_value=None):
            self.assertEqual(gate_ok.read(self.repo.root, self.head), (None, 'none'))


class FullTest(GateOkCase):
    """The `full` key: true only from a `gate.py --full` pass; verify_full takes nothing else."""

    def test_full_is_written_false_by_default_and_true_when_asked(self):
        _, rec = self.passed()
        self.assertIs(rec['full'], False)
        self.assertFalse(gate_ok.full_of(rec))
        _, rec = self.passed(full=True)
        self.assertIs(rec['full'], True)
        self.assertTrue(gate_ok.full_of(rec))
        got, why = gate_ok.verify_full(self.repo.root, self.head)
        self.assertIsNone(why)
        self.assertEqual(got['commit'], self.head)

    def test_a_default_mode_record_is_refused(self):
        self.passed()
        rec, why = gate_ok.verify_full(self.repo.root, self.head)
        self.assertIsNone(rec)
        self.assertIn('without --full', why)

    def test_a_record_from_before_the_key_does_not_count(self):
        path, _ = self.passed(full=True)
        self.rewrite(path, full=gate_ok)                # written before records carried the key
        rec, why = gate_ok.verify(self.repo.root, self.head)
        self.assertIsNone(why)                          # still a record for everything else
        self.assertFalse(gate_ok.full_of(rec))
        rec, why = gate_ok.verify_full(self.repo.root, self.head)
        self.assertIsNone(rec)
        self.assertIn('does not say whether the gate ran --full', why)
        for odd in ('true', 1, None):                   # only the JSON true counts
            self.rewrite(path, full=odd)
            self.assertIsNone(gate_ok.verify_full(self.repo.root, self.head)[0], odd)

    def test_verify_reasons_come_first(self):
        rec, why = gate_ok.verify_full(self.repo.root, self.head)
        self.assertIsNone(rec)
        self.assertEqual(why, 'no gate-OK record for %s' % self.head)
        path, _ = self.passed(full=True)
        self.rewrite(path, tree='0' * 40)
        rec, why = gate_ok.verify_full(self.repo.root, self.head)
        self.assertIsNone(rec)
        self.assertIn('names tree %s' % ('0' * 40), why)


class ModeTest(GateOkCase):
    """The `mode` key: full, select or docs; mode_of reads an older record by its `full`."""

    def test_mode_follows_full_by_default_and_docs_is_written_when_asked(self):
        _, rec = self.passed()
        self.assertEqual((rec['mode'], gate_ok.mode_of(rec)), ('select', 'select'))
        _, rec = self.passed(full=True)
        self.assertEqual((rec['mode'], gate_ok.mode_of(rec)), ('full', 'full'))
        _, rec = self.passed(mode='docs')
        self.assertEqual((rec['mode'], gate_ok.mode_of(rec), rec['full']), ('docs', 'docs', False))

    def test_a_mode_that_disagrees_with_full_is_never_written(self):
        for kw in ({'mode': 'full'}, {'full': True, 'mode': 'select'}, {'full': True, 'mode': 'docs'},
                   {'mode': 'partial'}):
            with self.assertRaises(gate_ok.RecordError, msg=kw):
                self.passed(**kw)

    def test_a_record_without_the_key_is_full_only_when_full_says_so(self):
        path, _ = self.passed(full=True)
        self.rewrite(path, mode=gate_ok)                # written before records carried the key
        self.assertEqual(gate_ok.mode_of(gate_ok.verify(self.repo.root, self.head)[0]), 'full')
        self.rewrite(path, full=False)
        self.assertIsNone(gate_ok.mode_of(gate_ok.verify(self.repo.root, self.head)[0]))

    def test_an_odd_or_contradicting_mode_is_unknown(self):
        self.assertIsNone(gate_ok.mode_of({'mode': 'partial', 'full': False}))
        self.assertIsNone(gate_ok.mode_of({'mode': 'full', 'full': False}))
        self.assertIsNone(gate_ok.mode_of({'mode': 'select', 'full': True}))
        self.assertIsNone(gate_ok.mode_of({'mode': 'full', 'full': 'true'}))  # only JSON true is full


class HashTest(unittest.TestCase):

    def test_the_config_hash_is_sha256_of_sorted_compact_utf8_json(self):
        self.assertEqual(gate_ok.config_hash({'b': [1, 2], 'a': 'é'}),
                         hashlib.sha256('{"a":"é","b":[1,2]}'.encode('utf-8')).hexdigest())
        self.assertEqual(gate_ok.config_hash({'a': 1, 'b': 2}), gate_ok.config_hash({'b': 2, 'a': 1}))
        self.assertNotEqual(gate_ok.config_hash({'a': 1}), gate_ok.config_hash({'a': 2}))
        self.assertNotEqual(gate_ok.config_hash([1, 2]), gate_ok.config_hash([2, 1]))



RAN_ON = {'node': 'box1', 'host_key': 'SHA256:' + 'k' * 43, 'job': 'j-20261028T120000Z-abcdef'}
KEYS_SET = {'schema': 1, 'steps': {'unit': {'argv': 'a' * 64}}}


class ImportTest(GateOkCase):
    """import_record: a record a load node's gate.py --full wrote, taken into this machine's
    record folder only when everything verify() checks holds against main's gate config at the
    commit, it is a --full pass of this repository, and ran_on is main's own."""

    def node_record(self, rev=None, **kw):
        """The record a node's gate builds for `rev`, never written here."""
        rev = rev or self.head
        kw.setdefault('config_rev', rev)
        kw.setdefault('full', True)
        kw.setdefault('input_keys', KEYS_SET)
        return gate_ok.build(self.repo.root, rev, kw.pop('gate', GATE), KIT,
                             kw.pop('repo', 'kit-test'), '2026-10-06T10:00:00Z', STEPS, **kw)

    def take(self, rec, commit=None, ran_on=RAN_ON, config_at=lambda rev: GATE, repo='kit-test'):
        return gate_ok.import_record(self.repo.root, rec, commit or self.head, ran_on, config_at,
                                     repo)

    def test_a_node_record_is_taken_with_ran_on_and_no_input_keys_and_then_verifies(self):
        rec = self.node_record()
        path, out = self.take(rec)
        self.assertEqual(rec['input_keys'], KEYS_SET)             # the node's dict is untouched
        self.assertNotIn('ran_on', rec)
        self.assertEqual(out, dict(rec, input_keys=None, ran_on=RAN_ON))
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), out)
        self.assertEqual(gate_ok.verify(self.repo.root, self.head, GATE), (out, None))
        self.assertEqual(gate_ok.verify(self.repo.root, self.head,
                                        config_at=lambda rev: GATE), (out, None))
        self.assertEqual(gate_ok.verify_full(self.repo.root, self.head), (out, None))

    def test_it_replaces_a_local_record_of_the_commit(self):
        self.passed()
        _, out = self.take(self.node_record())
        self.assertEqual(gate_ok.verify(self.repo.root, self.head)[0]['ran_on'], RAN_ON)

    def refused(self, want, rec=None, **kw):
        with self.assertRaises(gate_ok.RecordError) as cm:
            self.take(self.node_record() if rec is None else rec, **kw)
        self.assertIn(want, str(cm.exception))
        self.assertIsNone(gate_ok.verify(self.repo.root, self.head)[0])   # nothing written

    def test_what_main_cannot_take_is_refused_and_nothing_is_written(self):
        good = self.node_record()
        self.refused('no gate-OK record', rec=[good])
        self.refused('full commit hash', commit='main')
        for ran_on in (None, {}, dict(RAN_ON, node=''), dict(RAN_ON, job=7),
                       dict(RAN_ON, extra='x'), {'node': 'box1', 'job': 'j'}):
            with self.subTest(ran_on=ran_on):
                self.refused('ran_on is {node, host_key, job}', ran_on=ran_on)
        self.refused('checked against the gate config', config_at=None)
        self.refused('another gate config than its config_rev',
                     config_at=lambda rev: dict(GATE, fail=['FAILED', 'ERROR']))
        self.refused('names no config_rev', rec=self.node_record(config_rev=None))
        self.refused('not from a --full gate', rec=self.node_record(full=False))
        self.refused('not from a --full gate', rec=dict(good, mode='select'))
        self.refused("of repository 'other', not 'kit-test'", rec=self.node_record(repo='other'))
        self.refused('names tree', rec=dict(good, tree='f' * 40))
        self.refused("says 'FAIL'", rec=dict(good, verdict='FAIL'))
        self.refused('lacks steps', rec={k: v for k, v in good.items() if k != 'steps'})

    def test_a_config_that_cannot_be_read_at_the_commit_refuses(self):
        def unreadable(rev):
            raise gate_ok.RecordError('the gate config at %s cannot be read' % rev[:10])
        self.refused('cannot be read', config_at=unreadable)

    def test_a_record_of_another_commit_or_config_rev_refuses(self):
        first = self.head
        self.repo.write('x.txt', 'x\n')
        self.head = self.repo.commit('more', 'x.txt')
        self.refused('names commit %s' % first, rec=self.node_record(first, config_rev=first))
        self.refused('names config_rev %s, not the commit itself' % first,
                     rec=self.node_record(config_rev=first))


class RanOnTest(GateOkCase):
    """build/record_pass `ran_on`: a landing's merged gate checked on a load node is recorded on
    main with the node it ran on, and with no input_keys, so no later gate serves a step from it."""

    def test_a_record_with_ran_on_is_written_and_verifies_with_it(self):
        path, rec = self.passed(full=True, config_rev=self.head, ran_on=RAN_ON)
        self.assertEqual((rec['ran_on'], rec['input_keys']), (RAN_ON, None))
        self.assertIsNot(rec['ran_on'], RAN_ON)
        self.assertEqual(gate_ok.verify(self.repo.root, self.head), (rec, None))

    def test_ran_on_with_input_keys_is_refused(self):
        with self.assertRaises(gate_ok.RecordError) as cm:
            self.passed(ran_on=RAN_ON, input_keys=KEYS_SET)
        self.assertIn('has no input_keys', str(cm.exception))

    def test_a_ran_on_that_is_not_the_three_strings_is_refused(self):
        for ran_on in ({}, dict(RAN_ON, node=''), dict(RAN_ON, job=7), dict(RAN_ON, extra='x'),
                       'box1'):
            with self.subTest(ran_on=ran_on):
                with self.assertRaises(gate_ok.RecordError) as cm:
                    gate_ok.build(self.repo.root, self.head, GATE, KIT, 'kit-test',
                                  '2026-10-06T10:00:00Z', STEPS, ran_on=ran_on)
                self.assertIn('ran_on is {node, host_key, job}', str(cm.exception))

    def test_no_ran_on_writes_no_key(self):
        _path, rec = self.passed()
        self.assertNotIn('ran_on', rec)


class VerifyUnchangedTest(GateOkCase):
    """verify() after the import_record refactor: a record this machine's gate wrote reads back
    exactly as written, and verify writes nothing."""

    def test_an_existing_record_verifies_unchanged(self):
        path, rec = self.passed(full=True, config_rev=self.head, input_keys=KEYS_SET)
        with open(path, 'rb') as f:
            before = f.read()
        for got in (gate_ok.verify(self.repo.root, self.head),
                    gate_ok.verify(self.repo.root, self.head, GATE),
                    gate_ok.verify(self.repo.root, self.head, config_at=lambda rev: GATE),
                    gate_ok.verify_full(self.repo.root, self.head)):
            self.assertEqual(got, (rec, None))
            self.assertEqual(got[0], json.loads(before.decode('utf-8')))
            self.assertNotIn('ran_on', got[0])
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)


if __name__ == '__main__':
    unittest.main()
