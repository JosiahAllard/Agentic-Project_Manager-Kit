# Doc index

Every tracked `.md` file is named in backticks on an entry line of this file; `tools/doc_index.py`
(run by preflight, commit and review_pack) fails when one is not. Find by topic with
`python tools/doc_index.py --find <topic>` (`--tools` for the Tools section) and read one section with
`--section "<title>"`. Add a new doc's line in the same commit that adds the doc.

## Docs

- `docs/INDEX.md` | this index | adding or finding a doc | kw: index, docs
- `CLAUDE.md` | kit repo rules for agents | always | kw: rules, claude
- `AGENTS.md` | the same rules for other agent hosts | always | kw: rules, agents
- `README.md` | what the kit is | first look | kw: readme, overview
- `ARCHITECTURE.md` | kit tool architecture (the contract; owner approves changes) | before changing a tool | kw: architecture, contract
- `.claude/agent-profile.md` | this repo's profile for worker agents | starting a task | kw: profile, branches, build, test
- `docs/ROSTER.md` | agent roster | choosing an agent | kw: roster, agents
- `docs/action-hub.md` | Action Hub | the PM action list | kw: action-hub, hub
- `docs/backlog.md` | backlog.py | reading or changing backlog records | kw: backlog
- `docs/backlog-lint.md` | backlog lint | checking backlog records | kw: backlog-lint, lint
- `docs/backlog-store.md` | backlog store design | changing the store | kw: backlog-store, store
- `docs/branch-overlap.md` | branch overlap: unmerged branches touching a file set | checking a sweep's exclusions | kw: branch_overlap, overlap
- `docs/commit.md` | commit.py and machine_paths.py | committing | kw: commit, machine_paths
- `docs/daily-full.md` | daily_full | the nightly full run | kw: daily-full, nightly
- `docs/doc-tools.md` | doc_budget, doc_index, codemap | finding or checking docs | kw: doc-tools, doc_index, doc_budget, codemap, find, section, stale
- `docs/engineering-rulings.md` | engineering rulings | before a design choice | kw: rulings, engineering
- `docs/gate.md` | gate.py | running the gate | kw: gate, test
- `docs/go-live.md` | go-live list | switching a repo to the kit | kw: go-live
- `docs/guard.md` | shell guard and git hooks | a command is refused | kw: guard, hooks, refusal
- `docs/install.md` | installing the kit in a repository | setting up a repo | kw: install, installer, junctions, plugin
- `docs/kit-json.md` | `.kit.json` keys, types and defaults | configuring | kw: kit-json, config
- `docs/landed.md` | landed: has a branch landed | checking a branch | kw: landed
- `docs/launcher.md` | launcher | starting sessions | kw: launcher, launch
- `docs/ledger.md` | the rulings ledger | recording a ruling | kw: ledger, rulings
- `docs/lock-api.md` | machine lock API | heavy runs | kw: lock, machine-lock
- `docs/lock-mcp.md` | lock MCP server | lock from an agent | kw: lock-mcp, mcp
- `docs/lock-release.md` | lock release pre-check | releasing a lock | kw: lock-release
- `docs/machine-setup.md` | machine setup | a new machine | kw: machine-setup, setup
- `docs/model-ab.md` | model A/B results | choosing a model | kw: model, ab
- `docs/agent-review.md` | agent review: usage, refusals, definition findings (undecided) | choosing a model, editing agent definitions | kw: agent-review, agents, model
- `docs/new-worktree.md` | new_worktree | starting a task | kw: new-worktree, worktree
- `docs/pending.md` | the pending-steps file | pending steps | kw: pending
- `docs/pm-data-branch.md` | PM data on a private branch of the kit repo | PM data | kw: pm-data, data-branch
- `docs/pm-first-run.md` | PM first run | the first PM session | kw: pm-first-run
- `docs/plugin-source.md` | the plugin's source: export of pushed kit main, refresh, migrate | installing or refreshing the plugin | kw: plugin-source, plugin, plugin_refresh, export
- `docs/pm-start.md` | PM session-start checklist | starting a PM session | kw: pm-start, checklist
- `docs/pm-watch.md` | PM watcher | watching sessions | kw: pm-watch, watcher
- `docs/preflight.md` | preflight, board and checkout guard | starting any task | kw: preflight, board, checkout
- `docs/redfirst.md` | redfirst | proving a test fails first | kw: redfirst, red
- `docs/repo-checks.md` | forbidden, trailers, pieces | repo checks | kw: forbidden, trailers, pieces
- `docs/review-pack.md` | review_pack | preparing a review | kw: review-pack, review
- `docs/shipping.md` | what ships | changing the shipped set | kw: shipping, ships
- `docs/sync-base.md` | sync_base | main moved | kw: sync-base, merge
- `docs/test-tools.md` | testlist, tool_deps, pytests | test selection | kw: testlist, tool_deps, pytests, tests
- `docs/windows-token-leak.md` | Windows token leak: slow process launches | slow launches | kw: windows, token-leak
- `docs/worktree-paths.md` | worktree paths | where worktrees live | kw: worktree-paths
- `docs/worktree-remove.md` | worktree remove | removing a worktree | kw: worktree-remove, sweep
- `claude/agents/*.md` | one agent definition per file | editing an agent | kw: agents, agent-definition
- `claude/skills/*/*.md` | one skill per folder | editing a skill | kw: skills, skill
- `templates/stacks/godot/.claude/skills/godot-render-check/SKILL.md` | Godot render check and e2e tiers | Godot repos | kw: godot, render, e2e
- `templates/common/CLAUDE.md` | starter CLAUDE.md for a new repo | installing | kw: template, claude
- `templates/common/INDEX.md` | starter doc index for a new repo | installing | kw: template, index
- `templates/common/LEDGER.md` | starter rulings ledger | installing | kw: template, ledger
- `templates/common/agent-profile.TEMPLATE.md` | starter agent profile | installing | kw: template, profile
- `templates/common/coordinator-profile.TEMPLATE.md` | starter coordinator profile | installing | kw: template, coordinator
- `knowledge/asset-generation/README.md` | asset generation knowledge base | generating assets | kw: asset-generation, knowledge
- `knowledge/asset-generation/BEST_PRACTICES.md` | best practices | generating assets | kw: best-practices, assets
- `knowledge/asset-generation/MODELS.md` | approved models | picking a model | kw: models, assets
- `tests/backloglint/fixtures/*.md` | backlog lint test fixtures | editing backlog_lint tests | kw: fixtures, backlog-lint

