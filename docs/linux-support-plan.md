# Linux support plan

Status: plan only (2026-10-25). Nothing here is built. It extends, and for Linux supersedes, the
section "6. Platform seam (later refactor: Linux and macOS)" of docs/multi-host-plan.md: that
section set the seam's shape and four questions; this plan verifies the Windows-only surface,
designs each backend, orders the work into branches and asks what only the owner can decide.

Goal (the owner's): the whole kit runs on Linux out of the box, at parity with Windows: the PM,
coordinators, the launcher, the hub, the guard, the machine lock, the gate, landings, scheduled
jobs, machine setup, plugin refresh, skills and docs. Windows behaviour does not change: every
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
  `"platforms": ["windows"]` for both hosts, with `.exe` process names (verified).

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
  have no Linux need (Linux opens no console windows). That the shield and PidHold have none
  either is inference, not verified: it assumes Claude Code on Linux does not kill a tool call's
  tree by pid, which section 9 lists as unknown; slice 7 probes it.
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

- No CI: no workflow, no .github folder. That the repository is public on GitHub is unverified:
  this plan did not check its visibility; no longer needed for cost (question 3 answered).
- `git grep -c -E "skip(Unless|If)\((os\.name|sys\.platform)" -- '*.py'` finds 142 platform
  skips in 68 test files, with free-text reasons; `skip(Unless|If)\(WINDOWS` finds 13 more in 6
  files that skip through a module constant (`WINDOWS = os.name == 'nt'`).
- The kit's own runners need job objects: tools/kit_tests.py (the pool) and gate.py (the pool and
  the machine lock, which fails closed). A first Linux leg cannot use either.
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
    schedule.py   systemd user timers (cron fallback)
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
   silently does less (see 2.3 and owner question 4).

### 2.2 Windows mechanism to Linux mechanism

| Windows | Linux | Notes |
|---|---|---|
| (pid, FILETIME ctime) identity | (pid, starttime from /proc/<pid>/stat field 22) plus boot_id | starttime is in clock ticks since boot, so it repeats across reboots; the seam folds the boot into the ctime value (below) |
| Toolhelp parent walk | /proc/<pid>/stat ppid, checked against each starttime | same ancestry rule |
| NtQueryInformationProcess command line | /proc/<pid>/cmdline | NUL-separated, no CRT re-quoting |
| Job object (kill on close, CPU mask, memory, peak) | cgroup v2 leaf under the user's delegated slice | see 2.3 |
| SetProcessAffinityMask / cpu sets | os.sched_setaffinity | for the pool and the fallback only; the slot's mask lives in cpuset.cpus |
| WMI Win32_Process Create (detach) | `systemd-run --user --scope` or double fork + setsid | systemd-run also places the run in its own cgroup |
| Named mutex, WAIT_ABANDONED | fcntl.flock on a lock file in the store, opened O_CLOEXEC | the kernel drops it when the holder dies: the same "abandoned counts as free" meaning |
| RunMutex (liveness) | the run holds flock on `runs/<id>.alive`; readers probe with LOCK_NB | run_dead and run_live keep their meaning |
| msvcrt.locking (treelock) | fcntl.lockf on the same lock file | byte range kept |
| Register-ScheduledTask | systemd user `.timer` + `.service` units; loginctl enable-linger | cron (`crontab -l`/`crontab -`) when there is no user systemd |
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
| pid shield, PidHold | inferred: nothing needed; pidfd where a hold is wanted | rests on Claude Code on Linux not killing a tool call's tree by pid (unknown, section 9; slice 7 probes it) |
| Git Bash lookup | /bin/bash | kitlib/proc/bash.py answers /bin/bash |
| conhost + PowerShell session window | tmux session (or a configured terminal command) | owner question 6 |
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
- Escape, against that threat model: setsid and double fork do not move a process between
  cgroups, where they do leave a process group, so the leaf holds the tree that ordinary tools
  make. It is not a security boundary: a same-user process can write its own pid into any
  cgroup.procs it has write access to, which includes the rest of the user's delegated subtree.
  Here a job object is stricter (a process cannot leave its job, and the Job class never allows
  breakaway; the automatic-tasks tick, which launches with CREATE_BREAKAWAY_FROM_JOB on purpose,
  is the one exception, section 1.3). The plan accepts that difference and states it in the docs
  rather than defending against a deliberate move.

