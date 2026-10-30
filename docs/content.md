# Content and render_content

Every role and skill is written once, in `content/`, and rendered into each host's own files
(ARCHITECTURE.md "Content"; docs/multi-host-plan.md section 2.3). The rendered files are committed;
a test fails when they differ from what the renderer writes now (tests/content/test_render.py,
`RealContentTest`).

- `content/roles/<name>.md`: one role. Front matter, then a neutral body.
- `content/skills/<name>/`: one skill folder, shipped byte for byte on every host. Claude's plugin
  loads it from there, and so does Codex's, from the export (see "Codex").
- `content/default-profile.md`: the agent profile every role reads when a repository has none
  (docs/agent-profile.md). Not rendered: it holds no tokens and names no host
  (tests/content/test_default_profile.py).
- `python tools/render_content.py` (`--check`, the default) lists each rendered file that differs,
  is missing, or is stale (in a folder a host renderer owns but no longer rendered, or in a folder
  it has retired: `retired()`, Claude's old `hosts/claude/shell/skills/`). Exit 1 if any.
- `python tools/render_content.py --write` writes them and removes the stale ones. `--host <name>`
  (repeatable) limits either to the named hosts. Exit 2: usage, or a content file of the wrong shape (the line names
  the file and the problem), or `--write` into an installed plugin export, a folder with the
  `.pm-kit-export` marker in it or above it (kitlib.content.write refuses it; name the checkout with `-C <checkout>`).

The renderer also writes each host's plugin manifests from `kit.json` (the kit's one name and
version, ARCHITECTURE.md "Packaging"; see "Manifests" below), and the two tables in
`docs/ROSTER.md` (see "The roster" below). Never edit
`hosts/*/shell/agents/`, `hosts/*/shell/skills/`, `.claude-plugin/*.json` or a host's other rendered
manifests by hand: edit `content/` or `kit.json`, then `--write`, and commit both (see "Generated
files" and "Render on commit" below). Code: `kitlib/content` (load, render, drift, write), `kitlib/host/content.py` (the
role model, tokens, and the `ContentRenderer` protocol), `hosts/claude/adapter/content.py` (Claude's
renderer).

## Role front matter

