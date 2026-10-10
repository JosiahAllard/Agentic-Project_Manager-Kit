"""kitlib.preflight: the start-of-task picture tools/preflight.py prints (docs/preflight.md).

  settings  `.kit.json` "preflight" checked past its schema, and the base branch pick
  machine   the engines and UDP ports lines (tasklist, netstat), shared with kitlib.board
  hooks     a repo's report hooks, "module:function" in its tools dir
  nextblock the `next` block: the kit commands this session runs, its values filled in
  lockline  the machine lock line, from the kit lock's `status --json` snapshot
  report    every reading in order, as lines
"""
