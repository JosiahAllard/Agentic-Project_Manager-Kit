"""Concrete Codex sources for the kernel-verified native MCP frontend.

The controller installs CodexSources with an explicit native profile. Its input
is FrontendInspector output, not model arguments or a self-claimed wire record.
Actual MCP _meta must arrive from that original native parent's stdio channel.
No default mode/account/permission flags, enrollment grants or writer leases are
invented. File-login identity support has CodexAccount's documented limits.

Capture active caller facts once, then verify unchanged persisted facts for idle
liveness. Native channel publication is metadata evidence only, separate from a
selected-route/executor/writer delivery lease. The optional configured direct
adapter composes those facts for two selected Codex chats on the same desktop
app. It never resumes a thread, chooses a model or activates a service.
"""
import ctypes
from contextlib import contextmanager
from ctypes import wintypes as w
from dataclasses import dataclass,field
from pathlib import Path
import threading

from kitlib.ident import identifier
from optional.bridge.core.contracts import AdapterDescription
from optional.bridge.core.messages import BeforeWriteError,DeliveryError,OutboundMessage
from optional.bridge.core.pairing import ChatBinding
from kitlib.proc.pipe import (K,bind,close,open_process,process_image,process_sids)
from optional.bridge.win import frontend as frontend_types
from optional.bridge.win.channel import revalidate_channel
from optional.bridge.win.controller import Controller
from optional.bridge.win.frontend import NativeFrontend
from optional.bridge.port import NativeSource
from optional.bridge.win.transport import PipeTarget
from kitlib.proc.process import handle_ctime,parent_pid
from .account import CodexAccount
from .calls import CodexCalls
from .context import CodexObserver,_sha
from .delivery import CodexDesktopAdapter,DesktopLease
from .delivery_transport import NativeEndpoint

wait=bind(K,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD)
get_pid=bind(K,'GetProcessId',[w.HANDLE],w.DWORD)


@dataclass(frozen=True)
class _Captured:
    facts:object
    metadata:dict=field(repr=False)
    session_sha256:str
    context_sha256:str


