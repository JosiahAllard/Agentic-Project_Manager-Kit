"""Real Windows service ownership: thread-held mutex and exclusive user pipe.

The controller derives scope from its configured user/state identity. A scope is
not a role grant. Checks from workers verify the retained supervisor thread and
live owned endpoint; only the acquiring thread can release the mutex.
"""
import ctypes
from ctypes import wintypes as w
import os
import re
import threading

from kitlib.proc.pipe import (K,bind,close,descriptor,from_sddl,local_free,
    process_sids,PrivateListener,SecurityAttributes)

create_mutex=bind(K,'CreateMutexExW',[w.LPVOID,w.LPCWSTR,w.DWORD,w.DWORD],w.HANDLE)
wait=bind(K,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD)
release=bind(K,'ReleaseMutex',[w.HANDLE],w.BOOL)
thread_id=bind(K,'GetCurrentThreadId',[],w.DWORD)
open_thread=bind(K,'OpenThread',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE)
_owners,_lock={},threading.RLock()


class LeaderError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__('bridge supervisor unavailable: '+code)


class ServiceLeader:
    def __init__(self,scope):
        if type(scope) is not str or not re.fullmatch('[0-9a-f]{32}',scope):
            raise LeaderError('invalid-service-scope')
        self.name='Local\\pm-kit-bridge-'+scope
        self.endpoint=r'\\.\pipe\LOCAL\pm-kit-bridge-'+scope
        self.thread=threading.get_ident()
        self.handle=self.thread_handle=self.listener=None
        self.acquired,self.abandoned=False,False
        with _lock:
            if self.name in _owners: raise LeaderError('service-already-owned')
            _owners[self.name]=self
        try:
            user=process_sids(os.getpid())[0]
            security=w.LPVOID()
            if not from_sddl('O:%sD:P(A;;GA;;;%s)'%(user,user),1,ctypes.byref(security),None):
                raise LeaderError('service-acl-unavailable')
            try:
                attributes=SecurityAttributes(ctypes.sizeof(SecurityAttributes),security,False)
                self.handle=create_mutex(ctypes.byref(attributes),self.name,0,0x00120001)
            finally: local_free(security)
            if not self.handle: raise LeaderError('service-mutex-unavailable')
            owner,actual=descriptor(self.handle)
            if owner!=user or actual.split('D:',1)[1] not in (
                    'P(A;;GA;;;%s)'%user,'P(A;;0x1f0001;;;%s)'%user):
                raise LeaderError('service-acl-mismatch')
            outcome=wait(self.handle,0)
            if outcome not in (0,0x80): raise LeaderError('service-already-owned')
            self.acquired,self.abandoned=True,outcome==0x80
            self.thread_handle=open_thread(0x00100000,False,thread_id())
            if not self.thread_handle: raise LeaderError('supervisor-thread-unavailable')
            self.listener=PrivateListener(self.endpoint)
        except Exception:
            self.close()
            raise LeaderError('service-ownership-unavailable') from None

    def owns(self):
        return (self.acquired and self.handle is not None and self.thread_handle is not None
            and wait(self.thread_handle,0)==258 and self.listener is not None and self.listener.handle is not None)

    def close(self):
        if threading.get_ident()!=self.thread:
            raise LeaderError('supervisor-owner-thread-required')
        if self.listener is not None:
            self.listener.close()
            self.listener=None
        if self.acquired:
            if not release(self.handle): raise LeaderError('service-mutex-release-failed')
            self.acquired=False
        for field in ('thread_handle','handle'):
            value=getattr(self,field)
            if value is not None:
                close(value)
                setattr(self,field,None)
        with _lock:
            if _owners.get(self.name) is self: del _owners[self.name]

    def __enter__(self): return self

    def __exit__(self,*error): self.close()
