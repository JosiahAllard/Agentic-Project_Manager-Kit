# Adding a host

A host is an AI platform the kit runs on, such as Claude Code. Adding one means adding a folder
and one `hosts.json` entry. No file in `kitlib/`, `content/` or another host's folder changes
(docs/multi-host-plan.md section 2.7; ARCHITECTURE.md "Hosts"). A host is supported when its test
class passes the conformance suite, `tests/host/conformance.py`.

The worked example is the fake host in `tests/host/fakehost/`. It exists only in the tests, and it
passes the same suite as Claude (`tests/host/test_conformance_fake.py`). It has no ask, no subagent
identity in its hooks, no MCP and no skills, so it shows each way the kit degrades. Copy its files
and rename them. Claude is the full example: `hosts/claude/` and
`tests/host/test_conformance_claude.py`. Codex (`hosts/codex/`, `tests/host/test_conformance_codex.py`)
shows a real host without ask or subagent identity, with a several-target edit tool and a `screen`.

## 1. The `hosts.json` entry

Add one object under the host's name: lowercase letters, digits and `-`. `kitlib.host.registry`
checks the whole file, and one bad field refuses all of it. The fake host's entry is
`tests/host/fakehost/host.json`.

| Field | What it is |
|---|---|
| `adapter` | The adapter's dotted module name, `hosts.<name>.adapter`. kitlib never imports it. `tools/_host.py` does. |
| `shell` | `hosts/<name>/shell`, the folder with the host's manifests and hook entries |
| `platforms` | The operating systems it runs on. Use `["windows"]` until the OS work (plan section 6). This field picks the hook interpreter (`kitlib.host.hookmain.manifest_command`). |
| `default` | Leave it out. Exactly one host is the default, and it is `claude`. |
| `tiers` | `light`, `standard`, `strong` and `deep`, each mapped to the host's model id (docs/ROSTER.md) |

`capabilities` says what the host's hooks can carry. The core uses it to degrade one way for every host:

| Field | What it is |
|---|---|
| `hook_events` | The host's own names for the events the kit hooks. Every event the adapter parses is listed here, and none other. |
| `supports_ask` | `false`: an `ask` verdict becomes a deny with the same reason (`kitlib.host.degrade`) |
| `subagent_identity_in_hooks` | `false`: `Action.agent` is always `None`, and only the role policy applies |
| `mcp` | Whether the host runs MCP servers, which includes the lock server |
| `skills` | Whether it reads `SKILL.md` folders. If `true`, every skill ships byte for byte. |
| `agent_format` | The file format of its agent definitions, for example `markdown` or `toml` |

`profile` holds the host data the core reads. Each field has a consumer (`kitlib.host.HostProfile`):

| Field | What it is |
|---|---|
| `owner_paths` | Its instruction and settings paths in a repository. A folder ends in `/`. They are read-only to agents. |
| `worktrees_directory` | The repo path its agent worktrees go in |
| `state_directory` | Its per-user folder name |
| `process_names` | Its agent process images. The lock uses them to tell an agent host from other processes. |
| `mcp_tool_format` | How it names an MCP tool, using `{plugin}`, `{server}` and `{tool}` |
| `session_env` | The variable that holds its session id. The lock shows it but never trusts it. |
| `skills_directory` | The repo path it reads a repository's own skills from |
| `attribution` | The commit trailer: `name`, `email`, `model_env` (or `null`), and `model_pattern` (`null` allows any model name) |
| `agent_name_format` | How it names a bundled agent: `{plugin}` once at most, then `{name}` last |
| `session_process` | The process image of a session it launches |
| `env_prefixes` | The environment variable prefixes its sessions own (`CLAUDE_`). A child launched on another host never inherits them (`kitlib.sessionmark.child_environment`). |

## 2. The adapter: `hosts/<name>/adapter/`

The package exports `ADAPTER`, a `kitlib.host.HostAdapter`:

- `name`: the `hosts.json` key. `profile` and `capabilities` come from `registry.host(name)`.
- `parse(event_name, payload) -> Action`. Each tool call becomes `edit` (with `path`), `shell`
  (with `command` and `shell`, plus `workdir` or `background` if the host has them), `mcp` (the
  full tool name) or `other`. Start events become `session_start` or `subagent_start`. A
  subagent's identity goes in `agent`, and its bare role in `agent_role`. A payload of the wrong
  shape raises, and the hook then denies it. A call that acts on several targets (Codex's
  `apply_patch` adds, deletes and renames files in one call) returns a tuple of Actions, one per
  target: `hookmain` judges each and the first deny decides. An empty tuple is denied.
