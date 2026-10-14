"""Windows Claude protocol-1 peer adapter and persistent authenticated return pipe.

Protocol fields are observed native interfaces, not a public compatibility promise.
Both terminal and desktop use this transport. Native permissions are retained;
host account, binding and sender-mode observers remain mandatory trusted ports.
"""
import ctypes
from ctypes import wintypes as w
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from urllib.parse import quote, unquote
import uuid

from optional.bridge.hosts.claude.sessions import ClaudeDirectory, ClaudeSession, canonical_pipe
from optional.bridge.core.contracts import AdapterDescription, Capability
from optional.bridge.core.jsonwire import JsonLineDecoder, encode_frame
from optional.bridge.core.messages import BeforeWriteError, DeliveryError, NativeResult, OutboundMessage
from optional.bridge.core.prompt import delivery_prompt
from kitlib.proc.pipe import PrivateListener, client_pid, peek, protect_file, read
from optional.bridge.win.transport import PipeTarget, write_peer
from kitlib.proc.process import own_ctime

MAX_PENDING = 512
STATES = {'held':'held', 'refused':'refused', 'denied':'refused', 'expired':'expired'}


def peer_address(pipe):
    canonical_pipe(pipe)  # accepts only local Claude peer pipes.
    return 'uds:' + quote(pipe, safe=':_/.-')


def native_message_id(message):
    if type(message) is not OutboundMessage:
        raise BeforeWriteError('invalid-native-message')
    # Deterministic across a broker restart, but distinct for different callers,
    # roots, bodies, selected recipients and expiries. This is not caller proof.
    content = json.dumps([message.operation_id, message.task_root,
        message.sender.reference, message.recipient.reference,
        message.intent.body_sha256, message.expires_at], separators=(',',':'))
    return str(uuid.uuid5(uuid.NAMESPACE_OID, 'pm-kit-claude-peer-v1:' + content))


