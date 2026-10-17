---
name: builder
description: Use when you build or fix on a branch - as worker, senior-worker, strong-worker, deep-worker or a specialty builder. Covers how to work, the smallest fix, commits, the gate, follow-ups and records, the report, the kit tools, and what you never do.
---

# Builder

1. Start with `python <kit>/tools/preflight.py -C <worktree>`. A profile section that names a topic: `python <kit>/tools/doc_index.py -C <worktree> --find <topic>` (`--tools` for tools, `--section "<heading>"` for one section), then that one section; never `docs/INDEX.md` whole. `<kit>/tools/codemap.py` shows where code lives. Use your search and read tools, not shell grep, cat or sed; a script you need goes in your scratch folder, never the repo; write targets are literal paths, never a shell variable.
2. Fix what was observed with the smallest change. No new config key, hook, subsystem, option, abstraction or generalisation unless the brief names it. Size the fix to the harm.
3. Understand before changing: state the mechanism you believe is at work and the observation that would prove you wrong, then look for it. A fix without a confirmed mechanism is a guess.
4. Stay inside your brief's scope: files that are not yours stay untouched; report what you noticed in them. Mark every inference as an inference: "I observed" and "I expect" are different sentences.
5. Prove the change was exercised: name the observable that would be absent without it, and show it. A green run alone proves only that nothing broke.
6. Commit by explicit path, small, the subject saying what and why: `python <kit>/tools/commit.py -C <worktree> -m "<subject>" <path> ...`; it adds the trailer for your host and model. Unsure of your model: say so; never amend to change a trailer. Commit at each milestone and before any revert, stash or checkout experiment, and keep a state note in your scratch folder (done, in progress, next, what is running) so a fresh agent can resume.
7. Base moved: merge the exact commit your caller named, never the local main, never rebase; re-run the gate with `--full` (skill branch-sync).
8. Run the gate to a log and read the log, never piped through grep, head or tail; the final one is `python <kit>/tools/gate.py -C <worktree> --full` on the exact tip you report. Waiting on it: `<kit>/docs/gate.md` "Waiting on a gate: who arms what" (your own gate: no Monitor). A pure pass/fail check whose log you do not need: dispatch the test-runner role. Tests you write bind port 0.
9. Docs land with the code they describe, on the same branch. Every doc sentence about code names the artefact, every figure has a source; cite sections by title; grep before re-pointing. A sentence the code contradicts is reported.
10. Use shared resources exactly as your brief says (locks, ports, scratch folder); two uses could collide and the brief does not say: stop and ask. Heavy work beyond your gate takes the machine lock (skill machine-lock-run).
11. Follow-ups: a minor follow-up of your story, or any small defect you find (a tool bug, a wrong doc line, a flake fix), is fixed on this branch when it fits in about one small commit; never a new record. Too large, needs another pulled item's files, or needs the owner: report it; the coordinator decides on a record (`backlog.py add --found-on <branch> --why-not-folded "<reason>"`) and leaves it unpriced. A record needs an observed failure cited (refusal row, failed run, incident, live finding), a concrete file:line or doc or dead-code cleanup, or an owner request. A hypothetical gets a Known-limits line or nothing; a one-off flake gets nothing until it fails a second time.
12. Report: lead with the answer, what is true now that was not before. Then what you confirmed and how, what remains inferred, branch and commits, the gate with the line that proves each result, and anything outside your scope. No narrative of your process.

## Never

- Put the owner's personal details (email address, name, account names) in anything sent off this machine: no User-Agent, header, URL, query string or form field. Web fetches use a neutral User-Agent.
- Chain a write (a commit, merge, reset, backlog.py write, landing command) with other commands in one shell call: compound commands draw safety-check refusals the same steps run singly do not.
- Go on after a safety-check (classifier) refusal: it is final; stop at the first one and report it verbatim, with no retry, rewording, other shell or agent.
- Kill a process by name or command-line match (`Stop-Process -Name`, `taskkill /IM`, `CommandLine -match ... | Stop-Process`): other sessions' gates match too. End only a process you started, by its pid, while it still has the start time you saw.
- Search from a filesystem or drive root (`find /`, a recursive listing of a whole drive): scope it to the repo, worktree or a known folder, with a timeout.
- Land, merge to the main branch, push, force-push or rewrite its history; `reset --hard` or `git clean`.
- Weaken a test or a check to get green, or retry a failing run more than once without a new idea.
- Review your own work as if you were independent, or keep going on a theory the evidence has already contradicted.
- Write the owner's clear-session marker.

Details: skill shell-and-worktree-traps; `<kit>/docs/gate.md`.
