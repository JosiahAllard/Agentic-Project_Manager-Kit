"""kitlib.lock.kinds reads the table through kitlib.config.policy.kinds_table, its only reader."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.config import ConfigError, policy
from kitlib.lock import kinds


class KindsTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-kinds-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, data, bom=False):
        path = os.path.join(self.dir, 'kinds.json')
        with open(path, 'w', encoding='utf-8-sig' if bom else 'utf-8') as f:
            json.dump(data, f)
        return path

    def test_one_file_one_reader(self):
        self.assertEqual(kinds.KINDS_FILE, policy.KINDS_FILE)
        self.assertIn('test', kinds.load())
        self.assertFalse(hasattr(kinds, 'json'), 'kinds.py still carries its own JSON reader')

    def test_load_reads_through_the_config_reader(self):
        seen = []

        def spy(path):
            seen.append(path)
            return {'x': {'cpu': 'heavy'}}
        with mock.patch.object(policy, 'kinds_table', spy):
            table = kinds.load('some/path.json')
        self.assertEqual(seen, ['some/path.json'])
        self.assertEqual(table, {'x': {'cpu': 'heavy', 'gpu': 0, 'share': False, 'warn_min': 60}})

    def test_a_config_error_is_a_kinds_error(self):
        with mock.patch.object(policy, 'kinds_table', side_effect=ConfigError('broken table')):
            with self.assertRaisesRegex(kinds.KindsError, 'broken table'):
                kinds.load('p.json')
        with self.assertRaisesRegex(kinds.KindsError, 'no kinds table'):
            kinds.load(os.path.join(self.dir, 'missing.json'))

    def test_notes_drop_and_a_bom_reads(self):
        path = self.write({'_note': 'n', 'kinds': {'comment': 'c', 'build': {'cpu': 'medium'}}},
                          bom=True)
        self.assertEqual(sorted(kinds.load(path)), ['build'])

    def test_an_entry_the_lock_cannot_size_fails_closed(self):
        with self.assertRaisesRegex(kinds.KindsError, 'cpu must be medium or heavy'):
            kinds.load(self.write({'kinds': {'b': {'cpu': 'huge'}}}))
        with self.assertRaisesRegex(kinds.KindsError, 'must be objects'):
            kinds.load(self.write({'kinds': {'b': 3}}))


class Aliases(KindsTest):
    def test_load_rejects_a_bad_alias_at_load_time(self):
        kinds_ = {'a': {'cpu': 1}, 'b': {'cpu': 1}}
        for aliases in ({'a': {'to': 'b', 'why': 'w'}}, {'x': {'to': 'nope', 'why': 'w'}}):
            with self.assertRaises(kinds.KindsError):
                kinds.load(self.write({'kinds': kinds_, 'aliases': aliases}))
        path = self.write({'kinds': kinds_, 'aliases': {'x': {'to': 'a', 'why': 'w'}}})
        self.assertEqual(sorted(kinds.load(path)), ['a', 'b'])
        self.assertEqual(kinds.resolve('x', kinds.load(path), path), 'a')

    def test_a_kind_that_is_not_text_comes_back_unchanged(self):
        for odd in (['x'], {'x': 1}, None, 3):
            self.assertEqual(kinds.resolve(odd, {'a': {}}), odd)

    def test_canonical_reads_a_retired_name_as_the_kind_it_names(self):
        path = self.write({'kinds': {'a': {'cpu': 1}}, 'aliases': {'x': {'to': 'a', 'why': 'w'}}})
        self.assertEqual(kinds.aliases(path), {'x': 'a'})
        self.assertEqual([kinds.canonical(k, path) for k in ('x', 'a', 'other', None)],
                         ['a', 'a', 'other', None])
        self.assertEqual(kinds.canonical('x', table_aliases={}), 'x')

    def test_an_aliases_object_that_does_not_read_compares_names_as_written(self):
        path = self.write({'kinds': {'a': {'cpu': 1}}, 'aliases': {'x': {'to': 'nope', 'why': 'w'}}})
        self.assertEqual(kinds.aliases(path), {})
        self.assertEqual(kinds.canonical('x', path), 'x')
        with self.assertRaises(kinds.KindsError):
            kinds.resolve('x', {'a': {}}, path)          # booking still fails closed


class LiveTable(unittest.TestCase):
    """The kit's own kinds.json: generic names, the retired ones as aliases."""

    def test_the_generic_names_are_kinds_and_the_retired_ones_aliases(self):
        table = kinds.load()
        for k in ('test', 'app-run'):
            self.assertIn(k, table)
        for old in ('engine-test', 'engine-run'):
            self.assertNotIn(old, table)
        self.assertEqual(kinds.aliases(), {'engine-test': 'test', 'engine-run': 'app-run'})
        self.assertEqual(kinds.resolve('engine-test', table), 'test')
        self.assertEqual(kinds.resolve('engine-run', table), 'app-run')

    def test_a_renamed_kind_keeps_its_size(self):
        table = kinds.load()
        for k in ('test', 'app-run'):
            self.assertEqual(table[k], {'cpu': 'medium', 'gpu': 0, 'share': False, 'warn_min': 30})