class ClaudePeerEndpoint:
    """An owned peer entry; never adopts, sweeps or edits another native entry.

    Receives control receipts only. Ordinary text lacks reliable in-reply-to
    evidence and is deliberately not assigned to a pending request. Replies go
    through the caller-authenticated shared-core bridge_reply API.
    """
    def __init__(self, registry_dir, workspace, directory):
        if type(directory) is not ClaudeDirectory:
            raise DeliveryError('invalid-peer-directory')
        self.registry = Path(registry_dir).resolve(strict=True)
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.registry.is_dir() or not self.workspace.is_dir():
            raise DeliveryError('invalid-peer-profile')
        self.directory = directory
        self.pipe = r'\\.\pipe\LOCAL\cc-msg-kit-' + secrets.token_hex(16)
        self.registry_path = self.registry / ('%d.json' % os.getpid())
        self.key_path = self.registry / ('%d.%s.key' % (
            os.getpid(), hashlib.sha256(canonical_pipe(self.pipe).encode()).hexdigest()))
        self._token = secrets.token_hex(16)
        self._owned, self._pending, self._results = {}, {}, {}
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._listener, self._thread, self._fault, self._closed = None, None, False, False
        try:
            if self.registry_path.exists():
                raise DeliveryError('peer-registration-already-exists')
            creation = str(own_ctime())
            if creation == '0':
                raise DeliveryError('peer-process-identity-unavailable')
            self._listener = PrivateListener(self.pipe)
            # Publish the key first. Restrict newly created files before writing
            # their data; advertise the registry only after the listener exists.
            self._publish(self.key_path, {'peerToken': self._token,
                'procStart':creation, 'procStartFt':creation})
            self._publish(self.registry_path, {'pid':os.getpid(), 'sessionId':str(uuid.uuid4()),
                'cwd':str(self.workspace), 'startedAt':int(time.time()*1000),
                'procStart':creation, 'procStartFt':creation, 'peerProtocol':1,
                'kind':'interactive', 'entrypoint':'kit-agent-bridge',
                'messagingSocketPath':self.pipe, 'name':'PM kit bridge', 'nameSource':'derived'})
            self._thread = threading.Thread(target=self._receive, name='bridge-claude-peer', daemon=True)
            self._thread.start()
        except Exception:
            self.close()
            raise DeliveryError('peer-endpoint-start-failed') from None

    def _publish(self, path, value):
        data = json.dumps(value,separators=(',',':')).encode('utf-8')
        with path.open('xb') as output:
            info = path.stat()
            self._owned[path] = (info.st_dev, info.st_ino, b'')
            protect_file(path)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            self._owned[path] = (info.st_dev, info.st_ino, data)

    def expect(self, message, snapshot):
        with self._lock:
            if self._closed or self._fault or not self._thread.is_alive():
                raise BeforeWriteError('peer-return-endpoint-unavailable')
            native_id = native_message_id(message)
            if native_id in self._pending:
                raise DeliveryError('native-operation-already-attempted')
            if len(self._pending) >= MAX_PENDING:
                raise BeforeWriteError('peer-pending-capacity')
            self._pending[native_id] = (message, snapshot)
            return native_id

    def forget_before_write(self, native_id):
        with self._lock:
            self._pending.pop(native_id,None)
            self._results.pop(native_id,None)

    def result(self, message, native_id):
        expected = native_message_id(message)
        if native_id is not None and native_id != expected:
            raise DeliveryError('native-receipt-correlation')
        with self._lock:
            pending = self._pending.get(expected)
            if pending is None or pending[0] != message:
                return None
            self.directory.require_live(pending[1])
            return self._results.get(expected)

    def _receive(self):
        while not self._stop.is_set():
            try:
                self._listener.accept(timeout=.1)
            except Exception as error:
                if getattr(error,'code',None) == 'private-pipe-accept-timeout':
                    continue
                self._fault = True
                return
            try:
                actual = w.ULONG()
                if not client_pid(self._listener.handle,ctypes.byref(actual)):
                    continue
                frames = self._connection()
                self._accept_frames(actual.value,frames)
            except Exception:
                # Malformed, unauthenticated, stale or oversized peer input is
                # never authority. No bodies, secrets or native errors are logged.
                pass
            finally:
                try:
                    self._listener.reset()
                except Exception:
                    self._fault = True
            if self._fault:
                return

    def _connection(self):
        decoder, frames = JsonLineDecoder(), []
        deadline = time.monotonic()+.5
        while not self._stop.is_set() and time.monotonic() < deadline:
            buffer, count = ctypes.create_string_buffer(4096), w.DWORD()
            if read(self._listener.handle,buffer,len(buffer),ctypes.byref(count),None):
                if count.value:
                    frames.extend(decoder.feed(buffer.raw[:count.value]))
                    if len(frames) > 4:
                        raise DeliveryError('peer-input-frame-limit')
                else:
                    self._stop.wait(.01)
                continue
            error = ctypes.get_last_error()
            if error in (109,233):  # closed, not connected
                decoder.finish()
                return frames
            if error == 232:  # PIPE_NOWAIT: no data yet, not proof of closure.
                # A pipe retains its last client's PID after closure; that PID
                # cannot tell an idle connection from a drained closed one.
                available = w.DWORD()
                if not peek(self._listener.handle,None,0,None,ctypes.byref(available),None):
                    if ctypes.get_last_error() not in (109,232,233):
                        raise DeliveryError('peer-input-unavailable')
                    decoder.finish()
                    return frames
                self._stop.wait(.01)
                continue
            raise DeliveryError('peer-input-unavailable')
        raise DeliveryError('peer-input-timeout')

    def _accept_frames(self, pid, frames):
        if (len(frames) < 2 or set(frames[0]) != {'type','token'} or frames[0]['type'] != 'auth'
                or type(frames[0]['token']) is not str
                or not hmac.compare_digest(frames[0]['token'],self._token)):
            return
        for frame in frames[1:]:
            if frame.get('type') != 'control' or frame.get('action') != 'peer_message_status':
                continue
            native_id, source = frame.get('orig_msg_id'), frame.get('from')
            if type(native_id) is not str or type(source) is not str or not source.startswith('uds:'):
                continue
            try:
                pipe = canonical_pipe(unquote(source[4:],errors='strict'))
            except (ValueError, UnicodeError):
                continue
            # Unknown dropped statuses are not permission refusal. Native
            # status_detail='refused' is the only dropped-to-refused mapping.
            state = 'refused' if frame.get('status_detail') == 'refused' else STATES.get(frame.get('status'))
            if state is None:
                continue
            with self._lock:
                pending = self._pending.get(native_id)
                if pending is None:
                    continue
                message, snapshot = pending
                if snapshot.pid != pid or canonical_pipe(snapshot.pipe) != pipe:
                    continue
                self._listener.verify_client(snapshot.pid,snapshot.creation)
                self.directory.require_live(snapshot)
                previous = self._results.get(native_id)
                if previous is not None and previous.state in ('refused','expired'):
                    continue
                self._results[native_id] = NativeResult(message.operation_id,
                    message.recipient.reference,state,native_id)

    def close(self):
        if self._closed:
            return
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise DeliveryError('peer-listener-cleanup-unconfirmed')
        if self._listener is not None:
            self._listener.close()
        changed = False
        for path, (device,inode,data) in tuple(self._owned.items()):
            try:
                info = path.lstat()
                if ((info.st_dev, info.st_ino) != (device,inode) or info.st_size != len(data)
                        or getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    changed = True
                    continue
                with path.open('rb') as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev,opened.st_ino) != (device,inode) or source.read(len(data)+1) != data:
                        changed = True
                        continue
                path.unlink()
                del self._owned[path]
            except FileNotFoundError:
                del self._owned[path]
        self._closed = True
        if changed:
            raise DeliveryError('peer-owned-registration-changed')


