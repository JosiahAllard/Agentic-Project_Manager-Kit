"""Which hosts the bridge serves: each peer optional/bridge/peers.json lists that has a bridge half.

peers.json is the bridge's own list, apart from the kit's hosts.json: a bridge peer need not be a
kit host. It holds
    peers      the peer names, in order
    refused    the [sender, recipient] host pairs the bridge never delivers (core/hostpairs.py)
A half is the package optional/bridge/hosts/<module>/ (the peer's name with '-' as '_') whose
__init__ exports HOST, a port.BridgeHost of that name, beside its bridge-profile.json. A listed
peer without a half is not served, and a folder for an unlisted peer is ignored: it is never
imported and its files are never read. Nothing here names a host.

bridge-profile.json is data the service and the MCP frontend read without importing the half:
    required, optional      the provider's setting names
    directories, names      which of them are folders, and which are plain names
    channel_env (optional)  the environment variables in which the host's app may nominate its
                            own tool pipe to the frontend it launches (win/channel.py)
"""
import importlib
import json
import re

from .core.hostpairs import HostPairs
from .core.messages import DeliveryError
from .paths import HOSTS_REL, PEERS_REL, kit_file
from .port import check_host

PACKAGE = 'optional.bridge.hosts'
SCHEMA_KEYS = ('required', 'optional', 'directories', 'names')
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
MAX_SCHEMA_BYTES = 4096
PEERS = str(kit_file(PEERS_REL))     # tests patch this to a peers.json of their own
PEERS_KEYS = {'peers', 'refused'}


def read_peers(path=None):
    """(peers, refused) from a peers.json (default: PEERS), checked as a HostPairs rule.
    DeliveryError('invalid-bridge-peers') when it cannot be read or is malformed."""
    try:
        with open(PEERS if path is None else path, 'rb') as f:
            data = f.read(MAX_SCHEMA_BYTES + 1)
        if len(data) > MAX_SCHEMA_BYTES:
            raise ValueError('size')
        doc = json.loads(data.decode('utf-8'))
        if (type(doc) is not dict or set(doc) != PEERS_KEYS or type(doc['peers']) is not list
                or type(doc['refused']) is not list or any(type(pair) is not list for pair in doc['refused'])):
            raise ValueError('shape')
        rule = HostPairs(doc['peers'], doc['refused'])
    except (OSError, ValueError, DeliveryError):
        raise DeliveryError('invalid-bridge-peers') from None
    return rule.peers, tuple(sorted(rule.refused))


def host_pairs(path=None):
    """The HostPairs rule of a peers.json (default: PEERS): which listed peer may send to which."""
    return HostPairs(*read_peers(path))


def module_name(name):
    return name.replace('-', '_')


def half_dir(name, kit=None):
    """The folder of `name`'s half (it may not exist)."""
    return kit_file(HOSTS_REL, kit) / module_name(name)


def has_half(name, kit=None):
    return (half_dir(name, kit) / '__init__.py').is_file()


def profile_path(name, kit=None):
    """The path of `name`'s bridge-profile.json."""
    return half_dir(name, kit) / 'bridge-profile.json'


def profile_schema(schema):
    """A bridge-profile.json document as (required, optional, directories, names) sets of setting
    names: directories and names are required or optional, and never both. channel_env, when
    present, must be a list of environment variable names (channel_names reads it)."""
    if (type(schema) is not dict or not set(SCHEMA_KEYS) <= set(schema)
            or set(schema) - set(SCHEMA_KEYS) - {'channel_env'}
            or any(type(value) is not list or len(value) > 16 or any(type(key) is not str for key in value)
                   or len(set(value)) != len(value) for value in schema.values())
            or len({item.upper() for item in schema.get('channel_env', ())}) != len(schema.get('channel_env', ()))
            or any(not ENV_NAME.fullmatch(item) for item in schema.get('channel_env', ()))):
        raise DeliveryError('invalid-native-profile-schema')
    required, optional, directories, names = (set(schema[key]) for key in SCHEMA_KEYS)
    if required & optional or not (directories | names) <= required | optional or directories & names:
        raise DeliveryError('invalid-native-profile-schema')
    return required, optional, directories, names


def channel_names(schema):
    """The channel_env list of a checked bridge-profile.json document, as a tuple."""
    profile_schema(schema)
    return tuple(schema.get('channel_env', ()))


def read_profile(name, kit=None):
    """`name`'s bridge-profile.json, parsed and checked. DeliveryError when it is not."""
    try:
        with open(profile_path(name, kit), 'rb') as f:
            data = f.read(MAX_SCHEMA_BYTES + 1)
        if len(data) > MAX_SCHEMA_BYTES:
            raise ValueError('size')
        schema = json.loads(data.decode('utf-8'))
    except (OSError, ValueError):
        raise DeliveryError('invalid-native-profile-schema') from None
    profile_schema(schema)
    return schema


def names(path=None, kit=None):
    """The listed peers that have a half, in peers.json order. `path`: a peers.json to read
    instead of the kit's (default: PEERS, which tests patch)."""
    return tuple(name for name in read_peers(path)[0] if has_half(name, kit))


def half(name, path=None):
    """The BridgeHost of `name`. DeliveryError('native-provider-unavailable') when the peer is not
    listed or has no half; DeliveryError('invalid-bridge-host') when its HOST is malformed."""
    if name not in names(path):
        raise DeliveryError('native-provider-unavailable')
    module = importlib.import_module(PACKAGE + '.' + module_name(name))
    return check_host(getattr(module, 'HOST', None), name)


def installed(path=None):
    """{name: BridgeHost} for every served host."""
    return {name: half(name, path) for name in names(path)}


def channel_env(path=None, kit=None):
    """Every served host's channel_env names, read from bridge-profile.json as data: no half is
    imported. A served half whose profile cannot be read nominates nothing (it fails closed for
    itself and leaves the others working). A nominated channel grants nothing until the service
    re-verifies it against the verified native host."""
    out = []
    for name in names(path, kit):
        try:
            found = channel_names(read_profile(name, kit))
        except DeliveryError:
            continue
        for item in found:
            # Windows environment names ignore case: APP_PIPE and app_pipe are one variable.
            if item.upper() not in {seen.upper() for seen in out}:
                out.append(item)
    return tuple(out)
