# PM data on a private branch of the kit repo

Status: tasks 1-8 built. Task 9: the owner's PM data repository moved onto `pmdata` with `pm_data.py init --adopt` on 2026-10-11; not yet confirmed working end to end (docs/go-live.md).

One git repository holds the public kit and one user's private PM data (rulings ledger, backlog, refusals log, handoffs, state). The data lives on an orphan branch, `pmdata`, that shares no history with `main`, is never merged, and is pushed only to a second, private remote. Other users who clone the kit get `main` only.

## Layout

| What | Where |
|---|---|
| Kit | the repository's main checkout, branch `main`, remote `origin` (public) |
| PM data | a linked worktree of the same repository, branch `pmdata`, at a path the user picks (often the old PM folder) |
| Private remote | any other remote name; default `private`; only `pmdata` is ever pushed there |
| Record of the split | `<git common dir>/kit-pm-data.json`: `{"branch": "pmdata", "remote": "private", "root": "<sha of the data branch's root commit>"}` |
| Machine config | The machine config, key `pm_data`: `{"path": "<abs>", "branch": "pmdata", "remote": "private"}` (`Owned('kitlib.pmdata')`) |
| Hub mirror | `hub/<collection>/<doc_id>.json` in the data worktree: one folder per hub collection, one JSON file per document; written by `tools/hub_mirror.py save` (docs/action-hub.md) |

The root commit is recorded when the branch is made, so the guard still knows it if the branch is renamed or deleted. The branch is called `pmdata`, not `pm`, so `pm/<slug>` work branches keep working (git refuses `refs/heads/pm/x` while `refs/heads/pm` exists; `pmdata/<slug>` would collide the same way, so no work branch uses that prefix). A test asserts `init` refuses the name `pm`.

The data worktree's `.kit.json` (on `pmdata`) sets `repo.main_branch: "pmdata"` and `repo.remote` to the private remote, so every tool that reads "the main branch" and "the remote" there means the data branch and the private remote. Its `backlog.store` must be a folder, `main` or `branch`, never `ref`: the repository's one `refs/backlog/main` belongs to the kit, and the data branch must not share it. `pm_data.py init` refuses a `.kit.json` that says `ref`.

## Making the branch (`tools/pm_data.py init`)

1. Refuse unless run in the main checkout, on a branch, with no `pmdata` branch yet (or `--adopt` for an existing one) and a private remote name that is not the public remote and whose URL is not the public remote's (compared after trimming `.git` and a trailing slash).
2. `git commit-tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 -m <text>` makes the root on the empty tree; `git branch pmdata <sha>` pins it. This works on every git the kit supports; `git worktree add --orphan` needs 2.42.
3. `git worktree add <path> pmdata`, then `git worktree lock --reason "PM data"` so `git worktree prune` and the sweep (`sweep_skip` skips a locked worktree) never take it.
4. Branch config, set with explicit `git config` calls: `branch.pmdata.remote`, `branch.pmdata.pushRemote` and `branch.pmdata.merge=refs/heads/pmdata`. Set `push.default=simple` in the repository config if unset. Set nothing under `remote.<public>.*` (no `push` line: it would change bare pushes of other branches).
5. If a private URL was given: `git remote add <name> <url>` with git's default fetch refspec, then `git push <name> refs/heads/pmdata:refs/heads/pmdata`.
6. Write `kit-pm-data.json` and the machine config key; print the one line the user adds to `.kit.json` of the data worktree if it has none.

