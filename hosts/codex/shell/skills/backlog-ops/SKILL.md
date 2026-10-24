---
name: backlog-ops
description: Use when you need to read or change a repo's backlog store with <kit>/tools/backlog.py - next, show, add, pull, close, block, set (including narrowing a record's scope). Gives the forms that pass the safety checks and what to do when one is refused.
---

# Backlog operations

The store is the only tracker, read and written only through `python <kit>/tools/backlog.py -C <worktree>` (with no verb it prints usage); its place is `.kit.json` `"backlog".store`. Every call is bare, one per call, naming your worktree with `-C <worktree>`: no `cd`, pipe, chaining or wrapper. Global options (`-C`, `--session`, `--as`) go before the verb.

1. Read: `python <kit>/tools/backlog.py -C <worktree> next` (the ranked offer for `--session`; `next --all` uncapped); the same way `show <ID>`, `list --blocked`, `list --needs-priority`, `list --status <open|blocked|parked|pulled>` (no `--in-flight` or `--parked`; in flight is `pulled`), `stale --days <N>` (untouched records, dead body paths, moved `files`). `show` printing `NOT FOUND: ... removed by <sha>` means the record was closed, retired or deleted.
2. Add: `python <kit>/tools/backlog.py -C <worktree> add <ID> --summary "<line>" --scope <path> [--scope <path>] --found-on <branch|none> --why-not-folded "<reason>"` (`none` also for a branch that landed and was removed: name it in the reason). Always unpriced (no priority flag); scope = the real files. A scope overlapping the finding branch's diff or pulled records is REFUSED: fold it into that branch, or file it after that branch lands. A record needs a cited observed failure, a file:line cleanup or an owner request; a hypothetical gets a Known-limits line or nothing, and a one-off flake no record until its second failure.
3. Pull what `next` offers, before work starts: `python <kit>/tools/backlog.py -C <worktree> pull <ID>`; a session-owned item: `python <kit>/tools/backlog.py -C <worktree> --session <key> pull <ID>` (a retry that only adds `--session` needs no `--reason`).
4. Unpriced records refuse the pull: a worker never prices; it stops and reports to its coordinator. A pull outside the offer is a pre-emption, the owner's decision: a coordinator asks it through the PM and, on the owner's yes, runs `pull <ID> --reason "<why>"`; a worker stops and reports. A pull whose scope collides with a live pull on another branch is a lead's action (`pull-collision`): a coordinator or the PM runs the same `pull <ID> --reason "<why>"`; a worker stops and reports. Widening a pulled record's scope into one is the same kind (`set-widen-collision`): `set <ID> --scope <path> [--scope <path> ...]`, the whole new list, one `--scope` per path, no `--reason`. A `priority: -1` record is never pulled: the owner or the PM releases its hold first.
5. Narrow an over-broad scope instead of overriding a collision: `python <kit>/tools/backlog.py -C <worktree> set <ID> --scope <path> [--scope <path>]`, then `show <ID>`.
6. Close with the work, in the same commit or as the profile says: `python <kit>/tools/backlog.py -C <worktree> close <ID> --reason "<what was done>"` (an owner-held one, and a retire or delete of an owner- or PM-priced one: only the owner or the PM; their role decides it, no flag). Not to be done: `python <kit>/tools/backlog.py -C <worktree> retire <ID> --why "<why>"`. Give a pull back: `python <kit>/tools/backlog.py -C <worktree> release <ID> --reason "<why>"`.
7. Block: `python <kit>/tools/backlog.py -C <worktree> block <ID> --on <member> [--on <member>] [--owner-ask "<question>"]` (members include `owner`, `item:<ID>`); `unblock <ID>` clears a lone blocker, `python <kit>/tools/backlog.py -C <worktree> unblock <ID> --on <member> --reason "<why>"` one of several (clearing `owner`: only the owner or the PM).
8. Price and edit: `python <kit>/tools/backlog.py -C <worktree> set <ID> --priority <1-5> --priority-by <who>` (an owner or PM price, a reprice of an owner- or PM-priced record or one blocked on the owner, and setting 1 or -1 are the owner's and the PM's; no verb takes a ruling id); `set <ID> --body-file <path>`; park on an owner question: `python <kit>/tools/backlog.py -C <worktree> set-status <ID> parked --q <Q-id>`.
9. `python <kit>/tools/backlog.py -C <worktree> check` exits 0 before you commit.

Store kinds: with `main` a write stages the record in your worktree (it rides in your commit) and refuses the main checkout; with `ref` or `branch` it lands on the store at once and never touches the checkout.

Exit 0 OK; 1 `NOT FOUND:`, `ERROR:` or a RED check; 2 `USAGE:` or `REFUSED:`. `USAGE:`, `NOT FOUND:` and `ERROR:` are your own mistake: fix the command once. Only `REFUSED:` is a policy guard.

## Refusals

The kit's one statement of the refusal rule; every other role and skill points here.

- A classifier refusal or a permission denial is final: it ends that action. Send your caller the exact command, directory and verbatim message (a coordinator sends it to the PM, the PM to the owner) and continue other work. Never retry, reword, add a pre-emption flag, or route it through another tool, agent, shell or session.
- A tool's `REFUSED:` on substance is final the same way. A refusal of the form only, whose message names the literal form, may be retried once, by a fresh agent, on the PM's word.
- A kit-guard (hook) refusal is neither: fix the form its Instead: line gives, then retry.

## Never

- Edit a record file or the store's git objects by hand.
- Write a machine path in any field: use the placeholder the repo documents (for example `%REPO_DATA%/...`).
- Invent a ruling id, or take a role or model label as an owner price or authorization.
- Pass `--reason` to fill a free slot or, as a worker, to re-run a pull the tool refused as outside the offer. `--reason` never overrides a collision with a live pull.

Details: `<kit>/docs/backlog.md` ("Commands", "The fold-in guard", "Owner gates").
