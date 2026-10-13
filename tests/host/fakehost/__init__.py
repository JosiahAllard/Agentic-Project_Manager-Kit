"""fakehost: a made-up second host that exists only in the tests (docs/new-host.md, the worked
example). It proves the host slot takes a host that is not Claude Code, with no file in kitlib/,
content/ or hosts/ changed: the same conformance suite (tests/host/conformance.py) runs over it.

  host.json        its hosts.json entry; the tests add it to a copy of the kit's hosts.json
  adapter.py       its HostAdapter (parse, render, context, sessions, content renderer, manifests)
  shell/           its shell: hooks/run.py (the one hook entry), the rendered agents/ and plugin.json
  payloads.json    its recorded hook payloads
  sessions/        its recorded session files

Unlike Claude it has no ask, no subagent identity in its hooks, no MCP and no skills, so every
degradation path of the contract is exercised. Re-render its shell after a role change:
python -c "from tests.host.test_conformance_fake import write_shell; write_shell()"
"""
