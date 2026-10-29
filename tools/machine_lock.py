"""machine_lock.py: heavy work under the machine lock, and the lock's status and changes.

  run --kind K --repo R --slug S [options] -- <command...>   a heavy run (kitlib.lock.run)
  background --repo R --slug S [options] -- <command...>     a pausable gpu run
  status [ID] [--json]                                       the lock; --json is api.snapshot()
  stop ID [--force]                                          end a run
  top ID                                                     the owner's: a waiting run to the
                                                             front of the queue
  hold whole|half|slots|agent ... / hold end|edit ID / hold list    holds
  division [--set JSON | --default]                          the owner's division
  game [on|off|set JSON]                                     game mode

run may run the command on a load node instead (kitlib.loadnode.dispatch, docs/load-nodes.md
"Lock runs on a node"): --node auto (the default) when a node has opted the repository in and
has more room, --node main never, --node NAME on that node; status adds a `node:` line per node.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.lock, prints and exits.
Everything it shows or changes goes through kitlib.lock.api. Exit codes: 0 done, the
command's own code for run, 1 no such record or not stopped, 2 usage or refused, 6 busy
(--no-wait), 8 --max-wait ran out, 10 the lock cannot be had (fail closed; with --node NAME,
the node could not be used: nothing ran), 11 a hold keeps it out, 12 the hold cannot be used.
A run on a load node also: 9 the connection to the node was lost (the job runs on there: the
follow and stop lines are said), 13 the job succeeded but its artifacts did not all come (none
was copied in), 14 the node started the job but it ended there with no exit code of its own or
could not be followed (it is never run again here).
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.lock import api                                        # noqa: E402
from kitlib.lock import detach as dt                               # noqa: E402
from kitlib.lock import holds as hl                                # noqa: E402
from kitlib.lock import layout as lay                              # noqa: E402
from kitlib.lock import status as st                               # noqa: E402
from kitlib.lock.background import run_background                  # noqa: E402
from kitlib.lock.run import (ORIGIN_ENV, ScriptArgError, check_resource,  # noqa: E402
                             path_alias_line, run_command, script_command)
from kitlib.lock.store import (EXIT_REQUIRE, EXIT_USAGE, LockError, Store,  # noqa: E402
                               caller, iso, new_id, utc_now)
from kitlib.loadnode import box, dispatch                          # noqa: E402
from kitlib.proc import bash, process                              # noqa: E402
# Not used here: loaded at start because kitlib.proc.daemons imports it inside functions that run
# at the end of a run, and a first load then could read a checkout a landing has rewritten.
from kitlib.proc import tcp                                        # noqa: E402, F401

PREFIX = "[machine-lock]"
TOOL = os.path.abspath(__file__)
API_EXIT = {"invalid": EXIT_USAGE, "forbidden": EXIT_USAGE, "not_found": 1, "conflict": 11,
            "busy": 6, "store": EXIT_REQUIRE}


def say(text):
    print("%s %s" % (PREFIX, text), file=sys.stderr, flush=True)


def out(text):
    print("%s %s" % (PREFIX, text), flush=True)


# ------------------------------------------------------------------ argument types
def _seconds(s):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", s or "")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else None


def parse_wait(s):
    v = _seconds(s)
    if v is None:
        raise argparse.ArgumentTypeError("--max-wait takes N, Ns, Nm, Nh or Nd, not %r" % s)
    return v


def parse_duration(s):
    v = _seconds(s)
    if v is None or not 0 < v < float("inf"):
        raise argparse.ArgumentTypeError("takes N, Ns, Nm, Nh or Nd (more than 0), not %r" % s)
    return v


def parse_hhmm(s):
    try:
        return datetime.datetime.strptime(s.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("takes HH:MM (24 h, local time), not %r" % s)


def parse_date(s):
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("takes YYYY-MM-DD, not %r" % s)


def parse_slots(s):
    """'0,2,4-5' -> [0, 2, 4, 5]."""
    got = set()
    for part in (s or "").split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise argparse.ArgumentTypeError("slots are like 0,2,4-5, not %r" % s)
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if hi < lo:
            raise argparse.ArgumentTypeError("%s: the range runs backwards" % part)
        got.update(range(lo, hi + 1))
    return sorted(got)


def parse_threads(s):
    """'8-15,20' -> the thread ids (kitlib.lock.layout.parse_threads)."""
    try:
        return lay.parse_threads(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def parse_glob(s):
    if not box.glob_ok(s):
        raise argparse.ArgumentTypeError("%r is not a relative glob under the checkout (%s)"
                                         % (s, box.GLOB_RULE))
    return s


def parse_json(s):
    try:
        return json.loads(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError("not JSON: %s" % e)


def window_args(p):
    p.add_argument("--for", dest="dur", type=parse_duration, metavar="D",
                   help="N, Ns, Nm, Nh or Nd: from now (or from --from) for that long")
    p.add_argument("--from", dest="start", type=parse_hhmm, metavar="HH:MM", help="local time")
    p.add_argument("--until", type=parse_hhmm, metavar="HH:MM",
                   help="local time; not after --from means the next day")
    p.add_argument("--date", type=parse_date, metavar="YYYY-MM-DD",
                   help="--from's day (default: today, or tomorrow when today's window is over)")
    p.add_argument("--note")


def parser():
    ap = argparse.ArgumentParser(prog="machine_lock.py", allow_abbrev=False,
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", allow_abbrev=False, help="run a heavy command under the lock",
                       usage="machine_lock.py run --kind K --repo R --slug S [options] -- "
                             "<command...>")
    r.add_argument("--kind", required=True, help="a kind of the kinds table: its size")
    r.add_argument("--repo", required=True)
    r.add_argument("--slug", required=True)
    r.add_argument("--cores", type=int, metavar="N", help="fewer cpu slots than the kind's "
                   "size: 1 to that size, never more")
    r.add_argument("--hold", metavar="H", help="run inside hold H: an owner whole hold (the "
                   "owner's own runs only), or an agent hold made for it (exit 12 when "
                   "it cannot)")
    r.add_argument("--gpu-share", choices=("training",), help="one gpu slot of a shareable kind")
    r.add_argument("--within", type=parse_threads, metavar="THREADS",
                   help="stay inside these threads (a Linux cpuset list such as 8-15, e.g. a "
                        "container's): it takes only cpu slots whose threads all lie inside "
                        "and waits for those; exit 2 when they are too few for its size "
                        "(--cores) or it has --hold")
    r.add_argument("--resource", metavar="NAME",
                   help="an exclusive resource (any non-empty name, e.g. ports:e2e-a) the run "
                        "holds for its whole life: while another live run holds it, a run naming "
                        "it waits (exit 6 with --no-wait), however many slots are free")
    r.add_argument("--on-release", metavar="CMDLINE", help="a shell command run after the command "
                   "ends (MACHINE_LOCK_END says how) and before the slots are freed")
    w = r.add_mutually_exclusive_group()
    w.add_argument("--no-wait", action="store_true", help="exit 6 at once when it must wait")
    w.add_argument("--max-wait", dest="wait_s", type=parse_wait, default=None,
                   help="N, Ns, Nm, Nh or Nd; exit 8 when it runs out (default: no limit)")
    r.add_argument("--expect", dest="expect_s", type=parse_duration, metavar="D",
                   help="how long it expects to hold its slots (default: its history's median, "
                        "else its hold's end, else its kind's warn_min)")
    r.add_argument("--note")
    r.add_argument("--detach", action="store_true",
                   help="run outside this shell; prints '<run id> pid <n>'")
    r.add_argument("--detached-id", help=argparse.SUPPRESS)
    r.add_argument("--node", default="auto", metavar="auto|main|NAME",
                   help="where it runs: auto (default) here or on a load node that opted the "
                        "repository in and has more room, main here only, NAME that load node "
                        "(docs/load-nodes.md)")
    r.add_argument("--artifact", action="append", default=[], type=parse_glob, metavar="GLOB",
                   help="on a load node: files to copy back, relative to the checkout's top "
                        "(repeatable)")
    r.add_argument("--registry", help=argparse.SUPPRESS)    # tests: a load node registry file
    b = sub.add_parser("background", allow_abbrev=False,
                       help="a pausable gpu training run in a gpu slot",
                       usage="machine_lock.py background --repo R --slug S [options] -- "
                             "<command...>")
    b.add_argument("--repo", required=True)
    b.add_argument("--slug", required=True)
    b.add_argument("--expect", dest="expect_s", type=parse_duration, metavar="D")
    b.add_argument("--note")
    b.add_argument("--detach", action="store_true")
    b.add_argument("--detached-id", help=argparse.SUPPRESS)
    s = sub.add_parser("status", allow_abbrev=False, help="the lock, or one run or hold")
    s.add_argument("id", nargs="?")
    s.add_argument("--json", action="store_true", help="the API snapshot (docs/lock-api.md)")
    s.add_argument("--days", type=int, default=7, help="history stats over the last N days")
    s.add_argument("--registry", help=argparse.SUPPRESS)    # tests: a load node registry file
    t = sub.add_parser("stop", allow_abbrev=False, help="end a run")
    t.add_argument("id")
    t.add_argument("--force", action="store_true",
                   help="kept for old scripts; ending another session's run is the owner's "
                        "and needs no flag (an agent is refused, exit 2)")
    o = sub.add_parser("top", allow_abbrev=False,
                       help="the owner's: move a waiting run to the front of the queue (runs "
                            "holding slots keep them)")
    o.add_argument("id")
    h = sub.add_parser("hold", allow_abbrev=False, help="holds")
    hs = h.add_subparsers(dest="hold_cmd", required=True)
    x = hs.add_parser("whole", allow_abbrev=False, help="the owner's: every slot, for the "
                      "owner's own runs")
    window_args(x)
    x = hs.add_parser("half", allow_abbrev=False, help="the owner's: half the slots")
    window_args(x)
    x = hs.add_parser("slots", allow_abbrev=False, help="the owner's: the slots named")
    x.add_argument("--cpu", type=parse_slots, default=[], metavar="LIST")
    x.add_argument("--gpu", type=parse_slots, default=[], metavar="LIST")
    window_args(x)
    x = hs.add_parser("agent", allow_abbrev=False, help="keep the slots a run needs (an agent's)")
    x.add_argument("--repo", required=True)
    x.add_argument("--slug", required=True)
    x.add_argument("--kind", required=True)
    x.add_argument("--gpu-share", choices=("training",))
    window_args(x)
    x = hs.add_parser("end", allow_abbrev=False, help="end a hold now")
    x.add_argument("id")
    x = hs.add_parser("edit", allow_abbrev=False, help="change a live hold")
    x.add_argument("id")
    x.add_argument("--from", dest="start", type=parse_hhmm, metavar="HH:MM")
    x.add_argument("--date", type=parse_date, metavar="YYYY-MM-DD")
    e = x.add_mutually_exclusive_group()
    e.add_argument("--until", type=parse_hhmm, metavar="HH:MM", help="end at the next such time")
    e.add_argument("--for", dest="dur", type=parse_duration, metavar="D", help="end D from now")
    x.add_argument("--note")
    hs.add_parser("list", allow_abbrev=False, help="the live and upcoming holds")
    d = sub.add_parser("division", allow_abbrev=False, help="show or set the owner's division")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--set", type=parse_json, metavar="JSON", help="the division (docs/lock-api.md)")
    g.add_argument("--default", action="store_true", help="every key its default")
    m = sub.add_parser("game", allow_abbrev=False, help="game mode")
    m.add_argument("action", nargs="?", choices=("show", "on", "off", "set"), default="show")
    m.add_argument("games", nargs="?", type=parse_json, metavar="JSON",
                   help="with set: [{\"exe\": \"Game.exe\", \"pin\": false, \"name\": \"...\"}]")
    for p in [ap] + list(sub.choices.values()) + list(hs.choices.values()):
        p.add_argument("--test-root", dest="test_root_%d" % id(p), help=argparse.SUPPRESS)
        p.add_argument("--test-hardware", dest="test_hw_%d" % id(p), help=argparse.SUPPRESS)
        p.add_argument("--test-kinds", dest="test_kinds_%d" % id(p), help=argparse.SUPPRESS)
        p.add_argument("--test-actor", dest="test_actor_%d" % id(p), choices=("owner", "agent"),
                       help=argparse.SUPPRESS)
    return ap


def _hidden(a, name):
    vals = [v for k, v in vars(a).items() if k.startswith(name + "_") and v]
    return vals[-1] if vals else None


def make_store(a):
    """The machine's store, or (tests) a scratch one with fake hardware and its own kinds."""
    root, hw_file, kinds = (_hidden(a, n) for n in ("test_root", "test_hw", "test_kinds"))
    hw = None
    if hw_file:
        with open(hw_file, encoding="utf-8") as f:
            hw = json.load(f)
    return Store(root=os.path.abspath(root) if root else None, say=say, hardware=hw,
                 kinds_path=kinds)


