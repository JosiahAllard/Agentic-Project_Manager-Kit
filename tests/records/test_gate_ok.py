"""kitlib.records.gate_ok: the gate-OK record written and verified against real throwaway repos."""
import hashlib
import json
import os
import unittest
from unittest import mock

from kitlib.records import gate_ok
from tests import gitfixture

GATE = {'steps': [{'name': 'unit', 'run': ['python', '-m', 'unittest']}], 'fail': ['FAILED']}
KIT = {'version': '0.1', 'commit': 'a' * 40}
STEPS = [{'name': 'unit', 'result': 'pass', 'seconds': 1.5}, {'name': 'docs'}]
AUTHOR = '%s <%s>' % (gitfixture.NAME, gitfixture.EMAIL)


class GateOkCase(unittest.TestCase):

    def setUp(self):
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

    def test_the_record_is_in_the_common_dir_whichever_checkout_writes_it(self):
        wt = self.repo.worktree('a')
        path, _ = self.passed(root=wt)
        self.assertTrue(os.path.samefile(os.path.dirname(path),
                                         os.path.join(self.repo.common_dir(), 'kit-gate-ok')))
        self.assertEqual(os.path.basename(path), self.head + '.json')
        self.assertIsNotNone(gate_ok.verify(self.repo.root, self.head, GATE)[0])

    def test_a_write_leaves_no_temp_file_and_a_second_pass_replaces_the_first(self):
        path, first = self.passed()
        path2, second = self.passed(gate={'other': True})
        self.assertEqual(path, path2)
        self.assertEqual(os.listdir(os.path.dirname(path)), [self.head + '.json'])
        self.assertEqual(gate_ok.read(self.repo.root, self.head)[0]['gate_config'], second['gate_config'])
        self.assertNotEqual(first['gate_config'], second['gate_config'])

    def test_a_replace_held_open_is_retried_and_the_temp_file_is_removed_on_failure(self):
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(a)
            if len(calls) < 2:
                raise PermissionError('held open')
            real(a, b)

        with mock.patch.object(gate_ok.time, 'sleep'), mock.patch.object(gate_ok.os, 'replace', flaky):
            path, _ = self.passed()
        self.assertEqual(len(calls), 2)
        with mock.patch.object(gate_ok.time, 'sleep'), \
                mock.patch.object(gate_ok.os, 'replace', side_effect=PermissionError('held open')):
            with self.assertRaises(gate_ok.RecordError):
                self.passed()
        self.assertEqual(os.listdir(os.path.dirname(path)), [self.head + '.json'])

    def test_a_write_that_fails_part_way_leaves_no_temp_file(self):
        rec = gate_ok.build(self.repo.root, self.head, GATE, KIT, 'kit-test', 'x', STEPS)
        with mock.patch.object(gate_ok.json, 'dumps', side_effect=OSError('disk full')):
            with self.assertRaises(gate_ok.RecordError) as cm:
                gate_ok.write(self.repo.root, rec)
        self.assertIn('disk full', str(cm.exception))
        self.assertEqual(os.listdir(os.path.join(self.repo.common_dir(), 'kit-gate-ok')), [])

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


class HashTest(unittest.TestCase):

    def test_the_config_hash_is_sha256_of_sorted_compact_utf8_json(self):
        self.assertEqual(gate_ok.config_hash({'b': [1, 2], 'a': 'é'}),
                         hashlib.sha256('{"a":"é","b":[1,2]}'.encode('utf-8')).hexdigest())
        self.assertEqual(gate_ok.config_hash({'a': 1, 'b': 2}), gate_ok.config_hash({'b': 2, 'a': 1}))
        self.assertNotEqual(gate_ok.config_hash({'a': 1}), gate_ok.config_hash({'a': 2}))
        self.assertNotEqual(gate_ok.config_hash([1, 2]), gate_ok.config_hash([2, 1]))


if __name__ == '__main__':
    unittest.main()
