"""The machine lock's MCP server as Codex runs it: its read tools only (from the Codex branch's
lock_adapter.py). hosts/codex/shell/mcp/lock_server.py serves it.

Codex has no ask, so an owner's lock change through MCP cannot be put to the owner for approval;
on Codex the lock is changed through the lock CLI or the owner's own tools, never this server. The
write tools are left out of the list and refused if called; the read tools behave as on every host.
"""
from kitlib.lockmcp.server import Server, TOOLS

READ_TOOLS = frozenset(name for name, (_desc, _schema, write) in TOOLS.items() if not write)
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
