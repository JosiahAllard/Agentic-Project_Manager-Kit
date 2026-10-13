"""Value types the core, the service and the host halves share.

IDs are opaque references. Constructing one of these records attests nothing: identity comes only
from a host half's SenderAttestor (optional/bridge/port.py). The draft operation, refusal and
registration types of the scaffold were removed with it (optional/bridge/README.md).
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

CONTRACT_VERSION = 1


class Capability(str, Enum):
    DISCOVER = 'discover'
    READ_STATUS = 'read-status'
    ATTACH = 'attach'
    SEND = 'send'
    START_TASK = 'start-task'
    INTERRUPT = 'interrupt'
    STREAM_EVENTS = 'stream-events'


@dataclass(frozen=True)
class WorkspaceIdentity:
    host_id: str
    project_id: str
    workspace_id: str


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    native_session_id: str
    adapter_id: str
    incarnation_id: str
    workspace: WorkspaceIdentity


@dataclass(frozen=True)
class AdapterDescription:
    """What a DeliveryAdapter says it can do. The core sends only through an adapter whose
    description names its recipient's adapter_id, this CONTRACT_VERSION and Capability.SEND."""
    adapter_id: str
    capabilities: Tuple[Capability, ...]
    limitation: Optional[str] = None
    contract_version: int = CONTRACT_VERSION
