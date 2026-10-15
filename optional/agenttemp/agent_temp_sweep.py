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

Exit codes: 0 done (or nothing to do); 1 some old entry could not be removed (in use or failed);
2 no config, a bad config, an unsafe dir or a dir that cannot be listed; 3 the process list could
not be read, so every old unit under claude/ was kept (3 wins over 1).
"""
import argparse
import collections
import fnmatch
import json
import os
import re
import stat
import sys
import time

# The machine config's place comes from the kit library (kitlib.config.user), found at the kit's
# root through the real path: an installed junction (~/.claude/tools/agenttemp) resolves into the kit.
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

Config = collections.namedtuple("Config", "dir max_age_days keep")


class SweepConfigError(ValueError):
    pass


def check(cfg):
    """The errors in an "agent_temp_sweep" object, as text lines (empty: valid)."""
    if not isinstance(cfg, dict):
        return ["%s must be an object" % KEY]
    errs = ["%s: unknown key %r" % (KEY, k) for k in cfg if k not in ("dir", "max_age_days", "keep")]
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
    return Config(cfg["dir"], cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS), list(cfg.get("keep", [])))


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


def units(d, skipped=None):
    """[(relpath, parent, name)] of the sweep's units, relpath "/"-joined: every top-level entry of
    d but a real folder named claude; for that, every entry of each claude/<project> folder, and
    each file or link straight in claude/. Every path is d joined with names os.scandir gave.
    skipped: a list that gets (relpath, why) for each claude/ or claude/<project> folder that
    cannot be listed; nothing in it is a unit."""
    skipped = [] if skipped is None else skipped
    out = []
    for e in _top(d):
        if not _is_sessions(e):
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


def sweep(d, max_age_days, apply=False, now=None, home=None, keep=(), lister=None):
    """[Entry] for every unit of d (units()) and every claude/<project> folder it removes, oldest
    first. action: "young" (too new), "kept" (keep-list or BUILT_IN_KEEP), "live" (under claude/: a running process
    names it, or the process list could not be read), "would" (old; apply not set), "removed", or
    "in-use" / "failed" (kept), or "skipped" (a claude/ or claude/<project> folder that could not
    be listed; its units are not in the list). lister gives the processes (default_lister); it is called only
    when an old unit under claude/ is met. Raises SweepConfigError when d may not be swept
    (unsafe_dir)."""
    why = unsafe_dir(d, home)
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
    found = units(d, skipped)
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
    a = p.parse_args(argv)
    home = a.home or home
    try:
        cfg = load_config(home)
        if cfg is None:
            print("agent_temp_sweep: no %r key in the machine config; nothing to sweep" % KEY,
                  file=sys.stderr)
            return 2
        d, age = cfg.dir, cfg.max_age_days
        entries = sweep(d, age, apply=a.apply, now=now, home=home, keep=cfg.keep, lister=lister)
    except SweepConfigError as e:
        print("agent_temp_sweep: refused: %s" % e, file=sys.stderr)
        return 2
    for e in entries:
        if e.action != "young":
            print(e.line(), file=out)
    old = [e for e in entries if e.action not in KEPT]
    print("%s: %d entries, %d kept (newer than %g days, on the keep-list, a live session or not "
          "listable), %d %s" % (
              d, len(entries), len(entries) - len(old), age, len(old),
              "old" if a.apply else "old (dry run: --apply removes them)"), file=out)
    blind = [e for e in entries if e.action == "live" and e.why.startswith(UNREADABLE)]
    if blind:
        print("agent_temp_sweep: kept every claude/ folder: process list unavailable: %s"
              % blind[0].why[len(UNREADABLE) + 2:-1], file=out)    # UNREADABLE (<why>)
        return EXIT_LIST_UNAVAILABLE
    return 1 if any(e.action in ("in-use", "failed") for e in old) else 0


if __name__ == "__main__":
    sys.exit(main())
