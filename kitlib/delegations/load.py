"""Read the owner's delegation list (docs/delegations.md): one file, read only, never written.

The file is `owner-delegations.json` in the PM repository's machine config folder
(`<PM repo>/.pm-kit`, kitlib.config.user.locate's OK; tools/config_move.py copies it there from
`~/.pm-kit`). No setting and no environment variable moves it; only a test passes `home=`. With no
PM-repo pointer there is no list (nothing is delegated); a machine config that cannot be read
(locate's UNMOVED or BAD) is a DelegationError. A link or junction on the file or on the folder
refuses, so the list is the file the owner wrote in that folder.
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
    """The home folder's copy, `~/.pm-kit/owner-delegations.json`: the move's source, which the
    guard still holds (kitlib.guard.delegfile). load reads the PM repository's."""
    return os.path.join(home or user.home_dir(), user.DIR_NAME, FILE_NAME)


def _linked(path):
    return os.path.islink(path) or os.path.isjunction(path)


def load(home=None):
    """The Delegations of this machine, or DelegationError (DelegationMissing when absent or
    when there is no PM-repo pointer)."""
    loc = user.locate(home)
    if loc.error:
        raise DelegationError('the machine config cannot be read, so neither can the delegation '
                              'list: %s' % loc.error)
    if loc.state != user.OK:
        raise DelegationMissing('no PM repo pointer, so no delegation list: nothing is delegated')
    path = os.path.join(loc.folder, FILE_NAME)
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
