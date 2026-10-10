"""SessionStart hook: prints the coordinator standing rules as additionalContext.

Prints nothing unless PM_KIT_ROLE is "coordinator" (or the old "conductor"). Always exits 0. The live checks come later.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def stdin_cwd():
    """The "cwd" of the SessionStart hook's stdin JSON, or None (the checks then use os.getcwd())."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        return json.loads(sys.stdin.read() or "{}").get("cwd") or None
    except Exception:
        return None


def main():
    if os.environ.get("PM_KIT_ROLE") not in ("coordinator", "conductor"):
        return
    try:
        from kitlib import coordinatorstart
        text = coordinatorstart.render(cwd=stdin_cwd())
    except Exception as e:
        text = "Coordinator session start: could not check: %s: %s" % (type(e).__name__, e)
    if text is None:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": text}}))


if __name__ == "__main__":
    main()
    sys.exit(0)
