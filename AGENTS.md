# PM kit

The one rules file for every agent host; `CLAUDE.md` imports it.

- Contract: ARCHITECTURE.md.
- Python standard library only. Windows only.
- `kitlib/<pkg>/`: logic, with no argparse, no printing to the user and no sys.exit. `tools/`: thin entry points. `hosts/<name>/`: everything specific to one AI host, from `hosts.json`: `adapter/` (Python) and `shell/` (hook manifests, rendered agents and skills). `boot/hook.py`: the one hook entry of every host (`<host> <event>`). `boot/kitboot.py`: the check every host entry runs before it imports kitlib. `kit.json`: the kit's one version. `githooks/`: pre-push, and the opt-in pre-commit. `content/`: every role and skill, written once, and `default-profile.md`, the agent profile roles read in a repo with none; `python tools/render_content.py --write` renders them into each host's agents and manifests (and Codex's skills copy; Claude loads `content/skills/` itself), which are never edited by hand; `tools/commit.py` renders on commit (docs/content.md). Tests go in `tests/<pkg>/`, written as unittest tests. They import the library and never the CLIs, except to test arguments and exit codes.
- Work on a branch in a worktree under `.claude/worktrees/<slug>`, never in the main checkout. The PM lands. The repo profile for worker agents is `docs/agent-profile.md`; each host's own-folder stub (`.claude/agent-profile.md`, `.codex/agent-profile.md`) points to it.
- Tools outside this repo (a repository's own tools, an older kit) are reference only. Never edit them.
- A kit coordinator's state note lives outside the repo, in the kit-coordinator subfolder of the machine's agent temp folder (`state-<session>.md`): an untracked file in the main checkout makes `gate.py land` refuse. `docs/ops/*.local.md` is git-ignored as a backstop.
- Heavy runs (the full test suite) go through `python tools/gate.py --full`; it takes the machine lock itself. From another directory: `python <worktree>/tools/gate.py -C <worktree> --full`, both absolute (docs/gate.md "The caller's checkout").
- Never put the owner's personal details (email address, name, account names) in anything sent off this machine.
- Never chain a write (commit, merge, reset, backlog.py write, landing command) with other commands in one shell call.
- A safety-check or hook refusal is final: stop at the first, report it verbatim; no retry, rewording, other shell or agent.
