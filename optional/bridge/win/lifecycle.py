"""Private Windows state, atomic publication and owned broker lifetime."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from optional.bridge.core.messages import DeliveryError
from kitlib.proc.pipe import (K,INVALID,SecurityAttributes,bind,close,
    create_file,descriptor,from_sddl,local_free,process_sids,protect_file)
from kitlib.proc.process import own_ctime
from .controller import Controller
from .evidence import regular,replace,stable_bytes
from .frontend import FrontendInspector
from .leader import ServiceLeader,LeaderError
from .service import BrokerService

create_directory=bind(K,'CreateDirectoryW',[w.LPCWSTR,w.LPVOID],w.BOOL)


def _verify_root(path):
    regular(path,directory=True)
    handle=create_file(str(path),0x20000,7,None,3,0x02000000,None)
    if handle==INVALID: raise DeliveryError('private-state-root-unavailable')
    try:
        user=process_sids(os.getpid())[0]
        owner,actual=descriptor(handle,1)
        if owner!=user or actual.split('D:',1)[1] not in (
                'P(A;OICI;FA;;;%s)'%user,'P(A;OICI;GA;;;%s)'%user):
            raise DeliveryError('private-state-root-required')
    finally: close(handle)


def private_root(path):
    """Create private missing directories; never rewrite an existing directory ACL."""
    path=Path(path)
    if not path.is_absolute() or path==Path(path.anchor):
        raise DeliveryError('invalid-service-state-root')
    missing=[]
    current=path
    while not current.exists():
        missing.append(current)
        current=current.parent
    for ancestor in (current,*current.parents): regular(ancestor,directory=True)
    user=process_sids(os.getpid())[0]
    security=w.LPVOID()
    if not from_sddl('O:%sD:P(A;OICI;GA;;;%s)'%(user,user),1,ctypes.byref(security),None):
        raise DeliveryError('private-state-acl-unavailable')
    try:
        attributes=SecurityAttributes(ctypes.sizeof(SecurityAttributes),security,False)
        for item in reversed(missing):
            if not create_directory(str(item),ctypes.byref(attributes)) and ctypes.get_last_error()!=183:
                raise DeliveryError('private-state-root-unavailable')
            _verify_root(item)
    finally: local_free(security)
    _verify_root(path)
    return path.resolve(strict=True)


def scope_for(root):
    info=regular(root,directory=True)
    data=[process_sids(os.getpid())[0],info.st_dev,info.st_ino]
    return hashlib.sha256(json.dumps(data,separators=(',',':')).encode()).hexdigest()[:32]


class Supervisor:
    def __init__(self,state_root,kit_root,python,native_hosts, *,
            source_factory=None,hook_sink=None,runtime_builder=None):
        self.root=private_root(state_root)
        self.identity=(regular(self.root,directory=True).st_dev,regular(self.root,directory=True).st_ino)
        self.marker=self.root/'service.json'
        self.leader=self.controller=self.service=self.runtime=None
        self.packet=None
        self.stop=threading.Event()
        self.owner_view=threading.Event()
        self.closed=False
        try:
            self.leader=ServiceLeader(scope_for(self.root))
            self.controller=Controller(self.root,self.leader)
            if runtime_builder is not None:
                self.runtime=runtime_builder(self.controller)
                source_factory=self.runtime.source_factory
                hook_sink=self.runtime.hook_sink
            inspector=FrontendInspector(kit_root,self.marker,python,native_hosts)
            self.service=BrokerService(self.controller,inspector,
                source_factory=source_factory,hook_sink=hook_sink,view_handler=self.request_owner_view)
            self._publish()
        except Exception:
            self.close()
            raise

    def _same_root(self):
        info=regular(self.root,directory=True)
        if (info.st_dev,info.st_ino)!=self.identity:
            raise DeliveryError('service-state-root-changed')
        _verify_root(self.root)

    def _publish(self):
        self._same_root()
        self.packet=json.dumps({'protocol':1,'pipe':self.leader.endpoint,
            'pid':os.getpid(),'creation':own_ctime()},separators=(',',':')).encode()
        if self.marker.exists():
            regular(self.marker)
        temporary=self.root/('service-'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as output:
                protect_file(temporary)
                output.write(self.packet)
                output.flush()
                os.fsync(output.fileno())
            self._same_root()
            replace(temporary,self.marker)  # waits out a frontend reading the marker
        finally:
            if temporary.exists(): temporary.unlink()

    def run(self):
        """Serve requests; native providers and dispatch are installed by the bootstrap."""
        if self.closed: raise DeliveryError('service-ownership-unverified')
        self.service.run(self.stop)

    def request_owner_view(self):
        if self.closed: raise DeliveryError('owner-view-unavailable')
        self.controller._require()
        self.owner_view.set()

    def close(self):
        if self.closed: return
        if self.leader is not None and threading.get_ident()!=self.leader.thread:
            raise LeaderError('supervisor-owner-thread-required')
        self.closed=True
        self.stop.set()
        try:
            if self.packet is not None:
                self._same_root()
                try: current=stable_bytes(self.marker,2048)
                except FileNotFoundError: current=None
                if current==self.packet: self.marker.unlink()
        finally:
            try:
                if self.service is not None: self.service.close()
            finally:
                try:
                    if self.runtime is not None: self.runtime.close()
                finally:
                    try:
                        if self.controller is not None: self.controller.close()
                    finally:
                        if self.leader is not None: self.leader.close()

    def __enter__(self): return self

    def __exit__(self,*error): self.close()
