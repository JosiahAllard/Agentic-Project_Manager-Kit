"""optional/comfy/compare_sheet.py: the labelled N-up compare sheet.

A sheet holds images only: a missing file or a file that is not an image (a prompt written in
place of a generated image, say) refuses the whole sheet with exit 2.

usage: python -m unittest tests.optional.comfy.test_compare_sheet -v
"""
import base64
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'comfy')
sys.path.insert(0, CODE)
import compare_sheet as S  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 16
GIF = b'GIF89a' + b'\x00' * 16
WEBP = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'\x00' * 8


class Kinds(unittest.TestCase):
    def test_known_image_headers(self):
        self.assertEqual(S.image_type(PNG), 'image/png')
        self.assertEqual(S.image_type(JPEG), 'image/jpeg')
        self.assertEqual(S.image_type(GIF), 'image/gif')
        self.assertEqual(S.image_type(WEBP), 'image/webp')

    def test_text_is_not_an_image(self):
        self.assertIsNone(S.image_type(b'A red sword on a white background, studio light'))
        self.assertIsNone(S.image_type(b''))


class Items(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_label_defaults_to_the_file_stem(self):
        path = self.write('sword_a.png', PNG)
        self.assertEqual(S.parse_item(path), ('sword_a', path))

    def test_label_equals_path(self):
        path = self.write('b.png', PNG)
        self.assertEqual(S.parse_item('krea2 seed 7=' + path), ('krea2 seed 7', path))

    def test_an_existing_path_holding_equals_is_a_path(self):
        path = self.write('x=1.png', PNG)
        self.assertEqual(S.parse_item(path), ('x=1', path))

    def test_load_reads_type_and_bytes(self):
        path = self.write('a.png', PNG)
        tiles = S.load([('a', path)])
        self.assertEqual(tiles, [('a', path, 'image/png', PNG)])

    def test_load_refuses_a_missing_file(self):
        with self.assertRaises(S.SheetError) as e:
            S.load([('a', os.path.join(self.tmp, 'gone.png'))])
        self.assertIn('gone.png', str(e.exception))

    def test_load_refuses_a_prompt_in_place_of_an_image(self):
        good = self.write('a.png', PNG)
        prompt = self.write('b.png', b'a knight, painterly, 3/4 view')
        with self.assertRaises(S.SheetError) as e:
            S.load([('a', good), ('b', prompt)])
        self.assertIn('not an image', str(e.exception))
        self.assertIn('b.png', str(e.exception))

    def test_load_refuses_nothing(self):
        with self.assertRaises(S.SheetError):
            S.load([])


class Render(unittest.TestCase):
    def test_every_tile_is_labelled_and_embedded(self):
        tiles = [('one <a>', 'p1', 'image/png', PNG), ('two', 'p2', 'image/jpeg', JPEG)]
        page = S.render(tiles, cols=2, title='Swords & co')
        self.assertIn('one &lt;a&gt;', page)
        self.assertIn('two', page)
        self.assertIn('Swords &amp; co', page)
        self.assertIn('data:image/png;base64,' + base64.b64encode(PNG).decode('ascii'), page)
        self.assertIn('data:image/jpeg;base64,', page)
        self.assertIn('repeat(2,', page)
        self.assertNotIn('<script', page)

    def test_cols_defaults_to_all_tiles_up_to_four(self):
        self.assertEqual(S.default_cols(1), 1)
        self.assertEqual(S.default_cols(3), 3)
        self.assertEqual(S.default_cols(9), 4)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = S.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_writes_the_sheet(self):
        a = os.path.join(self.tmp, 'a.png')
        with open(a, 'wb') as f:
            f.write(PNG)
        out = os.path.join(self.tmp, 'sheet.html')
        code, text, _ = self.run_main(out, 'first=' + a, a, '--cols', '2')
        self.assertEqual(code, 0)
        self.assertIn('SHEET ' + out, text)
        with open(out, encoding='utf-8') as f:
            self.assertIn('first', f.read())

    def test_a_non_image_refuses_and_writes_nothing(self):
        a = os.path.join(self.tmp, 'a.txt')
        with open(a, 'w') as f:
            f.write('prompt text')
        out = os.path.join(self.tmp, 'sheet.html')
        code, _, err = self.run_main(out, a)
        self.assertEqual(code, 2)
        self.assertIn('not an image', err)
        self.assertFalse(os.path.exists(out))

    def png(self, name='a.png'):
        a = os.path.join(self.tmp, name)
        with open(a, 'wb') as f:
            f.write(PNG)
        return a

    def assert_refused_untouched(self, out, before, *images):
        code, _, err = self.run_main(out, *images)
        self.assertEqual(code, 2, err)
        self.assertIn('REFUSED: OUT', err)
        if before is None:
            self.assertFalse(os.path.exists(out))
        else:
            with open(out, 'rb') as f:
                self.assertEqual(f.read(), before)

    def test_out_must_be_an_html_name(self):
        a = self.png()
        self.assert_refused_untouched(os.path.join(self.tmp, 'sheet.txt'), None, a)

    def test_out_that_is_an_input_is_refused(self):
        a = self.png('a.html')                  # an image with an .html name, given as OUT too
        self.assert_refused_untouched(a, PNG, a)

    def test_an_existing_non_html_out_is_never_overwritten(self):
        a = self.png()
        out = os.path.join(self.tmp, 'notes.html')
        with open(out, 'wb') as f:
            f.write(b'my notes\n')
        self.assert_refused_untouched(out, b'my notes\n', a)

    def test_an_existing_sheet_is_rewritten(self):
        a = self.png()
        out = os.path.join(self.tmp, 'sheet.HTM')
        self.assertEqual(self.run_main(out, a)[0], 0)
        self.assertEqual(self.run_main(out, 'again=' + a)[0], 0)
        with open(out, encoding='utf-8') as f:
            self.assertIn('again', f.read())

    def test_cols_must_be_positive(self):
        a = os.path.join(self.tmp, 'a.png')
        with open(a, 'wb') as f:
            f.write(PNG)
        code, _, _ = self.run_main(os.path.join(self.tmp, 's.html'), a, '--cols', '0')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
