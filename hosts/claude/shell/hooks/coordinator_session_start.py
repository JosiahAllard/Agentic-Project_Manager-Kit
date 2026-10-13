"""SessionStart hook: prints the coordinator standing rules as additionalContext.

Prints nothing unless PM_KIT_ROLE is "coordinator" (or the old "conductor"), read as kitlib.sessionmark
reads it: a mark carrying the kit plugin's agent prefix counts; the raw value when the kit will not
load (boot/kitboot.py), and then the failure is the text. The checks run in the "cwd" of the hook's
stdin JSON, else os.getcwd(). Always exits 0.
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'boot'))
ROLES = ("coordinator", "conductor")


def main(stdin, stdout, environ=os.environ):
    try:
        import kitboot
        kitboot.boot()
        from kitlib import coordinatorstart, sessionmark
        from hosts.claude.adapter.host import ADAPTER
    except Exception as e:                             # plain stdout: SessionStart reads it as context
        if environ.get("PM_KIT_ROLE") in ROLES:
            stdout.write("Coordinator session start: the kit did not load: %s: %s\n" % (type(e).__name__, e))
        return 0
    if sessionmark.mark() not in ROLES:
        return 0
    try:
        cwd = None if stdin is None or stdin.isatty() else json.loads(stdin.read() or "{}").get("cwd")
    except Exception:
        cwd = None
    try:
        text = coordinatorstart.render(cwd=cwd or None)
    except Exception as e:
        text = "Coordinator session start: could not check: %s: %s" % (type(e).__name__, e)
    stdout.write(ADAPTER.context("SessionStart", text)[0])
    return 0


if __name__ == "__main__":
    main(sys.stdin, sys.stdout)
    sys.exit(0)
