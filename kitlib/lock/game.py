"""Game mode: while a listed game runs, the lock keeps one L3 domain (a cache-sharing group of cores) for it.

games.json: {"enabled": bool, "games": [{"exe": "Game.exe", "pin": bool, "name": "..."}]}

sync() looks, at most every settings.game_poll_s, for a listed exe in one Toolhelp snapshot
(process names only: nothing is opened). When one runs and game mode is on, it makes a game hold
(kitlib.lock.holds, owner "game", scope "domain") over the cpu slots of the preferred L3 domain
(the one holding the best efficiency or scheduling class, else the first); on a machine with one
L3, half the cpu slots, preferred threads first. The gpu is untouched. Runs already on those
slots finish (holds never evict); new runs plan around them. The hold rolls: its end is kept
settings.game_roll_s ahead while the game runs, and it ends when the game exits (or game mode is
switched off), or lapses by itself if nothing looks.

Pinning the game onto its domain is best effort and only when the entry's pin is on. It opens
the game process with write access, so pin defaults off for titles known to use Vanguard,
EasyAntiCheat or BattlEye, and with pin off the game process is never opened at all.

Tests pass fakes through store.game_probes: {"procs": callable -> [(pid, ppid, name)],
"pin": callable(pid, mask) -> result, "unpin": callable(pid, game) -> result, "ctime":
callable(pid) -> creation time, "affinity": callable(pid, ctime) -> mask, "services": callable
-> [installed anti-cheat service]}.
"""
import contextlib
import datetime
import re

from kitlib.lock import holds as hl
from kitlib.lock import layout as lay
from kitlib.lock.store import EXIT_USAGE, LockError, iso, read_json, utc_now
from kitlib.proc import process

AC_SERVICES = {"vgc": "Vanguard", "vgk": "Vanguard", "EasyAntiCheat": "EasyAntiCheat",
               "EasyAntiCheat_EOS": "EasyAntiCheat", "BEService": "BattlEye"}
# Titles known to ship one of those (lower-case exe names). An exe not here gets pin on by default.
AC_TITLES = {
    "valorant.exe": "Vanguard", "valorant-win64-shipping.exe": "Vanguard",
    "league of legends.exe": "Vanguard",
    "fortniteclient-win64-shipping.exe": "EasyAntiCheat", "r5apex.exe": "EasyAntiCheat",
    "r5apex_dx12.exe": "EasyAntiCheat", "eldenring.exe": "EasyAntiCheat",
    "deadbydaylight-win64-shipping.exe": "EasyAntiCheat", "rustclient.exe": "EasyAntiCheat",
    "fallguys_client_game.exe": "EasyAntiCheat", "thefinals.exe": "EasyAntiCheat",
    "rainbowsix.exe": "BattlEye", "rainbowsix_be.exe": "BattlEye",
    "escapefromtarkov.exe": "BattlEye", "destiny2.exe": "BattlEye", "dayz_x64.exe": "BattlEye",
    "arma3_x64.exe": "BattlEye", "tslgame.exe": "BattlEye", "pubg.exe": "BattlEye",
}
EXE_RE = re.compile(r"^[^\\/:*?\"<>|]{1,200}\.exe$", re.I)


def anti_cheat(exe):
    """The anti-cheat a title is known to use, or None."""
    return AC_TITLES.get((exe or "").lower())


def installed_ac_services():
    """[service name] of the anti-cheat services installed here (read: the service manager is
    asked whether each exists; nothing is started or opened for change)."""
    from kitlib.proc.win32 import ADV
    out = []
    scm = ADV.OpenSCManagerW(None, None, 0x0001)            # SC_MANAGER_CONNECT
    if not scm:
        return out
    try:
        for name in AC_SERVICES:
            h = ADV.OpenServiceW(scm, name, 0x0004)         # SERVICE_QUERY_STATUS
            if h:
                ADV.CloseServiceHandle(h)
                out.append(name)
    finally:
        ADV.CloseServiceHandle(scm)
    return out


def _probe(store, name):
    return (getattr(store, "game_probes", None) or {}).get(name)


# ------------------------------------------------------------------ the list
def read_list(store):
    """{enabled, games: [{exe, pin, name}]}: games.json, entries in any other shape dropped. No
    file: off, no games."""
    d = store.read("games.json") or {}
    games = []
    for g in d.get("games") or []:
        if isinstance(g, dict) and isinstance(g.get("exe"), str) and EXE_RE.match(g["exe"]):
            games.append({"exe": g["exe"], "pin": g.get("pin") is True,
                          "name": g.get("name") if isinstance(g.get("name"), str) else None})
    return {"enabled": d.get("enabled") is True, "games": games}