class GpuValues(KindsTest):
    def test_a_bool_gpu_fails_closed(self):
        # False == 0 and True == 1 in Python: only the int 0 and "pool" are gpu values.
        for bad in (False, True, 1, "0", None):
            with self.assertRaisesRegex(kinds.KindsError, 'kind b: gpu', msg=repr(bad)):
                kinds.load(self.write({'kinds': {'b': {'cpu': 1, 'gpu': bad}}}))
        for ok in (0, "pool"):
            kinds.load(self.write({'kinds': {'b': {'cpu': 1, 'gpu': ok}}}))


class IntegerSizes(KindsTest):
    """A kind's cpu is a whole number of cores 1..agent_cap; medium and heavy stay as
    aliases (the live tables and the lock tests' table use them)."""

    def one(self, cpu, agent_cap=None):
        return kinds.load(self.write({'kinds': {'b': {'cpu': cpu}}}), agent_cap=agent_cap)['b']

    def test_a_whole_number_loads(self):
        self.assertEqual(self.one(3)['cpu'], 3)
        self.assertEqual(self.one(1, agent_cap=6)['cpu'], 1)
        self.assertEqual(self.one(6, agent_cap=6)['cpu'], 6)

    def test_the_aliases_still_load(self):
        self.assertEqual(self.one('medium', agent_cap=6)['cpu'], 'medium')
        self.assertEqual(self.one('heavy', agent_cap=6)['cpu'], 'heavy')

    def test_zero_negatives_fractions_text_and_bools_fail_closed(self):
        for bad in (0, -1, 2.5, 3.0, '3', True, False, None, [3]):
            with self.assertRaisesRegex(kinds.KindsError, 'kind b: cpu', msg=repr(bad)):
                self.one(bad)

    def test_more_than_the_agent_cap_fails_closed(self):
        with self.assertRaisesRegex(kinds.KindsError, 'more than the agent cap 6'):
            self.one(7, agent_cap=6)
        self.assertEqual(self.one(7)['cpu'], 7)     # no layout given: the cap is checked later

    def test_one_bad_entry_fails_the_whole_table(self):
        path = self.write({'kinds': {'a': {'cpu': 2}, 'b': {'cpu': 9}}})
        with self.assertRaises(kinds.KindsError):
            kinds.load(path, agent_cap=6)

    def test_check_cap_reads_a_loaded_or_given_table(self):
        kinds.check_cap({'a': {'cpu': 'heavy'}, 'b': {'cpu': 6}}, 6)
        with self.assertRaisesRegex(kinds.KindsError, 'kind b: cpu 7 is more than the agent cap 6'):
            kinds.check_cap({'a': {'cpu': 2}, 'b': {'cpu': 7}}, 6)

    def test_the_repo_table_fits_an_agent_cap_of_six(self):
        self.assertTrue(kinds.load(agent_cap=6))

    def test_the_maint_lint_kind_is_one_cpu_slot_and_no_gpu(self):
        table = kinds.load()
        self.assertEqual((table['maint-lint']['cpu'], table['maint-lint']['gpu']), (1, 0))
        self.assertEqual(table['maint-lint']['warn_min'], 15)
        kinds.check_cap(table, 6)
        kinds.check_cap({'maint-lint': table['maint-lint']}, 1)

    def test_describe_names_the_cores(self):
        lines = kinds.describe({'a': {'cpu': 3, 'gpu': 0, 'share': False},
                                'b': {'cpu': 1, 'gpu': 0, 'share': False},
                                'c': {'cpu': 'medium', 'gpu': 0, 'share': False}})
        self.assertEqual(lines, ['a: 3 cores', 'b: 1 core', 'c: medium cpu'])


if __name__ == '__main__':
    unittest.main()
