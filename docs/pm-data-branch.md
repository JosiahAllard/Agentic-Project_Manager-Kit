# PM data on a private branch of the kit repo

Status: design, not built. Not live until the kit's go-live review (docs/go-live.md).

One git repository holds the public kit and one user's private PM data (rulings ledger, backlog, refusals log, handoffs, state). The data lives on an orphan branch, `pmdata`, that shares no history with `main`, is never merged, and is pushed only to a second, private remote. Other users who clone the kit get `main` only.

## Layout

| What | Where |
|---|---|
| Kit | the repository's main checkout, branch `main`, remote `origin` (public) |
| PM data | a linked worktree of the same repository, branch `pmdata`, at a path the user picks (often the old PM folder) |
| Private remote | any other remote name; default `private`; only `pmdata` is ever pushed there |
| Record of the split | `<git common dir>/kit-pm-data.json`: `{"branch": "pmdata", "remote": "private", "root": "<sha of the data branch's root commit>"}` |
| Machine config | `~/.claude-pm-kit/config.json`, key `pm_data`: `{"path": "<abs>", "branch": "pmdata", "remote": "private"}` (`Owned('kitlib.pmdata')`) |

The root commit is recorded when the branch is made, so the guard still knows it if the branch is renamed or deleted. The branch is called `pmdata`, not `pm`, so `pm/<slug>` work branches keep working (git refuses `refs/heads/pm/x` while `refs/heads/pm` exists; `pmdata/<slug>` would collide the same way, so no work branch uses that prefix). A test asserts `init` refuses the name `pm`.

The data worktree's `.kit.json` (on `pmdata`) sets `repo.main_branch: "pmdata"` and `repo.remote` to the private remote, so every tool that reads "the main branch" and "the remote" there means the data branch and the private remote. Its `backlog.store` must be a folder, `main` or `branch`, never `ref`: the repository's one `refs/backlog/main` belongs to the kit, and the data branch must not share it. `pm_data.py init` refuses a `.kit.json` that says `ref`.

## Making the branch (`tools/pm_data.py init`)

1. Refuse unless run in the main checkout, on a branch, with no `pmdata` branch yet (or `--adopt` for an existing one) and a private remote name that is not the public remote and whose URL is not the public remote's (compared after trimming `.git` and a trailing slash).
2. `git commit-tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 -m <text>` makes the root on the empty tree; `git branch pmdata <sha>` pins it. This works on every git the kit supports; `git worktree add --orphan` needs 2.42.
3. `git worktree add <path> pmdata`, then `git worktree lock --reason "PM data"` so `git worktree prune` and the sweep (`sweep_skip` skips a locked worktree) never take it.
4. Branch config, set with explicit `git config` calls: `branch.pmdata.remote`, `branch.pmdata.pushRemote` and `branch.pmdata.merge=refs/heads/pmdata`. Set `push.default=simple` in the repository config if unset. Set nothing under `remote.<public>.*` (no `push` line: it would change bare pushes of other branches).
5. If a private URL was given: `git remote add <name> <url>` with git's default fetch refspec, then `git push <name> refs/heads/pmdata:refs/heads/pmdata`.
6. Write `kit-pm-data.json` and the machine config key; print the one line the user adds to `.kit.json` of the data worktree if it has none.

A bare `git push` in the data worktree goes to the upstream, the private remote; with no upstream it fails ("no upstream") rather than guessing. The pre-push check below is the enforcement; the config is only the default path.

## Never configure a refspec

`RefBackend.ensure_refspec` (`kitlib/backlog/store.py`) adds `+refs/backlog/main:refs/backlog/main` to `remote.<remote>.fetch` before it fetches; when the remote has no store, every later plain `git fetch` in that repo fails (the open stray-refspec defect). Nothing here may do the same. Every fetch and push in this design names its refspec on the command line (`git fetch origin refs/heads/main:refs/remotes/origin/main`, `git push private refs/heads/pmdata:refs/heads/pmdata`) and never edits `remote.*.fetch` or `remote.*.push`. The tests assert that `.git/config` is byte-identical after every verb except `init`.

## Pre-push guard

`kitlib/guard/prepush.py` `check` judges only `refs/heads/<main>` remote refs today, so `git push origin pmdata` and a branch containing the data history pass it. Add a first stage that runs when `kit-pm-data.json` exists, before the main-branch rules, for every ref line and whatever the remote:

- **Leak by name.** The remote is not the private remote and the local or remote ref is `refs/heads/<data branch>`: refuse.
- **Leak by history.** The remote is not the private remote and the pushed local sha has the recorded `root` as an ancestor (`refs.is_ancestor`): refuse. This covers a data-branch commit merged or cherry-picked onto main, any other branch and any tag. A deleting push (all-zero local sha) is not judged.
- **Private remote gets only the data.** The remote is the private remote and the remote ref is not `refs/heads/<data branch>` (so no tag and no other branch goes there), or the local sha lacks the root as ancestor: refuse. (A private remote that also held the kit would let `main` and the data branch mix there.)
- **Fail closed.** `kit-pm-data.json` unreadable or malformed, or the root object missing from this clone, refuses every push with the file named.

