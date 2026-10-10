"""The lock MCP server's logic (ARCHITECTURE.md, Scheduler UI): MCP tools over kitlib.lock.api.
docs/lock-mcp.md is the contract; mcp/lock_server.py is the stdio entry point.

The names here import nothing, so the guard can read them without loading the server."""

# The Claude plugin's name, as .claude-plugin/plugin.json gives it (tests/lockmcp checks the two
# agree). Claude-specific: another host's plugin has its own name over the same kit.
CLAUDE_PLUGIN_NAME = 'claude-pm-kit'
# The lock MCP server's key under the plugin's mcpServers, and the name it reports.
SERVER_NAME = 'machine-lock'


def claude_tool_prefix(plugin=CLAUDE_PLUGIN_NAME, server=SERVER_NAME):
    """The Claude plugin's MCP prefix: the one Claude Code gives a plugin MCP server's tools,
    mcp__plugin_<plugin>_<server>__. INFERRED from Claude Code's plugin naming; no real
    PreToolUse payload has been seen yet."""
    return 'mcp__plugin_%s_%s__' % (plugin, server)
