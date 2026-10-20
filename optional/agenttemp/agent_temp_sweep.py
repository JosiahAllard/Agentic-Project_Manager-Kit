"""agent_temp_sweep.py: clears old entries from the agents' shared temp folder while nothing uses them.

  agent_temp_sweep.py [--apply] [--home DIR]

Without --apply it only lists what it would remove (a dry run); with it, it removes. The folder and
the age come from the "agent_temp_sweep" key of the machine config, never from code or
the command line: {"dir": <absolute folder>, "max_age_days": <number, default 3>, "keep": [...]}.
"keep" (default empty) lists relative paths under dir that the sweep never removes: a top-level
name, or claude/<project>/<session>; * and ? match within one name; case-insensitive. A unit (below)
is kept when an item names it, a folder holding it, or anything inside it: the sweep never enters a
unit, so "kit-coordinator/r2" keeps all of kit-coordinator, and "*/x" keeps every top-level folder.
Built in, whatever the config says (BUILT_IN_KEEP): a top-level "<name>-coordinator" or
"<name>-consultant" folder is always kept, since the launcher keeps a consultant's notes file
there and a coordinator its state note (kitlib.launcher.start CONSULTANT_FOLDER, COORDINATOR_FOLDER).

The units are the top-level files and folders of dir, except a folder named "claude": Claude Code
keeps each session's scratch at claude/<project>/<session>, so each entry of claude/<project> is its
own unit (a file or link straight in claude/ is one too). claude/ is never removed; claude/<project>
only once it is empty and itself older than the age. A unit on the keep-list is reported "kept" and
never aged, claimed or entered. A unit is removed only when all of these hold:
- the newest time anywhere in it (written or created, links not followed) is older than the age;
- under claude/ only: no running process has the unit's name (a session id) in its command line.
  The process list is read once per run (LIST_TIMEOUT, 30 s); when it cannot be read, no unit
  under claude/ is removed that run ("live"), the run prints "kept every claude/ folder: process
  list unavailable: <why>" and exits 3. A Claude Code process normally carries no session id in its command line
  (only --resume <id> or --session-id <id> does), so this rarely holds a session back; the age and
  the claim below carry the safety, and a session left idle and with no open handle in its folder
  for longer than the age loses its scratch (the keep-list is the remedy);
- it can be claimed: renamed aside (<name>.sweeping-<pid>) in one step. On Windows that fails while
  a process has a file in it open (unless opened to allow deletion) or its working folder in it, so
  an entry in use is kept;
- it is still old once claimed (the age is read again under the new name, so a write between the
  listing and the claim keeps it, put back under its own name);
- every part of it can be removed. A removal that fails part-way puts what is left back under its
  own name.
A claude/ or claude/<project> folder that cannot be listed (gone since, or no permission) is
reported "skipped" and left alone; the rest of the run goes on.
Links are removed as links; their targets are never entered. dir may not be a link, and may not,
as written or by its real path (every junction or link above it resolved), be a filesystem root,
the home folder, a folder in it or a folder holding it, inside a git checkout or worktree (a .git
folder or file in it or any folder above it), or the machine config folder or the kit checkout, a
folder inside one or a folder holding one (kitlib.machinesetup.agenttemp.unsafe_dir, which machine
setup applies to the folder it sets up too). Standard library and the kit's own kitlib only.

The user temp's claude folder (a second root). Sessions not started by the kit's launcher (the
desktop app's Code tab) keep their scratch under the user's own TEMP, <user temp>/claude/<project>/
<session>, outside dir. With "user_temp": true in the config (a boolean, default false), each run
also sweeps that folder: the user temp is HKCU\\Environment TEMP (else TMP), %VAR% expanded, never
the process environment (the launcher sets a session's TEMP to dir). Only its claude/ units are
swept, under every rule above (age, keep-list, live, claim); nothing else in the user temp is
listed or touched, and its claude/ folder itself is never removed. It is skipped, with one
"user temp: skipped: <why>" line and no change to the exit code, when the registry has no value,
when <user temp>/claude does not exist or may not be swept (second_root_unsafe), or when it is dir,
in dir or holds it (swept once, by the main run). second_root_unsafe: the folder may have no link
anywhere in its path, and must pass dir's refusals (unsafe_dir), with one exception: a folder in the
home folder is allowed when it is exactly %LOCALAPPDATA%\\Temp\\claude (as written and by real path),
Windows' default user temp; every other refusal still holds there.

The one-time drain (--drain-old-user-temp; a dry run unless --apply). It sweeps only the Windows
default user temp's claude folder, %LOCALAPPDATA%\\Temp\\claude (derived, never typed in), with the
rules of a second root, once the user's TEMP points elsewhere. The gate: while any claude.exe
(the CLI or the desktop app, name case-blind) started before HKCU\\Environment was last written, the
drain removes nothing and exits 4, naming each such process; it also exits 4 when the process list
or that time cannot be read (fail closed). The last write is never earlier than the TEMP change, so
the gate can only hold back more. An empty process list, or a TEMP that is missing, not absolute
or still holds an unexpanded %VAR%, shuts it too. Known limits: the gate counts claude.exe only, so
Claude Code run by node.exe (the npm install) is not counted; and a claude.exe started after the
change by a parent started before it inherits the parent's old TEMP. Only the age and claim rules
protect those sessions' folders. The dry run prints the gate and the list either way. It has
nothing to drain (exit 0) when that folder is missing, is the current user temp's, or lies in dir.
Owner steps, after a kit with this flag is the one the scheduled task runs:
  1. add "user_temp": true to agent_temp_sweep in the machine config (an older sweep refuses the key);
  2. restart the Claude desktop app and end every session started before the TEMP change;
  3. python <kit>\\optional\\agenttemp\\agent_temp_sweep.py --drain-old-user-temp  (read the list)
  4. python <kit>\\optional\\agenttemp\\agent_temp_sweep.py --drain-old-user-temp --apply
     (re-run later for what was too young; exit 4 means a session still predates the change).

Exit codes: 0 done (or nothing to do); 1 some old entry could not be removed (in use or failed);
2 no config, a bad config, an unsafe dir or a dir that cannot be listed; 3 the process list could
not be read, so every old unit under claude/ was kept (3 wins over 1); 4 (--drain-old-user-temp
only) the drain's gate is shut: a claude.exe predates the user environment's last change, or that
cannot be told.
"""
import argparse
import collections
import datetime
import fnmatch
import json
import os
import re
import stat
import sys
import time

