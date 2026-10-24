# Stray restore

`tools/stray_restore.py` discards a stray in a repository's main checkout only when the tip of a branch already holds the same bytes at the same path. Anything it cannot prove is LEAVE, and it never touches a LEAVE path. It has no override flag. A stray is a change `git status` shows in the main checkout (`kitlib/git/stray.py`, `docs/preflight.md`); the gate step `main-checkout-stray` reports strays, and landing refuses to run while there are any.

```
python <kit>/tools/stray_restore.py -C <checkout>            # the dry run: writes nothing
python <kit>/tools/stray_restore.py -C <checkout> --apply    # discard what is proven
```

`<checkout>` is the main checkout or any linked worktree of it. The tool always acts on the main checkout. The logic is in `kitlib/strayrestore`, and the tests are in `tests/strayrestore`.

## The proof

- The tool reads each stray's work file and hashes it in Python as a git blob (sha1, or sha256 for a sha256 repository). No git filter touches the bytes it hashes. Under `--apply` it reads the file again after moving it aside, and the verdict comes from that second read ("How `--apply` discards a file").
- The refs it compares against are the tips of `refs/heads/*` and `refs/remotes/*/*`, listed with one `git for-each-ref` and no fetch. Symbolic refs (`origin/HEAD`) are skipped.
  - Never counted: tags, `refs/stash`, any other ref namespace, a detached worktree HEAD, and history behind a tip.
- Same path only: the blob must be at the stray's own path in the tip's tree, as a plain file (mode 100644 or 100755). The same bytes at another path do not count.
- Only the raw form counts: the file's blob id must be the tip's blob id. A file that equals what a checkout would write for the tip's blob, such as a CRLF copy of an LF blob, is LEAVE. Its bytes are in no git object, and getting them back would depend on the attributes and `core.autocrlf` at recovery time.
- The match is recorded as the ref, its tip commit and the blob id.

## What each stray gets

| git status | Verdict | `--apply` does |
|---|---|---|
| `??` untracked, matched | DELETE | removes the file |
| ` M` changed in the work tree only, matched | RESTORE | restores HEAD's bytes |
| ` D` deleted in the work tree only | UNDELETE | writes HEAD's file back; no match is needed, since nothing is lost |
| no tip holds the bytes at that path | LEAVE | nothing |
| staged, typechange, intent-to-add, renamed | LEAVE | nothing: the index holds a version the tool does not prove |
| a `filter` attribute set (LFS or another driver) | LEAVE | nothing: a driver runs other code |
| a symlink, junction or reparse point on the path, a path that resolves outside the checkout, a directory or gitlink, a file that cannot be read | LEAVE | nothing |

A RESTORE always goes to HEAD's blob, never to the matching branch blob. The match proves the discarded bytes are kept at `<ref>:<path>`, and HEAD's own bytes are kept in HEAD.

The whole run is refused (exit 2, nothing touched) when the main checkout has an unmerged path, or when MERGE_HEAD, CHERRY_PICK_HEAD, REVERT_HEAD, `rebase-merge/`, `rebase-apply/` or BISECT_LOG is present.

## How `--apply` discards a file

1. The file is moved (`os.replace`) into `<git common dir>/kit-stray-restore/q-<time>-<pid>/<path>`.
2. The moved copy is read and matched again. A writer that changed the file after the first check cannot get new bytes past this second check.
3. If the copy still matches, a RESTORE runs `git restore --source=<head> --worktree` on the path, where `<head>` is the HEAD commit read at the start of the run, the one the log names. A DELETE does nothing more. The log line is written, then the quarantine copy is deleted.
4. If the copy no longer matches, it is moved back. If something has been written at the path since, the copy stays in quarantine and the LEAVE line names where it is. The same happens when a file appears at the path before `git restore` runs.

If a step fails (a `git restore` error, say), that path is put back and the run stops. Every path not yet reached is LEAVE ("not reached"), and the tool prints `ERROR:`, then the `left:` lines, and exits 1. A failure to write the log line stops the run the same way, after the path was acted on: the file is already deleted or restored, and its line reads `deleted` or `restored`. The moved copy stays in quarantine, and the `ERROR:` line names where it is.

Afterwards the tool lists what `git status` still shows (`left:` lines).

A run killed part way (a crash, a closed terminal) can leave a stray in `<git common dir>/kit-stray-restore/q-*/<path>` with no log line. Look there after a killed run, and move each file back to its path in the main checkout (or delete it, when `git status` shows the path is already restored).

## The log

`<git common dir>/kit-stray-restore.jsonl` gets one JSON line per discarded path, and only under `--apply`. Its fields:

- `ts`, `top`, `path`, `status` (the porcelain XY) and `action` (deleted, restored or undeleted).
- `match_ref`, `match_commit` and `bytes_blob`: the ref, its tip and the blob of the discarded bytes. All three are null for `undeleted`.
- `head` and `head_blob`: the HEAD commit read at the start of the run, which a RESTORE or UNDELETE writes from, and its blob at the path.
- `mark`: the session mark. `session`: `PM_KIT_SESSION_NAME`, or `-`.

To recover a discarded file, run `git cat-file blob <bytes_blob> > <path>` in Git Bash or cmd. It writes the exact bytes the tool discarded. Do not run it in Windows PowerShell 5.1: its `>` re-encodes the output.

## Exit codes

- 0: nothing is LEAVE. After `--apply`, nothing is left in `git status`.
- 1: something is LEAVE or left, or an error stopped `--apply` part way. It is also 1 when the final `git status` fails after the plan found strays: the tool prints `ERROR:` and no `left:` lines, since git could not list them. When an earlier error stopped the run as well, the `ERROR:` line carries both.
- 2: a usage error, a refusal (the session mark, an operation in progress) or a git failure before anything was touched. It is also 2 when the plan found no strays and the final `git status` then fails.

## Who runs it

The owner (an unmarked session), the PM, a coordinator or the lander. The tool reads the session mark (`kitlib/sessionmark.py`, `docs/guard.md` "Session mark") and refuses any other mark.

A subagent shares its parent's mark, so a worker that a coordinator dispatches passes this check. Only the guard could tell such a worker apart. That is accepted on purpose: a restore loses nothing by design, because every discarded byte is kept in a branch tip.

The mark itself can be dropped. As `kitlib/sessionmark.py` says, `PM_KIT_ROLE= python ...` runs a command unmarked, and an unmarked run counts as the owner, so any agent can pass `allowed()`. The check guards against mistakes, not against evasion. The tool's safety rests on the no-loss rule: it discards only bytes a branch tip holds, whoever runs it.

The guard judges a `python <kit>/tools/stray_restore.py ...` run by the script's own file, and it never sees the tool's child git. So the run needs no guard rule.

## Known limits

- The proof holds while the matching branch exists. If that branch is deleted later, `git gc` can drop the blob, and `bytes_blob` in the log then names an object that is gone. No ref is written to keep it alive. Recover what you need before you delete the branch.
- Only branch tips count. A stray equal to an older commit on a branch is LEAVE.
- There is a small window between the last existence check and `git restore`, in which a writer could create the file and have it overwritten. The quarantine step closes the window for the stray itself, but not for a file created in that moment.
