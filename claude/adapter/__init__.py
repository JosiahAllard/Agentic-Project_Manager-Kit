"""claude.adapter: Claude Code's half of the host contract (kitlib.host; ARCHITECTURE.md "Hosts").

content: the ContentRenderer that writes claude/agents/*.md and claude/skills/** from content/.
kitlib never imports this package; an entry point picks it (tools/render_content.py).
"""
