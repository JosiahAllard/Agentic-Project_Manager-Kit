"""Prepare inert bridge artifacts in a new private Windows directory.

This materializer neither installs native configuration nor starts the runtime.
The startup validator checks the explicit document, and original native profiles
remain references only. Files are exclusive creations; retained object handles
make rollback independent of a replacement at the same pathname.
"""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass,field
import hashlib
import json
import msvcrt
import os
from pathlib import Path

from optional.bridge.core.messages import DeliveryError
from kitlib.proc.pipe import (K,INVALID,bind,close,
    create_file,from_sddl,local_free,process_sids)
from optional.bridge import registry
from optional.bridge.paths import FRONTEND_REL,SERVICE_REL,kit_file
from optional.bridge.port import InstallFragment
from .runtime import parse_config
from .evidence import document,regular,stable_bytes,version
from .lifecycle import _verify_root
from .launch import PYTHON_FLAGS
from .startup import render as render_startup,task_name


set_information=bind(K,'SetFileInformationByHandle',
    [w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD],w.BOOL)


class _UnicodeString(ctypes.Structure):
    _fields_=[('Length',w.USHORT),('MaximumLength',w.USHORT),('Buffer',w.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_=[('Length',w.ULONG),('RootDirectory',w.HANDLE),
        ('ObjectName',ctypes.POINTER(_UnicodeString)),('Attributes',w.ULONG),
        ('SecurityDescriptor',w.LPVOID),('SecurityQualityOfService',w.LPVOID)]


class _Status(ctypes.Union):
    _fields_=[('Status',w.LONG),('Pointer',w.LPVOID)]


class _IoStatus(ctypes.Structure):
    _fields_=[('Status',_Status),('Information',ctypes.c_size_t)]


class _FileId(ctypes.Structure):
    _fields_=[('VolumeSerialNumber',ctypes.c_ulonglong),('FileId',ctypes.c_ubyte*16)]


nt_create_file=bind(ctypes.WinDLL('ntdll',use_last_error=True),'NtCreateFile',
    [ctypes.POINTER(w.HANDLE),w.ULONG,ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatus),w.LPVOID,w.ULONG,w.ULONG,w.ULONG,w.ULONG,w.LPVOID,w.ULONG],w.LONG)
get_information=bind(K,'GetFileInformationByHandleEx',
    [w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD],w.BOOL)


def _handle_identity(handle):
    info=_FileId()
    if not get_information(handle,18,ctypes.byref(info),ctypes.sizeof(info)):
        raise DeliveryError('installation-object-identity-unavailable')
    return info.VolumeSerialNumber,int.from_bytes(bytes(info.FileId),'little')


def _create_directory_handle(path):
    # NtCreateFile FILE_CREATE returns the new directory handle in the same
    # operation. No subsequent path-open or first stat can confer ownership.
    parent_identity=_identity(regular(path.parent,directory=True))
    parent=create_file(str(path.parent),0x200a0,7,None,3,0x02200000,None)
    if parent==INVALID: raise DeliveryError('installation-parent-unavailable')
    security=w.LPVOID()
    try:
        if (_handle_identity(parent)!=parent_identity
                or _identity(regular(path.parent,directory=True))!=parent_identity):
            raise DeliveryError('installation-parent-changed')
        leaf=path.name.encode('utf-16-le')
        if not 0<len(leaf)<=65532 or '\x00' in path.name:
            raise DeliveryError('invalid-installation-destination')
        buffer=ctypes.create_string_buffer(leaf+b'\x00\x00')
        name=_UnicodeString(len(leaf),len(leaf)+2,ctypes.cast(buffer,w.LPWSTR))
        user=process_sids(os.getpid())[0]
        if not from_sddl('O:%sD:P(A;OICI;GA;;;%s)'%(user,user),1,ctypes.byref(security),None):
            raise DeliveryError('installation-private-acl-unavailable')
        attributes=_ObjectAttributes(ctypes.sizeof(_ObjectAttributes),parent,
            ctypes.pointer(name),0x40,security,None)
        handle=w.HANDLE()
        io=_IoStatus()
        status=nt_create_file(ctypes.byref(handle),0x130080,ctypes.byref(attributes),
            ctypes.byref(io),None,0x10,7,2,0x21,None,0)
        if status!=0 or io.Information!=2 or not handle.value or handle.value==INVALID:
            # A returned handle without FILE_CREATED is never adopted/deleted.
            if handle.value and handle.value!=INVALID: close(handle.value)
            raise DeliveryError('installation-destination-exists-or-unavailable')
        return handle.value
    finally:
        if security: local_free(security)
        close(parent)


@dataclass(frozen=True)
class PreparedFile:
    path:Path=field(repr=False)
    role:str
    sha256:str


@dataclass(frozen=True)
class PathBinding:
    role:str
    path:Path=field(repr=False)


@dataclass(frozen=True)
class PreparedInstallation:
    root:Path=field(repr=False)
    files:tuple
    dependencies:tuple
    bindings:tuple

    @property
    def manifest(self):
        return next(item for item in self.files if item.role=='manifest')


def _packet(value,limit=64*1024):
    try:
        data=(json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2,
            allow_nan=False)+'\n').encode('utf-8')
        if not 0<len(data)<=limit: raise ValueError()
        return data
    except Exception: raise DeliveryError('invalid-installation-document') from None


