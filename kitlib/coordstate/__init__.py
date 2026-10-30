"""kitlib.coordstate: a coordinator's event log and the state note rendered from it
(docs/coord-state.md).

  log      the event log `events-<session>.jsonl` in the coordinator folder: where it is, the
           bounded, fail-open append the kit tools call (note), reading and compacting it
  render   the state note: the log joined with the repository's own records (land result
           records, the verdict queue, a submitted landing's output), as text
"""
