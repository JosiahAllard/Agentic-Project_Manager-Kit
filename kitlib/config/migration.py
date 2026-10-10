"""The machine config folder's move from its retired name to kitlib.config.user.DIR_NAME
(docs/kit-json.md "Moving the machine config folder").

OLD_DIR_NAME is the one place the retired name is spelled. kitlib.config.user.locate reads the
old folder's config file only while the new folder has none; writers refuse until the owner has
made the new file (MOVE_STEPS), and nothing here moves, copies or deletes a file. The modules
that use OLD_DIR_NAME are listed in tests/scrub/test_old_config_name.py.
"""
import json
import os

# The machine config folder's retired name, read as a fallback while the new one has no config.
OLD_DIR_NAME = '.claude-pm-kit'


def state_paths(name, home=None):
    """(read, write) for the kit file `name` that lives beside the machine config (a tool's state,
    such as hub_backlog_state.json; never config.json, which kitlib.config.user.locate decides).
    `write` is always the new folder's file. `read` is the new folder's file while anything is
    there by that name, else the retired folder's while it has the file (the fallback), else the
    new one: so the first run after the move reads the state the last run wrote."""
    from . import user                      # user imports this module; resolved at call time
    h = home or user.home_dir()
    new = os.path.join(h, user.DIR_NAME, name)
    old = os.path.join(h, OLD_DIR_NAME, name)
    if not os.path.lexists(new) and os.path.isfile(old):
        return old, new
    return new, new

# What a writer that finds only the old file says (old file, new file).
MOVE_STEPS = ('the machine config is still only in the old folder (%s); the kit writes the new one '
              '(%s) only. Copy the old file there, check it with tools/preflight.py (its config line), '
              'then retire the old folder (docs/kit-json.md "Moving the machine config folder")')


def differing_keys(new_text, old_text):
    """The sorted top-level keys whose values differ between two config texts, or None when either
    does not parse as a JSON object. Names only: no value is returned."""
    try:
        new, old = json.loads(new_text), json.loads(old_text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(new, dict) or not isinstance(old, dict):
        return None
    return sorted(k for k in set(new) | set(old) if k not in new or k not in old or new[k] != old[k])
