---
name: ledger-ops
description: Use as the PM when you record an owner ruling with <kit>/tools/ledger.py add, especially one that supersedes an earlier entry. Gives the one bare call per entry and the --supersedes rules.
---

# Ledger operations

PM only. The format and every verb are in `<kit>/docs/ledger.md`; this is the form that passes the guard.

1. One entry is one bare call, one per shell call: `python <kit>/tools/ledger.py add --repo <repo> --prov <typed|selected|relayed-from-terminal|delegated> --q "<question>" --a "<answer>" --decides "<one line>" --source "<where given>" [--supersedes <id>]`. No heredoc, no script, no `cd`, pipe or chaining, and no second `add` in the same call; the call prints the new id. There is no file flag: long text stays inside the quoted values, and the whole answer goes in `--a`. A Decides over 400 characters draws a warning; shorten it.
2. Do not guess an id. Read it from the `add` output; look one up with `python <kit>/tools/ledger.py grep "<text>"` or `show <id>`.
3. `--supersedes <id>` names an earlier entry of the same repo file. With `ledger_dir` set it may also name an `R-nnnn` entry of the frozen `ledger_path` file (read, never written). An id found in neither, or not earlier than the new entry, is `REFUSED: the new entry is invalid: ... supersedes <id>, which is not an earlier entry ...` (exit 1, nothing written). Fix the id once from `grep` or `show`; if the entry is in another repo's file, record the new entry in that repo's file (`--repo`), or leave `--supersedes` off and say so in Decides.
4. Superseding does not change the old entry: the ledger is append-only and the frozen file is never written. `retire <id> --class superseded --why "<why>" --by <new id>` turns an entry in a repo file into its tombstone, and `retire R-nnnn` is refused.
5. `--repo all` is refused when `ledger_dir` is set: a ruling for every repository goes in `--repo pm`. Provenance is the owner's own words (`typed`, `selected`, `relayed-from-terminal`); `delegated` needs Source `under <id>` or `under hub:A-nnn`.
6. A `REFUSED:` is final on substance (skill backlog-ops "Refusals"); a usage error (exit 2) is yours: fix the command once.
