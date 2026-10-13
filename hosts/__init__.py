"""hosts: one folder per AI host the kit runs on (ARCHITECTURE.md "Hosts"; hosts.json).

hosts/<name>/adapter/ is the host's Python adapter (kitlib.host.HostAdapter), imported by the name
hosts.json gives it. hosts/<name>/shell/ is what the host itself loads: hook entries, manifests and
the rendered agents and skills; it is not a Python package. kitlib never imports this package.
"""
