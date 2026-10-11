"""claude: everything in the kit that is specific to Claude Code (ARCHITECTURE.md "Host split").

Plugin parts the manifest (.claude-plugin/plugin.json) points at: agents/, skills/, hooks/.
Python seams other code imports: claude_host (how Claude Code lays out its files, for
kitlib.pmwatch) and guard_selftest (is Claude Code still sending agent_id). hub/ is the Action Hub
artifact page and the tools that write ArtifactData batches for it. kitlib never imports this
package.
"""
