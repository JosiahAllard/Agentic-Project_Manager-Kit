# What ships

Everything below lives only in the kit. Claude accounts and repositories point at the kit and
never copy it. `tests/skills`, `tests/templates` and `tests/knowledge` scrub every shipped text
file for ruling ids, repository names, user paths and the owner's name.

## The Claude Code plugin (`.claude-plugin/plugin.json`)

- **Agents** (`claude/agents/`): the agent definitions; docs/ROSTER.md says which model and
  effort each runs at and what it is for.
- **Skills** (`claude/skills/<name>/SKILL.md`): backlog-ops, branch-review, branch-sync,
  coordinator-dispatch, doc-claims-discipline, flaky-test-triage, land-branch (uses
  `tools/land.py`), machine-lock-run, mutation-testing, session-handoff,
  shell-and-worktree-traps, subagent-dispatch. branch-review and coordinator-dispatch hold the
  coordinator's procedure, loaded only when needed. Skills name kit tools by path
  (`tests/skills` checks each exists); `tools/doc_index.py`, `tools/doc_budget.py` and
  `tools/gate.json` are each repository's own.
- **Lock MCP server** (`mcp/lock_server.py`, docs/lock-mcp.md).

## Stack templates (`templates/`)

- `templates/common/CLAUDE.md`: the common rules file, with `<PLACEHOLDER>` slots a repository
  fills. `templates/common/LEDGER.md` starts a new rulings ledger. `templates/common/.gitignore`,
  `templates/common/gitattributes` (no leading dot: a tracked `.gitattributes` would apply to the kit
  itself) and `INDEX.md` are what `installer/new-repo.ps1` writes into a new repository
  (docs/install.md).
  `templates/common/agent-profile.TEMPLATE.md` and `templates/common/coordinator-profile.TEMPLATE.md`
  are what the coordinator drafts a repository's two profiles from (claude/agents/coordinator.md, "On start");
  their headings are the ones the agents read, and `tests/templates` checks them.
- `templates/stacks/{dotnet,godot,java,python}`: `stack.json` (build and test commands,
  gitignore, gitattributes; `new-repo.ps1 -Stack` applies the two lists), `tools/gate.json` (a `"gate"` section of `.kit.json`; point to it
  with `"gate": "tools/gate.json"`) and `.github/workflows/{quick,full}.yml`. Godot also carries
  `tools/godot.cmd` and `.claude/skills/godot-render-check/SKILL.md` (its `{kit_root}` is filled in at install).
- The Java dependency-pin check is a hook-module check, not a gate key (the schema has none).

## Optional machine-wide tools (`optional/`, tests in `tests/optional`)

- `comfy`: a ComfyUI image-generation client. Its lock tool defaults to the plugin's
  `tools/machine_lock.py`; env `KIT_MACHINE_LOCK` names another. No connection to the generator
  exits 2 with `GENERATOR UNAVAILABLE` and stops the run; an HTTP error from ComfyUI exits 1.
  `render_glb.py` renders GLB contact sheets in Blender (`--grid COLS`: one view of many
  models). `compare_sheet.py` writes a labelled HTML page of images; it refuses a non-image
  input, and an output that is not .html/.htm, is one of the images, or is an existing non-HTML
  file.
- `procwatch`: a process watcher. It spares ComfyUI; add patterns with env `PROCWATCH_PROTECT`
  (`;`-separated regexes).
- `usage`: usage reports and the statusline (`statusline-usage.ps1`, `write-usage-status.ps1`).
- `agenttemp`: `agent_temp_sweep.py` clears old entries from the agents' shared temp folder,
  set by `agent_temp_sweep` in the machine config (docs/kit-json.md).

## Knowledge (`knowledge/asset-generation`)

README, BEST_PRACTICES and MODELS. No session log and no samples: each repository records its
sessions in its own `docs/ops/assets.md`, and lessons that hold up go into BEST_PRACTICES.

## Stays repo-local

- A model training and evaluation skill: one repository's workflow, meaningless elsewhere.
- `worktree-start`: wraps one repository's own worktree tools; the kit has `new_worktree` for
  the general case.
- `godot-render-check` is not a plugin skill: it ships, made generic, in the Godot stack template
  (repository values come from its docs), keeping the plugin's skills generic.
- `jobrun.py`/`jobrun.sh`: not shipped; `kitlib/proc` and the machine lock cover them.
- The source's `docs/` and `.claude/` profiles under the common template were not ported.
