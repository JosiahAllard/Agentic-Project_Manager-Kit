"""The guard as Claude Code's hook reaches it, for the tests: a PreToolUse payload (world.event,
bash, ps) parsed by hosts.claude.adapter into a kitlib.host.Action, then kitlib.guard.decide.

  action(payload)        ADAPTER.parse('PreToolUse', payload)
  decide(payload, ctx)   None for an allow, else the kitlib.host.Decision (verdict, rule, reason,
                         instead): the tests read an allow as None, as they did before the split
  output(payload, ctx)   the (stdout, exit code) Claude Code reads back (ADAPTER.render)
  MCP_PREFIX             the lock MCP's tool prefix on Claude (hosts.json mcp_tool_format)
"""
from hosts.claude.adapter.host import ADAPTER
from kitlib import guard
from kitlib.guard import rules


def action(payload, event_name='PreToolUse'):
    return ADAPTER.parse(event_name, payload)


def decide(payload, ctx=None):
    d = guard.decide(action(payload), ctx)
    return None if d.verdict == 'allow' else d


def output(payload, ctx=None):
    return ADAPTER.render('PreToolUse', guard.decide(action(payload), ctx))


MCP_PREFIX = rules.host_mcp_prefix(ADAPTER.profile)
