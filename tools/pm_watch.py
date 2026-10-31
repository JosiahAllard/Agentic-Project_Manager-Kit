"""pm_watch.py: the PM watcher (docs/pm-watch.md). Silent until something needs the PM, then
prints one line per event and exits. The PM runs it as a background command without --stream and
relaunches it at once when it exits, then handles the events, so no model turn is spent while
nothing happens and the heartbeat never goes stale. With --stream it never exits on an event: it
prints each one and keeps polling, for a caller that reads its output line by line; the PM's
background run does not use it (the PM start check warns on it). Every pass but a --once one writes a heartbeat, <state>.beat
({pid, beat, stream, started}), that the PM session-start check reads (docs/pm-start.md).

Events:
  BRANCH <repo> <ref> <hash>    a watched remote branch got a new tip (--branch, repeatable)
  USAGE account N seven_day B%  weekly usage crossed a band in 95/98/99
  USAGE account N five_hour B% five-hour usage crossed a band in 95/98/99
  RESET account N               the weekly window reset (re-place coordinators, favour the sooner reset)
  BGFAIL run <id> <reason>      a machine_lock background run ended failed (alerts/<id>.json)
  LOCKNOTICE <id> <title>       a new lock-window notice (--notice-dir); the hub server lists it
                                (docs/action-hub.md "Lock-window notices")
  IDLE <name> <N>m since <iso>  a session in sessions.json (named, not autostart=false) has its own
                                main and subagent transcripts all older than --idle-minutes, found by
                                the custom-title record; once per quiet spell
  STOPPED <name> at <iso>       a session in sessions.json that launch.py stop ended with no
                                successor (the launcher's stop.log), with no transcript activity
                                since; once per stop, and never IDLE for it
                                (docs/pm-watch.md "Stopped sessions")
  LIMIT <name> account N stalled at the weekly limit until <iso> (pid P)
                                a session stopped at its account's weekly limit, its process still
                                running; once per stall, and never IDLE for it (docs/pm-watch.md)
  PLUGIN pm-kit plugin behind kit main <sha12>: python tools/plugin_refresh.py --apply
                                the plugin export is not at the kit's refs/remotes/<remote>/main;
                                once per main sha, reported on the first run too; with
                                plugin.auto_refresh on it also starts the refresh
                                (docs/plugin-source.md "Behind warnings")
  REMOTE <name> link lost: ...  a running session in sessions.json (or the PM's) started with
                                --remote-control reads unlinked in the session registry; once
                                per episode, and REMOTE <name> link back when it reads linked again;
                                reported on the first run too (docs/pm-watch.md "Remote Control link")
  ERROR <what>                  a check failed 3 times running
While the hub's session-watch job is up (GET health jobs: no failure, a run within 3 periods) and
watches the same sessions file, alerts folder and notice folder, the session checks (USAGE, RESET, BGFAIL, LOCKNOTICE, IDLE, STOPPED, LIMIT, REMOTE and
their ERRORs) print nothing: the job pushes them; the heartbeat says so ("yielded"), and BRANCH,
PLUGIN and HUB stay here (docs/pm-watch.md "Hub session-watch job").
State lives in the state file, so a relaunch never repeats an event it already reported. The first
run only records a baseline, except for PLUGIN and REMOTE, which report what they find then.
  python tools/pm_watch.py [--branch <repo-dir>=<ref> ...]
                           [--branch-poll 300] [--usage-poll 600] [--alerts-dir <dir>]
                           [--idle-minutes 20] [--idle-poll 300] [--sessions <json>]
                           [--notice-dir <dir>] [--state <json>] [--once | --stream]
--sessions, --alerts-dir and --state default to the "pm_watch" keys of
the machine config (sessions_file, alerts_dir, state_file). Usage is read in this process
(kitlib.launcher.usage, `launch.py usage`) from the accounts of the config's "launcher" key;
without that key it exits 2. The retired --usage-script flag and pm_watch.usage_script key
are refused with exit 2. --once polls once,
prints any events and exits 0 (for checks; the PM's background run omits it). --notice-dir
defaults to machine_lock.notice.dir of the same file; unset, no LOCKNOTICE check runs.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import pmwatch as W                                     # noqa: E402
from kitlib.config import user as kuser                            # noqa: E402
from hosts.claude.adapter import claude_host                        # noqa: E402


RETIRED_USAGE_SCRIPT = ("--usage-script and pm_watch.usage_script are retired: usage is read in this "
                        "process from the launcher settings (kitlib.launcher.usage); remove the "
                        "flag or the key")


def parse(argv, cfg, launcher_set=None):
    ap = argparse.ArgumentParser(description="The PM watcher.")
    ap.add_argument("--branch", action="append", default=[], metavar="REPO=REF")
    ap.add_argument("--usage-script", default=None, help=argparse.SUPPRESS)   # retired: refused below
    ap.add_argument("--branch-poll", type=int, default=300)
    ap.add_argument("--usage-poll", type=int, default=600)
    ap.add_argument("--alerts-dir", default=cfg.get("alerts_dir") or claude_host.default_alerts_dir())
    ap.add_argument("--idle-minutes", type=int, default=20)
    ap.add_argument("--idle-poll", type=int, default=300)
    ap.add_argument("--sessions", default=cfg.get("sessions_file"))
    ap.add_argument("--notice-dir", default=W.configured_notice_dir())
    ap.add_argument("--state", default=cfg.get("state_file") or W.default_state_file())
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="poll once, print events, exit 0")
    mode.add_argument("--stream", action="store_true",
                      help="never exit on an event: print each and keep polling (not the PM's "
                           "background run, which exits with its events)")
    a = ap.parse_args(argv)
    if a.usage_script is not None or "usage_script" in cfg:
        ap.error(RETIRED_USAGE_SCRIPT)
    if not a.sessions:
        ap.error("--sessions not given and pm_watch.sessions_file not in %s" % kuser.display())
    if not (W.launcher_configured() if launcher_set is None else launcher_set):
        ap.error("no usage source: %s has no launcher key naming the accounts (docs/launcher.md "
                 "\"Settings\"); without one only the default ~/.claude account would be watched"
                 % kuser.display())
    return a


def main(argv=None, sleep=time.sleep, launcher_set=None, plugin=None, hub=None, rc=None,
         yield_to=None):
    """`plugin`: the PLUGIN check (None: W.plugin_check() over the machine config; False: none).
    `hub`: the HUB check (None: W.hub_check() over the hub server on this machine; False: none).
    `rc`: the REMOTE check (None: W.rc_check() over the session registry; False: none).
    `yield_to`: the session checks' step-aside test (None: W.session_yield() over this run's
    sessions file, alerts and notice folders against the job's, W.job_inputs; False: never)."""
    try:
        cfg = W.load_config()
    except kuser.ConfigError as e:
        print("pm_watch: machine config: %s" % e, file=sys.stderr)
        return 2
    a = parse(argv, cfg, launcher_set)
    host = claude_host                  # the watcher's host seam: transcripts, registry, preloads
    w = W.Watcher(W.load_state(a.state) or {}, [tuple(b.split("=", 1)) for b in a.branch],
                  W.usage, a.alerts_dir, a.sessions,
                  host.newest_finder(), idle_minutes=a.idle_minutes,
                  branch_poll=a.branch_poll, usage_poll=a.usage_poll, idle_poll=a.idle_poll,
                  notice_dir=a.notice_dir,
                  plugin=W.plugin_check() if plugin is None else (plugin or None),
                  hub=W.hub_check() if hub is None else (hub or None),
                  rc=W.rc_check(a.sessions, host.rc_records) if rc is None else (rc or None),
                  preload_modules=host.PRELOAD,
                  yield_to=W.session_yield((a.sessions, a.alerts_dir, a.notice_dir),
                                           W.job_inputs(cfg, host.default_alerts_dir(),
                                                        W.configured_notice_dir()))
                  if yield_to is None else (yield_to or None))
    beat, started = W.heartbeat_file(a.state), W.own_start_time()
    return W.run(w, time.time, sleep, lambda s: W.save_state(a.state, s),
                 lambda text: print(text, flush=True),
                 lambda now: W.write_heartbeat(beat, os.getpid(), now, a.stream, started,
                                               yielded=w.yielded),
                 once=a.once, stream=a.stream)


if __name__ == "__main__":
    sys.exit(main())
