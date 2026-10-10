# sync_base

`tools/sync_base.py [-C DIR] <hash> --model "<model name>" [--base BRANCH]` merges exactly the base commit the coordinator named into the branch checked out at `-C` (default `.`). Logic: `kitlib/syncbase`; the module docstring holds the full rules. Ported from the PM repo's `kit/tools/sync_base.py` at 2d390a8.

- Never a rebase; the merge commit, or git's prepared MERGE_MSG on a conflict, ends with `Co-Authored-By: Claude <model> <noreply@anthropic.com>`. `--model` falls back to `CLAUDE_AGENT_MODEL`.
- Fetches `<remote> <base>` first (`.kit.json` "repo".remote and .main_branch), then refuses a hash that is not on the fetched `refs/remotes/<remote>/<base>` (refusal-log P27). The refusal names the hash from `git ls-remote <remote> <base>`, never local main.
- The main checkout is refused (`kitlib.git.checkout.main_refusal`), as is a dirty tree.
- Doc conflicts get the per-line audit from `syncbase.conflict_advice()`; a clean merge is checked for regrowth (text the base deleted that came back).
- Exit codes are in the `tools/sync_base.py` docstring: 0 merged, 1 refused or a finding, 2 usage, configuration or the main checkout.

Not ported: the `-C` hand-off to a repo-local copy, `KIT_CALLER_CWD`, `_repo_guard.check_same_repo`, and the `KIT_REPO_ROOT`/`KIT_CONFIG` overrides. Tests: `tests/syncbase`.
