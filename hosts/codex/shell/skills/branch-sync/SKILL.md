---
name: branch-sync
description: Use when the coordinator says the main branch moved and your branch must catch up. Covers merging the exact named commit, what never to do (rebase, local main, force-push), conflicts on your own branch, and re-gating.
---

# Bringing a branch up to date

The coordinator names a commit hash. You merge that hash, and nothing else. Run each command as one plain call, never wrapped in Set-Location, Start-Process or `cmd /c`: the kit's tool (`<kit>/tools/...`) with `-C <worktree>` naming your worktree.

1. Commit work in progress by explicit path first (`python <kit>/tools/commit.py -C <worktree> -m "<subject>" <path> ...`): the tracked tree must be clean.
2. Untracked generated files the base now tracks make git refuse the merge: delete only those the repo profile's "Branches and worktrees" section names, nothing else.
3. Merge: `python <kit>/tools/sync_base.py -C <worktree> <hash>`. It fetches, refuses a hash not on the fetched remote main (do not substitute another: tell the coordinator), merges exactly that hash `--no-ff`, checks a clean merge for regrowth and adds the commit trailer.
4. Regrowth reported (a file the base shrank came back big, a stale branch reinstating deleted text): `git checkout <hash> -- <path>`, re-apply only your own change, commit it.
5. Conflicts (the merge is left in progress) in files you changed: resolve on your branch with your file-edit tool, one command per call, never a scratch script that rewrites a tracked file.
6. Conflicts in files you did not touch, or text you did not write: `git merge --abort` and tell the coordinator.
7. Docs and backlog-record conflicts: never keep both sides blindly; a union resurrects rows the base retired. Run `git diff <hash> -- <file>` and justify every printed line, deletions and additions; if you cannot, stop.
8. Finish a conflicted merge with `python <kit>/tools/commit.py -C <worktree> -m "<subject>" <resolved path> ...` (the repo's `"commit".merge_fold` lets the merge's own staged files through; the tool adds the trailer).
9. Re-gate the merged tree: `python <kit>/tools/gate.py -C <worktree> --full` on a strict branch (a path in `STRICT_DEFAULTS`); on any other, the optimized default gate (no flag) once the repo's gate config names smoke steps, `--full` until then (landing refuses a tip without the right record; `<kit>/docs/gate.md` "Smoke steps"). Name the string that proves the merge is in the build.
10. Report the branch ready again, with the hash you merged and the gate's verdict line.

## Never

- Rebase, merge local `main` or `origin/...` by name, force-push, reset or rewrite history. You do not push.
- `git commit --no-edit` (the safety check refuses it), or a commit identity the tool did not write.
- Resolve a conflict in someone else's change.

Details: `<kit>/docs/sync-base.md`; the repo profile, "Branches and worktrees".
