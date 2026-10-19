# Agent bridge: status

What the bridge does today, per host and direction. Design and install are in
`optional/bridge/README.md`.

**Claude and Codex are both served.** `hosts.json` registers both, so the service
and `bridge_prepare.py` accept a Codex profile, and the MCP frontend reads Codex's channel variable.
Being served is not being deliverable. The limits below still hold:
- Claude to Codex cannot be delivered.
- Only the Codex desktop app can deliver to a Codex chat.
- The Codex terminal adapter is dead.

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
| Claude terminal chat | Only through the bridge's PreToolUse hook (`hosts/claude/hook.py`). The account file alone gives no permission mode. | Works on fixtures. No live bridge call has run. |
| Codex desktop chat | The native tool metadata, the active turn in its rollout, the creator account matched to the current file login, and the effective sandbox and approval mode | Works on fixtures. A read-only probe verified a real desktop host, chat, turn and mode. |
| Codex terminal chat | The same observer covers terminal rollouts. | Fixture shapes only. No live terminal was open to probe. |

## Delivery: sender → recipient

| From → to | Adapter | State |
|---|---|---|
| any → Claude terminal or desktop | `claude-peer`. It writes Claude's protocol-1 peer packet into the recipient's own pipe and receives status packets on an owned return pipe. | Built. It writes once, and the outcome is **unknown** (`native-outcome-unconfirmed`) until a correlated `held`, `refused` or `expired` packet arrives. Neither acceptance nor a reply is confirmed natively. Return expectations live only for the endpoint's lifetime (512 at most). Waking an idle chat is unproved. |
| Codex desktop → Codex desktop | `codex-desktop`, through the app's own tools pipe. Both chats must be in the same app, distinct, selected and on the same account, and the sender must have an active turn. | Built. A correlated success means "scheduled" only, never "ran" or "replied". The tools pipe uses a measured private protocol, not a public contract. |
| Claude → Codex | none | **Not deliverable.** The Codex adapter accepts only a Codex desktop sender with an active turn as the native executor (`codex-direct-route-unavailable`). There is no writer lease for another host. |
| Codex terminal as recipient | `codex-terminal` (`CodexTerminalAdapter`) | **Dead.** It advertises no capability and every send refuses before writing. No verified control channel to the original terminal exists (the default control socket was absent; the experimental queue methods are unproved). The service never installs it. |
| Codex chat's reply → a Codex sender | the return delivery over `codex-desktop` | Recorded and readable, but **no native bytes are sent**: a completed Codex reply author has no active executor authority. |

Routes, durable queueing, receipts, events, refusals, replies and the owner window work the same
for every host. They are core and service mechanics, and they are tested.

## Install and operation

| Item | State |
|---|---|
| `bridge_prepare.py` | Writes inactive review files: the runtime config, MCP entries, Claude's hook fragment, the startup task and a hashed manifest. The scheduler accepted the task XML with validate-only. |
| Registering the MCP servers and hook in a host | **Manual** (README "Install"). |
| Startup task | Rendered and validated, never registered |
| Service (`bridge_service.py`) | Idle start not yet run on this layout. |
| Migrating from the older bridge the owner runs | Not done. Its in-memory sends must be reconciled before any cutover. |
| Linux and macOS | None. Named pipes, ACLs, tokens and job objects (plan section 6). |

## Open before any live use

1. Re-run the Claude hook canary and the relocated idle start on this layout.
2. A live canary from an owner-selected Claude chat to a Claude chat: delivery, wake and a
   correlated reply.
3. A writer lease that lets a non-Codex sender reach a Codex chat, or a documented decision that
   it never will.
4. A verified control channel to the original Codex terminal before `codex-terminal` advertises
   SEND.
5. Precise task ancestry for incoming operations. Codex currently uses the chat as the task root,
   so a refusal blocks that whole chat.
6. Durable return-address recovery and retention for the Claude peer.
7. Owner-present registration in each host, with a rollback, after reconciling the older bridge.
8. A full gate of the combined tree on Windows (`tests/bridge`, `tests/proc`, `tests/host` at least).
