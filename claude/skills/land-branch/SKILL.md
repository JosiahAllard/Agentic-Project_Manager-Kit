---
name: land-branch
description: Use when you are a lander told by the coordinator to land one reviewed branch on the main branch, and as a coordinator before sending a branch to a lander and after every landing. Covers the expected hashes, tools/land.py, reading its verdict, read-only verification, separate cleanup, what to do on a red gate, a stuck landing or a classifier refusal, and the coordinator's landing brief, record closing and after-landing notices.
---

# Land a branch

Prerequisites, all from the coordinator's message: the branch, the expected main sha (full), the branch's reviewed tip sha (full), the review verdict in words (LAND, or LAND WITH FOLLOW-UP and where it is recorded), and any extra gate. Missing one: say what you need, do nothing. Read the repo's `.claude/agent-profile.md` sections Landing and Shared resources once; each names a topic, so `python tools/doc_index.py --find landing` and read the section it names. A repo whose profile names a different landing command uses that one; the rules below still hold.

## Steps

1. Work from the main checkout on the main branch, never inside a worktree you will remove. `git status` clean. Use your scratch folder for logs.
2. `git -C <main checkout> fetch <remote>` alone in its call (the guard lets a lander fetch the main checkout from a worktree only so: nothing chained but one `cd` into that checkout), then `git rev-parse <main> <remote>/<main>`: both must equal the expected main sha. Differs: stop, report; the coordinator re-confirms.
3. Land, as one bare call with output to a log file and `run_in_background` (the gate can outlast the default tool timeout; a killed gate orphans its jobs), never through a pipe:
   `python tools/land.py <branch> --expect-main <sha> --expect-tip <sha> [--lane <lane>] [--wait <s>]`
   (the repo's stub; the kit checkout's `tools/land.py` where the repo has none). Both hashes are full hashes. It waits for its turn in the repo's landing queue, takes the landing lock, merges `--no-ff`, gates the merged tree under the machine lock, and pushes only when that is green; on a red merged gate it undoes the merge itself. A landing that waited for its turn may find main moved forward: when the new main descends from `--expect-main`, it merges onto the new main and lands, saying `main moved on from --expect-main`; main moved any other way, or main moved before a landing that did not wait, is REFUSED. Extra gate from the brief first, under the lock the profile names. Pure pass/fail: dispatch `test-runner`.
4. Read the verdict from the log's first line starting `LANDED `, `NOT LANDED:`, `STUCK:`, `REFUSED:`, `BUSY:`, `USAGE:` or `NOT RUN:` (docs/gate.md). Exit codes: 0 landed; 1 `NOT LANDED` or gate failed, nothing left behind (read the failing step's log from the folder it names); 2 usage; 3 STUCK; 4 REFUSED, nothing was changed; 5 BUSY, another landing holds the lock. Only a flake on the repo's list (`python tools/doc_index.py --find flakes`) earns one rerun; anything else is a real failure.
   - 1, 2, 4, 5: report the line and the failing lines; the coordinator decides. Never wait in a loop, never override or break a lock.
   - 3 STUCK: the landing lock stays held. Report the `STUCK:` line and its `left:` line verbatim and do nothing else; the numbered steps it prints are for the coordinator to approve.
5. Size check each doc both sides touched when the brief asks for one: merged line count = main's count plus the branch's net lines (`git diff --numstat <merge-base> <branch> -- <path>`), plus or minus 1; else stop and report (an auto-merge can restore text main deleted).
6. Verify, read-only, whatever the exit code said: `git ls-remote <remote> <main>` and `git rev-parse <main>` match, then one `git merge-base --is-ancestor <branch tip> <remote>/<main>` per branch (exit 0). Nothing else in those calls. Not verified: report; the coordinator decides.
7. Backlog records: a branch closes its own record in its last work commit; you never close, remove or amend one. A record the brief names that is still open at the pushed main: report it; the coordinator routes the close.
8. Cleanup only after step 6, only the worktree you were told is ours and only if clean, each its own call. `land.py` removes the landed branch's remote branch, worktree and local branch itself; for any part its `kept` lines name: the repo's cleanup command from `python tools/doc_index.py --find cleanup`, else `python tools/land.py --sweep-landed` (a dry run; read it), then `python tools/land.py --sweep-landed --apply`, or `python tools/worktree_remove.py <worktree>` for the one worktree. Not `git worktree remove` or `git branch -d` in the main checkout: from a worktree the guard refuses them. Never `branch -D`, never `--force`. Delete only what the brief lists: an untracked stray it never named is reported, not removed. "Filename too long" or "Permission denied": leave it and report. Use absolute paths (`cd` holds a Windows handle).
9. Report one block: branch; old and new main sha; fast-forward or merge commit; test counts; extra gate result; pushed yes/no with both shas; worktree removed yes/no; any record still open; any judgement you made.

## Classifier refusal

Stop that line of work. Report the verbatim reason to the coordinator. No retry, no rewording, no other shell or agent, no reading files to justify it. Refusals are final.

## Coordinator: ordering a landing

- One at a time, each through a fresh `lander` spawned for that landing - or a batch of up to 3
  where the repo allows it (`--find landing`; never a high-risk branch). A verdict queued into a
  running lander draws a classifier refusal, so the verdict goes in the new lander's brief: the
  prerequisites above, naming the reviewer(s) and the reviewed range base..tip.
- A branch lands only with your verdict and its full gate green on a tree that includes the main
  it will land on. `land.py` aborts any conflict (docs too): the lander resolves none and the
  brief carries no conflict rules; the author merges that main and re-gates.
- A test that pins files outside its own module must be mapped to them in the gate's selector
  (`--find gate`), or a change to them lands untested and the next landing finds main red.
- The backlog store is the only tracker; closing an item is part of the landing (skill
  backlog-ops), never a records-agent edit; git history and the landing message are the record.
  First move what must survive out of the closing record: the owner's decisions, quoted and dated -> the
  decisions log; constraints later work relies on -> beside the code they constrain or the doc
  owning the area. A closed record has no file and agents never search history for one. Open
  follow-ups become their own records, never a line kept inside an old one.
- After every landing tell every open workstream that main moved and what changed in their area.

## Never

Edit code; close or amend a backlog record; force-push; `git add -A`; chain a write with other commands; land without a verdict or a branch whose tip moved after review; kill a process by name; remove a worktree you were not told to land; `reset --hard`.
