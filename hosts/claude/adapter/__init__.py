"""hosts.claude.adapter: Claude Code's half of the host contract (kitlib.host; ARCHITECTURE.md "Hosts").

content: the ContentRenderer that writes hosts/claude/shell/agents/*.md and hosts/claude/shell/skills/** from content/.
events: Claude Code's hook payloads parsed into kitlib.host.Action.
host: ADAPTER, the kitlib.host.HostAdapter (parse, render, profile from hosts.json), re-exported
here since hosts.json names this package as Claude's adapter module.
kitlib never imports this package; an entry point picks it (tools/render_content.py,
hosts/claude/shell/hooks/guard.py, tools/_host.py).
"""
from hosts.claude.adapter.host import ADAPTER  # noqa: F401
