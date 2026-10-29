# Load nodes

A load node is a compute-only Ubuntu box on the owner's network that takes heavy runs (tests,
builds) off the main machine. The main machine reaches it over ssh as one account (`pmnode`),
with one key that can run only the kit's forced command, and the box's host key pinned in the
registry. The node never gets GitHub access: a job's commit arrives by `git push` from main.

`tools/node.py` is the main machine's side; `optional/loadnode/loadnode.py serve` (the forced
command) and `kitlib/loadnode/box.py` are the node's.

## The registry

The owner's list of nodes is `<PM repo>/.pm-kit/load-nodes.json`. It names machines and accounts
on the owner's network, so it is never committed: `node.py` refuses it unless git says the PM
repository ignores it.

```json
{"version": 1,
 "nodes": [{"name": "box1", "address": "192.168.1.50", "user": "pmnode",
            "host_key": "ssh-ed25519 AAAA...", "slots": 1, "memory_mb": 12288,
            "port": 22, "identity_file": "~/.ssh/pmkit_node_ed25519",
            "repos": ["my-repo"]}]}
```

- `host_key`: the box's `/etc/ssh/ssh_host_ed25519_key.pub`, read on the box itself. ssh is
  given only this key for the node (a temp known_hosts file), so a changed key is refused.
- `slots`: the most `node.py run` jobs the node takes at once.
- `memory_mb`: a routing check only. `--node auto` passes over a node with less free memory than
  `memory_mb / slots`. What a job really needs is the owner's to know.
- `repos`: the opt-in. Only these repositories (their machine lock repo names) may run on the
  node, named or chosen. Left out, the node takes none.
