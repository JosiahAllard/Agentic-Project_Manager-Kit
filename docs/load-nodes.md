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
  not (every node is still listed).

## Running a job

```
node.py run --kind K --repo R --slug S [--node auto|NAME] [--artifact GLOB]...
            [--max-wait D | --no-wait] [--expect D] [--note TEXT] [--reattach S] [-C DIR] -- <command>
```

The job is the checkout's HEAD:

1. Main pushes HEAD to `refs/node/<job>` on the node's mirror of the repository, through the
   pinned ssh. No remote is added to the checkout.
2. The node checks the ref, makes a worktree at that commit, and starts the command under its own
   machine lock, detached from the ssh call.
3. The output streams back as it comes.
4. The files matching each `--artifact` glob (relative to the checkout's top) are copied back to
   the same paths in the checkout.
5. The node removes the job.

With `--node NAME`, the checkout must have no uncommitted tracked changes (the node runs HEAD) and
the node must list the repository in `repos`.

Artifacts land only when every one came whole. Each is checked for size and sha256 and fetched
once more on a failure. None may be a tracked file, and none may land outside the checkout. Main
copies all of them or none. When they do not all come, the staging folder is kept and named, and
the job stays on the node until a later start sweeps it (7 days after it ended).

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

A node that fails before its job starts, or is busy, gives way to main.

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

## Exit codes

| Code | `run` and `follow` | `list`, `status` and `stop` |
|---|---|---|
| 0 and up | the command's own exit code | 0 every node answered; for stop, asked |
| 1 | | a node did not answer; for stop, the node refused |
| 2 | usage: an unknown node or kind, a repository not opted in, uncommitted changes for `--node NAME`, a bad glob | usage, or a registry that is missing, invalid or not ignored by git |
| 6 | the node was busy (with `--node NAME`) | |
| 9 | LOST: the connection did not come back; the job runs on | |
| 10 | the node could not be used: unreachable, host key, push, start refused, a kit too old for the run verbs, or its runner vanished | |
| 13 | the job succeeded but its artifacts did not all come | |
