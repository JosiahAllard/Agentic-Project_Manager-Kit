"""kitlib.pluginsource default_kit: plugin_refresh.py's --kit default when the tool runs from an
export folder (no .git): the kit checkout the export's marker records, else a plain refusal."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import pluginsource as P


class DefaultKitTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='kit-defkit-')
        self.addCleanup(shutil.rmtree, self.d, True)

    def export(self, marker):
        folder = os.path.join(self.d, '0123456789ab')
        os.makedirs(folder)
        with open(os.path.join(folder, P.MARKER), 'w', encoding='utf-8') as fh:
            json.dump(marker, fh)
        return folder

    def test_a_checkout_is_its_own_kit(self):
        self.assertEqual(P.default_kit(self.d), (self.d, None))

    def test_an_export_defaults_to_the_checkout_its_marker_records(self):
        checkout = os.path.join(self.d, 'kit')
        os.makedirs(os.path.join(checkout, '.git'))
        folder = self.export({'sha': 'x', 'checkout': checkout})
        self.assertEqual(P.default_kit(folder), (checkout, None))

    def test_an_export_with_no_checkout_is_refused_plainly(self):
        folder = self.export({'sha': 'x'})
        kit, why = P.default_kit(folder)
        self.assertIsNone(kit)
        self.assertIn('this is an export; pass --kit <kit checkout>', why)
        self.assertIn(folder, why)

    def test_an_export_whose_checkout_has_gone_is_refused(self):
        folder = self.export({'sha': 'x', 'checkout': os.path.join(self.d, 'gone')})
        kit, why = P.default_kit(folder)
        self.assertIsNone(kit)
        self.assertIn('pass --kit', why)


if __name__ == '__main__':
    unittest.main()
