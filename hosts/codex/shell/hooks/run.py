"""Codex's one hook entry (hooks.json): `run.py <event> < payload.json`.

A read or bookkeeping tool (FAST: the policy's allowlist without write_stdin) in the owner's unmarked
session is allowed before the kit even boots: MARK and its test mirror kitlib.sessionmark (ENV, and
mark() is None only for an empty value after strip), which cannot be imported before the boot;
tests/codex/test_content holds them equal. Anything else boots the kit through the plugin's own
copy of boot/kitboot.py (shell/boot): this checkout when the plugin sits in a kit checkout (kit.json
four folders up), else PM_KIT_ROOT, verified. Then hosts.codex.adapter.entry.run: PreToolUse through
kitlib.host.hookmain with what is left of DEADLINE seconds, a budget for the whole call that starts
before the boot (a pinned verify hashes every file; hooks.json gives Codex 10), a start
event adding the kit header to the context. A PreToolUse that cannot boot, import or answer exits 2,
which Codex reads as a block; a start event says in plain text that the kit did not load.
"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'boot'))
FAST, DEADLINE, MARK = frozenset(('Read', 'Grep', 'Glob', 'WebSearch', 'WebFetch', 'view_image', 'read_file', 'list_dir', 'update_plan', 'get_goal', 'clock__curr_time', 'request_user_input', 'request_user_input_async')), 7, 'PM_KIT_ROLE'


def main(argv, stdin, stdout, environ=os.environ):
    began, event, text = time.monotonic(), (argv[0] if argv else 'PreToolUse'), stdin.read()
    try:
        if event == 'PreToolUse' and not (environ.get(MARK) or '').strip() and json.loads(text).get('tool_name') in FAST:
            return 0                                   # the owner's read: nothing to decide
    except Exception:
        pass                                           # hookmain reads it again and denies
    try:
        import kitboot
        kit = kitboot.boot(here=os.path.join(HERE, '..', '..', '..', '..', 'boot', 'kitboot.py') if os.path.isfile(os.path.join(HERE, '..', '..', '..', '..', 'kit.json')) else None)
        from hosts.codex.adapter import entry
        return entry.run(event, kit, text, stdout, os.path.abspath(__file__), max(0.0, round(DEADLINE - (time.monotonic() - began), 2)))
    except BaseException as e:                         # a guarded call blocks by exit code; a start event says why
        return 2 if event == 'PreToolUse' else stdout.write('PM kit: the kit did not load: %s: %s\n' % (type(e).__name__, e)) and 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout))