`remote` comes from git's argv (the remote's name or, for a URL push, its URL; a URL equal to a configured remote's counts as that remote). The existing main-branch rules then run as before. `--no-verify` skips the hook, as for every rule here; the `git-push-main` guard rule still covers agent sessions, and a new row denies an agent's `--no-verify` on a push when `kit-pm-data.json` exists (docs/guard.md "Rules").

`gate.py land` also refuses a branch whose history contains the recorded root: add it to `ancestry_refusal` (`kitlib/backlog/at_landing.py`) beside the backlog store's root, so a contaminated branch is refused before the merge instead of after it.

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
| Data worktree folder deleted | `git worktree prune` is blocked by the lock; `pm_data.py init --adopt` re-adds it |
| Pre-push runs with `--no-verify` | not caught; the data was never on the public remote's `pmdata` unless pushed by name, which the agent rule denies |
| A contaminated branch reaches `main` | `ancestry_refusal` before the merge, then the pre-push root check |
| `git gc` / shallow clones of the data worktree's repo | out of scope; the root commit must stay reachable from `pmdata` |

## Code that must change (the spec disagrees with the code today)

- `kitlib/ledger/vcs.py` `finish` pushes with a literal `origin` and `default_base` returns `origin/<main_branch>`. In this layout `origin` is the public remote, so `ledger.py add --push` would try to push `pmdata` there. Both read `repo.remote` from the ledger repository's config instead.
- `kitlib/guard/prepush.py` `check` skips any remote ref that is not `refs/heads/...` and judges only the main branch (see above).
- `kitlib/config/user.py` `SCHEMA` has no `pm_data`; `tests/config/test_user_docs.py` keeps `docs/kit-json.md` equal to the schema, so the key and its doc line land in the same commit.
- `kitlib/worktree/new.py` `create` loads the config of the main checkout (`load(main)`); the data worktree's own `.kit.json` is read only through the place rule above.

## Tests

Throwaway repositories (`tests/gitfixture.py`) with bare scratch remotes; no network.

- `init`: root has no parent and an empty tree; `pmdata` and `main` share no commit (`merge-base` fails); worktree locked; `branch.pmdata.*` and `push.default` as specified; `.git/config` has no `remote.*.push` and no changed `remote.*.fetch`; refuses a `ref` backlog store, an equal URL, an existing branch without `--adopt`.
- Bare `git push` in the data worktree reaches only the private remote; with no upstream it fails.
- Pre-push (extends `tests/guard/test_prepush.py`): `pmdata` to the public remote refused; a branch with a data-branch commit cherry-picked refused; a tag on one refused; `main` to public passes; `pmdata` to private passes; a tag or `main` to private refused; unreadable record fails closed; remote given as URL.
- `ancestry_refusal` refuses a branch containing the root.
- `kit_sync`: clean fast-forward; dirty, off main, mid-merge, landing held, offline (unreachable remote), diverged; `.git/config` unchanged afterwards.
- Guard place: a subagent edit under `pm_data.path` denied; the path's own `.kit.json` is the config used; other worktrees unchanged.
- Ledger: `add --commit --push` pushes to `repo.remote`.
- Scrub: the doc and shipped text carry no repo, owner or machine names (`tests/scrub`).

## Tasks

Each lands independently on its own branch; sizes are S/M/L.

| # | Task | Size | Gate and risk | Waits for |
|---|---|---|---|---|
| 1 | `kitlib/pmdata`: `pm_data` schema key, `kit-pm-data.json` read/write, `locate()`; docs/kit-json.md line | S | kit suite | - |
| 2 | `tools/pm_data.py init/--adopt` and its tests | M | kit suite | 1 |
| 3 | Pre-push leak stage and `ancestry_refusal` root; guard `--no-verify` row; docs/guard.md | M | kit suite; High risk (`kitlib/guard`), fresh reviewer | 1 |
| 4 | Guard place: `pm_data.path` as MAIN-like with its own config | S | kit suite; High risk, fresh reviewer | 1 |
| 5 | `ledger/vcs.py` uses `repo.remote` | S | kit suite | - |
| 6 | `kitlib/pmstart` `kit_sync` check; docs/pm-start.md | M | kit suite | 1 |
| 7 | `githooks/pre-commit` (ledger check on the ledger file) | S | kit suite; High risk (hooks) | 1 |
| 8 | `new-repo.ps1 -PmData/-PrivateRemote`; docs/install.md | S | kit suite plus the installer's dry run | 2 |
| 9 | Go-live: switch row in docs/go-live.md, then the migration in the PM's own data repository | S | joint review | 2-8 |

## Open - needs the owner

Nothing open.

## Decided by the owner

- **Pre-commit dispatch.** The kit's `githooks/pre-commit` runs only the kit's own checks; it does not chain to a repository-local hook (`core.hooksPath` is repository-wide, so such a hook never runs). Repository hooks may be wanted later, so the hook has one dispatch point where a chain step could slot in without rework: `kitlib/githooks/precommit.py` `CHECKS`, today the ledger check only (task 7; docs/ledger.md "Pre-commit check"). The `sh` front end `githooks/pre-commit` starts python only when `.kit.json` names `pre_commit_check`, so a step that must run in a repository that has not opted in also needs one condition there, beside that test. No chaining code is built now.
