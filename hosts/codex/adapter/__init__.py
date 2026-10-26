"""hosts.codex.adapter: Codex's half of the host contract (kitlib.host; ARCHITECTURE.md "Hosts").

host: ADAPTER, the kitlib.host.HostAdapter, re-exported here since hosts.json names this package as
Codex's adapter module. The rest of the package is listed in hosts/codex/__init__.py, but
plugin_copy: the Codex plugin copy step ADAPTER.plugin_copy hands plugin_refresh --apply
(docs/plugin-source.md "Host plugin copies").
kitlib never imports this package; an entry point picks it (tools/render_content.py,
boot/hook.py, tools/_host.py).
"""
from hosts.codex.adapter.host import ADAPTER  # noqa: F401
