"""Which hosts the bridge serves: each host registered in hosts.json that has a bridge half.

A half is the package optional/bridge/hosts/<module>/ (the host's name with '-' as '_') whose
__init__ exports HOST, a port.BridgeHost of that name. A registered host without a half is not
served; a half without a registered host is ignored, except that its channel_env is still read
(channel_env below). Nothing here names a host.
"""
import importlib

from kitlib.host import registry as host_registry

from .core.messages import DeliveryError
from .paths import HOSTS_REL, kit_file
from .port import check_host

PACKAGE = 'optional.bridge.hosts'


def module_name(name):
    return name.replace('-', '_')


def half_dir(name, kit=None):
    """The folder of `name`'s half (it may not exist)."""
    return kit_file(HOSTS_REL, kit) / module_name(name)


def has_half(name, kit=None):
    return (half_dir(name, kit) / '__init__.py').is_file()


def names(path=None, kit=None):
    """The registered hosts that have a half, in hosts.json order. `path`: a hosts.json to read
    instead of the kit's."""
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


def profile_schema(name, kit=None):
    """The path of `name`'s bridge-profile.json: the settings its provider takes."""
    return half_dir(name, kit) / 'bridge-profile.json'


def present(kit=None):
    """Every half on disk, registered or not: (module name, ...) sorted."""
    root = kit_file(HOSTS_REL, kit)
    return tuple(sorted(p.name for p in root.iterdir() if (p / '__init__.py').is_file()))


def channel_env():
    """Every half's inherited-channel environment variables, registered or not. The MCP frontend
    reads them before it knows which host launched it; a nominated channel grants nothing until
    the service re-verifies it against the verified native host (win/channel.py)."""
    out = []
    for module in present():
        host = getattr(importlib.import_module(PACKAGE + '.' + module), 'HOST', None)
        for item in getattr(host, 'channel_env', ()):
            if type(item) is str and item and item not in out:
                out.append(item)
    return tuple(out)
