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
   that ref again; a ref whose start never came (main stopped, or ssh failed before the start
   went out) is dropped by a later start on that node once its job id is 7 days old.
2. The node checks the ref, makes a worktree at that commit, and starts the command under its own
   machine lock, detached from the ssh call.
3. The output streams back as it comes.
4. The files matching each `--artifact` glob (relative to the checkout's top) are copied back to
   the same paths in the checkout. A glob is ASCII, has `/` between its parts and no `..` or
   `.git` part, and is not absolute (`kitlib.loadnode.box.glob_ok`); another is usage, exit 2.
5. The node removes the job: its worktree, with everything the command wrote that no
   `--artifact` glob copied back.

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

## Gating a branch on a node

```
node.py gate [--node auto|NAME] [--note TEXT] [--reattach S] [-C DIR]
```

The checkout's HEAD is gated `--full` on a node, and the gate-OK record it writes there is taken
into main's record folder (docs/gate.md "Gate-OK record"), so the branch lands as if gated here.

1. Main reads what its own gate would: the repository name (`.kit.json` "repo" "name", else the
   checkout's folder name: `kitlib.gate.runner.repo_name`), the gate's lock kind, and the default
   base (docs/gate.md "Default base") as a commit.
2. Main pushes HEAD to `refs/node/<job>` and the base to `refs/node/<job>-base`, then asks the
   node's `gate` verb. Main sends no command.
3. The node gives the job its own bare repository, `jobs/<job>/repo.git` (the mirror's objects
   through its alternates, no hooks), with `<main>` and `refs/remotes/<remote>/<main>` at the
   base, and a detached worktree of it at HEAD named `gate/<repo>`: the gate there measures
   against the same base and names the same repository. The job runner runs the node kit's own
   `tools/gate.py -C <worktree> --full`, which books the node's machine lock for the gate's kind
   itself. Ctrl+C or `node.py stop` reaches it as a Ctrl+C, then a kill after 60 seconds.
4. The output streams back. When the gate passes, the node reads the gate-OK record it wrote
   (`gate_ok.verify_full`) and puts it in the job's result.
5. Main takes the record only through `gate_ok.import_record`: every check `verify` makes, with
   the gate config main's own kit reads at the commit (the record's `config_rev` must be the
   commit itself and its `gate_config` hash that config's), a `--full` pass, and the record's
   `repo` main's name. Main adds `ran_on` (the node's name, its pinned host key's fingerprint and
   the job id, from main's registry), sets `input_keys` to null (a landing serves no step from
   another machine's run), writes it, says its path and cleans the job. With no `dev_drive` only
   the .git copy is written, with a warning, as the gate itself does. A node kit at another commit
   than main's is a warning only: the config hash check is what refuses.
6. A gate that fails or is stopped ends with its own exit code; no record is read.

A record main does not take is exit 15. The job stays on the node, and main says why and the
follow and stop lines; the follow line names the checkout, `node.py follow NODE JOB -C "<dir>"`,
and takes the record into it again once the cause is fixed (a follow with `-C` takes the commit
from the node's result, not from HEAD now).

`--node auto` (the default) chooses as "Choosing a machine" does, sized by the gate's kind, among
the nodes that opted the repository in and list the `gate` verb (a node whose kit is older is
named and passed over). When none qualifies, for each reason auto runs on main there, and when
the checkout's config or default base cannot be read, main runs its own
`gate.py -C <checkout> --full` and says why. A node that fails before its job starts gives way to
main; once it has started the job, nothing does. `--node NAME`: that node or nothing; an unknown
node, a repository not opted in, uncommitted tracked changes or a config that cannot be read is
usage, exit 2, and a node whose kit serves no `gate` verb is exit 10.

A kit checkout (this repository) is gated only by its own `tools/gate.py`: a node's installed kit
would gate it with other code than the checkout's. Auto keeps it on main and says so;
`--node NAME` is usage, exit 2. No lock-window notices are written for a gate job.

Known limits:

- Unproven on a live box: tested against the real node code and real git on the main machine
  only. The live proof follows the machine-setup proofs.
- The node's own copy of the record (in its record folder, when its machine config names a
  `dev_drive`) and its gate logs and result folder stay on the node; the job folder goes.
- `gate.py` itself does not send a gate to a node; `node.py gate` is the only way in.

## A landing's merged gate on a node

The parts a landing needs to run its merged gate (docs/gate.md "Landing") on a load node are in
the kit; the landing does not use them yet, so every landing still gates on main. The node checks
the job and runs the gate; main checks the record it sends back against main's own reading of the
same merge, step by step.

1. Main starts a `NodeJob` in land mode (`kitlib.loadnode.job`, `land={full, docs, kit_files}`):
   the merge commit, the base (main's tip the merge was made on), the repository name and main's
   kit files hash (`kitlib.gate.reuse.kit_files_hash`). Before the job's refs it pushes the base to
   the node mirror's `refs/pmkit/main/<main>` (never forced; a refusal or failure is a WARNING and
   the job goes on), so later job pushes send only what is new. The pre-push hook takes no part:
   it is not a branch.
2. The node's `land` verb (`box.check_land_spec`: the gate spec's keys, plus `full` and `docs` as
   booleans and `kit_files` as 64 lowercase hex) refuses unless `kit_files` is its own kit's files
   hash: `{"ok": false, "kit": true}`, and both job refs are dropped. A landing's gate runs only on
   a node with exactly main's kit files; any other kit, or one whose hash cannot be read, is for the
   landing to gate on main instead, saying why in one line. A node kit with no `land` verb is
   "its kit serves no land verb", as a kit with no `gate` verb is for `node.py gate`.
3. The job runner runs the node kit's `loadnode.py landgate <job>` (`kitlib.loadnode.landgate`,
   never served over ssh) in the job's worktree. It refuses again on another kit, a tree not at
   the merge, or a config at the base it cannot read (exit 4), runs `runner.merged_gate` as the
   landing would (`full`, `docs`), and fails (exit 1) if the gate left the tree changed. On a pass
   it writes a gate-OK record with the node kit's identity and files hash, `base` and `config_rev`
   at the base, the gate's `source`, and `mode` "docs" for a docs-only gate. The record is read
   back (`gate_ok.verify`) into the job's result with `land: true`.