def actor(a, store):
    """Who is asking, from this process's tree (store.caller): an agent host anywhere above
    it makes it an agent, whatever its environment. Tests only: --test-actor owner|agent stands in
    for it on a scratch store (--test-root); the machine's store refuses the flag."""
    me = caller(name="machine_lock.py")
    test = _hidden(a, "test_actor")
    if test:
        if not store.testing:
            raise LockError(EXIT_USAGE, "usage: --test-actor needs --test-root")
        me = dict(me, role=test, host=None if test == "owner" else me["host"])
    return me


def api_result(r, ok_text=None):
    if not r["ok"]:
        say("refused: %s" % r["error"]["message"])
        return API_EXIT.get(r["error"]["code"], 1)
    if ok_text:
        out(ok_text(r["result"]))
    return 0


# ------------------------------------------------------------------ detach
def detach(argv):
    """The same run started again through Win32_Process.Create, outside this process tree, so it
    outlives the shell that started it, under the windowless carrier (kitlib.lock.detach). Prints
    '<run id> pid <wrapper pid>' (the carrier's pid when its python was not seen in time)."""
    rid = new_id("r")
    i = argv.index("--")
    child = dt.command(sys.executable, TOOL, argv[:i], rid, argv[i:])
    why = dt.missing(child)
    if why:
        say("could not start the command: --detach: %s" % why)
        return 1
    env = dict(os.environ)
    try:
        env[ORIGIN_ENV] = ";".join("%d:%d" % pc for pc in sorted(process.own_ancestors()))
    except Exception:
        env.pop(ORIGIN_ENV, None)
    pid, why = dt.create(child, os.getcwd(), env)
    if pid is None:
        say("could not start the command: --detach %s" % why)
        return 1
    print("%s pid %s" % (rid, dt.wrapper_pid(pid) or pid), flush=True)
    return 0


