"""Claude Code's half of the bridge (optional/bridge/port.py; optional/bridge/STATUS.md "Claude").

    provider  assembly.build_sources: desktop and terminal sources (runtime.ClaudeSources)
    delivery  assembly.build_delivery: the protocol-1 peer adapter and its owned return pipe
    install   the plugin-scoped MCP server entry and the terminal PreToolUse hook (hook.py)

The modules are imported only when the service, the installer or a test asks, so reading HOST
(the registry, the MCP frontend) loads no Win32 binding.
"""
from pathlib import Path

from optional.bridge.port import BridgeHost, InstallFragment

NAME = 'claude'
HERE = Path(__file__).resolve().parent


def _provider(controller, native_images, **settings):
    from .assembly import build_sources
    return build_sources(controller, native_images, **settings)


def _delivery(provider, runtime):
    from .assembly import build_delivery
    return build_delivery(provider, runtime)


def _install(profile, *, python, marker, kit_root):
    """The plugin registers the bare server key; plugin_name and scoped_name bind the plugin
    manifest name and the actual registered scope for a consumer. The hook is the exact native
    exec-form argv the provider later verifies (interpreter, script, marker)."""
    from optional.bridge.core.messages import DeliveryError
    from optional.bridge.win.launch import PYTHON_FLAGS
    from .provenance import PluginMcp
    try:
        namespace = PluginMcp(profile['plugin_name'], profile['mcp_server_name'])
    except KeyError:
        raise DeliveryError('claude-plugin-name-required') from None
    hook = Path(kit_root) / HERE.relative_to(HERE.parents[3]) / 'hook.py'
    descriptor = {'type': 'command', 'command': str(python), 'args': [*PYTHON_FLAGS, str(hook), str(marker)]}
    return InstallFragment(
        server={'name': namespace.server_name, 'plugin_name': namespace.plugin_name,
                'scoped_name': namespace.registered_name},
        dependencies=(('claude-provenance', Path(kit_root) / HERE.relative_to(HERE.parents[3]) / 'provenance.py'),
                      ('claude-hook', hook)),
        files=(('claude-hooks.json', 'claude-hook-fragment',
                {'hooks': {'PreToolUse': [{'matcher': namespace.matcher, 'hooks': [descriptor]}]}}),))


HOST = BridgeHost(NAME, _provider, delivery=_delivery, install=_install)