class _Runtime:
    def __init__(self,controller,profile,host_id,native):
        self.controller,self.native,self.host_id=controller,native,host_id
        self._lock,self._closed,self._captures=threading.RLock(),False,{}
        self._observe_lock=threading.Lock()
        self._frontend_handle,self.observer=None,None
        try:
            if (type(native) is not NativeFrontend or native.kind not in ('codex','codex-desktop','codex-terminal')
                    or type(native.pid) is not int or type(native.creation) is not int):
                raise DeliveryError('invalid-codex-native-frontend')
            self.observer=CodexObserver(profile,native.host_pid,native.host_creation,native.host_executable)
            self.account=CodexAccount(profile)
            self.calls=CodexCalls(self.observer,self.account,host_id)
            self._frontend_handle=open_process(0x00101000,False,native.pid)
            self._frontend_image=process_image(native.pid)
            self.require()
        except Exception:
            self.close()
            raise DeliveryError('codex-native-frontend-unverified') from None

    def require(self):
        if (self._closed or not self._frontend_handle or get_pid(self._frontend_handle)!=self.native.pid
                or handle_ctime(self._frontend_handle)!=self.native.creation
                or wait(self._frontend_handle,0)!=258 or process_image(self.native.pid)!=self._frontend_image
                or process_sids(self.native.pid)[0]!=self.observer.user
                or parent_pid(self._frontend_handle)!=self.native.host_pid):
            raise DeliveryError('codex-native-frontend-changed')
        self.observer.require_host()

    def alive(self):
        try:
            self.require()
            return True
        except Exception:
            return False

    def observe(self,metadata):
        """Capture state atomically; invoke controller/pairing outside state lock.

        Observations serialize separately. Live checks never take observe_lock,
        so an owner holding pairing.lock can still verify captured state while
        this source waits to enroll. The exact capture object is its epoch.
        """
        with self._observe_lock:
            try:
                with self._lock:
                    self.require()
                    authority=self.calls.observe(metadata)
                    facts=self.observer.observe(metadata)
                    if facts.binding(self.host_id)!=authority.sender:
                        raise ValueError('source changed')
                    if self.native.kind!='codex' and self.native.kind!='codex-'+facts.kind:
                        raise ValueError('frontend kind mismatch')
                    native_meta=metadata['x-codex-turn-metadata']
                    captured_meta={'x-codex-turn-metadata':{key:native_meta[key] for key in (
                        'thread_id','turn_id','thread_source','auto_review_enabled','node_repl_auto_review_required')}}
                    session,context,lifecycle=self.observer._records(self.observer._find(facts.thread_id))
                    captured=_Captured(facts,captured_meta,_sha(session),_sha(context))
                    if len(self._captures)>=128 and facts.thread_id not in self._captures:
                        raise ValueError('source capacity')
                    previous=self._captures.get(facts.thread_id)
                    # Identical re-observation preserves the live epoch. A
                    # changed native context invalidates its previous callbacks
                    # before enrollment; a failed update never restores old flags.
                    if captured==previous:
                        captured=previous
                    self._captures[facts.thread_id]=captured
                    detail={'title':facts.thread_id,'runtime':'codex-'+facts.kind,'mode':facts.mode,
                        'workspace':facts.workspace}
                def verify_epoch(binding):
                    return self.verify(binding,expected=captured)
                self.controller.enroll(authority.sender,verify_epoch,detail)
                if not verify_epoch(authority.sender):
                    raise ValueError('source changed during enrollment')
                return authority
            except Exception:
                raise DeliveryError('codex-native-source-unverified') from None

    def verify(self,binding, *, expected=None):
        """Exact idle liveness, without reusing active caller authority."""
        with self._lock:
            try:
                self.require()
                if type(binding) is not ChatBinding:
                    return False
                captured=self._captures.get(binding.identity.session_id)
                if (captured is None or expected is not None and captured is not expected
                        or captured.facts.binding(self.host_id)!=binding):
                    return False
                before=self.account.observe()
                facts=self.observer.observe_existing(captured.facts,captured.metadata,
                    session_sha256=captured.session_sha256,context_sha256=captured.context_sha256)
                if facts.account_ref!=before or self.account.observe()!=before:
                    return False
                self.require()
                return facts.binding(self.host_id)==binding
            except Exception:
                return False

    def verify_active(self,binding, *, expected):
        """Native executor proof: the captured original source turn is started.

        Idle identity is deliberately insufficient. CodexCalls rechecks current
        login and the active native lifecycle using the trusted captured metadata;
        the exact capture epoch/context must still match before returning.
        """
        with self._lock:
            try:
                if not self.verify(binding,expected=expected):
                    return False
                authority=self.calls.observe(expected.metadata)
                return (authority.sender==binding and self.verify(binding,expected=expected)
                    and self.observer.observe(expected.metadata)==expected.facts)
            except Exception:
                return False

    def snapshot(self,binding):
        with self._lock:
            if self._closed or type(binding) is not ChatBinding:
                return None
            captured=self._captures.get(binding.identity.session_id)
            return captured if captured is not None and captured.facts.binding(self.host_id)==binding else None

    def native_channel(self,binding):
        """Publish only an inspector-verified app-owned ancestor pipe descriptor.

        The native app-server context belongs to codex.exe; its tools server may
        belong to ancestor ChatGPT.exe. They must not be confused or substituted.
        A missing descriptor stays absent. This supplies no delivery authority.
        """
        with self._lock:
            if not self.verify(binding):
                raise DeliveryError('codex-native-channel-context-unverified')
            captured=self._captures[binding.identity.session_id]
            channel=getattr(self.native,'channel',None)
            if captured.facts.kind!='desktop' or channel is None:
                return None
            channel_type=getattr(frontend_types,'NativeChannel',None)
            if (channel_type is None or type(channel) is not channel_type
                    or type(channel.target) is not PipeTarget):
                raise DeliveryError('codex-native-channel-descriptor-unverified')
            try:
                # Shared protected-channel verifier uses metadata-only access;
                # it rechecks kernel server identity, image/user/owner and strict
                # native-host ancestry without read/write rights or native frames.
                current=revalidate_channel(channel.to_wire(),self.native.host_pid,
                    self.native.host_creation,self.observer.user)
                if current!=channel or not self.verify(binding):
                    raise ValueError('source or channel changed')
                target=current.target
                return NativeEndpoint(target.name,target.pid,target.creation,str(current.executable))
            except Exception:
                raise DeliveryError('codex-native-channel-unverified') from None

    def close(self):
        with self._lock:
            self._closed=True
            self._captures.clear()
            if self._frontend_handle:
                close(self._frontend_handle)
                self._frontend_handle=None
            if self.observer is not None:
                self.observer.close()


