"""claude.adapter: Claude Code's half of the host contract (kitlib.host; ARCHITECTURE.md "Hosts").

content: the ContentRenderer that writes claude/agents/*.md and claude/skills/** from content/.
events: Claude Code's hook payloads parsed into kitlib.host.Action.
host: ADAPTER, the kitlib.host.HostAdapter (parse, render, profile from hosts.json), re-exported
here since hosts.json names this package as Claude's adapter module.
kitlib never imports this package; an entry point picks it (tools/render_content.py,
claude/hooks/guard.py, tools/_host.py).
"""
from claude.adapter.host import ADAPTER  # noqa: F401
