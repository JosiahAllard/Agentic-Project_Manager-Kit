"""Read the owner's delegation list (docs/delegations.md): one file, read only, never written.

The file is `owner-delegations.json` in the machine config folder (`~/.pm-kit`). No setting, no
environment variable and no older folder moves it; only a test passes `home=`. A link or junction
on the file or on the folder refuses, so the list is the file the owner wrote in that folder.
"""
import collections
import os

from kitlib.config import user
from kitlib.config.types import ConfigError, parse_json

from . import schema

FILE_NAME = 'owner-delegations.json'

Delegations = collections.namedtuple('Delegations', 'path schema operations')


class DelegationError(Exception):
    """The delegation list exists but cannot be used. Its message names every problem found."""


class DelegationMissing(DelegationError):
    """There is no delegation list: nothing is delegated."""


def delegations_file(home=None):
    return os.path.join(home or user.home_dir(), user.DIR_NAME, FILE_NAME)


def _linked(path):
    return os.path.islink(path) or os.path.isjunction(path)


def load(home=None):
    """The Delegations of this machine, or DelegationError (DelegationMissing when absent)."""
    path = delegations_file(home)
    folder = os.path.dirname(path)
    if _linked(folder):
        raise DelegationError('%s is a link or junction; the delegation list is read only from a '
                              'real folder' % folder)
    if _linked(path):
        raise DelegationError('%s is a link or junction; the delegation list must be a plain file' % path)
    if not os.path.lexists(path):
        raise DelegationMissing('no delegation list at %s: nothing is delegated' % path)
    if not os.path.isfile(path):
        raise DelegationError('%s is not a file' % path)
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise DelegationError('%s cannot be read: %s' % (path, e))
    try:
        data = parse_json(text, path)
        ops = schema.check(data, path)
    except ConfigError as e:
        raise DelegationError(str(e))
    return Delegations(path, data['schema'], ops)
