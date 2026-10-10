"""agent_temp_sweep.py: clears old entries from the agents' shared temp folder while nothing uses them.

  agent_temp_sweep.py [--apply]

Without --apply it only lists what it would remove (a dry run); with it, it removes. The folder and
the age come from the "agent_temp_sweep" key of the machine config, never from code or
the command line: {"dir": <absolute folder>, "max_age_days": <number, default 3>}. An entry (a
top-level file or folder of dir) is removed only when all of these hold:
- the newest time anywhere in it (written or created, links not followed) is older than the age;
- it can be claimed: renamed aside (<name>.sweeping-<pid>) in one step. On Windows that fails while
  a process has a file in it open (unless opened to allow deletion) or its working folder in it, so
  an entry in use is kept;
- it is still old once claimed (the age is read again under the new name, so a write between the
  listing and the claim keeps it, put back under its own name);
- every part of it can be removed. A removal that fails part-way puts what is left back under its
  own name.
Links are removed as links; their targets are never entered. dir may not be a filesystem root, the
home folder or a folder holding it, or a link. Standard library and the kit's own kitlib only.

Exit codes: 0 done (or nothing to do); 1 some old entry could not be removed (in use or failed);
2 no config, a bad config or an unsafe dir.
"""
import argparse
import json
import os
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


class SweepConfigError(ValueError):
    pass


def check(cfg):
    """The errors in an "agent_temp_sweep" object, as text lines (empty: valid)."""
    if not isinstance(cfg, dict):
        return ["%s must be an object" % KEY]
    errs = ["%s: unknown key %r" % (KEY, k) for k in cfg if k not in ("dir", "max_age_days")]
    d = cfg.get("dir")
    if not (isinstance(d, str) and os.path.isabs(d)):
        errs.append("%s.dir must be an absolute path" % KEY)
    age = cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not age > 0:
        errs.append("%s.max_age_days must be a number of days above 0" % KEY)
    return errs


def load(home=None):
    """(dir, max_age_days) from the machine config, or None when it has no "agent_temp_sweep".
    A bad value or an unreadable config raises SweepConfigError."""
    if kuser is None:
        raise SweepConfigError("the kit library (kitlib) is not in the kit root %s (%s), so the machine "
                               "config cannot be found; run the copy inside the kit or its installed "
                               "junction" % (KIT_ROOT, KITLIB_ERROR))
    path = kuser.config_file(home)
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
    return cfg["dir"], cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS)


def unsafe_dir(d, home=None):
    """Why d may not be swept, or None: it must be an existing folder that is not a drive or
    filesystem root, not the home folder or above it, and not a link."""
    d = os.path.abspath(d)
    if os.path.dirname(d) == d:
        return "%s is a filesystem root" % d
    h = os.path.abspath(home or os.path.expanduser("~"))
    if os.path.normcase(h) == os.path.normcase(d) or \
            os.path.normcase(h).startswith(os.path.normcase(d.rstrip("\\/")) + os.sep):
        return "%s is the home folder or holds it" % d
    if is_link(d):
        return "%s is a link" % d
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


def sweep(d, max_age_days, apply=False, now=None, home=None):
    """[Entry] for every top-level entry of d, oldest first. action: "young" (kept: too new),
    "would" (old; apply not set), "removed", or "in-use" / "failed" (kept). Raises
    SweepConfigError when d may not be swept (unsafe_dir)."""
    why = unsafe_dir(d, home)
    if why:
        raise SweepConfigError(why)
    now = time.time() if now is None else now
    cutoff = now - max_age_days * DAY
    out = []
    for e in sorted(os.scandir(d), key=lambda e: e.name):
        t = newest(e.path)              # a claim a stopped sweep left is an entry like any other
        age = max(0.0, (now - t) / DAY)
        if t > cutoff:
            out.append(Entry(e.name, age, "young"))
        elif not apply:
            out.append(Entry(e.name, age, "would"))
        else:
            out.append(_claim_and_remove(d, e.name, age, cutoff, now))
    out.sort(key=lambda x: -x.age_days)
    return out


def _claim_and_remove(d, name, age, cutoff, now):
    src = os.path.join(d, name)
    claimed = os.path.join(d, "%s%s%d" % (name, CLAIM, os.getpid()))
    try:
        os.rename(src, claimed)
    except OSError as e:
        return Entry(name, age, "in-use", "could not be claimed (%s)" % (e.strerror or e))
    t = newest(claimed, renamed=True)   # written to between the listing and the claim: keep it
    if t > cutoff:
        try:
            os.rename(claimed, src)
        except OSError as e:
            return Entry(name, age, "failed", "written to after it was listed, and could not be "
                         "put back under its own name (%s)" % (e.strerror or e))
        return Entry(name, max(0.0, (now - t) / DAY), "young", "written to after it was listed")
    failed = []
    _remove_tree(claimed, failed)
    if not failed:
        return Entry(name, age, "removed")
    try:
        if os.path.lexists(claimed):
            os.rename(claimed, src)     # what is left goes back under its own name
    except OSError:
        pass
    return Entry(name, age, "failed", "%d part(s) could not be removed, first %s"
                 % (len(failed), failed[0]))


def main(argv=None, home=None, out=None, now=None):
    """home and now are the tests' (a scratch home folder, a clock moved forward)."""
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="agent_temp_sweep.py",
                                description="Clear old, unused entries from the agents' temp folder "
                                            "(the folder and age come from the machine config).")
    p.add_argument("--apply", action="store_true", help="remove (default: only list, a dry run)")
    a = p.parse_args(argv)
    try:
        got = load(home)
        if got is None:
            print("agent_temp_sweep: no %r key in the machine config; nothing to sweep" % KEY,
                  file=sys.stderr)
            return 2
        d, age = got
        entries = sweep(d, age, apply=a.apply, now=now, home=home)
    except SweepConfigError as e:
        print("agent_temp_sweep: refused: %s" % e, file=sys.stderr)
        return 2
    old = [e for e in entries if e.action != "young"]
    for e in old:
        print(e.line(), file=out)
    print("%s: %d entries, %d newer than %g days kept, %d %s" % (
        d, len(entries), len(entries) - len(old), age, len(old),
        "old" if a.apply else "old (dry run: --apply removes them)"), file=out)
    return 1 if any(e.action in ("in-use", "failed") for e in old) else 0


if __name__ == "__main__":
    sys.exit(main())
