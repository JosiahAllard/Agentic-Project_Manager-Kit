# Agentic-Project_Manager-Kit

- Contract: ARCHITECTURE.md. Rulings are cited as PM R-nnnn (the PM ledger).
- Python standard library only. Windows only.
- `kitlib/<pkg>/`: logic, with no argparse, no printing to the user and no sys.exit. `tools/`: thin entry points. `claude/`: everything Claude Code specific (agents, skills, the guard and session-start hooks, the Action Hub page). `githooks/`: pre-push, and the opt-in pre-commit. Tests go in `tests/<pkg>/`, written as unittest tests. They import the library and never the CLIs, except to test arguments and exit codes.
- Work on a branch in a worktree under `.claude/worktrees/<slug>`, never in the main checkout. The PM lands.
- Tools outside this repo (a repository's own tools, an older kit) are reference only. Never edit them.
- Heavy runs (the full test suite) go through `python ~/.claude/tools/machine_lock/machine_lock.py run --repo Agentic-Project_Manager-Kit --slug <slug> --lock --kind engine-test -- <cmd>`.