# The machine config's place comes from the kit library (kitlib.config.user), found at the kit's
# root through the real path: a link to this folder resolves into the kit.
# Without it nothing is swept: load() refuses (exit 2), never a traceback (exit 1 means a partial sweep).
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, KIT_ROOT)
try:
    from kitlib.config import user as kuser                         # noqa: E402
    from kitlib.machinesetup import agenttemp as kagenttemp         # noqa: E402
    KITLIB_ERROR = None
except ImportError as e:
    kuser, kagenttemp, KITLIB_ERROR = None, None, e

KEY = "agent_temp_sweep"
DEFAULT_MAX_AGE_DAYS = 3
CLAIM = ".sweeping-"            # the name an entry is renamed to while it is removed: <name>.sweeping-<pid>
DAY = 86400.0
SESSIONS = "claude"             # Claude Code's scratch: <dir>/claude/<project>/<session>
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
KEPT = ("young", "kept", "live", "skipped")    # actions that leave a unit in place by design
EXIT_LIST_UNAVAILABLE = 3       # the process list could not be read: every old claude/ unit was kept
EXIT_DRAIN_REFUSED = 4          # --drain-old-user-temp: a claude.exe predates the TEMP change, or unknown
FIELDS = ("dir", "max_age_days", "keep", "user_temp")

Config = collections.namedtuple("Config", "dir max_age_days keep user_temp")


class SweepConfigError(ValueError):
    pass


