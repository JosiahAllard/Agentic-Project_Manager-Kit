# What ships

Everything below lives only in the kit. Claude accounts and repositories point at the kit and
never copy it. `tests/skills`, `tests/templates` and `tests/knowledge` scrub every shipped text
file for ruling ids, repository names, user paths and the owner's name.

## The built-in scrub

`tests/scrub/test_builtin.py` reads every file `git ls-files` lists, tests/ included, with the
classes in `kitlib/scrub` (CLASSES). It needs no machine config, so a clean clone runs it too;
the install's own names stay with `tests/scrub/test_generic.py` and `scrub_patterns`
(docs/kit-json.md).

- `gender`: the masculine subject, object, possessive and reflexive pronouns as whole words in
  any case, `\bh(?:e|is|im|imself)\b` (GENDER), and nothing else; the kit writes "the owner".
- `record-id`: a backlog record id. STOP_IDS lists the acronyms and markers that only look like
  one, and an invented id in a fixture starts with `EX-` (MADE_UP_ID_PREFIX).
- `provenance`: the kit this one replaced, called old, earlier, previous or legacy, or one of its
  files named; the bare title "PM kit" (AGENTS.md) passes.
- `pinned-hash`: a commit hash, outside tests/ only; git's empty tree and the hex alphabet pass
  (NOT_HASHES).
- `hardware`: a GPU model, a CPU family or chiplet, or a total core or thread count of ten or
  more.
- `drive`: a drive path into one of MACHINE_FOLDERS, the machine folders that tests/shipped.py
  BASE_FORBIDDEN also refuses (a test checks the two agree): `Users` only, since the kit codes no
  other folder name. The machine config's `machine_path_folders` are added by the caller:
  `tests/scrub/test_builtin.py` passes them to `scrub.classes`, and tests/shipped.py
  `machine_patterns` refuses them as "machine path" with the base patterns; the package itself
  reads no config. It reads tests/
  too; other drive paths there are invented fixtures, and `tests/scrub/test_generic.py` refuses every drive path
  outside tests/.
- `name`: a salted digest in NAME_STORE, hashed the way `kitlib/forbidden` hashes (docs/repo-checks.md,
  "forbidden"), so the names are never written down. The shipped store is empty; filling it is the
  owner's call.

Every class matches the whole file, so a phrase that a markdown wrap splits over two lines is
still found, at the line it starts on.

A permanent exception is an ALLOWED entry (path glob, class, line text, reason) in the test file;
an entry that no longer matches anything fails the check, so it is removed with the text it covered.
Hits that later work removes wait in its PENDING_* blocks, a baseline per (class, file): more hits
than the baseline fails, a file with no baseline fails on its first hit, and fewer hits prints
"lower the baseline" without failing. Each block is done when it is empty.

## The Claude Code plugin (`.claude-plugin/plugin.json`)

- **Agents** (`hosts/claude/shell/agents/`): the agent definitions; docs/ROSTER.md says which model and
  effort each runs at and what it is for.
- **Skills** (`content/skills/<name>/SKILL.md`, the plugin's skills folder itself, never copied:
  docs/content.md): backlog-ops, branch-review, branch-sync,
  coordinator-dispatch, doc-claims-discipline, flaky-test-triage, land-branch (uses
  `tools/land.py`), machine-lock-run, mutation-testing, session-handoff,
  shell-and-worktree-traps, subagent-dispatch, specialty-3d-assets, specialty-tech-art,
  specialty-sound, specialty-review, specialty-research, specialty-records, specialty-spec.
  branch-review and coordinator-dispatch hold the
  coordinator's procedure, loaded only when needed. Skills name kit tools by path
  (`tests/skills` checks each exists); `tools/doc_index.py`, `tools/doc_budget.py` and
  `tools/gate.json` are each repository's own.
- **Lock MCP server** (`mcp/lock_server.py`, docs/lock-mcp.md).
- **Hub MCP server** (`mcp/hub_server.py`, docs/hub-mcp.md).

## Stack templates (`templates/`)

- `templates/common/AGENTS.md`: the common rules file, with `<PLACEHOLDER>` slots a repository
  fills; `templates/common/CLAUDE.md` is the one line `@AGENTS.md` that makes Claude Code read it. `templates/common/LEDGER.md` starts a new rulings ledger. `templates/common/.gitignore`,
  `templates/common/gitattributes` (no leading dot: a tracked `.gitattributes` would apply to the kit
  itself) and `INDEX.md` are what the committed repo setup (`tools/machine_setup.py --repo`,
  `installer/new-repo.ps1`) writes into a repository that asks for it (docs/install.md
  "Committed setup (--repo)").
  `templates/common/agent-profile.TEMPLATE.md` and `templates/common/coordinator-profile.TEMPLATE.md`
  are what the coordinator drafts a repository's two profiles from (hosts/claude/shell/agents/coordinator.md, "On start");
  their headings are the ones the agents read, and `tests/templates` checks them.
- `templates/stacks/{dotnet,godot,java,python}`: `stack.json` (build and test commands,
  gitignore, gitattributes; `new-repo.ps1 -Stack` applies the two lists), `tools/gate.json` (a `"gate"` section of `.kit.json`; point to it
  with `"gate": "tools/gate.json"`) and `.github/workflows/{quick,full}.yml`. Some also carry a
  stack runner under `tools/`: `dotnet_each.py` (dotnet), `godot.cmd` (godot), `js_tests.py` (python). Godot also carries
  `.claude/skills/godot-render-check/SKILL.md` (its `{kit_root}` is filled in at install).
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
- `bridge`: agent-to-agent messaging between open host chats, an optional install that no host
  shell mentions until the owner registers it (`optional/bridge/README.md`; what works:
  `optional/bridge/STATUS.md`). Tests in `tests/bridge`.
- `agenttemp`: `agent_temp_sweep.py` clears old entries from the agents' shared temp folder and its
  session folders, set by `agent_temp_sweep` in the machine config (docs/kit-json.md). It never
  removes a `*-coordinator` or `*-consultant` folder, whatever `keep` says (`BUILT_IN_KEEP`; they
  hold a coordinator's state note and a consultant's notes, docs/launcher.md "Start").

## Knowledge (`knowledge/asset-generation`)

README, BEST_PRACTICES and MODELS. No session log and no samples: each repository records its
sessions in its own `docs/ops/assets.md`, and lessons that hold up go into BEST_PRACTICES.

## Stays repo-local

- A model training and evaluation skill: one repository's workflow, meaningless elsewhere.
- `worktree-start`: wraps one repository's own worktree tools; the kit has `new_worktree` for
  the general case.
- `godot-render-check` is not a plugin skill: it ships, made generic, in the Godot stack template
  (repository values come from its docs), keeping the plugin's skills generic.
- `jobrun.py`/`jobrun.sh`: not shipped. The shell-prefix wrapper they were part of was dropped by the owner's ruling, and nothing in the kit wraps an agent's Bash calls; `kitlib/proc` covers a process the kit starts itself, and the machine lock covers heavy runs.
- The source's `docs/` and `.claude/` profiles under the common template were not ported.
