"""pmstart.machine_config: the whole file as a dict, {} for a missing, non-object or unparsable file."""
import json
import os
import tempfile
import unittest

from kitlib import pmstart


class MachineConfigTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.json")

    def write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as f:
            f.write(text)

    def test_missing_file_gives_empty_dict(self):
        self.assertEqual(pmstart.machine_config(self.path), {})

    def test_file_that_is_not_a_json_object_gives_empty_dict(self):
        self.write("[1]")
        self.assertEqual(pmstart.machine_config(self.path), {})

    def test_bad_json_gives_empty_dict(self):
        self.write("{not json")
        self.assertEqual(pmstart.machine_config(self.path), {})

    def test_reads_the_whole_file_with_a_bom(self):
        data = {"launcher": {"accounts": {"2": ["~/.claude"]}}, "pm_start": {"hub": {}}}
        self.write(json.dumps(data), encoding="utf-8-sig")
        self.assertEqual(pmstart.machine_config(self.path), data)


if __name__ == "__main__":
    unittest.main()
