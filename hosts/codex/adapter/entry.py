"""What Codex's hook entry (hosts/codex/shell/hooks/run.py) does once the kit has booted.

  run(event, kit, text, stdout, entry, deadline) -> exit code
      a start event (ADAPTER.CONTEXT_EVENTS): the kit header and a lead's start checks as context
      (hosts.codex.adapter.start), exit 0; any other: kitlib.host.hookmain.run with ADAPTER over the
      payload `text`, `deadline` seconds for the whole call.
"""
import io

from kitlib.host import hookmain

from hosts.codex.adapter import ADAPTER, start


def run(event, kit, text, stdout, entry, deadline, started=None):
    if event in ADAPTER.CONTEXT_EVENTS:
        stdout.write(start.output(ADAPTER, event, kit, io.StringIO(text)))
        return 0
    return hookmain.run(ADAPTER, event, io.StringIO(text), stdout, entry=entry, deadline=deadline, started=started)
