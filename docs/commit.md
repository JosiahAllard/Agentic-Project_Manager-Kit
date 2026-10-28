# commit.py and machine_paths.py

Two tools. `tools/commit.py` is the entry over `kitlib/commit`. `tools/machine_paths.py` is the entry over `kitlib/machinepaths`. Each library's module docstring holds the full rules, and this page holds the contract.

## commit.py

`commit.py [-C DIR] -m SUBJECT [-b BODY | --body-file FILE] [--model NAME] [--host NAME] [--owner-authored] [--owner-approved VALUE ...] [--lander-main-checkout] PATH ...`

`commit.py [-C DIR] -m SUBJECT [-b BODY | --body-file FILE] [--model NAME] [--host NAME] --owner-approval hub:A-<n> PATH ...` and `commit.py [-C DIR] --owner-approval hub:A-<n> --consume COMMIT` commit, and consume, a change the owner approved on the hub ("Owner approval" below).

`commit.py [-C DIR] -m SUBJECT [-b BODY | --body-file FILE] [--model NAME] [--host NAME] [--owner-authored] --allow-empty` commits no paths: HEAD's tree unchanged, only the message and its trailers, for a branch that needs only a `Closes:` or `Part-of:` line. It is refused (exit 1) when anything is staged, a merge is in progress, or the branch is unborn ("no HEAD to keep"); a hook that changes the commit's tree (a pre-commit hook staging a file) has the commit undone and refused (`commit_only`, `undo_commit`); a PATH, `--owner-approval` or `--consume` with it is a usage error (exit 2). Every other check below runs as for any commit, and the host trailer is added as usual (`Request.allow_empty`, `commit_only`). Without the flag, no PATH is still a usage error: no paths never means an empty commit.

`--host NAME` names the agent host for the Co-Authored-By trailer: a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude). `--provider` is its old name, still accepted. A name `hosts.json` lacks is a usage error (exit 2) before any work. The trailer comes from `kitlib.attribution`: Claude's is `Claude <model>` as below; Codex's needs the actual explicit model id, emits `Codex <model-id> <noreply@openai.com>` and never uses CLAUDE_AGENT_MODEL. Explicit paths, owner identity/approval, staged-file and document checks keep their rules. The shared trailer classifier recognizes both forms.

It stages exactly the PATHs named and commits them in the work tree that DIR is in (default: here). `-C` is resolved by `kitlib.git.repo.toplevel`.
- A path is relative to that work tree's root, or absolute inside it.
- `./` and `..` are folded before the owner-only check.
- A relative `--body-file` is relative to the caller's directory.
- No flag may be abbreviated.

What it refuses (`kitlib/commit/__init__.py`, `_commit`):
- **Explicit paths only.** A directory, `.`, `-A` or a glob is a usage error. A path outside the work tree is a usage error too.
- **Foreign staged files.** Anything already staged and not named is refused, because it may belong to another session.
  - With `"commit".merge_fold` on, during a merge with one MERGE_HEAD, the files the merge staged itself are exempt (`merge_owned`).
  - An owner-only file is exempt only when its staged state is exactly the merged branch's copy: the same mode and blob as `MERGE_HEAD`'s, or a deletion of a path `MERGE_HEAD` deleted (`carries_merged_copy`). That branch must have changed it from the merge base, so the merge is carrying the owner's change in, not undoing this branch's. Every commit the merge brings in (reachable from `MERGE_HEAD`, not from `HEAD`) that changes it must be authored by the owner or carry an owner approval the hub verifies, judged as `gate land` judges a branch (`non_owner_paths`, through `kitlib.guard.ownercommits.violations` against `HEAD`); when git cannot tell, it stays refused. A commit reachable from `<remote>/<main>` as last fetched is not judged again (`landed_main`, the ref pre-push trusts): it was judged when it landed, and an approved one's approval stops verifying 24 hours after the owner's decision, or whenever the hub is down. A conflict resolved to that copy counts too; any other staged state, an unresolved conflict, or an octopus merge stays refused.
  - Naming such a file is still refused as owner-only. The refusal says to leave it out of the list only for a path the merge would carry unnamed.
  - A file another writer stages after that check is not committed: the commit is built from a private index holding only the named, rendered and merge-carried paths (`commit_only`). Whatever is still staged after the commit is named on a `WARNING: staged by another writer during this call` line.
