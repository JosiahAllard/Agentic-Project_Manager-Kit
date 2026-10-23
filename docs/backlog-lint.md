# Backlog lint

Held for the joint review: no hook or settings entry runs it.

`tools/backlog_lint.py` flags records that a coordinator's `next` offers but that coordinator cannot take, so an offer is never filled with them. Logic: `kitlib/backloglint`. It reads each repo's published records (`GitReader`) and never edits one: the store its `.kit.json` names (no `.kit.json` or no store key: the hidden ref store), at the rev and folder `kitlib.hubbacklog.store_at` gets from the store itself (`open_backend(...).landed()`), read locally with no fetch. A store not in the clone is exit 2, never a clean run. Any object with `records()` returning `backloglint.Record` can stand in.

```
python tools/backlog_lint.py [--repo NAME] [--config PATH] [--offer SESSION [--cap N | --next-output FILE]]
```

Exit 0 clean, 1 findings, 2 config, read or `--next-output` error. One line per finding, worst first (priced items by priority, then unpriced), ending in the fix:

| Kind | When | Fix printed |
|---|---|---|
| `session` | a live record (open, blocked or parked) matches a `session` rule and its `session:` is missing or another | `<backlog_cmd> set <ID> --session <s>` |
| `hold` | an open record with no blocker matches a `hold` rule, or its text fields match a hold keyword | `<backlog_cmd> block <ID> --on live-client`, or `--on owner --owner-ask "<the question>"` |
| `offer` | with `--offer S`: a record among the items `next --session S` offers (`none` = untagged) that has either finding above | every fix of the record, joined by ` ; ` |

A record with an `offer` line gets no separate `session` or `hold` line: the offer line carries all its reasons.

Each fix runs as printed once the placeholders are filled in. `backlog_cmd` defaults to `python "<kit>/tools/backlog.py" -C <worktree>`, with the kit's absolute path (a repository holds no copy of the kit's tools): `backlog.py` may refuse every write verb in its main checkout, and `-C <worktree>` hands the write to that worktree's own copy (its `--help`, `-C <worktree>`). `<worktree>` is a worktree of that repository, filled in by the reader.

The exact offer comes from `--next-output FILE`, a saved printout of the store's own `next` (its `rank N  priority P  ID` lines; needs one `--repo`). The file may be UTF-8 (with or without BOM) or UTF-16 with a BOM, which is what Windows PowerShell 5.1's `>` writes. A file with no such line and no `NOTHING CAN BE PULLED` is an error (exit 2), not an empty offer. Without `--next-output` the offer is approximate: pullable records the session owns (open, priority 1-5, no blocker) in rank order (pre-empted, pinned, priority, effort, id), first `--cap`. That ignores scope collisions and branch claims; on one real store it put three records already closed on unlanded branches at the top, so prefer `--next-output`.

Hold keywords are matched only in the header fields `summary`, `requires` and `expect`, never the body (body notes such as "Live L0 note" would otherwise flag finished work). A matched phrase is printed so a false hit is easy to see.

## What only an id rule catches

In the example config the gamma and pm rules match by path and text as well as id, so a new record id is still caught: gamma by `scope:` under `artifacts/models/` or `research/gamma/`, pm by scope on `tools/new_worktree.py` or `tools/sync_base.py` or a summary saying "kit tool" or "the PM takes it". One incident record is caught only by its id:

- EX-LINK-LOOKUP-COST (hold live-client). Its text never says it needs a live client ("overhead not yet profiled"), so no keyword matches. Its scope also covers unit-tested link code, and a path rule on `worker/link/.../link/client/` would claim other records that need no client (EX-LINK-LABEL, EX-LINK-TABS on a sample store) while missing it, because its scope spans three trees.

## Config

The machine config (docs/kit-json.md "Machine config keys"), key `backlog_lint`. Example: `docs/backlog-lint.example.json`.

- `repos.<name>.root` (path, required): the repository folder.
- `repos.<name>.store` (optional): a folder to read instead of the repo's own store; absent, the store the repo's `.kit.json` names (above), so the lint has no default of its own. `ref` (default `.kit.json` `repo.main_branch`, else main or master): the branch a folder store is read on. A `.kit.json` the store cannot load, or a store not in the clone, is a read error, exit 2.
- `repos.<name>.backlog_cmd` (optional): how a printed fix starts; default `python "<kit>/tools/backlog.py" -C <worktree>` (the kit's absolute path).
- `repos.<name>.rules` (list, first match wins per kind): each has exactly one of `session` (a session name) or `hold` (`owner` or `live-client`), an optional `name`, and at least one selector:
  - `ids`: fnmatch patterns on the record id (`EX-SIM-*`);
  - `paths`: prefixes; matches when every `scope:` member starts with one of them (`artifacts/models/`), so a record that also touches other areas is not claimed;
  - `areas`: exact `area:` values;
  - `text`: case-insensitive regexes over the text fields (`\bkit tool\b`).
- `repos.<name>.keywords` (optional): `{"owner": [regex...], "live-client": [regex...]}`, case-insensitive, replacing the defaults in `kitlib/backloglint` `KEYWORDS`.
- `repos.<name>.text_fields` (optional): header fields the keywords and `text` selectors search; default `summary`, `requires`, `expect`.

A wrong type anywhere in the entry (`rules` not a list, a `session` that is not a string) is a config error, exit 2.
