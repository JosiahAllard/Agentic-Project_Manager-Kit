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
  test pool. kitlib/proc/pipe.py does the same at import.
- The committed Claude hook manifest (hosts/claude/shell/hooks/hooks.json) runs `pythonw`, and
  kitlib/host/hookmain.py keeps `INTERPRETERS = {'windows': 'pythonw'}`; hosts.json lists
  `"platforms": ["windows"]` for both hosts, with `.exe` process names (verified).

### 1.2 The surface, counted

A scan of the 1,265 tracked Python files (529 outside tests/) for each mechanism. The PM's first
counts are in the last column; where they differ, this scan's figure stands.

| Mechanism | Files (all) | Outside tests | PM's count |
|---|---|---|---|
| Direct Win32 (ctypes windll/WinDLL, msvcrt, winreg, wintypes) | 75 | 42 | ~40 (outside tests: right) |
| Job objects: API calls | 4 | 3 | 8 |
| Job objects: mentioned | 22 | | |
| CPU affinity / processor groups: API calls | 6 | 4 | 8 |
| CPU affinity / cpu sets: mentioned | 26 | | |
| WMI / CIM | 19 | 8 | 17 |
| Named pipes | about 20 | | 22 (about right) |
| Scheduled tasks | 28 | 14 | 26 |
| PowerShell (spawned or written) | 153 | 62 | 92 (between the two) |
| Branches on os.name / sys.platform | 119 | 23 | ~107 (96 of the 119 are test skips) |
| creationflags | 72 | 27 | |
| Named mutexes | 4 | 3 | |
| msvcrt | 6 | 5 | |

So the code that needs a POSIX backend is about 42 library files, not 40 spread evenly: it
concentrates in kitlib/proc, kitlib/lock, kitlib/fs/treelock.py, kitlib/config, kitlib/launcher,
kitlib/machinesetup, the scheduled-task writers and the bridge.

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
- **Shields with no Linux need**: shield.py shield (a DACL against Claude Code's tree kill),
  PidHold (handles that stop pid reuse), console.py (hooks CreateProcess so no console window
  flashes), winwatch.py (user32 window watch), spawn.py no_window_flags and pythonw_beside.
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

Every content file matches a PowerShell pattern, but nearly all of that is one shared root-search
line. The real PowerShell-only text is two of the 21 skills (shell-and-worktree-traps,
branch-sync) and lines in five of the 13 roles, about a dozen lines outside
shell-and-worktree-traps. 61 docs mention PowerShell. The content renderer (kitlib/host/content.py,
kitlib/content) has per-host tokens only ({{name}}, {{agent:}}, {{model:}}, {{tool:}}); there is
no per-platform text. The hub's owner to-do steps (hubsite/todosteps.py) render `{run:<command>}`
as PowerShell, following the owner's ruling that hand steps carry commands. Codex's dialect
default is 'powershell' (hosts/codex events.py).

### 1.5 Tests and CI

- No CI: no workflow, no .github folder. The repository is public on GitHub.
- 69 test files carry about 159 `skipUnless(os.name == 'nt', ...)` with free-text reasons.
- The kit's own runners need job objects: tools/kit_tests.py (the pool) and gate.py (the pool and
  the machine lock, which fails closed). A first Linux leg cannot use either.
- tests/realgit.py `real_git(reason)` is the house pattern for a marker with a reasons table.

## 2. The platform layer

### 2.1 The seam

`kitlib/platform/` is the one place that knows the operating system.

