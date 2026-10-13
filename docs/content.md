# Content and render_content

Every role and skill is written once, in `content/`, and rendered into each host's own files
(ARCHITECTURE.md "Content"; docs/multi-host-plan.md section 2.3). The rendered files are committed;
a test fails when they differ from what the renderer writes now (tests/content/test_render.py,
`RealContentTest`).

- `content/roles/<name>.md`: one role. Front matter, then a neutral body.
- `content/skills/<name>/`: one skill folder, shipped byte for byte on every host.
- `python tools/render_content.py` (`--check`, the default) lists each rendered file that differs,
  is missing, or is stale (in a folder a host renderer owns, but no longer rendered). Exit 1 if any.
- `python tools/render_content.py --write` writes them and removes the stale ones. `--host <name>`
  limits either to one host. Exit 2: usage, or a content file of the wrong shape (the line names
  the file and the problem).

The renderer also writes each host's plugin manifests from `kit.json` (the kit's one name and
version, ARCHITECTURE.md "Packaging"; see "Manifests" below). Never edit
`hosts/claude/shell/agents/`, `hosts/claude/shell/skills/`, `.claude-plugin/*.json` or
`hosts/claude/shell/hooks/hooks.json` by hand: edit `content/` or `kit.json`, then `--write`, and
commit both. Code: `kitlib/content` (load, render, drift, write), `kitlib/host/content.py` (the
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
| `capabilities` | `all` (no tool list), or the tools in order: `[read, edit, write, search, find, bash, powershell, worktree, artifact, dispatch, message, list-agents, stop-task, monitor, tool-search, skill, ask-owner, cron-create, cron-list, cron-delete, web-search, web-fetch]` (`kitlib.host.content.CAPABILITIES`) |
| `cache` | optional: `long`, the host's long prompt cache |
| `model.<host>` | optional: that host runs the role on this model instead of the tier's. None is in use. |
| `mode` | optional: the role policy mode, `controller`, `write`, `read`, `test` or `lander` (`kitlib.host.content.MODES`). Every kit role names one. The guard reads it (`kitlib.content.role_modes`) where a repository sets `guard.role_policy` (docs/guard.md "Role policy"); no host's file carries it, so a change to it renders nothing |
| `writes` | optional: `[path, ...]`, repo-relative posix paths or globs (no `..`, no drive, no `\`) a `read`, `test` or `lander` role may still edit in its checkout (docs/guard.md "Role policy"). The consultant names its notes file. Not rendered either |

A role file keeps its own line endings; the host file gets the same ones.

## Skills

A skill is not rendered, so it has no tokens and says nothing only one host understands
(docs/multi-host-plan.md section 5, form C): a numbered checklist, one line per step with its
command, a "Never" list, then "Details:" pointers. It names a role bare (the reviewer role), the
kit's folder as `<kit>` (`<kit>/docs/gate.md`), and an edit as "your file-edit tool"; commits go
through `python tools/commit.py`, which takes the host from `PM_KIT_HOST`. The coordinator, PM and
worker role files say what a bare role name and `<kit>` are on their host. Tests:
tests/skills `test_skills_are_host_neutral`; tests/content/test_skill_commands.py checks that every
`python tools/<name>.py` command a skill or role shows exists and that the tool's parser defines
each flag shown.

## Tokens

A body or description names a value that differs by host only as a token. An unknown token, or a
`{{` that is not one, refuses the render. A value `hosts.json` already holds is read from the host's
profile there, never written a second time in the renderer (Claude: `PROFILE_TOKENS`).

| token | meaning | Claude | Codex |
|---|---|---|---|
| `{{agent:<role>}}` | the name the host dispatches a role by | `pm-kit:<role>` | `kit-<role>` |
| `{{model:<tier>}}` | the host's model for a tier | hosts.json `tiers`: `{{model:strong}}` is `opus` | `the account's model` while hosts.json maps the tier to `account-default` |
| `{{tool:<capability>}}` | the host's tool for a capability (the front matter's names) | the renderer's `TOOLS`: `{{tool:message}}` is `SendMessage`, `{{tool:dispatch}}` `Agent` | the renderer's `TOOLS` (inferred names): `send_message`, `spawn_agent`, `apply_patch`; a capability with no Codex tool says so |
| `{{profile_path}}` | a repository's agent profile | `.claude/agent-profile.md` | `docs/agent-profile.md` |
| `{{coordinator_profile_path}}` | a repository's coordinator profile | `.claude/coordinator-profile.md` | `docs/coordinator-profile.md` |
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
`effort` when set, and for `cache: long` the `experimental` map with `cacheTtl: 1h`. It copies each
skill folder to `hosts/claude/shell/skills/<name>/`. It owns both folders.

An agent body gets no `${CLAUDE_PLUGIN_ROOT}` substitution, so a role names the kit's folder
`<kit>`, as a skill does, and the kit header says what it is (tests/content
`test_no_agent_file_names_the_plugin_root_variable`).

## Codex

`hosts/codex/adapter/content.py` writes `hosts/codex/shell/agents/kit-<name>.toml`: a header comment,
`name` (`kit-<name>`), `description`, `model` (none while the tier maps to `account-default`: the
account's model runs; docs/ROSTER.md), `model_reasoning_effort` (the role's `effort`, else its tier's),
`sandbox_mode` (the role's `sandbox`) and `developer_instructions`, the body with Codex's token values
as a TOML multi-line string. A Codex agent file has no tool list, so `capabilities` and `cache` are not
rendered. It copies each skill folder to `hosts/codex/shell/skills/<name>/` and owns both folders. Its
manifests are `hosts/codex/shell/plugin.json` (the name and version, the hooks file),
`hosts/codex/shell/mcp.json` (the read-only lock server) and `hosts/codex/shell/hooks/hooks.json`
(every declared hook event running `hooks/run.py <event>`, as one command string). Its
`vendored(root)` ships a byte copy of `boot/kitboot.py` as `hosts/codex/shell/boot/kitboot.py`: the
Codex plugin root is `hosts/codex/shell`, so a copied plugin carries the check that verifies the kit
it binds; `--check` fails when the copy drifts. Claude needs none: its plugin root is the whole kit,
`boot/` included. Status: hosts/codex/README.md.

## Manifests

A renderer with `manifests(kit, content)` also writes its host's plugin files, `kit` being
`kitlib.content.load_kit` of `kit.json` (`{"name", "version"}`: a lowercase name and an `N.N.N`
version, nothing else). Claude's are each JSON with indent 2 and a final newline:

- `.claude-plugin/plugin.json`: the name and version, every agent file, the skills folder, the
  hooks file and the machine-lock MCP server (`python ${CLAUDE_PLUGIN_ROOT}/mcp/lock_server.py`).
  It stays at the kit root because the plugin root is the whole kit: the hooks and the MCP server
  run kitlib and tools beside them, and an installed plugin reaches only files under its root.
  The export (docs/plugin-source.md) stamps its version as `<version>-main.<count>`.
- `.claude-plugin/marketplace.json`: the checkout as a local marketplace of that one plugin
  (`"source": "./"`), the version as `metadata.version`.
- `hosts/claude/shell/hooks/hooks.json`: the plugin's hooks (`HOOKS` in the renderer). Each command
  line comes from `kitlib.host.hookmain.manifest_command`, the interpreter by name for the platforms
  hosts.json gives the host (`pythonw` on Windows), so no manifest spells it by hand.

A version bump is one edit to `kit.json`, then `--write`. tests/content `ManifestTest`.

## Kit header

`hosts/claude/shell/hooks/kit_header.py <event>` runs on SessionStart (startup, resume, clear,
compact) and on SubagentStart (every subagent). It boots the kit (docs/kitboot.md) and adds
`kitlib.host.header.text(<kit folder>)` to the agent's context (`additionalContext`, rendered by the
Claude adapter's `context`). The text names the kit's absolute folder, says that `<kit>` in skills
and role files means that folder (with a worked command), that a tool path without `<kit>/` is the
repository's own stub, and that `<worktree>`, `<repo>`, `<slug>` and the like are placeholders. A
kit that does not load is said in plain text instead.

The coordinator's SessionStart hook stays a separate entry: hooks.json runs both, and Claude Code
joins their context. Keeping it apart keeps its path, which settings re-pointed by machine setup
still name (docs/machine-setup.md "Settings"), and its role check.

Open: the Claude Code docs show SubagentStart's `additionalContext` reaching the subagent only for
a hook in settings.json. Whether a plugin's hooks.json may register SubagentStart needs a live
check on the owner's machine: start a subagent with the plugin enabled and ask it for the kit
folder. If it cannot, only the main session gets the header, and a brief must name the kit folder
for a subagent.