def check(cfg):
    """The errors in an "agent_temp_sweep" object, as text lines (empty: valid)."""
    if not isinstance(cfg, dict):
        return ["%s must be an object" % KEY]
    errs = ["%s: unknown key %r" % (KEY, k) for k in cfg if k not in FIELDS]
    if not isinstance(cfg.get("user_temp", False), bool):
        errs.append("%s.user_temp must be true or false" % KEY)
    d = cfg.get("dir")
    if not (isinstance(d, str) and os.path.isabs(d)):
        errs.append("%s.dir must be an absolute path" % KEY)
    age = cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not age > 0:
        errs.append("%s.max_age_days must be a number of days above 0" % KEY)
    keep = cfg.get("keep", [])
    if not isinstance(keep, list):
        errs.append("%s.keep must be a list of relative paths" % KEY)
    else:
        for k in keep:
            if not (isinstance(k, str) and k.strip()):
                errs.append("%s.keep: %r is not a non-empty string" % (KEY, k))
            elif os.path.isabs(k) or os.path.splitdrive(k)[0] or k[0] in "\\/":
                errs.append("%s.keep: %r must be relative to dir" % (KEY, k))
            elif ".." in re.split(r"[\\/]", k):
                errs.append("%s.keep: %r may not hold '..'" % (KEY, k))
    return errs


def load(home=None):
    """(dir, max_age_days) from the machine config, or None when it has no "agent_temp_sweep".
    A bad value or an unreadable config raises SweepConfigError (load_config also gives keep)."""
    cfg = load_config(home)
    return None if cfg is None else (cfg.dir, cfg.max_age_days)


def load_config(home=None):
    """Config(dir, max_age_days, keep) from the machine config, or None when it has no
    "agent_temp_sweep". A bad value or an unreadable config raises SweepConfigError."""
    if kuser is None:
        raise SweepConfigError("the kit library (kitlib) is not in the kit root %s (%s), so the machine "
                               "config cannot be found; run the copy inside the kit or its installed "
                               "junction" % (KIT_ROOT, KITLIB_ERROR))
    try:
        path = kuser.readable_file(home)       # a BAD location is never "no config"
    except kuser.ConfigError as e:
        raise SweepConfigError(str(e))
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise SweepConfigError("cannot read %s: %s" % (path, e))
    cfg = data.get(KEY) if isinstance(data, dict) else None
    if cfg is None:
        return None
    errs = check(cfg)
    if errs:
        raise SweepConfigError("; ".join(errs))
    return Config(cfg["dir"], cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS), list(cfg.get("keep", [])),
                  cfg.get("user_temp", False))


def unsafe_dir(d, home=None):
    """Why d may not be swept, or None: kitlib.machinesetup.agenttemp.unsafe_dir with this kit
    (KIT_ROOT), the refusals machine setup applies to the folder it sets up (a root, the home folder
    or in it, the kit or the machine config folder, a git checkout, a link; as written and by its
    real path); and d must be an existing folder."""
    if kagenttemp is None:
        return "the kit library (kitlib) is not in the kit root %s (%s)" % (KIT_ROOT, KITLIB_ERROR)
    d = os.path.abspath(d)
    why = kagenttemp.unsafe_dir(d, os.path.abspath(home or os.path.expanduser("~")), KIT_ROOT)
    if why:
        return why
    if not os.path.isdir(d):
        return "%s is not a folder" % d
    return None


def registry_user_temp():
    """The user's own temp folder: HKCU\\Environment TEMP, else TMP, %VAR% expanded; None when
    neither is set or the key cannot be read. Never the process environment: the kit's launcher sets
    a session's TEMP to the agents' temp folder."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for name in ("TEMP", "TMP"):
                try:
                    value, kind = winreg.QueryValueEx(k, name)
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    if kind == winreg.REG_EXPAND_SZ:
                        value = winreg.ExpandEnvironmentStrings(value)
                    return value
    except (ImportError, OSError):
        return None
    return None


def local_temp():
    """Windows' default user temp, %LOCALAPPDATA%\\Temp; None without LOCALAPPDATA."""
    base = os.environ.get("LOCALAPPDATA")
    return os.path.join(base, "Temp") if base and os.path.isabs(base) else None


