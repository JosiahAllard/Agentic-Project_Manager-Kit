# sync_base

`tools/sync_base.py [-C DIR] <hash> --model "<model name>" [--host NAME] [--base BRANCH] [--no-wait]` merges exactly the base commit the coordinator named into the branch checked out at `-C` (default `.`). Logic: `kitlib/syncbase`; the module docstring holds the full rules.
`--host NAME` names the agent host for the Co-Authored-By trailer: a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude). `--provider` is its old name, accepted for one release. A name `hosts.json` lacks is a usage error (exit 2) before any work. The trailer comes from `kitlib.attribution` (docs/commit.md): a Codex one needs the actual explicit model id, is `Codex <model-id> <noreply@openai.com>` on the clean and the prepared conflict message, and never reads CLAUDE_AGENT_MODEL. The host and model are checked before anything changes; the pushed-base, dirty-tree, conflict and regrowth checks keep their rules.

- Never a rebase; the merge commit, or git's prepared MERGE_MSG on a conflict, ends with `Co-Authored-By: Claude <model> <noreply@anthropic.com>`. `--model` falls back to `CLAUDE_AGENT_MODEL`.
- Fetches `<remote> <base>` first (`.kit.json` "repo".remote and .main_branch), then refuses a hash that is not on the fetched `refs/remotes/<remote>/<base>` (refusal-log P27). The refusal names the hash from `git ls-remote <remote> <base>`, never local main.
- With `.kit.json` "landing".`in_flight` set (default off), before the fetch it waits for a landing in flight as `tools/new_worktree.py` does ([new-worktree.md](new-worktree.md), "Waiting for a landing in flight"); `--no-wait` says so and goes on. It still merges exactly `<hash>`; when the fetched `<remote>/<base>` is past it, one line says so: `note      origin/main is at <tip>, past <hash>: ask the coordinator whether to sync to it`.
- The main checkout is refused (`kitlib.git.checkout.main_refusal`), as is a dirty tree.
- Doc conflicts get the per-line audit from `syncbase.conflict_advice()`; a clean merge is checked for regrowth (text the base deleted that came back).
- Exit codes are in the `tools/sync_base.py` docstring: 0 merged, 1 refused or a finding, 2 usage, configuration or the main checkout, 130 Ctrl+C.

Not ported: the `-C` hand-off to a repo-local copy, `KIT_CALLER_CWD`, `_repo_guard.check_same_repo`, and the `KIT_REPO_ROOT`/`KIT_CONFIG` overrides. Tests: `tests/syncbase`.
