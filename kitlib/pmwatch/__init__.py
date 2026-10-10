"""kitlib.pmwatch: the PM watcher's checks (docs/pm-watch.md).

Each check turns the previous state and a fresh reading into event lines. tools/pm_watch.py loops,
prints the lines and exits. Event lines and state keys are those of the PM repo's
launcher/pm_watch.py at 95f7262. Transcript reading is host-specific and lives in claude/claude_host.py.
"""
import json
import os
import re
import subprocess
import time

from kitlib.config import user as kuser
from kitlib.git import cmd as gitcmd

KEY = "pm_watch"
WEEK_BANDS = (95, 98, 99)
FIVE_BANDS = (95, 98, 99)         # 95 no new work, 98 landings only; no PM headroom
LINE = re.compile(r"account (\d+|codex): five_hour=(reset|\d+)%?.*?seven_day=(reset|\d+)%?")


def load_config(path=None):
    """The "pm_watch" dict from the user-profile config; {} when the file or key is missing."""
    p = os.path.expanduser(path) if path else kuser.config_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            cfg = json.load(f).get(KEY)
    except (OSError, ValueError, AttributeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def default_state_file():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "pm_watch", "state.json")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, s):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, path)


def branch_tip(repo, ref):
    args = ["ls-remote", "origin", ref]
    r = gitcmd.subprocess_run(["git", "-C", repo] + args, capture_output=True, text=True, timeout=60,
                              env=gitcmd.network_env(args, os.environ))
    if r.returncode != 0:
        raise RuntimeError("ls-remote exit %d" % r.returncode)
    return r.stdout.split()[0][:8] if r.stdout.strip() else None


def parse_usage(text):
    out = {}
    for m in LINE.finditer(text):
        conv = lambda v: 0 if v == "reset" else int(v)
        out[m.group(1)] = {"five": conv(m.group(2)), "week": conv(m.group(3))}
    if not out:
        raise RuntimeError("the usage report printed no account lines")
    return out


def launcher_configured(home=None):
    """True when the machine config has a `launcher` key, so in-process usage reads the accounts
    it names; without one it would read only the default ~/.claude account. A config that cannot
    be read counts as no key."""
    from kitlib.config import user as kuser
    from kitlib.config.types import ConfigError
    try:
        return kuser.user_config(home).get("launcher") is not None
    except ConfigError:
        return False


def usage(script=None, report=None):
    """Every account's usage. No script: read in this process (kitlib.launcher.usage, from the
    launcher settings of the machine config); else the lines a PowerShell script prints."""
    if not script:
        if report is None:
            from kitlib.launcher import settings as launcher_settings, usage as launcher_usage
            report = lambda: launcher_usage.report(launcher_settings.load())
        return parse_usage("\n".join(report()))
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                       capture_output=True, text=True, timeout=120)
    return parse_usage(r.stdout)


def band(v, bands):
    return max([b for b in bands if v >= b], default=0)


def usage_events(prev, cur):
    ev = []
    for acct, u in cur.items():
        p = prev.get(acct)
        if p is None:
            continue
        wb, pwb = band(u["week"], WEEK_BANDS), band(p["week"], WEEK_BANDS)
        if wb > pwb:
            ev.append("USAGE account %s seven_day %d%% (now %d%%)" % (acct, wb, u["week"]))
        fb, pfb = band(u["five"], FIVE_BANDS), band(p["five"], FIVE_BANDS)
        if fb > pfb:
            ev.append("USAGE account %s five_hour %d%% (now %d%%)" % (acct, fb, u["five"]))
        if p["week"] - u["week"] >= 20:
            ev.append("RESET account %s (weekly %d%% -> %d%%)" % (acct, p["week"], u["week"]))
    return ev


