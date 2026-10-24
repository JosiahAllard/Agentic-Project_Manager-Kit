# Upkeep

Read from skill `coordinator-dispatch`, "Upkeep".

- **Prune daily.** Start each day with `python <kit>/tools/doc_index.py -C <repo> --stale`: confirm (touch it),
  rewrite or delete each line, in ONE docs-writer commit. A new `.md`, shard or section adds its
  index line in the same commit, or the doc-budget check refuses the branch.
- Durable facts -> the repo's docs, landed like any docs change, never in the host's config
  folder; session state -> your state note (`<agent temp>/<repo slug>-coordinator/state-<session>.md`). Worktrees are created and removed as the
  profile says.
- **Stale worktrees.** A B or C tree in `--classify` whose work is stale (superseded, ruled out, its
  record closed): mark it, `python <kit>/tools/worktree_remove.py -C <repo> <branch> --mark-stale
  "<why>"` (only a clean, unlanded, unclaimed tree; `--classify` offers the line for those and
  notes why the others take none). The nightly sweep then abandons it under `archive/<slug>` once it is idle 24 h, unless
  it changes first; `<branch> --unmark-stale` clears the mark. A branch with no worktree is
  abandoned by hand, `<branch> --branch --abandon "<why>"`, on the PM's word.
- **Grow the tool set.** Weekly, run the repetition report on the repo
  (`<kit>/optional/usage/repetition_report.py`): repeated shell shapes, or a scratch script
  written twice, become lines in the repo's `docs/ops/tool-requests.md`; build the top one as
  ordinary work (`tools/` is High risk).