Fallback when the user has no delegated cgroup v2 (old systemd, containers, WSL without
systemd): process group + sched_setaffinity + PR_SET_PDEATHSIG + an RLIMIT_AS cap. It is
cooperative: a child that calls setsid or resets its affinity escapes. The backend names it
`Contain(weak=True)`, the lock's status shows `containment: weak`, and owner question 4 decides
whether heavy runs may use it at all.

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
  from `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader` or /proc/driver/nvidia;
  live use stays with api.gpu_use (nvidia-smi already).

No double-booking: unchanged in rule. One store per user (`<profile>/.claude/machine-lock`), a new
layout adopted only while nothing is live or queued, under the queue lock. Two gaps are Linux's
own. A container sees the host's CPUs in /sys while its cgroup allows fewer, so the allowed set
comes from sched_getaffinity and the cgroup's cpuset.cpus.effective, never from /sys alone; the
backend closes that one. Two kit users on one box each get a store and would book the same CPUs
twice. Proposal, pending owner question 10: the store moves to a machine path
(`/var/lib/pm-kit/machine-lock`, group-writable by a `pm-kit` group) when machine setup finds
more than one kit user; under the recommended answer (one kit user per box) machine setup
instead refuses a second user. The same double-booking is why owner question 11 (WSL) matters.

The proving test feeds four synthetic /sys and /proc trees, written by hand into the test data
(fake_hardware, extended) from the kernel's documented layout: a plain desktop, a hybrid part
(cpu_core and cpu_atom), a box with gaps in its CPU ids and a container whose cpuset allows
fewer CPUs than /sys lists. It checks that no two slots share an id. No real machine was recorded
for them, and none is needed for the division logic; the hybrid and gap trees are the ones a CI
runner cannot give (a hosted runner is a plain VM, inferred). The CI leg adds the runner's own
live /sys as a fifth, plain case.

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
refuses Linux** (`the kit runs on Windows only`, verified in tools/kit.py main), so there is no
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

