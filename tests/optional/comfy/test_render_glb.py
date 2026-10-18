"""optional/comfy/render_glb.py: argument parsing and sheet layout, without Blender.

The module imports bpy only when Blender runs it; here the pure parts are imported and tested.

usage: python -m unittest tests.optional.comfy.test_render_glb -v
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

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


def prints(path, code=0):
    """A stand-in for subprocess.run: the candidate python prints `path` as its sys.executable."""
    return mock.Mock(return_value=mock.Mock(returncode=code, stdout=(path or "") + "\n"))


class MachineLockRerun(unittest.TestCase):
    """A render re-runs Blender under the machine lock (kind render); only its own re-run skips."""

    ARGV = ['--grid', '2', 'out dir/sheet.png', 'a.glb']

    def fake_file(self, *parts):
        """A real empty file at tmp/parts..., so os.path.isfile is true for it."""
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return path

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.python = self.fake_file("bin", "python.exe")

    def rerun(self, env, python=None, binary="blender.exe"):
        opts = R.parse_args(self.ARGV)
        tool = self.fake_file("tool", "machine_lock.py")
        clean = {k: v for k, v in os.environ.items() if k not in ("MACHINE_LOCK_RUN", R.RERUN_ENV)}
        with mock.patch.dict(os.environ, dict(clean, **env), clear=True), \
                mock.patch.object(R, "MACHINE_LOCK_TOOL", tool), \
                mock.patch.object(R.shutil, "which", return_value=python or self.python), \
                mock.patch.object(R.subprocess, "run", prints(python or self.python)), \
                mock.patch.object(R.subprocess, "call", return_value=0) as call, \
                mock.patch.object(R.sys, "exit") as exit_:
            R.machine_lock_rerun(opts, self.ARGV, binary)
            left = os.environ.get(R.RERUN_ENV)
        return call, exit_, left

    def refused(self, python, binary="blender.exe"):
        opts = R.parse_args(self.ARGV)
        tool = self.fake_file("tool", "machine_lock.py")
        with mock.patch.object(R, "MACHINE_LOCK_TOOL", tool), \
                mock.patch.object(R.shutil, "which", return_value=python), \
                mock.patch.object(R.subprocess, "run", prints(python)), \
                mock.patch.object(R.subprocess, "call") as call, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as e:
                R.machine_lock_rerun(opts, self.ARGV, binary)
        self.assertEqual(e.exception.code, 2)
        call.assert_not_called()
        self.assertIn("refused: no usable python", err.getvalue())

    def test_a_missing_python_refuses(self):
        self.refused(os.path.join(self.tmp, "nope", "python.exe"))

    def test_a_windowsapps_stub_refuses(self):
        self.refused(self.fake_file("Microsoft", "WindowsApps", "python.exe"))

    def test_blender_as_the_python_refuses(self):
        self.refused(self.python, binary=self.python)

    def test_an_alias_runs_the_python_it_resolves_to(self):
        alias = self.fake_file("Microsoft", "WindowsApps", "python.exe")
        with mock.patch.object(R.subprocess, "run", prints(self.python)) as run:
            self.assertEqual(R.usable_python(alias, "blender.exe"), self.python)
        self.assertEqual(run.call_args[0][0], [alias, "-c", "import sys;print(sys.executable)"])
        self.assertIn("timeout", run.call_args[1])

    def test_a_stub_that_fails_is_rejected(self):
        stub = self.fake_file("Microsoft", "WindowsApps", "python.exe")
        for run in (prints("", code=9009), prints(""), mock.Mock(side_effect=OSError("stub")),
                    mock.Mock(side_effect=R.subprocess.TimeoutExpired("python", 10))):
            with mock.patch.object(R.subprocess, "run", run):
                self.assertIsNone(R.usable_python(stub, "blender.exe"))

    def test_an_alias_that_resolves_to_blender_is_rejected(self):
        blender = self.fake_file("Blender", "blender.exe")
        with mock.patch.object(R.subprocess, "run", prints(blender)):
            self.assertIsNone(R.usable_python(self.python, blender))

    def test_a_real_python_is_used_to_run_the_tool(self):
        call, _, _ = self.rerun({})
        self.assertEqual(call.call_args[0][0][0], self.python)

    def test_a_script_error_in_blender_exits_nonzero(self):
        call, _, _ = self.rerun({})
        cmd = call.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--python-exit-code") + 1], "1")
        self.assertGreater(cmd.index("--python-exit-code"), cmd.index("--"))
        self.assertLess(cmd.index("--python-exit-code"), cmd.index("--python"))

    def test_no_enclosing_run_takes_a_render_slot(self):
        call, exit_, _ = self.rerun({})
        self.assertEqual(call.call_count, 1)
        cmd = call.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--kind") + 1], "render")
        self.assertEqual(cmd[cmd.index("--slug") + 1], "render-sheet")
        self.assertEqual(call.call_args[1]["env"][R.RERUN_ENV], "1")
        exit_.assert_called_once_with(0)

    def test_the_rerun_is_blender_on_this_script_with_the_same_arguments(self):
        call, _, _ = self.rerun({})
        cmd = call.call_args[0][0]
        run = cmd[cmd.index("--") + 1:]
        self.assertEqual(run[:6], ["blender.exe", "--background", "--python-exit-code", "1",
                                   "--python", os.path.abspath(R.__file__)])
        self.assertEqual(run[6:], ["--"] + self.ARGV)

    def test_an_enclosing_run_still_goes_through_the_tool(self):
        call, exit_, _ = self.rerun({"MACHINE_LOCK_RUN": "r-cpu-run"})
        self.assertEqual(call.call_count, 1)
        exit_.assert_called_once_with(0)

    def test_its_own_rerun_skips_and_consumes_the_marker(self):
        call, exit_, left = self.rerun({"MACHINE_LOCK_RUN": "r-render", R.RERUN_ENV: "1"})
        call.assert_not_called()
        exit_.assert_not_called()
        self.assertIsNone(left)

    def test_no_tool_refuses_rather_than_render_unlocked(self):
        opts = R.parse_args(self.ARGV)
        clean = {k: v for k, v in os.environ.items() if k not in ("MACHINE_LOCK_RUN", R.RERUN_ENV)}
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(R, "MACHINE_LOCK_TOOL", os.path.join(HERE, "no-such-tool.py")), \
                mock.patch.object(R.subprocess, "call") as call, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as e:
                R.machine_lock_rerun(opts, self.ARGV, "blender.exe")
        self.assertEqual(e.exception.code, 2)
        call.assert_not_called()
        self.assertIn("refused", err.getvalue())

    def test_main_asks_for_the_slot_before_rendering(self):
        fake_bpy = mock.MagicMock()
        fake_bpy.app.binary_path = "blender.exe"
        with mock.patch.object(R, "bpy", fake_bpy), \
                mock.patch.object(R, "machine_lock_rerun", side_effect=SystemExit(0)) as rerun:
            with self.assertRaises(SystemExit):
                R.main(self.ARGV)
        rerun.assert_called_once()
        self.assertEqual(rerun.call_args[0][1:], (self.ARGV, "blender.exe"))
        fake_bpy.ops.wm.read_factory_settings.assert_not_called()


if __name__ == '__main__':
    unittest.main()
