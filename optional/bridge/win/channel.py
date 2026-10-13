"""Read-only native endpoint identity, never writer/delegate authority.

Only a frontend's inherited environment can nominate its endpoint. Publication
is private metadata outside model tool arguments. Both frontend and broker query
kernel server identity and a bounded, incarnation-checked ancestor chain. A host
provider still decides whether this app relationship supports a native lease.
"""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass,field
import os
from pathlib import Path

from optional.bridge.core.messages import DeliveryError
from kitlib.proc.pipe import (INVALID,close,create_file,descriptor,
    process_image,process_sids,server_pid)
from kitlib.proc.process import ancestors_of,own_ctime,proc_ctime
from .transport import LOCAL_PIPE,PipeTarget


@dataclass(frozen=True)
class NativeChannel:
    target:PipeTarget=field(repr=False)
    executable:Path=field(repr=False)

    def __post_init__(self):
        if (type(self.target) is not PipeTarget or not isinstance(self.executable,Path)
                or not self.executable.is_absolute() or len(str(self.executable))>1024
                or any(ord(char)<32 for char in str(self.executable))):
            raise DeliveryError('invalid-native-channel')
        self.target.__post_init__()

    def to_wire(self):
        self.__post_init__()
        return {'pipe':self.target.name,'pid':self.target.pid,'creation':self.target.creation,
            'executable':str(self.executable)}


def _process(pid,creation,user,executable=None):
    if (type(pid) is not int or not 0<pid<=0xffffffff or type(creation) is not int or creation<=0
            or proc_ctime(pid)!=creation or process_sids(pid)[0]!=user
            or executable is not None and process_image(pid)!=executable
            or proc_ctime(pid)!=creation):
        raise DeliveryError('native-channel-process-unverified')


def _observe(name,observer_pid,observer_creation,user):
    """One local metadata-only open; no reads, writes, pipe waiting or retries.

    Local CreateFile reports unavailable/busy instances immediately. READ_CONTROL
    and FILE_READ_ATTRIBUTES request metadata access only; overlapped mode avoids
    selecting synchronous I/O. All later calls query local kernel identity/security.
    """
    if type(name) is not str or LOCAL_PIPE.fullmatch(name) is None:
        raise DeliveryError('invalid-native-channel')
    _process(observer_pid,observer_creation,user)
    handle=create_file(name,0x00020080,0,None,3,0x40000000,None)
    if handle==INVALID or not handle:raise DeliveryError('native-channel-unavailable')
    try:
        actual=w.ULONG()
        if not server_pid(handle,ctypes.byref(actual)) or not actual.value:
            raise DeliveryError('native-channel-server-unverified')
        creation=proc_ctime(actual.value)
        image=process_image(actual.value)
        server_user,server_owner=process_sids(actual.value)
        if server_user!=user or descriptor(handle)[0] not in (server_user,server_owner):
            raise DeliveryError('native-channel-owner-unverified')
        _process(actual.value,creation,user,image)
        # Strict ancestors, not the observer itself or an unrelated same-user app.
        ancestry=ancestors_of(observer_pid,observer_creation,limit=8)
        if (actual.value,creation) not in ancestry:
            raise DeliveryError('native-channel-ancestor-unverified')
        _process(observer_pid,observer_creation,user)
        _process(actual.value,creation,user,image)
        return NativeChannel(PipeTarget(name,actual.value,creation),image)
    finally:close(handle)


def capture_inherited_channel(names=None):
    """Called once inside the actual MCP frontend; no endpoint argument is accepted.

    `names`: the environment variables a host may nominate its app channel in (default:
    registry.channel_env(): every served half's bridge-profile.json channel_env, read as data). Two nominations refuse."""
    if names is None:
        from optional.bridge import registry
        names=registry.channel_env()
    values=[os.environ.get(name) for name in names]
    values=[value for value in values if value]
    if not values:return None
    if len(values)>1:raise DeliveryError('native-channel-ambiguous')
    value=values[0]
    try:
        pid,creation=os.getpid(),own_ctime()
        return _observe(value,pid,creation,process_sids(pid)[0])
    except DeliveryError:raise
    except Exception:raise DeliveryError('native-channel-unavailable') from None


def revalidate_channel(wire,host_pid,host_creation,user):
    """Broker port after frontend/native verification, outside ordinary request JSON."""
    if wire is None:return None
    try:
        if (type(wire) is not dict or set(wire)!={'pipe','pid','creation','executable'}
                or type(wire['executable']) is not str):
            raise DeliveryError('invalid-native-channel')
        target=PipeTarget(wire['pipe'],wire['pid'],wire['creation'])
        image=Path(wire['executable'])
        claimed=NativeChannel(target,image)
        actual=_observe(target.name,host_pid,host_creation,user)
        if actual!=claimed:raise DeliveryError('native-channel-identity-changed')
        return actual
    except DeliveryError:raise
    except Exception:raise DeliveryError('native-channel-unavailable') from None