def _identity(info): return info.st_dev,info.st_ino


def _version(info): return version(info)


def _dispose(handle):
    # FileDispositionInfo applies to this pinned object, never a pathname that
    # could now designate a foreign replacement. A nonempty directory refuses.
    delete=ctypes.c_ubyte(1)
    return bool(set_information(handle,4,ctypes.byref(delete),ctypes.sizeof(delete)))


class _Root:
    def __init__(self,path):
        self.path,self.fd,self.identity=path,None,None
        self.handle=_create_directory_handle(path)
        try:
            self.identity=_handle_identity(self.handle)
            self.fd=msvcrt.open_osfhandle(int(self.handle),os.O_RDONLY|os.O_BINARY)
            self.require()
            _verify_root(path)
        except Exception:
            removed=self.discard()
            self.close()
            if not removed:
                # Creation succeeded but no owned object was proved deletable.
                # No path-based fallback may adopt or delete a replacement.
                raise DeliveryError('installation-directory-cleanup-unconfirmed') from None
            raise

    def require(self):
        if (self.fd is None or _handle_identity(self.handle)!=self.identity
                or _identity(os.fstat(self.fd))!=self.identity
                or _identity(regular(self.path,directory=True))!=self.identity):
            raise DeliveryError('installation-directory-changed')
        for parent in self.path.parents: regular(parent,directory=True)

    def discard(self):
        try:
            if (self.handle is None or self.identity is None
                    or _handle_identity(self.handle)!=self.identity):
                return False
            return _dispose(self.handle)
        except Exception: return False

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd=None
        elif self.handle is not None: close(self.handle)
        self.handle=None


class _File:
    def __init__(self,path,stream):
        self.path,self.stream=path,stream
        self.seal(b'')

    def seal(self,data):
        self.data,self.stamp=data,_version(os.fstat(self.stream.fileno()))

    def unchanged(self):
        try:
            before=os.fstat(self.stream.fileno())
            if _version(before)!=self.stamp: return False
            self.stream.seek(0)
            data=self.stream.read(len(self.data)+1)
            return data==self.data and _version(os.fstat(self.stream.fileno()))==self.stamp
        except Exception: return False

    def require(self):
        if (not self.unchanged()
                or _identity(regular(self.path))!=_identity(os.fstat(self.stream.fileno()))):
            raise DeliveryError('installation-file-changed')

    def discard(self):
        if self.unchanged(): _dispose(msvcrt.get_osfhandle(self.stream.fileno()))

    def close(self): self.stream.close()


def _write_file(path,data):
    handle=create_file(str(path),0xC0010000,7,None,1,0x80,None)
    if handle==INVALID: raise DeliveryError('installation-file-exists-or-unavailable')
    try: fd=msvcrt.open_osfhandle(int(handle),os.O_RDWR|os.O_BINARY)
    except Exception:
        _dispose(handle)
        close(handle)
        raise
    try: stream=os.fdopen(fd,'w+b',buffering=0)
    except Exception:
        _dispose(msvcrt.get_osfhandle(fd))
        os.close(fd)
        raise
    owned=_File(path,stream)
    try:
        count=stream.write(data)
        owned.seal(data[:count])
        if count!=len(data): raise DeliveryError('installation-file-write-incomplete')
        os.fsync(fd)
        owned.require()
        return owned
    except Exception:
        owned.discard()
        owned.close()
        raise


def _destination(value,config,bindings):
    try:
        path=Path(value)
        if (not path.is_absolute() or path==Path(path.anchor)
                or ':' in path.name or path.name.rstrip(' .')!=path.name):
            raise DeliveryError('invalid-installation-destination')
        # No new ancestors, existing directory adoption, or reparse traversal.
        for ancestor in (path.parent,*path.parent.parents): regular(ancestor,directory=True)
        path=path.resolve(strict=False)
        try: path.lstat()
        except FileNotFoundError: pass
        else: raise DeliveryError('installation-destination-exists-or-unavailable')
        for protected in (config.kit_root,*(item.path for item in bindings)):
            if path==protected or path in protected.parents or protected in path.parents:
                raise DeliveryError('installation-destination-overlaps-native-path')
        return path
    except DeliveryError: raise
    except Exception: raise DeliveryError('invalid-installation-destination') from None


def _bindings(config):
    result=[PathBinding('state-root',config.state_root),
        PathBinding('service-marker',config.state_root/'service.json')]
    for kind,images in config.native_hosts.items():
        result.extend(PathBinding(kind+'.native-image.'+str(index),image) for index,image in enumerate(images))
    for kind,profile in config.profiles.items():
        result.extend(PathBinding(kind+'.'+name,Path(value)) for name,value in profile.items()
            if Path(value).is_absolute())
    return tuple(result)


