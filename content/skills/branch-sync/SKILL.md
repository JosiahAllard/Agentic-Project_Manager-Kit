---
name: branch-sync
description: Use when the coordinator says the main branch moved and your branch must catch up. Covers merging the exact named commit, what never to do (rebase, local main, force-push), conflicts on your own branch, and re-gating.
---

# Bringing a branch up to date

The coordinator names a commit hash. You merge that hash, and nothing else. The repo's profile (`python tools/doc_index.py --find sync`, if the repo has that tool) may name a sync tool that does steps 4 to 6 for you; use it as one plain call, never wrapped in Set-Location, Start-Process or cmd /c, and never call main's copy of it.

1. Never rebase. Never merge local `main` or `origin/...` by name. Never force-push, reset or rewrite history. You do not push.
2. The tracked tree must be clean: commit work in progress by explicit path first.
3. Untracked generated files the base now tracks (for a Godot repo, `.uid` files for scripts you do not own) make git refuse the merge: delete only those first.
4. Merge the hash: `git merge --no-ff <hash>` (with no tool, end the message with the commit trailer your brief gives, naming your exact model; if you do not know your model, say so and stop).
5. It must be a commit on the base branch (`git merge-base --is-ancestor <hash> <remote>/<main>`). If not, do not substitute another hash: tell the coordinator.
6. Clean merge: check for regrowth, a file the base shrank a lot that came back big, or a stale branch reinstating deleted text. On regrowth: `git checkout <hash> -- <path>`, re-apply only your own change, commit.
7. Conflicts (merge left in progress): in files you changed, resolve on your own branch, with the Edit tool only - never a scratch script that rewrites a tracked file - and one command per call. In files you did not touch, or anything you did not write: `git merge --abort` and tell the coordinator.
8. Docs and backlog-record conflicts: do not keep both sides' lines blindly; a union resurrects rows the base retired. Run `git diff <base> -- <file>` and justify every printed line. If you cannot, stop.
9. Finish a conflicted merge with the commit tool (`python tools/commit.py`, naming the files you resolved; the repo's `"commit".merge_fold` setting lets the merge's own staged files through and the tool adds the trailer). Never `git commit --no-edit`: the classifier refuses it.
10. Rebuild and rerun your whole gate on the merged tree (`python tools/gate.py`); name the string that proves the merge is in the build. Then report the branch ready again with the hash you merged.
