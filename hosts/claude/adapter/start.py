"""What Claude Code's start hooks add to the context, as one text (ADAPTER.start_text; read by
tools/agent_view.py, docs/content.md "Agent view"):

  text(event, kit, payload, environ)   the kit header (hooks/kit_header.py, every SessionStart and
                                       SubagentStart); at a SessionStart, the coordinator rules for a
                                       coordinator session (hooks/coordinator_session_start.py) and
                                       the PM checklist for a PM session (hooks/pm_session_start.py,
                                       which the PM's own settings register: docs/pm-start.md)

Each part calls what its hook calls. The session's role is its mark (PM_KIT_ROLE,
kitlib.sessionmark). A check that fails is a line in the text, never raised.
"""
import os

from kitlib.host import header


def text(event, kit, payload=None, environ=None):
    from kitlib import sessionmark
    env = os.environ if environ is None else environ
    out = header.text(kit)
    if event != 'SessionStart':
        return out
    try:
        from kitlib import coordinatorstart
        if coordinatorstart.is_coordinator(env):
            more = coordinatorstart.render(env=env, cwd=(payload or {}).get('cwd') or None)
        elif sessionmark.mark(env) == 'pm':
            more = pm_checklist()
        else:
            more = ''
    except Exception as e:
        more = 'could not check: %s: %s' % (type(e).__name__, e)
    return out + ('\n' + more if more else '')


def pm_checklist():
    """The PM checklist as hooks/pm_session_start.py makes it."""
    from kitlib import pmstart
    cfg = pmstart.load_config()
    if cfg is None:
        from kitlib.config import user
        return 'PM session start: no pm_start config in %s; checklist skipped.' % user.display()
    return pmstart.checklist(cfg)
