# Doc writing

Shared by the specialty-records and specialty-spec skills; the same bytes in each folder.

- **State, not story.** Living docs (`docs/ops/` registries, decision-log shards, open-item lists)
  hold open items only. Closing an item = deleting its line.
- The backlog store (`docs/backlog/`) is the only progress tracker and is never hand-edited:
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