def env_changed_at():
    """When HKCU\\Environment was last written (epoch seconds), or None when it cannot be read.
    Setting TEMP writes it, so this is never earlier than the TEMP change."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryInfoKey(k)[2] / 1e7 - 11644473600.0     # 100 ns ticks since 1601
    except (ImportError, OSError):
        return None


_ISO = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}[:][0-9]{2}[:][0-9]{2})(?:[.]([0-9]+))?"
                  r"(Z|[+-][0-9]{2}[:][0-9]{2})?$")


def started_at(s):
    """A process's Created (ISO 8601, as kitlib.pmstart.list_processes gives it: up to 7 fraction
    digits, a local offset or Z) as epoch seconds; None when it does not parse."""
    m = _ISO.match(str(s).strip()) if isinstance(s, str) else None
    if not m:
        return None
    frac = ((m.group(2) or "") + "000000")[:6]
    zone = m.group(3) or ""
    try:
        return datetime.datetime.fromisoformat(
            "%s.%s%s" % (m.group(1), frac, "+00:00" if zone == "Z" else zone)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _forms(path):
    """[normcase abspath, normcase realpath] of path (one when they agree)."""
    a = os.path.normcase(os.path.abspath(path))
    try:
        r = os.path.normcase(os.path.realpath(a))
    except (OSError, ValueError):
        return [a]
    return [a] if r == a else [a, r]


def _within(a, b):
    """a is b or lies under it (normcase'd absolute paths)."""
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def overlaps(a, b):
    """a and b are the same folder, or one holds the other, as written or by real path."""
    return any(_within(x, y) or _within(y, x) for x in _forms(a) for y in _forms(b))


def _link_on_path(path):
    """The first link (symlink, junction, any reparse point) in path or a folder above it, or None."""
    p = os.path.abspath(path)
    while True:
        if is_link(p):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up


def second_root_unsafe(root, home=None):
    """Why the session folder `root` (<user temp>/claude) may not be swept as a second root, or
    None. It may have no link in it or above it; it must pass unsafe_dir (dir's refusals), with
    one exception: the home-folder refusal is lifted for exactly %LOCALAPPDATA%\\Temp\\claude
    (local_temp(), as written; no link means the real path is the same), where the kit and machine
    config refusals and the git checkout refusal still hold; and it must be an existing folder."""
    root = os.path.abspath(root)
    link = _link_on_path(root)
    if link:
        return "%s is a link or under one (%s)" % (root, link)
    why = unsafe_dir(root, home)
    if not why:
        return None
    lt = local_temp()
    if not (lt and os.path.basename(root) == SESSIONS and
            os.path.normcase(os.path.dirname(root)) == os.path.normcase(os.path.abspath(lt))):
        return why
    if kagenttemp is None:
        return why
    home = os.path.abspath(home or os.path.expanduser("~"))
    for name, other in (("the kit checkout", KIT_ROOT),
                        ("the machine config folder", kuser.config_dir(home))):
        if overlaps(root, other):
            return "%s is %s %s, in it or holds it" % (root, name, other)
    top = kagenttemp.in_checkout(root)
    if top:
        return "%s is inside a git checkout or worktree (%s has .git)" % (root, top)
    if not os.path.isdir(root):
        return "%s is not a folder" % root
    return None


def is_link(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0)) or \
        (hasattr(os.path, "isjunction") and os.path.isjunction(path))


def _when(st):
    """Written or created, whichever is later (st_birthtime where the platform has it)."""
    return max(st.st_mtime, getattr(st, "st_birthtime", st.st_ctime))


def newest(path, renamed=False):
    """The newest written-or-created time anywhere in path, links not followed (a link counts by
    its own times). Unreadable parts count as now, so they are never taken for old. renamed: the
    sweep has just renamed path (its claim), so path's own change time is the sweep's, not a use;
    only its written time counts (what is inside counts in full)."""
    try:
        st = os.lstat(path)
    except OSError:
        return time.time()
    t = st.st_mtime if renamed else _when(st)
    if not stat.S_ISDIR(st.st_mode) or is_link(path):
        return t
    stack = [path]
    while stack:
        top = stack.pop()
        try:
            it = list(os.scandir(top))
        except OSError:
            return time.time()
        for e in it:
            try:
                est = e.stat(follow_symlinks=False)
            except OSError:
                return time.time()
            t = max(t, _when(est))
            if e.is_dir(follow_symlinks=False) and not is_link(e.path):
                stack.append(e.path)
    return t


def _remove_tree(path, failed):
    """Removes path (links as links); appends what could not be removed to failed."""
    if is_link(path):
        try:
            os.unlink(path)             # the link itself, a folder link or junction included
        except OSError:
            try:
                os.rmdir(path)
            except OSError as e:
                failed.append("%s: %s" % (path, e))
        return
    if not os.path.isdir(path):
        try:
            _unlink(path)
        except OSError as e:
            failed.append("%s: %s" % (path, e))
        return
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        failed.append("%s: %s" % (path, e))
        return
    for e in entries:
        _remove_tree(e.path, failed)
    try:
        os.rmdir(path)
    except OSError as e:
        failed.append("%s: %s" % (path, e))


