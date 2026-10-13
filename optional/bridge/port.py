"""The bridge port: what a host's half (optional/bridge/hosts/<name>/) gives the bridge service.

This replaces the scaffold's never-imported ports.py with the seam the working code really uses
(docs/multi-host-plan.md section 2.4; optional/bridge/README.md "The port"). The service
(win/runtime.py) reaches a host only through these names and loops over the installed halves
(registry.py); it never names a host.

A half exports HOST, a BridgeHost:

    provider(controller, native_images, *, host_id, **profile) -> Provider
        Called once per service start with the half's validated bridge-profile.json settings.
    delivery(provider, runtime) -> DeliveryAdapter, or None when the half cannot deliver
    install(profile, *, python, marker, kit_root) -> InstallFragment
        What the half registers in its host when the bridge is installed (install.py)

Its bridge-profile.json (data, read without importing the half; registry.py) lists the provider's
settings and, as channel_env, the variables its app may nominate a tool pipe in.

A Provider is called with each verified MCP frontend (win/frontend.NativeFrontend) and returns
that frontend's SenderAttestor: the service calls its attest() for every call, and its prepare()
too when it has one (NativeSource); a prepare that is not callable refuses the source. A plain
callable is taken as attest itself. The Provider may also have sender_mode(binding) ->
'prompting' | 'bypass', hook_sink (a HookSink), refresh() and close(); the service uses each it
finds and nothing else.

None of these types attests anything by being constructed. Identity comes only from the half's
own kernel and native checks inside attest() and send().
"""
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Tuple, runtime_checkable

from .core.contracts import AdapterDescription, Capability, CONTRACT_VERSION
from .core.messages import BeforeWriteError, DeliveryError, NativeResult, OutboundMessage, SenderAuthority

SENDER_MODES = ('prompting', 'bypass')


@runtime_checkable
class SenderAttestor(Protocol):
    def attest(self, caller_proof) -> SenderAuthority:
        """The verified sender behind one MCP call's private metadata (never model arguments).
        Anything but a SenderAuthority, or any raise, refuses the call."""


@runtime_checkable
class DeliveryAdapter(Protocol):
    def describe(self) -> AdapterDescription:
        """Its adapter_id, CONTRACT_VERSION and capabilities; SEND is required to be sent to."""

    def send(self, message: OutboundMessage) -> NativeResult:
        """Write once. BeforeWriteError only when no native byte was written; any other raise is
        an unknown outcome, and the core never resends it."""

    def reconcile(self, message: OutboundMessage, native_id: str) -> Optional[NativeResult]:
        """The native receipt of an earlier send, read without resending; None for no news."""


@runtime_checkable
class ReturnChannel(Protocol):
    def close(self) -> None:
        """Remove what the channel published (its pipe, its registry entries)."""


@runtime_checkable
class HookSink(Protocol):
    def __call__(self, peer, payload: bytes) -> object:
        """A native hook event from a verified helper process (win/rpc.PipeClient). The sink
        checks the producer itself; the service only routes the frame to the one installed sink."""


@dataclass(frozen=True)
class NativeSource:
    """The SenderAttestor a provider returns for one frontend: `observe(metadata)` attests, and
    the optional `prepare(request) -> (metadata, consume)` leases native metadata for one tools/call
    before it reaches the MCP server (a host whose caller proof arrives by a hook)."""
    observe: Callable
    prepare: Optional[Callable] = None

    def __post_init__(self):
        if not callable(self.observe) or self.prepare is not None and not callable(self.prepare):
            raise DeliveryError('invalid-native-source')

    def attest(self, caller_proof):
        return self.observe(caller_proof)


@dataclass(frozen=True)
class InstallFragment:
    """One host's part of a prepared installation (install.py).

    server        the MCP server entry's extra fields: 'name' (required, the server's registered
                  name) and any metadata a consumer needs, such as a scoped name
    dependencies  (role, absolute Path) pairs the installation relies on; each must exist
    files         (file name, role, JSON-able document) the preparation writes beside mcp.json,
                  such as a hook registration; file names are flat and start with the host's name
    """
    server: dict
    dependencies: Tuple[Tuple[str, object], ...] = ()
    files: Tuple[Tuple[str, str, object], ...] = ()


@dataclass(frozen=True)
class BridgeHost:
    name: str
    provider: Callable
    delivery: Optional[Callable] = None
    install: Optional[Callable] = None


class OwnedDelivery:
    """A DeliveryAdapter that owns the ReturnChannel it replies through: closing it closes the
    channel, and once closed it describes no capability and sends nothing."""

    def __init__(self, adapter, channel, *, adapter_id, unavailable):
        self.adapter, self.channel = adapter, channel
        self.adapter_id, self.unavailable = adapter_id, unavailable
        self.closed = False

    def describe(self):
        if self.closed:
            return AdapterDescription(self.adapter_id, (), 'Owned return endpoint closed.')
        return self.adapter.describe()

    def send(self, message):
        if self.closed:
            raise BeforeWriteError(self.unavailable)
        return self.adapter.send(message)

    def reconcile(self, message, native_id):
        if self.closed:
            raise DeliveryError(self.unavailable)
        return self.adapter.reconcile(message, native_id)

    def close(self):
        if self.closed:
            return
        self.channel.close()
        self.closed = True


def sends(adapter):
    """True when `adapter` meets DeliveryAdapter well enough for the core to send through it:
    callable describe and send, and a description of this contract version naming SEND."""
    try:
        description = adapter.describe()
    except Exception:
        return False
    return (callable(getattr(adapter, 'send', None)) and type(description) is AdapterDescription
            and description.contract_version == CONTRACT_VERSION
            and type(description.capabilities) is tuple and Capability.SEND in description.capabilities)


def check_host(value, name):
    """`value` as the BridgeHost a half named `name` exports; DeliveryError otherwise."""
    if (type(value) is not BridgeHost or value.name != name or not callable(value.provider)
            or value.delivery is not None and not callable(value.delivery)
            or value.install is not None and not callable(value.install)):
        raise DeliveryError('invalid-bridge-host')
    return value
