---
name: land-branch
description: Use when you are a lander told by the coordinator to land one reviewed branch on the main branch, and as a coordinator before sending a branch to a lander and after every landing. Covers the expected hashes, <kit>/tools/land.py, reading its verdict, read-only verification, separate cleanup, what to do on a red gate, a stuck landing or a safety-check refusal, and the coordinator's landing brief, record closing and after-landing notices.
---

# Land a branch

Prerequisites, all from the coordinator: the branch, the expected main sha and the branch's reviewed tip sha (both full), the verdict in words (LAND, or LAND WITH FOLLOW-UP and where it is recorded), and any extra gate. Missing one: say what you need, do nothing. Read the repo profile's Landing and Shared resources sections (`python <kit>/tools/doc_index.py -C <main checkout> --find landing`); a repo whose profile names another landing command uses it, and these rules still hold.

1. Work from the main checkout on the main branch, never inside a worktree you will remove; `git status` clean; logs go to your scratch folder.
2. `git -C <main checkout> fetch <remote>` alone in its call, then `git rev-parse <main> <remote>/<main>`: both must equal the expected main sha, else stop and report.
3. Run the brief's extra gate first, under the lock the profile names, and release it: the landing books its own lock. Pure pass/fail: dispatch the test-runner role.
4. Land as one bare call, in the background with output to a log (the gate can outlast a shell timeout; a killed gate orphans its jobs), never through a pipe: `python <kit>/tools/land.py -C <main checkout> <branch> --expect-main <sha> --expect-tip <sha> [--lane <lane>] [--wait <s>]`. It queues, takes the landing lock, merges `--no-ff`, gates the merged tree under the machine lock, pushes only when green, undoes a red merge itself, and lands onto a main that moved forward from `--expect-main` while it waited.
5. Read the log's first line starting `LANDED `, `NOT LANDED:`, `STUCK:`, `REFUSED:`, `BUSY:`, `USAGE:` or `NOT RUN:`. Exit 0 landed; 9 landed ungated (report its `WARNING:` line: the coordinator runs the full gate); 1 not landed or 4 refused (nothing left behind), 2 usage, 5 busy: report the line and the failing lines, the coordinator decides; 3 STUCK: the lock stays held, report the `STUCK:` and `left:` lines verbatim and do nothing else.
6. Only a flake on the repo's list (`python <kit>/tools/doc_index.py -C <main checkout> --find flakes`) earns one rerun.
7. When the brief asks, size-check each doc both sides touched: merged lines = main's plus the branch's net (`git diff --numstat <merge-base> <branch> -- <path>`), plus or minus 1; else stop and report.
8. Verify read-only, whatever the exit code: `git ls-remote <remote> <main>` equals `git rev-parse <main>`, then one `git merge-base --is-ancestor <tip> <remote>/<main>` per branch (exit 0). Not verified: report.
9. Records: the branch closed its own in its last work commit; one the brief names still open at the pushed main is reported, never closed by you.
10. Cleanup, only after step 8: `land.py` removes the landed branch, worktree and remote branch itself. For any part its `kept` lines name: the repo's command (`python <kit>/tools/doc_index.py -C <main checkout> --find cleanup`), else `python <kit>/tools/land.py -C <main checkout> --sweep-landed` (a dry run; read it) then `python <kit>/tools/land.py -C <main checkout> --sweep-landed --apply`, or `python <kit>/tools/worktree_remove.py -C <main checkout> <worktree>`. "Filename too long" or "Permission denied": leave it, report. Absolute paths only.
11. Report one block: branch; old and new main sha; merge commit; test counts; extra gate; pushed yes/no with both shas; worktree removed; any record still open; any judgement you made.

## Coordinator: ordering a landing

- One landing at a time, each by a fresh lander role spawned for it (or a batch of up to 3 where `--find landing` allows; never a high-risk branch). The verdict goes in the new lander's brief, with the reviewers and the reviewed range base..tip: a verdict queued into a running lander draws a refusal.
- A branch lands only with your verdict and its full gate green on a tree that includes the main it lands on. `land.py` aborts any conflict, docs too: the author merges that main and re-gates; the brief carries no conflict rules.
- A test pinning files outside its module is mapped to them in the gate's selector (`--find gate`), or main goes red at the next landing.
- Before a record closes, move what must survive (owner decisions, quoted and dated, to the decisions log; constraints beside the code or the owning doc); open follow-ups become their own records.
- After every landing, tell every open workstream that main moved and what changed in their area.

## Never

Edit code; close or amend a backlog record; force-push; `git add -A`; `reset --hard`; `branch -D` or `--force`; `git worktree remove` or `git branch -d` from a worktree; chain a write with other commands; land without a verdict or a branch whose tip moved after review; wait in a loop, override or break a lock; kill a process by name; remove a worktree or untracked file the brief did not name. A safety-check refusal is final: report it verbatim, no retry, rewording, other shell or agent.

Details: `<kit>/docs/gate.md` ("Commands", "Exit codes", "Landing", "Branch cleanup"); `<kit>/docs/worktree-remove.md`.
