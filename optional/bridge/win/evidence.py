"""Bounded stable Windows native evidence; rejects links and replacement races."""
import ctypes
import json
import msvcrt
import os
from pathlib import Path
import stat
import time

from kitlib.proc.pipe import INVALID,close,create_file

GENERIC_READ=0x80000000
SHARE_ALL=7  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING=3


class EvidenceError(ValueError):
    def __init__(self):
        self.code='native-evidence-unavailable'
        super().__init__('native evidence unavailable')


def regular(path, *, directory=False):
    path=Path(path)
    info=path.lstat()
    if (path.is_symlink() or getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise EvidenceError()
    return info


def version(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,
        getattr(info,'st_birthtime_ns',info.st_ctime_ns))


def _open_shared(path):
    """Open for reading, sharing read, write AND delete.

    Python's open() does not share delete. So it fails with a sharing violation (PermissionError
    13) while os.replace is still landing the file under this name, because MoveFileEx holds a
    DELETE-access handle on it for a moment after the name appears. Sharing delete also lets a
    writer unlink the file while this reader holds it. It does not let a writer os.replace over it:
    replacing a file that any handle holds open fails (WinError 5, access denied) whatever that
    handle shares, so a writer replacing a file these reads may hold uses replace() below, which
    waits the reader out. Substitution is still caught: stable_bytes compares the file's version
    before, during and after the read.

    CreateFileW failing raises OSError carrying its Windows error, so a missing file is
    FileNotFoundError (winerror 2) and a holder that shares nothing is PermissionError (32)."""
    handle=create_file(str(path),GENERIC_READ,SHARE_ALL,None,OPEN_EXISTING,0,None)
    if handle in (None,INVALID):
        error=ctypes.get_last_error()
        raise OSError(None,ctypes.FormatError(error).strip(),str(path),error)
    try: descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY)
    except BaseException:
        close(handle)
        raise
    return os.fdopen(descriptor,'rb')


REPLACE_BUDGET=1.0  # seconds a replace waits for readers before failing as it always did


def replace(source, target, *, budget=REPLACE_BUDGET):
    """os.replace that waits out readers holding target open.

    A stable_bytes read holds the file for the length of one bounded read, and while it does the
    replace fails with WinError 5 (or 32 against a reader that does not share delete). Only those
    two are retried, with short doubling sleeps, until budget seconds have passed; then the last
    error is raised, as os.replace would have. Any other error is raised at once."""
    deadline=time.monotonic()+budget
    delay=0.005
    while True:
        try: return os.replace(source,target)
        except PermissionError as error:
            if getattr(error,'winerror',None) not in (5,32): raise
            remaining=deadline-time.monotonic()
            if remaining<=0: raise
            time.sleep(min(delay,remaining))
            delay=min(delay*2,0.1)


def stable_bytes(path, limit):
    if type(limit) is not int or not 0<limit<=4*1024*1024: raise EvidenceError()
    before=regular(path)
    if not 0<before.st_size<=limit: raise EvidenceError()
    with _open_shared(path) as source:
        opened=os.fstat(source.fileno())
        if version(opened)!=version(before): raise EvidenceError()
        data=source.read(limit+1)
        after=os.fstat(source.fileno())
        if (len(data)!=before.st_size or version(after)!=version(opened)
                or after.st_ctime_ns!=opened.st_ctime_ns or version(regular(path))!=version(after)):
            raise EvidenceError()
    return data


def document(data):
    def pairs(values):
        out={}
        for key,value in values:
            if key in out: raise EvidenceError()
            out[key]=value
        return out
    def invalid(value): raise EvidenceError()
    try:
        result=json.loads(data.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid)
        if type(result) is not dict: raise EvidenceError()
        # JSON decoding already bounds recursion; explicitly limit retained
        # structure as well. Values are never logged or rendered by this reader.
        nodes,stack=0,[(result,0)]
        while stack:
            value,depth=stack.pop()
            nodes+=1
            if nodes>20000 or depth>64: raise EvidenceError()
            if type(value) is dict: stack.extend((item,depth+1) for item in value.values())
            elif type(value) is list: stack.extend((item,depth+1) for item in value)
        return result
    except Exception:
        raise EvidenceError() from None
