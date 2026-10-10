"""procwatch: what is running on this machine, who started it, and what was left behind.

    python procwatch.py                 report (the default; changes nothing)
    python procwatch.py --json          the full classification as JSON
    python procwatch.py --log [FILE]    also append a one-line summary to FILE (for a scheduled run);
                                        no FILE = ~/.claude/logs/procwatch.log (folder made); a FILE
                                        inside this tool's folder is moved there too
    python procwatch.py --kill          ALSO terminate the kill-allowlisted suspects (see RULES)
    python procwatch.py --kill --only msys-follow,msbuild-node   narrow the kill to these rules

Every process is put in one group by image + command-line pattern, with its parent chain,
whether its parent is alive (a parent pid that now belongs to a younger process counts as
dead), age, CPU (sampled over --sample seconds), memory and handles. A group is EXPECTED or
SUSPECT; a suspect row says why. Git-Bash programs are judged by their MSYS parent (Git's
`ps -l`), because their Windows parent is a fork that normally exits at once; without Git's ps
they are never called orphans.

--kill touches a process only when ALL hold: its rule is kill-allowlisted (RULES kill=True), it
is a suspect under that rule (which always includes: its parent is dead and it is older than the
rule's age floor; idle where the rule says so), it is not protected, and its creation time is
re-checked immediately before TerminateProcess. Protected (never touched, listed as expected):
PROTECT_PATTERNS - ComfyUI plus any regexes in the PROCWATCH_PROTECT environment variable
(';'-separated, matched against the command line), e.g. a repo's live production process.

Exit: 0 no suspects, 1 suspects reported, 2 error. Standard library only; Windows only.
"""
import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MIN = 60
HOUR = 3600
PAGED_POOL_WARN = 4 * 2**30      # kernel paged pool; ~1-2 GB is normal here, the find-/ leak reached ~15 GB

# Never killed and never a suspect: production and the owner's generators.
PROTECT_PATTERNS = [
    r"\\comfyui\\", r"(^|\\)comfy desktop\.exe",
] + [x for x in os.environ.get("PROCWATCH_PROTECT", "").split(";") if x]

MSYS_FILTERS = {"grep.exe", "egrep.exe", "sed.exe", "awk.exe", "gawk.exe", "cut.exe", "tee.exe",
                "head.exe", "wc.exe", "sort.exe", "uniq.exe", "tr.exe", "cat.exe", "xargs.exe"}
ROOT_SEARCH = re.compile(
    r"""(?ix)
    (^|[\s"'\\/])find(\.exe)?"?\s+(-\w+\s+)*["']?(/|/c/?|[a-z]:[\\/]?)["']?(\s|$)   # find / , find /c , find C:\
    | \bdir\b[^|&]*\s/s\b[^|&]*\s[a-z]:\\?(\s|"|$)                                  # dir /s C:\
    | \b(get-childitem|gci|ls)\b[^|;]*\s(-path\s+)?["']?[a-z]:\\?["']?\s[^|;]*-r(ecurse)?\b
    | \bwhere(\.exe)?\s+/r\s+[a-z]:\\?(\s|$)
    """)


def _name(p):
    return (p.get("name") or "").lower()


def _cmd(p):
    return (p.get("cmd") or "").lower()


def _in_git_usr_bin(p):
    return "\\git\\usr\\bin\\" in (p.get("image") or "").lower()


def _msys_image(p):
    return _in_git_usr_bin(p)


def _dead_parent_older(p, floor):
    return (not p["parent_alive"]) and (p["age"] or 0) >= floor


def _idle(p):
    return p.get("cpu_pct") is not None and p["cpu_pct"] < 1.0


# Each rule: id, what it is, match(p), suspect(p, ctx) -> reason or None, kill allowlisted?
# First matching rule wins, so the specific ones come first.
def _r_root_search(p, ctx):
    if p["parent_alive"]:
        return "search from a drive root (runaway; its tool shell is still alive)"
    return "search from a drive root, parent dead (orphaned by a tool timeout)" if (p["age"] or 0) >= 2 * MIN else None