def _render(config,output,hosts=None):
    """Every prepared file: the runtime config, each host's MCP server entry (mcp.json) and the
    files its half adds (port.InstallFragment), and the startup task. Nothing here names a host:
    a profile whose host has no half, or whose half cannot install, refuses."""
    hosts=registry.installed() if hosts is None else hosts
    canonical={'protocol':1,'kit_root':str(config.kit_root),'python':str(config.python),
        'state_root':str(config.state_root),'host_id':config.host_id,
        'native_hosts':{kind:[str(image) for image in images] for kind,images in config.native_hosts.items()},
        'profiles':config.profiles}
    marker=config.state_root/'service.json'
    frontend=kit_file(FRONTEND_REL,config.kit_root)
    service=kit_file(SERVICE_REL,config.kit_root)
    dependencies=[PathBinding('python',config.python),PathBinding('mcp-frontend',frontend),
        PathBinding('service-entry',service)]
    command={'command':str(config.python),'args':[*PYTHON_FLAGS,str(frontend),str(marker)]}
    servers,extra={},[]
    for kind,profile in config.profiles.items():
        host=hosts.get(kind)
        if host is None or host.install is None: raise DeliveryError('native-install-unavailable')
        fragment=host.install(profile,python=config.python,marker=marker,kit_root=config.kit_root)
        if (type(fragment) is not InstallFragment or type(fragment.server) is not dict
                or type(fragment.server.get('name')) is not str or 'configuration' in fragment.server):
            raise DeliveryError('invalid-install-fragment')
        servers[kind]={**fragment.server,'configuration':command}
        dependencies.extend(PathBinding(role,Path(path)) for role,path in fragment.dependencies)
        for name,role,document_value in fragment.files:
            if (type(name) is not str or not name.startswith(kind+'-') or '/' in name or '\\' in name
                    or name in ('runtime.json','mcp.json','manifest.json')):
                raise DeliveryError('invalid-install-fragment')
            extra.append((name,role,_packet(document_value)))
    payloads=[('runtime.json','runtime-config',_packet(canonical)),
        ('mcp.json','native-mcp-fragments',_packet({'protocol':1,'servers':servers})),*extra]
    if len({name for name,_,_ in payloads})!=len(payloads): raise DeliveryError('invalid-install-fragment')
    for item in dependencies: regular(item.path)
    payloads.extend((name,'windows-startup-'+name,data) for name,data in render_startup(config,output).items())
    return payloads,tuple(dependencies)


def prepare(config_document,output_directory,hosts=None):
    """Return immutable file/path metadata for a prepared, inactive installation.

    The output must be a new directory beneath an existing regular parent and
    disjoint from native profiles, remembered state and installed kit. It is not
    a service state directory. The manifest names review artifacts and required
    original paths; it does not guess native settings files or grant authority.
    """
    root=None
    owned=[]
    try:
        # Roundtrip creates a bounded immutable JSON snapshot. Schema and path
        # policy are the very same validator called by actual runtime startup.
        config=parse_config(document(_packet(config_document)))
        bindings=_bindings(config)
        output=_destination(output_directory,config,bindings)
        payloads,dependencies=_render(config,output,hosts)
        root=_Root(output)
        files=[]
        for name,role,data in payloads:
            root.require()
            path=output/name
            owned.append(_write_file(path,data))
            files.append(PreparedFile(path,role,hashlib.sha256(data).hexdigest()))
        manifest={'protocol':1,'source':{'kit_root':str(config.kit_root),'module':__name__},
            'role':'installation-materializer','scope':'prepared,notactivated',
            'active_capabilities':[],
            'startup':{'task_name':task_name(config),'registered':False,
                'definition':str(output/'startup-task.xml'),'launcher':str(output/'start-bridge.ps1')},
            'files':[{'role':item.role,'destination':str(item.path),'sha256':item.sha256} for item in files],
            'dependencies':[{'role':item.role,'path':str(item.path)} for item in dependencies],
            'bindings':[{'role':item.role,'path':str(item.path)} for item in bindings]}
        root.require()
        data=_packet(manifest)
        owned.append(_write_file(output/'manifest.json',data))
        files.append(PreparedFile(output/'manifest.json','manifest',hashlib.sha256(data).hexdigest()))
        root.require()
        for item in owned: item.require()
        return PreparedInstallation(output,tuple(files),dependencies,bindings)
    except Exception as error:
        for item in reversed(owned):
            item.discard()
            item.close()
        owned.clear()
        if root is not None: root.discard()
        if isinstance(error,DeliveryError): raise
        raise DeliveryError('installation-preparation-failed') from None
    finally:
        for item in reversed(owned): item.close()
        if root is not None: root.close()


def prepare_file(config_path,output_directory,hosts=None):
    """Read the bounded operator input and prepare review artifacts only."""
    return prepare(document(stable_bytes(Path(config_path),64*1024)),output_directory,hosts)