- `.github/workflows/linux.yml`: runs on the owner's self-hosted Linux runner (answered: not
  GitHub-hosted; any Linux build, a VM included), Python at the version the kit runs on (3.14 on
  the owner's machine today), git configured with a throwaway identity. The box has real
  hardware and GPUs, so the live /sys case below is that box's.
- The first leg runs `python -m unittest discover -s tests -t .` in a ratchet mode, not
  kit_tests.py or gate.py (both need job objects and the lock). The ratchet logic lives in
  kitlib/ci/ratchet.py (run the suite, collect the failing and erroring test ids, compare them
  with tests/linux-baseline.txt), with tests in tests/ci/; tools/ci_ratchet.py is the thin entry
  point (arguments, printing, exit code), as AGENTS.md lays out. A new failure fails the leg, a
  fixed one must be removed from the baseline in the same branch. Import failures count as
  failures.
- tests/platforms.py gives `windows_only(reason)` and `posix_only(reason)`, built like
  tests/realgit.py with a reasons table keyed by module.Class. The 142 platform skips in 68 files
  and the 13 `skipUnless(WINDOWS, ...)` in 6 files (section 1.5) move to it in one mechanical
  branch, so `kit_tests --list` can count Windows-only tests by reason and the count is reported
  per slice.
- When the pool runs on Linux (slice 5), the leg switches to `tools/kit_tests.py`; once the lock
  has a real Linux store root (slice 9), to `gate.py --full` with a CI lock profile.
- Proposal, pending owner question 8: a Windows leg (windows-latest) once Linux is green, so a
  Windows regression from a shared change is caught off the owner's machine too.
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

Target: a systemd distribution with user sessions, cgroup v2 and a kernel of 5.14 or later
(Ubuntu 24.04 as the reference); git, python3 (the kit's version) and a terminal multiplexer.

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
9. extras: environment.d, `.desktop`, chmod; cgroup delegation check (`systemctl --user show
   -p Delegate` on the user slice, and the controllers in cgroup.subtree_control); loginctl
   enable-linger so timers run while logged out.
10. timers: worktree sweep, agent-temp sweep, automatic tasks, hub at login, coordinators after
    reboot, as systemd user units named `pm-kit-*.timer`.
11. kit command: `~/.local/bin/kit`.

Each step reports what it could not do (no linger, no delegation, no sudo) as a named problem,
and the lock refuses heavy runs while containment is missing unless owner question 4 says
otherwise. Plugin refresh: pluginsource's detached refresh uses the detach backend.

## 7. macOS

Out of scope for this plan. On darwin the seam picks no backend: kitlib.platform.NAME is
'unsupported', there is no darwin folder, and every primitive raises PlatformUnsupported, so
kitlib imports there (as on Linux after slice 2) and any OS call refuses by name; tools/kit.py
keeps refusing darwin after slice 14. A darwin backend would be a new folder beside linux/, built
by its own plan; nothing here builds or tests it: macOS has no cgroups (the lock could only limit concurrency, the old
question 2), no /proc, launchd instead of systemd, and case-insensitive paths by default. The
owner confirmed this scope (question 1).

## 8. Slices

Each slice is one branch, landed in this order unless marked; "Proof" is the test that must be
green before it lands. Risk uses the kit's levels.

The machine lock's real store root is `real_root()` in lock/store.py, built on `profile_dir()`
(SHGetKnownFolderPath, verified); its Linux form comes only in slice 9 (linux/paths.py). So
slices 3 to 7 build and prove the lock on Linux against a test store, `Store(root=<a temp
folder>)`, the way the Windows lock tests already do. Store.__init__ calls real_root() even when
given a root (verified, to set `testing`), so slice 3 makes the Linux stub's "not on this
platform" error count as "testing" when a root is given and refuse when none is. Nothing on
Linux uses the machine's store, and the CI leg does not run gate.py, until slice 9.

1. **CI leg and test markers.** `.github/workflows/linux.yml`, kitlib/ci/ratchet.py with its thin
   entry point tools/ci_ratchet.py, tests/ci/, tests/platforms.py, tests/linux-baseline.txt, the
   mechanical move of the 155 platform skips (section 4). Risk High (CI config). Proof: the leg
   runs green on the baseline; a planted failure fails it; `kit_tests --list` shows the same skip
   count on Windows before and after.
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
5. **Containment and the pool.** linux/contain.py (cgroup v2 and the weak fallback), cpu
   affinity, tooldeps/pool.py and pytests/runner.py on the seam; the CI leg moves to
   kit_tests.py. Risk High. Proof: a child that calls setsid stays in the cgroup and dies with
   it; memory.peak is read; kill_on_close kills the tree when the owner dies; the weak mode is
   reported as weak.
6. **Lock hardware.** linux/hardware.py feeding lock/layout.detect; fake_hardware gains the four
   synthetic /sys and /proc trees of section 2.4. Risk High (lock logic). Proof: the four
   synthetic trees, and the CI runner's live /sys, divide with no shared id; the Windows layout
   tests are unchanged.
7. **Lock runs and detach.** lock/run.py Slot, give-back, release, detach and gate/submit on the
   seam, on a test store root. Risk High. Proof: a detached run survives its caller; give-back
   narrows cpuset.cpus. On a Linux box with Claude Code (the Linux box or VM of owner question
   3 (c), or any Linux machine the owner lends for the probe), a probe of
   whether ending a tool call kills its tree by pid; if it does, a shield is added here before
   the slice lands (sections 1.3 and 9).
8. **Guard path semantics.** The placer, the ten findings in section 3 as tests first, then the
   fixes; the guard declares Linux ready. Lands alone after a strong-tier review. Risk High.
   Proof: each finding's test refuses on Linux; the Windows guard suite is unchanged.
9. **Paths and configuration.** linux/paths.py: pointer, profile, temp, environment; config/user.py
   and config/pointer.py on the seam; lock/store.py real_root on the Linux profile, so the
   machine's store exists on Linux; worktree/holders.py and classify.py from /proc (holders stops
   failing open); the CI leg moves to gate.py --full with a CI lock profile. Risk Medium (High
   for the store root). Proof: machine config resolves on Linux from the pointer file; holders
   finds a process whose cwd is in a worktree; Store() opens the real root; a landing runs end to
   end in CI on a throwaway repository.
10. **Scheduled jobs.** linux/schedule.py; autostart, hubsite autostart, worktree and temp
    sweeps, automatic tasks, consoletasks read-back. Risk Medium. Proof: units render and
    round-trip through read-back; with user systemd in CI, a timer fires once.
11. **Sessions.** launcher window (tmux), stop, pmstart process listing, autotasks launch flags.
    Risk Medium. Proof: a launched session is found by name and stopped; no breakaway flag off
    Windows.
12. **Hook interpreter.** per-platform interpreters (hookmain INTERPRETERS), the manifest form
    owner question 9 picks. Needs slice 8. Risk High (hook entry). Proof: the rendered manifest
    runs the hook on both; a missing interpreter blocks rather than passes; the rendered Windows
    hooks.json of each host is byte-identical to the base's.
13. **Shell text.** shell-and-worktree-traps sections, the 10 Windows-shell lines (section 1.4),
    hub to-do step forms, Codex dialect. Risk Low. Proof: content render check; hub renders each
    form. Windows stays byte-identical: every rendered Windows file differs from the base's only
    in the named lines; a step with one `{run:}` form renders byte-identical to the base's; Codex's
    rendered Windows output, and its dialect default on Windows ('powershell'), are unchanged.
14. **Machine setup on Linux.** installer/machine-setup.sh, machinesetup steps on the seam,
    tools/kit.py stops refusing Linux (and keeps refusing darwin, and WSL unless owner question
    11 brings it in). Risk High (install). Proof: machine setup on a fresh Ubuntu container ends
    with no problems and the lock status shows strong containment.
15. **Peer messaging and the bridge.** peer.py on Claude Code's Linux socket (after a probe), the
    bridge's leader lock and transport on Unix sockets, procwatch. Risk High (networking). May
    stay Windows-only at first (owner question 7).
16. **Docs.** owner setup docs, ARCHITECTURE.md's platform line, AGENTS.md's "Windows only",
    the multi-host plan's section 6 marked superseded. Risk Low. Proof: docs gate.

Slices 9 to 11 and 13 can run in parallel after slice 7 (slice 9 touches lock/store.py, so it
does not overlap another lock slice); the others are in order.

## 9. Risks and unknowns

- Unknown: whether `from ctypes import wintypes` imports on Linux on the kit's Python (it raised
  on some older versions); slice 2 checks, and makes it lazy or guarded either way in all nine
  modules of section 1.1 (rule 3).