def _r_follow(p, ctx):
    return "tail -f with a dead parent: a stopped Monitor/background pipeline" if _dead_parent_older(p, 10 * MIN) else None


def _r_filter(p, ctx):
    return "pipeline filter with a dead parent (left by a stopped Monitor/timeout)" if _dead_parent_older(p, 10 * MIN) else None


def _r_sleep(p, ctx):
    return "sleep with a dead parent (loop remnant)" if _dead_parent_older(p, 10 * MIN) else None


def _r_msys_shell(p, ctx):
    return "Git-Bash shell with a dead parent for 30+ min" if _dead_parent_older(p, 30 * MIN) else None


def _r_msbuild_node(p, ctx):
    reuse = "/nodereuse:true" in _cmd(p)
    if not p["parent_alive"] and _idle(p) and (p["age"] or 0) >= (15 * MIN if reuse else 5 * MIN):
        return ("idle MSBuild node kept by node reuse (turning node reuse off stops this)" if reuse
                else "idle MSBuild node whose build is gone")
    return None


def _r_roslyn(p, ctx):
    n = ctx["count"]["roslyn-server"]
    return f"{n} compiler servers running (one per SDK is normal)" if n > 2 else None


def _r_gradle_daemon(p, ctx):
    n = ctx["count"]["gradle-daemon"]
    if n > 3:
        return f"{n} Gradle daemons (JVM-arg/version mismatch spawns duplicates); stop with gradlew --stop"
    if _idle(p) and (p["age"] or 0) >= 3 * HOUR:
        return "Gradle daemon idle and older than 3 h"
    return None


def _r_kotlin_daemon(p, ctx):
    return "Kotlin daemon idle for 2+ h" if _idle(p) and (p["age"] or 0) >= 2 * HOUR else None


def _r_gradle_client(p, ctx):
    return "Gradle client with a dead parent for 30+ min" if _dead_parent_older(p, 30 * MIN) else None


def _r_testhost(p, ctx):
    return "test host whose runner is gone" if _dead_parent_older(p, 30 * MIN) else None


def _r_godot_headless(p, ctx):
    return "headless engine with a dead parent (the lock's orphan check owns these)" if _dead_parent_older(p, 30 * MIN) else None


def _r_agent_script(p, ctx):
    return "agent background script whose session is gone" if _dead_parent_older(p, 60 * MIN) else None


def _r_conhost(p, ctx):
    return "console host with a dead parent" if _dead_parent_older(p, 30 * MIN) else None


# Groups --kill may end even while the parent (the agent's tool shell) is alive, past this age: a search from a
# drive root is never legitimate, and a drive-root search with a live shell can leak GB of paged pool
# (observed 2026-09-24).
LIVE_PARENT_KILL = {"root-search": 10 * MIN}


