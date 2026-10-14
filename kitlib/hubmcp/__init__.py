"""kitlib.hubmcp: the Action Hub as MCP tools (docs/hub-mcp.md).

server.py holds the tools and the JSON-RPC; mcp/hub_server.py serves them over stdio. Nothing here
is imported at load: the guard reads these names without loading the server.

SERVER_NAME     the server's name in the plugin manifest; a client shows its tools as
                mcp__plugin_<plugin>_hub__<tool>
OWNER_ONLY      the tool names pinned for the owner's hub writes. None of them ships yet: tools/list
                never lists one and tools/call refuses it. They land together with the guard change
                that reads these names and denies them to agents.
OWNER_ROUTES    each OWNER_ONLY name -> (method, path below kitlib.hubsite.api.BASE): the owner route
                of kitlib.hubsite.api.ROUTES it is to call
"""

SERVER_NAME = 'hub'

OWNER_ONLY = ('hub_answer', 'hub_todo_done', 'hub_story_submit', 'hub_to_pm_send')

OWNER_ROUTES = {
    'hub_answer': ('POST', '/questions/{id}/answer'),
    'hub_todo_done': ('POST', '/todos/{id}/done'),
    'hub_story_submit': ('POST', '/stories'),
    'hub_to_pm_send': ('POST', '/to_pm/send'),
}
