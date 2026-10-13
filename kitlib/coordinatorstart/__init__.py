"""kitlib.coordinatorstart: the coordinator session-start context: standing rules, then live checks.

Pure functions that return data; the hook (boot/hook.py claude SessionStart coordinator) prints it. The text
comes from the "coordinator_start" key of the machine config and is emitted only when the
environment variable PM_KIT_ROLE is "coordinator". After the standing lines come the optional ENG
filing and pricing-pass lines (each switched on in the config), then the live checks (rulings, cap,
machine state, other coordinators, and stubs: a line only when the repo's stubs are not current),
run as in kitlib.pmstart: each is a pure function over fakeable inputs (process list, file reads,
a subprocess runner, now), and a failed one is one "could not check X: reason" line.

Install values come from the config, never from the code: "repos_root" (the folder whose top-level
folders are the repositories, matched against the session's cwd), "ledger" (else the machine
config's ledger_path) and "landing_lock" ({repo: the landing lock's file name in that repo's git
common dir}; a repo not named there gets no landing-lock line).
"""
import datetime
import json
import os
import re
import sys

from kitlib import pmstart, sessionmark, stubs
from kitlib.config import user as kuser
from kitlib.git import cmd as gitcmd
from kitlib.host import registry
from kitlib.ledger import safepath

KEY = "coordinator_start"
ROLE_VAR = "PM_KIT_ROLE"
ROLE = "coordinator"
LEGACY_KEY = "conductor_start"   # the old names, read until the live config and launcher are renamed
LEGACY_ROLE = "conductor"
NAME_VAR = "PM_KIT_SESSION_NAME"  # optional: this session's Remote Control name (else the process tree)
TIMEOUT = 10
LEDGER_CAP = 20
DECIDES_CHARS = 200
SOON_HOURS = 4
# Engineering decisions (docs/engineering-rulings.md): the filing format, the consultant as
# recipient, and the owner-only list.
ENG_LINE = ("ENG rulings: an engineering question (API shape, class versus record, signatures, module layout, "
            "test strategy, which of several fixes, scope splits) goes to consultant-<repo> in one SendMessage (to the PM "
            "when no consultant is running), then you keep working: "
            "ENG <repo> <item>: <question> | options: a) .. b) .. | rec: <x> | why: <one line>. "
            "Owner-only, never an ENG (send it to the PM): anything that changes or supersedes an owner ruling; Realism and game feel "
            "(what a player or bot may see or do, timings, detection risk); Security (permissions, settings, the "
            "classifier, credentials, hooks that guard owner files); Money and accounts (purchases, cloud credit, "
            "game accounts, bans); Priority and pre-empts (one owner entry per change); Go-live and live-client "
            "runs, and removals of owner files; anything the owner flagged for review (eng_review).")
# When a coordinator prices a record (hosts/claude/shell/agents/coordinator.md, On start step 6 and Work sets,
# Follow-ups): the start-up pricing pass leaves follow-up records unpriced.
# Emitted only when coordinator_start.pricing_pass is true.
PRICING_LINE = ("Pricing pass and backlog audit before your first dispatch, follow-up records excepted: run "
                "`python tools/backlog.py list --needs-priority`. A follow-up record (filed from a finding on a "
                "branch, a review or a worker's report; a new one says so in its summary) stays unpriced until "
                "you or the PM promote it. Check "
                "every other unpriced open record (an owner story, a record from the PM's brief) for duplicates, "
                "size and fit, then price it 2-5 under the standing valuation rules (`set <ID> --priority <n> "
                "--priority-by agent`). Promote a follow-up at any time it enters a work set: price it 2-5; "
                "price before pull. When `next --all` lists fewer startable records than the set needs, pick the "
                "follow-ups worth doing now, check each for duplicates, size and fit, and price only those going "
                "into this set; never a sweep. `next` never offers an unpriced record; its banner counts it "
                "under `unprioritised+open`, not under `held` (`held` = blocked). "
                "A record that needs the owner (priority 1, a pre-emption, an owner-held area, a question only "
                "he can answer) goes to the PM as one question; never ask him directly.")


def is_coordinator(env=None):
    """The session mark (ROLE_VAR, read by kitlib.sessionmark: the kit plugin's prefix removed) is
    the coordinator's or its old name."""
    return sessionmark.mark(os.environ if env is None else env) in (ROLE, LEGACY_ROLE)


