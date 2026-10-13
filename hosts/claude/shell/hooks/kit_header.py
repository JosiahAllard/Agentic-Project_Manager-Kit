"""Kit header hook, run on SessionStart and SubagentStart (hooks.json): tells the agent the kit's folder
and what `<kit>` and the other placeholders in its skills and role files mean (kitlib.host.header;
docs/content.md "Kit header").

  kit_header.py <event>    the event's hook JSON with the header as additionalContext; exit 0

The folder is the kit boot/kitboot.py binds: the plugin's own root, or the pinned release. A kit that
will not load is said in plain text instead (SessionStart reads stdout as context).
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'boot'))


def main(argv, stdout):
    event = argv[0] if argv else 'SessionStart'
    try:
        import kitboot
        kit = kitboot.boot()
        from hosts.claude.adapter.host import ADAPTER
        from kitlib.host import header
        text = ADAPTER.context(event, header.text(kit))[0]
    except Exception as e:
        text = 'PM kit header: the kit did not load: %s: %s\n' % (type(e).__name__, e)
    stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:], sys.stdout))