- **HEAD moved.** A HEAD another writer moves before `git commit` refuses the commit (`commit_only`). HEAD is read back once after `git commit`.
  - A HEAD read back whose first parent is the HEAD checked, holding the tree and message this call built, is this call's commit. When a hook changed it (a commit-msg hook adding a trailer, a pre-commit hook staging a file into the private index), it is still this call's unless the tagged reflog entry below names another commit.
  - A hook's `git add` reaches only the private index. For each path a hook changed, the real index is set to the commit's copy when it still holds the copy from before the hook (`align_index`, an `index` line), so neither the next commit nor a `WARNING` takes it back. A path removed by the hook is removed from the real index. If the real index holds another copy of that path (someone staged it before or during the call), or the index cannot be written, the commit stands, that copy stays staged, and the call is refused (exit 1) with the `git restore --staged --source=<commit>` command that takes the commit's copy. The index is read (`ls-files`) and written (`update-index`) in two git calls: a path staged between them is overwritten with the commit's copy.
  - Otherwise `git commit` ran with `GIT_REFLOG_ACTION` set to a tag of this call's own (`reflog_tag`), and the one HEAD reflog entry carrying it names this call's commit and the HEAD git made it on (`tagged_entry`).
  - If the other writer committed after the check but before git read HEAD, this call's commit lands on top of theirs and lacks their change. While it is still HEAD it is undone (`update-ref` back to its parent) and refused, and the other writer's commit stays as it is. MERGE_HEAD, MERGE_MSG and MERGE_MODE come back only when the undone commit was a merge; CHERRY_PICK_HEAD, REVERT_HEAD and SQUASH_MSG are not restored.
  - If a third commit already sits on top of it, nothing is undone: the refusal (exit 1, `NOT undone`) says this call's commit was made on the other writer's, names both, and says to restore the changes it may undo by hand.
  - A commit made on the HEAD checked, with another writer's commit on top of it, stands: exit 0, the `committed` line names this call's commit, and a `NOTE:` line names theirs. If HEAD no longer contains it (another writer moved HEAD elsewhere), the call is refused, `NOT undone`.
  - With no tagged entry (no reflog, or the fake git, which writes none), a commit at HEAD whose first parent is the HEAD checked is this call's, as above; any other counts as this call's only when its tree and message are this call's, and anything else is refused, `NOT undone`, with a `git log -3` hint.
  - A HEAD read back with no parent, other than this call's first commit on an unborn branch, is refused, `NOT undone`.
  - A HEAD git cannot read is refused too, except on an unborn branch.
- **Machine paths.** A machine path in the subject or body is refused (`kitlib.machinepaths.find`), unless the commit is `--owner-authored`.
  - A machine path in the staged copy of any file being committed is refused too, under the repository's own `"machine_paths"` config. There is no owner exemption here. This check fails closed.
  - Either refusal unstages what this call staged.
- **Owner-only paths** (the `owner` section; the rule is in `kitlib/config/owner.py`) are committed only by the owner, with `--owner-authored`.
  - `--owner-authored` requires the author identity git will record (`git var GIT_AUTHOR_IDENT`) to be `"owner".email`. It must not be in `"owner".agent_emails`.
  - `--owner-approved` is required only when an owner-only path is in the commit.
  - Every `--owner-approved` value must be a blanket entry or a committed path. It is written in its canonical spelling under `"owner".trailer`.
  - Or an agent commits the exact change the owner approved on the hub, with `--owner-approval hub:A-<n>` ("Owner approval" below).