- Unknown: Claude Code's Linux messaging address, its hook interpreter lookup, its managed
  settings path and its session file fields; slice 15 and 12 probe first.
- Unknown: whether Claude Code on Linux kills a tool call's process tree by pid when it ends the
  call, as it does on Windows (the reason for the pid shield). Sections 1.3 and 2.2 infer it does
  not; slice 7 probes it on a Linux box before it lands.
- Unknown: how the owner's self-hosted runner delegates cgroups to the runner user; slice 5 may
  need `sudo systemd-run` in CI only, which tests the backend but not the delegation step.
- Risk: the weak containment fallback looks like it works. Mitigation: named, shown in status,
  refused for heavy runs by default.
- Risk: guard semantics drift between Windows and Linux. Mitigation: one placer with a platform
  argument, and the guard tests run both placers on every platform (they are pure).
- Risk: the seam move changes Windows behaviour by accident. Mitigation: re-exports first, the
  full Windows suite on every slice, and the Windows CI leg once it exists.
- Risk: multi-user and container boxes break the one-store rule. Mitigation: section 2.4.
- Risk: the scope is large (16 branches, most High). It is paced by the coordinator's caps;
  nothing here asks for parallel heavy runs.

## 10. Questions for the owner

1. **macOS.** ANSWERED (owner, 2026-10-26): out of this plan; macOS gets its own plan later.
   The seam names darwin unsupported and refuses it by name (section 7).
