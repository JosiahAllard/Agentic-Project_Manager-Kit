"""SessionStart hook: prints the PM checklist as additionalContext (kitlib.pmstart; docs/pm-start.md).
A kit that will not load (boot/kitboot.py) is said in plain text, which SessionStart also reads as
context. Always exits 0."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'boot'))


def main(stdout):
    try:
        import kitboot
        kitboot.boot()
        from hosts.claude.adapter.host import ADAPTER
    except Exception as e:
        return stdout.write("PM session start: the kit did not load: %s: %s\n" % (type(e).__name__, e)) and 0
    try:
        from kitlib import pmstart
        cfg = pmstart.load_config()
        if cfg is None:
            from kitlib.config import user
            text = "PM session start: no pm_start config in %s; checklist skipped." % user.display()
        else:
            text = pmstart.checklist(cfg)
    except Exception as e:
        text = "PM session start: checklist failed: %s: %s" % (type(e).__name__, e)
    stdout.write(ADAPTER.context("SessionStart", text)[0])
    return 0


if __name__ == "__main__":
    main(sys.stdout)
    sys.exit(0)
