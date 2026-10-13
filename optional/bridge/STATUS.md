# Agent bridge: status

What the bridge does today, per host and direction. Design and install are in
`optional/bridge/README.md`. The evidence comes from the source branch's records, as of
`codex/robust-bridge-plan` 1cdc244. The port moved code and changed little behaviour; what it did
change is listed in "Changes from the source".

**Only Claude is served until Codex is registered in `hosts.json`.** On this branch `hosts.json`
registers only `claude`. Until plan step 9 adds `codex`:
- the service refuses a Codex profile (`native-provider-unavailable`)
- `bridge_prepare.py` refuses a Codex profile
- the MCP frontend reads no Codex channel variable

Every Codex row below therefore describes the Codex half's code, which the tests run against a
test `hosts.json`. None of it is available from an installed kit until Codex is registered.

**Bottom line:**
- No message has been delivered end to end between two live chats.
- No idle chat has been shown to wake on a message.
- No live correlated reply has come back.
- The service, storage, routes, frontend and helpers are built and tested against owned pipes,
  processes and fixture records.
- The live native checks were read-only probes and one Claude canary. No message was sent.

## Senders: who can call the bridge

| Host and form | Attested how | State |
|---|---|---|
| Claude desktop chat | Its signed-in account, its active local task and its live registry record, matched to the frontend's kernel parent | Works on fixtures. A read-only probe matched one real desktop task and its permission class. |
| Claude terminal chat | Only through the bridge's PreToolUse hook (`hosts/claude/hook.py`). The account file alone gives no permission mode. | Works on fixtures. One live canary, run with the source's paths, measured the hook's plugin-scoped provenance shape. Claude's unattended permission flow denied that call, so no live bridge call has run. |
| Codex desktop chat (not served until registered) | The native tool metadata, the active turn in its rollout, the creator account matched to the current file login, and the effective sandbox and approval mode | Works on fixtures. A read-only probe verified a real desktop host, chat, turn and mode. |
| Codex terminal chat (not served until registered) | The same observer covers terminal rollouts. | Fixture shapes only. No live terminal was open to probe. |

## Delivery: sender → recipient

| From → to | Adapter | State |
|---|---|---|
| any → Claude terminal or desktop | `claude-peer`. It writes Claude's protocol-1 peer packet into the recipient's own pipe and receives status packets on an owned return pipe. | Built. It writes once, and the outcome is **unknown** (`native-outcome-unconfirmed`) until a correlated `held`, `refused` or `expired` packet arrives. Neither acceptance nor a reply is confirmed natively. Return expectations live only for the endpoint's lifetime (512 at most). Waking an idle chat is unproved. |
| Codex desktop → Codex desktop (not served until registered) | `codex-desktop`, through the app's own tools pipe. Both chats must be in the same app, distinct, selected and on the same account, and the sender must have an active turn. | Built. A correlated success means "scheduled" only, never "ran" or "replied". The tools pipe uses a measured private protocol, not a public contract. |
| Claude → Codex | none | **Not deliverable.** The Codex adapter accepts only a Codex desktop sender with an active turn as the native executor (`codex-direct-route-unavailable`). There is no writer lease for another host. |
| Codex terminal as recipient | `codex-terminal` (`CodexTerminalAdapter`) | **Dead.** It advertises no capability and every send refuses before writing. No verified control channel to the original terminal exists (the default control socket was absent; the experimental queue methods are unproved). The service never installs it. |
| Codex chat's reply → a Codex sender | the return delivery over `codex-desktop` | Recorded and readable, but **no native bytes are sent**: a completed Codex reply author has no active executor authority. |

Routes, durable queueing, receipts, events, refusals, replies and the owner window work the same
for every host. They are core and service mechanics, and they are tested.

## Install and operation

| Item | State |
|---|---|
| `bridge_prepare.py` | Writes inactive review files: the runtime config, MCP entries, Claude's hook fragment, the startup task and a hashed manifest. The scheduler accepted the task XML with validate-only. |
| Registering the MCP servers and hook in a host | **Manual** (README "Install"). It was never activated by the source. |
| Startup task | Rendered and validated, never registered |
| Service (`bridge_service.py`) | Idle start tested from a relocated copy, with the source's paths: marker, leadership, no chats, no SEND, clean shutdown. Needs re-running on this layout. |
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
- **Serving:** a host's half is used only when `hosts.json` registers the host. The source wired
  Codex whenever a profile named it.
- **Channel variables:** these are data in each half's `bridge-profile.json` (`channel_env`), read
  only for served hosts. The source's frontend read `CODEX_APP_TOOLS_PIPE_PATH` unconditionally.
- **Attesting:** the service calls `attest()` on any object a provider returns that has one.
  `NativeSource.attest` is its `observe`.
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

1. Register Codex in `hosts.json` (plan step 9). Until then only Claude is served.
2. Re-run the Claude hook canary and the relocated idle start on this layout.
3. A live canary from an owner-selected Claude chat to a Claude chat: delivery, wake and a
   correlated reply.
4. A writer lease that lets a non-Codex sender reach a Codex chat, or a documented decision that
   it never will.
5. A verified control channel to the original Codex terminal before `codex-terminal` advertises
   SEND.
6. Precise task ancestry for incoming operations. Codex currently uses the chat as the task root,
   so a refusal blocks that whole chat.
7. Durable return-address recovery and retention for the Claude peer.
8. Owner-present registration in each host, with a rollback, after reconciling the older bridge.
9. A full gate of the combined tree on Windows. The source's last full gate was 6,366 tests in
   335 modules, nine skipped, before its final assembly commits.

## Verification of this port

This port was checked on Linux only, with a ctypes shim and no Win32, so nothing was run on
Windows:
- `tests/bridge`: 420 tests, of which 153 pass on Linux.
  - 266 of the 267 that fail on Linux open real pipes, tokens or processes, and they are exactly
    the tests that failed the same way on the source branch.
  - The other one is the new Windows-only CLI test for the Codex registration gap.
  - On Windows, the native-channel tests that hand the frontend a Codex app pipe skip until Codex
    is registered: the frontend subprocess reads the kit's real `hosts.json`.
- `tests/proc/test_pipe.py`: 10 tests; the 9 Windows-only ones skip on Linux.
- `tests/ident`: 7 tests, all passing on Linux.
- The bridge part of the Claude conformance suite: 4 tests; 3 pass on Linux and the
  fake-pipe delivery test skips there.

Before relying on the port, run the bridge's own packages on Windows:
`python tools/gate.py --full`, or at least `tests/bridge`, `tests/proc` and `tests/host`.
