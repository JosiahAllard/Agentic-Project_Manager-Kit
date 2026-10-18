"""The agent memory folder a repository names in its .claude/settings.local.json (or settings.json):
`autoMemoryDirectory` = <memory root>/<repo>. The value is a machine path, so it is written to the
local file, which git ignores, and never to the tracked settings.json (the scrub tests refuse a
machine path in a tracked file). tools/kit_stubs.py `memory` is the entry point; the repo setup
(kitlib.machinesetup.repo) and the PM session-start check (kitlib.pmstart memory) use it too.

The memory root is the root of the drive (or UNC share) the main checkout is on, plus FOLDER: a repo on
another drive keeps its memory on that drive. `override` replaces the whole root; config_override()
is where the machine config's key will be read, and gives None until it exists. <repo> is the
folder name of the repository's main checkout, so every linked worktree names the same folder.
No path or drive letter is written in this module: each is computed from the checkout.

survey() reads the key from either file (the local one first) and gives one of four states:
  OK       the key names the wanted folder (compared case- and separator-blind)
  OTHER    the key names another absolute folder (a drive path, a UNC path or ~/...): kept, never
           replaced; the owner chose it
  MISSING  neither file has the key: apply() adds it to the local file
  BAD      what this cannot edit, in either file: not JSON, not an object, the key not an absolute path
           string, a symbolic or hard link, or a checkout git cannot place: apply() refuses it
apply() writes only in the MISSING state, and only the local file. A missing file is created holding the key alone (a .tmp
renamed onto the path, which refuses a file made since the survey); an existing file is re-read and
refused when it changed since the survey, then written with the key added and nothing else changed:
a file in a json.dumps layout is rewritten in that layout (kitlib.machinesetup.settings), any other
gets one line inserted after its opening brace. It never makes the memory folder (Claude Code makes
it when it first saves) and never commits; there is nothing to commit.
"""
import collections
import json
import ntpath
import os
import re

from kitlib.host import registry
from kitlib.machinesetup import settings as jsonfile

KEY = 'autoMemoryDirectory'
FOLDER = 'Agent-memory'
OK, OTHER, MISSING, BAD = 'ok', 'other', 'missing', 'bad'
ABSOLUTE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\|//|~[\\/])')
TMP = '.kit-memory.tmp'

Memory = collections.namedtuple('Memory', 'state path value want raw layout data why')


class Refused(Exception):
    """apply() wrote nothing: the message says why."""


def config_override():
    """The machine config's memory root, or None. The key is not in the machine config's schema
    yet; until it is, every repository uses its drive's root."""
    return None


def memory_root(checkout, override=None):
    """<the checkout's drive or UNC share>/FOLDER with forward slashes, or `override` written with
    forward slashes and no trailing one."""
    if override:
        return override.replace('\\', '/').rstrip('/')
    drive = ntpath.splitdrive(os.path.abspath(checkout))[0]
    return drive.replace('\\', '/') + '/' + FOLDER


def _main_top(checkout, main_top=None):
    """The main checkout of the repository `checkout` is in, or None when git cannot place it.
    `main_top` is kitlib.git.repo.main_checkout_toplevel unless given."""
    if main_top is None:
        from kitlib.git.repo import main_checkout_toplevel as main_top
    top = main_top(checkout)
    return top.rstrip('\\/') if top else None


def repo_name(checkout, main_top=None):
    """The folder name of the main checkout of the repository `checkout` is in, or None."""
    top = _main_top(checkout, main_top)
    return (ntpath.basename(top) or None) if top else None


def wanted(checkout, override=None, main_top=None):
    """The autoMemoryDirectory value for `checkout`: <the memory root of its main checkout>/<repo>,
    or None. The main checkout names the drive too, so every linked worktree names one folder."""
    top = _main_top(checkout, main_top)
    name = ntpath.basename(top) if top else None
    if not name:
        return None
    return memory_root(top, override) + '/' + name


LOCAL = 'settings.local.json'


def _settings():
    """(the host's settings folder, 'settings.json'), read from hosts.json at call time, so a broken
    hosts.json fails the call that needs it (registry.RegistryError), not the import."""
    return (registry.default_profile().state_directory, 'settings.json')


def settings_rel():
    """The shared settings file (read for the key, never written), forward slashes, relative."""
    return '/'.join(_settings())