- `screen(action, environ)` (optional): the host's own policy over one Action, a `Decision` or
  `None`. It runs before the guard; its deny stands alone. Use it only for what is true of this
  host and no other (Codex: a shell command that runs `apply_patch`, a tool outside its allowlist).
- `agent(payload)` (optional): the subagent id alone. A deny for a payload that `parse` refused
  still gets the right role tag.
- `render(event_name, decision) -> (stdout, exit code)`. Use the host's own deny form. An allow
  says nothing. A host without ask never receives an ask.
- `context(event_name, text) -> (stdout, exit code)`. A start event adds `text` (the kit header,
  `kitlib.host.header`) to the agent's context. No text writes nothing.
- `sessions()`: a `SessionHost` with `sessions(cwd)`, `loaded()`, `usage()` and `models()`, read-only.
- `content()`: an object whose `renderer()` is a `kitlib.host.content.ContentRenderer`. It has
  `host`, `tier_models()` (the same map as `hosts.json` `tiers`), `owned()` (folders in the shell
  where it writes every file), `render(content)` for every role (and every skill if the host has
  skills), and `manifests(kit, content)` for the plugin files carrying `kit.json`'s version and the
  hook registration (command lines from `hookmain.manifest_command`). Role bodies fill tokens from
  the host's own table (docs/content.md "Tokens").
- `command_host()`, `launcher()` and `plugin_installer()`: `None` until the host has them.

## 3. The shell: `hosts/<name>/shell/`

- One hook entry per event, 20 lines or fewer. It puts `boot/` on `sys.path`, calls
  `kitboot.boot()` before any kitlib or adapter import (docs/kitboot.md), then calls
  `kitlib.host.hookmain.run(ADAPTER, event, stdin, stdout)`. A start event writes
  `ADAPTER.context(event, header.text(kit))` instead. `hookmain` fails closed: any error becomes
  the host's deny. The fake host's single entry is `tests/host/fakehost/shell/hooks/run.py`.
- The rendered files. Run `python tools/render_content.py --write` and commit what it writes.
  `--check` fails on drift. Never edit them by hand.

## 4. Fixtures and the conformance subclass

Recorded payloads go in `tests/host/<name>/payloads.json`, in the shape the conformance module
describes:
- `events`: each hook event mapped to `tool`, `session_start` or `subagent_start`
- `cases`: a payload and the whole `Action` it parses into (`actions`, a list, for a call with
  several targets)
- `malformed`: payloads that `parse` must refuse
- `render`: exact outputs for a few decisions

Record real payloads from the host. Never write them from its docs alone. Cover `edit`, `shell`,
`other`, and `mcp` if the host has MCP. If it carries subagent identity, also record a subagent's
tool call and a main-session call.

`tests/host/test_conformance_<name>.py` subclasses `conformance.HostConformance` and
`unittest.TestCase`. It sets `HOST` and `FIXTURES`, and implements:
- `read_output(stdout, code)`: returns `(verdict, reason)`
- `read_context(stdout)`: returns the context text
- `hook_scripts(manifests)`: returns every `(event, entry path)` the host registers
- `session_host()`: returns a `SessionHost` over recorded session files, with the ids it should list

The suite checks:
- the registry entry, and the adapter `tools/_host.py` imports
- parse for every case, and subagent identity only where it is declared
- a several-target call denied when any one target is
- render of allow, deny and ask, and the ask lowered through `hookmain.run`
- fail-closed denies
- the kit header at each start event
- sessions, rendered-file drift, and every role rendered
- `kit.json`'s version in the manifests
- boot before kitlib in every shell script, and an existing entry for every declared event
- the 20-line limit, and the host's name kept out of kitlib, tools, boot, content and other hosts

Two lists cover what is left to fix: `LEAK_ALLOWED` and `ENTRY_OVER`. Both can only shrink.
`tests/host/test_conformance_gate.py` checks that a change under `hosts/`, `kitlib/host/`,
`content/`, `kit.json` or `hosts.json` runs the suite (tests/selection.json).

## 5. Optional: the bridge half

For agent-to-agent messaging, add `optional/bridge/hosts/<name>/`, which implements the bridge
port (plan section 2.4). The core never imports it. With the bridge not installed, no host shell
mentions it.
