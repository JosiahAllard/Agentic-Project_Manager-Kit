"""hosts.claude: everything in the kit that is specific to Claude Code (ARCHITECTURE.md "Hosts").

adapter/ (the Python package hosts.json names): the HostAdapter (host), its payload parser
(events), the content renderer (content), Claude's session files for kitlib.pmwatch (claude_host)
and the agent_id self-test (guard_selftest).
shell/ (what Claude Code loads, not a package): hooks/ (the hook entries and the rendered
hooks.json), hub/ (the Action Hub artifact page and the entry points that write ArtifactData batches
for it), and the rendered agents/ and skills/. The plugin manifest stays at the kit root
(.claude-plugin/plugin.json, rendered), since the plugin root is the whole kit. kitlib never imports
this package.
"""