# ------------------------------------------------------------------ commands
def cmd_run(a, cmd, store, argv):
    if not cmd:
        say("usage: machine_lock.py %s ... -- <command>: no command after --" % a.cmd)
        return EXIT_USAGE
    resource = getattr(a, "resource", None)     # run only; before a detach books anything
    check_resource(resource)                    # "" is usage here, not in a detached log
    if resource and resource.startswith("landing:"):
        say("usage: --resource %s: landing resources belong to land.py (kitlib.gate.land); "
            "name another resource" % resource)
        return EXIT_USAGE
    try:                            # before a detach, whose log is the only place it would show
        bash.resolve(cmd)
        script_command(cmd)         # run and background both start a .bat through its line
    except (bash.BareBashError, ScriptArgError) as e:
        say("could not start the command: %s" % e)
        return 1
    node = getattr(a, "node", "main")           # run only
    if node not in ("auto", "main") and (a.detach or a.detached_id):
        say("usage: --node %s: a run on a load node does not detach; follow it with node.py "
            "follow, or drop --node" % node)
        return EXIT_USAGE
    if a.detach and not a.detached_id:
        return detach(argv)
    common = dict(store=store, rid=a.detached_id, detached=bool(a.detached_id),
                  expect_s=a.expect_s, note=a.note)
    if a.cmd == "background":
        return run_background(cmd, a.repo, a.slug, **common)
    router = dispatch.Router(node, a.artifact, registry_file=a.registry, out=job_out)
    return run_command(cmd, a.kind, a.repo, a.slug, cores=a.cores, hold=a.hold,
                       gpu_share=a.gpu_share, wait_s=a.wait_s, no_wait=a.no_wait,
                       on_release_cmd=a.on_release, within=a.within, resource=a.resource,
                       route=router, **common)


