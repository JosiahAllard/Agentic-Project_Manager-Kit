# PM data on a private branch of the kit repo

Status: built; the owner's PM data repository is on `pmdata`, not yet confirmed working end to end (docs/go-live.md).

One git repository holds the public kit and one user's private PM data (rulings ledger, backlog, refusals log, handoffs, state). The data lives on an orphan branch, `pmdata`, that shares no history with `main`, is never merged, and is pushed only to a second, private remote. Other users who clone the kit get `main` only.

## Layout

| What | Where |
|---|---|
| Kit | the repository's main checkout, branch `main`, remote `origin` (public) |
| PM data | a linked worktree of the same repository, branch `pmdata`, at a path the user picks (often the old PM folder) |
| Private remote | any other remote name; default `private`; only `pmdata` is ever pushed there |
| Record of the split | `<git common dir>/kit-pm-data.json`: `{"branch": "pmdata", "remote": "private", "root": "<sha of the data branch's root commit>"}` |
| Machine config | The machine config, key `pm_data`: `{"path": "<abs>", "branch": "pmdata", "remote": "private"}` (`Owned('kitlib.pmdata')`) |
| Hub store | `hub/<collection>/<doc_id>.json` in the data worktree: one folder per hub collection, one JSON file per document; written by the hub server (`tools/hub.py serve`) and committed and pushed to the data branch by its own git sync (`kitlib.hubsite.gitsync`; `hub.py status` prints it as the `git:` line). The data worktree's `.kit.json` needs the machine-path allow entry `{"glob": "hub/*", "line": ".", "why": "hub documents are the owner's own text"}` under `machine_paths.allow`, for a store folder `hub`; it is an allow-list, not a scrub. The old hub's data folder (the same `hub/` folder, or an ArtifactData export) is copied in once with `hub.py migrate` (docs/action-hub.md "Data from the old hub") |

The root commit is recorded when the branch is made, so the guard still knows it if the branch is renamed or deleted. The branch is called `pmdata`, not `pm`, so `pm/<slug>` work branches keep working (git refuses `refs/heads/pm/x` while `refs/heads/pm` exists; `pmdata/<slug>` would collide the same way, so no work branch uses that prefix).

The data worktree's `.kit.json` (on `pmdata`) sets `repo.main_branch: "pmdata"` and `repo.remote` to the private remote, so every tool that reads "the main branch" and "the remote" there means the data branch and the private remote. Its `backlog.store` must be a folder, `main` or `branch`, never `ref`: the repository's one `refs/backlog/main` is not the data branch's, and the data branch must not share it. A checkout of the same repository whose `.kit.json` names no store reads that ref and is refused a write there, pointed at this worktree's store (docs/backlog.md "Stores"). `pm_data.py init` refuses a `.kit.json` that says `ref`.

## Making the branch (`tools/pm_data.py init`)

1. Refuse unless run in the main checkout, on a branch, with no `pmdata` branch yet (or `--adopt` for an existing one) and a private remote name that is not the public remote and whose URL is not the public remote's (compared after trimming `.git` and a trailing slash).
2. `git commit-tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 -m <text>` makes the root on the empty tree; `git branch pmdata <sha>` pins it.
3. `git worktree add <path> pmdata`, then `git worktree lock --reason "PM data"` so `git worktree prune` and the sweep (`sweep_skip` skips a locked worktree) never take it.
4. Branch config, set with explicit `git config` calls: `branch.pmdata.remote`, `branch.pmdata.pushRemote` and `branch.pmdata.merge=refs/heads/pmdata`. Set `push.default=simple` in the repository config if unset. Set nothing under `remote.<public>.*` (no `push` line: it would change bare pushes of other branches).
5. If a private URL was given: `git remote add <name> <url>` with git's default fetch refspec, then `git push <name> refs/heads/pmdata:refs/heads/pmdata`.
6. Write `kit-pm-data.json` and the machine config key; print the one line the user adds to `.kit.json` of the data worktree if it has none.

Built as `tools/pm_data.py init --path <dir> [--branch] [--remote] [--url] [--adopt]` over `kitlib/pmdata/init.py`, whose docstring is the detail. Every check runs before the first write, so a refusal (exit 1) changes nothing. The private url is compared with every other remote's in every form it reaches. `--adopt` takes a branch with one root that no main-branch history holds, and refuses a `.kit.json` on the branch that does not name the data branch, the private remote and a non-`ref` store. Exit 3 means all was made locally and the push failed (the lines say how to recover); exit 4 means it stopped part-way and `--adopt` finishes it; exit 5 means an error escaped `kitlib.pmdata.init` (`init --adopt` checks what was made); 130 is Ctrl+C.

