# commit.py and machine_paths.py

Two tools, both ported from the PM kit (PM 9813c73). `tools/commit.py` is the entry over `kitlib/commit`. `tools/machine_paths.py` is the entry over `kitlib/machinepaths`. Each library's module docstring holds the full rules, and this page holds the contract.

## commit.py

`commit.py [-C DIR] -m SUBJECT [-b BODY | --body-file FILE] [--model NAME] [--host NAME] [--owner-authored] [--owner-approved VALUE ...] [--lander-main-checkout] PATH ...`

`--host NAME` names the agent host for the Co-Authored-By trailer: a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude). `--provider` is its old name, accepted for one release. A name `hosts.json` lacks is a usage error (exit 2) before any work. The trailer comes from `kitlib.attribution`: Claude's is `Claude <model>` as below; Codex's needs the actual explicit model id, emits `Codex <model-id> <noreply@openai.com>` and never uses CLAUDE_AGENT_MODEL. Explicit paths, owner identity/approval, staged-file and document checks keep their rules. The shared trailer classifier recognizes both forms.

It stages exactly the PATHs named and commits them in the work tree that DIR is in (default: here). `-C` is resolved by `kitlib.git.repo.toplevel`.
- A path is relative to that work tree's root, or absolute inside it.
- `./` and `..` are folded before the owner-only check.
- A relative `--body-file` is relative to the caller's directory.
- No flag may be abbreviated.

What it refuses (`kitlib/commit/__init__.py`, `_commit`):
- **Explicit paths only.** A directory, `.`, `-A` or a glob is a usage error. A path outside the work tree is a usage error too.
- **Foreign staged files.** Anything already staged and not named is refused, because it may belong to another session.
  - With `"commit".merge_fold` on, during a merge with one MERGE_HEAD, the files the merge staged itself are exempt (`merge_owned`).
  - An owner-only file is exempt only when its staged state is exactly the merged branch's copy: the same mode and blob as `MERGE_HEAD`'s, or a deletion of a path `MERGE_HEAD` deleted (`carries_merged_copy`). That branch must have changed it from the merge base, so the merge is carrying the owner's change in, not undoing this branch's. Every commit the merge brings in (reachable from `MERGE_HEAD`, not from `HEAD`) that changes it must be authored by the owner, judged as `gate land` judges a branch (`non_owner_paths`, through `kitlib.guard.ownercommits.violations` against `HEAD`); when git cannot tell, it stays refused. A conflict resolved to that copy counts too; any other staged state, an unresolved conflict, or an octopus merge stays refused.
  - Naming such a file is still refused as owner-only. The refusal says to leave it out of the list only for a path the merge would carry unnamed.
  - A file another writer stages after that check is not committed: the commit is built from a private index holding only the named, rendered and merge-carried paths (`commit_only`). Whatever is still staged after the commit is named on a `WARNING: staged by another writer during this call` line.
- **HEAD moved.** A HEAD another writer moves before `git commit` refuses the commit (`commit_only`). HEAD is read back once after `git commit`.
  - A HEAD read back whose first parent is the HEAD checked, holding the tree and message this call built, is this call's commit. When a hook changed it (a commit-msg hook adding a trailer, a pre-commit hook staging a file into the private index), it is still this call's unless the tagged reflog entry below names another commit.
  - A hook's `git add` reaches only the private index. For each path a hook changed, the real index is set to the commit's copy when it still holds the copy from before the hook (`align_index`, an `index` line), so neither the next commit nor a `WARNING` takes it back. If another writer staged that path during the call, the commit stands, their copy stays staged, and the call is refused (exit 1) with the `git restore --staged --source=<commit>` command that takes the commit's copy.
  - Otherwise `git commit` ran with `GIT_REFLOG_ACTION` set to a tag of this call's own (`reflog_tag`), and the one HEAD reflog entry carrying it names this call's commit and the HEAD git made it on (`tagged_entry`).
  - If the other writer committed after the check but before git read HEAD, this call's commit lands on top of theirs and lacks their change. While it is still HEAD it is undone (`update-ref` back to its parent) and refused, and the other writer's commit stays as it is. MERGE_HEAD, MERGE_MSG and MERGE_MODE come back only when the undone commit was a merge; CHERRY_PICK_HEAD, REVERT_HEAD and SQUASH_MSG are not restored.
  - If a third commit already sits on top of it, nothing is undone: the refusal (exit 1, `NOT undone`) says this call's commit was made on the other writer's, names both, and says to restore the changes it may undo by hand.
  - A commit made on the HEAD checked, with another writer's commit on top of it, stands: exit 0, the `committed` line names this call's commit, and a `NOTE:` line names theirs. If HEAD no longer contains it (another writer moved HEAD elsewhere), the call is refused, `NOT undone`.
  - With no tagged entry (no reflog, or the fake git, which writes none), a commit at HEAD whose first parent is the HEAD checked is this call's, as above; any other counts as this call's only when its tree and message are this call's, and anything else is refused, `NOT undone`, with a `git log -3` hint.
  - A HEAD git cannot read is refused too, except on an unborn branch.
