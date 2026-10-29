"""The plan a machine-setup survey builds, and the path helpers its parts share."""
import os
import stat

from kitlib import platform

# The Claude shell in a kit checkout (hosts.json "shell"), and the folder it was before the hosts/
# move (docs/multi-host-plan.md step 7). Settings and skill links written before the move name
# MOVED_FROM paths; the settings and links steps re-point them into SHELL (docs/machine-setup.md
# "Moved hook paths").
SHELL = 'hosts/claude/shell'
MOVED_FROM = 'claude'


def make_folder(folder, mode=0o755):
    """Make `folder` and any missing parents, then set each folder this call made to `mode`, so a
    root umask of 027 or 077 cannot leave a root-owned folder (/etc/pm-kit) unreadable to the kit
    user. A folder that was already there keeps its mode."""
    made, p = [], folder
    while p and not os.path.isdir(p):
        made.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(folder, exist_ok=True)
    for d in made:
        os.chmod(d, mode)


def python_name():
    """The interpreter a printed hint names: `python3` on Linux (stock Ubuntu has no `python`),
    `python` elsewhere. NAME is read at call time."""
    return 'python3' if platform.NAME == 'linux' else 'python'


def kit_file(kit, rel):
    """`rel` (posix, kit-relative) under the kit folder `kit`."""
    return os.path.join(kit, *rel.split('/'))


class Report(list):
    """The lines a change's `do` returns for the run to print before its `done:` line. Only a
    Report is printed: any other return value (a list of icacls outputs, say) is ignored."""


class Change(object):
    """One change: `text` is what a dry run prints, `do` makes it, `undo` (text) restores it by hand."""

    def __init__(self, section, text, do, undo=None, detail=None):
        self.section, self.text, self.do, self.undo = section, text, do, undo
        self.detail = detail or []


class Plan(object):
    """A survey's changes, notes and refusals. lines() prints them grouped by section, in SECTIONS
    order; the default mode (no older kit clone) uses FRESH_SECTIONS, which adds a `plugin`
    section. The changes are made (run()) in the order the survey added them, not by section:
    survey() adds each folder's settings write, then the PM hook, then the plugin changes, then
    the owner's email, then the extras, so claude's write to a settings.json lands after the
    settings write the survey diffed, and the owner's email before the sweep key (both write the
    machine config)."""
    SECTIONS = ('links', 'settings', 'owner', 'extras')
    FRESH_SECTIONS = ('links', 'settings', 'plugin', 'owner', 'extras')

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
    """Where a junction or symlink points, as stored (a dangling one too). A relative symlink target
    is relative to the link's own folder, not the current directory, so it is joined to that."""
    try:
        t = os.readlink(path)
    except OSError:
        return os.path.realpath(path)
    for prefix in ('\\\\?\\', '\\??\\'):
        if t.startswith(prefix):
            return t[len(prefix):]
    if not os.path.isabs(t):
        return os.path.normpath(os.path.join(os.path.dirname(path), t))
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
