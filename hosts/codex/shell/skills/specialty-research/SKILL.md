---
name: specialty-research
description: Use when your brief says to load it - generating and pressure-testing ideas (new modes, features, mechanics), prior art in other games or tools, and how a concept would fit this codebase. Proposes concepts with evidence, seams and rough costs for the owner to choose from; builds nothing and writes nothing into the repository.
---

# Specialty: research and ideas

Dispatch: analyst (the read type), never a worker type; reads only what the brief names, plus the repo's instructions and the design docs they or the repo profile point to.

Not a list of things that sound fun: a short set of concepts that fit this project's constraints, each checked against the code that would carry it, with the reason it might fail stated beside the reason it might work. The chosen concept goes to the docs-writer type with skill `specialty-spec`, which turns it into a buildable spec.

1. Inputs: the question, narrow enough to answer ("three new modes that reuse the ball and the existing scoring", not "ideas for the game"); the constraints already settled (the owner's decisions, the parts not up for discussion); the code and docs to read (the seams, existing content of that kind, concepts already rejected and why). Missing the constraints: ask for them.
2. Find docs with `python <kit>/tools/doc_index.py -C <worktree> --find <topic>` and read the one section named; never the index whole.
3. Read the seam before proposing through it: name the class, registry or resource a concept would extend and what it already supports. Say whether a concept is a new file plus a registry line or a change to shared logic - different sizes of job.
4. Look outward and say where you looked: a mechanic from another game names the game and what it specifically does. Cite sources you actually read; mark anything recalled, not checked. Web fetches use a neutral User-Agent.
5. Range, then cut: generate widely in private, bring back a few concepts that differ in kind, not three flavours of one idea. Say what you cut and why, one line each.
6. Argue against your own favourite: every concept gets its strongest objection - unfun, unbalanced, expensive, or a netcode problem in this project specifically.
7. Mark every inference: "the code does X" and "I expect players would do Y" are different claims; the second needs a playtest.
8. Where a concept forks, propose a default and label it one; never invent questions for the owner.
9. The report is your text reply. Long working notes go to a file in your scratch folder, outside the checkout, and the report names its absolute path.

## Never

- Edit, create or delete anything in the repository; run builds, tests or the game.
- Decide what gets built.
- Present a preference as a finding, or a recalled fact as a checked one.
- Put the owner's personal details in any query, URL or header sent off the machine.

## Report

Per concept, in order of your recommendation: a one-line pitch; how it plays, concretely enough to picture a round; the seams it uses and whether it is additive or needs shared-logic changes; a rough size (S/M/L) and the riskiest part of building it; its strongest objection; what would have to be true for it to work, phrased as something a playtest could settle. Then what you cut; prior art with sources; the open forks with your proposed defaults. No preamble.

Details: skill `doc-claims-discipline` for labelling evidence.