def load_config(path=None):
    """The "coordinator_start" dict, or None when the file or key is missing. Bad JSON raises
    ValueError; with no `path`, a machine config location that cannot be told
    (kitlib.config.user.locate BAD) raises kitlib.config.ConfigError."""
    p = os.path.expanduser(path) if path else kuser.readable_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError:
        return None
    cfg = None
    if isinstance(data, dict):
        cfg = data.get(KEY)
        if not isinstance(cfg, dict):
            cfg = data.get(LEGACY_KEY)
    return cfg if isinstance(cfg, dict) else None


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _run(args, timeout=TIMEOUT):
    r = gitcmd.subprocess_run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("%s exit %d: %s" % (os.path.basename(args[0]), r.returncode, r.stderr.strip()[:200]))
    return r.stdout


def _need(cfg, key):
    if cfg.get(key) is None:
        raise KeyError("%s not configured" % key)
    return cfg[key]


def _parse_time(text):
    """An ISO time as an aware datetime; a 7-digit fraction (PowerShell) is cut to 6."""
    t = re.sub(r"(\.\d{6})\d+", r"\1", str(text))
    d = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()


def own_name(procs, env, pid):
    """This session's Remote Control name: PM_KIT_SESSION_NAME, else the nearest session process
    (pmstart.session_images) above pid; None if unknown."""
    if env.get(NAME_VAR):
        return env[NAME_VAR]
    by_pid = {p.get("ProcessId"): p for p in procs}
    images = pmstart.session_images()
    seen = set()
    while pid in by_pid and pid not in seen:
        seen.add(pid)
        p = by_pid[pid]
        if p.get("Name") in images:
            n = pmstart.rc_name(p.get("CommandLine"))
            if n:
                return n
        pid = p.get("ParentProcessId")
    return None


def find_entry(sessions, cwd, name=None, root=None):
    """(entry, note): the agent=true sessions entry whose repo is cwd's top folder under root (the
    config's repos_root; KeyError when it is not configured)."""
    if not root:
        raise KeyError("repos_root not configured")
    root = root.replace("\\", "/").rstrip("/")
    c = cwd.replace("\\", "/").rstrip("/")
    pre = root.lower() + "/"
    if not (c.lower() + "/").startswith(pre):
        raise ValueError("cwd %s is not under %s" % (cwd, root))
    top = c[len(pre):].split("/")[0].lower()
    hits = [e for e in sessions.values()
            if isinstance(e, dict) and e.get("agent") is True and str(e.get("repo", "")).lower() == top]
    if not hits:
        raise LookupError("no agent=true sessions entry for repo folder %s" % top)
    if len(hits) == 1:
        return hits[0], None
    for e in hits:
        if name and (e.get("name") == name or name.startswith(str(e.get("stem") or "\0"))):
            return e, None
    return hits[0], "%d sessions entries match repo %s; took the first (%s)" % (len(hits), top, hits[0].get("name"))


def rulings(entry, ledger_path, read=_read):
    """Ledger entries newer than the coordinator's state note's highest R-id, for its repo or all.
    A ledger path the ledger's path checks refuse (kitlib.ledger.safepath) is one line saying why."""
    note = _need(entry, "state")
    ids = [int(m) for m in re.findall(r"R-(\d{4})", read(note))]
    if not ids:
        return ["no R-id in your state note %s: cannot tell which rulings are new" % note]
    top, repo = max(ids), str(entry.get("repo", "")).lower()
    why = safepath.refusal(ledger_path)
    if why is not None:
        return [why]
    found, cur = [], None
    for line in read(ledger_path).splitlines():
        m = re.match(r"## R-(\d{4}) \| [^|]*\| ([^|]*) \|", line)
        if m:
            cur = None
            if int(m.group(1)) > top and m.group(2).strip().lower() in (repo, "all"):
                cur = ["R-" + m.group(1), None, None]
                found.append(cur)
        elif cur is not None:
            if line.startswith("Decides:"):
                cur[1] = line[8:].strip()
            elif cur[2] is None and line.strip():
                cur[2] = line.strip()
    if not found:
        return ["no rulings newer than R-%04d for %s or all" % (top, repo)]
    lines = ["%s: %s" % (r[0], (r[1] or r[2] or "(no Decides)")[:DECIDES_CHARS]) for r in found]
    if len(lines) > LEDGER_CAP:
        more = len(lines) - LEDGER_CAP
        lines = lines[:LEDGER_CAP] + ["... and %d more newer rulings: read %s" % (more, ledger_path)]
    return ["rulings newer than R-%04d (your state note) for %s or all:" % (top, repo)] + lines