2. **Linux CI runner.** ANSWERED (owner, 2026-10-26): a self-hosted box; support any Linux
   build, VM included; the primary target is full Linux support out of the box. Not
   GitHub-hosted. I read this as leaving open whether the box is separate hardware or a VM on
   the owner's Windows machine, and so whether its heavy work sits outside the machine lock's
   view. A Linux container for a quick local check is a tool, not a runner.
3. **Spending.** ANSWERED (owner, 2026-10-26): a Linux box or VM, with real hardware and GPU
   tests. No hosted-runner spend is planned, so the repository's visibility no longer matters
   to cost.
4. **No cgroup v2 delegation.** (a) refuse heavy runs and say why; (b) run them with weak
   containment, shown as weak; (c) refuse the whole lock. Recommendation: (a).
5. **Supported Linux.** (a) systemd distributions, kernel 5.14+, Ubuntu 24.04 as the reference;
   (b) any Linux with cron fallbacks everywhere; (c) Ubuntu only. Recommendation: (a), with cron
   kept only for scheduled jobs.
6. **Session windows.** (a) tmux sessions, attachable from any terminal; (b) a configured
   terminal emulator command; (c) both, tmux by default. Recommendation: (c).
7. **The bridge and peer messaging on Linux.** (a) after parity of the rest; (b) in the first
   pass; (c) never. Recommendation: (a).
8. **A Windows CI leg** (section 4 writes it as a proposal pending this answer). (a) yes, after
   the Linux leg is green; (b) no, the owner's machine stays the only Windows proof.
   Recommendation: (a); it catches shared-code regressions off the owner's machine. Question 3
   is answered with no hosted spend, so a hosted windows-latest leg's cost is unchecked.
9. **Hook manifest form.** (a) machine setup installs a `pythonw` shim on the Linux PATH so one
   committed manifest serves both; (b) per-platform manifests rendered side by side; (c) one
   manifest running `python3 || pythonw` through the shell. Recommendation: (b), because a
   missing shim fails open (section 3), while a rendered manifest is checked by the render test.
10. **Multi-user boxes** (section 2.4 writes the shared store as a proposal pending this
    answer). (a) one kit user per Linux box: machine setup refuses a second; (b) a shared machine
    store under /var/lib with a group. Recommendation: (a) now, (b) when a second user appears.
11. **WSL.** (a) out: tools/kit.py refuses WSL (its kernel release names Microsoft) like any
    unsupported platform; (b) in: WSL2 with systemd counts as a Linux box; (c) later, after
    native Linux parity, as its own decision. Recommendation: (c), refusing WSL meanwhile. A kit in
    WSL beside the Windows kit on one machine would keep a second lock store for the same CPUs
    and memory and double-book them (section 2.4), and WSL's cgroup delegation and systemd are
    not the reference target (question 5).

## 11. Proposed build records

For the coordinator to file, in the store's id form (these slugs map to its uppercase ids). Each
carries its own scope.