def settings_path(checkout):
    return os.path.join(checkout, *_settings())


def local_rel():
    """The file the key is written to: the shared one's local twin, which git does not track."""
    return '/'.join(_settings()[:-1] + (LOCAL,))


def local_path(checkout):
    return os.path.join(checkout, *_settings()[:-1], LOCAL)


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _load(path):
    """(None, None, None, None) for no file; (raw, layout, data, None) for a JSON object; (raw, None,
    None, why) for what this cannot edit."""
    if not os.path.lexists(path):
        return None, None, None, None
    shared = jsonfile.shared_link(path)
    if shared:
        return None, None, None, ('%s is %s; replacing it would split it from the file it shares '
                                  '(add the key by hand)' % (path, shared))
    if not os.path.isfile(path):
        return None, None, None, '%s is not a file' % path
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
    except OSError as e:
        return None, None, None, 'cannot read %s: %s' % (path, getattr(e, 'strerror', None) or e)
    try:
        layout, data = jsonfile.read(raw)
    except ValueError as e:
        return raw, None, None, '%s is not JSON this can edit: %s' % (path, e)
    if not isinstance(data, dict):
        return raw, None, None, '%s does not hold a JSON object' % path
    return raw, layout, data, None


def survey(checkout, override=None, main_top=None):
    """The Memory of `checkout` (states in the module docstring). The key is looked for in the local
    file, then the shared one; `path` is the file holding it, or the local file when MISSING."""
    local = local_path(checkout)
    if override is None:
        override = config_override()
    want = wanted(checkout, override, main_top)

    def bad(why, path=local, raw=None):
        return Memory(BAD, path, None, want, raw, None, None, why)
    if want is None:
        return bad('git cannot place the main checkout of %s, so the memory folder is not known' % checkout)
    raw, layout, data, why = _load(local)
    if why:
        return bad(why, raw=raw)
    shared = settings_path(checkout)
    s_raw, s_layout, s_data, s_why = _load(shared)
    if s_why:
        return bad(s_why, shared, s_raw)
    for path, (r, lay, dat) in ((local, (raw, layout, data)), (shared, (s_raw, s_layout, s_data))):
        if not isinstance(dat, dict) or KEY not in dat:
            continue
        value = dat[KEY]
        if not isinstance(value, str) or not ABSOLUTE.match(value):
            return bad('%s: %s is %s, not an absolute folder (fix it by hand)' % (path, KEY, json.dumps(value)),
                       path, r)
        return Memory(OK if _same(value, want) else OTHER, path, value, want, r, lay, dat, None)
    return Memory(MISSING, local, None, want, raw, layout, data, None)


def _inserted(m):
    """m.raw with the key inserted as one line after its opening brace; Refused when the result does
    not read back as the same object plus the key."""
    bom = m.raw.startswith(b'\xef\xbb\xbf')
    text = m.raw.decode('utf-8-sig')
    nl = '\r\n' if m.layout['crlf'] else '\n'
    i = text.index('{') + 1
    rest = text[i:]
    entry = '%s: %s' % (json.dumps(KEY), json.dumps(m.want, ensure_ascii=False))
    if not rest.strip().startswith('}'):
        nxt = re.search(r'\n([ \t]+)\S', rest)
        indent = nxt.group(1) if nxt else '  '
        new = text[:i] + nl + indent + entry + ',' + rest
    else:
        new = text[:i] + nl + '  ' + entry + nl + rest.lstrip()
    if json.loads(new) != dict(m.data, **{KEY: m.want}):
        raise Refused('%s: the key could not be added without changing the rest (add it by hand)' % m.path)
    raw = new.encode('utf-8')
    return (b'\xef\xbb\xbf' + raw) if bom else raw


