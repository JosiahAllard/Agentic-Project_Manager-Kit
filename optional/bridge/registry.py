"""Which hosts the bridge serves: each host registered in hosts.json that has a bridge half.

A half is the package optional/bridge/hosts/<module>/ (the host's name with '-' as '_') whose
__init__ exports HOST, a port.BridgeHost of that name, beside its bridge-profile.json. A registered
host without a half is not served, and a folder for an unregistered host is ignored: it is never
imported and its files are never read. hosts.json is the source of truth (docs/new-host.md);
nothing here names a host.

bridge-profile.json is data the service and the MCP frontend read without importing the half:
    required, optional      the provider's setting names
    directories, names      which of them are folders, and which are plain names
    channel_env (optional)  the environment variables in which the host's app may nominate its
                            own tool pipe to the frontend it launches (win/channel.py)
"""
import importlib
import json
import re

from kitlib.host import registry as host_registry

from .core.messages import DeliveryError
from .paths import HOSTS_REL, kit_file
from .port import check_host

PACKAGE = 'optional.bridge.hosts'
SCHEMA_KEYS = ('required', 'optional', 'directories', 'names')
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
MAX_SCHEMA_BYTES = 4096


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
    """The registered hosts that have a half, in hosts.json order. `path`: a hosts.json to read
    instead of the kit's (default: kitlib.host.registry.PATH, which tests patch)."""
    return tuple(name for name in host_registry.names(path) if has_half(name, kit))


def half(name, path=None):
    """The BridgeHost of `name`. DeliveryError('native-provider-unavailable') when the host is not
    registered or has no half; DeliveryError('invalid-bridge-host') when its HOST is malformed."""
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
