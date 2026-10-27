# Doc writing

- **State, not story.** Living docs (`docs/ops/` registries, decision-log shards, open-item lists)
  hold open items only. Closing an item = deleting its line.
- The backlog store (<kit>/docs/backlog.md) is the only progress tracker and is never hand-edited:
  setting or closing a record is the kit's `backlog.py` tool, not a docs commit. There is no
  `STATUS.md` or `ROADMAP.md` to also update, and no rename log or per-story changelog to keep.
- Entry format: `ID | status | area | keywords | ref (commit, file:line or doc section)`, 300
  characters at most. Keywords over sentences.
- No rationale, evidence, dates of progress or "previously / corrected": the commit message is the
  only history. Exception: one sentence of rationale that prevents a specific silent failure.
- Never restate another doc or the repo's instructions file; point to it.
- Cite symbols, not line numbers, and sections by title. A sentence about code names the artefact
  and sources every figure; grep before re-pointing a reference; a sentence the code contradicts
  goes in your report. Mark anything inferred `(inferred)`.
- The doc-budget check gates landing. Red: prune first -
  delete entries superseded or already absorbed into an instruction rule, a code-side constraint
  or a closed backlog record, naming in the commit body where each now lives. Nothing prunable:
  stop and report rather than add. Decision logs count like any other doc.
- Durable facts go to the repo's `docs/ops/*.md` (flakes, ports, risk map, test tiers, machine,
  assets) or its docs, never into your host's user folder.
- The repo's instructions file and the owner-only paths (the profile's **Records** section) are
  never yours to edit.

## Findings and doc edits

1. Write the evidence next to the claim and let observed / measured / inferred / untested read off it; do not pick the label first. A claim that names its evidence ("one timestamp, nothing to compare against") cannot be upgraded in a retelling without deleting the evidence that contradicts it.
2. A finding that holds has a control, one changed variable, and a prediction written down before the run. A number carries its comparison, sample size and conditions.
3. A finding that falls is withdrawn in the file that claims it, in the same change: a claim left filed gets repeated as fact, even by whoever withdrew it.
4. Before deleting or trimming a bullet, list every rule in it; say in the change what the deletion removed, not just what it fixed (trimming for a length budget can drop an authorisation's conditions).
5. Adding a capability: write the user-facing doc FIRST and check each sentence against the member it describes; state the feature's limits beside it.
6. Then find every doc that asserted its absence: grep the CONCEPT (every file naming the feature) and read the hits, never only the phrases you just edited (an unlock command once left several "no unlock command" claims behind).
7. Keep kinds of evidence apart: a passing unit or adapter test, a metadata check and observed behaviour in a real install are different claims; never collapse them into "it works".

Details: the repo's doc rules (`python <kit>/tools/doc_index.py -C <worktree> --find docs`).
