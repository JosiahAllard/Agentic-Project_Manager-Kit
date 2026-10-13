"""Compiled service assembly owns its original-registry Claude return peer."""
from optional.bridge.hosts.claude.peer import ClaudePeerEndpoint
from optional.bridge.hosts.claude.runtime import ClaudeSources
from optional.bridge.core.messages import DeliveryError
from optional.bridge.port import OwnedDelivery
from optional.bridge.win.runtime import Runtime

NAME='claude'


def build_sources(controller,native_executables,**settings):
    """Use the supervisor's image set for discovery as well as frontend identity."""
    if set(settings)&{'native_executable','marker'}:
        raise DeliveryError('claude-profile-service-override')
    if 'plugin_name' not in settings or settings['plugin_name'] is None:
        raise DeliveryError('claude-plugin-scope-required')
    return ClaudeSources(controller,native_executable=native_executables,
        marker=controller.root/'service.json',**settings)


def build_delivery(provider,runtime):
    """Called only by compiled bootstrap, before publishing its service marker.

    The private broker state root is the bridge's own explicit workspace. Native
    chat workspaces are never borrowed. Only this endpoint's newly owned native
    registry/key files are published or removed. Publication occurs when the
    configured service is actually launched, not when importing this module.
    The endpoint is the half's ReturnChannel; the OwnedDelivery closes it.
    """
    if (type(provider) is not ClaudeSources or type(runtime) is not Runtime
            or runtime.closed or runtime.providers.get(NAME) is not provider
            or runtime.controller is not provider.controller):
        raise DeliveryError('invalid-claude-delivery-assembly')
    provider.controller._require()
    endpoint=ClaudePeerEndpoint(provider.directory.registry,
        provider.controller.root,provider.directory)
    try:
        adapter=provider.delivery_adapter(endpoint,runtime.sender_mode)
        return OwnedDelivery(adapter,endpoint,adapter_id='claude-peer',
            unavailable='claude-return-endpoint-unavailable')
    except Exception:
        endpoint.close()
        raise