def _unlink(path):
    """A plain file (never a link: chmod would reach its target)."""
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)    # a read-only file
        os.unlink(path)


class Entry:
    def __init__(self, name, age_days, action, why=""):
        self.name, self.age_days, self.action, self.why = name, age_days, action, why

    def line(self):
        return "%-8s %s (%.1f days old)%s" % (self.action, self.name, self.age_days,
                                             ": " + self.why if self.why else "")


def _is_sessions(e):
    """The top-level entry is Claude Code's session scratch folder: a real folder named claude."""
    return e.name.lower() == SESSIONS and e.is_dir(follow_symlinks=False) and not is_link(e.path)


def _real_dir(e):
    return e.is_dir(follow_symlinks=False) and not is_link(e.path)


def _entries(path):
    return sorted(os.scandir(path), key=lambda e: e.name)


def _top(d):
    """The top-level entries of d; SweepConfigError when d itself cannot be listed."""
    try:
        return _entries(d)
    except OSError as e:
        raise SweepConfigError("%s cannot be listed: %s" % (d, e.strerror or e))


def _listing(rel, path, skipped):
    """The entries of the folder `path` (rel under dir), or [] after adding rel to skipped when it
    cannot be listed (gone since, or no permission): that folder is left alone, the rest goes on."""
    try:
        return _entries(path)
    except OSError as e:
        skipped.append((rel, "cannot be listed (%s)" % (e.strerror or e)))
        return []


def units(d, skipped=None, sessions_only=False):
    """[(relpath, parent, name)] of the sweep's units, relpath "/"-joined: every top-level entry of
    d but a real folder named claude; for that, every entry of each claude/<project> folder, and
    each file or link straight in claude/. Every path is d joined with names os.scandir gave.
    skipped: a list that gets (relpath, why) for each claude/ or claude/<project> folder that
    cannot be listed; nothing in it is a unit. sessions_only: the claude/ units alone (a second
    root: the rest of d is not the kit's)."""
    skipped = [] if skipped is None else skipped
    out = []
    for e in _top(d):
        if not _is_sessions(e):
            if not sessions_only:
                out.append((e.name, d, e.name))
            continue
        for p in _listing(e.name, e.path, skipped):
            if not _real_dir(p):
                out.append(("%s/%s" % (e.name, p.name), e.path, p.name))
                continue
            rel = "%s/%s" % (e.name, p.name)
            for s in _listing(rel, p.path, skipped):
                out.append(("%s/%s" % (rel, s.name), p.path, s.name))
    return out


def _projects(d):
    """[(relpath, path)] of the claude/<project> folders (real folders only)."""
    for e in _top(d):
        if _is_sessions(e):
            return [("%s/%s" % (e.name, p.name), p.path) for p in _listing(e.name, e.path, [])
                    if _real_dir(p)]
    return []


def is_kept(rel, keep):
    """A keep item names rel ("/"-joined, under dir), a folder holding it, or something inside it:
    name by name over the shorter of the two, case-insensitive, * and ? within one name. The sweep
    never enters a unit, so an item inside one (kit-coordinator/r2) keeps the whole unit."""
    parts = rel.lower().split("/")
    for k in keep or ():
        pat = [x for x in k.lower().replace("\\", "/").split("/") if x]
        if pat and all(fnmatch.fnmatchcase(n, p) for n, p in zip(parts, pat)):
            return True
    return False


# kept whatever "keep" says: the launcher's <repo>-consultant notes folder and a coordinator's
# <repo>-coordinator state-note folder (kitlib.launcher.start CONSULTANT_FOLDER, COORDINATOR_FOLDER)
BUILT_IN_KEEP = ("*-coordinator", "*-consultant")


LIST_TIMEOUT = 30       # seconds for the process list; the session start's 4 s is too short at 04:00
UNREADABLE = "the process list could not be read"