class CodexSources:
    """Controller-configured factory, not an ordinary MCP tool or ready flag."""
    def __init__(self,controller,profile_root,host_id, *, desktop_executable=None):
        if type(controller) is not Controller:
            raise DeliveryError('invalid-codex-runtime-controller')
        self.controller,self.profile,self.host_id=controller,Path(profile_root),identifier(host_id)
        if not self.profile.is_absolute():
            raise DeliveryError('invalid-codex-runtime-profile')
        self.desktop_executable=None if desktop_executable is None else Path(desktop_executable).resolve(strict=True)
        if self.desktop_executable is not None and not self.desktop_executable.is_file():
            raise DeliveryError('invalid-codex-desktop-image')
        self._lock,self._closed,self._runtimes=threading.RLock(),False,{}

    def __call__(self,native):
        with self._lock:
            if self._closed or type(native) is not NativeFrontend:
                raise DeliveryError('codex-native-factory-unavailable')
            for key,runtime in tuple(self._runtimes.items()):
                if not runtime.alive():
                    runtime.close()
                    del self._runtimes[key]
            key=(native.pid,native.creation)
            runtime=self._runtimes.get(key)
            if runtime is None:
                if len(self._runtimes)>=128:
                    raise DeliveryError('codex-native-factory-capacity')
                runtime=_Runtime(self.controller,self.profile,self.host_id,native)
                self._runtimes[key]=runtime
            elif runtime.native!=native:
                raise DeliveryError('codex-native-frontend-changed')
            return NativeSource(runtime.observe)

    def native_channel(self,binding):
        with self._lock:
            if self._closed or type(binding) is not ChatBinding:
                raise DeliveryError('codex-native-factory-unavailable')
            runtimes=[runtime for runtime in self._runtimes.values() if runtime.verify(binding)]
            if len(runtimes)!=1:
                raise DeliveryError('codex-native-channel-source-unverified')
            return runtimes[0].native_channel(binding)

    def _runtime_list(self):
        with self._lock:
            if self._closed:
                raise DeliveryError('codex-native-factory-unavailable')
            return tuple(self._runtimes.values())

    def _capture(self,binding):
        matches=[]
        for runtime in self._runtime_list():
            captured=runtime.snapshot(binding)
            if captured is not None:
                matches.append((runtime,captured))
        if len(matches)!=1:
            raise BeforeWriteError('codex-direct-capture-unverified')
        return matches[0]

    def sender_mode(self,binding):
        """Private current native mode lookup, not caller choice or executor proof.

        Controller callbacks run outside factory/runtime locks. The exact capture
        epoch remains current across account/context/permission/live checks. An
        idle turn may supply identity/mode, but direct execution stays active-only.
        """
        try:
            self.controller._require()
            runtime,captured=self._capture(binding)
            if (not runtime.verify(binding,expected=captured) or not self.controller._live(binding)
                    or captured.facts.mode not in ('prompting','bypass')):
                raise ValueError('mode unverified')
            self.controller._require()
            if not runtime.verify(binding,expected=captured) or runtime.snapshot(binding) is not captured:
                raise ValueError('mode changed')
            return captured.facts.mode
        except Exception:
            raise DeliveryError('codex-sender-mode-unverified') from None

    def _has_desktop_facility(self):
        try:
            if self.desktop_executable is None:
                return False
            self.controller._require()
            for runtime in self._runtime_list():
                with runtime._lock:
                    captures=tuple(runtime._captures.values())
                for captured in captures:
                    binding=captured.facts.binding(self.host_id)
                    if (captured.facts.kind=='desktop' and runtime.verify(binding,expected=captured)
                            and self.controller._live(binding)):
                        endpoint=runtime.native_channel(binding)
                        if endpoint is not None and Path(endpoint.executable)==self.desktop_executable:
                            return True
            return False
        except Exception:
            return False

    def delivery_adapter(self, *, timeout=5, start_timeout=2):
        """Private bootstrap adapter; no SEND from a configured callback alone."""
        return _DirectDesktopAdapter(self,timeout=timeout,start_timeout=start_timeout)

    @contextmanager
    def _delivery_lease(self,message):
        process=None
        try:
            if (type(message) is not OutboundMessage or self.desktop_executable is None
                    or message.sender.identity.adapter_id!='codex-desktop'
                    or message.recipient.identity.adapter_id!='codex-desktop'
                    or message.sender.identity.native_session_id==message.recipient.identity.native_session_id
                    or message.sender.account_ref!=message.recipient.account_ref):
                raise BeforeWriteError('codex-direct-route-unavailable')
            source,source_capture=self._capture(message.sender)
            target,target_capture=self._capture(message.recipient)
            source_channel=getattr(source.native,'channel',None)
            target_channel=getattr(target.native,'channel',None)
            source_endpoint=source.native_channel(message.sender)
            target_endpoint=target.native_channel(message.recipient)
            image=self.desktop_executable
            if (source_endpoint is None or source_endpoint!=target_endpoint or source_channel!=target_channel
                    or Path(source_endpoint.executable)!=image):
                raise BeforeWriteError('codex-direct-app-channel-mismatch')
            process=open_process(0x00101000,False,source_endpoint.pid)
            def recheck():
                # No controller/pairing callbacks execute under factory/runtime
                # locks. Core owns selected_guard; this only reads its route.
                try:
                    self._runtime_list()
                    self.controller._require()
                    self.controller.pairs.require_selected(message.sender,message.recipient)
                    if (not self.controller._live(message.sender) or not self.controller._live(message.recipient)
                            or not source.verify_active(message.sender,expected=source_capture)
                            or not target.verify(message.recipient,expected=target_capture)
                            or getattr(source.native,'channel',None)!=source_channel
                            or getattr(target.native,'channel',None)!=target_channel
                            or self.desktop_executable!=image or not process
                            or get_pid(process)!=source_endpoint.pid
                            or handle_ctime(process)!=source_endpoint.creation or wait(process,0)!=258
                            or process_image(source_endpoint.pid)!=image
                            or process_sids(source_endpoint.pid)[0]!=source.observer.user
                            or source.observer.user!=target.observer.user):
                        return False
                    # The helper pins/checks the actual pipe at connect/write;
                    # retained app identity and capture epochs fence this lease.
                    self.controller._require()
                    return (source.snapshot(message.sender) is source_capture
                        and target.snapshot(message.recipient) is target_capture)
                except Exception:
                    return False
            if not recheck():
                raise BeforeWriteError('codex-direct-context-unverified')
            yield DesktopLease(message.recipient,source_capture.facts,source_endpoint,recheck)
        finally:
            if process: close(process)

    def close(self):
        with self._lock:
            self._closed=True
            for runtime in self._runtimes.values(): runtime.close()
            self._runtimes.clear()


class _DirectDesktopAdapter(CodexDesktopAdapter):
    def __init__(self,factory, *, timeout, start_timeout):
        self.factory=factory
        super().__init__(lease=factory._delivery_lease,timeout=timeout,start_timeout=start_timeout)

    def describe(self):
        if not self.factory._has_desktop_facility():
            return AdapterDescription('codex-desktop',(),
                'Requires a configured desktop executable and current verified app-owned channel; '
                'direct distinct selected Codex sources only; cross-host delegation unavailable.')
        return super().describe()
