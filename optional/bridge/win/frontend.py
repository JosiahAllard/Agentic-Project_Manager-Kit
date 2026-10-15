"""Owned MCP stdio client and kernel verification of its native parent."""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass,field
import os
from pathlib import Path

from optional.bridge.core.messages import DeliveryError
from kitlib.proc.pipe import bind,local_free,process_image,process_sids
from kitlib.proc.process import (PROCESS_QUERY_LIMITED_INFORMATION,SYNCHRONIZE,WAIT_TIMEOUT,
    close,command_line,handle_ctime,open_process,proc_ctime,process_parents)
from kitlib.proc.win32 import K32
from .channel import NativeChannel,capture_inherited_channel,revalidate_channel
from .evidence import regular,stable_bytes
from .hosts import native_hosts as normalize_hosts
from .launch import PYTHON_FLAGS
from optional.bridge.paths import FRONTEND_REL,RPC_WORKER_REL,kit_file
from .rpc import MAX_BYTES,PipeClient,decode,encode,request
from .transport import PipeTarget


def running(pid,creation):
    """True only while process (pid, creation) has not exited. An exited process stays openable,
    with its creation time, while any handle to it is open anywhere on the machine, so a matching
    creation time alone does not mean it still runs: its exit is read from the wait state."""
    handle=open_process(pid,PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE)
    if not handle: return False
    try: return handle_ctime(handle)==creation and K32.WaitForSingleObject(handle,0)==WAIT_TIMEOUT
    finally: close(handle)


def _path(value):
    return Path(value).resolve(strict=True)


def arguments(pid,creation):
    """Parse the actual Windows process command line, never a claimed argv."""
    line=command_line(pid,creation)
    if type(line) is not str or not line or len(line)>16384:
        raise DeliveryError('frontend-command-unavailable')
    parse=bind(ctypes.WinDLL('shell32',use_last_error=True),'CommandLineToArgvW',
        [w.LPCWSTR,ctypes.POINTER(ctypes.c_int)],ctypes.POINTER(w.LPWSTR))
    count=ctypes.c_int()
    result=parse(line,ctypes.byref(count))
    if not result: raise DeliveryError('frontend-command-unavailable')
    try:
        if not 1<=count.value<=8: raise DeliveryError('frontend-command-unavailable')
        return tuple(result[index] for index in range(count.value))
    finally: local_free(ctypes.cast(result,w.HANDLE))


@dataclass(frozen=True)
class NativeFrontend:
    pid:int
    creation:int
    host_pid:int
    host_creation:int
    kind:str
    host_executable:Path=field(repr=False)
    channel:NativeChannel|None=field(default=None,repr=False)


class FrontendInspector:
    def __init__(self,kit_root,marker,python,native_hosts):
        marker=Path(marker)
        if not marker.is_absolute(): raise DeliveryError('invalid-service-marker')
        self.kit,self.marker,self.python=_path(kit_root),marker.resolve(strict=False),_path(python)
        _path(self.marker.parent)
        self.hosts=normalize_hosts(native_hosts)
        self.user=process_sids(os.getpid())[0]

    def _process(self,pid,creation,image):
        if (type(pid) is not int or type(creation) is not int
                or proc_ctime(pid)!=creation or process_image(pid)!=image
                or process_sids(pid)[0]!=self.user):
            raise DeliveryError('frontend-process-unverified')

    def _script(self,pid,creation,path,tail):
        self._process(pid,creation,self.python)
        argv=arguments(pid,creation)
        if (len(argv)!=4+len(tail) or argv[1:3]!=PYTHON_FLAGS
                or _path(argv[0])!=self.python or _path(argv[3])!=path
                or tuple(_path(item) for item in argv[4:])!=tail):
            raise DeliveryError('frontend-command-unverified')

    def inspect(self,peer,channel=None):
        self.verify_helper(peer)
        parents=process_parents()
        pid=parents.get(peer.pid)
        creation=proc_ctime(pid) if pid else None
        if type(creation) is not int or creation>peer.creation:
            raise DeliveryError('frontend-parent-unverified')
        argv=arguments(pid,creation)
        default=Path(os.environ['LOCALAPPDATA'])/'pm-kit/agent-bridge/service.json'
        tail=() if len(argv)==4 and self.marker==default.resolve() else (self.marker,)
        self._script(pid,creation,kit_file(FRONTEND_REL,self.kit),tail)
        host=parents.get(pid)
        host_creation=proc_ctime(host) if host else None
        if type(host_creation) is not int or host_creation>creation:
            raise DeliveryError('frontend-host-unverified')
        image=process_image(host)
        kinds=[kind for kind,value in self.hosts.items() if image in value]
        if len(kinds)!=1: raise DeliveryError('frontend-host-unverified')
        self._process(host,host_creation,image)
        # Recheck incarnations after traversing parents; recycled PIDs grant nothing.
        self._process(pid,creation,self.python)
        self._process(peer.pid,peer.creation,self.python)
        verified=revalidate_channel(channel,host,host_creation,self.user)
        # Channel metadata cannot replace the exact helper/native process checks.
        self._process(host,host_creation,image)
        self._process(pid,creation,self.python)
        self._process(peer.pid,peer.creation,self.python)
        return NativeFrontend(pid,creation,host,host_creation,kinds[0],image,verified)

    def verify_helper(self,peer):
        """Kernel RPC helper proof only; each private producer checks its own parent."""
        if type(peer) is not PipeClient: raise DeliveryError('frontend-process-unverified')
        self._script(peer.pid,peer.creation,kit_file(RPC_WORKER_REL,self.kit),())


