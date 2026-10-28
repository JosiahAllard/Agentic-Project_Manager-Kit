# Engineering rulings

Engineering questions go from coordinators and workers to the repository's consultant (`consultant-<repo>`, content/roles/consultant.md). The consultant decides them unless they fall in an owner-only class, or the owner has asked to review that design; those go to the PM. A repository with no consultant running sends its engineering questions to the PM, who decides them the same way.

## What stays owner-only
- Anything that changes or supersedes one of the owner's own rulings.
- Realism and game feel: what a player or bot may see or do, timings, detection risk.
- Security: permissions, settings, the classifier, credentials, hooks that guard owner files.
- Money and accounts: purchases, cloud credit, which game accounts are used, bans.
- Priority and pre-empts: one owner entry per change.
- Go-live and live-client runs, and removals of owner files.
- Anything the owner has flagged for review (see below).

Everything else is engineering. Examples: API shapes, class versus record, signatures, module layout, test strategy, which of several fixes, scope splits between records.

## How an agent asks
One SendMessage to `consultant-<repo>` (ListAgents finds it):
`ENG <repo> <item>: <question> | options: a) .. b) .. | rec: <x> | why: <one line>`
The asker keeps working: an engineering question never parks an item for long.

## How the consultant handles it
1. Classify it. Owner-only, flagged for review (`eng_review`), or one that would overturn a ruling: answer "not mine: ask the PM" and send it to the PM. The PM sends owner-only and flagged ones to the hub as a question, and the item parks.
2. Otherwise decide, answer the asker, and send the call to the PM for logging (`engineering call for delegated logging`). The PM logs it with `ledger.py add --prov delegated --a "<consultant>: ..." --source "consultant-<repo>"` into its private record; no id is relayed.
3. Send it to the PM instead when unsure, when the change is hard to reverse, or when it crosses repos.

## How the owner keeps oversight
- **Review flags:** the owner names an item, area or record prefix ("review designs: EX-API-*"). The PM keeps the list in config `eng_review`. Matching questions go to the hub instead of being decided, by the consultant or the PM.
- **Daily digest:** a hub inbox note listing the day's delegated rulings, one line each, with no ids. The owner can overturn any of them; an overturn is a new ruling by the owner that supersedes it.

## Kit pieces
- **Coordinator standing line:** `kitlib.coordinatorstart.ENG_LINE` carries the filing format, the consultant as recipient, and the owner-only list. The coordinator's start adds it only when `coordinator_start.eng_filing` is `true` in the machine config; absent or any other value adds nothing.
- **`eng_review`:** a list of strings (items, areas or record prefixes) under `pm_start` in the machine config. `kitlib.pmstart.eng_review` is a checklist line at every PM start: the flags when set; no line while the key is absent or the list is empty; "could not check eng_review" when the value is anything but a list of non-empty strings.
- **Digest feed:** `python tools/ledger.py list --prov delegated --since YYYY-MM-DD [--repo REPO]` prints one `<id> | date | Decides` line per live ruling of that provenance dated on or after the day (the UTC date in the entry header), in ledger order. A tombstone is skipped; an entry with no Decides line prints `(no Decides)`. Exit codes and the ledger's location: docs/ledger.md.