```
kitlib/platform/
  __init__.py     picks the backend once from sys.platform; exports NAME ('windows'|'linux')
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
   23 library files that branch today; the allowlist only shrinks.
2. The Windows backend starts as re-exports, so moving a caller onto the seam changes no Windows
   behaviour; the Windows suite proves each move.
3. No module imports a Windows DLL at import time. win32.py and pipe.py bind lazily (first call),
   so an import on Linux works and a call fails with a clear "not on this platform" error.
4. Guard path text goes through one function per shell (kitlib/guard/paths.py norm and the
   shell's resolve_path); hook commands come from one helper (hookmain.manifest_command), as the
   multi-host plan already ruled.
5. A Linux primitive that cannot give the Windows guarantee says so in its name or raises; it never
   silently does less (see 2.3 and owner question 4).

### 2.2 Windows mechanism to Linux mechanism

| Windows | Linux | Notes |
|---|---|---|
| (pid, FILETIME ctime) identity | (pid, starttime from /proc/<pid>/stat field 22) plus boot_id | the nest token becomes `rid:pid:starttime`; boot_id from /proc/sys/kernel/random/boot_id stops a reboot from reviving a token |
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
| pid shield, PidHold | nothing needed: no tree kill by pid; pidfd where a hold is wanted | |
| Git Bash lookup | /bin/bash | kitlib/proc/bash.py answers /bin/bash |
| conhost + PowerShell session window | tmux session (or a configured terminal command) | owner question 6 |
| Get-CimInstance Win32_Process | /proc scan | pmstart, launcher/stop |
| `.exe` process names in hosts.json | per-platform names (`claude`, `node`, `codex`) | hosts.json gains a per-platform field |

### 2.3 The Job contract on Linux

The lock's promises rest on job objects: a slot's processes stay on its CPUs and under its
memory, the whole tree dies with the slot, and no child escapes. On Linux the equal mechanism is
a cgroup v2 leaf the kit owns:

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
- No escape: a child cannot leave a cgroup it has no write access to; setsid and double fork do
  not move it (this is stronger than process groups).

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
own and the backend closes them: two users on one box each get a store, so the store moves to a
machine path (`/var/lib/pm-kit/machine-lock`, group-writable by a `pm-kit` group) when machine
setup finds more than one kit user, and a container sees the host's CPUs in /sys while its cgroup
allows fewer, so the allowed set comes from sched_getaffinity and the cgroup's cpuset.cpus.effective,
never from /sys alone. The proving test feeds recorded /sys trees (fake_hardware, extended) for a
plain desktop, a hybrid part, a box with gaps and a container, and checks that no two slots
share an id.

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

The ordering rule that keeps Linux safe meanwhile: **until the guard slice lands, the guard
refuses every write on Linux.** Today the Claude manifest's `pythonw` does not exist on Linux, and
a hook command that cannot start is, for Claude Code, a non-blocking error (inferred from its hook
contract): the tool call goes ahead unguarded. So slice 2 adds a check in hookmain that blocks
every guarded tool call when the platform is not one the guard declares ready, and the hook
manifest slice (12) cannot land before the guard slice (8).

## 4. CI and test marking

- `.github/workflows/linux.yml`: ubuntu-latest (24.04), actions/setup-python at the Python the
  kit runs on (3.14 on the owner's machine today), git configured with a throwaway identity.
- The first leg runs `python -m unittest discover -s tests -t .` in a ratchet mode, not
  kit_tests.py or gate.py (both need job objects and the lock). A small script
  (tools/ci_ratchet.py) runs the suite, writes the failing and erroring test ids, and compares them
  with tests/linux-baseline.txt: a new failure fails the leg, a fixed one must be removed from the
  baseline in the same branch. Import failures count as failures.
- tests/platforms.py gives `windows_only(reason)` and `posix_only(reason)`, built like
  tests/realgit.py with a reasons table keyed by module.Class. The 159 bare `skipUnless(os.name ==
  'nt')` move to it in one mechanical branch, so `kit_tests --list` can count Windows-only tests by
  reason and the count is reported per slice.
- When the pool runs on Linux (slice 5), the leg switches to `tools/kit_tests.py`; when the lock
  runs (slice 7), to `gate.py --full` with a CI lock profile.
- A Windows leg (windows-latest) follows once Linux is green, so a Windows regression from a
  shared change is caught off the owner's machine too.
- CI adds only strictness: nothing in the landing path reads it until the owner says so.

## 5. Per-platform shell text

- Skills and roles: a renderer token `{{os:windows|linux}}...{{/os}}` would double every rendered
  file; the plan prefers the smaller fix: write the shared text shell-neutral (most of it is
  already: `git`, `python`), and give shell-and-worktree-traps one section per platform, picked by
  the agent from its own platform line. Only the dozen PowerShell-only lines change.
- The hub's to-do steps: `{run:<command>}` gains per-OS forms (`{run.windows:...}`,
  `{run.linux:...}`); the hub shows the form for the machine the step is for (the owner's
  machine record says its OS), and a step with only one form shows it with its shell named. The
  "hand steps carry commands" ruling is kept: every step still carries the command to run.
- Codex's dialect default follows the platform (bash on Linux).
- Docs: each owner-facing setup doc gets a Linux block beside the Windows one; the 61 docs that
  mention PowerShell are sorted in the docs slice: most are Windows-internal and say so.

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

Out of scope for this plan. The seam keeps a `darwin` slot so the work can follow, but nothing
here builds or tests it: macOS has no cgroups (the lock could only limit concurrency, the old
question 2), no /proc, launchd instead of systemd, and case-insensitive paths by default. Owner
question 1 asks to confirm.

## 8. Slices

Each slice is one branch, landed in this order unless marked; "Proof" is the test that must be
green before it lands. Risk uses the kit's levels.

1. **CI leg and test markers.** `.github/workflows/linux.yml`, tools/ci_ratchet.py,
   tests/platforms.py, tests/linux-baseline.txt, the mechanical move of the nt skips. Risk High
   (CI config). Proof: the leg runs green on the baseline; a planted failure fails it;
   `kit_tests --list` shows the same skip count on Windows before and after.
2. **Import-clean kitlib and the seam skeleton.** kitlib/platform/ (api, windows re-exports,
   linux stubs that raise), lazy DLL binding in proc/win32.py and proc/pipe.py, the
   no-os-checks test with its allowlist, and hookmain's block-while-unready check. tools/kit.py
   keeps refusing. Risk High. Proof: on Linux every kitlib module imports (a test walks them);
   on Windows the full suite is unchanged; a guarded tool call on Linux blocks.
3. **Locks.** linux/locks.py: flock named locks, run liveness, treelock and writelock on fcntl;
   lock/store.py lock() and RunMutex through the seam. Risk High (concurrency). Proof: two
   processes contend for a named lock; a killed holder frees it; run_dead sees a killed run;
   the treelock tests run on both.
4. **Process identity and kill.** linux/process.py; lock/run.py nest token with starttime and
   boot_id. Risk High. Proof: ancestry, descendants, command_line and kill_tree on a spawned
   tree; a nest token from before a pid reuse is refused.
5. **Containment and the pool.** linux/contain.py (cgroup v2 and the weak fallback), cpu
   affinity, tooldeps/pool.py and pytests/runner.py on the seam; the CI leg moves to
   kit_tests.py. Risk High. Proof: a child that calls setsid stays in the cgroup and dies with
   it; memory.peak is read; kill_on_close kills the tree when the owner dies; the weak mode is
   reported as weak.
6. **Lock hardware.** linux/hardware.py feeding lock/layout.detect; fake_hardware gains recorded
   /sys trees. Risk High (lock logic). Proof: the four recorded machines divide with no shared
   id; the Windows layout tests are unchanged.
7. **Lock runs, detach and the gate.** lock/run.py Slot, give-back, release, detach and
   gate/submit on the seam; the CI leg moves to gate.py --full. Risk High. Proof: a detached run
   survives its caller; give-back narrows cpuset.cpus; a landing runs end to end in CI on a
   throwaway repository.
8. **Guard path semantics.** The placer, the ten findings in section 3 as tests first, then the
   fixes; the guard declares Linux ready. Lands alone after a strong-tier review. Risk High.
   Proof: each finding's test refuses on Linux; the Windows guard suite is unchanged.
9. **Paths and configuration.** linux/paths.py: pointer, profile, temp, environment; config/user.py
   and config/pointer.py on the seam; worktree/holders.py and classify.py from /proc (holders
   stops failing open). Risk Medium. Proof: machine config resolves on Linux from the pointer
   file; holders finds a process whose cwd is in a worktree.
10. **Scheduled jobs.** linux/schedule.py; autostart, hubsite autostart, worktree and temp
    sweeps, automatic tasks, consoletasks read-back. Risk Medium. Proof: units render and
    round-trip through read-back; with user systemd in CI, a timer fires once.
11. **Sessions.** launcher window (tmux), stop, pmstart process listing, autotasks launch flags.
    Risk Medium. Proof: a launched session is found by name and stopped; no breakaway flag off
    Windows.
12. **Hook interpreter and hosts.json.** per-platform interpreters and process names, the
    manifest form owner question 9 picks. Needs slice 8. Risk High (hook entry). Proof: the
    rendered manifest runs the hook on both; a missing interpreter blocks rather than passes.
13. **Shell text.** shell-and-worktree-traps sections, the dozen role/skill lines, hub to-do step
    forms, Codex dialect. Risk Low. Proof: content render check; hub renders each form.
14. **Machine setup on Linux.** installer/machine-setup.sh, machinesetup steps on the seam,
    tools/kit.py stops refusing Linux. Risk High (install). Proof: machine setup on a fresh
    Ubuntu container ends with no problems and the lock status shows strong containment.
15. **Peer messaging and the bridge.** peer.py on Claude Code's Linux socket (after a probe), the
    bridge's leader lock and transport on Unix sockets, procwatch. Risk High (networking). May
    stay Windows-only at first (owner question 7).
16. **Docs.** owner setup docs, ARCHITECTURE.md's platform line, AGENTS.md's "Windows only",
    the multi-host plan's section 6 marked superseded. Risk Low. Proof: docs gate.

Slices 9 to 11 and 13 can run in parallel after slice 7; the others are in order.

## 9. Risks and unknowns

- Unknown: whether `from ctypes import wintypes` imports on Linux on the kit's Python (it raised
  on some older versions); slice 2 checks and binds it lazily either way.
- Unknown: Claude Code's Linux messaging address, its hook interpreter lookup, its managed
  settings path and its session file fields; slice 15 and 12 probe first.
- Unknown: how GitHub's hosted runners delegate cgroups to the runner user; slice 5 may need
  `sudo systemd-run` in CI only, which tests the backend but not the delegation step.
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

1. **macOS.** (a) out for now, the seam keeps a slot; (b) in, with concurrency-only locking;
   (c) in, later, as its own plan. Recommendation: (a), stated in the docs.
2. **Linux CI runner.** (a) GitHub-hosted ubuntu-latest, free for this public repository;
   (b) a self-hosted runner on a Linux box; (c) Docker Desktop on this machine, run by hand.
   Recommendation: (a), with (c) for local checks; no runner on the owner's machine.
3. **Spending.** (a) none: hosted standard runners cost nothing for a public repository;
   (b) larger hosted runners for faster full runs; (c) a Linux box or VM for real hardware and
   GPU tests. Recommendation: (a) now; (c) only if the owner wants GPU slots tested on real
   hardware.
4. **No cgroup v2 delegation.** (a) refuse heavy runs and say why; (b) run them with weak
   containment, shown as weak; (c) refuse the whole lock. Recommendation: (a).
5. **Supported Linux.** (a) systemd distributions, kernel 5.14+, Ubuntu 24.04 as the reference;
   (b) any Linux with cron fallbacks everywhere; (c) Ubuntu only. Recommendation: (a), with cron
   kept only for scheduled jobs.
6. **Session windows.** (a) tmux sessions, attachable from any terminal; (b) a configured
   terminal emulator command; (c) both, tmux by default. Recommendation: (c).
7. **The bridge and peer messaging on Linux.** (a) after parity of the rest; (b) in the first
   pass; (c) never. Recommendation: (a).
8. **A Windows CI leg.** (a) yes, after the Linux leg is green; (b) no, the owner's machine
   stays the only Windows proof. Recommendation: (a); it is free and catches shared-code
   regressions.
9. **Hook manifest form.** (a) machine setup installs a `pythonw` shim on the Linux PATH so one
   committed manifest serves both; (b) per-platform manifests rendered side by side; (c) one
   manifest running `python3 || pythonw` through the shell. Recommendation: (b), because a
   missing shim fails open (section 3), while a rendered manifest is checked by the render test.
10. **Multi-user boxes.** (a) one kit user per Linux box; (b) a shared machine store under
    /var/lib with a group. Recommendation: (a) now, (b) when a second user appears.

## 11. Proposed build records

For the coordinator to file, in the store's id form (these slugs map to its uppercase ids). Each
carries its own scope.

| Slug | Summary | Scope |
|---|---|---|
| linux-ci-leg | Linux CI leg, ratchet baseline, platform test markers | .github/workflows/linux.yml, tools/ci_ratchet.py, tests/platforms.py, tests/linux-baseline.txt, tests/ (skip moves) |
| linux-import-seam | Import-clean kitlib, kitlib/platform skeleton, no-os-checks test, hook block while unready | kitlib/platform/, kitlib/proc/win32.py, kitlib/proc/pipe.py, kitlib/host/hookmain.py, tests/platform/ |
| linux-locks | flock named locks, run liveness, treelock and writelock | kitlib/platform/linux/locks.py, kitlib/lock/store.py, kitlib/fs/treelock.py, kitlib/fs/writelock.py, tests/lock/, tests/fs/ |
| linux-process-identity | /proc identity, ancestry, kill; nest token with starttime and boot_id | kitlib/platform/linux/process.py, kitlib/proc/process.py, kitlib/lock/run.py, tests/proc/, tests/lock/ |
| linux-containment | cgroup v2 Job contract, weak fallback, pool and unit runner on Linux | kitlib/platform/linux/contain.py, kitlib/platform/linux/cpu.py, kitlib/proc/job.py, kitlib/tooldeps/pool.py, kitlib/pytests/runner.py, tests/proc/, tests/tooldeps/ |
| linux-lock-hardware | /sys and /proc hardware detection, recorded machines | kitlib/platform/linux/hardware.py, kitlib/lock/layout.py, tests/lock/ |
| linux-lock-runs | slots, give-back, detach and gate on Linux | kitlib/lock/run.py, kitlib/lock/detach.py, kitlib/gate/submit.py, kitlib/platform/linux/detach.py, tests/lock/, tests/gate/ |
| linux-guard-paths | Guard placer for POSIX paths and the safety findings | kitlib/guard/, tests/guard/ |
| linux-config-paths | Pointer, profile, temp, environment; worktree holders from /proc | kitlib/platform/linux/paths.py, kitlib/config/, kitlib/worktree/holders.py, kitlib/worktree/classify.py, tests/config/, tests/worktree/ |
| linux-scheduled-jobs | systemd user timers for every scheduled task, read-back | kitlib/platform/linux/schedule.py, kitlib/launcher/autostart.py, kitlib/hubsite/autostart.py, kitlib/worktree/task.py, kitlib/machinesetup/, kitlib/pmstart/consoletasks.py, tests/ |
| linux-sessions | tmux sessions, stop, process listing, launch flags | kitlib/launcher/, kitlib/pmstart/, kitlib/autotasks/, tests/launcher/, tests/pmstart/ |
| linux-hook-manifests | Per-platform hook interpreter, manifests and process names | hosts.json, kitlib/host/hookmain.py, kitlib/host/content.py, hosts/*/shell/, tests/host/ |
| linux-shell-text | Per-platform skill text, hub to-do step forms, Codex dialect | content/, kitlib/hubsite/todosteps.py, hosts/codex/adapter/, tests/hubsite/, tests/content/ |
| linux-machine-setup | Fresh-box machine setup and installer, kit.py stops refusing Linux | installer/machine-setup.sh, kitlib/machinesetup/, tools/machine_setup.py, tools/kit.py, tests/machinesetup/ |
| linux-peer-bridge | Peer messaging and the bridge on Unix sockets, procwatch | hosts/claude/adapter/peer.py, hosts/claude/adapter/peer_worker.py, optional/bridge/, kitlib/machinesetup/procwatch.py, tests/ |
| linux-docs | Owner setup docs and the platform lines in the contract docs | docs/, ARCHITECTURE.md, AGENTS.md |