def _drop(tmp):
    if os.path.lexists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def apply(m):
    """Write the key for a MISSING survey `m`: [what was written]. [] for OK and OTHER; Refused for
    BAD, for a file changed or made since the survey, or a write that fails."""
    if m.state in (OK, OTHER):
        return []
    if m.state != MISSING:
        raise Refused(m.why)
    tmp = m.path + TMP
    try:
        if m.raw is None:
            new = jsonfile.write(dict(jsonfile.NEW_LAYOUT), {KEY: m.want})
            os.makedirs(os.path.dirname(m.path), exist_ok=True)
            with open(tmp, 'wb') as fh:
                fh.write(new)
            try:
                os.rename(tmp, m.path)                          # Windows: refuses an existing file
            except FileExistsError:
                raise Refused('%s was made since the survey; not replaced (run again)' % m.path)
        else:
            shared = jsonfile.shared_link(m.path)
            if shared:
                raise Refused('%s is %s; not replaced' % (m.path, shared))
            with open(m.path, 'rb') as fh:
                if fh.read() != m.raw:
                    raise Refused('%s changed since the survey; not replaced (run again)' % m.path)
            if m.layout['exact']:
                new = jsonfile.write(m.layout, dict(m.data, **{KEY: m.want}))
            else:
                new = _inserted(m)
            with open(tmp, 'wb') as fh:
                fh.write(new)
            os.replace(tmp, m.path)
    except OSError as e:
        _drop(tmp)
        raise Refused('cannot write %s: %s' % (m.path, getattr(e, 'strerror', None) or e))
    except BaseException:
        _drop(tmp)
        raise
    return ['memory: added %s = %s to %s' % (KEY, m.want, m.path)]


_apply = apply       # run() takes an `apply` switch


def owner_command(checkout, kit=None):
    """The command that adds the key to `checkout`'s settings."""
    if kit is None:
        from kitlib.stubs import kit_root
        kit = kit_root()
    return 'python "%s" memory -C "%s" --apply' % (os.path.join(kit, 'tools', 'kit_stubs.py'), checkout)


def problem_line(checkout, label=None, override=None, main_top=None, kit=None):
    """One line when `checkout`'s settings lack the key or cannot be read; None when the key is there
    (the wanted folder or one the owner chose). The missing line is an owner step, worded as the
    checklist's other owner-only commands (kitlib.pmstart.fglock): the command writes another
    repository's main checkout, so an agent never runs it."""
    label = label or os.path.basename(os.path.normpath(checkout))
    m = survey(checkout, override, main_top)
    if m.state in (OK, OTHER):
        return None
    if m.state == BAD:
        return 'could not check memory (%s): %s' % (label, m.why)
    return ('memory (%s): %s has no %s; its memory folder is %s. Ask the owner (via the hub) to run, in '
            'their own PowerShell window: %s (it writes %s, which git ignores; nothing to commit)'
            % (label, m.path, KEY, m.want, owner_command(checkout, kit), local_rel()))


def _unignored(checkout):
    """A warning line when git says the local file is not ignored in `checkout`, else None (also when
    git cannot say)."""
    from kitlib.git import cmd
    r = cmd.call(['check-ignore', '-q', '--', local_rel()], checkout)
    if r.rc == 1:
        return ('memory: WARNING: git does not ignore %s here; add it to .gitignore or .git/info/exclude '
                'before committing, or the machine path is committed' % local_rel())
    return None


def run(checkout, apply=False, kit=None, override=None, main_top=None, unignored=None):
    """kitlib.stubs.Outcome(code, lines) of `kit_stubs.py memory`: 0 when the key is there, added,
    or (dry run) would be added; 1 when the file cannot be edited or the write failed. After a write,
    `unignored(checkout)` (default: git check-ignore) may give a warning line."""
    from kitlib.stubs import Outcome
    m = survey(checkout, override, main_top)
    if m.state == BAD:
        return Outcome(1, ['memory: refused: %s' % m.why])
    if m.state == OK:
        return Outcome(0, ['memory: %s names %s (current)' % (m.path, m.value)])
    if m.state == OTHER:
        return Outcome(0, ['memory: %s names %s; kept (an owner\'s choice is never replaced; the kit\'s '
                           'value would be %s)' % (m.path, m.value, m.want)])
    if not apply:
        return Outcome(0, ['memory: would add %s = %s to %s (dry run; nothing written)' % (KEY, m.want, m.path),
                           '  to write it: %s' % owner_command(checkout, kit)])
    try:
        lines = _apply(m)
    except Refused as e:
        return Outcome(1, ['memory: refused: %s' % e])
    lines.append('memory: written to %s, a machine-local file; nothing to commit' % local_rel())
    note = (unignored or _unignored)(checkout)
    if note:
        lines.append(note)
    return Outcome(0, lines)
