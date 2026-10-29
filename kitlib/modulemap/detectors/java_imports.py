"""java-imports: the layering crossings a ratchet counts inside (or between) modules.

Driven by the map's ratchets with `detector: "java-imports"`, not by edges: for each such ratchet,
every `.java` file its `files` globs name (fnmatch, `*` crosses `/`, case does not matter) that
has an import line naming one of its `imports` package prefixes is one build crossing, counted by
that ratchet and never refused or allowed by an edge. A file counts once however many lines
match. An import line is `import [static] <prefix>` where the prefix ends a name (the next
character is not a letter, digit, `_` or `$`), so `org.x.api` does not match `org.x.apix` (the
rule Retribution's ApiLayeringTest applies).

Known misses: fully qualified names used without an import, and imports in Kotlin or Groovy.
"""
import fnmatch
import re

from . import Crossing, line_of
from kitlib.modulemap.load import JAVA_IMPORTS

NAME = JAVA_IMPORTS


def import_line(prefixes):
    alts = '|'.join(re.escape(p.strip().rstrip('.*').rstrip('.')) for p in prefixes)
    return re.compile(r'^[ \t]*import[ \t]+(?:static[ \t]+)?(?:%s)(?![\w$])[^\r\n]*' % alts, re.M)


def detect(files, mm, read):
    for r in mm.ratchets:
        if r.get('detector') != JAVA_IMPORTS:
            continue
        globs = [g.replace('\\', '/').lower() for g in r['files']]
        pat = import_line(r['imports'])
        for f in files:
            low = f.lower()
            if not low.endswith('.java') or not any(fnmatch.fnmatchcase(low, g) for g in globs):
                continue
            text = read(f)
            m = pat.search(text) if text is not None else None
            if m:
                yield Crossing(r['from'], r['to'], 'build', NAME, f, line_of(text, m.start()),
                               m.group(0).strip(), r['id'])