| Slug | Summary | Scope |
|---|---|---|
| linux-ci-leg | Linux CI leg, ratchet baseline, platform test markers | .github/workflows/linux.yml, kitlib/ci/, tools/ci_ratchet.py, tests/ci/, tests/platforms.py, tests/linux-baseline.txt, tests/ (skip moves) |
| linux-import-seam | Import-clean kitlib, kitlib/platform skeleton, no-os-checks test, hook block while unready, per-platform process names | kitlib/platform/, kitlib/proc/win32.py, kitlib/proc/pipe.py, kitlib/gate/outfile.py, kitlib/lock/layout.py, kitlib/lock/store.py, kitlib/proc/cpu.py, kitlib/proc/process.py, kitlib/proc/shield.py, kitlib/proc/winwatch.py, kitlib/host/hookmain.py, kitlib/host/registry.py, hosts.json, tests/platform/, tests/host/, tests/gate/ |
| linux-locks | flock named locks, run liveness, treelock and writelock, on a test store root | kitlib/platform/linux/locks.py, kitlib/lock/store.py, kitlib/fs/treelock.py, kitlib/fs/writelock.py, tests/lock/, tests/fs/ |
| linux-process-identity | /proc identity, ancestry, kill; the boot folded into the Linux ctime | kitlib/platform/linux/process.py, kitlib/proc/process.py, kitlib/lock/run.py, tests/proc/, tests/lock/ |
| linux-containment | cgroup v2 Job contract, weak fallback, pool and unit runner on Linux | kitlib/platform/linux/contain.py, kitlib/platform/linux/cpu.py, kitlib/proc/job.py, kitlib/tooldeps/pool.py, kitlib/pytests/runner.py, tests/proc/, tests/tooldeps/ |
| linux-lock-hardware | /sys and /proc hardware detection, synthetic /sys trees | kitlib/platform/linux/hardware.py, kitlib/lock/layout.py, tests/lock/ |
| linux-lock-runs | slots, give-back, detach and gate submit on Linux, on a test store root | kitlib/lock/run.py, kitlib/lock/detach.py, kitlib/gate/submit.py, kitlib/platform/linux/detach.py, tests/lock/, tests/gate/ |
| linux-guard-paths | Guard placer for POSIX paths and the safety findings | kitlib/guard/, tests/guard/ |
| linux-config-paths | Pointer, profile, temp, environment, the lock store's real root; worktree holders from /proc; CI leg on gate.py | kitlib/platform/linux/paths.py, kitlib/config/, kitlib/lock/store.py, kitlib/worktree/holders.py, kitlib/worktree/classify.py, .github/workflows/linux.yml, tests/config/, tests/lock/, tests/worktree/ |
| linux-scheduled-jobs | systemd user timers for every scheduled task, read-back | kitlib/platform/linux/schedule.py, kitlib/launcher/autostart.py, kitlib/hubsite/autostart.py, kitlib/worktree/task.py, kitlib/machinesetup/, kitlib/pmstart/consoletasks.py, tests/ |
| linux-sessions | tmux sessions, stop, process listing, launch flags | kitlib/launcher/, kitlib/pmstart/, kitlib/autotasks/, tests/launcher/, tests/pmstart/ |
| linux-hook-manifests | Per-platform hook interpreter and manifests | hosts.json, kitlib/host/hookmain.py, kitlib/host/content.py, hosts/*/shell/, tests/host/ |
| linux-shell-text | Per-platform skill text, hub to-do step forms, Codex dialect | content/, kitlib/hubsite/todosteps.py, hosts/codex/adapter/, tests/hubsite/, tests/content/ |
| linux-machine-setup | Fresh-box machine setup and installer, kit.py stops refusing Linux | installer/machine-setup.sh, kitlib/machinesetup/, tools/machine_setup.py, tools/kit.py, tests/machinesetup/ |
| linux-peer-bridge | Peer messaging and the bridge on Unix sockets, procwatch | hosts/claude/adapter/peer.py, hosts/claude/adapter/peer_worker.py, optional/bridge/, kitlib/machinesetup/procwatch.py, tests/ |
| linux-docs | Owner setup docs and the platform lines in the contract docs | docs/, ARCHITECTURE.md, AGENTS.md |