- `port` (22) and `identity_file` (ssh's default key) may be left out. Unknown keys are refused;
  `_x` and `comment` notes are allowed.

## Setting up a node

On the box, once, by its owner: `sudo sh setup-node.sh <main-ip> '<main public key line>'` from
`optional/loadnode/`. Its header lists the options: `--kit-src` for the kit tar, `--allow-user`
for the admin account, and `--dry-run`. The script makes the `pmnode` account and its forced-command
key line, locks sshd and the firewall to main's address, and installs Python 3.14 and the kit at
`/opt/pm-kit`. A re-run with the same arguments changes nothing. Toolchains a job needs (compilers,
SDKs) are the owner's to install.

## node.py list and status

- `node.py list`: each node, what it may take, its host key fingerprint, and whether its identity
  file is there. No network.
- `node.py status [NAME ...] [--json] [--timeout S]`: asks each node for its machine lock,
  hardware, the verbs it serves and its live jobs. Exit 0 when every node answered, 1 when one did
  not (every node is still listed). A node reads its own free memory through the platform
  (`kitlib.platform.memory_mb`: `/proc/meminfo` and the cgroup's limit on Linux).
- `machine_lock.py status` (text) ends with one `node:` line per node in the registry, asked the
  same way with a 10 second limit (`kitlib.loadnode.dispatch.status_lines`):
  `node: NAME  ok  cpu F/S free  gpu F/S free  memory X/Y GB free  N running, M waiting`, with
  `kit V (here W)` added when the node's kit differs, or `node: NAME  STATE: detail` for a node
  that cannot be used (unreachable, host key mismatch, no reply and the like). No registry is no
  line; a registry that cannot be used is one line saying why. `status --json` is unchanged.

## Running a job

```
node.py run --kind K --repo R --slug S [--node auto|NAME] [--artifact GLOB]...
            [--max-wait D | --no-wait] [--expect D] [--note TEXT] [--reattach S] [-C DIR] -- <command>
```

The job is the checkout's HEAD:

1. Main checks the job as the node will (a `--note` of at most 200 characters, waits of at most
   7 days) and then pushes HEAD to `refs/node/<job>` on the node's mirror of the repository,
   through the pinned ssh. No remote is added to the checkout. A start the node refuses drops
   that ref again.
2. The node checks the ref, makes a worktree at that commit, and starts the command under its own
   machine lock, detached from the ssh call.
3. The output streams back as it comes.
4. The files matching each `--artifact` glob (relative to the checkout's top) are copied back to
   the same paths in the checkout. A glob is ASCII, has `/` between its parts and no `..` or
   `.git` part, and is not absolute (`kitlib.loadnode.box.glob_ok`); another is usage, exit 2.
5. The node removes the job.

With `--node NAME`, the checkout must have no uncommitted tracked changes (the node runs HEAD) and
the node must list the repository in `repos`.

Artifacts land only when every one came whole. Each is checked for size and sha256 and fetched
once more on a failure. None may be a tracked file in any case (NTFS opens `CONFIG.H` as a tracked
`config.h`), none may have a part shaped like an 8.3 short name (`~` and a digit), every path is
ASCII (git's case-blind match folds ASCII letters only, NTFS folds all of them), and none may
land outside the checkout. Main copies all of them or none: each goes first to a new file beside
its place, then all are moved in, and a copy or move that fails (a locked file, a file where a
folder must be) puts the checkout back as it was. When they do not all land, the staging folder
is kept and named, and the job stays on the node until a later start sweeps it (7 days after it
ended); `node.py follow` with the same globs fetches them again.

When the machine config turns lock-window notices on for the kind, a start notice and an end
notice bracket the job's window on the node, as for a run on main.

### Choosing a machine

`--node auto` (the default) runs the job where the kind's slots are free now. A machine qualifies
when its machine lock has the slots free and nobody waits there. A node also qualifies only when:

- it lists the repository in `repos`;
- it answered and serves the run verbs;
- its lock is fully readable;
- it runs fewer jobs than `slots`;
- it has `memory_mb / slots` free.

A node's need is the kind's size on main capped at the node's slots. The machine with the most
free cpu slots wins; a tie goes to main. When none qualifies, the job runs on main, whose lock
queues it.

Auto runs on main, and says why, for any of these:

- tracked changes are not committed;
- no node opted the repository in;
- there is no registry;
- main's lock cannot be read;
- the kind cannot be sized.

A node that fails before its job starts, or is busy, gives way to main. Once the node has started
the job, only the node's own lock refusing it as busy (the command never ran) still does; no
other failure sends it to main, where it would run twice.

### A dropped connection

When the connection drops, main reattaches from the last whole frame it had, after 2, 4, 8, 16
and then every 30 seconds. It keeps trying for `--reattach` seconds (default 120) from the last
data it got. Past that the job is LOST: it keeps running on the node, and main says the lines to
pick it up:

```
node.py follow NODE JOB [-C DIR] [--artifact GLOB]...   stream it to its end, fetch, clean
node.py stop NODE JOB                                   ask the node to stop it
```

The follow line said on LOST already names the checkout and the globs: a follow without them
cleans the job and its artifacts with it.

## Lock runs on a node

```
machine_lock.py run --kind K --repo R --slug S [--node auto|main|NAME] [--artifact GLOB]... [options] -- <command>
```

`machine_lock.py run` sends the run to a load node by the same rules as `node.py run`
(`kitlib.loadnode.dispatch.Router`, asked by `kitlib.lock.run.run_command` after its own checks
and before anything is booked on main). A run on a node books nothing in main's lock; the node's
lock holds it, and its exit code comes back as the run's.

- `--node auto` (the default): the run goes to a node when one qualifies and has more free cpu
  slots than main ("Choosing a machine"); main says `running on load node NAME: why`, or
  `running here: why` when main wins. When the run cannot go to a node it runs here as before.
  Most of these are silent: no registry, no node has opted the repository in, a run inside
  another lock run (`MACHINE_LOCK_RUN`), an unknown kind, or an option only main has
  (`--cores`, `--hold`, `--gpu-share`, `--within`, `--resource`, `--on-release`, `--detach`
  and a detached run, and a library caller's own `env=`). The others say `running here, not on a load node: why`: a
  registry that cannot be used, a Windows command, the run not started in the checkout's top,
  uncommitted tracked changes, a start the node would refuse (a `--note` over 200 characters, a
  wait over 7 days), a repo or slug a node does not take, a kind that cannot be sized, or main's
  lock that cannot be read.
- A Windows command stays on main: its first word ends in `.bat`, `.cmd`, `.ps1` or `.exe`, or is
  `powershell`, `pwsh` or `cmd`, or an argument holds a drive letter (`C:` followed by a slash or
  the end) or a backslash (`kitlib.loadnode.dispatch.windows_only`).
- `--node main`: the run stays here, and no node is asked.
- `--node NAME`: the run goes to that node or not at all. Any reason above is usage, exit 2
  (`usage: --node NAME: why`), as is `--detach`; a node that cannot be used before the job starts
  is exit 10 and nothing ran.
- A node that fails before the job starts, or is busy, gives way to main under auto (`running
  here instead`). Once the node has started the job no failure sends it to main: a LOST
  connection is exit 9 with the follow and stop lines, and a job that ended with no exit code of
  its own or could not be followed is exit 14.
- Ctrl+C before the job starts is exit 130 and nothing ran on a node. After it starts, main asks
  the node to stop the job, says the `node.py follow` line, and exits 130.
- `--artifact GLOB` (repeatable) is the same as `node.py run`'s and is used only on a node.
  `--registry` (hidden, on `run` and `status`) names another registry file, for tests.

## Exit codes

| Code | `run` and `follow` | `list`, `status` and `stop` |
|---|---|---|
| 0 and up | the command's own exit code | 0 every node answered; for stop, asked |
| 1 | | a node did not answer; for stop, the node refused |
| 2 | usage: an unknown node or kind, a repository not opted in, uncommitted changes for `--node NAME`, a bad glob, a start main checks as the node would refuse (a `--note` over 200 characters, a wait over 7 days) | usage, or a registry that is missing, invalid or not ignored by git |
| 6 | the node was busy (with `--node NAME`) | |
| 9 | LOST: the connection did not come back; the job runs on | |
| 10 | the node could not be used before the job started: unreachable, host key, push, start refused, a kit too old for the run verbs; nothing ran | |
| 13 | the job succeeded but its artifacts did not all come, or could not all be copied into the checkout (none was) | |
| 14 | the node started the job, but it ended there with no exit code of its own (its runner vanished, the node lost it) or could not be followed; it is not run again on main | |
| 130 | Ctrl+C; a started job is asked to stop and the follow line is said | |

`machine_lock.py run` on a node gives the same codes: 2, 6 and 10 only with `--node NAME` (auto
runs the job on main instead), and 9, 13, 14 and 130 as above.
