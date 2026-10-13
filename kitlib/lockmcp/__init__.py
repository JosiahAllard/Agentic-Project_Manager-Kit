"""The lock MCP server's logic (ARCHITECTURE.md, Scheduler UI): MCP tools over kitlib.lock.api.
docs/lock-mcp.md is the contract; mcp/lock_server.py is the stdio entry point.

The names here import nothing at load time (the host registry only when a prefix is asked for), so
the guard can read them without loading the server."""

# The kit plugin's name, as .claude-plugin/plugin.json gives it (tests/lockmcp checks the two
# agree); every host's shell names the kit's plugin so.
PLUGIN_NAME = 'pm-kit'
# The lock MCP server's key under the plugin's mcpServers, and the name it reports.
SERVER_NAME = 'machine-lock'


def tool_prefix(host=None, plugin=PLUGIN_NAME, server=SERVER_NAME, path=None):
    """What a plugin MCP server's tool names start with on `host` (a hosts.json name; None: the
    default host): its profile mcp_tool_format up to {tool}, filled in with `plugin` and
    `server`. On Claude, mcp__plugin_<plugin>_<server>__: INFERRED from Claude Code's plugin
    naming; no real PreToolUse payload has been seen yet."""
    from kitlib.host import registry
    name = registry.default(path) if host is None else host
    fmt = registry.host(name, path).profile.mcp_tool_format
    return fmt.split('{tool}', 1)[0].format(plugin=plugin, server=server)