Built as `tools/pm_data.py init --path <dir> [--branch] [--remote] [--url] [--adopt]` over `kitlib/pmdata/init.py`, whose docstring is the detail. Every check runs before the first write, so a refusal (exit 1) changes nothing. Where it goes beyond the steps above: the empty tree is written by `git write-tree` of an empty index (the same id as the literal); `kit-pm-data.json` is written right after the branch, before anything is pushed; the private url is compared with the url and pushurl of every other remote, in every form it reaches (every matching `url.<base>.insteadOf` and `pushInsteadOf` rule applied; host/path for https, ssh and `host:path`; a path for `file://`, a UNC path for `file://server/share`), and a public remote with no url refuses; the name `pm`, a name with `/`, the prefix of an existing branch and a case twin of one are refused, names compared without case; a new branch is refused when the private remote (a remote-tracking ref, or `git ls-remote` when the remote answers) already has one; `--adopt` takes a branch with one root that no main-branch history holds, refuses a recorded root that differs, and refuses a `.kit.json` on the branch whose `repo.main_branch` or `repo.remote` is not the data branch and private remote or whose `backlog.store` is `ref`, the default included. Exit 3 means all was made locally and the push failed; the lines say how to recover, and, when the private remote already holds a different data branch, list the steps for the owner to run (the tool runs none): unlock and remove the data worktree, delete the local branch and `kit-pm-data.json`, fetch the remote's branch, then `init --adopt`; exit 4 means it stopped part-way and `--adopt` finishes it, and an unexpected error in the writes or the push is caught the same way (exit 4 once a write started, else exit 1 with its text); exit 5 means an error escaped `kitlib.pmdata.init` (`tools/pm_data.py` prints "crashed: <type>: <message>"; what was made is not known, and `init --adopt` checks it); 130 is Ctrl+C, with the lines made so far.

A bare `git push` in the data worktree goes to the upstream, the private remote; with no upstream it fails ("no upstream") rather than guessing. The pre-push check below is the enforcement; the config is only the default path.

## Never configure a refspec

`RefBackend.ensure_refspec` (`kitlib/backlog/store.py`) adds `+refs/backlog/main:refs/backlog/main` to `remote.<remote>.fetch` before it fetches; when the remote has no store, every later plain `git fetch` in that repo fails (the open stray-refspec defect). Nothing here may do the same. Every fetch and push in this design names its refspec on the command line (`git fetch origin refs/heads/main:refs/remotes/origin/main`, `git push private refs/heads/pmdata:refs/heads/pmdata`) and never edits `remote.*.fetch` or `remote.*.push`. The tests assert that `.git/config` is byte-identical after every verb except `init`.

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

## Code that must change (the spec disagrees with the code today)

- `kitlib/ledger/vcs.py` `finish` pushes with a literal `origin` and `default_base` returns `origin/<main_branch>`. In this layout `origin` is the public remote, so `ledger.py add --push` would try to push `pmdata` there. Both read `repo.remote` from the ledger repository's config instead.
- `kitlib/guard/prepush.py` `check` skipped any remote ref that is not `refs/heads/...` and judged only the main branch; it now runs the leak stage on every ref line first (see above).
- `kitlib/config/user.py` `SCHEMA` has no `pm_data`; `tests/config/test_user_docs.py` keeps `docs/kit-json.md` equal to the schema, so the key and its doc line land in the same commit.
- `kitlib/worktree/new.py` `create` loads the config of the main checkout (`load(main)`); the data worktree's own `.kit.json` is read only through the place rule above.

## Tests

Throwaway repositories (`tests/gitfixture.py`) with bare scratch remotes; no network.

