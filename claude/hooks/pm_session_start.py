"""SessionStart hook: prints the PM checklist as additionalContext. Always exits 0."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def main():
    try:
        from kitlib import pmstart
        cfg = pmstart.load_config()
        if cfg is None:
            text = "PM session start: no pm_start config in ~/.claude-pm-kit/config.json; checklist skipped."
        else:
            text = pmstart.checklist(cfg)
    except Exception as e:
        text = "PM session start: checklist failed: %s: %s" % (type(e).__name__, e)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": text}}))


if __name__ == "__main__":
    main()
    sys.exit(0)