- **The main checkout** (`kitlib.git.checkout.main_refusal`), unless `--lander-main-checkout` is given. That flag is a usage error anywhere else.

The message is the subject, then the body, then the trailers:
- The trailers are the owner-approval lines (`Owner-Approval: hub:A-<n>` first, with `--owner-approval`), then `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.
- An `--owner-authored` commit carries `Owner-Authored: yes` instead of `Co-Authored-By`.
- The model comes from `--model`, else `CLAUDE_AGENT_MODEL`. A blank value counts as unset.
- `"Claude "` is added unless the name is a `"commit".coauthor_families` family, or starts with one followed by a space, `.` or `-`.
- The session line comes last (`kitlib.commit.session_lines`). Lines of the form `<key>: <value>` that end the body, `<key>` being a host's `attribution.session_trailer` key in `hosts.json` (`Claude-Session`), are taken out of the body and written in the trailer block, where git reads them as trailers. This holds with or without the environment variable below, so a `-b "...~~Claude-Session: <url>"` is a trailer, not a body line. The same line anywhere else in the body stays in the body.
  - With no such line, an agent's commit on Claude gets `Claude-Session: https://claude.ai/code/<id>`, `<id>` being `CLAUDE_CODE_BRIDGE_SESSION_ID`. Nothing is written when that variable is unset, blank, or not a plain id (letters, digits, `_`, `-`).
  - The URL prefix and the variable name are harness facts observed on 2026-10-14, not a documented contract. A host change to either is a `hosts.json` edit.
  - An `--owner-authored` commit gets no session line from the environment. Codex has no session trailer (`"session_trailer": null`).
- The body comes from `-b TEXT`, where a typed `\n` becomes a newline (`~~` also becomes a newline; for a literal `~~` use --body-file), or from `--body-file FILE`, read as UTF-8 with CRLF changed to LF.
  - `-m` takes the subject once; a second `-m` is a usage error (exit 2) pointing at `-b` / `--body-file`, and nothing is committed.
  - A `-b` whose whole value names an existing file, or looks like an absolute path, is a usage error.

**Render on commit** (docs/content.md "Render on commit"; `kitlib.content.oncommit`): in a work tree with the kit's layout, a named path under `content/`, or `kit.json` or `hosts.json`, makes it run that work tree's `tools/render_content.py --write` before staging. Every rendered file the render wrote or removed is staged into the same commit, under the same foreign-file and owner-only checks, and a `rendered  <paths>` line follows `files` (`no rendered file changed` when the render changed nothing). A render that fails is refused (exit 1) with the command to run; what it wrote stays in the work tree. Any other repository, or a commit naming no render source, starts no render.

After a commit that includes a `.md` file or an owner-only file, it prints the doc-budget verdict.
- The kit's `tools/doc_budget.py -C <root>` produces it, and `kitlib.verdict` judges it.
- A `doc index` verdict follows (`kitlib.commit.doc_index_check`: `tools/doc_index.py -C <root>`, judged on `DOC INDEX OK` / `DOC INDEX FAILED`). Either failing prints the follow-up-commit notice. `.kit.json` "doc_index".enabled false prints nothing.
- With no such tool, the verdict is `docs      NOT CHECKED - ...`.
- A failing verdict leaves the commit in place. It only asks for a follow-up commit.

| Code | Meaning |
|---|---|
| 0 | committed (`committed <hash>  <subject>`, `files     ...`, and `rendered  ...` after a render) |
| 1 | refused (`refused: ...`), or git failed (`git add failed`, `commit failed`, `nothing to commit in`); with `--owner-approval`, also a consume that failed after the commit, which stands |
| 2 | `USAGE: ...`, the main-checkout refusal, or a refused `.kit.json` |

## Owner approval

`--owner-approval hub:A-<n>` commits a change to owner-only paths that an agent asked for and the owner approved on the Action Hub (docs/action-hub.md "approvals/A-NNN"), without the owner authoring it. The steps are `kitlib/commit/approval.py`; `kitlib/commit/__init__.py` runs them (`_approval_first`, `_approval_before_write`, `_stage`, `_put_back`, `_consume`).

