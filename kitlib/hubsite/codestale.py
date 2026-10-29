"""The one place that says a running hub server needs a restart because hub code newer than it
has landed (docs/action-hub.md "Stale code"). The server knows its own start stamp
(serve.Hub._watch_code), so it alone decides `stale`; GET health and GET snapshot carry
`code` = state(stale), and the PM checklist, the pm_watch HUB event and the page's banner
read that object through notice(). Nothing here restarts anything. A server run by its
scheduled task restarts itself (kitlib.hubsite.selfrestart) and shows `stale` only when it does
not: no task, a failed self-check or a restart that did not happen; the restart is then the
owner's."""
import os

HUB_TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools', 'hub.py')


def restart_step(tool=None):
    """The owner's to-do: restart the hub server, and the fallback for a hub with no
    autostart task (hub.py restart needs the task)."""
    tool = tool or HUB_TOOL
    return ('Owner step: restart the Action Hub: python "%s" restart '
            '(a hub with no autostart task: python "%s" stop, then python "%s" serve)'
            % (tool, tool, tool))


def text(tool=None):
    return 'this hub server is running older code than the hub code now on disk. ' \
        + restart_step(tool)


def state(stale):
    """The `code` object a server gives: {'stale': bool, 'restart': the text or None}."""
    stale = stale is True
    return {'stale': stale, 'restart': text() if stale else None}


def notice(code, tool=None):
    """The line to show for a `code` object a server gave, or None when it is absent, not an
    object or not stale (a hub that predates this field says nothing). Never raises."""
    if not isinstance(code, dict) or code.get('stale') is not True:
        return None
    return text(tool)
