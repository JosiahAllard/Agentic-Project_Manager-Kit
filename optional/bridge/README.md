# Agent bridge (optional install)

Messages between chats that are already open on one Windows machine: Claude Code terminal and
desktop chats and Codex desktop chats. The owner picks which chats may talk, in which direction.
A chat sends through MCP tools. The bridge stores each message durably, delivers it once through
the recipient host's native channel, and never resends a message whose outcome is unknown. What
works today, per host and direction, is in `optional/bridge/STATUS.md`. Read it before relying on
any of this: no message has yet been delivered end to end between live chats.

**Claude and Codex are both served.** The bridge serves a peer only when its own list,
`peers.json`, names it and the peer has a half (`registry.py`). That list is apart from the kit's
`hosts.json`: a bridge peer need not be a kit host. It names Claude and Codex. A profile for a peer
that is not listed is refused with `native-provider-unavailable`. The refusal happens while the
configuration is checked, before that half's `bridge-profile.json` is read. Every pair of served
peers may be connected, except a pair `peers.json` refuses (Contracts, "Host pairs"); today the
one refused pair is Codex to Codex. Being served does not make a direction work: STATUS.md has
the per-direction limits. Claude to Codex needs an owner-granted writer lease (Contracts, "Writer
leases"), and the Codex terminal adapter is dead.

This is the bridge from `codex/robust-bridge-plan`, ported as plan step 10
(docs/multi-host-plan.md section 2.4). Its design history, the Phase 0 evidence and the
24 bridge docs it replaces are in git history (that branch, up to its tip when this port was made).

## Layout

| Path | What |
|---|---|
| `core/` | Host-neutral and Win32-free: `messages` (records, `DeliveryError`), `outbox` (durable SQLite store), `dispatch` (`CoreBroker`), `pairing` (owner-selected routes), `hostpairs` (`HostPairs`, the host-pair rule), `owner` (owner decisions), `callers` (one-use caller proofs), `mcp` (the MCP tool server), `prompt` (delivered text header), `jsonwire` (bounded JSON lines), `contracts` (`AdapterDescription`, `Capability`, identities) |
| `port.py` | The port a host's half implements (below) |
| `peers.json` | The bridge's peers (`peers`) and the host pairs it refuses (`refused`, as `[sender, recipient]`) |
| `registry.py` | Which peers are served: `peers.json` names that have a half; `host_pairs()` reads the rule |
| `paths.py` | The entry scripts' kit paths, which the service verifies by command line |
| `win/` | The Windows service: `runtime` (config, assembly, `run_service`), `lifecycle` (supervisor, private state root, service marker), `leader` (singleton mutex and pipe), `controller`, `service` (private RPC handler), `rpc`/`rpcworker`/`worker`/`transport` (owned helper processes over named pipes), `frontend` (the MCP stdio frontend and its kernel verification), `channel` (an inherited native app channel), `hosts` (native image sets), `evidence` (bounded stable file reads), `install` (installation preparation), `startup` (startup task), `owner_view`, `launch` (`-I -B`) |
| `ui/window.py` | The owner's connection window (tkinter) |
| `mcp_frontend.py` | The MCP server entry a host runs: `python -I -B <kit>/optional/bridge/mcp_frontend.py <marker>` |
| `tools/` | `bridge_service.py` (the service), `bridge_prepare.py` (installation), `bridge_connections.py` (open the owner window), and the two private helper entries `bridge_rpc_worker.py` and `bridge_pipe_worker.py` |
| `hosts/<name>/` | A host's half: its `HOST`, `bridge-profile.json` and native code |

Layer rules:
- The core kit never imports `optional/` (tests/host/test_layering.py).
- The bridge imports from the kit only `kitlib.ident`, `kitlib.proc` (`pipe`, `errors`,
  `process`, `job`), `kitlib.sessionmark` (the frontend's launch mark) and, in the Claude half,
  `hosts.claude.adapter.peer` (the peer wire format and pipe write). It does not read
  `hosts.json`. Its Win32 code is in `win/`, the halves and `kitlib/proc`.
- `core/` names no host and touches no Win32. No bridge code outside `hosts/` names a host
  (tests/bridge/test_port.py); host names outside the halves are data, in `peers.json`. The
  service loops over the served halves.

## The port (`port.py`)

The seam that the scaffold's `ports.py` only sketched, declared from what the service actually
calls. A half's package exports `HOST`, a `BridgeHost`:

| Field | Called | Must |
|---|---|---|
| `provider(controller, native_images, *, host_id, **profile)` | once per service start | return a Provider (below), from the half's validated `bridge-profile.json` settings |
| `delivery(provider, runtime)` | after every provider is built, before the service marker is published | return a `DeliveryAdapter`, or be `None` for a half that cannot deliver |
| `install(profile, *, python, marker, kit_root)` | by `bridge_prepare.py` | return an `InstallFragment`: the MCP server entry's name and metadata, files to add (such as a hook registration), and the paths they depend on |

Beside it, the half's `bridge-profile.json` is data, read without importing the half. It has the
keys `required`, `optional`, `directories` and `names` (the provider's settings) and an optional
`channel_env`: the environment variables in which the host's app may nominate its own tool pipe to
the MCP frontend it launches.

A Provider is called with each verified MCP frontend (`win.frontend.NativeFrontend`) and returns
a `SenderAttestor` for it. The service calls `attest()` on whatever it returns, and also
`prepare()` when that exists. `port.NativeSource(observe, prepare)` is the usual one, and a plain
callable is taken as `attest` itself. The Provider's other methods are optional, and the service
uses only those it finds:
- `sender_mode(binding)`: `'prompting'` or `'bypass'`
- `hook_sink`: a `HookSink`
- `refresh()`
- `close()`

| Protocol | Method | Contract |
|---|---|---|
| `SenderAttestor` | `attest(caller_proof) -> SenderAuthority` | The sender behind one MCP call, from private metadata the frontend received from its native parent. It never comes from tool arguments. Anything else, or a raise, refuses the call. |
| `DeliveryAdapter` | `describe() -> AdapterDescription` | Gives the adapter id, the contract version and the capabilities. The core sends only through an adapter that names the recipient's adapter id and `SEND`. |
| | `send(message) -> NativeResult` | Writes once. `BeforeWriteError` means no byte was written. Any other raise is an unknown outcome, which is never resent. |
| | `reconcile(message, native_id)` | Reads an earlier send's native receipt without resending. `None` means no news. |
| `ReturnChannel` | `close()` | An owned endpoint for replies or receipts. `port.OwnedDelivery(adapter, channel, ...)` closes it with the adapter. |
| `HookSink` | `(peer, payload)` | Takes a native hook event from a verified helper process. The service routes the frame to the one provider that has a sink. Two sinks refuse the frame. |

`registry.py` serves a peer when `peers.json` lists it and `optional/bridge/hosts/<module>/`
exists. The module is the peer name with `-` replaced by `_`. A folder for an unlisted peer is
never imported or read. A `peers.json` that cannot be read or is malformed refuses with
`invalid-bridge-peers`. The MCP frontend reads only the served halves' `channel_env`, from their
`bridge-profile.json`, and imports no host code. A served half whose profile cannot be read
nominates no channel, and the other halves keep working.

## Contracts

- **Owner-selected routes** (`core/pairing.py`, `core/owner.py`). A chat is registered only through
  a provider's verified observation, and registration alone connects nothing. The owner selects
  each route, one-way or both ways, in the connection window. Every choice is one-use, short-lived
  and bound to the exact chat reference. A route is remembered for the same logical chat (session,
  native id, adapter, account, workspace) and restored when it reopens. A queued message keeps its
  original full binding and is refused rather than moved onto a reopened process. Display titles
  grant nothing. No MCP tool writes owner policy. Beside the owner's routes the only other way a
  route opens is a PM route (below), which writes no owner choice.
- **Host pairs** (`core/hostpairs.py`, `peers.json`). Which host may send to which, checked in the
  core and named by no code: a chat belongs to the peer its adapter id names (the name, or the
  name and `-`; the longest listed name wins). Every ordered pair of listed peers is allowed,
  self pairs included, unless `refused` lists it, so a newly listed peer reaches every other one
  with no pair written down. An adapter id no peer names is outside the rule. The service's broker
  refuses a refused pair at admission (`host-pair-refused`, nothing queued) and again before any
  send, so a message queued before the rule held is refused (`host-pair-refused`) without reaching
  an adapter. `refused` holds `["codex", "codex"]`, the owner's decision; the Codex half's own
  checks below stay as a backstop at the write.
- **Writer leases** (`core/owner.py` `WriterLeases`, `hosts/codex/runtime.py`). A Codex chat is
  written only by a Codex desktop chat in an active turn, which is the native executor. A sender
  of another host has none, so the owner may lend one: in the window, select a Codex desktop chat
  (the writer) and then the Codex recipient, and click the lease button. The lease:
  - names one writer for one recipient, and a new grant for that recipient replaces the old one
  - has no expiry: it ends when the owner clicks Stop, the PM revokes it (`OwnerControls.revoke`,
    a private port), the service restarts, or either chat's session is observed with a different
    binding (for example a permission change). A failed live check, a turn change or a chat missed
    by one discovery pass ends nothing: the write-time checks below refuse while the chat is away.
    A closed chat keeps its lease (it cannot deliver, and it still holds one of the 32 slots)
    until Stop, a revoke or a restart. The window lists every lease with its grant time, marks one
    whose chat is not listed now, and its "Stop selected" stops it by reference
  - is logged: every grant, revoke and replacement appends one JSON line (`at`, `event`, `by`,
    `writer`, `recipient`; chat references only) to `writer-leases.log` beside `pairings.sqlite`.
    A grant that cannot be logged is refused, and a writer it would have replaced keeps its lease
  - is granted with a one-use owner proof; a route choice's proof never grants a lease, and a
    lease's proof never selects a route
  - lives in the service's memory only, so a restart drops it; only its log is written to disk
  - is never an MCP tool, and no agent can grant, renew or read it
  - selects no route: the sender's own route to the recipient must still be selected

  At every write the writer must hold the lease, be live, be in an active turn, be on the
  recipient's account and not be the recipient, or the send refuses before any byte. A reply
  return from another host to a Codex chat uses the same lease. A Codex sender never uses a lease: the bridge does not carry
  Codex-to-Codex. The host-pair rule refuses it first (`host-pair-refused`); the half still
  refuses it at the write (`codex-direct-route-unavailable`). The window's lease buttons have no test.
- **Caller proofs** (`core/callers.py`). For each call, the frontend's attestor turns private
  metadata into a sender and task root, bound to that exact intent. A proof is used once and lasts
  5 seconds. The sender and task root are never tool arguments.
- **Durable outbox** (`core/outbox.py`, `core/dispatch.py`). Admission persists the whole body
  before any send. The operation id is deduplicated against its full original context. Dispatch
  re-attests both live bindings and the route, then writes inside a fenced transaction. A failure
  after the write, or a malformed receipt, becomes `unknown`. A refusal before the write keeps
  the adapter's code as its reason when the code is an identifier, and `native-before-write`
  otherwise. `unknown`, `held` and dispatched operations are never resent. After a restart, work
  that was in flight becomes `unknown`. A correlated native refusal is kept for its task root and
  blocks its queued siblings and later ids.
- **Replies** (`bridge_reply`, `CoreBroker.reply_to`). Only the operation's exact recipient may
  reply, and only over the reverse route the owner selected. A reply creates its own linked return
  delivery, so the original receipt still means only "reply recorded". Without a reverse route the
  reply is recorded but its return is permanently suppressed.
- **Native semantics**. The `held`, `expired` and `refused` states each keep their meaning, and
  none is inferred from silence. Delivered text carries the operation id and a reply instruction
  (`core/prompt.py`). Native permissions and approval prompts stay with each chat.
- **Codex delivery timing** (`NativeToolsPipe` in `hosts/codex/delivery_transport.py`). Each send
  runs one helper in two bounded phases. The start phase (helper spawn, its handshake and the
  host's re-attestation) has 3 s; a miss there fails before the write, since no request byte has
  left the helper. The response phase has 5 s counted from the commit, so a slow helper start never
  spends it; a miss there is uncertain and becomes `unknown`. A send reaches its outcome within
  8 s and ends, kill cleanup included, within 11.5 s, inside the service's 12 s worker join
  (`run_service` in `win/runtime.py`).
- **Service** (`win/`). There is one service per user and state root. It holds a thread-owned mutex
  and an exclusive current-user pipe, and its state root is private by ACL readback. It publishes
  `service.json` (pipe, pid, creation time) only after assembly succeeds. The frontend verifies the
  pipe server's kernel identity. The service verifies:
  - the helper's exact isolated command line
  - the frontend's exact command line
  - that the frontend's parent is one of the configured native images

  JSON identity claims grant nothing. Helpers run in job objects and are killed as a tree. A
  restarted service is rejoined by replaying only the frontend's initialize handshake; a tool call
  is never replayed.
- **Launch mark** (`mcp_frontend.py`, `win/frontend.py`, `Controller.launched`). A session the kit
  launched carries its kind in `PM_KIT_ROLE` (`kitlib/sessionmark.py`). The frontend reads it once,
  at start, so a later change to the session's environment reaches no frame. It sends the mark
  beside `channel` on every frame, the replayed handshake included. A value that is not a plain
  name (letters, digits, `-` and `_`, at most 64) is not sent. The service binds the mark into that
  frontend's `NativeFrontend` and refuses a frame that changes or drops it. Every authority the
  frontend attests carries it as `SenderAuthority.launched`. `Controller.launched(binding)` gives it
  only for the exact live chat that frontend attested last, and only while that frontend is
  attached; an unmarked frontend attesting the same chat clears it. A frame without `mark` is still
  served, so a frontend started before the mark existed keeps working until its host restarts. The
  reverse is refused: a service started before the mark refuses a new, marked frontend's frames as
  `invalid-private-request`, so restart the service after updating the kit. Unmarked sessions are
  unaffected. The mark's one use is PM routes (below): the `pm` mark lets a chat call the PM tools,
  and any mark makes a chat one the PM may connect.
  - Claude passes its MCP servers the session's whole environment, so a kit-launched Claude
    session's frontend is marked. Codex passes only `env_vars`, and the installer forwards no
    `PM_KIT_ROLE`. No kit launch starts a Codex chat yet, so every Codex chat is unmarked and needs
    the owner's click.
  - Claude Desktop chats are never marked: the kit launches none.
  - A mark belongs to one frontend process, so to one launched window.
  - The mark is only as strong as this bridge-only boundary (`core/owner.py`). Any process running
    as the same user can start a frontend with any `PM_KIT_ROLE`, or set another process's
    environment before it starts. The mark tells kit launches apart from the owner's own chats; it
    is no defence against code running as the owner.
- **PM routes** (`core/pmroutes.py`, `Controller.pm`, `OwnerControls.pm_routes` and `stop_pm`). The
  PM may connect two kit-launched chats without the owner's click; every other chat still needs it.
  - Only a chat whose authority carries the `pm` launch mark (`PM_MARK`, the launcher's
    `kitlib/launcher/window.py` `PM`) may call `bridge_pm_chats`, `bridge_pm_connect` or
    `bridge_pm_disconnect`; any other caller gets `pm-launch-mark-required`. No tool argument
    names a mark or a caller.
  - Both chats must be listed, live and kit-launched now (`Controller.launched` gives a mark), or
    the connect is refused (`sender-not-kit-launched`, `recipient-not-kit-launched`). The host-pair
    rule still applies, so Codex to Codex stays refused (`host-pair-refused`).
  - The owner's own row for a direction always wins. A direction the owner disconnected, by
    Disconnect chats or by Stop on a PM route, is refused to the PM (`owner-disconnected`) until
    the owner connects it. A direction the owner connected is reported as the owner's and the PM
    makes nothing there.
  - A PM route is held for the exact chat bindings, in the service's memory only: it writes no row
    to `pairings.sqlite`, a restart drops it, and a different binding for either chat's session
    ends it. At most 64 at a time.
  - At every use (admission, dispatch, replies) the route opens only while both chats are still
    kit-launched: a chat attested by an unmarked frontend, or whose frontend detached, refuses.
  - The PM removes only routes it made (`route-not-made-by-pm` for any other).
  - Logged: every connect, disconnect, owner stop, replacement and refused connect or disconnect
    appends one JSON line (`at`, `event`, `by`, `caller`, `sender`, `recipient`, and `sender_mark`
    and `recipient_mark` on a connect, `action` and `code` on a refusal; chat references only) to
    `pm-routes.log` beside `pairings.sqlite`. A connect that cannot be logged is refused.
  - The owner window lists every PM route the owner's row does not decide ("A → B, connected by
    the PM since …", with "(cannot deliver now)" when a chat is not listed or no longer
    kit-launched) and re-reads the list every 5 s without discovery. "Stop selected" stops one
    direction and writes the owner's disconnect for it, even when a chat is not listed. The
    window's PM rows have no test.
  - The PM agent and its subagents share one frontend, so a subagent the PM starts carries the
    `pm` mark and may call the PM tools too.
  - The service detaches a frontend whose process has ended only when the next frame from any
    frontend arrives (`win/service.py`). Until then an exited marked frontend's chat keeps its mark,
    so a PM route to it still opens; the chat's live checks are then the only guard.
  - The new tools change the Claude half's tool list, so prepare again (`bridge_prepare.py`) and
    re-register `claude-hooks.json`. The Codex half does not offer the PM tools.
- **MCP tools**:
  - `bridge_connections`
  - `bridge_send` (a queued receipt is not delivery)
  - `bridge_receipt`
  - `bridge_read_reply`
  - `bridge_reply`
  - `bridge_events`
  - `bridge_poll_native` (refreshes a receipt and never resends)
  - `bridge_pm_chats`, `bridge_pm_connect`, `bridge_pm_disconnect` (PM only, above)

## Configuration

`runtime.json` (protocol 1) holds these keys:
- `kit_root`: must be the kit the service runs from
- `python`: must be the running interpreter
- `state_root`, `host_id`
- `native_hosts`: kind → one or up to 8 absolute executable images. An image may belong to only
  one kind.
- `profiles`: kind → settings

Every kind must have a half. Its settings must match the half's `bridge-profile.json`, which lists
the required and optional settings, which are directories and which are names. A path must be
absolute and regular. A name is limited to `[A-Za-z0-9_-]` with no `__`. The configuration cannot
name a module, a caller, an owner approval or an adapter.

## Install

Installing the bridge adds nothing to the kit's host shells. With the bridge not installed, no
host shell, plugin manifest or settings file mentions it (tests/bridge/test_port.py checks the
shells).

1. **Prepare.** Run
   `python -I -B <kit>/optional/bridge/tools/bridge_prepare.py --config <input.json> --output <new folder>`.
   It writes inactive review files into a new private folder and touches no native profile, state
   or task:
   - `runtime.json`
   - `mcp.json`: each host's MCP server entry. The command is the isolated frontend command line
     above, with the marker's absolute path. The name and metadata come from the half's
     `install()`. Each entry includes `env_vars` only for that host's declared `channel_env`
     names; current environment values are never copied. This forwards a host-nominated channel
     to its frontend. It grants no writer lease (only the owner's click in the window does) and
     does not prove live delivery readiness.
   - each half's own files, such as `claude-hooks.json`
   - `start-bridge.ps1` and `startup-task.xml`: a per-user, least-privilege task, not registered
   - `manifest.json`: every file's hash, every dependency and every bound path, marked
     `prepared,notactivated`
2. **Register in each host.** The owner adds that host's `mcp.json` entry, and any hook file, to
   the host's own configuration. The halves say what goes where:
   - Claude: the server is a plugin server. Its `plugin_name` and `scoped_name` must be the plugin
     that carries it, because the provider checks the plugin-scoped tool names. The PreToolUse hook
     in `claude-hooks.json` goes in that plugin's (or the user's) hooks.
   - Codex: the `agent-bridge` server goes in its MCP configuration. Codex has no bridge hook.
3. **Start.** Register `startup-task.xml`, or run
   `python -I -B <kit>/optional/bridge/tools/bridge_service.py --config <prepared>/runtime.json`.
   Add `--connections` to open the owner window. `bridge_connections.py --marker <marker>` reopens
   it later.

Step 2 is deliberately manual. The source kept registration disabled until live acceptance, and
nothing has passed it (STATUS.md). Writing the entries into settings automatically, the way
machine setup adds the guard, is a small step once the owner accepts a live canary: the halves
already render the exact entries. A relocated kit needs a new preparation, because every path is
absolute.

## Adding a host half

Add `optional/bridge/hosts/<module>/` with:
- `__init__.py` exporting `HOST = port.BridgeHost(...)`, with the native modules imported lazily
- `bridge-profile.json`, with `channel_env` if the host's app nominates a tool pipe
- the provider, and optionally the delivery adapter

The half is served only once `peers.json` lists the peer (docs/new-host.md). A peer need not be
a kit host in `hosts.json`. Its pairs with every other peer are allowed unless `refused` lists
them, and `tests/bridge/test_host_pairs.py` walks them all with no change to the test.

Its tests go in `tests/bridge/`. The host's conformance class then runs the bridge part of the
suite by itself (`tests/host/bridge_conformance.py`, through `bridge_conformance.mixin(HOST)`).
The suite checks:
- the half's `HOST`, its profile schema and its install fragment
- on Windows, its delivery adapter's `describe` and `send` on a fake recipient pipe, when the
  conformance class supplies `bridge_delivery()`

## What was deleted, and what moved

The Phase 0 experiments and the never-wired scaffold were deleted once nothing imported them:
- `kitlib/bridgeprobe/`: AppContainer, enrollment, handle hand-off, leadership, worker broker,
  workspace and the probe runtime
- `claude/bridge_phase0.py`
- `tools/bridge_live_probe.py`, `tools/bridge_schema_probe.py` and `tools/bridge.py`
- `kitlib/bridge/{bootstrap,journal,pipeline,restart,delegation,schema_probe,ports,disabled,status,errors}.py`
- the tests of all of the above

tests/bridge/test_port.py keeps them deleted.

Four helpers were load-bearing and moved instead:

| Was | Now |
|---|---|
| `kitlib/bridgeprobe/winpipe.py` | `kitlib/proc/pipe.py`. Its probe-only JSON `send` and `frames_until_close` went to the test helper `tests/bridge/pipes.py`. |
| `bridgeprobe.runtime.ProbeError` | `kitlib.proc.errors.ProcError` |
| `bridge/bootstrap.identifier`, `digest` and `integer` | `kitlib/ident.py` (`IdentError` replaces `BootstrapError` for these) |
| `claude/bridge_phase0.read_record` | `hosts/claude/records.py` |

`codex/pmkit_codex/bridge_client.py` is now `hosts/codex/client.py`, and the bridge owns it. It is
the Codex plugin's client of this frontend. The Codex host (`hosts/codex/`, plan step 9) keeps no copy of
it. If its diagnostics ever probe the bridge, they import `optional.bridge.hosts.codex.client`
lazily and optionally. The legacy `session_delivery.py` helper was not ported, since nothing in
the bridge uses it.

## Tests

`tests/bridge/` covers the core, the service, both halves and the port. `tests/proc/test_pipe.py`
covers the promoted pipe helpers, and `tests/ident/` covers `kitlib.ident`. Most of these tests use
real named pipes, job objects and process tokens, so they run on Windows only (STATUS.md
"Verification").
