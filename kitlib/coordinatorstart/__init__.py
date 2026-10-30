"""kitlib.coordinatorstart: the coordinator session-start context: standing rules, then live checks.

Pure functions that return data; the hook (boot/hook.py claude SessionStart coordinator) prints it. The text
comes from the "coordinator_start" key of the machine config and is emitted only when the
environment variable PM_KIT_ROLE is "coordinator". After the standing lines come the optional ENG
filing and pricing-pass lines (each switched on in the config), then the live checks (cap,
machine state, other coordinators, the ledger line, the hub key: the session's sessions.json
key, which it posts hub questions --from and reads pending --for once at start (answers come by
push), or "unknown" when no entry is found), run as in kitlib.pmstart: each is a pure function over fakeable inputs
(process list, file reads, a subprocess runner, now, the machine config), and a failed one is one
"could not check X: reason" line. No check reads the rulings ledger: with the split ledger
(`ledger_dir`, docs/ledger.md "Files") the ledger line names the repo's file from the machine
config, and the file is never opened.

Install values come from the config, never from the code: "repos_root" (the folder whose top-level
folders are the repositories, matched against the session's cwd). The landing-lock line reads
the kit's fixed lock file (kitlib.records.landing LOCK_NAME); a "landing_lock" key is ignored.
"""
import datetime
import json
import os
import re
import sys

from kitlib import kitroot, pmstart, sessionmark
from kitlib.config import user as kuser
from kitlib.git import cmd as gitcmd
from kitlib.pmstart import configcheck
from kitlib.coordinatorstart import parts
from kitlib.records import landing

KEY = "coordinator_start"
ROLE_VAR = "PM_KIT_ROLE"
ROLE = "coordinator"
NAME_VAR = "PM_KIT_SESSION_NAME"  # optional: this session's Remote Control name (else the process tree)
TIMEOUT = 10
SOON_HOURS = 4
LOCK_SCHEMA_VERSION = 1       # kitlib.lock.api.SCHEMA_VERSION, the snapshot schema machine() reads
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
                "`python <kit>/tools/backlog.py -C <repo> list --needs-priority`. A follow-up record (filed from a finding on a "
                "branch, a review or a worker's report; a new one says so in its summary) stays unpriced until "
                "you or the PM promote it. Check "
                "every other unpriced open record (an owner story, a record from the PM's brief) for duplicates, "
                "size and fit, then price it 2-5 (`set <ID> --priority <n> "
                "--priority-by agent`). Promote a follow-up at any time it enters a work set: price it 2-5; "
                "price before pull. When `next --all` lists fewer startable records than the set needs, pick the "
                "follow-ups worth doing now, check each for duplicates, size and fit, and price only those going "
                "into this set; never a sweep. `next` never offers an unpriced record; its banner counts it "
                "under `unprioritised+open`, not under `held` (`held` = blocked). "
                "A record that needs the owner (priority 1, a pre-emption, an owner-held area, a question only "
                "the owner can answer) goes to the PM as one question; never ask the owner directly.")


