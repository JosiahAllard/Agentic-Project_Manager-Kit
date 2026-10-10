#!/usr/bin/env python3
"""A labelled N-up compare sheet: one self-contained HTML page of images side by side.

  python compare_sheet.py OUT.html [LABEL=]IMAGE [[LABEL=]IMAGE ...] [--cols N] [--title TEXT]

Each tile shows the image and its label (LABEL, or the file name without its extension). The
images are embedded, so the page can be published or opened anywhere as one file. Use it to put
generated candidates, or the tiles render_glb.py --keep-tiles writes, in front of the owner.

A sheet holds images only (PNG, JPEG, GIF, WebP, read by their first bytes). A missing file or
anything else - a prompt written where an image should be, say - refuses the whole sheet and
writes nothing (exit 2). So does an OUT that is not a .html or .htm name, that is one of the
images, or that is an existing file which is not an HTML page: the sheet never overwrites one. When the generator is down there is nothing to compare: stop and report
the outage rather than making a sheet.

Exit codes: 0 written, 2 refused.
"""
import argparse
import base64
import html
import os
import sys


class SheetError(Exception):
    """Why no sheet can be made from the inputs."""


SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def image_type(head):
    """The media type of an image whose file starts with `head`, or None."""
    for sig, mime in SIGNATURES:
        if head.startswith(sig):
            return mime
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return None


def parse_item(arg):
    """`(label, path)` from `LABEL=PATH` or `PATH`. An existing file is always a path, so a file
    name holding `=` needs no label."""
    if not os.path.exists(arg) and '=' in arg:
        label, path = arg.split('=', 1)
        if label:
            return label, path
    return os.path.splitext(os.path.basename(arg))[0], arg


def load(items):
    """[(label, path, media type, bytes)] for `items` [(label, path)]; SheetError for the first
    that is missing or not an image, or when there are none."""
    if not items:
        raise SheetError('no images given')
    tiles = []
    for label, path in items:
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            raise SheetError('cannot read %s: %s' % (path, e.strerror or e))
        mime = image_type(data[:16])
        if mime is None:
            raise SheetError('%s is not an image (PNG, JPEG, GIF or WebP). A compare sheet holds '
                             'generated images or renders only; if the generator is down, stop '
                             'and report it instead of making a sheet' % path)
        tiles.append((label, path, mime, data))
    return tiles


HTML_SUFFIXES = ('.html', '.htm')


def out_refusal(out, items):
    """Why the sheet may not be written to `out` given `items` [(label, path)], or None."""
    if os.path.splitext(out)[1].lower() not in HTML_SUFFIXES:
        return 'OUT %s is not a .html or .htm name' % out
    target = os.path.normcase(os.path.realpath(out))
    for _, path in items:
        if os.path.normcase(os.path.realpath(path)) == target:
            return 'OUT %s is one of the images' % out
    if os.path.lexists(out):
        if not os.path.isfile(out):
            return 'OUT %s exists and is not a file' % out
        try:
            with open(out, 'rb') as f:
                head = f.read(512).lstrip().lower()
        except OSError as e:
            return 'cannot read the existing OUT %s: %s' % (out, e.strerror or e)
        if not (head.startswith(b'<!doctype html') or head.startswith(b'<html')):
            return 'OUT %s exists and is not an HTML page; it is never overwritten' % out
    return None


def default_cols(n):
    return max(1, min(n, 4))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
:root { --bg: #f4f4f2; --fg: #1c1c1c; --tile: #ffffff; --line: #d6d6d2; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bg: #161616; --fg: #e8e8e6; --tile: #222222; --line: #3a3a3a; }
}
:root[data-theme="dark"] { --bg: #161616; --fg: #e8e8e6; --tile: #222222; --line: #3a3a3a; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
       font: 14px/1.4 system-ui, sans-serif; }
h1 { font-size: 18px; margin: 0 0 12px; }
.grid { display: grid; grid-template-columns: repeat(%(cols)d, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 640px) { .grid { grid-template-columns: repeat(%(narrow)d, minmax(0, 1fr)); } }
figure { margin: 0; background: var(--tile); border: 1px solid var(--line); border-radius: 6px;
         padding: 8px; }
img { display: block; width: 100%%; height: auto; image-rendering: auto; }
figcaption { margin-top: 6px; font-weight: 600; overflow-wrap: anywhere; }
figcaption small { display: block; font-weight: 400; opacity: .7; }
</style></head><body>
<h1>%(title)s</h1>
<div class="grid">
%(tiles)s
</div>
</body></html>
"""

TILE = ('<figure><img alt="%(label)s" src="data:%(mime)s;base64,%(data)s">'
        '<figcaption>%(n)d. %(label)s<small>%(name)s</small></figcaption></figure>')


def render(tiles, cols, title):
    """The sheet's HTML for `tiles` from load(), `cols` wide."""
    rows = []
    for n, (label, path, mime, data) in enumerate(tiles, 1):
        rows.append(TILE % {'label': html.escape(label), 'mime': mime, 'n': n,
                            'name': html.escape(os.path.basename(path)),
                            'data': base64.b64encode(data).decode('ascii')})
    return PAGE % {'title': html.escape(title), 'cols': cols, 'narrow': min(cols, 2),
                   'tiles': '\n'.join(rows)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog='compare_sheet.py', description=__doc__.split('\n\n')[0])
    ap.add_argument('out', help='the .html file to write')
    ap.add_argument('images', nargs='+', help='[LABEL=]IMAGE')
    ap.add_argument('--cols', type=int, help='tiles per row (default: all of them, up to 4)')
    ap.add_argument('--title', default='Compare')
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if args.cols is not None and args.cols < 1:
        print('--cols must be 1 or more', file=sys.stderr)
        return 2
    items = [parse_item(a) for a in args.images]
    why = out_refusal(args.out, items)
    if why:
        print('REFUSED: %s' % why, file=sys.stderr)
        return 2
    try:
        tiles = load(items)
    except SheetError as e:
        print('REFUSED: %s' % e, file=sys.stderr)
        return 2
    page = render(tiles, args.cols or default_cols(len(tiles)), args.title)
    parent = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(parent, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(page)
    for label, path, _, _ in tiles:
        print('TILE %s (%s)' % (label, path))
    print('SHEET %s' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
