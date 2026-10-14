"""Bounded stable Windows native evidence; rejects links and replacement races."""
import ctypes
import json
import msvcrt
import os
from pathlib import Path
import stat

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
    DELETE-access handle on it for a moment after the name appears. For the same reason, while
    such a reader holds the file, the writer's own os.replace or unlink of it fails. Substitution
    is still caught: stable_bytes compares the file's version before, during and after the read."""
    handle=create_file(str(path),GENERIC_READ,SHARE_ALL,None,OPEN_EXISTING,0,None)
    if handle in (None,INVALID):
        error=ctypes.get_last_error()
        raise OSError(None,ctypes.FormatError(error).strip(),str(path),error)
    try: descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY)
    except BaseException:
        close(handle)
        raise
    return os.fdopen(descriptor,'rb')


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