def check_games(games):
    """The list to write from `games` ([{exe, pin?, name?}]): an exe is a file name ending .exe;
    pin left out takes its default (off for a known anti-cheat title, else on). LockError(2)."""
    if not isinstance(games, list):
        raise LockError(EXIT_USAGE, "games must be a list")
    out, seen = [], set()
    for g in games:
        if not isinstance(g, dict) or not isinstance(g.get("exe"), str) or not EXE_RE.match(g["exe"]):
            raise LockError(EXIT_USAGE, "each game is {exe: \"Name.exe\", pin?, name?}; %r is not"
                            % (g,))
        if g["exe"].lower() in seen:
            raise LockError(EXIT_USAGE, "%s is listed twice" % g["exe"])
        seen.add(g["exe"].lower())
        pin = g.get("pin")
        if pin is None:
            pin = anti_cheat(g["exe"]) is None
        if type(pin) is not bool:
            raise LockError(EXIT_USAGE, "pin must be true or false")
        if g.get("name") is not None and not isinstance(g["name"], str):
            raise LockError(EXIT_USAGE, "name must be text")
        out.append({"exe": g["exe"], "pin": pin, "name": g.get("name")})
    return out


def write_list(store, enabled=None, games=None):
    """Writes games.json: `enabled` and/or `games` (check_games). Returns the list."""
    with store.lock("games"):
        cur = read_list(store)
        if enabled is not None:
            if type(enabled) is not bool:
                raise LockError(EXIT_USAGE, "enabled must be true or false")
            cur["enabled"] = enabled
        if games is not None:
            cur["games"] = check_games(games)
        store.write("games.json", cur)
    store._game_at = 0.0
    return cur


