# Adding a host

A host is an AI platform the kit runs on, such as Claude Code. Adding one means adding a folder
and one `hosts.json` entry. A host adds no hook script: its hooks run the kit's one entry,
`boot/hook.py <host> <event>` (plan decision 20). No file in `kitlib/`, `content/`, `boot/` or
another host's folder changes (docs/multi-host-plan.md section 2.7; ARCHITECTURE.md "Hosts"). A
host is supported when its test
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
| `shell` | `hosts/<name>/shell`, the folder with the host's manifests and rendered files |
| `platforms` | The operating systems it runs on, such as `["windows", "linux"]`. The renderer writes one hook manifest for each (`hooks.json` for Windows, `hooks.<platform>.json` beside it for the others), each command line from `kitlib.host.hookmain.manifest_command` for that platform's interpreter (docs/content.md "Manifests"). |
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

`hook` is what `boot/hook.py` reads before the kit boots, with json alone (`kitlib.host.registry.HookSpec`):

| Field | What it is |
|---|---|
| `tool_event` | The one `hook_events` entry whose calls the guard judges. Every other hook event is a start event: the adapter adds context for it. |
| `tool_field` | The dotted key of the tool's name in that event's payload: `tool_name`, or `call.name` for the fake host |
| `guarded_tools` | Every tool a guard rule or the host's own `screen` can refuse, by name; an entry ending in `*` is a prefix (`mcp__*`). A call to any other tool is allowed before the kit boots, so it never pays for a boot or a release check. The conformance suite fails when a recorded payload that parses into an edit, shell, read or MCP call (any kind but `other`; tests/host/conformance.py) names a tool left out. |
| `fast_path` | `all`: an unguarded call skips the kit in every session. `unmarked`: only in a session no launcher marked (`PM_KIT_ROLE` unset), for a host whose `screen` must see every call of a marked session (Codex holds a marked session to an allowlist). |

`profile` holds the host data the core reads. Each field has a consumer (`kitlib.host.HostProfile`):