## Tools

- `tools/preflight.py` | board and checkout guard (`docs/preflight.md`) | kw: preflight, board
- `tools/new_worktree.py` | start a task worktree (`docs/new-worktree.md`) | kw: new_worktree, worktree
- `tools/commit.py` | commit by explicit path (`docs/commit.md`) | kw: commit
- `tools/gate.py` | run the gate (`docs/gate.md`) | kw: gate, test
- `tools/redfirst.py` | prove a new test fails at base (`docs/redfirst.md`) | kw: redfirst
- `tools/sync_base.py` | merge a named base commit (`docs/sync-base.md`) | kw: sync_base, merge
- `tools/doc_index.py` | find or check docs (`docs/doc-tools.md`) | kw: doc_index, find, section
- `tools/doc_budget.py` | doc size budgets (`docs/doc-tools.md`) | kw: doc_budget
- `tools/codemap.py` | where code lives (`docs/doc-tools.md`) | kw: codemap
- `tools/backlog.py` | backlog store (`docs/backlog.md`) | kw: backlog
- `tools/backlog_lint.py` | backlog lint (`docs/backlog-lint.md`) | kw: backlog_lint
- `tools/review_pack.py` | review pack (`docs/review-pack.md`) | kw: review_pack
- `tools/land.py` | land a reviewed branch (`docs/shipping.md`) | kw: land
- `tools/landed.py` | has a branch landed (`docs/landed.md`) | kw: landed
- `tools/branch_overlap.py` | unmerged branches that change files in a set (`docs/branch-overlap.md`) | kw: branch_overlap, overlap
- `tools/machine_lock.py` | machine lock (`docs/lock-api.md`) | kw: machine_lock, lock
- `tools/lock_release_check.py` | lock release pre-check (`docs/lock-release.md`) | kw: lock_release_check
- `tools/worktree_remove.py` | remove a worktree (`docs/worktree-remove.md`) | kw: worktree_remove
- `tools/ledger.py` | rulings ledger (`docs/ledger.md`) | kw: ledger
- `tools/pending.py` | pending steps (`docs/pending.md`) | kw: pending
- `tools/launch.py` | launcher (`docs/launcher.md`) | kw: launch, launcher
- `tools/pm_watch.py` | PM watcher (`docs/pm-watch.md`) | kw: pm_watch
- `tools/daily_full.py` | nightly full run (`docs/daily-full.md`) | kw: daily_full
- `tools/testlist.py` | test name lists (`docs/test-tools.md`) | kw: testlist
- `tools/tool_deps.py` | tests a changed file selects (`docs/test-tools.md`) | kw: tool_deps
- `tools/pytests.py` | test runner (`docs/test-tools.md`) | kw: pytests
- `tools/forbidden.py` | forbidden-text check (`docs/repo-checks.md`) | kw: forbidden
- `tools/agent_roster.py` | docs/ROSTER.md against agent file model and effort (`docs/ROSTER.md`) | kw: agent_roster, roster
- `tools/board.py` | the coordinator's whole board in one call (`docs/preflight.md`) | kw: board
- `tools/checkout_guard.py` | is a path in the main checkout; strays in it (`docs/preflight.md`) | kw: checkout_guard, stray
- `tools/kit_stubs.py` | write or check a repo's stubs for the kit tools (`docs/install.md`) | kw: kit_stubs, stubs
- `tools/kit_tests.py` | run the kit test suite, whole or selected (`docs/test-tools.md`) | kw: kit_tests, tests
- `tools/ledger_refs.py` | where each ruling id is cited (`docs/ledger.md`) | kw: ledger_refs
- `tools/machine_paths.py` | refuse a hard-coded machine path (`docs/commit.md`) | kw: machine_paths
- `tools/machine_setup.py` | switch Claude config folders to this kit, or set up a fresh machine (`docs/machine-setup.md`) | kw: machine_setup, fresh
- `tools/agent_temp_init.py` | set up the agents' temp folder alone (`docs/machine-setup.md` "Agent temp init") | kw: agent_temp_init, agent temp
- `tools/plugin_refresh.py` | export pushed kit main and refresh the plugin in every account folder (`docs/plugin-source.md`) | kw: plugin_refresh, plugin
- `tools/pieces.py` | the path-to-piece table (`docs/repo-checks.md`) | kw: pieces
- `tools/pm_data.py` | make or re-link the PM-data branch (`docs/pm-data-branch.md`) | kw: pm_data
- `tools/trailers.py` | fail a commit with no agent trailer (`docs/repo-checks.md`) | kw: trailers
- `tools/worktree_paths.py` | which path a worktree write may go to (`docs/worktree-paths.md`) | kw: worktree_paths
- `tools/worktree_sweep_task.py` | nightly worktree sweep scheduled task (`docs/worktree-remove.md`) | kw: worktree_sweep_task, sweep