# ------------------------------------------------------------------ the domain
def domain_slots(L, hw):
    """The cpu slots a game takes: the slots of the preferred L3 domain (the one whose best
    thread ranks highest by efficiency, then scheduling class; the first among equals), or with
    one L3, half the cpu slots (at least one), preferred threads first."""
    rank = {t: i for i, t in enumerate(lay.preferred_threads(hw))}
    best = lambda slots: min(rank.get(t, 1 << 30) for i in slots for t in L["slots_cpu"][i])
    doms = [d for d in L.get("l3_slots") or [] if d]
    if len(doms) >= 2:
        return sorted(min(doms, key=lambda d: (best(d), d[0])))
    n = max(1, L["cpu_slots"] // 2)
    return sorted(sorted(range(L["cpu_slots"]), key=lambda i: (best([i]), i))[:n])


# ------------------------------------------------------------------ the look
def running(store, lst):
    """(entry, pid) of the first listed game running, or (None, None). Names only."""
    if not lst["games"]:
        return None, None
    procs = _probe(store, "procs") or process.snapshot
    try:
        snap = procs()
    except OSError:
        return None, None
    names = {}
    for pid, _pp, name in snap:
        names.setdefault((name or "").lower(), pid)
    for g in lst["games"]:
        pid = names.get(g["exe"].lower())
        if pid is not None:
            return g, pid
    return None, None


def live_game_hold(hs):
    return next((h for h in hs if h["owner"] == "game"), None)


def _pin(store, L, h, pid):
    """Pins game pid onto hold h's cpu slots once, under the holds mutex with the hold read again
    there (it may have ended or rolled meanwhile: a pin never writes an ended hold back). Records
    the result, the pid's creation time and the affinity it had (for _unpin) in the hold's game
    field; a hold that follows an expired one on the same process (its game's carry) keeps the
    affinity recorded there, since the process is still pinned. Returns the result, or None when
    the hold is gone."""
    with store.lock("holds"):
        d = read_json(store.path("holds", h["id"] + ".json"))
        if not isinstance(d, dict) or d.get("id") != h["id"]:
            return None
        g = dict(d.get("game") or {})
        if g.get("pinned_pid") == pid:
            h["game"] = g
            return g.get("pinned")
        mask = lay.cores_mask(L, h["_slots"]["cpu"])
        carry = g.pop("carry", None) or {}
        fake = _probe(store, "pin")
        if fake is not None:
            ctime_of = _probe(store, "ctime") or (lambda _pid: None)
            affinity = _probe(store, "affinity") or (lambda _pid, _ct: None)
        else:
            ctime_of, affinity = process.proc_ctime, process.affinity_of
        ct = ctime_of(pid)
        if ct is not None and carry.get("pinned_pid") == pid and carry.get("pinned_ctime") == ct:
            before = carry.get("before")
        else:
            before = None if ct is None else affinity(pid, ct)
        if fake is not None:
            result = fake(pid, mask)
        else:
            result = "gone" if ct is None else process.set_affinity(pid, ct, mask)
        g.update(pinned=result, pinned_pid=pid, pinned_ctime=ct, before=before)
        d["game"] = g
        store.write("holds/%s.json" % h["id"], d)
        h["game"] = g
        return result


def _unpin(store, h, lst):
    """Gives the game its own affinity back as its hold h ends while it may still run. Pin-on
    only: a game whose pin is off, in the hold or in the list now, is never opened, and
    nor is one the hold never pinned. Returns the result, or None when nothing was undone."""
    g = h.get("game") or {}
    if not g.get("pin") or g.get("pinned") not in ("pinned", "soft", "overridden"):
        return None
    exe = (g.get("exe") or "").lower()
    if not any(x["exe"].lower() == exe and x.get("pin") for x in lst["games"]):
        return None
    fake = _probe(store, "unpin")
    if fake is not None:
        return fake(g.get("pinned_pid"), g)
    try:
        return process.restore_affinity(int(g["pinned_pid"]), g.get("pinned_ctime"),
                                        g.get("before"), g.get("pinned") == "soft")
    except (TypeError, ValueError, KeyError):
        return "failed"


def unpin_expired(store, h):
    """{"unpinned": result} for game hold h ending as expired (holds.records prune): its game gets
    its affinity back when that process still runs (looked for by name only); {} otherwise."""
    with contextlib.suppress(OSError, LockError):
        lst = read_list(store)
        g, pid = running(store, lst)
        hg = h.get("game") or {}
        if g is None or g["exe"].lower() != (hg.get("exe") or "").lower() or (
                pid != hg.get("pinned_pid")):
            return {}
        un = _unpin(store, h, lst)
        return {} if un is None else {"unpinned": un}
    return {}


def _end_expired(store, lst, g, pid):
    """Ends the expired game holds (one sync did not roll in time). One for the process still
    running and still to be pinned is not undone: its pin record is returned to carry onto the
    next hold, so that hold's _unpin restores the affinity from before the first pin. Any other
    still running gets its affinity back; a game that exited is not opened."""
    carry = None
    for d in hl.expired_games(store):
        dg = d.get("game") or {}
        same = g is not None and dg.get("exe", "").lower() == g["exe"].lower()
        if same and g["pin"] and lst["enabled"] and dg.get("pinned_pid") == pid:
            carry = {k: dg.get(k) for k in ("pinned_pid", "pinned_ctime", "before")}
            un = None
        elif (g is None and lst["enabled"]) or (same and dg.get("pinned_pid") != pid):
            un = None
        else:
            un = _unpin(store, d, lst)
        hl.end(store, d, "expired", **({} if un is None else {"unpinned": un}))
    return carry


def sync(store, L, force=False, now=None):
    """Brings the game hold in line with what runs, at most every settings.game_poll_s (force:
    now). Returns None. Never raises for a failed look: game mode is best effort."""
    s = store.settings
    t = utc_now() if now is None else now
    mono = t.timestamp()
    if not force and mono - store._game_at < s.game_poll_s:
        return None
    store._game_at = mono
    with contextlib.suppress(OSError, LockError):
        lst = read_list(store)
        g, pid = running(store, lst) if lst["enabled"] else (None, None)
        with store.lock("holds"):
            carry = _end_expired(store, lst, g, pid)
            hs = hl.records(store, L)
            h = live_game_hold(hs)
            if h is not None and (g is None or (h.get("game") or {}).get("exe", "").lower()
                                  != g["exe"].lower()):
                # A game that exited is not looked for again; one still running (game mode off,
                # or another listed game seen first) gets its own affinity back.
                un = None if g is None and lst["enabled"] else _unpin(store, h, lst)
                hl.end(store, h, "game exited" if lst["enabled"] else "game mode off",
                       **({} if un is None else {"unpinned": un}))
                h = None
            if g is None:
                return None
            roll = datetime.timedelta(seconds=s.game_roll_s)
            if h is None:
                h, _ = hl.create(store, L, {
                    "owner": "game", "scope": "domain", "start": t, "end": t + roll,
                    "slots": {"cpu": domain_slots(L, store_hw(store)), "gpu": []},
                    "game": {"exe": g["exe"], "name": g.get("name"), "pid": pid,
                             "pin": g["pin"], "seen": iso(t),
                             **({"carry": carry} if carry else {})},
                    "note": "game mode: %s" % (g.get("name") or g["exe"])},
                    {"name": "game mode"}, now=t)
            elif (h["_end"] - t) < roll / 2:
                d = hl.public(h)
                d["end"] = iso(t + roll)
                d["game"] = dict(d.get("game") or {}, pid=pid, seen=iso(t))
                store.write("holds/%s.json" % h["id"], d)
                h.update(d)
        if g["pin"] and h is not None:
            _pin(store, L, h, pid)
    return None


def store_hw(store):
    return lay.hardware(store)


def state(store, L, hs=None):
    """Game mode for the API: {enabled, games: [{exe, name, pin, anti_cheat}], running: {exe,
    name, pid} or null, hold: id or null, slots: [cpu slot], pinned: result or null,
    anti_cheat_services: [installed]}."""
    lst = read_list(store)
    hs = hl.records(store, L) if hs is None else hs
    h = live_game_hold(hs)
    svc = _probe(store, "services") or installed_ac_services
    try:
        services = svc()
    except OSError:
        services = []
    g = (h or {}).get("game") or {}
    return {"enabled": lst["enabled"],
            "games": [dict(x, anti_cheat=anti_cheat(x["exe"])) for x in lst["games"]],
            "running": {"exe": g.get("exe"), "name": g.get("name"), "pid": g.get("pid")} if h else None,
            "hold": h["id"] if h else None, "slots": h["_slots"]["cpu"] if h else [],
            "would_take": domain_slots(L, store_hw(store)),
            "pinned": g.get("pinned") if h else None,
            "anti_cheat_services": [{"service": n, "product": AC_SERVICES[n]} for n in services]}
