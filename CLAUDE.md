# PM kit

- Contract: ARCHITECTURE.md.
- Python standard library only. Windows only.
- `kitlib/<pkg>/`: logic, with no argparse, no printing to the user and no sys.exit. `tools/`: thin entry points. `hooks/`: guard.py and pre-push. Tests go in `tests/<pkg>/`, written as unittest tests. They import the library and never the CLIs, except to test arguments and exit codes.
- Work on a branch in a worktree under `.claude/worktrees/<slug>`, never in the main checkout. The PM lands.
- Tools outside this repo (a repository's own tools, an older kit) are reference only. Never edit them.
- Heavy runs (the full test suite) go through python tools/gate.py --full; it takes the machine lock itself.