4. Main takes a land record only in the `NodeJob` that started it (`take_land`), through the
   landing's checker (`landgate.checker`), which reads main's own selection once with
   `runner.merged_steps` (the steps `merged_gate` would run, run nothing) and refuses when main's
   own gate would refuse the merge. The record is refused, at its first failing check, unless:
   it names the merge; `gate_ok`'s checks pass (keys, schema, verdict, tree, author); its
   `gate_config` is the hash of main's config at the base; its `base` and `config_rev` are the
   base; its `mode` and `full` match main's; its `source` is main's; its steps are exactly main's
   by name, once each; every step's result is `pass` or `warn` (a `skip` or `reused` step is
   refused); its `repo` is main's name; and its kit files are main's.
5. A refused record is exit 15, the job kept and the follow and stop lines said, as for `gate`;
   `node.py follow` never takes a land record ("taken only by the landing that started it"). A
   taken one sets the job's `land_record` and `ran_on` (node, host key fingerprint, job), for the
   landing's own record on main (`gate_ok.record_pass(ran_on=...)`, with no `input_keys`).

A kit checkout is never gated on a node, by a landing or by `node.py gate`.

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
  and a detached run, and a library caller's own `env=`). The others say
  `running here, not on a load node: why`: a registry that cannot be used, a Windows command, the run not started in the checkout's top,
  uncommitted tracked changes, a start the node would refuse (a `--note` over 200 characters, a
  wait over 7 days), a repo or slug a node does not take, a kind that cannot be sized, or main's
  lock that cannot be read.
- A node runs committed HEAD from the checkout's top, as `node.py run` does: an untracked or
  git-ignored file the command needs (a new test file not committed yet, data, build output) is
  not there, and what the command writes is removed with the job unless `--artifact GLOB`
  copies it back.
- A Windows command stays on main: its first word ends in `.bat`, `.cmd`, `.ps1`, `.exe`, `.com`
  or `.vbs`, or is `powershell`, `pwsh`, `cmd` or `py`, or an argument holds a drive letter (`C:`
  followed by a slash or the end) or a backslash (`kitlib.loadnode.dispatch.windows_only`). The
  rule is best effort: a Windows path inside a shell's script (`bash -c "... /z/..."`), a
  forward-slash UNC path and the like slip past it. `--node main` keeps such a run here.
- `--node main`: the run stays here, and no node is asked.
- `--node NAME`: the run goes to that node or not at all. Any reason above is usage, exit 2
  (`usage: --node NAME: why`), as is `--detach`; a node that cannot be used before the job starts
  is exit 10 and nothing ran.
- A node that fails before the job starts, or is busy, gives way to main under auto (`running
  here instead`). Once the node has started the job no failure sends it to main: a LOST
  connection is exit 9 with the follow and stop lines, and a job that ended with no exit code of
  its own or could not be followed is exit 14. A start request that went out with no reply back
  (its ssh timed out or the link dropped) counts as started: the node may have taken the job and
  may still be running it, so it is exit 14 with the follow and stop lines, never a run on main
  and never exit 10. Neither 9 nor 14 is ever run again: follow or stop the job with those lines.
- Ctrl+C before the job starts is exit 130 and nothing ran on a node. After it starts, main asks
  the node to stop the job, says the `node.py follow` line, and exits 130. A second Ctrl+C while
  it asks says the follow and stop lines and exits 130.
- `--artifact GLOB` (repeatable) is the same as `node.py run`'s and is used only on a node.
  `--registry` (hidden, on `run` and `status`) names another registry file, for tests.

## Exit codes

| Code | `run`, `gate` and `follow` | `list`, `status` and `stop` |
|---|---|---|
| 0 and up | the command's own exit code (`gate`: the gate's) | 0 every node answered; for stop, asked |
| 1 | | a node did not answer; for stop, the node refused |
| 2 | usage: an unknown node or kind, a repository not opted in, uncommitted changes for `--node NAME`, a bad glob, a start main checks as the node would refuse (a `--note` over 200 characters, a wait over 7 days), a kit checkout gated with `--node NAME` | usage, or a registry that is missing, invalid or not ignored by git |
| 6 | the node was busy (with `--node NAME`) | |
| 9 | LOST: the connection did not come back; the job runs on | |
| 10 | the node could not be used before the job started: unreachable, host key, push, start refused, a kit too old for the run verbs (or for `gate`, the gate verb); nothing ran | |
| 13 | the job succeeded but its artifacts did not all come, or could not all be copied into the checkout (none was) | |
| 14 | the node started the job, but it ended there with no exit code of its own (its runner vanished, the node lost it) or could not be followed; or the start request went out and no reply came back, so the job may still be running there; the follow and stop lines are said, as for 9, and it is never run again, on main or by a rerun | |
| 15 | `gate` and `follow`: the gate passed on the node, but main did not take its gate-OK record; the job stays there and the follow line, with `-C`, takes it again | |
| 130 | Ctrl+C; a started job is asked to stop and the follow line is said | |

`machine_lock.py run` on a node gives the same codes: 2, 6 and 10 only with `--node NAME` (auto
runs the job on main instead), and 9, 13, 14 and 130 as above. `node.py gate` gives 2 and 10
only with `--node NAME` too (auto gates on main instead).