def default_lister():
    """The running processes as dicts with a CommandLine (kitlib.pmstart.list_processes, given
    LIST_TIMEOUT seconds)."""
    from kitlib.pmstart import list_processes
    return list_processes(timeout=LIST_TIMEOUT)


def live_ids(procs):
    """The session ids (lower case) that appear in any process's command line."""
    ids = set()
    for p in procs or ():
        ids.update(m.lower() for m in UUID.findall(str((p or {}).get("CommandLine") or "")))
    return ids


def sweep(d, max_age_days, apply=False, now=None, home=None, keep=(), lister=None, sessions_only=False):
    """[Entry] for every unit of d (units()) and every claude/<project> folder it removes, oldest
    first. action: "young" (too new), "kept" (keep-list or BUILT_IN_KEEP), "live" (under claude/: a running process
    names it, or the process list could not be read), "would" (old; apply not set), "removed", or
    "in-use" / "failed" (kept), or "skipped" (a claude/ or claude/<project> folder that could not
    be listed; its units are not in the list). lister gives the processes (default_lister); it is called only
    when an old unit under claude/ is met. Raises SweepConfigError when d may not be swept
    (unsafe_dir). sessions_only: d is a second root (a user temp): only its claude/ units are
    swept, and the check is second_root_unsafe on d/claude, not unsafe_dir on d."""
    why = second_root_unsafe(os.path.join(d, SESSIONS), home) if sessions_only else unsafe_dir(d, home)
    if why:
        raise SweepConfigError(why)
    now = time.time() if now is None else now
    cutoff = now - max_age_days * DAY
    lister = lister or default_lister
    box = {}

    def live(name):
        """None (not live) or why a unit under claude/ is held back."""
        if "ids" not in box:
            try:
                box["ids"], box["err"] = live_ids(lister()), None
            except Exception as e:                # fail closed: nothing under claude/ goes
                box["ids"], box["err"] = set(), "%s: %s" % (type(e).__name__, e)
        if box["err"]:
            return "%s (%s)" % (UNREADABLE, box["err"])
        if name.lower() in box["ids"]:
            return "a running process names it"
        return None

    projects = [(rel, p, _own_time(p)) for rel, p in _projects(d)]
    skipped = []
    found = units(d, skipped, sessions_only)
    out = [Entry(rel, 0.0, "skipped", why) for rel, why in skipped]
    projects = [x for x in projects if x[0] not in {rel for rel, _ in skipped}]
    keep = tuple(keep or ()) + BUILT_IN_KEEP
    for rel, parent, name in found:
        if is_kept(rel, keep):
            out.append(Entry(rel, 0.0, "kept"))
            continue
        path = os.path.join(parent, name)
        t = newest(path)                # a claim a stopped sweep left is a unit like any other
        age = max(0.0, (now - t) / DAY)
        held = live(name) if t <= cutoff and "/" in rel else None
        if t > cutoff:
            out.append(Entry(rel, age, "young"))
        elif held:
            out.append(Entry(rel, age, "live", held))
        elif not apply:
            out.append(Entry(rel, age, "would"))
        else:
            out.append(_claim_and_remove(parent, name, age, cutoff, now, rel))
    for rel, p, t in projects:          # an empty, old project folder goes; claude/ itself never
        if t is None or t > cutoff or is_kept(rel, keep):
            continue
        inside = [e for e in out if e.name.startswith(rel + "/")]
        if not apply:
            if all(e.action == "would" for e in inside):
                out.append(Entry(rel, (now - t) / DAY, "would"))
            continue
        try:
            os.rmdir(p)                 # fails while anything is in it: a new session keeps it
        except OSError:
            continue
        out.append(Entry(rel, (now - t) / DAY, "removed"))
    out.sort(key=lambda x: -x.age_days)
    return out


def _own_time(path):
    """A folder's own written-or-created time, read before its units are removed (removing them
    writes it); None when it cannot be read."""
    try:
        return _when(os.lstat(path))
    except OSError:
        return None


