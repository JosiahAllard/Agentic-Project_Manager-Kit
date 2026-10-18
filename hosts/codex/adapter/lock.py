"""The machine lock's MCP server as Codex runs it: its read tools only (from the Codex branch's
lock_adapter.py). hosts/codex/shell/mcp/lock_server.py serves it.

The lock MCP server serves read tools only on every host (docs/lock-mcp.md "No write tools"); the owner changes the
lock outside any agent session, never through this server. A name that is not a read tool (the
retired writes in kitlib.lockmcp.OWNER_ONLY among them) is left out of the list, and a tools/call
of one over JSON-RPC gets -32602, unknown tool: Server.handle refuses it before call() runs. This
wrapper's call() stays as a second fence for a direct in-process call: there such a name is
refused as forbidden.
"""
from kitlib.lockmcp.server import Server, TOOLS

READ_TOOLS = frozenset(TOOLS)
INSTRUCTIONS = 'Read-only view of the PM kit machine lock. Owner writes are unavailable here.'


class ReadOnlyServer(Server):
    def tool_list(self):
        return [tool for tool in super().tool_list() if tool['name'] in READ_TOOLS]

    def call(self, name, args):
        if name not in READ_TOOLS:
            return {'ok': False, 'error': {'code': 'forbidden', 'field': 'name',
                                           'message': 'This server lists lock reads only; ask the PM for a '
                                                      'lock change.'}}, True
        return super().call(name, args)

    def handle(self, message):
        response = super().handle(message)
        if isinstance(message, dict) and message.get('method') == 'initialize' and response:
            response.get('result', {})['instructions'] = INSTRUCTIONS
        return response
