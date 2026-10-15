---
name: backlog-ops
description: Use when you need to read or change a repo's backlog store with <kit>/tools/backlog.py - next, show, add, pull, close, block, set (including narrowing a record's scope). Gives the forms that pass the safety checks and what to do when one is refused.
---

# Backlog operations

The store is the only tracker, read and written only through `python <kit>/tools/backlog.py -C <worktree>` (with no verb it prints usage); its place is `.kit.json` `"backlog".store`. Every call is bare, one per call, naming your worktree with `-C <worktree>`: no `cd`, pipe, chaining or wrapper. Global options (`-C`, `--session`, `--as`) go before the verb.

1. Read: `python <kit>/tools/backlog.py -C <worktree> next` (the ranked offer for `--session`; `next --all` uncapped); the same way `show <ID>`, `list --blocked`, `list --needs-priority`, `stale --days <N>` (untouched records, dead body paths, moved `files`).
2. Add: `python <kit>/tools/backlog.py -C <worktree> add <ID> --summary "<line>" --scope <path> [--scope <path>] --found-on <branch|none> --why-not-folded "<reason>"` (`none` also for a branch that landed and was removed: name it in the reason). Always unpriced (no priority flag); scope = the real files. A scope overlapping the finding branch's diff or pulled records is REFUSED: fold it into that branch, or file it after that branch lands.
3. Pull what `next` offers, before work starts: `python <kit>/tools/backlog.py -C <worktree> pull <ID>`; a session-owned item: `python <kit>/tools/backlog.py -C <worktree> --session <key> pull <ID>` (a retry that only adds `--session` needs no `--reason`).
4. Unpriced records refuse the pull: a worker never prices; it stops and reports to its coordinator. Outside the offer is a pre-emption: `--reason "<why>"`, plus `--ruling <id>` where the repo requires it. A pull whose scope collides with a live pull on another branch needs `--reason "<why>" --ruling R-nnnn`, the PM's ruling; so does widening a pulled record's scope into one with `set --scope`. `priority: -1` needs the owner's ruling.
5. Narrow an over-broad scope instead of overriding a collision: `python <kit>/tools/backlog.py -C <worktree> set <ID> --scope <path> [--scope <path>]`, then `show <ID>`.
6. Close with the work, in the same commit or as the profile says: `python <kit>/tools/backlog.py -C <worktree> close <ID> --reason "<what was done>"` (owner-held: `--ruling <id>`). Not to be done: `python <kit>/tools/backlog.py -C <worktree> retire <ID> --why "<why>"`. Give a pull back: `python <kit>/tools/backlog.py -C <worktree> release <ID> --reason "<why>"`.
7. Block: `python <kit>/tools/backlog.py -C <worktree> block <ID> --on <member> [--on <member>] [--owner-ask "<question>"]` (members include `owner`, `item:<ID>`); `unblock <ID>` clears a lone blocker, `python <kit>/tools/backlog.py -C <worktree> unblock <ID> --on <member> --reason "<why>"` one of several (clearing `owner` needs `--ruling`).
8. Price and edit: `python <kit>/tools/backlog.py -C <worktree> set <ID> --priority <1-5> --priority-by <who>` (an owner price takes `--ruling`); `set <ID> --body-file <path>`; park on an owner question: `python <kit>/tools/backlog.py -C <worktree> set-status <ID> parked --q <Q-id>`.
9. `python <kit>/tools/backlog.py -C <worktree> check` exits 0 before you commit.

Store kinds: with `main` a write stages the record in your worktree (it rides in your commit) and refuses the main checkout; with `ref` or `branch` it lands on the store at once and never touches the checkout.

Exit 0 OK; 1 `NOT FOUND:`, `ERROR:` or a RED check; 2 `USAGE:` or `REFUSED:`. `USAGE:`, `NOT FOUND:` and `ERROR:` are your own mistake: fix the command once. Only `REFUSED:` is a policy guard.

## Never

- Edit a record file or the store's git objects by hand.
- Write a machine path in any field: use the placeholder the repo documents (for example `%REPO_DATA%/...`).
- Invent a ruling id, or take a role or model label as an owner price or authorization.
- Pass `--reason` to fill a free slot or to re-run a pull the tool just refused. `--reason` never overrides a collision with a live pull.
- Retry, reword, add a pre-emption flag or switch shell after a real refusal (`REFUSED:`, safety check or hook): stop that line, send your coordinator the exact command, directory and verbatim refusal, and continue other work.

Details: `<kit>/docs/backlog.md` ("Commands", "The fold-in guard", "Owner gates").
