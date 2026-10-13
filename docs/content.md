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
`{{` that is not one, refuses the render.

| token | Claude |
|---|---|
| `{{agent:<role>}}` | `pm-kit:<role>` |
| `{{profile_path}}` | `.claude/agent-profile.md` |
| `{{coordinator_profile_path}}` | `.claude/coordinator-profile.md` |
| `{{instructions_file}}` | `CLAUDE.md` |
| `{{worktrees_dir}}` | `.claude/worktrees` |

## Claude

`hosts/claude/adapter/content.py` writes `hosts/claude/shell/agents/<name>.md` with `name`, `description` (quoted only
when YAML needs it), `tools` (the capabilities as Claude tool names; none for `all`), `model`,
`effort` when set, and for `cache: long` the `experimental` map with `cacheTtl: 1h`. It copies each
skill folder to `hosts/claude/shell/skills/<name>/`. It owns both folders.

An agent body gets no `${CLAUDE_PLUGIN_ROOT}` substitution, so a role names the kit's folder
`<kit>`, as a skill does, and the kit header says what it is (tests/content
`test_no_agent_file_names_the_plugin_root_variable`).

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
