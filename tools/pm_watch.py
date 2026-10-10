"""pm_watch.py: the PM watcher (docs/pm-watch.md). Silent until something needs the PM, then
prints one line per event and exits. The PM runs it as a background command and relaunches it
after handling the event, so no model turn is spent while nothing happens.

Events:
  BRANCH <repo> <ref> <hash>    a watched remote branch got a new tip (--branch, repeatable)
  USAGE account N seven_day B%  weekly usage crossed a band in 95/98/99
  USAGE account N five_hour B% five-hour usage crossed a band in 95/98/99
  RESET account N               the weekly window reset (re-place coordinators, favour the sooner reset)
  BGFAIL run <id> <reason>      a machine_lock background run ended failed (alerts/<id>.json)
  LOCKNOTICE <id> <title>       a lock-window notice not yet posted to the Action Hub (--notice-dir);
                                post with claude/hub/hub_notices.py
  IDLE <name> <N>m since <iso>  a session in sessions.json (named, not autostart=false) has its own
                                main and subagent transcripts all older than --idle-minutes, found by
                                the custom-title record; once per quiet spell
  ERROR <what>                  a check failed 3 times running
State lives in the state file, so a relaunch never repeats an event it already reported. The first
run only records a baseline.
  python tools/pm_watch.py [--branch <repo-dir>=<ref> ...] [--usage-script <ps1>]
                           [--branch-poll 300] [--usage-poll 600] [--alerts-dir <dir>]
                           [--idle-minutes 20] [--idle-poll 300] [--sessions <json>]
                           [--notice-dir <dir>] [--state <json>] [--once]
--usage-script, --sessions, --alerts-dir and --state default to the "pm_watch" keys of
the machine config (usage_script, sessions_file, alerts_dir, state_file). With no usage
script, usage is read in this process (kitlib.launcher.usage, `launch.py usage`) from the accounts
of the config's "launcher" key; with neither a script nor that key it exits 2. A script must
print the same lines, "account 2: five_hour=39% ... seven_day=51% ...". --once polls once,
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
from claude import claude_host                                     # noqa: E402


def parse(argv, cfg, launcher_set=None):
    ap = argparse.ArgumentParser(description="The PM watcher.")
    ap.add_argument("--branch", action="append", default=[], metavar="REPO=REF")
    ap.add_argument("--usage-script", default=cfg.get("usage_script"))
    ap.add_argument("--branch-poll", type=int, default=300)
    ap.add_argument("--usage-poll", type=int, default=600)
    ap.add_argument("--alerts-dir", default=cfg.get("alerts_dir") or claude_host.default_alerts_dir())
    ap.add_argument("--idle-minutes", type=int, default=20)
    ap.add_argument("--idle-poll", type=int, default=300)
    ap.add_argument("--sessions", default=cfg.get("sessions_file"))
    ap.add_argument("--notice-dir", default=W.configured_notice_dir())
    ap.add_argument("--state", default=cfg.get("state_file") or W.default_state_file())
    ap.add_argument("--once", action="store_true", help="poll once, print events, exit 0")
    a = ap.parse_args(argv)
    if not a.sessions:
        ap.error("--sessions not given and pm_watch.sessions_file not in %s" % kuser.display())
    if not a.usage_script and not (W.launcher_configured() if launcher_set is None else launcher_set):
        ap.error("no usage source: --usage-script not given, pm_watch.usage_script not set, and "
                 "%s has no launcher key naming the accounts (docs/launcher.md "
                 "\"Settings\"); without one only the default ~/.claude account would be watched"
                 % kuser.display())
    return a


def main(argv=None, sleep=time.sleep, launcher_set=None):
    try:
        cfg = W.load_config()
    except kuser.ConfigError as e:
        print("pm_watch: machine config: %s" % e, file=sys.stderr)
        return 2
    a = parse(argv, cfg, launcher_set)
    w = W.Watcher(W.load_state(a.state) or {}, [tuple(b.split("=", 1)) for b in a.branch],
                  lambda: W.usage(a.usage_script), a.alerts_dir, a.sessions,
                  claude_host.newest_finder(), idle_minutes=a.idle_minutes,
                  branch_poll=a.branch_poll, usage_poll=a.usage_poll, idle_poll=a.idle_poll,
                  notice_dir=a.notice_dir)
    w.start(time.time())
    W.save_state(a.state, w.s)
    while True:
        events = w.poll(time.time())
        W.save_state(a.state, w.s)
        if events:
            print("\n".join(events), flush=True)
            return 0
        if a.once:
            return 0
        sleep(30)


if __name__ == "__main__":
    sys.exit(main())
