---
name: specialty-research
description: Use when your brief says to load it - investigating code, infrastructure or an audit question, how a change would fit this codebase, prior art in other tools, or (one case among them) game concepts. Proposes options with evidence, seams and rough costs for the owner to choose from; builds nothing and writes nothing into the repository.
---

# Specialty: research and investigation

Dispatch: analyst (the read type), never a worker type; reads only what the brief names, plus the repo's instructions and the docs they or the repo profile point to.

Not a tour of what sounds plausible: a short answer, or a few options that fit this project's constraints, each checked against the code, config or logs that would carry it, with the reason it might fail beside the reason it might work. A chosen option goes to a worker, or to docs-writer with skill `specialty-docs`.

1. Inputs: the question, narrow enough to answer ("why does the gate re-run unchanged steps", "which callers break if this signature changes", "three new modes that reuse the ball and the existing scoring", not "look at the gate"); the settled constraints; what to read, and options already rejected. Missing the constraints: ask your caller.
2. Find docs with `python <kit>/tools/doc_index.py -C <worktree> --find <topic>` and read the one section named; never the index whole.
3. Read the seam before proposing through it: name the function, tool, config key or registry an option touches and what it already does. Trace callers and failure paths, not only the happy path. Say whether it is additive or changes shared logic.
4. Audits: state the claim, the check that would falsify it, the command or path:line you used, and the result. A count names what was counted and what was excluded.
5. Prior art: name the tool or game and what it does; cite only sources you read. Range, then cut: a few options that differ in kind, not three flavours of one idea; say what you cut and why, one line each.
6. Argue against your favourite: each option gets its strongest objection - wrong for this codebase, regression risk, cost, an ordering or netcode problem, or (a game concept) unfun or unbalanced.
7. Label evidence (`../specialty-docs/doc-writing.md`, "Findings and doc edits"): "the code does X" (cited) and "I expect Y" (a playtest or run settles it) differ; a recalled fact is marked recalled; a preference is never a finding.
8. Where an option forks, propose a default and label it one; never invent questions for the owner.
9. The report is your text reply. Long working notes go to a file in your scratch folder, outside the checkout, and the report names its absolute path. A page for the owner: skill `hub-ops`, step 4.

## Never

- Edit, create or delete anything in the repository; run builds, tests or the game.
- Decide what gets built.
- Put the owner's personal details in any query, URL or header sent off the machine; web fetches use a neutral User-Agent.

## Report

Lead with the answer. Per option, in order of your recommendation: a one-line pitch; what it changes, concretely; the seams and whether it is additive or needs shared-logic changes; size (S/M/L) and the riskiest part; its strongest objection; what would have to be true for it to work, phrased as something a test, run or playtest could settle. Then what you cut; prior art with sources; open forks with proposed defaults. No preamble.
