"""optional/comfy/render_glb.py: argument parsing and sheet layout, without Blender.

The module imports bpy only when Blender runs it; here the pure parts are imported and tested.

usage: python -m unittest tests.optional.comfy.test_render_glb -v
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'comfy')
sys.path.insert(0, CODE)
import render_glb as R  # noqa: E402


class Args(unittest.TestCase):
    def test_plain_form_is_unchanged(self):
        o = R.parse_args(['out.png', 'a.glb', 'b.glb'])
        self.assertEqual((o.out, o.files, o.grid, o.view, o.keep_tiles),
                         ('out.png', ['a.glb', 'b.glb'], None, '3/4', False))

    def test_grid_view_and_keep_tiles(self):
        o = R.parse_args(['--grid', '3', '--view', 'front', '--keep-tiles', 'out.png', 'a.glb'])
        self.assertEqual((o.grid, o.view, o.keep_tiles), (3, 'front', True))

    def test_unknown_view_refuses(self):
        with self.assertRaises(SystemExit):
            R.parse_args(['--grid', '2', '--view', 'top', 'out.png', 'a.glb'])

    def test_grid_must_be_positive(self):
        with self.assertRaises(SystemExit):
            R.parse_args(['--grid', '0', 'out.png', 'a.glb'])

    def test_needs_a_model(self):
        with self.assertRaises(SystemExit):
            R.parse_args(['out.png'])

    def test_argv_after_the_double_dash(self):
        self.assertEqual(R.script_args(['blender', '--python', 'x.py', '--', 'o.png', 'a.glb']),
                         ['o.png', 'a.glb'])
        self.assertEqual(R.script_args(['blender']), [])


class Plan(unittest.TestCase):
    def test_rows_of_views_by_default(self):
        tiles, cols, rows = R.plan(['a.glb', 'b.glb'], None, '3/4')
        self.assertEqual((cols, rows), (3, 2))
        self.assertEqual([(f, v) for f, v, _ in tiles],
                         [('a.glb', 'front'), ('a.glb', '3/4'), ('a.glb', 'side'),
                          ('b.glb', 'front'), ('b.glb', '3/4'), ('b.glb', 'side')])

    def test_grid_is_one_view_per_model_cols_wide(self):
        files = ['%d.glb' % i for i in range(5)]
        tiles, cols, rows = R.plan(files, 3, 'side')
        self.assertEqual((cols, rows), (3, 2))
        self.assertEqual([(f, v, d) for f, v, d in tiles], [(f, 'side', 90.0) for f in files])

    def test_grid_wider_than_the_models_shrinks(self):
        _, cols, rows = R.plan(['a.glb', 'b.glb'], 6, '3/4')
        self.assertEqual((cols, rows), (2, 1))

    def test_offsets_put_the_first_row_at_the_top(self):
        # Blender images are stored bottom row first
        self.assertEqual(R.dest_index(0, 0, cols=2, rows=2, tile=4), (1 * 4 + 0) * 8 + 0)
        self.assertEqual(R.dest_index(3, 0, cols=2, rows=2, tile=4), (0 * 4 + 0) * 8 + 4)
        self.assertEqual(R.dest_index(3, 2, cols=2, rows=2, tile=4), (0 * 4 + 2) * 8 + 4)


class Names(unittest.TestCase):
    def test_kept_tiles_are_named_for_the_compare_sheet(self):
        p = R.tile_path(os.path.join('d', 'sheet.png'), 0, os.path.join('m', 'sword.glb'), '3/4', True)
        self.assertEqual(p, os.path.join('d', 'sheet_sword_3q.png'))

    def test_a_model_named_twice_is_rendered_once(self):
        files = ['a.glb', os.path.join('.', 'a.glb'), 'b.glb', 'a.glb']
        self.assertEqual(R.unique_models(files), ['a.glb', 'b.glb'])

    def test_kept_tiles_of_two_models_with_one_name_differ(self):
        one, two = os.path.join('x', 'sword.glb'), os.path.join('y', 'sword.glb')
        stems = R.model_stems([one, two, 'shield.glb'])
        self.assertEqual(stems, {one: 'sword', two: 'sword_2', 'shield.glb': 'shield'})
        out = os.path.join('d', 'sheet.png')
        paths = {R.tile_path(out, i, m, '3/4', True, stems[m]) for i, m in enumerate([one, two])}
        self.assertEqual(paths, {os.path.join('d', 'sheet_sword_3q.png'),
                                 os.path.join('d', 'sheet_sword_2_3q.png')})

    def test_scratch_tiles_are_numbered(self):
        self.assertEqual(R.tile_path(os.path.join('d', 'sheet.png'), 4, 'a.glb', 'front', False),
                         os.path.join('d', '_tile_4.png'))

    def test_label(self):
        self.assertEqual(R.label('m/sword.glb', '3/4', 12345), 'sword.glb 3/4 12k tris')


if __name__ == '__main__':
    unittest.main()