| key | value |
|---|---|
| `name` | the file's stem |
| `description` | one line: plain, or quoted as YAML needs (a `: ` inside) |
| `tier` | `light`, `standard`, `strong` or `deep`; each host maps a tier to a model (docs/ROSTER.md) |
| `effort` | optional: `low`, `medium`, `high`, `xhigh`, `max` |
| `sandbox` | `read-only` or `workspace-write` |
| `capabilities` | `all` (no tool list), or the tools in order: `[read, edit, write, search, find, bash, powershell, worktree, enter-worktree, artifact, dispatch, message, list-agents, stop-task, monitor, tool-search, skill, ask-owner, cron-create, cron-list, cron-delete, web-search, web-fetch]` (`kitlib.host.content.CAPABILITIES`) |
| `cache` | optional: `long`, the host's long prompt cache |
| `compact` | optional: the role's auto-compact window in tokens, a whole number 100000..1000000 (`kitlib.host.content.COMPACT`). The light subagent roles (analyst, docs-writer, helper, lander, test-runner, weak-worker) set 170000, so they compact before a 200k parent would. Claude renders it; Codex does not ("Codex") |
| `model.<host>` | optional: that host runs the role on this model instead of the tier's. None is in use. |
| `mode` | optional: the role policy mode, `controller`, `write`, `read`, `test` or `lander` (`kitlib.host.content.MODES`). Every kit role names one. The guard reads it (`kitlib.content.role_modes`) where a repository sets `guard.role_policy` (docs/guard.md "Role policy"); no host's file carries it, so a change to it renders nothing |
| `writes` | optional: `[path, ...]`, repo-relative posix paths or globs (no `..`, no drive, no `\`) a `read`, `test` or `lander` role may still edit in its checkout (docs/guard.md "Role policy"). The loader accepts it; no role file uses it today. Not rendered either |
| `group` | optional: the role's group in the backlog policy (`kitlib/backlog/roles.py`) where its `mode` alone does not give it. Without it the group is the mode's: `controller` lead, `write` builder, `read` and `test` read-only, `lander` lander. The one value in use is `docs` (docs-writer: a `write` role that adds records but never pulls them), and it needs mode `write` (`kitlib.host.content.GROUP_MODES`). Not rendered |
| `run` | optional: `session`, a role started only as its own launched session, never dispatched as a subagent (pm, coordinator, deep-worker, consultant). The backlog policy holds such a role only from a launcher session mark (`SESSION_ONLY`); pm, which the policy names for its owner-level rows, is held so whatever its file says. Role files are not owner paths, so a `mode`, `group` or `run` change moves a role between backlog groups once it lands. Not rendered |
| `summary` | optional: one line, the role's "use for" cell in docs/ROSTER.md (its description when absent). Not in any host's file |

A role file keeps its own line endings; the host file gets the same ones.

## The roster

The role list lives only in `content/roles/` (docs/multi-host-plan.md section 7, decision 17).
`kitlib.content.roster(kit)` reads it: name, mode, tier, group and run per role file, through the
same cached read as the guard's `role_modes`. The launcher's `KIT_AGENTS` and the backlog
policy's groups are read from it, never written by hand. `docs/ROSTER.md` is prose with two
rendered regions, each between `<!-- rendered: roles -->` (or `tiers`) and
`<!-- end rendered -->`: the role table (name, `tier`, `effort`, `summary`) and the tier table
(each host's hosts.json `tiers`). `render_content.py` writes and checks them with every host's
files, whatever `--host` says (`kitlib/roster`); the prose around them is the owner's.
`tools/agent_roster.py` checks or writes that file alone. So a role is added or removed with one
file and a render. tests/content/test_role_names.py fails on a `{{agent:<name>}}`, a host's
dispatch name (`pm-kit:<name>`; Codex's `kit-<name>`, quoted, in hosts/codex/) or a role tuple in
code that names no role file.

## Skills

A skill is not rendered, so it has no tokens and says nothing only one host understands
(docs/multi-host-plan.md section 5, form C): a numbered checklist, one line per step with its
command, a "Never" list, then "Details:" pointers. It names a role bare (the analyst role), the
kit's folder as `<kit>` (`<kit>/docs/gate.md`), and an edit as "your file-edit tool"; commits go
through `python <kit>/tools/commit.py -C <worktree>`, which takes the host from `PM_KIT_HOST`. The
coordinator, PM and worker role files say what a bare role name and `<kit>` are on their host.

A repository holds no stubs of the kit's tools (decision 21): every command a skill, role or the
default profile shows is `python <kit>/tools/<name>.py`, with `-C <worktree>` (or `<repo>`,
`<main checkout>`) when the tool takes `-C`. The tools that take no `-C` act on the machine, not a
checkout (`machine_lock.py`, `launch.py`, `ledger.py`, `pm_watch.py` and the like), or name their
path another way (`checkout_guard.py [PATH]`). In the kit's own repository `<kit>` is that
repository, so the same commands work there.

Tests: tests/skills `test_skills_are_host_neutral`; tests/content/test_skill_commands.py checks
that every kit tool command a skill or role shows is written `python <kit>/tools/<name>.py`,
exists, shows `-C` when the tool's parser defines it, and that the parser defines each flag
shown.

## Tokens

A body or description names a value that differs by host only as a token. An unknown token, or a
`{{` that is not one, refuses the render. A value `hosts.json` already holds is read from the host's
profile there, never written a second time in the renderer (Claude: `PROFILE_TOKENS`).

| token | meaning | Claude | Codex |
|---|---|---|---|
| `{{agent:<role>}}` | the name the host dispatches a role by | `pm-kit:<role>` | `kit-<role>` |
| `{{model:<tier>}}` | the host's model for a tier | hosts.json `tiers`: `{{model:strong}}` is `opus` | `<tier>-tier` (`strong-tier`) while hosts.json maps the tier to `account-default`, else the model |
| `{{tool:<capability>}}` | the host's tool for a capability (the front matter's names) | the renderer's `TOOLS`: `{{tool:message}}` is `SendMessage`, `{{tool:dispatch}}` `Agent` | the renderer's `TOOLS` (inferred names): `send_message`, `spawn_agent`, `apply_patch`; a capability with no Codex tool says so |
| `{{tool:enter-worktree}}` | work in an existing worktree a caller named (never create one); a capability like the rest | `EnterWorktree` | run every command in that existing worktree (never create one; `-C <worktree>` on kit tools) |
| `{{profile_path}}` | a repository's agent profile | `.claude/agent-profile.md` | `.codex/agent-profile.md` |
| `{{coordinator_profile_path}}` | a repository's coordinator profile | `.claude/coordinator-profile.md` | `.codex/coordinator-profile.md` |
| `{{instructions_file}}` | the repository instructions file the host reads | `CLAUDE.md` | `AGENTS.md` |
| `{{worktrees_dir}}` | the repository folder agent worktrees go in | `.claude/worktrees` (hosts.json profile `worktrees_directory`) | `.codex/worktrees` |
| `{{owner_folder}}` | the host's own folder in a repository, owner-only, with its final `/` | `.claude/` (the folder among hosts.json profile `owner_paths`) | `.codex/` |
| `{{user_folder}}` | the host's per-user folder, home-relative | `~/.claude` (`~/` and hosts.json profile `state_directory`) | `~/.codex` |
| `{{config_dir_env}}` | the environment variable that moves that per-user folder | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` |
| `{{long_context_model}}` | a noun phrase for the model a launched long-context session (deep-worker, consultant) runs on | `the 1M-context opus model` (the `deep` tier's model from hosts.json) | `the account's model` |
| `{{coauthor_email}}` | the address in the host's Co-Authored-By trailer | `noreply@anthropic.com` (hosts.json profile `attribution.email`) | `noreply@openai.com` |
| `{{launch_agent}}` | the command that starts the host's main session as a named agent; `{{agent:<role>}}` follows it | `claude --agent` | `python <kit>/hosts/codex/shell/scripts/launch.py --role` (a plan only) |

A neutral sentence is preferred to a token where it reads as well (`<kit>/optional/comfy/comfy_gen.py`,
not a per-user link to it). tests/content `test_role_bodies_name_no_claude_value_a_token_covers`
fails a role that writes a Claude value bare, and tests/host/test_leaks a host name in `content/`.

## Claude

`hosts/claude/adapter/content.py` writes `hosts/claude/shell/agents/<name>.md` with `name`, `description` (quoted only
when YAML needs it), `tools` (the capabilities as Claude tool names; none for `all`), `model` (hosts.json `tiers`, the
one tier map),
`effort` when set, `autoCompactWindow` for `compact`, and for `cache: long` the `experimental` map with
`cacheTtl: 1h`; the front matter opens with the generated-file line as a YAML comment.
`autoCompactWindow` in agent front matter is a ceiling, read in Claude Code 2.1.309's own code (INFERRED, not tried
live): the CLI's agent schema takes an integer 100000..1000000, a plugin agent keeps it, and a subagent
runs on its parent's window unless that is larger, when it runs on the agent's; the
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment variable still beats both. So a 170000 agent under a
200000 coordinator compacts at 170000, and under a 150000 session at 150000. It owns that folder. It copies no skill: the
plugin manifest's `skills` is `./content/skills/` (decision 29 in docs/multi-host-plan.md section 7),
and `hosts/claude/shell/skills/`, the copy it wrote before, is a retired folder: a file there is stale.

An agent body gets no `${CLAUDE_PLUGIN_ROOT}` substitution, so a role names the kit's folder
`<kit>`, as a skill does, and the kit header says what it is (tests/content
`test_no_agent_file_names_the_plugin_root_variable`).

## Codex

`hosts/codex/adapter/content.py` writes `hosts/codex/shell/agents/kit-<name>.toml`: a header comment
opening with the generated-file line,
`name` (`kit-<name>`), `description`, `model` (none while the tier maps to `account-default`: the
account's model runs; docs/ROSTER.md), `model_reasoning_effort` (the role's `effort`, else its tier's),
`sandbox_mode` (`read-only` for a `read` or `lander` mode role, else the role's `sandbox`) and `developer_instructions`, the body with Codex's token values
as a TOML multi-line string. A Codex agent file has no tool list, so `capabilities` and `cache` are not
rendered; nor is `compact`, because no per-agent compact key in a Codex agent file has been established
(no Codex install here to check). It owns the agents folder alone: Codex loads the skills from
`./content/skills/` in the export, so none is copied. Its `retired()` names `hosts/codex/shell/skills`
and `hosts/codex/shell/boot`, the folders the old hand-made copy needed, so a file left in either is
stale. Its
manifests are `hosts/codex/shell/plugin.json` (the name and version; `./content/skills/`, the hooks file and `mcp.json` named from the export root, where the export places it as `.codex-plugin/plugin.json`, docs/plugin-source.md "Layout"),
`hosts/codex/shell/mcp.json` (the read-only lock server and the hub server) and `hosts/codex/shell/hooks/hooks.json`
(every declared hook event running `boot/hook.py codex <event>`, the export's own entry, as one
command string), with `hooks.linux.json` beside it as for Claude ("Manifests"): the Linux command
line joined as a POSIX shell line, `${PLUGIN_ROOT}/boot/hook.py` in double quotes and the body in
single quotes (`posix_quote`). Nothing is vendored: the Codex plugin root is the export folder, which
holds `boot/` and `hosts.json`, as Claude's does. Status:
hosts/codex/README.md.

## Manifests

A renderer with `manifests(kit, content)` also writes its host's plugin files, `kit` being
`kitlib.content.load_kit` of `kit.json` (`{"name", "version"}`: a lowercase name and an `N.N.N`
version, nothing else). Claude's are each JSON with indent 2 and a final newline:

- `.claude-plugin/plugin.json`: the name and version, every agent file, the skills folder
  (`./content/skills/`), the
  hooks file and the MCP servers of `MCP_SERVERS`: `machine-lock`
  (`python ${CLAUDE_PLUGIN_ROOT}/mcp/lock_server.py`) and `hub` (`mcp/hub_server.py`, docs/hub-mcp.md).
  It stays at the kit root because the plugin root is the whole kit: the hooks and the MCP servers
  run kitlib and tools beside them, and an installed plugin reaches only files under its root.
  The export (docs/plugin-source.md) stamps its version as `<version>-main.<count>`. That is the
  Windows file; `.claude-plugin/plugin.<platform>.json` beside it, one for each other platform
  hosts.json gives the host, is the same manifest with each MCP server run by that platform's
  interpreter (`kitlib.host.hookmain.mcp_interpreter`: `python3` on Linux, where stock Ubuntu has no
  `python`). Codex's `mcp.json` gets an `mcp.<platform>.json` the same way.
- `.claude-plugin/marketplace.json`: the checkout as a local marketplace of that one plugin
  (`"source": "./"`), the version as `metadata.version`.
- `hosts/claude/shell/hooks/hooks.json`: the plugin's hooks (`HOOKS` in the renderer), each running
  the kit's one hook entry, `${CLAUDE_PLUGIN_ROOT}/boot/hook.py claude <event> [<part>]`. Each command
  line comes from `kitlib.host.hookmain.manifest_command`, the interpreter by name for one platform
  (`INTERPRETERS`), so no manifest spells it by hand. hooks.json is the Windows file: `pythonw` in
  exec form.
- `hosts/claude/shell/hooks/hooks.linux.json`, one `hooks.<platform>.json` beside hooks.json for each
  other platform hosts.json gives the host: the same hooks, events, matchers and arguments, with that
  platform's command line. On Linux that is `/bin/sh -c <body> kit-hook <script> <args>`, the body
  running `python3 "$@"` by name from PATH: a tool event's body adds `|| exit 2`, so a python3 that
  fails or is missing blocks the call as boot/hook.py does (the guard-error deny); a start event's
  adds `|| echo "PM kit: the kit did not load: ..."`, which the host shows and exits 0. Every file
  is rendered on every machine, so a committed manifest has no drift; plugin.json names hooks.json
  alone, and the plugin export writes the running platform's file in its place, as it does for
  plugin.json and Codex's mcp.json (docs/plugin-source.md "Layout").

A version bump is one edit to `kit.json`, then `--write`. tests/content `ManifestTest`.

## Generated files

Every rendered file a comment can go in opens with one line (`kitlib.host.content.banner`, decision 19
in docs/multi-host-plan.md section 7): `Generated from <source>: edit that, then run python
tools/render_content.py --write. Never edit this file by hand.` A Claude agent file carries it as a
YAML comment on the front matter's first line, so the front matter stays the file's first thing; a
Codex agent file as its first `#` line. JSON holds no comment, and the manifests keep to the keys
their host documents, so plugin.json, marketplace.json, mcp.json, hooks.json and the plugin's
hosts.json carry none (plugin.json's description says it is rendered); neither do the byte-for-byte
copies (skills, `boot/`), which must stay their source's bytes. The line is part of the render, so
the drift check covers it.

The guard refuses a hand edit of a rendered file in a kit checkout, in any session, and names its
source and the render command (rule `generated-file`, docs/guard.md "Generated files").

## Render on commit

`tools/commit.py` renders when a path it commits is under `content/` or is `kit.json` or
`hosts.json`, in a work tree with the kit's layout (`content/roles/` and
`tools/render_content.py`): it runs that work tree's own `tools/render_content.py -C <work tree>
--write` first, and stages every file the render wrote or removed into the same commit, listed on a
`rendered` line (`kitlib.content.oncommit`). A render that fails refuses the commit and names the
command to run. A rendered file written earlier by hand with `--write`, which this render then does
not change, is not added: name it.

The opt-in pre-commit hook (`githooks/pre-commit`, docs/ledger.md "Pre-commit check") does the same
for a plain `git commit` in a kit checkout, through `kitlib.githooks.render`: its shell front end
starts python only when the kit's layout is there and a render source is staged (one `git diff
--cached` read), renders, and stages what changed into the commit's index; when it cannot, it
refuses the commit with the command to run. A commit of named paths (`git commit <paths>`) that
the render changes is refused too, naming the files to stage: git builds it from a temporary
index and keeps its own index with only the named paths, so a rendered file staged into the
commit would stay old in the index and the next commit would undo it. The gate's drift check
stays the backstop.

## Kit header

`boot/hook.py <host> <event>` adds it at every start event a host declares; on Claude it runs on
SessionStart (startup, resume, clear, compact) and on SubagentStart (every subagent). It boots the
kit (docs/kitboot.md) and adds
`kitlib.host.header.text(<kit folder>)` to the agent's context (`additionalContext`, rendered by the
Claude adapter's `context`). The text names the kit's absolute folder, says that `<kit>` in skills
and role files means that folder (with a worked command), that the kit's tools run from it and
never from a copy in the repository, a tool that acts on a checkout named with `-C` (decision 21 in
docs/multi-host-plan.md section 7), and that `<worktree>`, `<repo>`, `<slug>` and the like are
placeholders. A kit that does not load is said in plain text instead.

The coordinator's SessionStart hook stays a separate entry: hooks.json runs both, and Claude Code
joins their context. Keeping it apart keeps its path, which settings re-pointed by machine setup
still name (docs/machine-setup.md "Settings"), and its role check.

A plugin's SubagentStart reaches the subagent, from the plugin's hooks.json entry
(hosts/claude/adapter/content.py `HOOKS`). So a dispatched agent gets the header without its brief
naming the kit folder.

At SubagentStart a dispatched kit role also gets the agent-start block after the header
(kitlib.agentstart; docs/preflight.md "Agent start block"): `start_text` adds it when the payload's
`agent_type` names a kit role that is not `run: session` (hosts/claude/adapter/host.py
`_agent_start`, its one call site). A host's own agent (Explore) and a session role get the header
alone. With no `agent_id` (tools/agent_view.py asking for a role) the session mark names the role,
so the "At start" section shows the block. On Codex every child gets it after the child note
(hosts/codex/adapter/start.py): its hooks carry no child identity.

## Agent view

`python <kit>/tools/agent_view.py <role> [--host <name>] [-C <repo>] [--brief]` shows everything
one role is given and held to on one host (docs/multi-host-plan.md section 7, decision 18). The
host is `--host`, else `PM_KIT_HOST`, else the hosts.json default; `-C` is the repository the role
works in (default: here); `--brief` prints paths only and runs no start check. Exit 0 shown; 2 a
usage error, a role no role file defines (the line names every role) or an unknown host. An old
name (`kitlib.content.ALIASES`) shows its new role.

It is built from the code that renders and enforces, so it cannot drift from it. The host's side
comes only through the host contract (docs/new-host.md), which the conformance suite pins to what
the host writes: the ContentRenderer's `role_path`, `settings`, `tokens` and `tools`, and the
adapter's optional `start_text` and `start_parts`. File text is indented under its label.

Sections 1-7, what the agent is given, in the order it gets it:
1. **Role file**: `renderer.role_path(role)`, the file the host loads, with its text as `render`
   writes it, and a note when the committed copy differs.
2. **Repository instructions**: the `instructions_file` token in the repository, and each file it
   imports with a `@<path>` line.
3. **User instructions**: that file name in the host's per-user folder (the `user_folder` token,
   or the folder `config_dir_env` names). The kit never reads it.
4. **Kit header**: `kitlib.host.header.text`, at SubagentStart for a dispatched role and at
   SessionStart for a `run: session` one.
5. **At start**: what the start hooks add past the header for a session marked with the role
   (`PM_KIT_ROLE`) in the repository: the adapter's `start_text` for each part `start_parts` says
   the role runs (the plain hook, then each named part, labelled), the same call `boot/hook.py`
   makes, so the view is what the hooks write. For the PM and the coordinator that runs their start
   checks. A host without `start_text` is shown as not known, never guessed.
6. **Skills**: `content/skills`, where the host has skills, and a note when the role's tool list
   leaves out the host's skill tool.
7. **Repo profile**: the `profile_path` token (and `coordinator_profile_path` when the role's body
   names it) if the repository has it, else none; for `profile_path`, none names the kit's
   `content/default-profile.md`, the profile in use then (docs/agent-profile.md).

Sections 8-11, what it is held to:
8. **Model, tools and mode**: the role file's tier, effort and sandbox; what the host's file sets
   (`renderer.settings`: its model, effort, sandbox or tool list as that file writes them); each
   capability as the host's tool (`renderer.tools()`); `mode`, `writes`, group and run.
9. **Guard rules**: the `kitlib/guard/rolepolicy.py` `RULES` rows for its mode (`rules_for`), with
   its `LOCK_VERBS`; tests/guard/test_rolepolicy.py pins each row to the guard's answers.
10. **.kit.json switches**: what the guard reads from the main checkout's `.kit.json`
    (`guard.role_policy`, the role's `roles` entry, the mode `rolepolicy.mode` gives it,
    `edit_main_checkout`, `guard.data_roots`).
11. **Backlog rights**: every `kitlib/backlog/roles.py` `ACTIONS` row, grouped by the answer
    (`role_of`, `allows`, and the fact a grant needs).

Code: `kitlib/agentview`; tests `tests/agentview`. `content/README.md` maps each thing shown to
the file that changes it.
