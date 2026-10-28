"""Build daemons: the long-lived helpers a build starts and leaves running after it exits (a Gradle
or Kotlin daemon, an MSBuild node, the C#/VB compiler server). Found by image name and command
line, the same tests optional/procwatch uses. A process that matches none is not a daemon here:
ComfyUI, a game server or any other program a run's command left is never matched.

A daemon is shared: one a run's client started (so it sits in that run's job) is reused by any
other client on the machine through the build tool's registry (Gradle's, under GRADLE_USER_HOME).
So stop() ends a daemon only when every process it would end talks over TCP to processes in the
releasing run's job alone, none of them in the tree of a daemon it leaves running (why_left).
Gradle and Kotlin daemons serve their clients over TCP on this machine (a loopback address, or one
of the machine's own interface addresses), which the connection table shows (kitlib.proc.tcp
peers). MSBuild nodes and the compiler server use named pipes, whose client cannot be told from
outside, so they are always left.
"""
import re

# (label, image names it may have (empty: any), command-line test)
PATTERNS = (
    ("gradle-daemon", (), re.compile(r"gradledaemon", re.I)),
    ("kotlin-daemon", (), re.compile(r"kotlincompiledaemon", re.I)),
    ("msbuild-node", ("dotnet.exe", "msbuild.exe"), re.compile(r"[/-]nodemode:", re.I)),
    ("compiler-server", ("vbcscompiler.exe",), None),
    ("compiler-server", ("dotnet.exe",), re.compile(r"vbcscompiler\.dll", re.I)),
)

# Daemons whose clients reach them over named pipes (an MSBuild node, the compiler server): which
# process a pipe serves cannot be read from outside, so these are never stopped (why_left).
PIPE_LABELS = ("msbuild-node", "compiler-server")


def label_of(name, cmd):
    """The daemon label of a process named `name` (its image file name) with command line `cmd`,
    or None."""
    name = (name or "").lower()
    cmd = cmd or ""
    for label, names, test in PATTERNS:
        if names and name not in names:
            continue
        if test is None or test.search(cmd):
            return label
    return None


def find(pids, snap=None, cmdline=None, ctime_of=None):
    """[(pid, ctime, label)] of the daemons among pids (a job's), each named by its creation time
    so no reused pid is touched later."""
    if snap is None or cmdline is None or ctime_of is None:
        from . import process               # Windows only; a caller passing all three needs none
        snap = process.snapshot() if snap is None else snap
        cmdline = cmdline or process.command_line
        ctime_of = ctime_of or process.proc_ctime
    names = {p: n for p, _pp, n in snap}
    out = []
    for pid in pids:
        ct = ctime_of(pid)
        if ct is None:
            continue
        label = label_of(names.get(pid, ""), cmdline(pid, ct))
        if label:
            out.append((pid, ct, label))
    return out


def why_left(pid, tree, conns, in_job, label=None, kept=None):
    """Why daemon `pid` (with `tree`, its descendants' pids) must be left running, or None when
    it may be stopped: every process that stopping it would end has only peers in the releasing
    run's job. conns: kitlib.proc.tcp.connections(); in_job(pid): "in", "out", "gone" or
    "unknown" (kitlib.proc.job Job.contains). A peer that is gone serves nobody; one whose job
    cannot be told, or a connection on this machine (kitlib.proc.tcp peers) with no peer in
    the table, leaves the daemon.
    kept: {pid: (daemon pid, label)} of the processes in the trees of daemons this release leaves
    running; a peer among them is in the job yet runs on, so it leaves this daemon too."""
    from . import tcp
    if label in PIPE_LABELS:
        return "its clients connect over named pipes, whose client process cannot be told"
    kept = kept or {}
    mine = {int(pid)} | {int(p) for p in tree}
    for p, peer in tcp.peers(mine, conns):
        if peer is None:
            return "pid %d has a connection on this machine whose other end the table does " \
                "not show" % p
        if peer in mine:
            continue
        if peer in kept:
            return "pid %d serves pid %d of the %s (pid %d), which is left running" \
                % (p, peer, kept[peer][1], kept[peer][0])
        where = in_job(peer)
        if where == "out":
            return "pid %d serves pid %d, which is outside the run's job" % (p, peer)
        if where not in ("in", "gone"):     # "unknown", or any answer in_job should not give
            return "pid %d is connected to pid %d, whose job cannot be told" % (p, peer)
    return None


