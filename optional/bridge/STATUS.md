# Agent bridge: status

What the bridge does today, per host and direction. Design and install are in
`optional/bridge/README.md`. The evidence comes from the source branch's records, as of
`codex/robust-bridge-plan` at its tip when the port was made. The port moved code and changed little behaviour; what it did
change is listed in "Changes from the source".

**Claude and Codex are both served.** The bridge's own `peers.json` lists both, so the service
and `bridge_prepare.py` accept a Codex profile, and the MCP frontend reads Codex's channel variable.
Every pair of the two may be connected except Codex to Codex, which `peers.json` refuses (README
"Host pairs"). Being served is not being deliverable. The limits below still hold:
- Claude to Codex is delivered only through an owner-granted writer lease (README "Writer
  leases"). No live run has shown it.
- Only the Codex desktop app can deliver to a Codex chat.
- The Codex terminal adapter is dead.

**Bottom line:**
- No message has been delivered end to end between two live chats.
- No idle chat has been shown to wake on a message.
- No live correlated reply has come back.
- The service, storage, routes, frontend and helpers are built and tested against owned pipes,
  processes and fixture records.
- The live native checks were read-only probes and one Claude canary. No message was sent.
- The writer lease is tested on fixtures only (limits under "Delivery").

## Senders: who can call the bridge

| Host and form | Attested how | State |
|---|---|---|
| Claude desktop chat | Its signed-in account, its active local task and its live registry record, matched to the frontend's kernel parent | Works on fixtures. A read-only probe matched one real desktop task and its permission class. |
| Claude terminal chat | Only through the bridge's PreToolUse hook (`hosts/claude/hook.py`). The account file alone gives no permission mode. | Works on fixtures. One live canary, run with the source's paths, measured the hook's plugin-scoped provenance shape. Claude's unattended permission flow denied that call, so no live bridge call has run. |
| Codex desktop chat | The native tool metadata, the active turn in its rollout, the creator account matched to the current file login, and the effective sandbox and approval mode | Works on fixtures. A read-only probe verified a real desktop host, chat, turn and mode. |
| Codex terminal chat | The same observer covers terminal rollouts. | Fixture shapes only. No live terminal was open to probe. |

## Delivery: sender → recipient

| From → to | Adapter | State |
|---|---|---|
| any → Claude terminal or desktop | `claude-peer`. It writes Claude's protocol-1 peer packet into the recipient's own pipe and receives status packets on an owned return pipe. | Built. It writes once, and the outcome is **unknown** (`native-outcome-unconfirmed`) until a correlated `held`, `refused` or `expired` packet arrives. Neither acceptance nor a reply is confirmed natively. Return expectations live only for the endpoint's lifetime (512 at most). Waking an idle chat is unproved. |
| Codex → Codex | none | **Refused by design.** The bridge is for Claude and Codex and outside tools; Codex chats message each other through Codex's own messaging. `peers.json` refuses the pair, so the broker refuses it at admission (`host-pair-refused`, nothing queued) and again before any send. Behind that, the Codex half still refuses a Codex sender before writing (`codex-direct-route-unavailable`), and a lease never serves one. |
| Claude → Codex | `codex-desktop`, through an owner-granted **writer lease**: a Codex desktop chat in an active turn carries the message as the native executor | Built, fixtures only. The owner grants it in the window for one recipient until Stop, a PM revoke or either chat's binding being replaced (a turn change or a failed observation ends nothing); it lives in memory, a service restart drops it, and every grant is logged. A writer whose turn has ended is refused (`codex-direct-context-unverified`) until it makes a bridge call in a new active turn. The sender's own route must still be selected, and the writer must be live, in an active turn, on the recipient's account and not the recipient, at every write. Without a lease the adapter refuses before writing (`codex-direct-route-unavailable`). A reply return from another host to a Codex chat needs the lease too. A correlated success means "scheduled" only, never "ran" or "replied". The check after the write accepts the recipient's turn having moved on (the delivery starts a new turn) but still requires the same app process, image and channel, the lease, and the recipient's same rollout file holding the captured turn; it drops no chat from the observed list. A failure there gives **unknown**, its reason `native-outcome-uncertain:<adapter code>`. The tools pipe is a measured private protocol, not a public contract. Both chats must be in the same app, distinct and on the same account. |
| Codex terminal as recipient | `codex-terminal` (`CodexTerminalAdapter`) | **Dead.** It advertises no capability and every send refuses before writing. No verified control channel to the original terminal exists (the default control socket was absent; the experimental queue methods are unproved). The service never installs it. |
| Codex chat's reply → a Codex sender | none | Does not arise: the Codex-to-Codex message it would answer is refused at admission (`host-pair-refused`). A return delivery queued earlier is refused before any send, and the Codex half would refuse it at the write too. |

Routes, durable queueing, receipts, events, refusals, replies and the owner window work the same
for every host. They are core and service mechanics, and they are tested.

Writer lease limits:
- The writer must be in an active turn at every write. A writer whose turn has ended writes
  nothing, so in practice it waits inside a long-running turn.
- The Codex app attributes the message to the writer's chat, and the writer's approval mode
  governs the call, so a prompt may appear in the writer.
- Only Codex desktop chats in the same app and on the same account qualify, never a terminal.
- A restart drops every lease. Leases are capped at 32 at a time.
- A closed chat keeps its lease, and its slot, until Stop, a revoke or a restart; it cannot
  deliver. The window still lists it, marked as not listed, and can Stop it.
- A correlated refusal still blocks the recipient's whole chat, which is its task root.
- The owner window's lease buttons have no test.

PM routes (README "PM routes"): the PM-marked chat may connect two kit-launched chats; built and
tested on fixtures only, with no live run. Limits:
- Only Claude terminal sessions the kit launched carry a mark today, so only they can be PM-routed.
- A subagent of the PM shares the PM's frontend and its `pm` mark.
- An exited marked frontend keeps its chat's mark until the next frame from any frontend reaches
  the service.
- A restart drops every PM route. The owner window's PM rows and its 5 s re-read have no test.
- The owner window lists and connects no Codex-to-Codex route (README "Host pairs").

Idle roster at frontend start: **pending a read-only probe with the owner present.** A chat is
listed today only after its first bridge tool call, except Claude desktop chats, which are listed
when their frontend starts. When a frontend starts, neither half receives a chat identity it can
verify: a Codex binding needs the thread, turn and auto-review flags from a tool call's
`x-codex-turn-metadata`, and a Claude terminal binding needs `permission_mode` from a PreToolUse
hook. Listing Codex chats from rollout files is rejected: those files are tied to no frontend and
the auto-review flags would have to be invented. The probe must capture:
- the Codex desktop frontend's `initialize` params and their `_meta`
- the environment variable names that frontend receives
- whether Codex starts one frontend per thread or one per app

The idle chats the PM could route are kit-launched Claude terminals (Codex chats carry no launch
mark). Listing them before their first bridge call needs a SessionStart or UserPromptSubmit hook
that carries `permission_mode`.

## Install and operation

| Item | State |
|---|---|
| `bridge_prepare.py` | Writes inactive review files: the runtime config, MCP entries, Claude's hook fragment, the startup task and a hashed manifest. The scheduler accepted the task XML with validate-only. |
| Registering the MCP servers and hook in a host | **Manual** (README "Install"). It was never activated by the source. |
| Startup task | Rendered and validated, never registered |
| Service (`bridge_service.py`) | Idle start tested from a relocated copy, with the source's paths: marker, leadership, no chats, no SEND, clean shutdown. Needs re-running on this layout. |
| Codex updates | An update moves both pinned Codex executables: the app package folder holding `ChatGPT.exe` and the `bin\<16 hex>\codex.exe` copy. Preparation and every service start move them to the current install (`hosts/codex/locate.py`; README "Configuration"); tests on fake package trees, and the Windows package lookup checked against the installed app (26.1023.6123). An update while the service runs is not followed: frontends of the new `codex.exe` are refused (`frontend-host-unverified`) until the service restarts. |
| Migrating from the older bridge the owner runs | Not done. Its in-memory sends must be reconciled before any cutover. |
| Linux and macOS | None. Named pipes, ACLs, tokens and job objects (plan section 6). |

## Changes from the source

The port changes these behaviours, all visible to a caller or an operator:
- **Error classes and messages:**
  - The probe's `ProbeError` is now `kitlib.proc.errors.ProcError`, with the message
    `process call refused: <code>`. It was `live probe stopped: <code>`. The codes are unchanged.
    `read_record` raises it too.
  - `identifier`, `digest` and `integer` raise `kitlib.ident.IdentError`, a `ValueError`, with
    the message `value refused: <code>`. They raised `BootstrapError` with
    `bootstrap fixture rejected: <code>`.
- **Prepared manifest:** `manifest.json`'s `source.module` is `optional.bridge.win.install`. It was
  `kitlib.bridgewin.install`.
- **Hook routing:** a native hook frame goes to the one served provider that has a hook sink.
  When none or two have one, the frame is refused (`hook-producer-unavailable`). The source sent
  it to the provider named `claude`.
- **Serving:** a peer's half is used only when the bridge's `peers.json` lists the peer (not
  `hosts.json`: a bridge peer need not be a kit host). Claude and Codex are both listed. A profile
  for an unlisted peer fails the configuration check with `native-provider-unavailable`, and its
  half's files are not read. The source wired Codex whenever a profile named it.
- **Host pairs:** the core refuses a pair `peers.json` lists as refused, at admission and before
  any send (`host-pair-refused`). Codex to Codex is the one refused pair; the source queued such a
  message and the Codex half failed it at the write (`codex-direct-route-unavailable`). Every
  other pair, Claude to Claude included, is unchanged.
- **Channel variables:** these are data in each half's `bridge-profile.json` (`channel_env`), read
  only for served hosts. The source's frontend read `CODEX_APP_TOOLS_PIPE_PATH` unconditionally.
- **Attesting:** the service calls `attest()` on any object a provider returns that has one.
  `NativeSource.attest` is its `observe`. A source whose `prepare` is present but not callable is
  refused (`invalid-native-source`), so client `_meta` never reaches `attest()` unreplaced.
- **Channel variable names** are compared without case, as Windows does: two names that differ
  only by case are refused in one profile and counted once across halves.
- **Paths:** the entry scripts moved:
  - the frontend is `optional/bridge/mcp_frontend.py`
  - the service, prepare, connection and helper entries are in `optional/bridge/tools/`
  - the Claude hook is `optional/bridge/hosts/claude/hook.py`
  - the half profiles are `optional/bridge/hosts/<name>/bridge-profile.json`

  The service verifies these exact paths on every command line. So every installation file
  prepared before the port no longer matches: the MCP entries, the hook fragment, `runtime.json`,
  `start-bridge.ps1` and `startup-task.xml`. Prepare again, and register again.

The source's live evidence was gathered with the old paths. This covers the Claude hook canary
(its plugin-scoped PreToolUse provenance and the hook's kernel ancestry) and the idle start from
a relocated copy. Both need re-running against this layout before they count for it.

## Open before any live use

1. Re-run the Claude hook canary and the relocated idle start on this layout.
2. A live canary from an owner-selected Claude chat to a Claude chat: delivery, wake and a
   correlated reply.
3. A live canary of the writer lease: an owner-selected Claude chat to a Codex desktop chat,
   carried by a leased Codex chat in an active turn. It is built and tested on fixtures only.
4. A verified control channel to the original Codex terminal before `codex-terminal` advertises
   SEND.
5. Precise task ancestry for incoming operations. Codex currently uses the chat as the task root,
   so a refusal blocks that whole chat.
6. Durable return-address recovery and retention for the Claude peer.
7. Owner-present registration in each host, with a rollback, after reconciling the older bridge.
8. A full gate of the combined tree on Windows. The source's last full gate was 6,366 tests in
   335 modules, nine skipped, before its final assembly commits.

## Retest on main

Live round trip: **pending owner lease.** Nothing below the suite line has been run.

- **Suite, 2026-10-25, main after the writer-lease merge:** `python -m unittest discover -s tests/bridge -t .` on
  Windows: 489 tests, OK (199 s). This includes the Claude-to-Codex writer lease code that
  landed after the port. It runs on fixtures, so it proves
  nothing live.
- **Open bridge records:** only the retest record itself (pulled).

Live round-trip plan (the owner grants the lease; no agent can):
1. Prepare and register the bridge for both hosts (README "Install", steps 1 and 2), start the
   service with `--connections`.
2. Round trip: in the window select the Claude chat as sender and a Codex desktop chat as
   recipient, select a second Codex desktop chat in an active turn as writer, click the lease
   button. The Claude chat calls `bridge_send` with a one-line task ("reply with the word
   pong"). Expected: receipt `scheduled`; the Codex chat replies with `bridge_reply`; the Claude
   chat reads it with `bridge_read_reply`. Record each receipt state; "scheduled" alone is not a
   pass.
3. If that passes, one small real delegated task (for example, add one sentence to a scratch
   doc in a throwaway worktree), same path, checked by diff. The lease lasts until it is stopped; the writer must make a bridge call in each new turn before it can deliver.
4. Record the result here. A pass lets coordinators delegate suitable work to Codex workers.

Owner steps (PowerShell; `<repo>` is the kit checkout, `<kit>` the kit folder, `<out>` a new
private folder; `<out>-input.json` follows the half profiles, see README "Configuration"):
1. `Set-Location <repo>`
2. `python -I -B <kit>\optional\bridge\tools\bridge_prepare.py --config <out>-input.json --output <out>`
3. Review `<out>\manifest.json`, then add `<out>\mcp.json` entries: the Claude entry to the
   plugin's MCP config plus `<out>\claude-hooks.json` to its hooks; the `agent-bridge` entry to
   Codex's MCP configuration. Restart both hosts.
4. `python -I -B <kit>\optional\bridge\tools\bridge_service.py --config <out>\runtime.json --connections`
5. In the window: select the sender and recipient routes, select the writer Codex chat (in an
   active turn) and the recipient, click the lease button. Tell the worker to run the round trip.
6. Rollback: stop the service, remove the entries added in step 3.

## Verification of this port

This port was checked on Linux only, with a ctypes shim and no Win32, so nothing was run on
Windows:
- `tests/bridge`: 425 tests, of which 159 pass on Linux. The 266 that fail all open real pipes,
  tokens or processes, and they are exactly the tests that failed the same way on the source
  branch. Checked after merging step 9: there are no other new failing ids.
- `tests/proc/test_pipe.py`: 10 tests; the 9 Windows-only ones skip on Linux.
- `tests/ident`: 7 tests, all passing on Linux.
- The bridge part of the conformance suite runs for Claude and for Codex: 4 tests each. 3 pass
  on Linux, and the fake-pipe delivery test skips there. Codex records no fake recipient pipe yet,
  so that test also skips for Codex on Windows.

Before relying on the port, run the bridge's own packages on Windows:
`python tools/gate.py --full`, or at least `tests/bridge`, `tests/proc` and `tests/host`.
