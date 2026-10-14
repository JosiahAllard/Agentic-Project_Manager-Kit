"""hosts.codex: everything in the kit that is specific to Codex (ARCHITECTURE.md "Hosts"; README.md).

adapter/ (the Python package hosts.json names):
  the hook half: host (ADAPTER), events (payloads -> Action), policy (Codex's own screen), start
  (start-hook text), content (the renderer), lock (the read-only lock MCP filter);
  the runtime half, which runs kit tools inside a native session's sandbox: kit (the booted kit and
  its roles), sessions (app-server reads, the SessionHost), dispatch (a kit tool as checked argv),
  runtime (bindings, the CommandHost, the broker), controller and launcher (plans only), workflow
  (local task acceptance), session_registry, evidence (captured-hook checks), and the native checks
  run by hand: runtime_probe, command_canary, permissions_canary, workflow_canary.
shell/ (what Codex loads, not a package): plugin.json, mcp.json, hooks/ (the rendered hooks.json and
the one hook entry), mcp/ (the lock server entry), scripts/ (thin CLIs over the runtime half), and the
rendered agents/ and skills/.
kitlib never imports this package.
"""
