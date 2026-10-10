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
        self.assertIn('engine-test', kinds.load())
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

    def test_describe_names_the_cores(self):
        lines = kinds.describe({'a': {'cpu': 3, 'gpu': 0, 'share': False},
                                'b': {'cpu': 1, 'gpu': 0, 'share': False},
                                'c': {'cpu': 'medium', 'gpu': 0, 'share': False}})
        self.assertEqual(lines, ['a: 3 cores', 'b: 1 core', 'c: medium cpu'])


if __name__ == '__main__':
    unittest.main()
