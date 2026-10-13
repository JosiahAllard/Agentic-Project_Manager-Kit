"""The fake host's one hook entry (tests only; docs/new-host.md): boot the kit, build the adapter,
hand the event to kitlib.host.hookmain, or for a start event add the kit header.

  run.py <event> < payload.json
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', '..', 'boot'))


def main(argv, stdin, stdout):
    event = argv[0] if argv else 'before_tool'
    import kitboot
    kit = kitboot.boot()
    from kitlib.host import header, hookmain
    from tests.host.fakehost.adapter import ADAPTER
    if event in ADAPTER.CONTEXT_EVENTS:
        return stdout.write(ADAPTER.context(event, header.text(kit))[0]) and 0
    return hookmain.run(ADAPTER, event, stdin, stdout, entry=os.path.abspath(__file__))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout))
