---
name: specialty-records
description: Use when your brief says to load it - writing settled facts into a repository's docs (owner decisions, landing notes, plan ticks, findings the caller already established). Docs only - you transcribe and place; you never investigate, design or decide, and never touch code, tests, tools, CI or configuration.
---

# Specialty: records

Dispatch: docs-writer (the docs type, at its own effort), never a worker type; reads the repo profile's **Branches and worktrees**, **Build and test** (the docs gate) and **Records** (the paths you must never edit), each through `--find <topic>`, and nothing else.

You transcribe and place. A fact that looks wrong or contradicts the file: say so in your report and record nothing for that item; do not reconcile it yourself. Write to `doc-writing.md` beside this file, not the file's existing voice. No profile: use `<kit>/content/default-profile.md`, with the repo's README and instructions file where they exist.

1. Inputs: what to record (settled facts or wording; owner decisions as a paraphrase plus the ruling id, with the date); where (file and section heading); a slug. Facts come as keyword lines in the entry format, not paragraphs to transcribe.
2. Find each profile topic with `python <kit>/tools/doc_index.py -C <worktree> --find <topic>` and read the one section it names. Read the target section plus only what placement needs; never a plan file end to end.
3. A worktree for docs-only work (`--find worktree`; the same worktree tool as code work unless it names a docs-only one).
4. One region of a long plan file per task; asked for two distant regions, say so and make two commits.
5. Run the profile's docs gate - except in a coordinator-announced measurement window, when you commit without it and report "gate skipped: measurement window".
6. Commit by explicit path: `python <kit>/tools/commit.py -C <worktree> -m "<subject>" <path> ...`.
7. Landing needs `python <kit>/tools/gate.py -C <worktree> --full` on the committed tip: run it now, or in a measurement window report that the full gate has not run. Then stop and report the hash. The coordinator lands it.
8. The shell is for git, the docs gate and the repo's read-only tools (`preflight.py`, `doc_index.py`, `codemap.py`): no test runs other than that final `--full`, no other project scripts.

## Never

- Edit a path the profile's **Records** section forbids, or any code, test, tool, CI, configuration or memory file.
- Invent an owner decision, or a question for the owner.
- Delete an open item (narrative, rationale and closed items may go: the commit message carries them).
- Land, merge to the main branch or push.

## Report

The hash; files and sections changed; line counts before and after per file; anything you declined to record, and why.

Details: `doc-writing.md` beside this file; skill `doc-claims-discipline`.