A bare `git push` in the data worktree goes to the upstream, the private remote; with no upstream it fails ("no upstream") rather than guessing. The pre-push check below is the enforcement; the config is only the default path.

## Never configure a refspec

`RefBackend.ensure_refspec` (`kitlib/backlog/store.py`) adds `+refs/backlog/main:refs/backlog/main` to `remote.<remote>.fetch` before it fetches; when the remote has no store, every later plain `git fetch` in that repo fails. Nothing here may do the same. Every fetch and push in this design names its refspec on the command line (`git fetch origin refs/heads/main:refs/remotes/origin/main`, `git push private refs/heads/pmdata:refs/heads/pmdata`) and never edits `remote.*.fetch` or `remote.*.push`.

## Pre-push guard

`kitlib/guard/prepush.py` `check` runs a first stage, `leak_refusal`, when `kit-pm-data.json` exists, before the main-branch rules, for every ref line and whatever the remote (docs/guard.md "Pre-push" has the details):

- **Leak by name.** The remote is not the private remote and the local or remote ref is `refs/heads/<data branch>`: refuse.
- **Leak by history.** The remote is not the private remote and the pushed local sha has the recorded `root` as an ancestor (`refs.is_ancestor`): refuse. This covers the data branch merged onto main, any other branch and any tag. A cherry-picked data commit is a new commit without the root in its history, so a third check catches it by its objects: a push that would send any object reachable from the root or the data branch (the empty tree and blob excepted) is refused, which also refuses a file whose content is identical to a data file. Its limit: a copy edited by hand (any byte changed) is not caught. A deleting push (all-zero local sha) is not judged.
- **Private remote gets only the data.** The remote is the private remote and the remote ref is not `refs/heads/<data branch>` (so no tag and no other branch goes there), or the local sha lacks the root as ancestor, or the push deletes any ref there: refuse. (A private remote that also held the kit would let `main` and the data branch mix there.)
- **Fail closed.** `kit-pm-data.json` unreadable or malformed, or the root object missing from this clone, refuses every push with the file named.

The private remote is judged by the URL git hands the hook (its second argument): it must be one of the recorded remote's push URLs as git computes them, read without any `git -c` configuration, and no other remote's (docs/guard.md "Pre-push", "Which remote is private"). The existing main-branch rules then run as before. `--no-verify` and `git send-pack` skip the hook; the guard rule `git-push-pmdata` denies both when `kit-pm-data.json` exists, in every session but the owner's own (docs/guard.md "Rules"). The same rule denies, there, the other routes past the hook: `git http-push` and a remote helper run by hand (`git remote-https`, `git-remote-https`), a git alias from config that runs any of these (expanded and judged; one git may read from another HOME or a `-c` setting is denied unread), an unreadable line that runs a program the text does not name, and PowerShell's `& (<subexpression>)`. Because this stage trusts the configured URLs, the guard rule `git-remote-rewrite` denies repointing a remote there (`git remote set-url|add|rename|remove`, `git config` writes under `remote.`, `url.`, `include.`, `includeIf.`, `git config --edit`), and `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM` or `GIT_CONFIG` before a push is denied as a push from a place the guard cannot tell (`git-push-main`).

`gate.py land` also refuses a branch whose history contains the recorded root: `ancestry_refusal` (`kitlib/backlog/at_landing.py`) checks it first (`pmdata_refusal`), before the backlog store's root, so a contaminated branch is refused before the merge instead of after it. The PM's data is never landed with gate.py land; data commits go through the kit tools' own commit and push, so the landing check has no exemption for the data branch.

## Guard place and commit hooks

- `kitlib.git.repo.classify` calls the data worktree LINKED, so the guard would let a subagent edit PM data and would read the kit's `.kit.json` for it. Add the configured `pm_data.path` as a MAIN-like place: the main-checkout rules (subagent edits, `checkout`, `reset --hard`, `clean`, `stash`, bulk add) apply to it, and its config is its own `.kit.json` (an exception to "a worktree's own `.kit.json` is never read": only this one path, which agents cannot edit).
- `core.hooksPath` is one setting for the whole repository, so the kit's `githooks/` runs for the data worktree too. A repository-local `hooks/pre-commit` there (for example the ledger check) would never run. The existing opt-in `githooks/pre-commit` gains the ledger-staged condition: it runs `tools/ledger.py check --base HEAD` only when the commit changes the configured ledger file (or the standing digest beside it), and nothing else (docs/ledger.md "Pre-commit check").

