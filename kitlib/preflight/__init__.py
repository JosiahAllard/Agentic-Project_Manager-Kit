"""kitlib.preflight: the start-of-task picture tools/preflight.py prints (docs/preflight.md).

  settings  `.kit.json` "preflight" checked past its schema, and the base branch pick
  machine   the engines and UDP ports lines (tasklist, netstat)
  board     `--board`: a row per worktree against the base, and the stale-doc line
  state     `--board --write-state`: the one marked block of the coordinator's state file
  hooks    a repo's report hooks, "module:function" in its tools dir
  nextblock the `next` block: the kit commands this session runs, its values filled in
  lockline  the machine lock line, from the kit lock's `status --json` snapshot
  report    every reading in order, as lines
"""