def stop(found, in_job, terminate=None, snapshot=None, descendants=None, connections=None):
    """End each found daemon with its own children unless one of them serves a process outside
    the releasing run's job (why_left, in_job: Job.contains of that job). The process tree and
    connection table are read once and every daemon is judged on them before any is ended; the
    tree checked is the tree ended: exactly the (pid, ctime) pairs judged are ended, children
    first, each by terminate_verified, and the tree is never read again. So a judged child that
    has exited and whose pid a newer process holds is not hit ("reused"), and a process the
    daemon starts after the read is not ended. A pair two stopped trees share is ended once. A
    daemon left running keeps its tree running, so a daemon in that tree, or serving a process
    in it (a Kotlin daemon a left Gradle daemon compiles on), is left too. Whatever cannot be
    read leaves the daemon running. [(pid, label, result)]: terminate_verified's result for the
    daemon itself, or "left: <why>"."""
    from . import process, tcp            # Windows only; fakes stand in for each in tests
    terminate = terminate or process.terminate_verified
    snapshot = snapshot or process.snapshot
    descendants = descendants or process.descendants
    connections = connections or tcp.connections
    try:
        snap = snapshot()
        conns = connections()
    except Exception as e:                  # noqa: BLE001 - cannot tell: leave them all
        why = "its connections could not be read (%s)" % e
        return [(pid, label, "left: " + why) for pid, _ct, label in found]
    pairs, trees, why = {}, {}, {}          # pairs: the judged tree, parents before children
    for pid, ct, _label in found:
        try:
            pairs[pid] = [(int(pid), ct)] + [(int(p), c) for p, c in descendants(pid, ct, snap)]
        except Exception as e:              # noqa: BLE001
            pairs[pid] = [(int(pid), ct)]
            why[pid] = "its process tree could not be read (%s)" % e
        trees[pid] = {p for p, _c in pairs[pid]}
    asked = {}

    def where(p):                           # one question per peer, however many passes
        if p not in asked:
            asked[p] = in_job(p)
        return asked[p]
    changed = True
    while changed:                          # until no daemon newly left: leaving one can leave more
        changed = False
        kept = {}
        for pid, _ct, label in found:
            if pid in why:
                for p in trees[pid]:
                    kept.setdefault(p, (pid, label))
        for pid, _ct, label in found:
            if pid in why:
                continue
            held = sorted(p for p in trees[pid] if p in kept)
            if int(pid) in kept:
                d, dl = kept[int(pid)]
                w = "it runs in the tree of the %s (pid %d), which is left running" % (dl, d)
            elif held:
                d, dl = kept[held[0]]
                w = "ending its tree would end pid %d of the %s (pid %d), which is left " \
                    "running" % (held[0], dl, d)
            else:
                try:
                    w = why_left(pid, trees[pid] - {int(pid)}, conns, where, label, kept)
                except Exception as e:      # noqa: BLE001 - cannot tell: leave it
                    w = "its connections could not be read (%s)" % e
            if w:
                why[pid] = w
                changed = True
    out, ended = [], {}                     # ended: {(pid, ctime): terminate's result}
    for pid, ct, label in found:
        if pid in why:
            out.append((pid, label, "left: " + why[pid]))
            continue
        for pair in reversed(pairs[pid]):   # children first
            if pair not in ended:
                ended[pair] = terminate(*pair)
        out.append((pid, label, ended[(int(pid), ct)]))
    return out
