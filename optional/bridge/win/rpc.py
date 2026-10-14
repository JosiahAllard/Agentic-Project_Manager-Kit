"""Bounded private Windows JSON request/reply with kernel endpoint identities.

The handler must bind the actual client process to its trusted native frontend.
Pipe access, a PID, JSON labels or claimed owner status alone grant nothing.
Default handling denies. Unknown outcomes are never retried here.
"""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import json
import time

from optional.bridge.core.jsonwire import JsonLineDecoder,encode_frame,_check_structure
from optional.bridge.core.messages import BeforeWriteError,DeliveryError
from kitlib.proc.pipe import client_pid,read,write,peek
from kitlib.proc.errors import ProcError
from kitlib.proc.process import proc_ctime
from .leader import ServiceLeader
from .transport import PipeTarget,run_worker

MAX_BYTES=1024*1024


def encode(value):
    _check_structure(value,MAX_BYTES)
    data=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(data)>=MAX_BYTES: raise DeliveryError('private-frame-limit')
    return data+b'\n'


def decode(data):
    if type(data) is not bytes or not 0<len(data)<=MAX_BYTES or not data.endswith(b'\n'):
        raise DeliveryError('invalid-private-frame')
    def pairs(values):
        result={}
        for key,value in values:
            if key in result: raise DeliveryError('duplicate-private-key')
            result[key]=value
        return result
    def invalid(value): raise DeliveryError('invalid-private-number')
    try:
        value=json.loads(data.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid)
        _check_structure(value,MAX_BYTES)
        return value
    except Exception:
        raise DeliveryError('invalid-private-frame') from None


def request(target,message, *, timeout=5):
    if type(target) is not PipeTarget: raise BeforeWriteError('invalid-native-pipe-target')
    target.__post_init__()
    try: packet=encode(message)
    except Exception: raise BeforeWriteError('invalid-private-request') from None
    header=encode_frame({'pipe':target.name,'pid':target.pid,'creation':target.creation,
        'length':len(packet)},max_frame_bytes=2048)
    data=run_worker('bridge_rpc_worker.py',header+packet,limit=MAX_BYTES+2048,timeout=timeout)
    try:
        first,separator,body=data.partition(b'\n')
        decoder=JsonLineDecoder(max_frame_bytes=2048)
        frames=decoder.feed(first+b'\n')
        decoder.finish()
        if not separator or len(frames)!=1: raise ValueError('invalid helper result')
        status=frames[0]
        if status=={'state':'not-written'} and not body:
            raise BeforeWriteError('native-peer-unavailable-before-write')
        if (set(status)!={'state','length'} or status['state']!='reply'
                or type(status['length']) is not int or status['length']!=len(body)):
            raise ValueError('invalid helper result')
        return decode(body)
    except BeforeWriteError: raise
    except Exception: raise DeliveryError('private-outcome-uncertain') from None


@dataclass(frozen=True)
class PipeClient:
    pid:int
    creation:int


def read_frame(handle,deadline, *, blocking=False):
    data=bytearray()
    while time.monotonic()<deadline:
        buffer,count=ctypes.create_string_buffer(4096),w.DWORD()
        if read(handle,buffer,len(buffer),ctypes.byref(count),None):
            if count.value:
                data.extend(buffer.raw[:count.value])
                if len(data)>MAX_BYTES: raise DeliveryError('private-frame-limit')
                if b'\n' in data:
                    if not data.endswith(b'\n') or data.count(b'\n')!=1:
                        raise DeliveryError('extra-private-frames')
                    return decode(bytes(data))
        elif ctypes.get_last_error() not in (232,):
            raise DeliveryError('private-input-closed')
        if not blocking: time.sleep(.005)
    raise DeliveryError('private-read-timeout')


def write_frame(handle,packet,deadline):
    offset=0
    while offset<len(packet) and time.monotonic()<deadline:
        count=w.DWORD()
        piece=packet[offset:offset+4096]
        if not write(handle,piece,len(piece),ctypes.byref(count),None):
            raise DeliveryError('private-write-closed')
        offset+=count.value
        if not count.value: time.sleep(.005)  # PIPE_NOWAIT may have a full buffer.
    if offset!=len(packet): raise DeliveryError('private-write-timeout')


class PipeServer:
    def __init__(self,leader, *, handler=None):
        if type(leader) is not ServiceLeader or not leader.owns():
            raise DeliveryError('service-ownership-unverified')
        self.leader,self.handler=leader,handler

    def once(self, *, timeout=1):
        if not self.leader.owns(): raise DeliveryError('service-ownership-unverified')
        listener=self.leader.listener
        listener.accept(timeout=timeout)
        try:
            try:
                pid=w.ULONG()
                if not client_pid(listener.handle,ctypes.byref(pid)):
                    raise DeliveryError('private-client-unverified')
                creation=proc_ctime(pid.value)
                listener.verify_client(pid.value,creation)
                message=read_frame(listener.handle,time.monotonic()+3)
                listener.verify_client(pid.value,creation)
                if self.handler is None: result={'error':{'code':'private-client-unverified'}}
                else:
                    try: result=self.handler(PipeClient(pid.value,creation),message)
                    except Exception: result={'error':{'code':'private-request-unavailable'}}
            except (DeliveryError,ProcError):
                # Invalid frames and vanished clients affect this connection only.
                # Reset/leadership failures remain fatal outside this boundary.
                result={'error':{'code':'private-client-unavailable'}}
            packet=encode(result)
            write_frame(listener.handle,packet,time.monotonic()+3)
            # Do not disconnect until the helper drains the response and closes.
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                available=w.DWORD()
                if not peek(listener.handle,None,0,None,ctypes.byref(available),None): break
                if available.value: raise DeliveryError('extra-private-frames')
                time.sleep(.005)
        finally: listener.reset()
