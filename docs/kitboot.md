# kitboot: the check before kitlib

Every host entry runs `boot/kitboot.py` before it imports kitlib (ARCHITECTURE.md "Packaging";
docs/multi-host-plan.md section 2.5). It is standard library only and imports nothing from the
kit: it decides which kit may be imported.

## Which kit runs

| Environment | Kit |
|---|---|
| neither variable set | the source checkout `boot/` is in (today's behaviour) |
| `PM_KIT_ROOT` only | that folder; it must be absolute and must not be a release (a folder with `.kit-package.json`) |
| `PM_KIT_ROOT` and `PM_KIT_RELEASE_ID` | the release at `PM_KIT_ROOT`, verified against the pin first |
| `PM_KIT_RELEASE_ID` only | refused |

The kit must hold `kitlib/__init__.py` and `tools/machine_lock.py`. A pinned release is verified
by its own verifier, `kitlib/kitrelease/bootstrap.py`: kitboot reads it once, checks its hash
against the pinned marker's entry for it, and runs those bytes, which then hash every file of the
release (`kitlib/kitrelease`). A pinned kit writes no bytecode. A kitlib already loaded from any
other folder is refused.

## API

- `boot(environ=None, here=None) -> str`: `locate`, then `bind`. What an entry calls.
- `locate(environ=None, here=None) -> Kit(root, identity, commit)`: the checks above, `sys.path`
  untouched.
- `bind(kit) -> str`: the root first on `sys.path`; refuses a different kitlib already loaded.
- `verify(root, identity, commit=None) -> dict`: the release verifier's result (`root`, `commit`,
  `identity`, `version` from `kit.json`, `ready`).

Every refusal is `BootError`. The entries fail closed on it: the guard exits 2 (a block, the
reason on stderr), a session-start hook says in plain text that the kit did not load, and the lock
MCP server and the hub MCP server exit.

## Who calls it

- `boot/hook.py`, every host's one hook entry, from the folder beside it. It decides an unguarded
  tool (TodoWrite, WebFetch...: one its host's `hosts.json` `hook.guarded_tools` leaves out) before it boots,
  so those calls never pay for a release check. Every host's plugin root is laid out as the kit
  (Codex's is the whole export), so its `boot/` is the kit's own; no host ships a copy of it.
- `mcp/lock_server.py` and `mcp/hub_server.py`.
- The Codex shell's entry scripts (`hosts/codex/shell/scripts/`, for example `kit.py` and `launch.py`) and its MCP servers, through the kit's own `boot/kitboot.py` four folders above each.

Each puts `boot/` on `sys.path` relative to its own file, then calls `kitboot.boot()`. Tests:
`tests/boot`.