| Field | What it is |
|---|---|
| `owner_paths` | Its instruction and settings paths in a repository. A folder ends in `/`. They are read-only to agents. |
| `worktrees_directory` | The repo path its agent worktrees go in |
| `state_directory` | Its per-user folder name |
| `process_names` | Its agent process images on each platform: `{"windows": ["claude.exe", "node.exe"], "linux": ["claude", "node"]}`, with an entry for each of the host's `platforms` (a platform it does not declare yet may be listed ahead). The lock uses the running platform's list to tell an agent host from other processes, and treats every caller as an agent when no host lists any for it. |
| `mcp_tool_format` | How it names an MCP tool, using `{plugin}`, `{server}` and `{tool}` |
| `session_env` | The variable that holds its session id. The lock shows it but never trusts it. |
| `skills_directory` | The repo path it reads a repository's own skills from |
| `attribution` | The commit trailer: `name`, `email`, `model_env` (or `null`), and `model_pattern` (`null` allows any model name); optional `session_trailer` (`{"key", "env", "url"}` with one `{id}` in `url`, or `null`), the session line its commits carry (docs/commit.md) |
| `agent_name_format` | How it names a bundled agent: `{plugin}` once at most, then `{name}` last |
| `session_process` | The process image of a session it launches |
| `env_prefixes` | The environment variable prefixes its sessions own (`CLAUDE_`). A child launched on another host never inherits them (`kitlib.sessionmark.child_environment`). |
| `cli_program` | The name its CLI runs as on Linux (`claude`). |
| `cli_package` | Its npm package's scoped name, `@scope/name` (`@anthropic-ai/claude-code`). With `cli_program` it tells a Linux `node` process that runs the CLI from any other: its script lies in the package directly under a `node_modules` folder (`kitlib.pmstart.runs_cli`). |

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
- `CONTEXT_EVENTS`: the start events, every hook event but `hook.tool_event`.
- `start_text(event_name, kit, payload, environ, part)` (optional): the text one start hook adds,
  when it is more than the kit header (Codex adds a lead's start checks). `part` names one of
  several hooks registered on one event (Claude's coordinator rules and PM checklist are
  SessionStart parts); `None` is the plain hook, whose text begins with the kit header; a part the
  adapter does not have raises. `boot/hook.py` writes exactly this, and `tools/agent_view.py` shows
  it for each part a role runs, so the view is what the hook says. Without it, a start event gets
  the kit header and takes no part.
- `start_parts(event_name, role)` (optional): the parts, `None` for the plain hook, that a session
  of `role` runs at that event: every registration it loads, the host's manifest and any settings
  one role alone gets (Claude: the PM's settings add `pm`). Without it, `(None,)`.
- `DEADLINE` (optional): seconds the guard has for one tool call, the boot included, for a host
  that gives up on a slow hook (Codex: 7, under its manifest's 10 s timeout). None: no deadline.
- `sessions()`: a `SessionHost` with `sessions(cwd)`, `loaded()`, `usage()` and `models()`, read-only.
- `content()`: an object whose `renderer()` is a `kitlib.host.content.ContentRenderer`. It has
  `host`, `tier_models()` (the same map as `hosts.json` `tiers`), `owned()` (folders in the shell
  where it writes every file), `render(content)` for every role (and every skill if the host has
  skills), and `manifests(kit, content)` for the plugin files carrying `kit.json`'s version and the
  hook registration (command lines from `hookmain.manifest_command`). Role bodies fill tokens from
  the host's own table (docs/content.md "Tokens"): every one-value token in `kitlib.host.content.TOKENS`,
  `{{model:<tier>}}` from `tier_models()` and `{{tool:<capability>}}` from its capability map, passed
  to `kitlib.host.content.substitute` (`models=`, `tools=`). For `tools/agent_view.py` it also says
  what it gives one role: `tokens()` (that table, `{}` if its files fill none), `tools()` (the
  capability map, or `None`), `role_path(name)` (the file `render` writes the role to) and
  `settings(role)` (`[(key, value)]`, what that file sets for the role to run with, each value as
  the file writes it: `model` when it names one, `tools` when it limits the role to a tool list).
  The conformance suite checks each against `render`.
- `command_host()`, `launcher()` and `plugin_installer()`: `None` until the host has them.
- `push(settings)` (optional): the Action Hub's push-notification delivery, an object with
  `deliver(recipient, text)` and `is_pm(recipient)` (`kitlib/hubsite/push.py` docstring), over the
  launcher's settings. `deliver` must end within `push.STOP_S`, the hub's close join. `tools/hub.py serve` asks for it with `getattr`; without it the hub serves
  with no push notifications (docs/action-hub.md "Push notifications"; Claude:
  `hosts/claude/adapter/peer.py` `HubPush`).
- `agent_models()` (optional): the subagent model reader `tools/verdict.py add` checks
  `--reviewer-model` with, a function from a reviewer id to the sorted models that subagent's
  transcripts show: `[]` for a subagent id with no transcript found (`add` queues with a NOTE),
  `None` for an id that is not a subagent's (a session name, not looked up). `verdict.py` asks
  for it with `getattr`; without it no reviewer model is checked (Codex; docs/gate.md "Strict
  paths"; Claude: `hosts/claude/adapter/agent_models.py` `models`).
- The renderer's optional `vendored(root)`: `{path: bytes}` of kit files the host's plugin ships a
  copy of, read from the kit checkout `root` and drift-checked with the rest. A plugin root that is
  not the whole kit cannot reach the kit's `boot/` from a plugin cache, so it ships `boot/kitboot.py`
  and `boot/hook.py` this way, and a `hosts.json` holding its own entry beside them (Codex). The
  copy finds the kit through `PM_KIT_ROOT`, or the checkout it sits in.

## 3. The shell: `hosts/<name>/shell/`

- No hook script. The manifest the renderer writes registers each hook event as
  `<interpreter> <root>/boot/hook.py <host> <event>` (`hookmain.manifest_command`), `<root>` the
  plugin root's variable. The entry reads the host's `hosts.json` `hook` data, skips an unguarded
  call before the kit boots, boots the kit (docs/kitboot.md), imports the adapter `hosts.json` names
  and calls `kitlib.host.hookmain.dispatch`: the guard through `hookmain.run` for the tool event,
  which fails closed (any error becomes the host's deny), and `ADAPTER.context` for a start event.
  When the kit will not boot, a tool call exits 2 (a block on every host so far) and a start event
  says so in plain text. The fake host's `plugin.json` runs it this way.
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
- `hook_scripts(manifests)`: returns every `(event, script path, [arguments])` the host registers
- `session_host()`: returns a `SessionHost` over recorded session files, with the ids it should list

The suite checks:
- the registry entry, and the adapter `tools/_host.py` imports
- parse for every case, and subagent identity only where it is declared
- a several-target call denied when any one target is
- render of allow, deny and ask, and the ask lowered through `hookmain.run`
- fail-closed denies
- the kit header at each start event
- sessions, rendered-file drift, and every role rendered
- `tokens`, `tools`, `role_path` and `settings` against what `render` writes, and `start_text`
  (when the adapter has it): the plain hook's text beginning with the kit header and a part it does not
  have refused; `start_parts` (when it has it) the plain hook first, then named parts
- `kit.json`'s version in the manifests
- boot before kitlib in every shell script; every declared event registered, each hook running
  `boot/hook.py` (or the plugin's byte copy) told its host and event; no hook script in the shell
- the `hook` data against the fixtures and the adapter, and `boot/hook.py` run in-process: a
  guarded call judged, an unguarded one skipped before the kit boots, the kit header at each start
- the host's name kept out of kitlib, tools, boot, content and other hosts

One list covers what is left to fix: `LEAK_ALLOWED`. It can only shrink.
`tests/host/test_conformance_gate.py` checks that a change under `hosts/`, `kitlib/host/`,
`content/`, `kit.json` or `hosts.json` runs the suite (tests/selection.json).

## 5. Optional: the bridge half

For agent-to-agent messaging, add `optional/bridge/hosts/<name>/` (the name with `-` as `_`),
which exports `HOST`, an `optional.bridge.port.BridgeHost`, and has a `bridge-profile.json`
(its settings, and as `channel_env` any variable the host's app nominates a tool pipe in). It is
served only once the bridge's own list, `optional/bridge/peers.json`, names it; its `hosts.json`
entry does not serve it. Every pair with the other peers is allowed unless `refused` there lists
it (optional/bridge/README.md "The port", "Host pairs", "Adding a host half"). The core never imports it. With the
bridge not installed, no host shell mentions it. Once the half exists, the conformance class runs the
bridge part of the suite by itself: subclass `bridge_conformance.mixin(HOST)` beside
`conformance.HostConformance` (`tests/host/bridge_conformance.py`), and supply `bridge_delivery()`
for the fake-pipe delivery check.
