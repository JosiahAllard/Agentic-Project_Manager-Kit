"""Existing Windows Claude session discovery and retained native process identity.

No sessions are launched or adopted, no registry/key files are changed, and no
role is inferred from labels. Account provenance is a host port; absent evidence
leaves a candidate unavailable rather than granting access from a live PID.
"""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import threading

from optional.bridge.hosts.claude.records import read_record
from kitlib.proc.pipe import K, bind, close, process_image, process_sids, open_process
from kitlib.proc.process import handle_ctime

get_pid = bind(K, 'GetProcessId', [w.HANDLE], w.DWORD)
wait = bind(K, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
MAX_RECORDS = 4096
PIPE = re.compile(r'\\\\\.\\pipe\\(?:(LOCAL)\\)?(cc-msg-[A-Za-z0-9_-]+)\Z', re.I)


class NativeSessionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('native Claude session unavailable: ' + code)


@dataclass(frozen=True)
class ClaudeSession:
    native_session_id: str
    pid: int
    creation: int
    kind: str
    pipe: str = field(repr=False)
    workspace: str = field(repr=False)
    workspace_identity: tuple
    account_ref: str = field(repr=False)
    title: str | None = field(default=None, compare=False)
    executable: Path | None = field(default=None, repr=False)


def canonical_pipe(value):
    if type(value) is not str:
        raise NativeSessionError('invalid-local-peer-pipe')
    match = PIPE.fullmatch(value)
    if match is None:
        raise NativeSessionError('invalid-local-peer-pipe')
    return '\\\\.\\pipe\\' + ('local\\' if match[1] else '') + match[2].lower()


class ClaudeDirectory:
    """Controller-configured profile and exact immutable native images."""
    def __init__(self, registry_dir, native_executable, *, account_probe=None):
        self.registry = Path(registry_dir).resolve(strict=True)
        supplied = native_executable if type(native_executable) is tuple else (native_executable,)
        try:
            if not 1 <= len(supplied) <= 8:
                raise NativeSessionError('invalid-native-profile')
            images = []
            for value in supplied:
                raw = Path(value)
                if type(native_executable) is tuple and not raw.is_absolute():
                    raise NativeSessionError('invalid-native-profile')
                image = raw.resolve(strict=True)
                if not image.is_file() or image in images:
                    raise NativeSessionError('invalid-native-profile')
                images.append(image)
            self._executables = tuple(images)
        except (OSError, TypeError, ValueError):
            raise NativeSessionError('invalid-native-profile') from None
        if not self.registry.is_dir():
            raise NativeSessionError('invalid-native-profile')
        if account_probe is not None and not callable(account_probe):
            raise NativeSessionError('invalid-account-verifier')
        self.account_probe = account_probe
        self.user = process_sids(os.getpid())[0]
        self._lock, self._handles, self._closed = threading.RLock(), {}, False

    @property
    def executables(self):
        """Explicit allowed images; no basename or first-member fallback."""
        return self._executables

    @property
    def executable(self):
        """Legacy singleton interface; ambiguous multi-image access fails closed."""
        if len(self._executables) != 1:
            raise NativeSessionError('multiple-native-executables')
        return self._executables[0]

    def _record(self, pid):
        try:
            return read_record(self.registry / ('%d.json' % pid))
        except Exception:
            raise NativeSessionError('registry-evidence-unavailable') from None

    def _account(self, record):
        try:
            value = self.account_probe(record, self.registry) if self.account_probe is not None else None
        except Exception:
            value = None
        if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
            raise NativeSessionError('native-account-unverified')
        return value

    def _observe(self, pid):
        if self._closed:
            raise NativeSessionError('native-directory-closed')
        record = self._record(pid)
        if type(record.get('pid')) is not int or record['pid'] != pid or pid <= 0:
            raise NativeSessionError('registry-pid-mismatch')
        if type(record.get('peerProtocol')) is not int or record['peerProtocol'] != 1:
            raise NativeSessionError('unsupported-peer-protocol')
        kind = {'cli':'terminal', 'claude-desktop':'desktop'}.get(record.get('entrypoint'))
        if kind is None:
            raise NativeSessionError('unsupported-native-entrypoint')
        session = record.get('sessionId')
        if type(session) is not str or not 1 <= len(session) <= 128:
            raise NativeSessionError('invalid-native-session-id')
        pipe = canonical_pipe(record.get('messagingSocketPath'))
        raw_workspace = record.get('cwd')
        if type(raw_workspace) is not str or not Path(raw_workspace).is_absolute():
            raise NativeSessionError('native-workspace-unverified')
        workspace = Path(raw_workspace).resolve(strict=True)
        info = workspace.stat()
        if not workspace.is_dir():
            raise NativeSessionError('native-workspace-unverified')
        handle = open_process(0x00101000, False, pid)
        if not handle:
            raise NativeSessionError('native-process-unavailable')
        try:
            creation = handle_ctime(handle)
            image = process_image(pid)
            if (not creation or get_pid(handle) != pid or wait(handle, 0) != 258
                    or process_sids(pid)[0] != self.user
                    or image not in self.executables):
                raise NativeSessionError('native-process-identity-mismatch')
            facts = [record[key] for key in ('procStart','procStartFt') if key in record]
            if not facts or any(value != str(creation) for value in facts):
                raise NativeSessionError('native-process-creation-mismatch')
            account = self._account(record)
            title = record.get('name')
            title = title if type(title) is str and len(title) <= 256 else None
            if (handle_ctime(handle) != creation or wait(handle, 0) != 258 or process_image(pid) != image):
                raise NativeSessionError('native-process-identity-mismatch')
            snapshot = ClaudeSession(session, pid, creation, kind, pipe, str(workspace),
                (info.st_dev, info.st_ino), account, title, image)
            previous = self._handles.get(pid)
            if previous is not None:
                close(previous)
            self._handles[pid] = handle
            handle = None
            return snapshot
        finally:
            if handle:
                close(handle)

    def discover(self):
        with self._lock:
            if self._closed:
                raise NativeSessionError('native-directory-closed')
            paths, scanned = [], 0
            for path in self.registry.iterdir():
                scanned += 1
                if scanned > MAX_RECORDS:
                    raise NativeSessionError('native-registry-limit')
                if path.suffix == '.json' and path.stem.isascii() and path.stem.isdigit():
                    paths.append(path)
            found = []
            for path in sorted(paths):
                try:
                    found.append(self._observe(int(path.stem)))
                except (NativeSessionError, OSError):
                    continue
            live = {item.pid for item in found}
            for pid in tuple(self._handles):
                if pid not in live:
                    close(self._handles.pop(pid))
            return tuple(found)

    def require_live(self, snapshot):
        if type(snapshot) is not ClaudeSession:
            raise NativeSessionError('invalid-native-snapshot')
        with self._lock:
            if self._observe(snapshot.pid) != snapshot:
                raise NativeSessionError('native-binding-changed')
            return True

    def peer_token(self, snapshot):
        with self._lock:
            self.require_live(snapshot)
            canonical = canonical_pipe(snapshot.pipe)
            key = self.registry / ('%d.%s.key' % (
                snapshot.pid, hashlib.sha256(canonical.encode('utf-8')).hexdigest()))
            try:
                value = read_record(key)
            except Exception:
                raise NativeSessionError('external-peer-key-unavailable') from None
            facts = [value[field] for field in ('procStart','procStartFt') if field in value]
            token = value.get('peerToken')
            if (not facts or any(item != str(snapshot.creation) for item in facts)
                    or type(token) is not str or not re.fullmatch('[0-9a-fA-F]{32}', token)):
                raise NativeSessionError('external-peer-key-unavailable')
            self.require_live(snapshot)
            return token

    def close(self):
        with self._lock:
            for handle in self._handles.values():
                close(handle)
            self._handles.clear()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()