def job_out(data):
    """A load node job's own output, as it comes."""
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()
    else:
        buf.write(data)
        buf.flush()


def cmd_status(a, store):
    if a.id:
        d = (api.hold_record if a.id.startswith("h-") else api.run_record)(a.id, store)
        if d and d.get("ok") is False:
            return api_result(d)
        if not d:
            out("no record of %s %s" % ("hold" if a.id.startswith("h-") else "run", a.id))
            return 1
        print(json.dumps(d, indent=1) if a.json else "\n".join(st.record_lines(d)), flush=True)
        return 0
    snap = api.snapshot(store, history_days=a.days)
    if a.json:
        print(json.dumps(snap, indent=1), flush=True)
    else:
        for line in st.render(snap):
            out(line)
        warn = path_alias_line()
        if warn:
            out(warn)
        for line in dispatch.status_lines(a.registry, testing=store.testing):
            out(line)
    return 0


def cmd_stop(a, store):
    r = api.end_run(actor(a, store), a.id, store)
    if not r["ok"]:
        return api_result(r)
    budget = store.settings.stop_s
    limit, hook = time.monotonic() + budget, None
    while time.monotonic() < limit:
        d = api.run_record(a.id, store) or {}
        if not d.get("live"):
            out("run %s stopped%s" % (a.id, "" if hook is None else
                                      " (its --on-release command took %d s)"
                                      % (time.monotonic() - hook)))
            return 0
        if hook is None and d.get("releasing"):
            hook = time.monotonic()
            s = float(d.get("release_s") or store.settings.on_release_s)
            out("run %s's command has ended; its --on-release command is running (at most %d s)"
                % (a.id, s))
            limit = max(limit, hook + s + 10)
        time.sleep(0.5)
    out("run %s was asked to stop and has not ended after %d s%s" % (
        a.id, budget, "" if hook is None else " plus its --on-release command's time"))
    return 1


