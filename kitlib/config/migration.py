"""The machine config folder's move from its retired name to kitlib.config.user.DIR_NAME
(docs/kit-json.md "Moving the machine config folder").

OLD_DIR_NAME is the one place the retired name is spelled. kitlib.config.user.locate reads the
old folder's config file only while the new folder has none; writers refuse until the owner has
made the new file (MOVE_STEPS), and nothing here moves, copies or deletes a file. The modules
that use OLD_DIR_NAME are listed in tests/scrub/test_old_config_name.py.
"""
import json

# The machine config folder's retired name, read as a fallback while the new one has no config.
OLD_DIR_NAME = '.claude-pm-kit'

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
