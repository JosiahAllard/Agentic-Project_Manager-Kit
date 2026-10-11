"""agent_temp_sweep.py: clears old entries from the agents' shared temp folder while nothing uses them.

  agent_temp_sweep.py [--apply]

Without --apply it only lists what it would remove (a dry run); with it, it removes. The folder and
the age come from the "agent_temp_sweep" key of the machine config, never from code or
the command line: {"dir": <absolute folder>, "max_age_days": <number, default 3>, "keep": [...]}.
"keep" (default empty) lists relative paths under dir that the sweep never removes: a top-level
name, or claude/<project>/<session>; * and ? match within one name; case-insensitive; a kept folder
keeps everything in it.

The units are the top-level files and folders of dir, except a folder named "claude": Claude Code
keeps each session's scratch at claude/<project>/<session>, so each entry of claude/<project> is its
own unit (a file or link straight in claude/ is one too). claude/ is never removed; claude/<project>
only once it is empty and itself older than the age. A unit on the keep-list is reported "kept" and
never aged, claimed or entered. A unit is removed only when all of these hold:
- the newest time anywhere in it (written or created, links not followed) is older than the age;
- under claude/ only: no running process has the unit's name (a session id) in its command line.
  The process list is read once per run; when it cannot be read, no unit under claude/ is removed
  that run ("live"). A Claude Code process normally carries no session id in its command line
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
Links are removed as links; their targets are never entered. dir may not be a filesystem root, the
home folder or a folder holding it, a link, inside a git checkout or worktree (a .git folder or file
in it or any folder above it), or the machine config folder or the kit checkout, a folder inside one
or a folder holding one. Standard library and the kit's own kitlib only.

Exit codes: 0 done (or nothing to do); 1 some old entry could not be removed (in use or failed);
2 no config, a bad config or an unsafe dir.
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
    KITLIB_ERROR = None
except ImportError as e:
    kuser, KITLIB_ERROR = None, e

KEY = "agent_temp_sweep"
DEFAULT_MAX_AGE_DAYS = 3
CLAIM = ".sweeping-"            # the name an entry is renamed to while it is removed: <name>.sweeping-<pid>
DAY = 86400.0
SESSIONS = "claude"             # Claude Code's scratch: <dir>/claude/<project>/<session>
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
KEPT = ("young", "kept", "live")    # actions that leave a unit in place by design

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


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _within(path, root):
    """path is root or lies under it."""
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def unsafe_dir(d, home=None):
    """Why d may not be swept, or None: it must be an existing folder that is not a drive or
    filesystem root, not the home folder or above it, not a link, not in a git checkout or
    worktree, and not the machine config folder or the kit checkout, in one or holding one."""
    d = os.path.abspath(d)
    if os.path.dirname(d) == d:
        return "%s is a filesystem root" % d
    h = os.path.abspath(home or os.path.expanduser("~"))
    if _within(h, d):
        return "%s is the home folder or holds it" % d
    if is_link(d):
        return "%s is a link" % d
    if not os.path.isdir(d):
        return "%s is not a folder" % d
    for name, root in (("the kit checkout", KIT_ROOT),
                       ("the machine config folder", kuser.config_dir(h) if kuser else None)):
        if root and (_within(d, root) or _within(root, d)):
            return "%s is %s %s, in it or holds it" % (d, name, root)
    a = d
    while True:
        if os.path.lexists(os.path.join(a, ".git")):
            return "%s is inside a git checkout or worktree (%s has .git)" % (d, a)
        up = os.path.dirname(a)
        if up == a:
            return None
        a = up


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


def units(d):
    """[(relpath, parent, name)] of the sweep's units, relpath "/"-joined: every top-level entry of
    d but a real folder named claude; for that, every entry of each claude/<project> folder, and
    each file or link straight in claude/. Every path is d joined with names os.scandir gave."""
    out = []
    for e in _entries(d):
        if not _is_sessions(e):
            out.append((e.name, d, e.name))
            continue
        for p in _entries(e.path):
            if not _real_dir(p):
                out.append(("%s/%s" % (e.name, p.name), e.path, p.name))
                continue
            for s in _entries(p.path):
                out.append(("%s/%s/%s" % (e.name, p.name, s.name), p.path, s.name))
    return out


def _projects(d):
    """[(relpath, path)] of the claude/<project> folders (real folders only)."""
    for e in _entries(d):
        if _is_sessions(e):
            return [("%s/%s" % (e.name, p.name), p.path) for p in _entries(e.path) if _real_dir(p)]
    return []


def is_kept(rel, keep):
    """rel ("/"-joined, under dir) or a folder holding it matches a keep item: name by name,
    case-insensitive, * and ? within one name."""
    parts = rel.lower().split("/")
    for k in keep or ():
        pat = [x for x in k.lower().replace("\\", "/").split("/") if x]
        if pat and len(pat) <= len(parts) and \
                all(fnmatch.fnmatchcase(n, p) for n, p in zip(parts, pat)):
            return True
    return False


def default_lister():
    """The running processes as dicts with a CommandLine (kitlib.pmstart.list_processes)."""
    from kitlib.pmstart import list_processes
    return list_processes()


def live_ids(procs):
    """The session ids (lower case) that appear in any process's command line."""
    ids = set()
    for p in procs or ():
        ids.update(m.lower() for m in UUID.findall(str((p or {}).get("CommandLine") or "")))
    return ids


def sweep(d, max_age_days, apply=False, now=None, home=None, keep=(), lister=None):
    """[Entry] for every unit of d (units()) and every claude/<project> folder it removes, oldest
    first. action: "young" (too new), "kept" (keep-list), "live" (under claude/: a running process
    names it, or the process list could not be read), "would" (old; apply not set), "removed", or
    "in-use" / "failed" (kept). lister gives the processes (default_lister); it is called only
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
            return "the process list could not be read (%s)" % box["err"]
        if name.lower() in box["ids"]:
            return "a running process names it"
        return None

    projects = [(rel, p, _own_time(p)) for rel, p in _projects(d)]
    out = []
    for rel, parent, name in units(d):
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
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="agent_temp_sweep.py",
                                description="Clear old, unused entries from the agents' temp folder "
                                            "(the folder and age come from the machine config).")
    p.add_argument("--apply", action="store_true", help="remove (default: only list, a dry run)")
    a = p.parse_args(argv)
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
    print("%s: %d entries, %d kept (newer than %g days, on the keep-list or a live session), %d %s" % (
        d, len(entries), len(entries) - len(old), age, len(old),
        "old" if a.apply else "old (dry run: --apply removes them)"), file=out)
    return 1 if any(e.action in ("in-use", "failed") for e in old) else 0


if __name__ == "__main__":
    sys.exit(main())