Usage errors (exit 2, `approval.usage_error`): a value that is not `hub:A-<n>`; `--owner-approval` with `--owner-authored` or `--owner-approved`; `--consume` without `--owner-approval`, or with paths, `-m`, `-b` or `--body-file`.

Refused before anything is written (exit 1, `Nothing was written or committed.`):
- The hub cannot be asked, has no such approval, or it is not `approved`: consumed already, pending, denied or expired (`fetch`, `state_error`).
- A render source is named (the commit would carry rendered files nobody approved), or a merge is in progress (the commit would have two parents).
- An approved path is staged already.
- `prewrite_error`: the paths named are not exactly the approval's files; HEAD is not on the same content as its `head` (below; the refusal names the way round it, a worktree at that base: `new_worktree.py --approval hub:A-<n>`, docs/new-worktree.md "A base commit", then a merge of that branch); a path is not a plain file of the work tree (`..`, absolute, a backslash, a colon, a `.git` part, a symlink, or a parent outside the tree); a file in the work tree is not its `base_blob`; a content is not its `blob`; or a file would not change.
- `preverify_error`: the hub's verify route does not verify the approval for this repository (its `origin` remote, `kitlib.hubsite.approvals.remote_id`), its base and the approved blobs (`Verifier.verify_on`).

The same content (`kitlib.hubsite.approvals.same_content_error`): HEAD is the approval's `head`, or a descendant of it (`git merge-base --is-ancestor`) where every approved path has the same tree entry (mode, type and object id, or none) as at the `head`. The main branch may move on with other work after the owner approves: the change written on HEAD is still, path for path, the change the owner saw. An approved path edited, created, removed, renamed or given another mode since the `head` refuses, and the refusal names the first. Only the approved paths are compared (the owner's choice), not every owner-only path. The hub's verify route still checks the signed `head` exactly: the client proves the same content with git and asks the hub for the `head`.

A staged file the call does not name refuses first, as for any commit.

The work tree stays at the approval's base: never copy the approved content in first, or the call refuses (`prewrite_error`: the file is not its `base_blob`). `commit.py` writes the approved content from the hub.

Then the approved content is written (UTF-8; a null content deletes the file), the paths are staged, and each staged entry is checked against the approval: a git filter or `.gitattributes` that changed one on staging, or a staged mode other than a regular file's (100644 or 100755), refuses (`staged_error`). The other checks above (HEAD moved, machine paths) hold as for any commit; the owner-only refusal and `--owner-approved` do not apply. A refusal after the write and before the commit step puts every file back and unstages the paths (`The work tree is as it was, and nothing this call staged is staged.`). A refusal from the commit step itself (HEAD moved during the call; a commit made may stand or be undone, as for any commit) leaves the approved content in the work tree and the approval not consumed (`hub:A-<n> was not consumed; the approved content is left in the work tree.`).

The message carries `Owner-Approval: hub:A-<n>` first in its trailer block, then `Co-Authored-By`: it is an agent's commit. After the commit, `consume_commit`:
- The commit must carry that one `Owner-Approval` value, have one parent, and hold each changed path present as a regular file (`kitlib.guard.ownercommits.irregular`).
- Its changed paths and their blobs go to the verify route, then to the consume route with the version the snapshot gave, then to the verify route again with the commit. An approval this commit consumed already is only verified again.
- It prints `approval  hub:A-<n> consumed by <commit>`.
- On any failure the commit stands and the call exits 1 with `refused: hub:A-<n> was not consumed: <why>.`, or, when the consume route consumed it and the verify after it fails, `refused: hub:A-<n> was consumed by <commit>, and the hub does not verify it for that commit: <why>.` (`approval.consume_refusal`), then the command that retries (for a consumed approval, it only asks the hub to verify again): `python <kit>/tools/commit.py --owner-approval hub:A-<n> --consume <commit>`, run in the same work tree. That form (`_consume`) runs only these steps, for a COMMIT that `git rev-parse` resolves in the work tree.

