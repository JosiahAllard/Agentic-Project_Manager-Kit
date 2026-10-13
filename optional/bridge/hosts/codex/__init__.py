"""Codex's half of the bridge (optional/bridge/port.py; optional/bridge/STATUS.md "Codex").

    provider     runtime.CodexSources: desktop and terminal sources from the Codex profile
    delivery     the direct desktop adapter (runtime._DirectDesktopAdapter), between two selected
                 Codex desktop chats in the same app only
    install      the 'agent-bridge' MCP server entry; Codex has no bridge hook

bridge-profile.json's channel_env names the variable Codex Desktop names its app tools pipe in.

client.py is the Codex plugin's own client of the bridge's MCP frontend (bridge_client before).
The modules are imported only when the service, the installer or a test asks.
"""
from optional.bridge.port import BridgeHost, InstallFragment

NAME = 'codex'
SERVER_NAME = 'agent-bridge'


def _provider(controller, native_images, **settings):
    from .runtime import CodexSources
    return CodexSources(controller, **settings)


def _delivery(provider, runtime):
    return provider.delivery_adapter()


def _install(profile, *, python, marker, kit_root):
    return InstallFragment(server={'name': SERVER_NAME})


HOST = BridgeHost(NAME, _provider, delivery=_delivery, install=_install)
