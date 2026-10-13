---
name: land-branch
description: Use when you are a lander told by the coordinator to land one reviewed branch on the main branch, and as a coordinator before queueing a branch for landing and after every landing. Covers <kit>/tools/land.py --submit, the wait-result verdicts, read-only verification, separate cleanup, what to do on a conflict, a red gate, a stuck landing or a safety-check refusal, and the coordinator's queueing, record closing and after-landing notices.
---

# Land a branch

Landings go through the machine-lock queue as one entry each. The coordinator submits each LAND verdict at once (`land.py --submit`) and keeps a background `gate.py wait-result` running; a lander is an optional wrapper that does the same for one branch and reports. Landings go ahead of every ordinary waiting run on their own and merge onto whatever main is at their turn; nothing aligns them.

## Lander (optional)

Prerequisites, all from the coordinator: the branch, its reviewed tip sha (full), the verdict in words (LAND, or LAND WITH FOLLOW-UP and where it is recorded), and any extra gate. Missing one: say what you need, do nothing. Read the repo profile's Landing and Shared resources sections (`python <kit>/tools/doc_index.py -C <main checkout> --find landing`); a repo whose profile names another landing command uses it, and these rules still hold.

1. Work from the main checkout on the main branch, never inside a worktree you will remove; `git status` clean; logs go to your scratch folder.
2. Run the brief's extra gate first, under the lock the profile names, and release it: the landing books its own lock. Pure pass/fail: dispatch the test-runner role.
3. Submit as one bare call: `python <kit>/tools/land.py -C <main checkout> <branch> --expect-tip <sha> --submit [--lane <lane>]`. A detached process books one machine-lock entry, takes the landing lock, merges the main it finds `--no-ff`, gates the merged tree, pushes only when green, and undoes a red merge itself. The call prints one `SUBMITTED ... wait: ...` line and exits 0; no such line: report the output.
4. Start the `gate.py wait-result ... --kind land` command that line prints, as printed, in the background with output to a log (never a pipe, polling loop or Monitor), and wait for its notification. `python <kit>/tools/land.py -C <main checkout> --queue` shows who holds and who waits.
5. Read the verdict. OK: landed. LANDED_UNGATED: landed, report its `WARNING:` line (the coordinator runs the full gate). CONFLICT: nothing left behind; report it, the author merges main (never resolve it yourself). FAILED, RED or REFUSED: nothing left behind; report the line and the failing lines, the coordinator decides. STUCK: the lock stays held; report the `STUCK:` and `left:` lines verbatim and do nothing else.
6. Only a flake on the repo's list (`python <kit>/tools/doc_index.py -C <main checkout> --find flakes`) earns one resubmit.
7. When the brief asks, size-check each doc both sides touched: merged lines = main's plus the branch's net (`git diff --numstat <merge-base> <branch> -- <path>`), plus or minus 1; else stop and report.
8. Verify read-only, whatever the verdict: `git ls-remote <remote> <main>` equals `git rev-parse <main>`, then one `git merge-base --is-ancestor <tip> <remote>/<main>` per branch (exit 0). Not verified: report.
9. Records: the branch closed its own in its last work commit; one the brief names still open at the pushed main is reported, never closed by you.
10. Cleanup, only after step 8: `land.py` removes the landed branch, worktree and remote branch itself. For any part its `kept` lines name: the repo's command (`python <kit>/tools/doc_index.py -C <main checkout> --find cleanup`), else `python <kit>/tools/land.py -C <main checkout> --sweep-landed` (a dry run; read it) then `python <kit>/tools/land.py -C <main checkout> --sweep-landed --apply`, or `python <kit>/tools/worktree_remove.py -C <main checkout> <worktree>`. "Filename too long" or "Permission denied": leave it, report. Absolute paths only.
11. Report one block: branch; old and new main sha; merge commit; test counts; extra gate; pushed yes/no with both shas; worktree removed; any record still open; any judgement you made.

## Coordinator: queueing a landing

- Submit every LAND verdict as soon as you give it, from the main checkout: `python <kit>/tools/land.py -C <main checkout> <branch> --expect-tip <reviewed tip> --submit` (a batch of up to 3 where `--find landing` allows; never a high-risk branch). It prints one `SUBMITTED ...` line. Start the `gate.py wait-result <branch> --tip <sha> --kind land` command it prints as a background command, one per submission, and act when the host wakes you on its exit; do not poll, loop or sleep. No go to anyone, no expected-main hash, no waiting for a free lane: submitted landings queue and merge in ticket order, each onto the main it finds. A lander role (optional) may do the submit, wait and report for you.
- Read the verdict. OK -> close records (below) and tell workstreams main moved. CONFLICT -> send the branch back to its author to merge main and re-gate, re-review the change, resubmit with the new tip. RED or FAILED -> nothing landed: send it back with the failing lines, as for any red gate. REFUSED -> report the line and fix its cause. STUCK or LANDED_UNGATED -> report the lines verbatim; STUCK leaves the lock held for the owner, LANDED_UNGATED needs the full gate on main.
- Never pass anything to jump the queue and never break a landing lock: a landing waits in the machine-lock queue like any run and goes ahead of ordinary ones by itself.
- A branch lands only with your verdict and its full gate green. `land.py` aborts any conflict, docs too: the author merges main and re-gates; the brief carries no conflict rules.
- A test pinning files outside its module is mapped to them in the gate's selector (`--find gate`), or main goes red at the next landing.
- Before a record closes, move what must survive (owner decisions, quoted and dated, to the decisions log; constraints beside the code or the owning doc); open follow-ups become their own records.
- After every landing, tell every open workstream that main moved and what changed in their area.
- A fresh or successor session reads `land.py --queue` and the land result records first (skill session-handoff), and never resubmits a branch already queued.

## Never

Edit code; close or amend a backlog record; force-push; `git add -A`; `reset --hard`; `branch -D` or `--force`; `git worktree remove` or `git branch -d` from a worktree; chain a write with other commands; land without a verdict or a branch whose tip moved after review; wait in a loop, override or break a lock; kill a process by name; remove a worktree or untracked file the brief did not name. A safety-check refusal is final: report it verbatim, no retry, rewording, other shell or agent.

Details: `<kit>/docs/gate.md` ("Commands", "Exit codes", "Landing", "Branch cleanup"); `<kit>/docs/worktree-remove.md`.