def _claim_and_remove(d, name, age, cutoff, now, rel=None):
    rel = rel or name
    src = os.path.join(d, name)
    claimed = os.path.join(d, "%s%s%d" % (name, CLAIM, os.getpid()))
    try:
        os.rename(src, claimed)
    except OSError as e:
        return Entry(rel, age, "in-use", "could not be claimed (%s)" % (e.strerror or e))
    t = newest(claimed, renamed=True)   # written to between the listing and the claim: keep it
    if t > cutoff:
        try:
            os.rename(claimed, src)
        except OSError as e:
            return Entry(rel, age, "failed", "written to after it was listed, and could not be "
                         "put back under its own name (%s)" % (e.strerror or e))
        return Entry(rel, max(0.0, (now - t) / DAY), "young", "written to after it was listed")
    failed = []
    _remove_tree(claimed, failed)
    if not failed:
        return Entry(rel, age, "removed")
    try:
        if os.path.lexists(claimed):
            os.rename(claimed, src)     # what is left goes back under its own name
    except OSError:
        pass
    return Entry(rel, age, "failed", "%d part(s) could not be removed, first %s"
                 % (len(failed), failed[0]))


def main(argv=None, home=None, out=None, now=None, lister=None):
    """home, now and lister are the tests' (a scratch home folder, a clock moved forward, a fake
    process list)."""
    if out is None:
        # piped, stdout is the locale code page: an entry name outside it must print, not raise
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
        out = sys.stdout
    p = argparse.ArgumentParser(prog="agent_temp_sweep.py",
                                description="Clear old, unused entries from the agents' temp folder "
                                            "(the folder and age come from the machine config).")
    p.add_argument("--apply", action="store_true", help="remove (default: only list, a dry run)")
    p.add_argument("--home", help="the home folder whose machine config is read (default: the real "
                                  "home; machine setup's --home passes it)")
    p.add_argument("--drain-old-user-temp", action="store_true",
                   help="sweep only %%LOCALAPPDATA%%\\Temp\\claude, the Windows default user temp's "
                        "session folder, once no claude.exe predates the user environment's last "
                        "change (else exit 4); a dry run unless --apply")
    a = p.parse_args(argv)
    home = a.home or home
    lister = _once(lister or default_lister)       # one process list for every root and the gate
    try:
        cfg = load_config(home)
        if cfg is None:
            print("agent_temp_sweep: no %r key in the machine config; nothing to sweep" % KEY,
                  file=sys.stderr)
            return 2
        if a.drain_old_user_temp:
            return drain(cfg, a.apply, now, home, lister, out)
        entries = sweep(cfg.dir, cfg.max_age_days, apply=a.apply, now=now, home=home, keep=cfg.keep,
                        lister=lister)
    except SweepConfigError as e:
        print("agent_temp_sweep: refused: %s" % e, file=sys.stderr)
        return 2
    code = report(cfg.dir, entries, cfg.max_age_days, a.apply, out)
    if cfg.user_temp:
        code = max(code, user_temp_sweep(cfg, a.apply, now, home, lister, out))    # 3 wins over 1
    return code


def _once(lister):
    """lister, called at most once: its list (or its exception) is given again on every call."""
    box = {}

    def call():
        if "v" not in box:
            try:
                box["v"], box["e"] = lister(), None
            except Exception as e:
                box["v"], box["e"] = None, e
        if box["e"] is not None:
            raise box["e"]
        return box["v"]
    return call


def report(root, entries, age, apply, out):
    """Prints a sweep's entries (but the young) and its summary line; the exit code it calls for."""
    for e in entries:
        if e.action != "young":
            print(e.line(), file=out)
    old = [e for e in entries if e.action not in KEPT]
    print("%s: %d entries, %d kept (newer than %g days, on the keep-list, a live session or not "
          "listable), %d %s" % (
              root, len(entries), len(entries) - len(old), age, len(old),
              "old" if apply else "old (dry run: --apply removes them)"), file=out)
    blind = [e for e in entries if e.action == "live" and e.why.startswith(UNREADABLE)]
    if blind:
        print("agent_temp_sweep: kept every claude/ folder: process list unavailable: %s"
              % blind[0].why[len(UNREADABLE) + 2:-1], file=out)    # UNREADABLE (<why>)
        return EXIT_LIST_UNAVAILABLE
    return 1 if any(e.action in ("in-use", "failed") for e in old) else 0


