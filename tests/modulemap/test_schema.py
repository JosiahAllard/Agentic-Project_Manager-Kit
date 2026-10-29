"""The .kit.json "modules" section (docs/kit-json.md, docs/module-map.md "Configuring")."""
import unittest

from kitlib.config import schema


class ModulesSectionTest(unittest.TestCase):
    def test_the_section_names_the_map_and_the_ceilings(self):
        self.assertEqual(sorted(schema.SECTIONS['modules']), ['ceilings', 'map', 'page'])

    def test_a_file_cannot_repoint_the_module_map(self):
        self.assertNotIn('modules', schema.FILE_OK)


if __name__ == '__main__':
    unittest.main()
