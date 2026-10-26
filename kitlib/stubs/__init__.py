"""The kit location a printed owner command names, and the outcome type of tools/kit_stubs.py.

tools/kit_stubs.py has one verb, `memory`: kitlib.stubs.memory, the agent memory folder a
repository names in its .claude/settings.local.json (docs/install.md). Its owner command names the
kit by kit_root(): the kit this library is in, or the live checkout when it runs from an export
(KIT_LIVE_ROOT under a pin, else the main checkout the export's marker names; `live_kit`), never
an export a later prune may remove.
"""
import collections
import os

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

Outcome = collections.namedtuple('Outcome', 'code lines')


def kit_root():
    """The kit a printed command names, read at each call: KIT_ROOT, or the live kit checkout when
    this code runs from an export (kitlib.pluginsource.live_root: KIT_LIVE_ROOT, set by
    kitlib.gate.pin; else the main checkout the export's marker names, `live_kit`). An export whose
    marker names no main checkout gives the export itself."""
    from kitlib import pluginsource
    kit = pluginsource.live_root(KIT_ROOT)
    return live_kit(kit)[0] or kit


def live_kit(kit):
    """(folder, None): the kit a printed command may name for `kit`, which is `kit` itself unless it
    is an export (it holds kitlib.pluginsource.MARKER: the plugin's export or a pin's, both of which
    a prune may remove); for an export, the main checkout its marker names
    (kitlib.pluginsource.export_checkout) when that holds kit.json and a tools folder. (None, why)
    for an export whose marker names no such checkout."""
    from kitlib import pluginsource
    if not os.path.isfile(os.path.join(kit, pluginsource.MARKER)):
        return kit, None
    checkout = pluginsource.export_checkout(kit)
    if checkout and os.path.isfile(os.path.join(checkout, 'kit.json')) and os.path.isdir(
            os.path.join(checkout, 'tools')):
        return os.path.normpath(checkout), None
    return None, ('%s is an export of the kit, which a later prune may remove, and its marker names '
                  'no main checkout of the kit; run the tool from the kit\'s main checkout, or '
                  'refresh the plugin (docs/plugin-source.md "Layout")' % os.path.normpath(kit))