- **Machine paths.** A machine path in the subject or body is refused (`kitlib.machinepaths.find`), unless the commit is `--owner-authored`.
  - A machine path in the staged copy of any file being committed is refused too, under the repository's own `"machine_paths"` config. There is no owner exemption here. This check fails closed.
  - Either refusal unstages what this call staged.
- **Owner-only paths** (`"owner"`, `kitlib/config/owner.py`) are committed only by the owner, with `--owner-authored`.
  - `--owner-authored` requires the author identity git will record (`git var GIT_AUTHOR_IDENT`) to be `"owner".email`. It must not be in `"owner".agent_emails`.
  - `--owner-approved` is required only when an owner-only path is in the commit.
  - Every `--owner-approved` value must be a blanket entry or a committed path. It is written in its canonical spelling under `"owner".trailer`.
- **The main checkout** (`kitlib.git.checkout.main_refusal`), unless `--lander-main-checkout` is given. That flag is a usage error anywhere else.

The message is the subject, then the body, then the trailers:
- The trailers are the owner-approval lines, then `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.
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
| 1 | refused (`refused: ...`), or git failed (`git add failed`, `commit failed`, `nothing to commit in`) |
| 2 | `USAGE: ...`, the main-checkout refusal, or a refused `.kit.json` |

## machine_paths.py

`machine_paths.py [-C DIR]` searches every tracked file, and every untracked file git does not ignore, in the work tree. It looks for a drive-rooted machine folder, or a drive-rooted user profile. The machine folders are the machine config's `machine_path_folders` (docs/kit-json.md "Machine config keys"); the kit codes no folder name. Unset, only drive-rooted user-profile paths are searched, and the tree sweep prints a `NOTE:` line naming the key (`kitlib.machinepaths.UNSET_NOTE`). The tree sweep, the staged-file check of commit.py (`kitlib.machinepaths.scan_staged`), the message check of commit.py (`machine_path_error`) and the free-text check of backlog.py (`kitlib.backlog.plan.refuse_machine_paths`) all read it. A machine config that cannot be read does not stop any of them: user-profile paths are still searched, and each prints a `WARNING: machine config ...` line that names no value from the file: commit.py prints it once (the message check, or the staged-file check of an --owner-authored commit), and backlog.py adds it to a refusal or to the lines of the write (`kitlib.backlog.store.add_plan_note`). The `kitlib/machinepaths` docstring gives the pattern and the allow-list rules.

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

## Not ported

Each item below was dropped on purpose (port brief).
- **Backlog claim folding.** The kit backlog has no claim queue.
- **The `-C` hand-off** (chdir to main, `KIT_CALLER_CWD`, a glued `-Cdir`, `-C` given twice), `KIT_REPO_ROOT`, `KIT_CONFIG` and `_repo_guard`. Here `-C` is a plain option, and argparse takes the last value given.
- **machine_paths built-in allow entries.** These were the PM kit's `_paths.py` fallbacks, its `test_paths.py` table, the kit stubs and backlog.py's ledger. They named the older kit's files and one drive. A repository that relied on them needs explicit `allow` or `fixtures` entries.
- **`machine_paths.piece_fixtures`.** kitlib has no pieces-table resolver, so a non-empty list is refused rather than ignored. Use `fixtures` with the file's glob.
- **The doc_budget PowerShell twin.** Only `tools/doc_budget.py` is run.
