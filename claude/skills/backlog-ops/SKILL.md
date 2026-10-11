---
name: backlog-ops
description: Use when you need to read or change a repo's backlog store with tools/backlog.py - next, show, add, pull, close, block, set (including narrowing a record's scope). Gives the forms that pass the auto-mode classifier and what to do when one is refused.
---

# Backlog operations

The store is read and written only through `python tools/backlog.py` (`${CLAUDE_PLUGIN_ROOT}/docs/backlog.md`; the store's place comes from the repo's `.kit.json` `"backlog".store`). It is the only tracker. `python tools/backlog.py` with no arguments prints usage. Never edit a record file by hand.

## Rules for every call

- Bare command, one per call, naming your worktree: `python tools/backlog.py -C <worktree> <verb> ...` (works from any cwd; your shell may start in the main checkout every call). No `cd`, no pipes, no chaining, no Set-Location or Start-Process wrapping.
- With the `main` store the write verbs stage the record in the worktree and refuse the main checkout; with the `ref` or `branch` store a write lands at once on the store ref and never touches the checkout. Which one the repo uses decides whether the record rides in your commit; check `.kit.json` and the repo profile.
- Machine paths in any field: use the placeholder the repo documents (for example `%REPO_DATA%/...`), never a drive path.
- Rulings: cite by the ledger id the owner gave you (`--ruling R-nnnn`). Never invent one. Where a ledger is configured, `check` verifies it.

## Steps

1. Read: `next` (the offer, ranked, for `--session`), `next --all`-style listing with `list`, `show <ID>`, `list --blocked`, `list --needs-priority`, `stale [--days N]` (records untouched N+ days, dead body paths, moved `files`), `check` (run before you commit).
2. Add a record: `python tools/backlog.py add <ID> --summary "<line>" --scope <path> [--scope <path>] --found-on <branch> --why-not-folded "<reason>"` (`--found-on none` when no branch found it). Always unpriced; there is no priority flag on `add`. Scope = the real files it will touch. A finding whose scope overlaps the finding branch's diff or its pulled records is REFUSED: fold it into that branch, or file it after that branch lands (`${CLAUDE_PLUGIN_ROOT}/docs/backlog.md` "The fold-in guard").
3. Pull what `next` offers, before work starts: `python tools/backlog.py -C <worktree> pull <ID>`; a session-owned item takes its session, `python tools/backlog.py -C <worktree> --session <key> pull <ID>` (a corrected retry that only adds `--session` needs no `--reason`). An unpriced record refuses: pull only once its price is on the store you read, or set the same agent price in your worktree first when your brief says to. An item outside the offer is a pre-emption: `--reason "<why>"`; a true written reason is enough where the repo says so, otherwise it needs `--ruling`. Never pass it to fill a free slot or to re-run a pull the tool just refused; that reads as bypassing a gate. A `priority: -1` item needs the owner's ruling.
4. Narrow an over-broad scope instead of overriding a collision: `python tools/backlog.py set <ID> --scope <path> [--scope <path>]` (repeat the flag per member; confirm with `show <ID>`).
5. Close when the work is done, in the same commit as the work or as the repo's profile says: `python tools/backlog.py -C <worktree> close <ID> --reason "<what was done>"`. Owner-held records need `--ruling`. A record that should not be done: `retire <ID> --why "<why>"` (owner-held or owner-priced: add `--ruling R-nnnn`, a ledger entry whose `Items:` line names it; `--owner-said` is refused). Give a pull back unfinished with `release <ID> --reason "<why>"`.
6. Block: `block <ID> --on <member> [--on <member>] [--owner-ask "<question>"]`; members include `owner` and `item:<ID>`; `unblock <ID>` clears a lone blocker; with several, `unblock <ID> --on <member> --reason "<why>"` clears that one only (clearing `owner` needs `--ruling`).
7. Price and other edits: `set <ID> --priority <1-5> --priority-by <who>` (an owner price takes `--ruling`); other fields `set <ID> --<field> <value>`; body text `set <ID> --body-file <path>`. Park a record on an owner question with `set-status <ID> parked --q <Q-id>`.
8. `python tools/backlog.py check` exits 0 before you commit.

## Exit codes and refusals

Exit 0 OK; 1 `NOT FOUND:`, `ERROR:` or a RED check; 2 `USAGE:`, `REFUSED:` or bad arguments. `USAGE:`, `NOT FOUND:` and `ERROR:` are your own mistake or a missing record: fix the command once; that is not a reissue of a refusal. Only `REFUSED:` is a policy guard.

A real refusal (`REFUSED:` that is not your mistake), a classifier denial or a hook denial: stop that line. Do not retry, reword, add a pre-emption flag, or use another shell. Send your coordinator the exact command, the working directory and the verbatim refusal. Park the work; continue other lines. Re-review any branch tip that moved after its review before it lands.
