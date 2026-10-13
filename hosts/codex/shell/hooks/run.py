"""Codex's one hook entry (hooks.json): `run.py <event> < payload.json`.

PreToolUse goes to kitlib.host.hookmain.run with hosts.codex.adapter's ADAPTER: Codex's deny JSON, or
nothing for an allow. A start event (SessionStart, PostCompact, SubagentStart) adds the kit header
and a PM's or coordinator's start checks to the context (hosts.codex.adapter.start). The kit boots
first (boot/kitboot.py): this checkout's boot/, else, in a plugin copied out of the kit, the one in
PM_KIT_ROOT. A PreToolUse that cannot boot, import or answer exits 2, which Codex reads as a block;
a start event says in plain text that the kit did not load.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
BOOT = (os.path.join(HERE, '..', '..', '..', '..', 'boot'), os.path.join(os.environ.get('PM_KIT_ROOT') or HERE, 'boot'))
sys.path.insert(0, next((b for b in BOOT if os.path.isfile(os.path.join(b, 'kitboot.py'))), BOOT[1]))


def main(argv, stdin, stdout):
    event = argv[0] if argv else 'PreToolUse'
    try:
        import kitboot
        kit = kitboot.boot()
        from hosts.codex.adapter import ADAPTER, start
        from kitlib.host import hookmain
        if event in ADAPTER.CONTEXT_EVENTS:
            return stdout.write(start.output(ADAPTER, event, kit, stdin)) and 0
        return hookmain.run(ADAPTER, event, stdin, stdout, entry=os.path.abspath(__file__))
    except BaseException as e:                  # a guarded call blocks by exit code; a start event says why
        return 2 if event == 'PreToolUse' else stdout.write('PM kit: the kit did not load: %s: %s\n' % (type(e).__name__, e)) and 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout))
