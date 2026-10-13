"""guard.py: Claude Code's PreToolUse hook entry (ARCHITECTURE.md "Guard rules"; docs/guard.md).

  python hosts/claude/shell/hooks/guard.py < event.json      the hook JSON of a deny or an ask; nothing for an allow
  python hosts/claude/shell/hooks/guard.py --settings-entry  the PreToolUse entry for settings.json, exec form

A call no rule can refuse (a tool not in GUARDED and not mcp__*) is allowed before the kit is even
booted: that fast path is why this entry reads the payload itself. Any other boots the kit
(boot/kitboot.py: the source checkout, or the pinned release, verified) and runs
hosts.claude.adapter.host.ADAPTER through kitlib.host.hookmain.run, which fails closed with a deny
naming the error. When the kit will not boot or import, or the answer cannot be written (pythonw may
have no stdout), the hook exits 2, which Claude Code also reads as a block (the reason on stderr).
"""
import io, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'boot'))
ENTRY = os.path.abspath(__file__)
GUARDED = frozenset(('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash', 'PowerShell'))


def unguarded(event):
    tool = event.get('tool_name') if isinstance(event, dict) else None
    return isinstance(tool, str) and tool not in GUARDED and not tool.startswith('mcp__')


def main(stdin, stdout, argv=()):
    text = '' if list(argv) == ['--settings-entry'] else stdin.read()
    try:
        if text and unguarded(json.loads(text)):
            return 0                                   # Read, Grep, Glob...: nothing to decide
    except Exception:
        pass                                           # hookmain reads it again and denies
    import kitboot
    kitboot.boot()
    from hosts.claude.adapter.host import ADAPTER
    from kitlib.host import hookmain
    if list(argv) == ['--settings-entry']:
        return stdout.write(json.dumps(ADAPTER.settings_entry(ENTRY), indent=2) + '\n') and 0
    return hookmain.run(ADAPTER, 'PreToolUse', io.StringIO(text), stdout, entry=ENTRY)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.stdin, sys.stdout, sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as e:                         # nothing written: block by exit code
        try:
            sys.stderr.write('kit guard: %s: %s\n' % (type(e).__name__, e))
        except BaseException:
            pass
        sys.exit(2)
