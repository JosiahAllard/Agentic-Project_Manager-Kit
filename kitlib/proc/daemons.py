"""Build daemons: the long-lived helpers a build starts and leaves running after it exits (a Gradle
or Kotlin daemon, an MSBuild node, the C#/VB compiler server). Found by image name and command
line, the same tests optional/procwatch uses. A process that matches none is not a daemon here:
ComfyUI, a game server or any other program a run's command left is never matched.
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


def stop(found, kill_tree=None):
    """End each found daemon with its own children (ctime-checked). [(pid, label, result)], the
    result being terminate_verified's for the daemon itself."""
    if kill_tree is None:
        from . import process
        kill_tree = process.kill_tree
    out = []
    for pid, ct, label in found:
        res = kill_tree(pid, ct)
        out.append((pid, label, res.get(pid, "gone") if isinstance(res, dict) else res))
    return out
