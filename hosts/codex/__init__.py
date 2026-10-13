"""hosts.codex: everything in the kit that is specific to Codex (ARCHITECTURE.md "Hosts"; README.md).

adapter/ (the Python package hosts.json names): the HostAdapter (host), its payload parser
(events), its own hook policy (policy), the start-hook text (start), the content renderer
(content), the read-only lock MCP filter (lock), session reads over the local app-server
(sessions), and the runtime half that runs kit tools inside a native session's sandbox (runtime,
dispatch, controller, launcher, workflow, registry, evidence; canaries for the native checks).
shell/ (what Codex loads, not a package): plugin.json, mcp.json, hooks/ (the rendered hooks.json
and the one hook entry), mcp/ (the lock server entry), and the rendered agents/ and skills/.
kitlib never imports this package.
"""