- `init`: root has no parent and an empty tree; `pmdata` and `main` share no commit (`merge-base` fails); worktree locked; `branch.pmdata.*` and `push.default` as specified; `.git/config` has no `remote.*.push` and no changed `remote.*.fetch`; refuses a `ref` backlog store, an equal URL, an existing branch without `--adopt`.
- Bare `git push` in the data worktree reaches only the private remote; with no upstream it fails.
- Pre-push (`tests/guard/test_prepush.py` `PmDataTest`, `PrivateTest`): `pmdata` to the public remote refused, by name and with `--all`; a branch with a data-branch commit cherry-picked refused by its objects; a branch merging `pmdata` refused; a light and an annotated tag on a data commit refused; `main` to public passes, an empty file in both branches included; `pmdata` to private passes, `.git/config` unchanged; a tag, `main` or another branch to private refused; an unreadable or malformed record, or a root missing from the clone, fails closed; a remote given as URL is judged as the matching remote; deleting `pmdata` on public allowed; any deletion on private refused; a private URL another remote also has counts as public; no record changes nothing; the leak stage runs before the main-branch rules; a push by the private remote's name to a URL it does not push to, or with `git -c remote.private.pushurl=<other>`, is public; a remote path differing only in case is another repository; a remote named `origin/<x>` does not hide objects from `--remotes=origin`; a data ref, the remotes, the objects or the git directory git cannot read refuses.
- `ancestry_refusal` (`tests/gate/test_land_store.py` `PmDataAncestry`) refuses a branch containing the root before any merge, also with a folder store; an unreadable record, a missing root or an undecidable ancestry refuses.
- Guard (`tests/guard/test_rules.py` `PmDataNoVerify`): `push --no-verify`, abbreviated too, denied as `git-push-pmdata` when the record exists, `push_block` off, in a subagent and a marked session, not in the owner's own; `git send-pack` and `git-send-pack` denied the same way, and so is a push with a computed word (`git push $F`, `--n$F`, `git $S`), unchanged without a record; the fallback catches both on an unreadable line.
- Guard (`tests/guard/test_pmdata_gaps.py`): `git-remote-rewrite` for every repointing form, in a subagent, `pm` and `coordinator` session, not the owner's, not without a record, reads and other keys passing, `--global` judged by the machine config; `GIT_CONFIG_GLOBAL`/`SYSTEM`/`GIT_CONFIG` before a push pinned as denied; a computed program from a linked worktree and `& (Get-Command git)`; an alias for `push --no-verify`, a chain, a shell alias, an alias for send-pack, a remote rewrite or a helper, an unreadable alias config; a push from a copy holding the root, a shallow one, a check that fails, an unrelated repository passing; `git remote-https`, `http-push` and the dashed programs, also on an unreadable line. `tests/guard/test_pmdata_fixes.py`: an alias push from a copy; an alias under another `HOME` or with `-c`; `git config` options abbreviated (`--glo`, `--fil`, `--ed`) and `-f<path>`, for `git-remote-rewrite` and `git-hooks-path`; `HOME` set by `read`, `printf -v`, `for`, `mapfile`, `getopts` and cmd's `set`; a computed `git config` word; an alias for `config core.hooksPath`; the adopt refusal while the data worktree is gone; `gitread.has_commit` answers; an alias split at CR; `& (...)` not unreadable for the owner or without a record; an unknown directory with no machine `pm_data`.
- `kit_sync`: clean fast-forward; dirty, off main, mid-merge, landing held, offline (unreachable remote), diverged; `.git/config` unchanged afterwards.
- Guard place: a subagent edit under `pm_data.path` denied; the path's own `.kit.json` is the config used; other worktrees unchanged.
- Ledger: `add --commit --push` pushes to `repo.remote`.
- Scrub: the doc and shipped text carry no repo, owner or machine names (`tests/scrub`).

## Tasks

Each lands independently on its own branch; sizes are S/M/L.

| # | Task | Size | Gate and risk | Waits for | State |
|---|---|---|---|---|---|
| 1 | `kitlib/pmdata`: `pm_data` schema key, `kit-pm-data.json` read/write, `locate()`; docs/kit-json.md line | S | kit suite | - | done |
| 2 | `tools/pm_data.py init/--adopt` and its tests | M | kit suite | 1 | done |
| 3 | Pre-push leak stage and `ancestry_refusal` root; guard `--no-verify` row; docs/guard.md | M | kit suite; High risk (`kitlib/guard`), fresh reviewer | 1 | done |
| 4 | Guard place: `pm_data.path` as MAIN-like with its own config | S | kit suite; High risk, fresh reviewer | 1 | done |
| 5 | `ledger/vcs.py` uses `repo.remote` | S | kit suite | - | done |
| 6 | `kitlib/pmstart` `kit_sync` check; docs/pm-start.md | M | kit suite | 1 | done |
| 7 | `githooks/pre-commit` (ledger check on the ledger file) | S | kit suite; High risk (hooks) | 1 | done |
| 8 | `new-repo.ps1 -PmData/-PrivateRemote`; docs/install.md | S | kit suite plus the installer's dry run | 2 | done |
| 9 | Go-live: switch row in docs/go-live.md, then the migration in the PM's own data repository | S | joint review | 2-8 | migrated 2026-10-11; to confirm |

## Open - needs the owner

Nothing open.

## Decided by the owner

- **Pre-commit dispatch.** The kit's `githooks/pre-commit` runs only the kit's own checks; it does not chain to a repository-local hook (`core.hooksPath` is repository-wide, so such a hook never runs). Repository hooks may be wanted later, so the hook has one dispatch point where a chain step could slot in without rework: `kitlib/githooks/precommit.py` `CHECKS`, today the ledger check only (task 7; docs/ledger.md "Pre-commit check"). The `sh` front end `githooks/pre-commit` starts python only when `.kit.json` names `pre_commit_check`, so a step that must run in a repository that has not opted in also needs one condition there, beside that test. No chaining code is built now.
