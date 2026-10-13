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

Never edit `claude/agents/` or `claude/skills/` by hand: edit `content/`, then `--write`, and
commit both. Code: `kitlib/content` (load, render, drift, write), `kitlib/host/content.py` (the
role model, tokens, and the `ContentRenderer` protocol), `claude/adapter/content.py` (Claude's
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
| `{{kit_root}}` | `${CLAUDE_PLUGIN_ROOT}` |
| `{{worktrees_dir}}` | `.claude/worktrees` |

## Claude

`claude/adapter/content.py` writes `claude/agents/<name>.md` with `name`, `description` (quoted only
when YAML needs it), `tools` (the capabilities as Claude tool names; none for `all`), `model`,
`effort` when set, and for `cache: long` the `experimental` map with `cacheTtl: 1h`. It copies each
skill folder to `claude/skills/<name>/`. It owns both folders.
