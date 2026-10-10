"""Render GLB files to a contact sheet with Blender (headless, Workbench, no GPU raytracing).

  blender --background --python render_glb.py -- OUT.png A.glb [B.glb ...]
  blender --background --python render_glb.py -- --grid COLS [--view 3/4] OUT.png A.glb ...

Each model is normalised to the same height, with its triangle count in the label. Used to
eyeball generated 3D before anyone spends time cleaning it up.

Default: one row per model, one column per view (front, 3/4, side).
--grid COLS: one view per model (--view, default 3/4), COLS models per row, for comparing many
  candidates at once.
--keep-tiles: also keep each tile as OUT_<model>_<view>.png (3/4 is written 3q) beside OUT, so
  compare_sheet.py can put them on a labelled page. Two models with the same file name get
  _2, _3 ... after the later ones' names so no tile overwrites another; a model named twice is
  rendered once.

The sheet itself has no text on it; the TILE lines this prints give the labels in sheet order
(left to right, top to bottom).
"""
import argparse
import math
import os
import sys

try:
    import bpy
    from mathutils import Vector
except ImportError:     # imported outside Blender (the tests): the pure parts below still work
    bpy = None

VIEWS = [("front", 0.0), ("3/4", 35.0), ("side", 90.0)]
TILE = 360


def script_args(argv):
    """The arguments after Blender's `--`."""
    return argv[argv.index("--") + 1:] if "--" in argv else []


def _positive(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="render_glb.py")
    ap.add_argument("--grid", type=_positive, metavar="COLS",
                    help="one view per model, COLS models per row")
    ap.add_argument("--view", default="3/4", choices=[v for v, _ in VIEWS],
                    help="the view --grid renders (default 3/4)")
    ap.add_argument("--keep-tiles", action="store_true",
                    help="keep each tile as OUT_<model>_<view>.png")
    ap.add_argument("out")
    ap.add_argument("files", nargs="+")
    return ap.parse_args(argv)


def unique_models(files):
    """`files` in order with repeats of the same path left out."""
    seen, out = set(), []
    for f in files:
        key = os.path.normcase(os.path.abspath(f))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def model_stems(files):
    """{file: the name its kept tiles use}: the file's stem, with _2, _3 ... added to a later file
    whose stem an earlier one already has."""
    stems, taken = {}, set()
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        stem, n = base, 1
        while stem.lower() in taken:
            n += 1
            stem = f"{base}_{n}"
        taken.add(stem.lower())
        stems[f] = stem
    return stems


def plan(files, grid, view):
    """`([(file, view label, degrees)] in sheet order, cols, rows)`."""
    if grid:
        deg = dict(VIEWS)[view]
        tiles = [(f, view, deg) for f in files]
        cols = min(grid, len(files))
        return tiles, cols, -(-len(files) // cols)
    tiles = [(f, label, deg) for f in files for label, deg in VIEWS]
    return tiles, len(VIEWS), len(files)


def dest_index(i, y, cols, rows, tile):
    """The pixel index in the sheet where row `y` of tile `i` starts (Blender stores images
    bottom row first, so the first sheet row is the top one)."""
    r, c = divmod(i, cols)
    return ((rows - 1 - r) * tile + y) * cols * tile + c * tile


def tile_path(out_path, i, model, view, keep, model_stem=None):
    folder = os.path.dirname(out_path)
    if not keep:
        return os.path.join(folder, f"_tile_{i}.png")
    stem = os.path.splitext(os.path.basename(out_path))[0]
    if model_stem is None:
        model_stem = os.path.splitext(os.path.basename(model))[0]
    return os.path.join(folder, f"{stem}_{model_stem}_{view.replace('3/4', '3q')}.png")


def label(model, view, tris):
    return f"{os.path.basename(model)} {view} {tris // 1000}k tris"


def _setup_scene():
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "TEXTURE"
    scene.render.resolution_x = TILE
    scene.render.resolution_y = TILE
    scene.render.film_transparent = False
    return scene


def main(argv):
    opts = parse_args(argv)
    opts.files = unique_models(opts.files)
    stems = model_stems(opts.files)
    planned, cols, rows = plan(opts.files, opts.grid, opts.view)
    scene = _setup_scene()
    scene.world = bpy.data.worlds.new("w") if scene.world is None else scene.world

    tiles = []
    by_model = {}
    for model, view, deg in planned:
        by_model.setdefault(model, []).append((view, deg))
    for model in opts.files:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = _setup_scene()
        bpy.ops.import_scene.gltf(filepath=model)
        meshes = [o for o in scene.objects if o.type == "MESH"]
        tris = sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in meshes)
        pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        centre, size = (lo + hi) / 2, max(hi - lo)
        cam_data = bpy.data.cameras.new("cam")
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = size * 1.15
        cam = bpy.data.objects.new("cam", cam_data)
        scene.collection.objects.link(cam)
        scene.camera = cam
        for view, deg in by_model[model]:
            a = math.radians(deg)
            d = size * 3
            cam.location = centre + Vector((d * math.sin(a), -d * math.cos(a), size * 0.25))
            direction = centre - cam.location
            cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            tile = tile_path(opts.out, len(tiles), model, view, opts.keep_tiles, stems[model])
            scene.render.filepath = tile
            bpy.ops.render.render(write_still=True)
            tiles.append((tile, label(model, view, tris)))

    sheet = bpy.data.images.new("sheet", cols * TILE, rows * TILE)
    pixels = [0.15] * (cols * TILE * rows * TILE * 4)
    for i, (tile, _) in enumerate(tiles):
        img = bpy.data.images.load(tile)
        src = list(img.pixels)
        for y in range(TILE):
            at = dest_index(i, y, cols, rows, TILE)
            pixels[at * 4:(at + TILE) * 4] = src[y * TILE * 4:(y + 1) * TILE * 4]
        if not opts.keep_tiles:
            os.remove(tile)
    sheet.pixels = pixels
    sheet.filepath_raw = opts.out
    sheet.file_format = "PNG"
    sheet.save()
    for tile, text in tiles:
        print("TILE", text + (f" -> {tile}" if opts.keep_tiles else ""))
    print("SHEET", opts.out)


if __name__ == "__main__" and bpy is not None:
    main(script_args(sys.argv))
