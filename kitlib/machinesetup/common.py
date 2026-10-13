"""The plan a machine-setup survey builds, and the path helpers its parts share."""
import os
import stat

# The Claude shell in a kit checkout (hosts.json "shell"), and the folder it was before the hosts/
# move (docs/multi-host-plan.md step 7). Settings and skill links written before the move name
# MOVED_FROM paths; the settings and links steps re-point them into SHELL (docs/machine-setup.md
# "Moved hook paths").
SHELL = 'hosts/claude/shell'
MOVED_FROM = 'claude'


def kit_file(kit, rel):
    """`rel` (posix, kit-relative) under the kit folder `kit`."""
    return os.path.join(kit, *rel.split('/'))


class Change(object):
    """One change: `text` is what a dry run prints, `do` makes it, `undo` (text) restores it by hand."""

    def __init__(self, section, text, do, undo=None, detail=None):
        self.section, self.text, self.do, self.undo = section, text, do, undo
        self.detail = detail or []


class Plan(object):
    """A survey's changes, notes and refusals. lines() prints them grouped by section, in SECTIONS
    order; a fresh run (--fresh) uses FRESH_SECTIONS, which adds a `plugin` section. The changes
    are made (run()) in the order the survey added them, not by section: survey() adds each
    folder's settings write, then the PM hook, then the plugin changes, then the extras, so claude's
    write to a settings.json lands after the settings write the survey diffed."""
    SECTIONS = ('links', 'settings', 'extras')
    FRESH_SECTIONS = ('links', 'settings', 'plugin', 'extras')

    def __init__(self, sections=None):
        self.SECTIONS = tuple(sections or self.SECTIONS)
        self.changes, self.refusals = [], []
        self.notes = {s: [] for s in self.SECTIONS}

    def add(self, section, text, do, undo=None, detail=None):
        self.changes.append(Change(section, text, do, undo, detail))

    def note(self, section, text):
        self.notes[section].append(text)

    def refuse(self, text):
        self.refusals.append(text)

    def lines(self):
        out = []
        for s in self.SECTIONS:
            mine = [c for c in self.changes if c.section == s]
            out.append('')
            out.append('%s: %s' % (s, '%d change(s)' % len(mine) if mine else 'nothing to do'))
            for c in mine:
                out.append('  ' + c.text)
                out.extend('    ' + d for d in c.detail)
            out.extend('  note: ' + n for n in self.notes[s])
        out.append('')
        return out


MOUNT_POINT = getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)


def is_link(path):
    """A symlink or a directory junction. os.path.isjunction is Python 3.12+; before it, the
    junction's reparse tag from os.lstat (Windows, 3.8+) says the same."""
    if os.path.islink(path):
        return True
    if hasattr(os.path, 'isjunction'):
        return os.path.isjunction(path)
    try:
        return getattr(os.lstat(path), 'st_reparse_tag', 0) == MOUNT_POINT
    except OSError:
        return False


def link_target(path):
    """Where a junction or symlink points, as stored (a dangling one too)."""
    try:
        t = os.readlink(path)
    except OSError:
        return os.path.realpath(path)
    for prefix in ('\\\\?\\', '\\??\\'):
        if t.startswith(prefix):
            return t[len(prefix):]
    return t


def norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def under(path, root):
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def free_name(path):
    """`path`, or `path-2`, `path-3`... : the first name nothing holds yet."""
    if not os.path.lexists(path):
        return path
    n = 2
    while os.path.lexists('%s-%d' % (path, n)):
        n += 1
    return '%s-%d' % (path, n)