def cmd_top(a, store):
    return api_result(api.move_to_top(actor(a, store), a.id, store),
                      lambda res: "run %s moved to the top of the queue (position %s)" % (
                          res["id"], res["position"] or "not yet planned"))


def _window(a):
    return hl.window(utc_now(), a.start, a.until, a.dur, a.date)


def cmd_hold(a, store):
    who = actor(a, store)
    if a.hold_cmd == "list":
        snap = api.snapshot(store)
        for h in snap["holds"]:
            out(st.hold_line(h))
        if snap.get("holds_error"):
            out(snap["holds_error"])
            return 1
        if not snap["holds"]:
            out("holds: none")
        return 0
    if a.hold_cmd == "end":
        return api_result(api.hold_end(who, a.id, store),
                          lambda res: "hold %s ended" % res["hold"]["id"])
    if a.hold_cmd == "edit":
        now = utc_now()
        c = {"note": a.note} if a.note is not None else {}
        if a.dur is not None:
            c["end"] = iso(now + datetime.timedelta(seconds=a.dur))
        elif a.until is not None:
            day = now.astimezone().date()
            e = hl.local_at(day, a.until)
            c["end"] = iso(e if e > now else hl.local_at(day + datetime.timedelta(days=1), a.until))
        if a.start is not None:
            c["start"] = iso(hl.local_at(a.date or now.astimezone().date(), a.start))
        elif a.date is not None:
            raise LockError(EXIT_USAGE, "usage: --date needs --from")
        return api_result(api.hold_edit(who, a.id, c, store),
                          lambda res: "changed: " + st.hold_line(res["hold"]))
    start, end = _window(a)
    spec = {"start": iso(start), "end": iso(end), "note": a.note}
    if a.hold_cmd == "agent":
        spec.update(owner="agent", scope="slots", repo=a.repo, slug=a.slug, kind=a.kind,
                    gpu_share=a.gpu_share)
    else:
        spec.update(owner="owner", scope=a.hold_cmd)
        if a.hold_cmd == "slots":
            spec["slots"] = {"cpu": a.cpu, "gpu": a.gpu}
    r = api.hold_create(who, spec, store)
    code = api_result(r, lambda res: "made: " + st.hold_line(res["hold"]))
    if r["ok"]:
        for n in r["result"]["notes"]:
            out(n)
    return code