def is_coordinator(env=None):
    """The session mark (ROLE_VAR, read by kitlib.sessionmark: the kit plugin's prefix removed) is
    the coordinator's."""
    return sessionmark.mark(os.environ if env is None else env) == ROLE


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
    cfg = data.get(KEY) if isinstance(data, dict) else None
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
    (pmstart.session_images; on Linux one that runs a host's CLI, pmstart.runs_a_cli) above pid;
    None if unknown."""
    if env.get(NAME_VAR):
        return env[NAME_VAR]
    by_pid = {p.get("ProcessId"): p for p in procs}
    images, clis = pmstart.session_images(), pmstart.linux_clis()
    seen = set()
    while pid in by_pid and pid not in seen:
        seen.add(pid)
        p = by_pid[pid]
        if p.get("Name") in images and pmstart.runs_a_cli(p, clis):
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


def hub_key(sessions, entry):
    """[the `hub key: <key>` line]: the sessions.json key of `entry` (find_entry's pick, the same
    object), the key this coordinator posts hub questions --from and reads pending --for once at
    start; it runs no background wait, as the hub pushes its answers and done to-dos
    (docs/action-hub.md "For the PM"). LookupError when no key holds it."""
    for k, v in sessions.items():
        if v is entry and not str(k).startswith("_"):
            return ["hub key: %s (hub.py questions add --from %s --record REPO:ID|--class C; read "
                    "hub.py pending --for %s once at start; answers come by push)" % (k, k, k)]
    raise LookupError("no sessions.json key holds this session's entry")


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


def landing_lock(entry, run=_run, read=_read, root=None):
    """The landing-lock holder line for the coordinator's repo, or None without a repos_root.

    Read-only: the lock is the file <git common dir>/kit-landing.lock (kitlib.records.landing
    LOCK_NAME), whatever the config says: the old "landing_lock" key is ignored."""
    if not root:
        return None
    repo = "%s/%s" % (root.replace("\\", "/").rstrip("/"), entry.get("repo"))
    common = run(["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()
    try:
        text = read(os.path.join(common, landing.LOCK_NAME))
    except FileNotFoundError:
        return "landing lock (%s): free" % entry.get("repo")
    try:
        r = json.loads(text)
    except ValueError:
        return "landing lock (%s): HELD, record unreadable" % entry.get("repo")
    who = "pid %s" % r["pid"] if r.get("pid") is not None else "a token holder"
    return "landing lock (%s): HELD - branches %s, %s, phase %s, since %s" % (
        entry.get("repo"), " ".join(r.get("branches") or ()) or "-", who,
        r.get("phase"), r.get("started_utc"))


def machine(entry, run=_run, read=_read, now=None, kit=None, root=None):
    """Owner and agent holds in force or starting within 4 h, free gpu and cpu slots, landing lock.

    Runs the kit's own `tools/machine_lock.py status --json` (kitlib.kitroot.lock_tool; `kit`
    names another kit checkout) and reads its snapshot (kitlib.lock.api.snapshot, docs/lock-api.md
    "Snapshot"): `holds` and `slots`. A slot is free when no run holds it and no hold covers it. A
    payload of another schema_version raises ValueError rather than reading as an idle machine."""
    now = now or datetime.datetime.now().astimezone()
    s = json.loads(run([sys.executable, kitroot.lock_tool(kit), "status", "--json"]))
    if s.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ValueError("machine_lock status --json has schema_version %r, this check reads %d"
                         % (s.get("schema_version"), LOCK_SCHEMA_VERSION))
    soon = now + datetime.timedelta(hours=SOON_HOURS)
    owner = []
    for v in s.get("holds") or []:
        if _parse_time(v["start"]) <= soon and _parse_time(v["end"]) > now:
            owner.append("%s hold %s-%s%s" % ("agent" if v.get("owner") == "agent" else "owner",
                                              v.get("start_local", v["start"]), v.get("end_local", v["end"]),
                                              ": " + v["note"] if v.get("note") else ""))
    if s.get("holds_error"):
        owner.append("holds unreadable: %s" % s["holds_error"])
    slots = s.get("slots") or {}
    gpus, cores = slots.get("gpu") or [], slots.get("cpu") or []

    def free(pool):
        return sum(1 for x in pool if not x.get("holder") and not x.get("hold"))
    lines = ["machine: " + ("; ".join(owner) if owner else "no owner or agent hold within %d h" % SOON_HOURS),
             "machine: free gpu slots %d of %d; free cpu slots %d of %d" % (
                 free(gpus), len(gpus), free(cores), len(cores))]
    lock = landing_lock(entry, run, read, root)
    if lock:
        lines.append(lock)
    return lines


def ledger_lines(machine, repo):
    """[the line naming `repo`'s ledger file, if `ledger_dir` is set; then the rulebook line]
    from the machine config `machine` (kitlib.config.user.user_config's). `repo` (the sessions
    file's folder name) is matched against `ledger_repos` lower-cased with every '_' as '-' (so
    My_Tools-Repo is my-tools-repo); a repo not named there gets the folder and the names
    instead. The ledger file is never opened. The rulebook line (pmstart.rulebook_lines, with
    `rules --repo` for `repo`) is always there: it names the rules folder, each rulebook with
    its count, or says there is none."""
    book = pmstart.rulebook_lines(machine, repo)     # machine None: the config cannot be read
    if not machine or not machine.get("ledger_dir"):
        return book
    files = kuser.files_of(machine)
    repo = str(repo or "")
    key = ledger_name(repo)
    if key in files:
        return ["ledger (reference, HOW this repo works): %s" % files[key]] + book
    named = key if key == repo else "%s (from %s)" % (key, repo)
    return ["ledger (reference, HOW each repo works): one file per ledger_repos name in %s (%s); none is named %s"
            % (machine["ledger_dir"], ", ".join(files), named)] + book


def ledger_name(repo):
    """The ledger_repos name a repository folder name `repo` maps to: lower-cased, '_' as '-'."""
    return str(repo or "").lower().replace("_", "-")


def others(entry, procs, env, pid):
    """Other live sessions whose Remote Control name starts with this coordinator's stem."""
    stem = _need(entry, "stem")
    me = own_name(procs, env, pid)
    cand = []
    images, clis = pmstart.session_images(), pmstart.linux_clis()
    for p in procs:
        if (p.get("Name") in images and p.get("CommandLine")
                and pmstart.STORE_DIR not in (p.get("ExecutablePath") or "") and pmstart.runs_a_cli(p, clis)):
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


def context(cfg, env=None, cwd=None, now=None, procs=None, run=_run, read=_read, kit=None,
            pid=None, pm_cfg=None, live=True, machine_cfg=None,
            config_lines=None, run_loose=None, deadline=parts.DEADLINE):
    """The heading, one "- <text>" line per string in cfg["standing"], the ENG filing line (only when cfg["eng_filing"] is true), the pricing-pass line (only when cfg["pricing_pass"] is true), then the live checks (the ledger check prints only with `ledger_dir` set, and nothing when the machine config cannot be read). `machine_cfg`: the machine config (default kitlib.config.user.user_config(), read by the ledger check). `config_lines`: the machine config check, first of the live checks (default kitlib.pmstart.configcheck.lines). Last come the read-only start parts (kitlib.coordinatorstart.parts: state note tail, hub pending, backlog next, stale worktrees), run in parallel under one `deadline`; `run_loose` runs the worktree classify (default: `run` when it was passed, else stdout whatever the exit code)."""
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
                box["s"] = json.loads(read(sf))
                box["e"] = find_entry(box["s"], cwd, name, cfg.get("repos_root"))
                if box["e"][1]:
                    lines.append(box["e"][1])
            return box["e"][0]

        def own_key():
            try:
                entry = entry_once()
            except (LookupError, ValueError) as e:     # no entry: say so, never guess a key
                return ["hub key: unknown (%s); ask the PM for it" % str(e).strip("'\"")]
            if box["e"][1]:     # several entries match and the first was taken: it may not be ours
                return ["hub key: unknown (several sessions entries match this repo); ask the PM for it"]
            return hub_key(box["s"], entry)

        def ledger():
            # A config that cannot be read (user_config raises ConfigError for every such case):
            # only the rulebook line, saying it is unknown. Without ledger_dir the sessions entry
            # only names the repo in the rulebook command: unreadable, the line says `rules --all`
            # rather than fail. Any other error is a bug and is reported below.
            try:
                m = machine_cfg if machine_cfg is not None else kuser.user_config()
            except kuser.ConfigError:
                return pmstart.rulebook_lines(None)
            if not m.get("ledger_dir"):
                try:
                    repo = entry_once()["repo"]
                except (LookupError, ValueError, OSError):
                    repo = None
                return ledger_lines(m, repo)
            return ledger_lines(m, entry_once()["repo"])

        checks = [
            ("machine_config", config_lines or configcheck.lines),
            ("cap", lambda: cap(cfg, entry_once()["repo"], now.date())),
            ("machine state", lambda: machine(entry_once(), run, read, now, kit, cfg.get("repos_root"))),
            ("other coordinators", lambda: others(entry_once(), procs_once(), env, pid)),
            ("ledger", ledger),
            ("hub key", own_key),
        ]
        for name, fn in checks:
            try:
                lines.extend(fn())
            except KeyError as e:
                lines.append("could not check %s: %s" % (name, str(e).strip("'\"")))
            except Exception as e:
                lines.append("could not check %s: %s: %s" % (name, type(e).__name__, e))
        lines.extend(_start_parts(cfg, box, entry_once, env, run, run_loose, kit, deadline))
    return "Coordinator session start:\n" + "\n".join("- " + l for l in lines)


def _start_parts(cfg, box, entry_once, env, run, run_loose, kit, deadline):
    """The lines of the read-only start parts. The sessions entry is read once here (the checks
    above cached it or its failure), then the parts run in threads."""
    try:
        entry = entry_once()
    except Exception:
        entry = None
    key = None
    if entry is not None and "s" in box and not box["e"][1]:
        key = next((k for k, v in box["s"].items() if v is entry and not str(k).startswith("_")), None)
    if run_loose is None:
        run_loose = parts.run_loose if run is _run else run

    def need():
        if entry is None:
            raise KeyError("no sessions entry for this repo")
        return "%s/%s" % ((cfg.get("repos_root") or "").replace("\\", "/").rstrip("/"), entry.get("repo"))

    slugs = [s for s in (ledger_name(entry.get("repo")) if entry else None, key,
                         str(entry.get("stem") or "")[:-len(parts.FOLDER_SUFFIX)]
                         if entry and str(entry.get("stem") or "").endswith(parts.FOLDER_SUFFIX) else None) if s]
    jobs = [("state note", lambda: parts.state_note(env, env.get(NAME_VAR), slugs)),
            ("hub pending", lambda: parts.hub_pending(key, run, kit)),
            ("backlog next", lambda: parts.backlog_next(need(), key, run, kit)),
            ("worktrees", lambda: parts.worktrees(need(), run_loose, kit))]
    out = []
    for _, lines in parts.run_all(jobs, deadline):
        out.extend(lines)
    return out


def render(env=None, path=None, **kw):
    """The additionalContext text, or None when the role is not coordinator (print nothing)."""
    if not is_coordinator(env):
        return None
    try:
        cfg = load_config(path)
        if cfg is None:
            text = "Coordinator session start: no coordinator_start config in %s; skipped." % (path or kuser.display())
            # the machine config lines still show (kitlib.pmstart.configcheck, as at the PM part)
            return configcheck.appended(text, kw.get("config_lines"))
        return context(cfg, env=env, **kw)
    except Exception as e:
        return "Coordinator session start: could not check config: %s: %s" % (type(e).__name__, e)