def ledger_file(cfg, home=None):
    """The config's "ledger", else the machine config's ledger_path; KeyError when neither is set."""
    path = cfg.get("ledger") or kuser.ledger_path(home)
    if not path:
        raise KeyError("ledger not configured")
    return path


def cap(cfg, repo, today):
    caps = _need(cfg, "cap").get(repo) or _need(cfg, "cap").get(repo.lower())
    if not caps:
        raise KeyError("cap for %s not configured" % repo)
    for i, c in enumerate(caps):
        if not c.get("until") or c["until"] >= today.isoformat():
            if c.get("until") and i + 1 < len(caps):
                return ["agent cap %s until %s, then %s" % (c["cap"], c["until"], caps[i + 1]["cap"])]
            return ["agent cap %s%s" % (c["cap"], " until %s" % c["until"] if c.get("until") else "")]
    return ["agent cap: every configured period for %s has ended; ask the PM" % repo]


def lock_file_for(cfg, repo):
    """The landing lock's file name the config's "landing_lock" gives repo, or None."""
    locks = (cfg or {}).get("landing_lock") or {}
    return locks.get(repo) or locks.get(str(repo).lower())


def landing_lock(entry, run=_run, read=_read, root=None, lock_file=None):
    """The landing-lock holder line for the coordinator's repo, or None when no lock file is
    configured for it (or no repos_root).

    Read-only: the lock is the file <git common dir>/<lock_file>."""
    if not (root and lock_file):
        return None
    repo = "%s/%s" % (root.replace("\\", "/").rstrip("/"), entry.get("repo"))
    common = run(["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()
    try:
        text = read(os.path.join(common, lock_file))
    except FileNotFoundError:
        return "landing lock (%s): free" % entry.get("repo")
    try:
        r = json.loads(text)
    except ValueError:
        return "landing lock (%s): HELD, record unreadable" % entry.get("repo")
    who = "pid %s" % r["pid"] if r.get("pid") is not None else "a token holder"
    return "landing lock (%s): HELD - lane %s, branches %s, %s, phase %s, since %s" % (
        entry.get("repo"), r.get("lane"), " ".join(r.get("branches") or ()) or "-", who,
        r.get("phase"), r.get("started_utc"))


def machine(entry, run=_run, read=_read, now=None, home=None, root=None, lock_file=None):
    """Owner reservations and bookings in force or starting within 4 h, free gpu and cpu slots, landing lock."""
    home = home or kuser.home_dir()
    now = now or datetime.datetime.now().astimezone()
    s = json.loads(run([sys.executable, registry.state_path(home, "tools", "machine_lock", "machine_lock.py"),
                        "status", "--json"]))
    soon = now + datetime.timedelta(hours=SOON_HOURS)
    res = s.get("reservations") or {}
    owner = []
    for kind, items in (("owner reservation", (res.get("active") or []) + (res.get("upcoming") or [])),
                        ("booking", (s.get("lock") or {}).get("bookings") or [])):
        for v in items:
            if _parse_time(v["start"]) <= soon and _parse_time(v["end"]) > now:
                owner.append("%s %s-%s%s" % (kind, v.get("start_local", v["start"]), v.get("end_local", v["end"]),
                                              ": " + v["note"] if v.get("note") else ""))
    gpus, cores = s.get("gpus") or [], s.get("cores") or []
    lines = ["machine: " + ("; ".join(owner) if owner else "no owner reservation or booking within %d h" % SOON_HOURS),
             "machine: free gpu slots %d of %d; free cpu slots %d of %d" % (
                 sum(1 for g in gpus if not g.get("holder_id")), len(gpus),
                 sum(1 for c in cores if not c.get("holder_id")), len(cores))]
    lock = landing_lock(entry, run, read, root, lock_file)
    if lock:
        lines.append(lock)
    return lines


def stubs_line(entry, root=None, check=stubs.problem_line):
    """[the stubs line] for the coordinator's repo (repos_root/<repo>), [] when its stubs are current.
    KeyError without repos_root; a missing folder is a could-not-check line, not an error."""
    if not root:
        raise KeyError("repos_root not configured")
    repo = os.path.join(root, str(entry.get("repo")))
    if not os.path.isdir(repo):
        return ["could not check stubs: no repository folder %s" % repo]
    line = check(repo)
    return [line] if line else []


def others(entry, procs, env, pid):
    """Other live sessions whose Remote Control name starts with this coordinator's stem."""
    stem = _need(entry, "stem")
    me = own_name(procs, env, pid)
    cand = []
    images = pmstart.session_images()
    for p in procs:
        if (p.get("Name") in images and p.get("CommandLine")
                and pmstart.STORE_DIR not in (p.get("ExecutablePath") or "")):
            n = pmstart.rc_name(p["CommandLine"])
            if n and n.startswith(stem):
                cand.append((p.get("Created") or "", n))
    if me is None and cand:
        me = max(cand)[1]  # unknown: the newest is this one
    names = sorted({n for _, n in cand if n != me})
    if not names:
        return []
    return ["also running: %s; if one is your winding-down predecessor that is expected, "
            "otherwise stop and tell the PM" % ", ".join(names)]


def context(cfg, env=None, cwd=None, now=None, procs=None, run=_run, read=_read, home=None,
            pid=None, pm_cfg=None, live=True, stubs_check=stubs.problem_line):
    """The heading, one "- <text>" line per string in cfg["standing"], the ENG filing line (only when cfg["eng_filing"] is true), the pricing-pass line (only when cfg["pricing_pass"] is true), then the live checks (the stubs check prints only when the repo's stubs are missing or stale)."""
    env = os.environ if env is None else env
    lines = [str(t) for t in cfg.get("standing") or []]
    if cfg.get("eng_filing") is True:
        lines.append(ENG_LINE)
    if cfg.get("pricing_pass") is True:
        lines.append(PRICING_LINE)
    if live:
        now = now or datetime.datetime.now().astimezone()
        cwd = cwd or os.getcwd()
        pid = pid if pid is not None else os.getppid()
        box = {}

        def procs_once():
            if "p" not in box:
                box["p"] = procs if procs is not None else pmstart.list_processes()
            return box["p"]

        def entry_once():
            if "e" not in box:
                sf = (pm_cfg if pm_cfg is not None else pmstart.load_config() or {}).get("sessions_file")
                if not sf:
                    raise KeyError("pm_start.sessions_file not configured")
                try:
                    name = own_name(procs_once(), env, pid)
                except Exception:
                    name = None
                box["e"] = find_entry(json.loads(read(sf)), cwd, name, cfg.get("repos_root"))
                if box["e"][1]:
                    lines.append(box["e"][1])
            return box["e"][0]

        checks = [
            ("rulings", lambda: rulings(entry_once(), ledger_file(cfg, home), read)),
            ("cap", lambda: cap(cfg, entry_once()["repo"], now.date())),
            ("machine state", lambda: machine(entry_once(), run, read, now, home, cfg.get("repos_root"),
                                              lock_file_for(cfg, entry_once().get("repo")))),
            ("other coordinators", lambda: others(entry_once(), procs_once(), env, pid)),
            ("stubs", lambda: stubs_line(entry_once(), cfg.get("repos_root"), stubs_check)),
        ]
        for name, fn in checks:
            try:
                lines.extend(fn())
            except KeyError as e:
                lines.append("could not check %s: %s" % (name, str(e).strip("'\"")))
            except Exception as e:
                lines.append("could not check %s: %s: %s" % (name, type(e).__name__, e))
    return "Coordinator session start:\n" + "\n".join("- " + l for l in lines)


def render(env=None, path=None, **kw):
    """The additionalContext text, or None when the role is not coordinator (print nothing)."""
    if not is_coordinator(env):
        return None
    try:
        cfg = load_config(path)
        if cfg is None:
            return "Coordinator session start: no coordinator_start config in %s; skipped." % (path or kuser.display())
        return context(cfg, env=env, **kw)
    except Exception as e:
        return "Coordinator session start: could not check config: %s: %s" % (type(e).__name__, e)