def alert_events(seen, alerts_dir):
    """(events, seen now): one BGFAIL per alert file not in seen. An unreadable file (mid-write)
    is left unseen for the next poll; names no longer in the folder drop out of seen."""
    try:
        names = sorted(n for n in os.listdir(alerts_dir) if n.endswith(".json"))
    except FileNotFoundError:
        return [], []
    ev, now = [], []
    for n in names:
        if n in seen:
            now.append(n)
            continue
        try:
            with open(os.path.join(alerts_dir, n), encoding="utf-8") as f:
                a = json.load(f)
        except (OSError, ValueError):
            continue
        reason = " ".join(str(a.get("reason") or a.get("how") or "failed").split())
        ev.append("BGFAIL run %s %s" % (a.get("id") or n[:-5], reason))
        now.append(n)
    return ev, now


# Lock-window notices (kitlib.lock.notice writes them): <dir>/<run id>-start|end.json, each an
# Action Hub inbox doc with its id; posted ones move to <dir>/posted/.
POSTED = "posted"


def configured_notice_dir(path=None):
    """machine_lock.notice.dir of the user-profile config, or None."""
    p = os.path.expanduser(path) if path else kuser.config_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)["machine_lock"]["notice"]["dir"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return d if isinstance(d, str) and d else None


def pending_notices(d):
    """[(file name, doc)] of the notices in d not yet posted, oldest first. An unreadable file
    (one being written) is left for the next look."""
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("id"):
            out.append((n, doc))
    out.sort(key=lambda x: (str(x[1].get("at") or ""), x[0]))
    return out


def notice_batch(docs):
    """An ArtifactData batch that sets each notice doc as inbox/<its id>."""
    return [{"op": "set", "collection": "inbox", "doc_id": doc["id"],
             "data": {k: v for k, v in doc.items() if k != "id"}} for doc in docs]


def notice_file(doc_id):
    """The file name of the notice whose inbox id is doc_id ("L-<run id>-<phase>"), or None."""
    ok = (isinstance(doc_id, str) and doc_id.startswith("L-")
          and doc_id.endswith(("-start", "-end")) and os.path.basename(doc_id) == doc_id)
    return doc_id[2:] + ".json" if ok else None


def mark_posted(d, names):
    """Moves the named notice files into d/posted/. Returns the names moved."""
    os.makedirs(os.path.join(d, POSTED), exist_ok=True)
    moved = []
    for n in names:
        if not isinstance(n, str) or os.path.basename(n) != n or not n.endswith(".json"):
            continue
        try:
            os.replace(os.path.join(d, n), os.path.join(d, POSTED, n))
            moved.append(n)
        except FileNotFoundError:
            pass
    return moved


def notice_events(seen, notice_dir):
    """(events, seen now): one LOCKNOTICE per notice in notice_dir not in seen and not yet posted
    (posted ones move away, so they drop out of seen)."""
    ev, now = [], []
    for n, doc in pending_notices(notice_dir):
        if n not in seen:
            ev.append("LOCKNOTICE %s %s" % (doc["id"], " ".join(str(doc.get("title") or "").split())))
        now.append(n)
    return ev, now


def watched_sessions(sessions):
    """Sorted names of the sessions.json entries to watch: every entry with a name (coordinator or
    plain), except autostart=false. Empty if the file is unreadable."""
    try:
        with open(sessions, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return sorted({v["name"] for v in d.values() if isinstance(v, dict) and v.get("name")
                   and v.get("autostart") is not False})


def idle_baseline(names, newest, now, minutes):
    """{name: newest mtime} for sessions already quiet, so a relaunch-free first run stays silent.
    newest(name) is the newest activity time of that session, or None (claude.claude_host.newest_finder)."""
    out = {}
    for n in names:
        m = newest(n)
        if m is not None and now - m >= minutes * 60:
            out[n] = m
    return out


def idle_events(names, newest, fired, nolog, now, minutes):
    """(events, fired now, nolog now). IDLE once per quiet spell: fired maps name -> the mtime it
    fired at, and a session drops out of it when active again. A name with no transcript gives
    one ERROR, then stays quiet (nolog) until it has a transcript."""
    ev, cur, miss = [], {}, []
    for n in names:
        m = newest(n)
        if m is None:
            miss.append(n)
            if n not in nolog:
                ev.append("ERROR idle: no transcript for %s" % n)
        elif now - m >= minutes * 60:
            cur[n] = m
            if fired.get(n) != m:
                ev.append("IDLE %s %dm since %s" % (n, (now - m) // 60,
                          time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(m))))
    return ev, cur, miss


class Watcher:
    """One watcher run. start() records baselines; poll(now) runs the checks that are due and
    returns their event lines. The caller saves state after each poll and stops at the first
    non-empty one. Every check gives an ERROR line on its third failure in a row.

    branches: [(repo_dir, ref)]; usage_reader() -> {account: {"five", "week"}};
    sessions: path of sessions.json; newest(name) -> mtime or None; tip(repo, ref) -> short sha.
    """

    def __init__(self, state, branches, usage_reader, alerts_dir, sessions, newest,
                 idle_minutes=20, branch_poll=300, usage_poll=600, idle_poll=300, tip=branch_tip,
                 notice_dir=None):
        self.s = state
        self.branches = branches
        self.usage_reader = usage_reader
        self.alerts_dir = alerts_dir
        self.sessions = sessions
        self.newest = newest
        self.idle_minutes = idle_minutes
        self.branch_poll, self.usage_poll, self.idle_poll = branch_poll, usage_poll, idle_poll
        self.tip = tip
        self.notice_dir = notice_dir
        self.fails = {"branch": 0, "usage": 0, "alerts": 0, "idle": 0, "notices": 0}
        self.next_b = self.next_u = self.next_i = 0.0

    def start(self, now):
        s = self.s
        tips = s.setdefault("branches", {})
        for repo, ref in self.branches:             # a newly watched branch: baseline, no event
            key = "%s %s" % (repo, ref)
            if key not in tips:
                tips[key] = self.tip(repo, ref)
        if "usage" not in s:
            s["usage"] = self.usage_reader()
        if "bg_alerts" not in s:                    # first run: existing alerts are baseline
            s["bg_alerts"] = alert_events([], self.alerts_dir)[1]
        if self.notice_dir and "lock_notices" not in s:
            s["lock_notices"] = notice_events([], self.notice_dir)[1]
        if "idle" not in s:                         # first run: sessions already quiet are baseline
            s["idle"] = idle_baseline(watched_sessions(self.sessions), self.newest, now,
                                      self.idle_minutes)

    def _failed(self, what, e, events):
        self.fails[what] += 1
        if self.fails[what] == 3:
            events.append("ERROR %s check: %s" % (what, e))

    def poll(self, now):
        s, events = self.s, []
        tips = s.setdefault("branches", {})
        if now >= self.next_b:
            self.next_b = now + self.branch_poll
            try:
                for repo, ref in self.branches:
                    key, tip = "%s %s" % (repo, ref), self.tip(repo, ref)
                    if tip and tip != tips.get(key):
                        events.append("BRANCH %s %s %s" % (repo, ref, tip))
                        tips[key] = tip
                self.fails["branch"] = 0
            except Exception as e:
                self._failed("branch", e, events)
        if now >= self.next_u:
            self.next_u = now + self.usage_poll
            try:
                cur = self.usage_reader()
                self.fails["usage"] = 0
                events += usage_events(s.get("usage", {}), cur)
                s["usage"] = {**s.get("usage", {}), **cur}
            except Exception as e:
                self._failed("usage", e, events)
        if now >= self.next_i:
            self.next_i = now + self.idle_poll
            try:
                ev, s["idle"], s["idle_nolog"] = idle_events(
                    watched_sessions(self.sessions), self.newest, s.get("idle", {}),
                    s.get("idle_nolog", []), now, self.idle_minutes)
                events += ev
                self.fails["idle"] = 0
            except Exception as e:
                self._failed("idle", e, events)
        try:                                        # every loop: a listdir is cheap
            ev, s["bg_alerts"] = alert_events(set(s.get("bg_alerts", [])), self.alerts_dir)
            events += ev
            self.fails["alerts"] = 0
        except Exception as e:
            self._failed("alerts", e, events)
        if self.notice_dir:
            try:
                ev, s["lock_notices"] = notice_events(set(s.get("lock_notices", [])),
                                                      self.notice_dir)
                events += ev
                self.fails["notices"] = 0
            except Exception as e:
                self._failed("notices", e, events)
        return events