def cmd_division(a, store):
    if a.set is not None or a.default:
        r = api.set_division(actor(a, store), None if a.default else a.set, store)
        code = api_result(r, lambda res: "division written; %s" % (
            # applied: the layout in use is the wanted one; with a pending reason it is used
            # unwritten (layout.json invalid under live runs) or provisional (a failed look).
            "in use now, but %s" % res["pending"] if res["applied"] and res["pending"] else
            "in force now" if res["applied"] else
            "not in force: %s" % res["pending"]
            if res["pending"] and not st.waits_for_idle(res["pending"]) else
            "it applies once nothing is live or queued"))
        if code:
            return code
    snap = api.snapshot(store)
    L = snap["layout"]
    print(json.dumps({k: L[k] for k in ("division", "default_division", "wanted_division",
                                        "source", "warning", "hardware_change")}, indent=1),
          flush=True)
    return 0


def cmd_game(a, store):
    if a.action in ("on", "off"):
        code = api_result(api.set_game_mode(actor(a, store), a.action == "on", store),
                          lambda res: "game mode %s" % ("on" if res["enabled"] else "off"))
        if code:
            return code
    elif a.action == "set":
        if a.games is None:
            raise LockError(EXIT_USAGE, "usage: game set needs the list as JSON")
        code = api_result(api.set_games(actor(a, store), a.games, store),
                          lambda res: "%d game(s) listed" % len(res["games"]))
        if code:
            return code
    print(json.dumps(api.snapshot(store)["game"], indent=1), flush=True)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    i = argv.index("--") if "--" in argv else len(argv)
    head, cmd = argv[:i], argv[i + 1:]
    a = parser().parse_args(head)
    try:
        store = make_store(a)
        if a.cmd in ("run", "background"):
            return cmd_run(a, cmd, store, argv)
        if cmd:
            say("usage: only run and background take a command after --")
            return EXIT_USAGE
        return {"status": cmd_status, "stop": cmd_stop, "top": cmd_top, "hold": cmd_hold,
                "division": cmd_division, "game": cmd_game}[a.cmd](a, store)
    except LockError as e:
        say(str(e))
        return e.code
    except (OSError, ValueError) as e:
        say("require-lock: the lock store could not be read (%s); nothing was done" % e)
        return EXIT_REQUIRE


if __name__ == "__main__":
    sys.exit(main())
