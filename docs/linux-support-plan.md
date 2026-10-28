# Linux support plan

Status: plan only (2026-10-25), revised 2026-10-26 to the owner's eleven answers (section 10).
Nothing here is built. It extends, and for Linux supersedes, the
section "6. Platform seam (later refactor: Linux and macOS)" of docs/multi-host-plan.md: that
section set the seam's shape and four questions; this plan verifies the Windows-only surface,
designs each backend, orders the work into branches and asks what only the owner can decide.

Goal (the owner's): the whole kit runs on Linux out of the box, on any Linux distribution, at
parity with Windows: the PM, coordinators, the launcher, the hub, the guard, the machine lock,
the gate, landings, scheduled jobs, machine setup, plugin refresh, skills and docs. Parity here
means without peer messaging and the bridge (after parity, question 7) and without macOS (out,
question 1). The first goal is the plugin working on Linux; CI for Linux and Windows is a later
phase (question 8). Windows behaviour does not change: every
slice below keeps the Windows code path byte-for-byte where it can and proves it with the
existing Windows suite.

Evidence labels: "verified" is read in the code at the named symbol; "inferred" is reasoned from
code without running it on Linux; "unknown" needs a probe on a Linux box, and the slice that
needs it says so.

## 1. Where things stand

### 1.1 The front doors refuse Linux

- tools/kit.py:main refuses `the kit runs on Windows only` off win32 (verified).
- kitlib/__init__.py installs the console hook only on nt (verified); kitlib/proc/__init__.py's
  docstring says "Windows only".
- kitlib/proc/win32.py builds `ctypes.WinDLL("kernel32")` (and ntdll, advapi32, iphlpapi) at
  import (verified). Every module that imports it fails to import on Linux: process, cpu, job,
  shield, winwatch, and through them lock/store, lock/run, lock/layout, records/inflight and the
  test pool. kitlib/proc/pipe.py does the same at import, and so does kitlib/gate/outfile.py (an
  unguarded `import msvcrt` and a module-level `ctypes.WinDLL('kernel32')`, the gate's claim on
  its output file). A module-level `from ctypes import wintypes` is in nine kitlib modules
  (gate/outfile, lock/layout, lock/store, proc/cpu, proc/pipe, proc/process, proc/shield,
  proc/win32, proc/winwatch); whether it imports on Linux is unknown (section 9).
- The committed Claude hook manifest (hosts/claude/shell/hooks/hooks.json) runs `pythonw`, and
  kitlib/host/hookmain.py keeps `INTERPRETERS = {'windows': 'pythonw'}`; hosts.json lists
  `"platforms": ["windows"]` for both hosts, with `.exe` process names (verified). Since slice 12:
  INTERPRETERS adds `'linux': 'python3'`, both hosts list windows and linux, and each renders
  hooks.linux.json beside its unchanged hooks.json.

### 1.2 The surface, counted

Counted on this branch on 2026-10-25 over the tracked Python files, `git ls-files '*.py'`: 1,264 files, 528 of
them outside tests/. Each row is the number of those files that `git grep -l -i -E '<pattern>'`
matches, the alternatives in the pattern column joined by `|`. The PM's first counts are in the last column; where they differ, this scan's
figure stands.

| Mechanism | Pattern | Files (all) | Outside tests | PM's count |
|---|---|---|---|---|
| Direct Win32 | windll, WinDLL, msvcrt, winreg, wintypes | 76 | 44 | ~40 |
| Job objects: API calls | AssignProcessToJobObject, CreateJobObject, SetInformationJobObject, QueryInformationJobObject | 4 | 3 | 8 |
| Job objects: mentioned | `job object` | 20 | 14 | |
| CPU affinity / cpu sets: API calls | SetProcessAffinityMask, SetThreadGroupAffinity, SetProcessDefaultCpuSets, GetSystemCpuSetInformation, SetThreadSelectedCpuSets | 3 | 3 | 8 |
| CPU affinity / cpu sets: mentioned | `affinity`, `cpu.?set` | 27 | 14 | |
| WMI / CIM | `Win32_`, Get-CimInstance, Invoke-CimMethod, wmic, wmiprvse | 15 | 7 | 17 |
| Named pipes | CreateNamedPipe, `named.pipe`, `\\.\\pipe` | 8 | 6 | 22 |
| Scheduled tasks | ScheduledTask, schtasks | 26 | 13 | 26 |
| PowerShell (spawned or written) | powershell, pwsh | 153 | 62 | 92 |
| Branches on the platform | `os\.name`, `sys\.platform` | 119 | 23 | ~107 |
| creationflags | creationflags | 66 | 22 | |
| Named mutexes | CreateMutex, OpenMutex, `named mutex` | 4 | 3 | |
| msvcrt | msvcrt | 6 | 5 | |

Of the 119 files that branch on the platform, 96 are in tests/; 68 test files carry a platform
skip (section 1.5). The 44 Direct Win32 files outside tests/ are 25 in kitlib, 18 in optional/
(14 of them the bridge) and 1 in hosts/ (the Claude peer worker). So the library code that needs
a POSIX backend is about 25 kitlib files: it concentrates in kitlib/proc, kitlib/lock,
kitlib/fs/treelock.py, kitlib/config, kitlib/launcher, kitlib/machinesetup and the
scheduled-task writers, with the bridge apart.

### 1.3 Mechanism by mechanism (verified unless marked)

- **Process identity** is (pid, FILETIME creation time): kitlib/proc/process.py snapshot
  (Toolhelp), parent_pid, proc_ctime, alive, ancestry (parent walk with ctime checks),
  descendants, command_line (NtQueryInformationProcess), memory_mb, terminate_verified,
  kill_tree. The lock's nest token (kitlib/lock/run.py NEST_RE `rid:pid:ctime`, nest_check)
  depends on it.
- **CPU topology**: kitlib/proc/cpu.py (GetLogicalProcessorInformationEx): cores (SMT siblings,
  EfficiencyClass), groups (gid = group*64 + bit), l3_domains, cpu_sets. mask_text already writes
  the Linux cpuset list form, and lock/run.py within_of already parses it.
- **Job objects**: kitlib/proc/job.py class Job: kill_on_close, narrow, repin, assign, start
  (CREATE_SUSPENDED, assign, resume), contains, usage (peak job memory), processes,
  tree_working_set, terminate, end, unpin. Never breakaway: MSYS bash breaks away by default.
  Users: lock/run.py Slot (the slot's CPU mask and memory), the give-back path (narrow/repin),
  kitlib/tooldeps/pool.py Worker (`Job(kill_on_close=True)`), kitlib/pytests/runner.py run_unit,
  records/inflight.py, the Claude peer writer.
- **Detach**: kitlib/lock/detach.py CREATE_PS asks WMI (Win32_Process Create through PowerShell)
  to start the run outside the caller's tree and job; gate/submit.py and tools/machine_lock.py
  `--detach` use it.
- **Shields**: shield.py shield (a DACL against Claude Code's tree kill), PidHold (handles that
  stop pid reuse), console.py (hooks CreateProcess so no console window flashes), winwatch.py
  (user32 window watch), spawn.py no_window_flags and pythonw_beside. The console and window ones
  have no Linux need (Linux opens no console windows). The shield and PidHold have none either
  (verified on the reference VM, section 8.1, with Claude Code 2.1.312 against a local stand-in
  API: a probe tree of the call's shell, a child in its group, a grandchild in a session of its
  own and one reparented away, each beating). A Bash call that finishes, or that passes its
  timeout (moved to the background) or runs in the background, ends with no kill at all, so the
  stale kill of a reused pid that the Windows shield answers never comes. Stopping a call
  (Escape, TaskStop, or SIGTERM or SIGINT to claude) does kill its tree by pid: claude lists
  every process (`ps -A -o pid= -o ppid=`), sends SIGTERM to the shell's process group and to
  each descendant by pid (the one in a session of its own too), polls every 100 ms and lists
  again about 1.35 s later. The kept capture ends there; that it then sends SIGKILL to the group
  and each one still running is inferred (the probe's members ignore SIGTERM, yet all ended,
  and the call returned exit code 137). Each kill seen comes at most about 200 ms after a fresh
  listing, and pids are handed out in order up to pid_max (4194304 on the VM), so a reused pid is
  out of reach. A process already reparented away from the call when claude lists (the kit's
  detach: a setsid double fork) is not reached; one still mid double fork at that moment is a
  descendant and is, and this holds only while claude is not a child subreaper. That kill is
  aimed at the live tree, as a stop is meant to be.
- **Machine lock store**: lock/store.py real_root is `~/.claude/machine-lock` under the profile
  from SHGetKnownFolderPath; lock() is a named mutex `Global\MachineLock.<name>` (queue, holds,
  resources, revision, games, run.<id>), WAIT_ABANDONED counting as acquired. RunMutex is the
  run's liveness signal: owned on its own thread, abandoned when the process dies (run_dead,
  run_live). caller walks the process tree for hosts.json `.exe` names; DETACHED_LAUNCHERS is
  `wmiprvse.exe`.
- **Hardware**: lock/layout.py detect returns one dict (threads, groups, cores, l3, cpu_sets,
  memory_mb, gpus, source, problems, failed); GPUs come from DXGI, then CIM through PowerShell.
  fake_hardware is the test seam. The division into slots (machine.json keys) is portable logic.
- **File locks**: kitlib/fs/treelock.py uses msvcrt.locking and raises "the tree lock needs
  Windows (msvcrt)" otherwise; it backs writelock.py, the hub store and the automatic-tasks tick.
- **Configuration**: config/pointer.py reads `HKLM\SOFTWARE\pm-kit` pm_repo and answers "no
  registry on this platform" off Windows, so every machine-config lookup fails on Linux.
  config/user.py real_profile has no fallback off Windows; system_temp branches; dev_drive is a
  plain string.
- **Scheduled tasks**, all `Register-ScheduledTask` running pythonw and tools/no_window.py:
  launcher/autostart.py (coordinators after reboot, at logon), hubsite/autostart.py (hub server,
  at logon), worktree/task.py and machinesetup/worktreesweep.py (worktree sweep, daily),
  machinesetup/agenttemp.py (agent-temp sweep, daily), machinesetup/extras.py auto_task_line
  (automatic tasks, every 15 minutes), machinesetup/procwatch.py (opt-in), the bridge's startup
  task (rendered only). pmstart/consoletasks.py read_tasks reads them back with `schtasks /Query
  /XML`.
- **Sessions**: launcher/window.py runs `conhost.exe powershell.exe -NoExit -EncodedCommand`;
  launcher/stop.py finds `claude.exe` by its `--remote-control` name; pmstart lists processes
  through Get-CimInstance; autotasks tick.launch passes CREATE_BREAKAWAY_FROM_JOB (an
  AttributeError on Linux, inferred).
- **Named pipes**: hosts/claude/adapter/peer.py talks to Claude Code's `\\.\pipe\` messaging
  address from `<config>/sessions/<pid>.json`; the bridge (optional/bridge/win/) owns a private
  pipe and a `Local\` mutex. Claude Code's own frames say `from: uds:`, so on Linux it likely
  uses a Unix socket (inferred; probe).
- **Environment and install**: extras.Host writes a desktop `.lnk`, sets user variables through
  the registry and calls icacls; kitcommand.py writes kit.cmd and the user PATH;
  delegations/run.py builds a child environment from HKLM/HKCU; links.py uses `cmd /c rmdir`.
  installer/machine-setup.ps1 only validates arguments and calls tools/machine_setup.py.
- **Fails closed today** (good, and must stay so until replaced): records/landing.py `_proc`
  (holder counts as alive), worktree/classify.py process_cwds, ledger/safepath.py owner check,
  guard/context.py `_default_lock_held` (heavy work never counts as held).
- **Fails open today**: worktree/holders.py blockers returns [] off Windows (inferred: a sweep
  would not see a process using the worktree). Listed in slice 9.

### 1.4 Shell text

`git grep -il powershell -- content` matches 4 of the 13 roles (coordinator, docs-writer, lander,
test-runner) and 1 of the 21 skills (shell-and-worktree-traps, 28 lines, all Windows-shell
traps). In three of those roles the only match is the `powershell` entry of the capabilities
list (a host tool, mapped by the renderer); lander also has one prose line. Windows-shell text
outside that skill, by `git grep -n -E 'Get-ChildItem|Select-Object|\$env:|cmd\.exe|cmd /c|Set-Location'
-- content ':!content/skills/shell-and-worktree-traps'`, is 10 lines: the shared root-search
example in five roles (analyst, coordinator, docs-writer, lander, test-runner), coordinator's
`$env:` line, lander's and machine-lock-run's `Select-Object` lines, branch-sync's
`Set-Location`/`cmd /c` line and mutation-testing's cmd.exe line. `git grep -il powershell --
docs` matches 25 docs. The content renderer (kitlib/host/content.py,
kitlib/content) has per-host tokens only ({{name}}, {{agent:}}, {{model:}}, {{tool:}}); there is
no per-platform text. The hub's owner to-do steps (hubsite/todosteps.py) render `{run:<command>}`
as PowerShell, following the owner's ruling that hand steps carry commands. Codex's dialect
default is 'powershell' (hosts/codex events.py).

### 1.5 Tests and CI

- No CI: no workflow, no .github folder; CI is a later phase (section 4).
- `git grep -c -E "skip(Unless|If)\((os\.name|sys\.platform)" -- '*.py'` finds 142 platform
  skips in 68 test files, with free-text reasons; `skip(Unless|If)\(WINDOWS` finds 13 more in 6
  files that skip through a module constant (`WINDOWS = os.name == 'nt'`).
- The kit's own runners need job objects: tools/kit_tests.py (the pool) and gate.py (the pool and
  the machine lock, which fails closed). A first Linux run cannot use either.
- tests/realgit.py `real_git(reason)` is the house pattern for a marker with a reasons table.

## 2. The platform layer

### 2.1 The seam

`kitlib/platform/` is the one place that knows the operating system.

```
kitlib/platform/
  __init__.py     picks the backend once from sys.platform; exports NAME ('windows'|'linux'|
                  'unsupported'); on any other platform (darwin included) there is no backend
                  folder and every primitive raises PlatformUnsupported naming sys.platform
  api.py          the contract: names, signatures, docstrings; no OS code
  windows/        re-exports today's kitlib/proc and kitlib/lock primitives unchanged
  linux/
    process.py    identity, ancestry, descendants, command line, memory, kill
    cpu.py        topology, L3, efficiency classes, affinity
    contain.py    the Job contract on cgroup v2
    locks.py      named locks, run liveness, file locks
    detach.py     start outside the caller's tree
    schedule.py   systemd user timers, or cron where there is no user systemd
    ipc.py        Unix sockets
    paths.py      profile, config, state and temp folders, the machine pointer
    hardware.py   /sys and /proc readers for lock/layout.detect
```

Rules:

1. Outside kitlib/platform, kitlib/proc's Windows modules and tests, no module reads os.name or
   sys.platform. A test (tests/platform/test_no_os_checks.py) enforces it with an allowlist of the
   23 files outside tests/ that branch today (section 1.2); the allowlist only shrinks.
2. The Windows backend starts as re-exports, so moving a caller onto the seam changes no Windows
   behaviour; the Windows suite proves each move.
3. No module imports a Windows DLL at import time. proc/win32.py, proc/pipe.py and
   gate/outfile.py bind lazily (first call), and the six other modules with a module-level
   `from ctypes import wintypes` (lock/layout.py, lock/store.py, proc/cpu.py, proc/process.py,
   proc/shield.py, proc/winwatch.py) take it lazily or behind a guard, so an import on Linux
   works and a call fails with a clear "not on this platform" error. On Windows this moves one failure: a DLL or export that is
   missing fails today at import, and after the change at first call; slice 2 proves that the
   Windows imports still succeed and that a missing export still raises, naming the function.
4. Guard path text goes through one function per shell (kitlib/guard/paths.py norm and the
   shell's resolve_path); hook commands come from one helper (hookmain.manifest_command), as the
   multi-host plan already ruled.
5. A Linux primitive that cannot give the Windows guarantee says so in its name or raises; it never
   silently does less (see 2.3 and owner question 4, answered).

### 2.2 Windows mechanism to Linux mechanism

| Windows | Linux | Notes |
|---|---|---|
| (pid, FILETIME ctime) identity | (pid, starttime from /proc/<pid>/stat field 22) plus boot_id | starttime is in clock ticks since boot, so it repeats across reboots; the seam folds the boot into the ctime value (below) |
| Toolhelp parent walk | /proc/<pid>/stat ppid, checked against each starttime | same ancestry rule |
| NtQueryInformationProcess command line | /proc/<pid>/cmdline | NUL-separated, no CRT re-quoting |
| Job object (kill on close, CPU mask, memory, peak) | cgroup v2 leaf under a delegated subtree, or a lower tier of the ladder | see 2.3 |
| SetProcessAffinityMask / cpu sets | os.sched_setaffinity | for the pool and W2 (the weak tier of the ladder) only; the slot's mask lives in cpuset.cpus |
| WMI Win32_Process Create (detach) | `systemd-run --user --scope` with user systemd, else `setsid` (double fork) where there is no systemd | systemd-run also places the run in its own cgroup |
| Named mutex, WAIT_ABANDONED | fcntl.flock on a lock file in the store, opened O_CLOEXEC | the kernel drops it when the holder dies: the same "abandoned counts as free" meaning |
| RunMutex (liveness) | the run holds flock on `runs/<id>.alive`; readers probe with LOCK_NB | run_dead and run_live keep their meaning |
| msvcrt.locking (treelock) | fcntl.flock on the same lock file | the whole file: Linux locks are advisory, so readers are never blocked; lockf is per process (slice 3) |
| Register-ScheduledTask | systemd user `.timer` + `.service` units; loginctl enable-linger only where user systemd exists | cron (`crontab -l`/`crontab -`, `@reboot`) is first-class where there is no user systemd (question 5) |
| Task at logon (the hub server start) | an XDG autostart `.desktop` entry in `~/.config/autostart`; a shell-profile line as the fallback where there is no desktop session | settled at plan review; cron has no logon form |
| schtasks /Query /XML | `systemctl --user list-timers --output=json`, unit files | consoletasks reads the kit's own unit names |
| Named pipes | AF_UNIX stream sockets, mode 0600, SO_PEERCRED | in the XDG runtime folder |
| GetExtendedTcpTable | /proc/net/tcp and tcp6 | the parsers after the table are portable |
| Registry pointer (HKLM pm-kit) | /etc/pm-kit/pointer, else `$XDG_CONFIG_HOME/pm-kit/pointer` | one line: the PM repository path |
| SHGetKnownFolderPath profile | pwd.getpwuid(os.getuid()).pw_dir | never $HOME alone (a session can change it) |
| User environment in the registry | `~/.config/environment.d/pm-kit.conf` and a profile.d snippet | |
| kit.cmd on the user PATH | `~/.local/bin/kit` shell script | |
| icacls | chmod 0700 / 0600 | |
| Desktop `.lnk` | `.desktop` file in `~/.local/share/applications` | |
| pythonw, CREATE_NO_WINDOW, console hook, winwatch | nothing: Linux spawns no console windows | the no-window carrier is a no-op |
| pid shield, PidHold | nothing needed (verified); pidfd where a hold is wanted | Claude Code on Linux kills no finished call's tree; a stopped call's tree it kills by pid from a fresh `ps` listing (section 1.3) |
| Git Bash lookup | /bin/bash | kitlib/proc/bash.py answers /bin/bash |
| conhost + PowerShell session window | tmux session by default, a configured terminal command if set | question 6 |
| Get-CimInstance Win32_Process | /proc scan | pmstart, launcher/stop |
| `.exe` process names in hosts.json | per-platform names (`claude`, `node`, `codex`) | hosts.json gains a per-platform field in slice 2: lock/store.py agent_hosts (the lock's caller walk) matches these names |

The nest token today is three fields: lock/run.py (Slot, into MACHINE_LOCK_RUN) and
lock/background.py (into MACHINE_LOCK_BG) write `"%s:%d:%d" % (run.id, pid, ctime)`, and
lock/run.py NEST_RE is `^(<run id>):(\d+):(\d+)$` (verified). The run record's ctime, the
token's ctime and every liveness check compare the same opaque integer: nest_check (lock/run.py),
process.alive(pid, ctime) (proc/process.py), and lock/store.py wrapper_state and the run-record
liveness reads that call alive or wrapper_state (verified). So the boot goes into the value, not
beside it: on Linux the seam's ctime is `(boot << 64) | starttime`, where `boot` is
/proc/sys/kernel/random/boot_id read as one 128-bit integer (its 32 hex digits, dashes
removed). Two processes of different boots never share a ctime, so a record or token written
before a reboot names no live process after it, and every caller above refuses it with no change
to its code. Within one boot the order is starttime's, so the ordering uses stay right: the
end-of-run pass (lock/run.py end_sparing, which ages processes against a stamp taken before
listing them) and winwatch's parent check (a parent's ctime is not after its child's). The seam's
stamp, today lock/run.py filetime_now, returns the same encoding on Linux: the current boot
shifted, or-ed with the ticks since boot. starttime counts clock ticks (10 ms at the usual 100 Hz),
so two processes can share one; a tie errs safe in both ordering uses (a process stamped in the
stamp's own tick is not aged; an equal parent is still a parent), as Windows's coarse FILETIME
does today. The token stays three fields, NEST_RE is unchanged, Windows's ctime is still the
FILETIME byte for byte, and the format comments in lock/run.py, lock/background.py and
lock/bg_client.py (`<run id>:<pid>:<ctime>`) stay true. A search for fixed-width uses of ctime
(struct packing, c_ulonglong, conversion to a time) in kitlib, hosts and tools found none, so a
value wider than 64 bits reaches only Python ints, `%d` and JSON, which all take it.

### 2.3 The Job contract on Linux

The lock's promises rest on job objects: a slot's processes stay on its CPUs and under its
memory, the whole tree dies with the slot, and no child gets out of it by the means ordinary
tools use. On Linux the nearest mechanism is a cgroup v2 leaf the kit owns. The threat model it
must meet: a child that calls setsid, double forks or resets its own affinity, as build tools and
daemons do; not a same-user process that sets out to leave its cgroup.

The owner's answer (question 4): heavy runs must work on Linux, with full cgroup v2 containment
on native Linux, and where nothing stronger exists (Docker included) they go ahead on weak
containment, shown as such; the owner accepts that risk. The research below is that answer's
precondition, and it finds that full containment on native Linux needs one root step at machine
setup (section 6). Settled at plan review: the root step is offered at setup and declining it is
allowed; a box that declines is S2 (or W2 off systemd), shown as such, and still counts as "out
of the box". Heavy runs on weak containment are supported, as the owner answered; that direction
is not reopened. A default Docker container probes to its lower tier and is shown so; the plan
never claims strong containment in a container (the proof is slice 14).

Research, 2026-10-26 (read from documentation, nothing probed on a Linux box). Without root and
without cpuset delegation nothing gives full containment: no CPU pinning a child cannot undo, and
no guaranteed kill. systemd's default user manager delegates only pids, memory and cpu to the user
(user@.service.in: `Delegate=pids memory cpu`).

1. A `systemd-run --user --scope` or `--slice`: with no root, MemoryMax, CPUQuota and CPUWeight,
   pids limits and kill-the-unit work; `AllowedCPUs` needs the cpuset controller and is expected
   to have no effect (a probe settles it). With a root drop-in, `user@.service.d` setting
   `Delegate=cpuset cpu io memory pids`, and a re-login, it is full. It fails without user systemd
   (non-systemd distributions, containers, a bare ssh login with no PAM session) and on cgroup v1
   (deprecated in systemd v256, removed in v258). Detect: `user@$UID.service/cgroup.controllers`
   lists cpuset and memory, then start a probe scope and read back `cpuset.cpus.effective` and
   `memory.max`; never trust the exit code.
2. A user plus cgroup namespace (`unshare -U -C`): a cgroup namespace changes only the view and
   grants nothing (cgroup_namespaces(7)); not a candidate. Its neighbour, a PID namespace, kills
   every member with SIGKILL when its init dies, with no escape (pid_namespaces(7)): the strongest
   kill without cgroups, and it needs no capabilities. It fails on Ubuntu 23.10 and later
   (AppArmor's user-namespace restriction) and under Docker's default seccomp profile, and the
   namespace's own pids reach the nest token. Detect: a trial `unshare(CLONE_NEWUSER |
   CLONE_NEWPID)` in a forked child.
3. The container's own cgroup: plain Docker mounts cgroups read-only; `--privileged` makes them
   writable, and the kit must move the container's processes into a leaf first (the
   no-internal-process rule). On plain Docker the kit can still read the container-wide
   `cpuset.cpus.effective` and `memory.max` for the layout (section 2.4). Detect: a read-write
   cgroup2 entry in mountinfo and W_OK on `cgroup.subtree_control`.
4. A pidfd plus `PR_SET_CHILD_SUBREAPER` watchdog: it catches double forks and setsid. It misses
   processes launched for the tree by servers outside it (systemd-run --user, D-Bus, an existing
   tmux server, at and cron, daemons already running), the same gap a Windows job has; it misses
   everything if the watchdog is itself SIGKILLed; and it gives no CPU pinning (affinity is
   cooperative) and no tree memory cap (RLIMIT_AS breaks V8, the JVM and Go, so it is dropped).
   The kill walks `/proc/<pid>/task/*/children`, SIGSTOPs, then `pidfd_send_signal` SIGKILL, in
   bounded passes like end_sparing's END_PASSES.
5. A stronger route that is found: hand delegation by root at boot, the kernel documentation's
   model. A root boot hook (OpenRC `local.d`, or a root `@reboot` cron) makes
   `/sys/fs/cgroup/pm-kit` owned by the kit user. It is the only strong route without systemd.
   `bwrap --unshare-pid --die-with-parent` is the same as the PID namespace.

The ladder, chosen by probing the machine at run time, not by distribution name:

| Tier | What it is | Strength |
|---|---|---|
| S1 | a writable cgroup2 subtree with cpuset, memory and pids and `cgroup.kill` (kernel 5.14+), from a `Delegate=yes` user scope or a root-delegated pm-kit directory | strong: CPU pinned, memory capped, guaranteed kill |
| S2 | the same without cpuset (systemd's default delegation) | memory and kill strong, CPU cooperative |
| W1 | a user and PID namespace whose pid 1 is the kit's reaper | partial: guaranteed kill, CPU cooperative, no memory cap |
| W2 | subreaper + pidfd + process group + affinity, with a sampled RSS watchdog and no RLIMIT_AS | weak |

`containment: weak` (proposed wording) means: heavy runs go ahead; the kit kills every descendant
it can see (subreaper, process group) at slot end; it cannot reach processes launched for it by
servers outside its tree; cleanup is lost if the holder is SIGKILLed; CPU pinning is advisory;
the memory cap is a sampled watchdog. The lock's status, the heavy-run start line and the run
record name the tier and what it lacks. Proposed: S2 shows `containment: partial (cpus not
pinned)` and is not counted weak.

W1, as built (contain.py NsJob): a python starter (Ubuntu's AppArmor user-namespace rule can be
met for the interpreter alone) reads its uid and gid, unshares a user and a PID namespace and maps
them onto themselves (read after the unshare they are the overflow id, and the map is refused),
then forks pid 1, the reaper, which mounts a fresh /proc in a mount namespace of its own and forks
the stub. Every process in the namespace dies with pid 1, so the kill is a pidfd SIGKILL to it,
and kill_on_close is pid 1 reading the owner's watch pipe. It is shown as `partial` with what it
lacks (CPU cooperative, no memory cap), not as strong. A pid is a number in one namespace: a lock
run record names its wrapper's (`pidns`), and a checker in another one asks the run lock instead
(store.record_state); a run nested in a W1 job of its run is found by the job's namespace on the
run's record (`jobns`, nest_check); a probe inside a W1 job runs no scope trial (the user systemd
would take its pids for its own namespace's) and is W2, never a nested W1, so a run nested at any
depth stays in the namespace on record.

Not yet namespace-aware (each safe today, a later record):

- queue entries and background runs carry no `pidns` and no `jobns` (on Linux the run lock
  decides a queue entry; bg_ancestor fails closed);
- resource.py, giveback.py and the lock status (api.py: tree, GPU memory) read a record's pid
  as this namespace's;
- the holders search does not look inside a W1 job.

The S1 contract:

- create: `systemd-run --user --scope --unit=pmkit-<rid> -p AllowedCPUs=<list> -p
  MemoryMax=<n>` for a fresh command, or mkdir of a leaf under the kit's delegated subtree for an
  existing process; assign writes the pid to cgroup.procs.
- start suspended: fork, join the cgroup in the child before exec (the child writes its own pid),
  so nothing runs outside it, which matches CREATE_SUSPENDED + assign + resume.
- narrow / repin (give-back): rewrite cpuset.cpus (or AllowedCPUs through systemctl set-property).
- contains, processes: cgroup.procs. usage: memory.peak (kernel 5.19+) or memory.max_usage
  fallback; tree_working_set: memory.current.
- terminate, end, kill_on_close: cgroup.kill (kernel 5.14+); kill_on_close is a watchdog that holds
  the cgroup and kills it when the owner's pidfd closes.
- Stragglers: on Windows, Slot.release leaves them running unpinned (job.end(kill=False),
  kitlib/lock/run.py:1236) and only KILL_HOW kills. On Linux a populated leaf cannot be removed
  and keeps its cpuset, so leftovers move into a kit `leftovers` sibling cgroup (this needs write
  access on the common ancestor), and end_sparing (run.py:708) can move a spared daemon out the
  same way.
- Escape, against that threat model: setsid and double fork do not move a process between
  cgroups, where they do leave a process group, so the leaf holds the tree that ordinary tools
  make. It is not a security boundary: a same-user process can write its own pid into any
  cgroup.procs it has write access to, which includes the rest of the user's delegated subtree.
  Here a job object is stricter (a process cannot leave its job, and the Job class never allows
  breakaway; the automatic-tasks tick, which launches with CREATE_BREAKAWAY_FROM_JOB on purpose,
  is the one exception, section 1.3). The plan accepts that difference and states it in the docs
  rather than defending against a deliberate move.

Sources: systemd `user@.service.in`; systemd.io/CGROUP_DELEGATION; systemd.resource-control(5);
docs.kernel.org/admin-guide/cgroup-v2.html; man7.org cgroup_namespaces(7), pid_namespaces(7) and
PR_SET_CHILD_SUBREAPER(2const); the Docker `docker run` and seccomp documentation; Ubuntu
Discourse on the AppArmor user-namespace restriction; Arch forum thread 262917 (AllowedCPUs).

### 2.4 Lock hardware detection on Linux

lock/layout.detect keeps its dict; a Linux reader fills it from /sys and /proc with
`source: {cpu: "linux", gpu: ...}`:

- threads: the allowed set, os.sched_getaffinity(0) intersected with
  /sys/devices/system/cpu/online. A box whose ids have gaps (offline or isolated CPUs) keeps its
  real ids; the division works on the id list, never on range(threads).
- cores: /sys/devices/system/cpu/cpuN/topology/core_cpus_list (thread_siblings_list on older
  kernels) groups SMT siblings; package id from physical_package_id.
- efficiency classes: hybrid parts expose /sys/devices/cpu_core/cpus and /sys/devices/cpu_atom/cpus;
  otherwise cpu_capacity where present; otherwise one class.
- l3: cpuN/cache/indexK where level is 3, shared_cpu_list gives each L3 domain.
- groups: none on Linux; the reader chunks the id list by 64 so callers that walk groups keep
  working. cpu_sets is [].
- memory_mb: MemTotal from /proc/meminfo; a cgroup memory.max on the kit's own slice caps it.
- gpus: /sys/class/drm/card*/device (vendor, device, mem_info_vram_total for AMD), NVIDIA's VRAM
  from `nvidia-smi --query-gpu=pci.bus_id,name,memory.total --format=csv,noheader` (matched to
  the card by bus id) or, name only, /proc/driver/nvidia;
  live use stays with api.gpu_use (nvidia-smi already).

No double-booking: unchanged in rule. One store per user (`<profile>/.claude/machine-lock`), a new
layout adopted only while nothing is live or queued, under the queue lock. Two gaps are Linux's
own. A container sees the host's CPUs in /sys while its cgroup allows fewer, so the allowed set
comes from sched_getaffinity and the cgroup's cpuset.cpus.effective, never from /sys alone; the
backend closes that one. Two kit users on one box would each get a store and book the same CPUs
twice. The owner's answer (question 10): one kit user per box, so there is no shared store.
Settled at plan review, how it is enforced: without the root step, setup checks per user and
warns on a second user; with the root step, a machine-wide marker makes setup refuse a second
user (section 6, step 9).

The rule for CPU sets, settled at plan review: one kit per CPU set. A kit in a container is
supported only as the only kit on that host (a container cannot see a host's kit at all), and
machine setup run inside a container warns and does not refuse. WSL double-books the same way,
so it stays refused until the later phase after native parity (question 11).

The proving test feeds four synthetic /sys and /proc trees, written by hand into the test data
(tests/lock/linux_trees.py) from the kernel's documented layout: a plain desktop, a hybrid part
(cpu_core and cpu_atom), a box with gaps in its CPU ids and a container whose cpuset allows
fewer CPUs than /sys lists. It checks that no two slots share an id. No real machine was recorded
for them, and none is needed for the division logic; the hybrid and gap trees are ones a live box
may not offer. The CI phase adds its runner's own live /sys as a fifth case.

## 3. Guard path semantics and the safety review

The guard is the kit's safety boundary, so its Linux work is High risk, lands alone, and gets a
strong-tier review against this list. All findings are inferred from code; each becomes a test
before the fix.

| # | Gap on Linux | Where | Effect |
|---|---|---|---|
| 1 | an absolute `/path` resolves to None when the cwd has no drive | guard/shell.py resolve_path; rules.py `_write_from`, git_jail | shell writes and git targets outside the worktree pass; `git -C /abs` and `cd /abs` only warn |
| 2 | `/` is never a root | rules.py is_root (drive-only), root_search; jail.own_root | `grep -r x /`, `du /` pass; a session at `/` would own `/` |
| 3 | ledger glob lowers the pattern, not the path, then compares case-sensitively | rules.py ledger_glob | a case-different path misses the ledger check |
| 4 | backslash kept literal in bash words | shell.py BASH_ESC and the lexer; commands.py program_alt covers program words only | `..` built with escapes in a write target |
| 5 | MSYS mapping (`/c/x` to a drive path) and the `/tmp` mapping | shell.py msys_to_win; rules.py write_place, jail.bash_tmp, BASH_TMP_RE | Linux paths placed as Windows ones |
| 6 | normcase is a no-op on POSIX | guard/paths.py norm | a case-folding mount (exFAT, a mounted Windows disk) under-matches |
| 7 | home variables are USERPROFILE/HOMEDRIVE/HOMEPATH only | shell.py HOME_VAR_RE, rules.py `_ROOT_NAMES`, commands.py HOME_ENV_NAMES | `$HOME` writes not judged as home |
| 8 | heavy-tool names lowered with `.exe` stripped | policy.py heavy_match | misses Linux names (godot4, AppImage, flatpak run) |
| 9 | machine-path scrub matches drive forms only | the machinepaths pattern | `/home/<name>` paths not caught in outgoing text |
| 10 | symlinks | jail.real, git/repo.py real, check_write use realpath | a link into the main checkout is judged as main (good); a swap between check and use is a known limit, as with junctions |

Already failing closed and to stay so: guard/context.py `_placed` (no drive, no ledger key),
ledger/safepath.py (no owner check off Windows, refuses), `_default_lock_held`.

The design: path text is parsed by the shell's grammar, then placed by a platform-aware placer.
On Linux a path is absolute when it starts with `/`; there are no drives, no MSYS mapping and no
`\` separators; `/` and `$HOME` are roots; comparison folds case only where the filesystem does
(the placer asks once per mount, read from /proc/self/mountinfo, and folds when in doubt, which
refuses more). ledger_glob compares both sides the same way.

What keeps Linux safe meanwhile is the front door, not the guard. **Until slice 14, tools/kit.py
refuses Linux** (`the kit runs on Windows only`, verified in tools/kit.py main), and it keeps
refusing WSL (its kernel release names Microsoft) until after native Linux parity, so there is no
supported way to set up or launch the kit on a Linux box. The guard cannot be that stop: the
Claude manifest's `pythonw` does not exist on Linux, and a hook command that cannot start is, for
Claude Code, a non-blocking error (inferred from its hook contract), so the tool call goes ahead
unguarded and no check inside hookmain ever runs. A plugin installed on Linux by hand, around
kit.py, is therefore unguarded until slice 12 gives the hook a Linux interpreter; that is a known
limit of slices 2 to 11, stated in the docs, not something the guard prevents.

Slice 2 still adds a check in hookmain that blocks every guarded tool call when the platform is
not one the guard declares ready. It does nothing on Linux until a hook can start there (slice
12), and the order puts slice 8 (the guard declares Linux ready) before slice 12, so in the
planned order it never fires on Linux; it is the backstop if that order is broken. Its proof in
slice 2 is a hookmain unit test that calls the hook entry with the platform set to an unready
one, not a live hook on Linux. Slice 12 cannot land before slice 8, and slice 14 (kit.py stops
refusing) comes after both.

## 4. CI and test marking

The owner's answer (question 8): CI for Linux and Windows comes in a later phase, together; the
first goal is the plugin working on Linux. No CI leg precedes the first backend. What is built
now is the ratchet and the markers, without a workflow; the self-hosted runner and the
real-hardware and GPU answers (questions 2 and 3) apply when the phase comes.

- Now: tests/platforms.py gives `windows_only(reason)` and `posix_only(reason)`, built like
  tests/realgit.py with a reasons table keyed by module.Class. The 142 platform skips in 68 files
  and the 13 `skipUnless(WINDOWS, ...)` in 6 files (section 1.5) move to it in one mechanical
  branch, so `kit_tests --list` can count Windows-only tests by reason and the count is reported
  per slice.
- Now: the ratchet runs `python -m unittest discover -s tests -t .`, not kit_tests.py or gate.py
  (both need job objects and the lock). Its logic lives in kitlib/ci/ratchet.py (run the suite,
  collect the failing and erroring test ids, compare them with tests/linux-baseline.txt), with
  tests in tests/ci/; tools/ci_ratchet.py is the thin entry point (arguments, printing, exit
  code), as AGENTS.md lays out. A new failure fails the run, a fixed one must be removed from the
  baseline in the same branch. Import failures count as failures. Until the CI phase it is run by
  hand (the safety net in section 8).
- Later phase: `.github/workflows/` legs on the owner's self-hosted Linux runner (any Linux
  build, a VM included; real hardware and GPUs) and a Windows leg, Python at the version the kit
  runs on (3.14 on the owner's machine today), git configured with a throwaway identity. The
  Linux leg starts as the ratchet, moves to `tools/kit_tests.py` when the pool runs on Linux
  (slice 5) and to `gate.py --full` with a CI lock profile once the lock has a real Linux store
  root (slice 9). The runner's cgroup delegation is checked then.
- CI adds only strictness: nothing in the landing path reads it until the owner says so.

## 5. Per-platform shell text

- Skills and roles: a renderer token `{{os:windows|linux}}...{{/os}}` would double every rendered
  file; the plan prefers the smaller fix: write the shared text shell-neutral (most of it is
  already: `git`, `python`), and give shell-and-worktree-traps one section per platform, picked by
  the agent from its own platform line. Only the 10 Windows-shell lines outside that skill change
  (section 1.4), and the `powershell` capability maps to no tool on Linux.
- The hub's to-do steps: `{run:<command>}` gains per-OS forms (`{run.windows:...}`,
  `{run.linux:...}`); the hub shows the form for the machine the step is for (the owner's
  machine record says its OS), and a step with only one form shows it with its shell named. The
  "hand steps carry commands" ruling is kept: every step still carries the command to run.
- Codex's dialect default follows the platform (bash on Linux).
- Docs: each owner-facing setup doc gets a Linux block beside the Windows one; the 25 docs that
  mention PowerShell (section 1.4) are sorted in the docs slice: most are Windows-internal and say
  so.

## 6. Install and machine setup on a fresh Linux box

Target: any Linux distribution (question 5). Strong containment needs cgroup v2 and a kernel of
5.14 or later; anything else installs with a lower tier of the ladder (section 2.3). Ubuntu
24.04 is the reference. Needs git, python3 (the kit's version) and tmux (checked).

`installer/machine-setup.sh` does what the .ps1 does: checks arguments and runs
tools/machine_setup.py. machine_setup's steps on Linux:

1. pointer: write /etc/pm-kit/pointer (sudo once) or the XDG one when the owner declines sudo.
2. kit check: unchanged.
3. config folders: unchanged (portable).
4. links: symlinks (`os.symlink`), no `cmd /c`.
5. managed settings: Claude Code's Linux managed-settings path (/etc/claude-code; probe).
6. settings: the guard hook entry (boot/hook.py --settings-entry), interpreter python3.
7. fresh mode surveys: run in the contain backend.
8. owner email: unchanged.
9. extras: environment.d, `.desktop`, chmod; the delegation step: probe the ladder (section
   2.3) and offer the root step that gives full containment: a drop-in `user@.service.d` with
   `Delegate=cpuset cpu io memory pids` and a re-login, or, without systemd, a root boot hook
   that hands `/sys/fs/cgroup/pm-kit` to the kit user. The owner may decline it; the box is then
   S2 (or W2 off systemd), shown as such, and still counts as "out of the box". loginctl
   enable-linger so timers run while logged out, only where user systemd exists. The second-user
   rule (question 10): with the root step, a machine-wide marker makes a second kit user
   refused; without it, a per-user check warns. A kit run inside a container warns and does not
   refuse (section 2.4); WSL is refused (question 11).
10. timers: worktree sweep, agent-temp sweep, automatic tasks, coordinators after reboot, as
    systemd user units named `pm-kit-*.timer`, or cron entries (`@reboot`) where there is no user
    systemd. The hub's start at logon is an XDG autostart entry, with a shell-profile line as the
    fallback; detach uses `setsid` where there is no systemd.
11. kit command: `~/.local/bin/kit`.

Each step reports what it could not do (no linger, no delegation, no sudo, no user systemd, no
desktop session) as a named problem, so setup names each missing piece,
and heavy runs go ahead on the tier the box allows, shown as weak or partial where it is
(question 4). Plugin refresh: pluginsource's detached refresh uses the detach backend.

## 7. macOS

Out of scope for this plan. On darwin the seam picks no backend: kitlib.platform.NAME is
'unsupported', there is no darwin folder, and every primitive raises PlatformUnsupported, so
kitlib imports there (as on Linux after slice 2) and any OS call refuses by name; tools/kit.py
keeps refusing darwin after slice 14. A darwin backend would be a new folder beside linux/, built
by its own plan, later; nothing here builds or tests it: macOS has no cgroups (the lock could
only limit concurrency), no /proc, launchd instead of systemd, and case-insensitive paths by
default.

## 8. Slices

Each slice is one branch, landed in this order unless marked; "Proof" is the test that must be
green before it lands. Risk uses the kit's levels.

The machine lock's real store root is `real_root()` in lock/store.py, built on `profile_dir()`
(SHGetKnownFolderPath, verified); its Linux form comes only in slice 9 (linux/paths.py). So
slices 3 to 7 build and prove the lock on Linux against a test store, `Store(root=<a temp
folder>)`, the way the Windows lock tests already do. Store.__init__ calls real_root() even when
given a root (verified, to set `testing`), so slice 3 makes the Linux stub's "not on this
platform" error count as "testing" when a root is given and refuse when none is. Nothing on
Linux uses the machine's store until slice 9.

CI is a later phase (question 8), so no proof below runs "in CI": each Linux proof is a manual
run on a Linux machine (the safety net below).

Safety net while CI waits (approved at plan review): (1) `gate.py --full` on Windows for each
slice; (2) before each backend slice lands, a manual Linux run of `tools/ci_ratchet.py` at the
branch tip on a reference VM (Ubuntu 24.04, systemd, the delegation drop-in) and on one
weak-mode box or container, with the log attached to the branch report. A VM on the owner's
machine is outside the lock's view, so the VM run books the machine lock from the host as
heavy. Default: a Hyper-V VM on the owner's machine; slices 1 to 4 need only the ratchet there.
Creating the VM is an owner hand step, drafted only when slice 1 is ready for its Linux run.

1. **Test markers and the ratchet.** kitlib/ci/ratchet.py with its thin entry point
   tools/ci_ratchet.py, tests/ci/, tests/platforms.py, tests/linux-baseline.txt, the mechanical
   move of the 155 platform skips (section 4); no workflow and no .github. Risk High (the skip
   move touches many tests). Proof: the ratchet's own tests (a planted failure fails it, a
   broken run is never green), and a manual run on a Linux VM that reports the broken run:
   until slice 2 the tests package does not import there, so no Linux run is green yet;
   `kit_tests --list` shows the same skip count on Windows before and after.
2. **Import-clean kitlib and the seam skeleton.** kitlib/platform/ (api, windows re-exports,
   linux stubs that raise, PlatformUnsupported for any other platform), lazy DLL binding in
   proc/win32.py, proc/pipe.py and gate/outfile.py, a lazy or guarded `wintypes` import in
   lock/layout.py, lock/store.py, proc/cpu.py, proc/process.py, proc/shield.py and
   proc/winwatch.py (rule 3), the no-os-checks test with its allowlist,
   hookmain's block-while-unready check, and the per-platform `process_names` in hosts.json
   (windows: today's `.exe` names; linux: `claude`, `node`, `codex`), read by
   kitlib/host/registry.py for the running platform, so the lock's caller walk (lock/store.py
   agent_hosts) finds the agent host on Linux from the first lock slice on. tools/kit.py keeps
   refusing. Risk High. Proof, Linux: every kitlib module imports (a test walks them); a hookmain
   unit test with an unready platform blocks the guarded tool call (no live hook: none can start
   on Linux before slice 12, section 3); registry.union('process_names') gives the linux names.
   Proof, Windows: the full suite is unchanged; hookmain's output for a fixed set of hook events
   is byte-identical to the base's; registry.union('process_names') returns the same tuple as
   the base; every kitlib module still imports, and a test that removes one export from a bound
   DLL shows the lazy binding still raises, now at first call, naming the function (today it
   fails at import).
3. **Locks.** linux/locks.py: flock named locks, run liveness, treelock and writelock on fcntl;
   lock/store.py lock() and RunMutex through the seam, on a test store root. Risk High
   (concurrency). Proof: two processes contend for a named lock; a killed holder frees it;
   run_dead sees a killed run; the treelock tests run on both; Store() with no root refuses on
   Linux by name.
   Built on kit/linux-locks: kitlib/platform/linux/locks.py (hold, RunLock, run_lock_held,
   lock_fd), the Linux branches of Store.lock, run_mutex, run_mutex_held and run_dead, and
   TreeLock's; tests/lock/test_store_linux.py and tests/fs/test_treelock.py. run_dead on Linux
   reads the run lock alone until slice 4. Deviation: the treelock takes flock, not lockf, because
   a lockf lock belongs to the process: two descriptors in one process (writelock's threads) never
   exclude each other, and closing any descriptor on the file (writelock's own touch of it) drops
   the lock.
4. **Process identity and kill.** linux/process.py with the boot folded into ctime, and the
   seam's stamp (today lock/run.py filetime_now) in the same encoding (section 2.2); the callers
   that compare (pid, ctime) are not edited: lock/run.py nest_check and end_sparing,
   proc/process.py alive, lock/store.py wrapper_state and its run-record readers, lock/background.py
   and lock/bg_client.py (whose `<run id>:<pid>:<ctime>` comment stays true). Risk High. Proof:
   ancestry, descendants, command_line and kill_tree on a spawned tree; a nest token from before a
   pid reuse is refused; with the boot_id read through a seam the test points at a file, a run
   record and a nest token written under a previous boot, at the pid and starttime of a live
   process, are judged dead by process.alive and wrapper_state and refused by nest_check; the
   end-of-run pass ages a process started before its stamp and spares one started after; on
   Windows the ctime, the token and the record are byte for byte the base's.
5. **Containment and the pool.** linux/contain.py (the S1, S2, W1 and W2 tiers of section 2.3,
   the run-time probes, the subreaper watchdog and the `leftovers` cgroup mechanism; the call
   sites in lock/run.py are slice 7's), cpu
   affinity, tooldeps/pool.py and pytests/runner.py on the seam. Risk High. Proof, on the VM
   and on a weak-mode box: a child that calls setsid stays in the cgroup and dies with it (S1);
   memory.peak is read; kill_on_close kills the tree when the owner dies; each lower tier is
   reported with what it lacks; the probes never trust an exit code.
   Built on kit/linux-containment: kitlib/platform/linux/contain.py (kitlib.proc.job.Job on
   Linux) and cpu.py (the seam's affinity primitives); pool.py holds a worker's processes by
   pidfd; tests/proc/test_linux_contain.py and test_linux_cpu.py, and the pool tests on Linux.
   The watchdog runs inside the job's cgroup (J/w), so it never outlives the job, and every
   cgroup.kill is checked to target the job's own cgroup. W1 was built later (section 2.3): a
   user and PID namespace whose pid 1 is the reaper, proved on the VM under its AppArmor
   user-namespace rule. Deviations: W2 has no subreaper (an
   environment token finds orphans, contain.py says why); leftovers() moves stragglers to a scope
   of their own through systemd; processes() is None; a Linux job runs one command; the pid
   shield is Windows only, so each Linux unit and worker carries the "could not shield" note.
   Short Jobs (`Job(short=True)`: the calls in git/cmd.py (git), records/inflight.py (the
   configured in_flight command) and machinesetup/extras.py run_tree (the claude CLI); Windows
   ignores it) share one carrier scope: contain.py Carrier moves the child in with busctl
   AttachProcessesToUnit into its own cgroup under the carrier, reads the placement back, and
   falls back to the Job's own scope if either fails. Why: a systemd user scope per call costs
   about 84 ms on the VM, against about 17 ms attached. Known limits: one shared carrier unit
   (killing one short job never reaches another, but stopping the carrier, or systemd-oomd
   choosing it, ends them all), and the first call, which creates it, costs about 70-90 ms.
6. **Lock hardware.** linux/hardware.py feeding lock/layout.detect; fake_hardware gains the four
   synthetic /sys and /proc trees of section 2.4. Risk High (lock logic). Proof: the four
   synthetic trees, and the VM's live /sys, divide with no shared id; the Windows layout
   tests are unchanged.
   Built on kit/linux-lock-hardware: kitlib/platform/linux/hardware.py, layout.detect_linux, the
   seam's four hardware primitives; the VM's live /sys run (tests/lock/test_layout_linux.py
   LiveSys) waits for the VM. Deviation: fake_hardware stays unchanged; the four trees live in
   tests/lock/linux_trees.py, read through the real reader.
7. **Lock runs and detach.** lock/run.py Slot, give-back, release, detach and gate/submit on the
   seam, on a test store root, and the call sites of slice 5's `leftovers` mechanism (Slot.release
   and end_sparing). Risk High. Proof: a detached run survives its caller; give-back narrows
   cpuset.cpus; a straggler of a released slot is moved to `leftovers`. The tree-kill probe (does
   ending a tool call kill its tree by pid) and slice 7's live proofs (test_slot_linux,
   test_detach_linux Live) moved to their own backlog record when slice 7
   landed. Both ran there (section 8.1): the probe shows no shield is needed (section 1.3), and
   at containment S1 test_slot_linux Live (the give-back narrows cpuset.cpus; a straggler goes
   to a `leftovers` scope) and test_detach_linux Live (a detached run outlives its caller's
   scope) pass. test_leftovers.py is Windows only (every class skips on Linux); the Linux
   leftovers move is test_slot_linux Live's straggler test.
   Built on kit/linux-lock-runs. Deviation: Slot release unpins the job before the leftovers
   move (S2 and W2), so a straggler moved out gets every cpu back.
8. **Guard path semantics.** The placer, the ten findings in section 3 as tests first, then the
   fixes; the guard declares Linux ready. Lands alone after a strong-tier review. Risk High.
   Proof: each finding's test refuses on Linux; the Windows guard suite is unchanged.
9. **Paths and configuration.** linux/paths.py: pointer, profile, temp, environment; config/user.py
   and config/pointer.py on the seam; lock/store.py real_root on the Linux profile, so the
   machine's store exists on Linux; worktree/holders.py and classify.py from /proc (holders stops
   failing open); Risk Medium (High
   for the store root). Proof: machine config resolves on Linux from the pointer file; holders
   finds a process whose cwd is in a worktree; Store() opens the real root; a landing runs end to
   end on a throwaway repository on the VM.
   Built on kit/linux-config-paths: kitlib/platform/linux/paths.py (the profile folder from the
   password database, never $HOME; the pointer file /etc/pm-kit/pointer, else
   <profile>/.config/pm-kit/pointer, never $XDG_CONFIG_HOME, and a bad machine file is the answer;
   /tmp as the system temp whatever TMPDIR says; a /proc scan of this user's processes).
   user.real_profile and the home fence, lock/store.py profile_dir and real_root, pointer.registry
   and check (a leading / on Linux, so a path with a drive is refused), system_temp, work_temp and
   default_work_temp read the seam; user.py and holders.py leave test_no_os_checks' list.
   holders.blockers on Linux: a path in the tree that a process of this user uses (cwd, root, exe,
   an open or mapped file) is HELD; a process of this user that cannot be read in full, whose
   user cannot be told, or in another mount namespace (a live leader's threads' too) makes the
   root UNREAD, as does a tree the scan's mount table shows at another path too (a bind mount of
   the tree, a folder above it or one in it), has another mount inside it or a later mount at or
   above the holding one, or does not reach; a /proc that cannot be listed, lists no process, does
   not list the scan's own, hides processes (hidepid=2 or invisible, subset=pid, on any proc mount
   there) or whose mount options cannot be read is NOT_SEARCHED, as is a scan in a PID namespace
   other than the first (its ns/pid link is not pid:[4026531836]: a container or a WSL2 distro) or
   whose ns/pid link cannot be read;
   each is a refusal. A process is this user's by any of its four status Uid users or its
   folder's owner (its effective user; on Linux 7.0 it stays so for a not-dumpable one); each thread's cwd and root are read,
   and a zombie leader's running threads give every kind. cwd_holders, describe and classify.process_cwds read /proc too. The
   Linux Godot and Blender heavy defaults (section 3, finding 8) moved to their own record, as
   they changed Windows too; they landed on Linux only (kitlib/config/policy.py
   LINUX_HEAVY_DEFAULTS), so no Windows result changes. Slice 9's VM proofs moved to their own
   backlog record (with slice 7's live proofs) when slice 9 landed: (a) test_store_linux
   RealMachineStore, (b) test_holders_linux Live, (c) a landing end to end on a throwaway
   repository on the VM, and (d) how often an unreadable process of this user (ssh-agent,
   gpg-agent) leaves removals UNREAD. Run on the VM (section 8.1): (a) passes. (b) is open:
   the two held-tree tests pass, but `test_a_tree_nothing_uses_is_free` fails, since a free tree
   answers UNREAD. (d) explains it: UNREAD came back for 50 of 50 removals of a free tree with no
   agent running, and the same with ssh-agent, with gpg-agent too, and after both ended. The
   user's own systemd manager (`systemd --user`), its `(sd-pam)` and the ssh login's
   `sshd-session` are processes of this user whose `ns/mnt` cannot be read (the manager holds
   CAP_WAKE_ALARM; the other two are not dumpable), and
   they last as long as the login, so on a stock Ubuntu box every removal fell to the owner.
   Its own record's fix settles that part: those three are this login's own and no
   longer make a tree unread (docs/worktree-remove.md "Classify": a process in the user manager's
   `init.scope`, and a never-exec'd fork whose parent, not PID 1, is another user's), so (b) passes
   on the VM, and 50 of 50 runs of the free-tree test there came back free. Still open: ssh-agent, gpg-agent and a desktop's gcr agent stay unread processes,
   so a removal while one runs still falls to the owner (`describe` names it). (c) is open: the end-to-end landing tests skip on Linux ("the lock is
   Windows only"; in tests/gate/test_land*.py only a few unit tests run) and tools/kit.py refuses Linux, so a landing cannot run end to end
   there before slice 14.
   Known limits:
   a bind mount over a tree's file system leaves its removals UNREAD; in a WSL2 distro (a PID
   namespace of its own) every removal is NOT_SEARCHED; /proc is listed once, so a process that
   forks and exits during the scan can leave a child started after the listing that inherits its
   cwd unseen; a user with no password
   database entry has no profile folder, so the machine lock's store cannot be opened (and with no
   machine pointer file the pointer is BAD). Deviations: environment.d moves to slice 14 (machine setup writes it);
   another user's processes cannot be seen, so holders answers for this user's only; the user
   pointer file is the user's to write, so an agent could write it too: the guard now holds it
   (docs/guard.md "Write jail", tests/guard/test_linux_pointer.py), and the guard's mount table is
   read by linux/paths.py's mountinfo reader (read_mountinfo, parse_mountinfo).
10. **Scheduled jobs.** linux/schedule.py with systemd user timers and cron (first-class);
    autostart (coordinators after reboot), hubsite autostart (the hub's start at logon: XDG
    autostart, shell-profile fallback), worktree and temp sweeps, automatic tasks, consoletasks
    read-back; linger only where user systemd exists. Risk Medium. Proof: units, cron entries
    and autostart entries render and round-trip through read-back; on the VM with user systemd
    a timer fires once, and on a box without it a cron entry does.
11. **Sessions.** launcher window (tmux by default, a configured terminal command if set), stop,
    pmstart process listing, autotasks launch flags.
    Risk Medium. Proof: a launched session is found by name and stopped; no breakaway flag off
    Windows.
12. **Hook interpreter.** per-platform interpreters (hookmain INTERPRETERS) and the manifests.
    The form is settled (question 9): per-platform manifests, rendered for each platform. Open
    seam: how the plugin ships the right manifest on each platform. Needs slice 8. Risk High (hook entry). Proof: the rendered manifest
    runs the hook on both; a missing interpreter blocks rather than passes; the rendered Windows
    hooks.json of each host is byte-identical to the base's.
    Built on kit/linux-hook-manifests: hookmain.manifest_command per platform (INTERPRETERS
    linux: python3, run by /bin/sh; a tool event's line exits 2 when python3 fails or is missing,
    a start event's says the kit did not load), each host's hooks.linux.json beside its
    unchanged hooks.json, hosts.json platforms windows and linux for both hosts, and the seam:
    the plugin export writes the running platform's file as hooks.json
    (kitlib.pluginsource.platform_manifests). The posix-only run of the lines (tests/host/
    test_hook_linux.py) waits on a Linux machine. The MCP servers follow the same seam:
    hookmain.mcp_interpreter picks python3 on Linux, rendered into
    `.claude-plugin/plugin.linux.json` and Codex's `mcp.linux.json`, which the export swaps in
    (tests/content/test_render.py ManifestTest).
13. **Shell text.** shell-and-worktree-traps sections, the 10 Windows-shell lines (section 1.4),
    hub to-do step forms, Codex dialect. Risk Low. Proof: content render check; hub renders each
    form. Windows stays byte-identical: every rendered Windows file differs from the base's only
    in the named lines; a step with one `{run:}` form renders byte-identical to the base's; Codex's
    rendered Windows output, and its dialect default on Windows ('powershell'), are unchanged.
14. **Machine setup on Linux.** installer/machine-setup.sh, machinesetup steps on the seam
    (the offered delegation step, root drop-in or boot hook, which may be declined; cron
    `@reboot`; `setsid` detach and linger where there is no systemd or only where user systemd
    exists; each missing piece named; the tmux check; the second-user rule, a per-user warning
    without the root step and a machine-wide refusal with it; the container warning; the WSL
    refusal), tools/kit.py stops refusing Linux (and keeps refusing darwin, and WSL until native
    parity, question 11). The tree-kill probe has run (no shield, section 1.3). Risk High (install). Proof: machine setup on a fresh Ubuntu 24.04 VM
    with the delegation drop-in ends with no problems and the lock status shows S1; separately,
    machine setup in a default Docker container probes to its lower tier and the lock status
    shows it as such (setup warns that it is in a container). No proof claims strong containment
    in a container.
15. **Peer messaging and the bridge.** peer.py on Claude Code's Linux socket (after a probe), the
    bridge's leader lock and transport on Unix sockets, procwatch. Risk High (networking). After
    Windows parity, not in the first build (question 7).
16. **Docs.** owner setup docs, ARCHITECTURE.md's platform line, AGENTS.md's "Windows only",
    the multi-host plan's section 6 marked superseded. Risk Low. Proof: docs gate.
17. **CI legs (later phase, question 8).** `.github/workflows/` for the self-hosted Linux runner
    and Windows, the ratchet and then the pool and gate as in section 4, the Windows leg after
    Linux is green. Risk High (CI config). Proof: the Linux leg is green on the baseline and a
    planted failure fails it.

Slices 9 to 11 and 13 can run in parallel after slice 7 (slice 9 touches lock/store.py, so it
does not overlap another lock slice); the others are in order.

### 8.1 Running tests on the reference VM

The reference VM is the Hyper-V guest `kit-linux-ref` on the owner's machine: Ubuntu, 4 vCPUs,
user `kitvm`, reached by ssh with the key `~/.ssh/kit-linux-ref_ed25519` and its host key pinned
under the alias `kit-linux-ref`. Starting or stopping it is the PM's call; a worker that finds it
off, or ssh refusing the key, stops and reports. Its address changes at each boot, so read it from
its adapter (PowerShell; ssh's BatchMode makes a missing key fail at once rather than prompt):

```
$m = (Get-VMNetworkAdapter -VMName kit-linux-ref).MacAddress -replace '(..)(?!$)', '$1-'
$ip = (Get-NetNeighbor -LinkLayerAddress $m | ? AddressFamily -eq IPv4).IPAddress
$o = @('-o','HostKeyAlias=kit-linux-ref','-o','StrictHostKeyChecking=yes','-o','BatchMode=yes',
       '-i',"$env:USERPROFILE\.ssh\kit-linux-ref_ed25519")
$vm = $o + "kitvm@$ip"
```

1. Sync a commit, never the working tree, to a folder of its own (`<name>`: your slug):
   `git -C <worktree> archive --format=tar -o <scratch>\<name>.tar <rev>`;
   `scp @o <scratch>\<name>.tar "kitvm@${ip}:kit-runs/"`;
   `ssh @vm "cd ~/kit-runs && rm -rf <name> && mkdir <name> && tar -xf <name>.tar -C <name> && rm <name>.tar"`.
2. Run named tests under the host's machine lock. The VM is outside the lock's view; its 4 vCPUs
   are two of the host's cpu slots, so the run is `engine-test`, and the resource keeps two VM
   runs from sharing the VM:
   `python <kit>/tools/machine_lock.py run --repo <repo> --slug <slug> --kind engine-test --resource vm:kit-linux-ref -- ssh @vm "cd ~/kit-runs/<name> && python3 -m unittest -v <module>[.<Class>] ..." > <scratch>\<log> 2>&1`
   (in PowerShell, `@vm` splats; write the ssh options out in Git Bash). The containment tier the
   tests saw: `python3 -c "from kitlib.platform.linux import contain; print(contain.probe().text)"`
   in the same folder.
3. Collect: the host log is the run's whole output; read it when the run ends. Then
   `ssh @vm "rm -rf ~/kit-runs/<name>; systemctl --user list-units --all --no-legend 'pmkit-*'; pgrep -u kitvm -af python3"`
   removes the folder and shows any unit or process a test left behind (report one, never
   leave it running).

The tree-kill probe (section 1.3), to run again on a new Claude Code: its scripts are in
`~/ccprobe` on the VM (`probe.sh <scenario>`, `all.sh` for every one), and a Linux claude in
`~/ccinstall`. `mock.py` is a scripted stand-in for the Messages API on 127.0.0.1
(`ANTHROPIC_BASE_URL`, a fake key, a fresh HOME), so claude runs one Bash call with no account.
The call starts `tree.py`: P, the shell's child; A, a child of P in its group; B, a child of P in
a session of its own; C, double forked away. P, A and B ignore SIGTERM, SIGINT and SIGHUP, and
all four write beats and the signals they get. Scenarios: `finished`, `timeout` (moved to the
background), `bgexit`, `esc` (Escape in tmux), `fgterm` and `fgint` (the signal to claude),
`bgkill` and `timeoutkill` (TaskStop, found by ToolSearch). claude runs under
`strace -f -tt -e trace=kill,tkill,tgkill,pidfd_send_signal,pidfd_open,setsid,setpgid,execve -e signal=none`;
the report says which members kept beating after the call ended.

## 9. Risks and unknowns

- Unknown: whether `from ctypes import wintypes` imports on Linux on the kit's Python (it raised
  on some older versions); slice 2 checks, and makes it lazy or guarded either way in all nine
  modules of section 1.1 (rule 3).
- Unknown: Claude Code's Linux messaging address, its hook interpreter lookup, its managed
  settings path and its session file fields; slice 15 and 12 probe first.
- Settled (the tree-kill probe, on the reference VM with Claude Code 2.1.312): Claude Code on
  Linux kills no finished call's tree, and kills a stopped call's live tree by pid from a fresh
  listing, so no pid shield is needed (section 1.3). A later Claude Code may change it; section
  8.1 gives the probe's design to run again.
- Unknown (CI phase): how the owner's self-hosted runner delegates cgroups to the runner user.
- Unknown: the containment research of section 2.3 is read from documentation; slice 5 probes
  each tier on a real box (whether `AllowedCPUs` has an effect without cpuset delegation, whether
  a PID namespace starts, whether a container's cgroup is writable).
- Risk: the weak containment tiers look like they work. Mitigation: shown and recorded, not
  refused: the tier and what it lacks are named in the lock's status, the heavy-run start line
  and the run record (question 4).
- Risk: guard semantics drift between Windows and Linux. Mitigation: one placer with a platform
  argument, and the guard tests run both placers on every platform (they are pure).
- Risk: the seam move changes Windows behaviour by accident. Mitigation: re-exports first, the
  full Windows suite on every slice, and the Windows CI leg once the CI phase brings it.
- Risk: multi-user and container boxes break the one-store rule. Mitigation: one kit per CPU set
  and the second-user rule (section 2.4).
- Risk: the scope is large (17 branches, most High). It is paced by the coordinator's caps;
  nothing here asks for parallel heavy runs.

## 10. Questions for the owner

All eleven are answered (owner, 2026-10-26); the answers are folded into the sections above.

1. **macOS.** ANSWERED (owner, 2026-10-26): out of this plan; macOS gets its own plan later.
   The seam names darwin unsupported and refuses it by name (section 7).
2. **Linux CI runner.** ANSWERED (owner, 2026-10-26): a self-hosted box; support any Linux
   build, VM included; the primary target is full Linux support out of the box. Not
   GitHub-hosted. A Linux container for a quick local check is a tool, not a runner.
3. **Spending.** ANSWERED (owner, 2026-10-26): a Linux box or VM, with real hardware and GPU
   tests. No hosted-runner spend is planned.
4. **No cgroup v2 delegation.** ANSWERED (owner, 2026-10-26): heavy runs must work on Linux,
   with full cgroup v2 containment on native Linux as planned. Before the plan settles on the
   weak fallback, research must confirm no stronger option exists without delegation.
   Where nothing stronger exists (Docker included), heavy runs go
   ahead with weak containment, shown as `containment: weak`; the owner accepts that risk. The
   research is in section 2.3.
5. **Supported Linux.** ANSWERED (owner, 2026-10-26): any Linux distribution, with cron
   fallbacks where systemd is absent; breadth over systemd-only, in line with full Linux support
   out of the box.
6. **Session windows.** ANSWERED (owner, 2026-10-26): both supported; tmux by default, a
   configured terminal command if set.
7. **The bridge and peer messaging on Linux.** ANSWERED (owner, 2026-10-26): after Windows
   parity, not in the first build.
8. **A Windows CI leg.** ANSWERED (owner, 2026-10-26): CI for both Linux and Windows comes in a
   later phase; the initial goal is the plugin working on Linux. So the earlier rule of a Linux
   CI leg before the first backend lands no longer holds; the self-hosted box or VM and the
   real-hardware and GPU answers (questions 2 and 3) apply when that phase comes.
9. **Hook manifest form.** ANSWERED (owner, 2026-10-26): per-platform manifests, rendered for
   each platform.
10. **Multi-user boxes.** ANSWERED (owner, 2026-10-26): one kit user per box; machine setup
    refuses a second.
11. **WSL.** ANSWERED (owner, 2026-10-26): later, after native Linux parity; refused meanwhile
    (tools/kit.py refuses WSL, whose kernel release names Microsoft). A kit in WSL beside the
    Windows kit would keep a second lock store for the same CPUs and memory.

## 11. Proposed build records

For the coordinator to file, in the store's id form (these slugs map to its uppercase ids). Each
carries its own scope.

| Slug | Summary | Scope |
|---|---|---|
| linux-ci-ratchet | Ratchet, baseline and platform test markers (no workflow) | kitlib/ci/, tools/ci_ratchet.py, tests/ci/, tests/platforms.py, tests/linux-baseline.txt, tests/ (skip moves) |
| linux-ci-legs | Later phase: Linux and Windows CI legs on the self-hosted runner | .github/workflows/ |
| linux-import-seam | Import-clean kitlib, kitlib/platform skeleton, no-os-checks test, hook block while unready, per-platform process names | kitlib/platform/, kitlib/proc/win32.py, kitlib/proc/pipe.py, kitlib/gate/outfile.py, kitlib/lock/layout.py, kitlib/lock/store.py, kitlib/proc/cpu.py, kitlib/proc/process.py, kitlib/proc/shield.py, kitlib/proc/winwatch.py, kitlib/host/hookmain.py, kitlib/host/registry.py, hosts.json, tests/platform/, tests/host/, tests/gate/ |
| linux-locks | flock named locks, run liveness, treelock and writelock, on a test store root | kitlib/platform/linux/locks.py, kitlib/lock/store.py, kitlib/fs/treelock.py, kitlib/fs/writelock.py, tests/lock/, tests/fs/ |
| linux-process-identity | /proc identity, ancestry, kill; the boot folded into the Linux ctime | kitlib/platform/linux/process.py, kitlib/proc/process.py, kitlib/lock/run.py, tests/proc/, tests/lock/ |
| linux-containment | Containment ladder (S1, S2, W1, W2), run-time probes, the `leftovers` cgroup mechanism (its call sites are linux-lock-runs), the root delegation step's probe, pool and unit runner on Linux | kitlib/platform/linux/contain.py, kitlib/platform/linux/cpu.py, kitlib/proc/job.py, kitlib/tooldeps/pool.py, kitlib/pytests/runner.py, tests/proc/, tests/tooldeps/ |
| linux-lock-hardware | /sys and /proc hardware detection, synthetic /sys trees | kitlib/platform/linux/hardware.py, kitlib/lock/layout.py, tests/lock/ |
| linux-lock-runs | slots, give-back, detach and gate submit on Linux, on a test store root; the `leftovers` sparing call sites in Slot.release and end_sparing | kitlib/lock/run.py, kitlib/lock/detach.py, kitlib/gate/submit.py, kitlib/platform/linux/detach.py, tests/lock/, tests/gate/ |
| linux-guard-paths | Guard placer for POSIX paths and the safety findings | kitlib/guard/, tests/guard/ |
| linux-config-paths | Pointer, profile, temp, environment, the lock store's real root; worktree holders from /proc | kitlib/platform/linux/paths.py, kitlib/config/, kitlib/lock/store.py, kitlib/worktree/holders.py, kitlib/worktree/classify.py, tests/config/, tests/lock/, tests/worktree/ |
| linux-scheduled-jobs | systemd user timers and cron for every scheduled task, read-back | kitlib/platform/linux/schedule.py, kitlib/launcher/autostart.py, kitlib/hubsite/autostart.py, kitlib/worktree/task.py, kitlib/machinesetup/, kitlib/pmstart/consoletasks.py, tests/ |
| linux-sessions | tmux sessions, stop, process listing, launch flags | kitlib/launcher/, kitlib/pmstart/, kitlib/autotasks/, tests/launcher/, tests/pmstart/ |
| linux-hook-manifests | Per-platform hook interpreter and manifests | hosts.json, kitlib/host/hookmain.py, kitlib/host/content.py, hosts/*/shell/, tests/host/ |
| linux-shell-text | Per-platform skill text, hub to-do step forms, Codex dialect | content/, kitlib/hubsite/todosteps.py, hosts/codex/adapter/, tests/hubsite/, tests/content/ |
| linux-machine-setup | Fresh-box machine setup and installer, the root delegation step, second-user and WSL refusals, kit.py stops refusing Linux | installer/machine-setup.sh, kitlib/machinesetup/, tools/machine_setup.py, tools/kit.py, tests/machinesetup/ |
| linux-peer-bridge | Peer messaging and the bridge on Unix sockets, procwatch | hosts/claude/adapter/peer.py, hosts/claude/adapter/peer_worker.py, optional/bridge/, kitlib/machinesetup/procwatch.py, tests/ |
| linux-docs | Owner setup docs and the platform lines in the contract docs | docs/, ARCHITECTURE.md, AGENTS.md |