RULES = [
    # id, label, match, suspect, kill
    ("root-search", "recursive search from a drive root",
     lambda p: bool(ROOT_SEARCH.search(p.get("cmd") or "")) and _name(p) in
     {"find.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "where.exe"}, _r_root_search, True),
    ("msys-follow", "tail -f (Monitor / background watch)",
     lambda p: _name(p) == "tail.exe" and re.search(r"\s(-\w*f|--follow|-F)\b", p.get("cmd") or ""),
     _r_follow, True),
    ("msys-filter", "Git-Bash pipeline filter (grep/sed/awk...)",
     lambda p: _name(p) in MSYS_FILTERS and _in_git_usr_bin(p), _r_filter, True),
    ("msys-sleep", "Git-Bash sleep", lambda p: _name(p) == "sleep.exe" and _in_git_usr_bin(p), _r_sleep, True),
    ("msys-shell", "Git-Bash shell (tool shell / subshell)",
     lambda p: _name(p) in {"bash.exe", "sh.exe"}, _r_msys_shell, False),
    ("msbuild-node", "MSBuild worker node",
     lambda p: _name(p) in {"dotnet.exe", "msbuild.exe"} and "/nodemode:" in _cmd(p), _r_msbuild_node, True),
    ("roslyn-server", "C# compiler server (VBCSCompiler)",
     lambda p: _name(p) == "vbcscompiler.exe" or "vbcscompiler.dll" in _cmd(p), _r_roslyn, False),
    ("gradle-daemon", "Gradle daemon", lambda p: "gradledaemon" in _cmd(p), _r_gradle_daemon, False),
    ("kotlin-daemon", "Kotlin compile daemon", lambda p: "kotlincompiledaemon" in _cmd(p), _r_kotlin_daemon, False),
    ("test-host", "test host (dotnet testhost / Gradle test executor)",
     lambda p: _name(p) == "testhost.exe" or "testhost.dll" in _cmd(p) or "gradle test executor" in _cmd(p),
     _r_testhost, True),
    ("gradle-client", "Gradle client (gradlew)",
     lambda p: "gradle-wrapper.jar" in _cmd(p) or "org.gradle.launcher.gradlemain" in _cmd(p),
     _r_gradle_client, False),
    ("godot-headless", "headless Godot run",
     lambda p: _name(p).startswith("godot") and "--headless" in _cmd(p), _r_godot_headless, False),
    ("agent-script", "agent background script (python/node from a .claude path)",
     lambda p: _name(p) in {"python.exe", "pythonw.exe", "node.exe"} and
     ("\\.claude" in _cmd(p) or "/.claude" in _cmd(p) or "\\temp\\claude\\" in _cmd(p)
      or "\\agenttemp\\claude\\" in _cmd(p)),             # agents' TEMP moved off the system drive
     _r_agent_script, False),
    ("conhost", "console host", lambda p: _name(p) == "conhost.exe", _r_conhost, False),
]
RULE_IDS = [r[0] for r in RULES]


def protected(p):
    s = ((p.get("image") or "") + " " + (p.get("cmd") or "")).lower()
    return any(re.search(x, s) for x in PROTECT_PATTERNS)


def origin(p, by_pid):
    """Who started it: the nearest recognisable ancestor, and whether the chain is broken."""
    q, hops = p, 0
    while q["parent_alive"] and hops < 30 and q.get("eparent") in by_pid:
        q = by_pid[q["eparent"]]
        n = _name(q)
        if n == "claude.exe":
            c = _cmd(q)
            if "--type=" in c or "\\windowsapps\\claude_" in (q.get("image") or "").lower():
                return "Claude desktop app"
            if "--agent coordinator" in c or "--agent conductor" in c:
                m = re.search(r"-n\s+(\S+)", q.get("cmd") or "")
                return "coordinator claude.exe " + (m.group(1) if m else str(q["pid"]))
            return f"claude.exe {q['pid']} session"
        if n in {"code.exe", "explorer.exe", "services.exe", "svchost.exe", "steam.exe",
                 "discord.exe", "comfy desktop.exe", "windowsterminal.exe"}:
            return n
        hops += 1
    return "orphan" if not p["parent_alive"] else f"{q['name']} {q['pid']} (its parent is gone)"


def classify(procs, cpu_window_s):
    """procs: list of dicts with pid, ppid, name, image, cmd, ctime, age (s), cpu_pct, ws,
    priv, handles, session. Adds parent_alive, group, verdict, reason, kill_ok, origin."""
    by_pid = {p["pid"]: p for p in procs}
    msys_win = {p["mpid"]: p["pid"] for p in procs if p.get("mpid") is not None}
    for p in procs:
        mpp = p.get("mppid")
        if mpp is not None and mpp != 1:
            # Git-Bash program: its Windows parent is a fork that has usually exited, so the
            # MSYS parent decides (MSYS parent 1 = started by a Windows program: use Windows).
            p["eparent"] = msys_win.get(mpp)
            p["parent_alive"] = p["eparent"] is not None
            continue
        par = by_pid.get(p["ppid"])
        p["eparent"] = p["ppid"]
        p["parent_alive"] = bool(par) and p["ppid"] != 0 and (
            par["ctime"] is None or p["ctime"] is None or par["ctime"] <= p["ctime"])
        if _msys_image(p) and p.get("mpid") is None:
            p["parent_alive"] = True        # no MSYS table: a dead Windows parent proves nothing
            p["msys_unknown"] = True
    count = defaultdict(int)
    for p in procs:
        for rid, _l, match, _s, _k in RULES:
            if match(p):
                p["_rule"] = rid
                count[rid] += 1
                break
    ctx = {"count": count, "by_pid": by_pid}
    rules = {r[0]: r for r in RULES}
    for p in procs:
        p["origin"] = origin(p, by_pid)
        p["kill_ok"] = False
        rid = p.pop("_rule", None)
        if protected(p):
            p["group"], p["verdict"], p["reason"] = "protected: live brain / ComfyUI", "EXPECTED", ""
            continue
        if rid:
            _i, label, _m, suspect, kill = rules[rid]
            reason = suspect(p, ctx)
            p["group"], p["rule"] = f"{rid}: {label}", rid
            p["verdict"] = "SUSPECT" if reason else "EXPECTED"
            p["reason"] = reason or ""
            live_floor = LIVE_PARENT_KILL.get(rid)
            p["kill_ok"] = bool(reason and kill and (not p["parent_alive"] or (
                live_floor is not None and (p["age"] or 0) >= live_floor)))
            continue
        p["rule"] = None
        p["verdict"], p["reason"] = "EXPECTED", ""
        n = _name(p)
        if n == "claude.exe":
            p["group"] = "claude sessions / desktop app"
        elif p["origin"].startswith(("coordinator", "claude.exe")):
            p["group"] = "agent tool commands (parent alive)"
        elif p.get("session") == 0 or (p.get("image") or "").lower().startswith("c:\\windows\\") \
                or p.get("image") is None:
            p["group"] = "Windows / services"
        else:
            p["group"] = "owner apps"
    _flag_cpu(procs)
    return procs


BURNER_MIN = 8          # same image under one parent, each busy: a deliberate load test or a runaway
BUSY_PCT = 50.0         # % of one core over the sample window
RUNAWAY_AGE = 30 * MIN
DEV_IMAGES = {"bash.exe", "sh.exe", "python.exe", "pythonw.exe", "node.exe", "java.exe", "javaw.exe",
              "dotnet.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "find.exe"}


def _flag_cpu(procs):
    """Report-only CPU checks on processes an agent started (or orphans): a burner signature
    (BURNER_MIN+ busy siblings of one image) and single long-running busy processes. Build
    servers, daemons, engines and owner apps are left to their own rules."""
    def agentish(p):
        started = p["origin"].startswith(("coordinator", "claude.exe")) or (
            p["origin"] == "orphan" and _name(p) in DEV_IMAGES)
        return (started and not protected(p)
                and _name(p) != "claude.exe" and p["verdict"] == "EXPECTED"
                and p.get("rule") in (None, "msys-shell", "agent-script"))
    sib = defaultdict(list)
    for p in procs:
        if agentish(p) and (p["cpu_pct"] or 0) >= BUSY_PCT:
            sib[(p["ppid"], _name(p))].append(p)
    for (_pp, name), ps in sib.items():
        if len(ps) >= BURNER_MIN:
            for p in ps:
                p["group"], p["rule"], p["verdict"] = "cpu-burner: many busy siblings", "cpu-burner", "SUSPECT"
                p["reason"] = (f"{len(ps)} busy {name} under one parent: load test must hold the machine "
                               f"lock and be announced")
    for p in procs:
        if agentish(p) and p["verdict"] == "EXPECTED" and (p["cpu_pct"] or 0) >= BUSY_PCT \
                and (p["age"] or 0) >= RUNAWAY_AGE:
            p["group"], p["rule"], p["verdict"] = "cpu-runaway: busy agent process", "cpu-runaway", "SUSPECT"
            p["reason"] = f"agent-started process busy for {fmt_age(p['age'])} (check it is meant to be)"


def collect(sample_s=3.0):
    import winproc as W
    snap = W.snapshot()
    first = W.cpu_sample([pid for pid, _pp, _n in snap])
    W.sleep(sample_s)
    now = W.filetime_now()
    snap = W.snapshot()
    msys = W.msys_table() or {}      # after the snapshot, so every Git-Bash process in it is listed
    procs = []
    for pid, ppid, name in snap:
        if pid == 0:
            continue
        d = W.details(pid)
        age = (now - d["ctime"]) / 1e7 if d["ctime"] else None
        cpu_pct = None
        f = first.get(pid)
        if f and d["cpu"] is not None and f[0] == d["ctime"]:
            cpu_pct = round(100.0 * (d["cpu"] - f[1]) / sample_s, 1)
        procs.append({"pid": pid, "ppid": ppid, "name": name, "image": d["image"], "cmd": d["cmd"],
                      "ctime": d["ctime"], "age": age, "cpu_s": d["cpu"], "cpu_pct": cpu_pct,
                      "ws": d["ws"], "priv": d["priv"], "handles": d["handles"],
                      "session": None if d["image"] else 0,
                      "mpid": msys.get(pid, (None, None))[0], "mppid": msys.get(pid, (None, None))[1]})
    return procs


def fmt_age(s):
    if s is None:
        return "?"
    return f"{s / 60:.0f}m" if s < 2 * HOUR else f"{s / 3600:.1f}h"


def summarize(procs):
    groups = defaultdict(list)
    for p in procs:
        groups[(p["verdict"], p["group"])].append(p)
    rows = []
    for (verdict, group), ps in groups.items():
        rows.append({
            "verdict": verdict, "group": group, "count": len(ps),
            "orphans": sum(1 for p in ps if not p["parent_alive"]),
            "oldest": max((p["age"] or 0) for p in ps),
            "ws_mb": sum((p["ws"] or 0) for p in ps) // 2**20,
            "handles": sum((p["handles"] or 0) for p in ps),
            "cpu_pct": round(sum((p["cpu_pct"] or 0) for p in ps), 1),
            "kill_ok": sum(1 for p in ps if p["kill_ok"]),
            "reasons": sorted({p["reason"] for p in ps if p["reason"]}),
            "pids": [p["pid"] for p in sorted(ps, key=lambda x: -(x["age"] or 0))],
        })
    rows.sort(key=lambda r: (r["verdict"] != "SUSPECT", -r["count"]))
    return rows


def print_report(rows, procs, out=sys.stdout):
    sus = [r for r in rows if r["verdict"] == "SUSPECT"]
    print(f"procwatch {datetime.datetime.now():%Y-%m-%d %H:%M}: {len(procs)} processes, "
          f"{sum(r['count'] for r in sus)} suspect in {len(sus)} groups", file=out)
    print(f"{'verdict':8} {'n':>4} {'orph':>4} {'oldest':>7} {'MB':>6} {'handles':>7} {'cpu%':>6} "
          f"{'kill':>4}  group", file=out)
    for r in rows:
        print(f"{r['verdict']:8} {r['count']:>4} {r['orphans']:>4} {fmt_age(r['oldest']):>7} "
              f"{r['ws_mb']:>6} {r['handles']:>7} {r['cpu_pct']:>6} {r['kill_ok']:>4}  {r['group']}", file=out)
        if r["verdict"] == "SUSPECT":
            for why in r["reasons"]:
                print(f"{'':44}- {why}", file=out)
            print(f"{'':44}  pids {', '.join(map(str, r['pids'][:12]))}"
                  f"{' ...' if len(r['pids']) > 12 else ''}", file=out)


def kill(procs, only=None, terminate=None):
    """Terminate kill-allowlisted suspects. Returns [(pid, name, rule, result)]."""
    if terminate is None:
        import winproc as W
        terminate = W.terminate_verified
    done = []
    for p in procs:
        if (not p["kill_ok"] or p["verdict"] != "SUSPECT" or protected(p)
                or (p["parent_alive"] and p.get("rule") not in LIVE_PARENT_KILL)):
            continue
        if only and p.get("rule") not in only:
            continue
        done.append((p["pid"], p["name"], p["rule"], terminate(p["pid"], p["ctime"])))
    return done


def default_log(home=None):
    """The scheduled run's log: <home>/.claude/logs/procwatch.log (home = the user's profile)."""
    return os.path.join(home or os.path.expanduser("~"), ".claude", "logs", "procwatch.log")


def log_path(arg, home=None, here=HERE):
    """Where --log writes: the default for a bare --log, and the default too (with a note on stderr)
    for a path inside the tool's own folder - the tool folder is a link onto the kit checkout, so
    a log there would land in the repo. Any other path is kept."""
    if arg is None:
        return default_log(home)
    try:
        inside = os.path.commonpath([os.path.realpath(arg), os.path.realpath(here)]) == os.path.realpath(here)
    except ValueError:                              # different drives
        inside = False
    if inside:
        new = default_log(home)
        print(f"procwatch: not logging inside the tool folder ({arg}); writing {new}", file=sys.stderr)
        return new
    return arg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=float, default=3.0, help="CPU sampling window in seconds")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", nargs="?", const="", default=None,
                    help="append a one-line summary to this file (no file: ~/.claude/logs/procwatch.log)")
    ap.add_argument("--kill", action="store_true", help="terminate kill-allowlisted suspects")
    ap.add_argument("--only", help="comma list of rule ids --kill may act on: " + ",".join(RULE_IDS))
    a = ap.parse_args(argv)
    if os.name != "nt":
        print("procwatch: Windows only", file=sys.stderr)
        return 2
    only = set(a.only.split(",")) if a.only else None
    if only and not only <= set(RULE_IDS):
        print(f"procwatch: unknown rule(s) {sorted(only - set(RULE_IDS))}", file=sys.stderr)
        return 2
    import winproc as W
    procs = classify(collect(a.sample), a.sample)
    rows = summarize(procs)
    killed = kill(procs, only) if a.kill else []
    vit = W.perf_info() or {}
    pool_high = vit.get("paged_pool", 0) > PAGED_POOL_WARN
    gb = lambda k: f"{vit.get(k, 0) / 2**30:.1f}"   # noqa: E731
    vitals = (f"machine: paged pool {gb('paged_pool')} GB, nonpaged pool {gb('nonpaged_pool')} GB, "
              f"handles {vit.get('handles', 0)}, commit {gb('commit')}/{gb('commit_limit')} GB" +
              (f"  <- SUSPECT: paged pool over {PAGED_POOL_WARN // 2**30} GB (leaked kernel handles; "
               "a reboot frees it)" if pool_high else ""))
    if a.json:
        json.dump({"machine": vit, "groups": rows, "processes": procs, "killed": killed}, sys.stdout,
                  indent=1, default=str)
        print()
    else:
        print_report(rows, procs)
        print(vitals)
        for pid, name, rule, res in killed:
            print(f"kill {pid} {name} ({rule}): {res}")
    sus = [r for r in rows if r["verdict"] == "SUSPECT"]
    if a.log is not None:
        path = log_path(a.log or None)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        line = (f"{datetime.datetime.now():%Y-%m-%dT%H:%M:%S} procs={len(procs)} suspect="
                f"{sum(r['count'] for r in sus)} pool={gb('paged_pool')}G " +
                " ".join(f"{r['group'].split(':')[0]}={r['count']}" for r in sus) +
                (f" killed={sum(1 for k in killed if k[3] == 'killed')}" if a.kill else ""))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return 1 if (sus or pool_high) else 0


if __name__ == "__main__":
    sys.exit(main())