def user_temp_sweep(cfg, apply, now, home, lister, out):
    """The second root: <user temp>/claude, its session units only. A note and 0 when it is
    skipped (no registry value, no such folder, dir's own, or second_root_unsafe refuses it)."""
    u = registry_user_temp()
    if not u or not os.path.isabs(u):
        why = "HKCU\\Environment has no TEMP or TMP" if not u else "its TEMP %r is not absolute" % u
    else:
        root = os.path.join(u, SESSIONS)
        if overlaps(root, cfg.dir):
            why = "%s is %s, in it or holds it (the main run sweeps it)" % (root, cfg.dir)
        elif not os.path.isdir(root):
            why = "%s is not a folder" % root
        else:
            why = second_root_unsafe(root, home)
    if why:
        print("user temp: skipped: %s" % why, file=out)
        return 0
    try:
        entries = sweep(u, cfg.max_age_days, apply=apply, now=now, home=home, keep=cfg.keep,
                        lister=lister, sessions_only=True)
    except SweepConfigError as e:
        print("user temp: skipped: %s" % e, file=out)
        return 0
    return report(root, entries, cfg.max_age_days, apply, out)


def drain_blockers(lister):
    """Why the drain may not run yet: one line per claude.exe (any case) that started before
    HKCU\\Environment was last written or whose start cannot be read, or one line when that time
    or the process list cannot be read (fail closed). [] when the drain may run."""
    changed = env_changed_at()
    if changed is None:
        return ["the time HKCU\\Environment was last written cannot be read"]
    try:
        procs = list(lister() or ())
    except Exception as e:
        return ["%s (%s: %s)" % (UNREADABLE, type(e).__name__, e)]
    if not procs:                       # a real list holds this python at least
        return ["%s (it came back empty)" % UNREADABLE]
    when = datetime.datetime.fromtimestamp(changed).isoformat(timespec="seconds")
    out = []
    for p in procs:
        p = p or {}
        if str(p.get("Name") or "").lower() != "claude.exe":
            continue
        t = started_at(p.get("Created"))
        if t is None:
            out.append("pid %s: its start time %r cannot be read" % (p.get("ProcessId"), p.get("Created")))
        elif t < changed:
            out.append("pid %s started %s, before the user environment changed (%s)"
                       % (p.get("ProcessId"), p.get("Created"), when))
    return out


def drain(cfg, apply, now, home, lister, out):
    """--drain-old-user-temp: sweeps %LOCALAPPDATA%\\Temp\\claude as a second root once the gate
    (drain_blockers) is open; dry run: lists it either way. Exit 4 when the gate is shut."""
    lt = local_temp()
    root = os.path.join(lt, SESSIONS) if lt else None
    u = registry_user_temp()
    if not root:
        nothing = "LOCALAPPDATA is not set"
    elif not os.path.isdir(root):
        nothing = "%s is not there" % root
    elif u and os.path.isabs(u) and "%" not in u and overlaps(os.path.join(u, SESSIONS), root):
        nothing = "%s is the current user temp's (HKCU\\Environment TEMP %s)" % (root, u)
    elif overlaps(root, cfg.dir):
        nothing = "%s is %s, in it or holds it (the daily run sweeps it)" % (root, cfg.dir)
    else:
        nothing = None
    if nothing:
        print("drain: nothing to drain: %s" % nothing, file=out)
        return 0
    blockers = drain_blockers(lister)
    if not u:
        blockers.insert(0, "HKCU\\Environment has no TEMP or TMP, so %s may still be in use" % root)
    elif not os.path.isabs(u) or "%" in u:     # relative or an unexpanded %VAR%: which folder it is cannot be told
        blockers.insert(0, "HKCU\\Environment TEMP %r is not absolute or holds an unexpanded %%VAR%%, "
                        "so %s may still be in use" % (u, root))
    for b in blockers:
        print("drain: held: %s" % b, file=out)
    if blockers and apply:
        print("drain: refused: nothing removed; end those sessions (restart the desktop app) and "
              "run it again", file=out)
        return EXIT_DRAIN_REFUSED
    entries = sweep(lt, cfg.max_age_days, apply=apply, now=now, home=home, keep=cfg.keep,
                    lister=lister, sessions_only=True)
    code = report(root, entries, cfg.max_age_days, apply, out)
    if blockers:
        print("drain: the gate is shut; --apply would remove nothing yet", file=out)
        return EXIT_DRAIN_REFUSED
    return code


if __name__ == "__main__":
    sys.exit(main())