class ClaudePeerAdapter:
    def __init__(self, directory, endpoint, *, resolve=None, sender_mode=None):
        if type(directory) is not ClaudeDirectory or type(endpoint) is not ClaudePeerEndpoint:
            raise DeliveryError('invalid-claude-peer-components')
        self.directory, self.endpoint = directory, endpoint
        self.resolve, self.sender_mode = resolve, sender_mode

    def describe(self):
        return AdapterDescription('claude-peer',(Capability.SEND,Capability.READ_STATUS),
            'Observed protocol 1; native writes are unconfirmed without correlated evidence.')

    def send(self, message):
        try:
            if type(message) is not OutboundMessage or message.recipient.identity.adapter_id != 'claude-peer':
                raise ValueError('invalid target')
            snapshot = self.resolve(message.recipient) if self.resolve is not None else None
            if (type(snapshot) is not ClaudeSession
                    or snapshot.native_session_id != message.recipient.identity.native_session_id
                    or snapshot.account_ref != message.recipient.account_ref):
                raise ValueError('unverified native binding')
            self.directory.require_live(snapshot)
            mode = self.sender_mode(message) if self.sender_mode is not None else None
            if type(mode) is not str or mode not in ('prompting','bypass'):
                raise ValueError('unverified native sender mode')
            token = self.directory.peer_token(snapshot)
            native_id, source = native_message_id(message), peer_address(self.endpoint.pipe)
            text = delivery_prompt(message)
            frame = {'msgV':1,'msg_id':native_id,'uuid':native_id,'type':'user','priority':'next',
                'from':source,'message':{'role':'user','content':
                '<cross-session-message from="'+source+'" from-mode="'+mode+'">\n'+text+'\n</cross-session-message>'}}
            packet = encode_frame({'type':'auth','token':token}) + encode_frame(frame)
        except Exception:
            raise BeforeWriteError('native-claude-context-unverified') from None
        self.endpoint.expect(message,snapshot)
        try:
            # This is a single attempt. Even a lost helper acknowledgement must
            # leave the expectation intact for a later correlated native status.
            write_peer(PipeTarget(snapshot.pipe,snapshot.pid,snapshot.creation),packet)
        except BeforeWriteError:
            self.endpoint.forget_before_write(native_id)
            raise
        result = self.endpoint.result(message,native_id)
        if result is None:
            raise DeliveryError('native-outcome-unconfirmed')
        return result

    def reconcile(self, message, native_id):
        return self.endpoint.result(message,native_id)
