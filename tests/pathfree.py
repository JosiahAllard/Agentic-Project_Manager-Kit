"""Word asserts on text that carries a path. A test that asserts a plain word is absent from output
(assertNotIn('private', out)) goes red when the checkout, a worktree or the temp folder it runs in
has that word in its path (a branch slug becomes a folder name). `without_paths` removes those
paths first; the word is still checked on everything else."""
import os
import re
import tempfile

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SEP = '\\/'


def _pattern(root):
    """A regex for `root` in either slash style and any case (Windows paths differ in case:
    z:\\ and Z:\\), with no trailing separator."""
    parts = [p for p in re.split('[' + re.escape(_SEP) + ']+', root) if p]
    lead = '[' + re.escape(_SEP) + ']' if root[:1] in _SEP else ''
    return lead + ('[' + re.escape(_SEP) + ']+').join(re.escape(p) for p in parts)


def without_paths(text, *roots):
    """`text` with every root removed: KIT_ROOT, the working directory, the temp folder and each
    of `roots` (strings or path-likes). Longest first, so a root inside another is not left
    half-removed. Both slash styles, any case."""
    found = [str(r) for r in (KIT_ROOT, os.getcwd(), tempfile.gettempdir()) + roots
             if str(r).strip(_SEP)]
    for root in sorted(set(found), key=len, reverse=True):
        text = re.sub(_pattern(root), '', text, flags=re.IGNORECASE)
    return text
