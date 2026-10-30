"""kitlib.coordinatorstart.parts: the read-only parts of the coordinator start header that save a
start its routine calls: the state note's tail, `hub pending`, the `backlog next` offer and the
stale worktrees (`worktree_remove --classify`).

Each part is a function returning lines; run_all runs them in parallel threads under one overall
deadline, and a part that fails, raises or is late becomes one "could not read X: reason" line, so
no part can fail the session start or hold it up. Each part's text is capped (CAP chars). The live
agents (ListAgents) are a host tool of the session, not knowable from a hook: they are not here.
"""
import glob
import os
import subprocess
import sys
import threading
import time

from kitlib import kitroot
from kitlib.git import cmd as gitcmd

PART_TIMEOUT = 5      # seconds for one subprocess part
DEADLINE = 9          # seconds for all parts together
TAIL_LINES = 10       # state note lines shown
TAIL_BYTES = 8192     # read from the end of the note, never the whole file
LINE_CHARS = 200
CAP = 1500            # chars of one part
NEXT_LINES = 8
FOLDER_SUFFIX = "-coordinator"


def run_loose(args, timeout=PART_TIMEOUT):
    """stdout whatever the exit code (worktree_remove --classify exits 1 when it finds a tree to
    act on); a failure with no stdout raises."""
    r = gitcmd.subprocess_run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError("%s exit %d: %s" % (os.path.basename(args[0]), r.returncode, r.stderr.strip()[:200]))
    return r.stdout


def capped(lines, cap=CAP):
    """`lines` cut so their text stays within `cap` chars, with a marker naming the lines dropped."""
    out, used = [], 0
    for i, l in enumerate(lines):
        l = l[:LINE_CHARS]
        if used + len(l) > cap and out:
            out.append("... (+%d more lines)" % (len(lines) - i))
            break
        out.append(l)
        used += len(l) + 1
    return out


def agent_temp(env):
    return env.get("AGENT_TEMP") or env.get("TEMP") or os.path.expanduser("~")


def _tail(path, n=TAIL_LINES):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return [l for l in lines if l.strip()][-n:]


def state_note(env, name, slugs):
    """The note's path line and last lines. `name` (PM_KIT_SESSION_NAME) names the file exactly:
    <agent temp>/<slug>-coordinator/state-<name>.md, whichever coordinator folder holds it. Without
    a name, or without that file, the newest state-*.md of a folder named for one of `slugs` is
    shown and said to be guessed."""
    temp = agent_temp(env)
    note, guessed = None, False
    if name:
        hits = glob.glob(os.path.join(glob.escape(temp), "*" + FOLDER_SUFFIX, "state-%s.md" % glob.escape(name)))
        if hits:
            note = max(hits, key=os.path.getmtime)
    if note is None:
        hits = []
        for s in slugs:
            hits += glob.glob(os.path.join(glob.escape(temp), glob.escape(s) + FOLDER_SUFFIX, "state-*.md"))
        if hits:
            note, guessed = max(hits, key=os.path.getmtime), True
    if note is None:
        return ["state note: none found for %s under %s/*%s" % (name or "this session", temp.replace("\\", "/"),
                                                              FOLDER_SUFFIX)]
    head = "state note%s: %s" % (" (newest, guessed)" if guessed else "", note.replace("\\", "/"))
    return capped([head + " (last %d lines)" % TAIL_LINES] + ["  | " + l for l in _tail(note)])


def hub_pending(key, run, kit=None):
    if not key:
        raise KeyError("this session's sessions key is unknown")
    out = run([sys.executable, kitroot.tool("hub", kit), "pending", "--for", key], timeout=PART_TIMEOUT)
    rows = [l for l in out.splitlines() if l.strip()]
    return capped(["hub pending for %s: %s" % (key, "none" if not rows else "%d" % len(rows))]
                  + ["  " + l for l in rows])


def pm_data_path():
    """The PM data worktree of the machine config, or None: where a repo whose store is the PM data
    branch keeps its records (its own checkout then reads an empty default store)."""
    from kitlib import pmdata
    cfg = pmdata.machine_settings()
    return cfg.get("path") if cfg else None


def backlog_next(repo, session, run, kit=None, store=pm_data_path):
    """The offer, `passed over` and blockers left out. A repo checkout that holds no record at all
    (`BACKLOG empty | 0 records`) is asked again in the PM data worktree (`store()`), where the
    kit's own records are."""
    def ask(where):
        return run([sys.executable, kitroot.tool("backlog", kit), "-C", where, "--session", session or "kit",
                    "next"], timeout=PART_TIMEOUT)
    out = ask(repo)
    if out.startswith("BACKLOG empty | 0 records"):
        where = store()
        if where:
            out = ask(where)
    lines = []
    for l in out.splitlines():
        if l.startswith("  passed over") or l.startswith("  blockers"):
            break     # the offer, not what it skipped
        if l.strip():
            lines.append(l)
    return capped(["backlog next:"] + ["  " + l for l in lines[:NEXT_LINES]])


def worktrees(repo, run, kit=None):
    """The class summary and the A, B, C, E and O rows (L, live or kept, rows are left out)."""
    out = run([sys.executable, kitroot.tool("worktree_remove", kit), "-C", repo, "--classify"],
              timeout=PART_TIMEOUT)
    lines = []
    for l in out.splitlines():
        if l[:2] in ("A ", "B ", "C ", "E ", "O ", "L ") and l[2:3].strip():
            lines.append(l)                       # a class summary line: "C half-done 2 ..."
        elif l[:3] in ("  A", "  B", "  C", "  E", "  O") and l[3:5] == "  ":
            lines.append(l)                       # a tree row
    return capped(["worktrees (--classify, read-only):"] + ["  " + l for l in lines])


def run_all(jobs, deadline=DEADLINE, clock=time.monotonic):
    """[(name, lines)] for `jobs` ([(name, fn)]), each fn run in its own daemon thread; all share one
    `deadline`. A raise, or no answer by the deadline, is one "could not read <name>: ..." line."""
    results = {}

    def work(name, fn):
        try:
            results[name] = fn()
        except subprocess.TimeoutExpired:
            results[name] = ["could not read %s: no answer within %d s" % (name, PART_TIMEOUT)]
        except KeyError as e:
            results[name] = ["could not read %s: %s" % (name, str(e).strip("'\""))]
        except Exception as e:
            results[name] = ["could not read %s: %s: %s" % (name, type(e).__name__, str(e)[:200])]

    threads = [threading.Thread(target=work, args=j, daemon=True) for j in jobs]
    for t in threads:
        t.start()
    end = clock() + deadline
    for t in threads:
        t.join(max(0.0, end - clock()))
    return [(n, list(results.get(n) or ["could not read %s: no answer within %d s" % (n, deadline)]))
            for n, _ in jobs]
