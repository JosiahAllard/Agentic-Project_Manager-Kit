"""Paths as the tools print them: forward slashes (Git Bash eats a backslash typed in a copied
path, so a drive-letter `-C` with backslashes arrives with none), and the -C that shows that
damage."""
import os
import re

_DRIVE_PATH = re.compile(r'\b[A-Za-z]:[\\/][^\s"\'<>|*?]*')
_MANGLED = re.compile(r'^([A-Za-z]):([^\\/].*)$')


def forward(text, *known):
    """`text` with every Windows drive path (and each path in `known`, spaces included) in forward
    slashes. Any `X:` followed by a backslash token in `text` is rewritten, whatever it is."""
    for path in known:
        if path and '\\' in path:
            text = text.replace(path, path.replace('\\', '/'))
    return _DRIVE_PATH.sub(lambda m: m.group(0).replace('\\', '/'), text)


def mangled_dir(path):
    """The forward-slash form to try instead of `path` when it is a drive and colon with no
    separator after (`Z:GitHub...`) that names nothing on disk, else None. A drive-relative path
    that exists is real and is not flagged."""
    m = _MANGLED.match(path or '')
    if not m or os.path.exists(path):
        return None
    return '%s:/%s' % (m.group(1), m.group(2).replace('\\', '/'))


def mangled_message(path, fixed):
    return ('-C %s: a drive and colon with no separator, so the shell ate the backslashes of a '
            'Windows path. Instead: pass forward slashes, as -C %s' % (path, fixed))