Landing it: `gate land` and the pre-push hook (`kitlib.guard.ownercommits.violations`) pass a non-owner commit that changes an owner-only path only when it carries exactly one `Owner-Approval: hub:A-<n>`, has one parent (the approval's `head`, or a descendant of it with the same content, as above), changes exactly the approval's files to their approved blobs as regular files (mode 100644 or 100755: a symlink or gitlink entry refuses, `ownercommits.irregular`), and the hub verifies the approval as consumed by that commit. A merge, a root commit, or any doubt of the hub keeps it refused. An approval verifies until 24 hours after the owner's decision, consumed or not (`APPROVED_TTL_S`, `kitlib/hubsite/approvals.py`), so the branch must land within that time.

Known limit: the mode a commit writes is not bound beyond a regular file's. The signature binds each path's blob, and the same content binds the mode on the base side, but an approved commit that writes a path as 100755 where the owner saw 100644 (or the reverse) still passes `staged_error` and the landing check.

Elsewhere the trailer is shown and claims nothing. The review pack's co-author section shows `| owner approval hub:A-<n>` on the commit's row (`kitlib.reviewpack.coauthor_section`). Neither it nor `tools/trailers.py` reads the trailer as an owner claim (`kitlib.reviewpack.trailers`), and `"owner".trailer` may not be `Owner-Approval` in any case (`kitlib.config.owner.trailer_problem`).

## machine_paths.py

`machine_paths.py [-C DIR]` searches every file git tracks in the work tree (`kitlib.machinepaths.tracked_files`), never an untracked one. It looks for a drive-rooted machine folder, or a drive-rooted user profile, or a posix home (`/home/<name>`; a placeholder, `/home` alone and a path that merely contains `home` do not match). The machine folders are the machine config's `machine_path_folders` (docs/kit-json.md "Machine config keys"); the kit codes no folder name. Unset, only drive-rooted user-profile paths are searched, and the tree sweep prints a `NOTE:` line naming the key (`kitlib.machinepaths.UNSET_NOTE`). The tree sweep, the staged-file check of commit.py (`kitlib.machinepaths.scan_staged`), the message check of commit.py (`machine_path_error`) and the free-text check of backlog.py (`kitlib.backlog.plan.refuse_machine_paths`) all read it. A machine config that cannot be read does not stop any of them: user-profile paths are still searched, and each prints a `WARNING: machine config ...` line that names no value from the file: commit.py prints it once (the message check, or the staged-file check of an --owner-authored commit), and backlog.py adds it to a refusal or to the lines of the write (`kitlib.backlog.store.add_plan_note`). The `kitlib/machinepaths` docstring gives the pattern and the allow-list rules.

| Code | Meaning |
|---|---|
| 0 | `MACHINE PATHS OK (...)`; WARNING lines (a skipped file, a stale allow entry) do not fail |
| 1 | `MACHINE PATHS FAILED: ...`, a hit off the allow-list |
| 2 | `MACHINE PATHS NOT CHECKED: ...`: git could not list the tree, nothing was read, the config was refused, or `-C` is not in a work tree; or `USAGE:` |

## `.kit.json` keys read

docs/kit-json.md lists every key with its default.
- `commit.merge_fold` and `commit.coauthor_families` are read by commit.py.
- `owner.*` (paths, paths_except, email, agent_emails, trailer) is read by commit.py. It comes from the working tree's `.kit.json`.
- `checkout_guard.rule_pointer` is the refusal's pointer to where the rule is written down.
- `repo.main_branch` is named in the `--lander-main-checkout` usage error.
- `machine_paths.*` (allow, fixtures, known_binary, scan, fallback_vars) is read by both tools. commit.py reads it through `scan_staged`.
