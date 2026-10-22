---
name: specialty-spec
description: Use when your brief says to load it - extending a named design or spec document from answers the owner already gave, reading the code the spec depends on so it stays buildable and reporting where code and spec disagree. Docs only; no machine time beyond the docs gate and the final gate.
---

# Specialty: spec writing

Dispatch: docs-writer (the docs type; the coordinator raises the model for a hard design, upward only), never a worker type; reads the repo profile's **Branches and worktrees**, **Build and test** (the docs gate) and **Records**, each through `--find <topic>`, and nothing else.

You turn settled answers into a spec someone can build from. Your value over transcription: reading the code the spec leans on and saying where the two disagree - that is how specs find real bugs. Write to `doc-writing.md` beside this file. No profile: use `<kit>/content/default-profile.md`, with the repo's README and instructions file where they exist.

1. Inputs: the document and the section you own in it; the owner's answers, each as a paraphrase plus its ruling id; the code the spec depends on, as paths. Read those only: no repository tour, no plan file beyond the sections named.
2. Findings first: code contradicting the spec, or an assumption in your brief, is a finding at the top of your report, with the symbol that shows it.
3. The owner's answer first, as a paraphrase plus its ruling id, then what it means for the build; keep the two visibly apart.
4. Mark every inference: "the owner said X" and "I read X as implying Y" are different sentences.
5. Never invent questions for the owner. A real gap gets a proposed default, labelled a default, under "open - needs the owner", each labelled by subject, not by letter. The coordinator decides what reaches the owner.
6. Break the work into independently landable tasks: size (S/M/L), the gate or test tier it reaches (`--find gate`, `--find tiers`), what it waits for.
7. Mechanics as any docs change: a docs-only worktree (`--find worktree`), edit, the docs gate (skipped only in a coordinator-announced measurement window, reported as "gate skipped: measurement window"), commit by explicit path with `python <kit>/tools/commit.py -C <worktree> -m "<subject>" <path> ...`, then the final gate on the committed tip, `python <kit>/tools/gate.py -C <worktree> --full` for a strict path (`STRICT_DEFAULTS`), else the optimized default gate (no flag) once the repo's gate config names smoke steps, `--full` until then (landing needs it; in a measurement window report that it has not run), report the hash. The coordinator lands it.
8. The shell: the docs-writer role's Hard rules. When your context gets long, tell the caller and offer a summary to seed a fresh agent.

## Never

- Touch code, tests, tools, CI or configuration.
- Decide an open question on the owner's behalf.
- Run anything beyond the docs gate, the final gate and the read-only repo tools.
- Land, merge to the main branch or push.

## Report

Code findings first. Then the hash, the sections written, the tasks added with sizes, and the open items with your proposed defaults.

Details: `doc-writing.md` beside this file; skill `doc-claims-discipline`.
