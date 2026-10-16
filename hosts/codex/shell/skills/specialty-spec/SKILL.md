---
name: specialty-spec
description: Use when your brief says to load it - extending a named design or spec document from answers the owner already gave, reading the code the spec depends on so it stays buildable and reporting where code and spec disagree. Docs only; no machine time beyond the docs gate.
---

# Specialty: spec writing

Dispatch: docs-writer (the docs type; the coordinator raises the model for a hard design, upward only), never a worker type; reads the repo profile's **Branches and worktrees**, **Build and test** (the docs gate) and **Records**, each through `--find <topic>`, and nothing else.

You turn settled answers into a spec someone can build from. Your value over transcription: reading the code the spec leans on and saying where the two disagree - that is how specs find real bugs. Write to `doc-writing.md` beside this file. No profile: use `<kit>/content/default-profile.md`, with the repo's README and instructions file where they exist.

1. Inputs: the document and the section you own in it; the owner's answers verbatim, each with its question label; the code the spec depends on, as paths. Read those only: no repository tour, no plan file beyond the sections named.
2. Findings first: code contradicting the spec, or an assumption in your brief, is a finding at the top of your report, with the symbol that shows it.
3. The owner's answer in the owner's words first, then what it means for the build; keep the two visibly apart.
4. Mark every inference: "the owner said X" and "I read X as implying Y" are different sentences.
5. Never invent questions for the owner. A real gap gets a proposed default, labelled a default, under "open - needs the owner", each labelled by subject, not by letter. The coordinator decides what reaches the owner.
6. Break the work into independently landable tasks: size (S/M/L), the gate or test tier it reaches (`--find gate`, `--find tiers`), what it waits for.
7. Mechanics as any docs change: a docs-only worktree (`--find worktree`), edit, the docs gate (skipped only in a coordinator-announced measurement window, reported as "gate skipped: measurement window"), commit by explicit path with `python <kit>/tools/commit.py -C <worktree> -m "<subject>" <path> ...`, report the hash. The coordinator lands it.
8. The shell is for git, the docs gate and the repo's read-only tools (`preflight.py`, `doc_index.py`, `codemap.py`). After two or three rounds on one document, tell the caller your context is getting long and offer a summary to seed a fresh agent.

## Never

- Touch code, tests, tools, CI or configuration.
- Decide an open question on the owner's behalf.
- Run anything beyond the docs gate and the read-only repo tools.
- Land, merge to the main branch or push.

## Report

Code findings first. Then the hash, the sections written, the tasks added with sizes, and the open items with your proposed defaults.

Details: `doc-writing.md` beside this file; skill `doc-claims-discipline`.