def service_target(marker):
    data=decode(stable_bytes(Path(marker),limit=2048)+b'\n')
    if (type(data) is not dict or set(data)!={'protocol','pipe','pid','creation'}
            or type(data['protocol']) is not int or data['protocol']!=1):
        raise DeliveryError('invalid-service-marker')
    return PipeTarget(data['pipe'],data['pid'],data['creation'])


MAX_INITIALIZE_BYTES=16*1024


def _generation(marker):
    """Exact target plus stable atomic-publication identity, not a readiness flag."""
    path=Path(marker)
    before=regular(path)
    target=service_target(path)
    after=regular(path)
    def version(info):
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    if version(before)!=version(after):
        raise DeliveryError('service-marker-changed')
    return target,version(after)


def _forward(target,message,channel):
    frame={'protocol':1,'request':message}
    if channel is not None:frame['channel']=channel.to_wire()
    result=request(target,frame)
    if (type(result) is not dict or set(result)!={'protocol','response'}
            or type(result['protocol']) is not int or result['protocol']!=1):
        raise DeliveryError('invalid-service-response')
    response=result['response']
    if 'id' not in message:
        if response is not None:raise DeliveryError('invalid-service-notification-response')
    elif (type(response) is not dict or response.get('jsonrpc')!='2.0'
            or type(response.get('id')) is not type(message['id']) or response['id']!=message['id']
            or ('result' in response)==('error' in response)):
        raise DeliveryError('invalid-service-response')
    return response


def _initialized_result(response):
    if type(response) is not dict or 'error' in response or type(response.get('result')) is not dict:
        raise DeliveryError('service-initialize-refused')
    result=response['result']
    if (type(result.get('protocolVersion')) is not str or not result['protocolVersion']
            or type(result.get('serverInfo')) is not dict or type(result.get('capabilities')) is not dict):
        raise DeliveryError('invalid-service-initialize-result')


def serve_frontend(input_stream,output_stream,marker):
    """Recover only actual initialization state; actual tool frames forward once.

    A new verified marker generation triggers a handshake on the next incoming
    request. The cache contains only successful native initialize parameters and
    the actual initialized notification. Failed/uncertain requests are never
    stored or resent, and their caller metadata is never synthesized from cache.
    """
    unavailable=False
    try:channel=capture_inherited_channel()
    except Exception:channel=None;unavailable=True
    initialize=initialized=None
    initializing=bound=None
    while True:
        raw=input_stream.readline(MAX_BYTES+1)
        if not raw:return
        message=None
        try:
            message=decode(raw)
            if type(message) is not dict:raise DeliveryError('invalid-frontend-request')
            if unavailable:raise DeliveryError('native-channel-unavailable')
            method=message.get('method')
            current=_generation(marker)
            if method=='initialize':
                if ('id' not in message or type(message['id']) not in (str,int)
                        or message.get('jsonrpc')!='2.0' or type(message.get('params')) is not dict
                        or len(encode(message))>MAX_INITIALIZE_BYTES):
                    raise DeliveryError('invalid-native-initialize')
                response=_forward(current[0],message,channel)
                if _generation(marker)!=current:raise DeliveryError('service-marker-changed')
                _initialized_result(response)
                # Canonical private copy retains actual params; no tool payload.
                initialize=decode(encode(message))
                initialized=None
                initializing=current
                bound=None
            elif method=='notifications/initialized':
                if ('id' in message or message.get('jsonrpc')!='2.0'
                        or set(message)-{'jsonrpc','method','params'}
                        or 'params' in message and type(message['params']) is not dict
                        or len(encode(message))>MAX_INITIALIZE_BYTES or initialize is None):
                    raise DeliveryError('invalid-native-initialized-order')
                # This is the actual bounded native notification, including when
                # its acknowledgement fails. Repeating it cannot execute a tool.
                initialized=decode(encode(message))
                if initializing!=current:
                    replay=_forward(current[0],initialize,channel)
                    _initialized_result(replay)
                    if _generation(marker)!=current:raise DeliveryError('service-marker-changed')
                    initializing=current
                response=_forward(current[0],message,channel)
                if _generation(marker)!=current:raise DeliveryError('service-marker-changed')
                bound=current
            else:
                if bound!=current:
                    if initialize is None or initialized is None:
                        raise DeliveryError('native-initialization-unavailable')
                    # Never include a prior call or prior _meta here. Both frames
                    # are the actual cached handshake, verified by the new broker.
                    if initializing!=current:
                        replay=_forward(current[0],initialize,channel)
                        _initialized_result(replay)
                        if _generation(marker)!=current:raise DeliveryError('service-marker-changed')
                        # Only a correlated successful reply establishes this
                        # phase. Lost initialize acknowledgements stay unknown.
                        initializing=current
                    _forward(current[0],initialized,channel)
                    if _generation(marker)!=current:raise DeliveryError('service-marker-changed')
                    bound=current
                response=_forward(current[0],message,channel)
                if _generation(marker)!=current:raise DeliveryError('service-marker-changed')
        except Exception:
            identity=message.get('id') if type(message) is dict else None
            if type(message) is dict and 'id' not in message:response=None
            else:response={'jsonrpc':'2.0','id':identity,'error':{
                'code':-32603,'message':'bridge connection unavailable'}}
        if response is not None:
            output_stream.write(encode(response))
            output_stream.flush()
        if len(raw)>MAX_BYTES or not raw.endswith(b'\n'):return
