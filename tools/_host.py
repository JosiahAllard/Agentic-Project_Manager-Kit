"""The edge where a host's adapter is imported (ARCHITECTURE.md "Hosts"), kept out of kitlib.

kitlib never imports a host: kitlib.host.registry hands out the adapter's module name from
hosts.json, and this helper is the one place a shared tool turns that name into the module.
Not a command (no __main__ block, so kit_stubs gives it no stub). A tool run as a script has its
own folder on sys.path, so `import _host` finds this file.

  adapter_module(name, environ, path)   the imported adapter module of `name`, else of PM_KIT_HOST
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.host import registry                                    # noqa: E402


def adapter_module(name=None, environ=None, path=None):
    """The adapter module hosts.json names for `name` (None: registry.choose's pick). A registry
    fault is RegistryError; a module that will not import is ImportError, said by the caller."""
    return importlib.import_module(registry.host(registry.choose(name, environ, path), path).adapter)