## Session start: keep the kit current

A check in `kitlib/pmstart` `checklist`, `kit_sync`, runs when `pm_data` is set. Like the other checks it returns lines and never raises (a failure becomes "could not check kit_sync: reason").

1. The kit checkout is `kitlib.git.repo.main_checkout_toplevel(<pm_data.path>)`. Not on `main`, a merge in progress (`refs.merge_in_progress`), not clean (`refs.is_clean`) or the landing lock held (`kitlib.records.landing`): print one line saying which, and stop.
2. `git fetch origin refs/heads/main:refs/remotes/origin/main` (the public remote, explicit refspec, `kitlib.git.cmd` network timeout of 3 seconds; the hook's other checks are unaffected). Offline or timed out: print "kit not updated: <why>", stop.
3. Already contains `origin/main`: print nothing. Local main is a fast-forward behind: `git merge --ff-only refs/remotes/origin/main`, print "kit updated to <short sha>". Diverged: print "kit main has diverged from origin/main; not merged".

It never rebases, resets or forces, and never touches the data worktree. The hook runs outside the guard (it is the hook's own process), so the main-checkout rule on `merge` does not apply to it.

## Other users

A clone of the public remote has `main` only. `installer/new-repo.ps1` gains `-PmData <path>` (and `-PrivateRemote <url>`): it runs `pm_data.py init`, which makes an empty `pmdata` (one root commit, no files), the worktree at `<path>`, and the private remote if a URL is given. Without the switch nothing changes for a user who keeps data outside git. `kit_stubs.py` needs no change.

## Failure modes

| Case | Behaviour |
|---|---|
| Kit checkout dirty, off main, mid-merge or landing | session start warns, leaves it |
| Local main diverged from `origin/main` | warns; the lander resolves it |
| Offline at session start | warns; every other check still prints |
| Private remote missing or unreachable | `init` keeps the local branch and says the push was not done; ledger `--push` raises its usual "not pushed" refusal |
| Private remote URL equals the public URL | `init` refuses |
| `pmdata` branch deleted or renamed | the recorded root still guards; `pm_data.py init --adopt` re-links a branch |
| Data worktree folder deleted | `git worktree prune` is blocked by the lock; `pm_data.py init --adopt` re-adds it. Until then the guard cannot place `pm_data.path` to read the record, so on a machine whose config names `pm_data` it denies every push and send-pack from a repository without a record, in every session but the owner's own (rule `git-push-pmdata`, `rules.data_copy`, fail closed); the refusal names `pm_data.py init --adopt` |
| Pre-push runs with `--no-verify`, or `git send-pack` | not caught by the hook; the guard rule `git-push-pmdata` denies both in every session but the owner's own |
| `git http-push`, a remote helper by hand, or a git alias for a hook-skipping push | the guard rule `git-push-pmdata` denies it in every session but the owner's own |
| A remote repointed (`git remote set-url`, `git config remote.*`/`url.*`/`include.*`, `--edit`) | the guard rule `git-remote-rewrite` denies it in every session but the owner's own; an edit of the config file itself is not seen (docs/guard.md "Known limits") |
| Data copied into a repository without a record (`git clone` of this repository, a fetch of `pmdata`, a bundle) and pushed from there | that repository has no record and no kit hook (`core.hooksPath` is set per repository), so the leak stage never runs. When the machine config names `pm_data`, the guard rule `git-push-pmdata` denies every push and send-pack, in every session but the owner's own, from a repository without a record that holds the recorded root commit or is shallow (`rules.data_copy`), a git alias for one included (`rules.git_alias_config` expands aliases in such a repository too). Not caught: a machine config that names no `pm_data` (no root to look for); data files copied as plain files and committed afresh (new commits without the root, as for a copy edited by hand above) |
| A contaminated branch reaches `main` | `ancestry_refusal` before the merge, then the pre-push root check |
| `git gc` / shallow clones of the data worktree's repo | out of scope; the root commit must stay reachable from `pmdata` |

## Decided by the owner

- **Pre-commit dispatch.** The kit's `githooks/pre-commit` runs only the kit's own checks; it does not chain to a repository-local hook (`core.hooksPath` is repository-wide, so such a hook never runs). The hook has one dispatch point where a chain step could slot in: `kitlib/githooks/precommit.py` `CHECKS` (docs/ledger.md "Pre-commit check").
