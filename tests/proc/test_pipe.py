"""Tests for kitlib.proc.pipe: private pipes, their client and server identity checks and file ACLs.

Plain subprocesses and raw JSON lines stand in for a bridge harness. Each helper process is the test's own."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from kitlib.proc.errors import ProcError
from tests.platforms import windows_only

if os.name == 'nt':
    from ctypes import wintypes as w
    from kitlib.proc.pipe import PrivateListener, connect, peek, process_image, protect_file, read, write
    from kitlib.proc.process import own_ctime, proc_ctime

ROOT = Path(__file__).resolve().parents[2]


def pipe_name():
    return '\\\\.\\pipe\\LOCAL\\kit-pipe-test-' + uuid.uuid4().hex


def send(pipe, frames):
    data = b''.join(json.dumps(frame, separators=(',', ':')).encode() + b'\n' for frame in frames)
    count = w.DWORD()
    if not write(pipe.handle, data, len(data), ctypes.byref(count), None) or count.value != len(data):
        raise AssertionError('pipe write failed')


def frames_until_close(listener, timeout):
    """Every JSON line the client wrote, and True once it has closed (broken pipe)."""
    end, data = time.monotonic() + timeout, b''
    while time.monotonic() < end:
        available = w.DWORD()
        if not peek(listener.handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in (109, 232):
                return [json.loads(line) for line in data.splitlines() if line], True
            raise AssertionError('peek failed')
        if available.value:
            buffer = ctypes.create_string_buffer(min(4096, available.value))
            count = w.DWORD()
            if not read(listener.handle, buffer, len(buffer), ctypes.byref(count), None):
                raise AssertionError('read failed')
            data += buffer.raw[:count.value]
        else:
            time.sleep(.02)
    raise AssertionError('client did not close')


class Child:
    """A helper process given `code` with ROOT on its path; it prints one JSON line when ready."""

    def __init__(self, code, *args):
        self.process = subprocess.Popen([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, %r)\n' % str(ROOT)
                                         + code, *map(str, args)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        self.creation = proc_ctime(self.process.pid)

    def line(self):
        return json.loads(self.process.stdout.readline())

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


@windows_only('Windows only')
class PrivatePipes(unittest.TestCase):
    def test_reply_pipe_reuses_same_address_for_second_closed_client(self):
        name = pipe_name()
        listener = PrivateListener(name)
        try:
            for status in ('held', 'expired'):
                client = connect(name, os.getpid(), own_ctime())
                try:
                    send(client, [{'status': status}])
                finally:
                    client.close()
                listener.accept()
                frames, closed = frames_until_close(listener, 2)
                self.assertTrue(closed)
                self.assertEqual(frames, [{'status': status}])
                if status == 'held':
                    listener.reset()
        finally:
            listener.close()

    def test_verified_private_pipe_drains_closed_client(self):
        name = pipe_name()
        listener, client = PrivateListener(name), None
        try:
            client = connect(name, os.getpid(), own_ctime())
            send(client, [{'type': 'auth', 'token': 'fixture'}, {'type': 'control'}])
            client.close()
            listener.accept()
            frames, closed = frames_until_close(listener, 2)
            self.assertTrue(closed)
            self.assertEqual(frames, [{'type': 'auth', 'token': 'fixture'}, {'type': 'control'}])
        finally:
            if client:
                client.close()
            listener.close()

    def test_wrong_process_creation_refused_before_write(self):
        name = pipe_name()
        listener = PrivateListener(name)
        try:
            with self.assertRaisesRegex(ProcError, 'pipe-process-identity-mismatch'):
                connect(name, os.getpid(), own_ctime() - 1)
        finally:
            listener.close()

    def test_listener_readiness_bounded_and_process_image_exact(self):
        listener = PrivateListener(pipe_name())
        try:
            with self.assertRaisesRegex(ProcError, 'private-pipe-accept-timeout'):
                listener.accept(timeout=.05)
            self.assertEqual(process_image(os.getpid()), Path(sys.executable).resolve())
        finally:
            listener.close()

    def test_new_key_file_acl_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.key'
            path.write_bytes(b'fixture')
            protect_file(path)
            self.assertEqual(path.read_bytes(), b'fixture')


CONNECTED = ('import json,sys,ctypes; from ctypes import wintypes as w; '
             'from kitlib.proc.pipe import connect, write; '
             'p=connect(sys.argv[1],int(sys.argv[2]),int(sys.argv[3])); '
             "data=json.dumps({'claimed_role':'owner'}).encode()+b'\\n'; n=w.DWORD(); "
             'write(p.handle,data,len(data),ctypes.byref(n),None); '
             "print(json.dumps({'ready':True}),flush=True); sys.stdin.readline(); p.close()")

ANONYMOUS = '''import ctypes,json,sys
from ctypes import wintypes as w
from kitlib.proc.pipe import K,A,bind,create_file,close,INVALID
thread=bind(K,'GetCurrentThread',[],w.HANDLE)
impersonate=bind(A,'ImpersonateAnonymousToken',[w.HANDLE],w.BOOL)
revert=bind(A,'RevertToSelf',[],w.BOOL)
output=[]
for access in (0x80000000,0xC0000000):
    if not impersonate(thread()):
        raise RuntimeError('anonymous fixture unavailable')
    try:
        handle=create_file(sys.argv[1],access,0,None,3,0x00100000,None)
        error=ctypes.get_last_error() if handle==INVALID else 0
        if handle!=INVALID:
            close(handle)
        output.append(error)
    finally:
        if not revert():
            bind(K,'ExitProcess',[w.UINT],None)(19)
print(json.dumps({'anonymous_open_errors':output}),flush=True)
'''

RESTRICTED = '''import ctypes,json,sys
from ctypes import wintypes as w
from kitlib.proc.pipe import K,A,bind,close,local_free,create_file,INVALID
class SidAndAttributes(ctypes.Structure):
    _fields_=[('sid',w.LPVOID),('attributes',w.DWORD)]
current=bind(K,'GetCurrentProcess',[],w.HANDLE)
open_token=bind(A,'OpenProcessToken',[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)],w.BOOL)
convert=bind(A,'ConvertStringSidToSidW',[w.LPCWSTR,ctypes.POINTER(w.LPVOID)],w.BOOL)
restrict=bind(A,'CreateRestrictedToken',[w.HANDLE,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.LPVOID,
                                       w.DWORD,w.LPVOID,ctypes.POINTER(w.HANDLE)],w.BOOL)
impersonate=bind(A,'ImpersonateLoggedOnUser',[w.HANDLE],w.BOOL)
revert=bind(A,'RevertToSelf',[],w.BOOL)
is_restricted=bind(A,'IsTokenRestricted',[w.HANDLE],w.BOOL)
original,restricted,sid=w.HANDLE(),w.HANDLE(),w.LPVOID()
handle=create_file(sys.argv[1],0x80000000,7,None,3,0,None)
if handle==INVALID:
    raise RuntimeError('normal fixture access unavailable')
close(handle)
try:
    if not open_token(current(),0x000B,ctypes.byref(original)):
        raise RuntimeError('fixture token unavailable')
    if not convert('S-1-5-7',ctypes.byref(sid)):
        raise RuntimeError('fixture restricting SID unavailable')
    restricted_sids=(SidAndAttributes*1)(SidAndAttributes(sid,0))
    if not restrict(original,1,0,None,0,None,1,restricted_sids,ctypes.byref(restricted)):
        raise RuntimeError('fixture restricted token unavailable')
    checked=bool(is_restricted(restricted))
    if not checked or not impersonate(restricted):
        raise RuntimeError('fixture restriction unavailable')
    try:
        handle=create_file(sys.argv[1],0x80000000,7,None,3,0,None)
        error=ctypes.get_last_error() if handle==INVALID else 0
        if handle!=INVALID:
            close(handle)
    finally:
        if not revert():
            bind(K,'ExitProcess',[w.UINT],None)(19)
    print(json.dumps({'normal_open_verified':True,'restricted_token_verified':checked,
                     'restricted_open_error':error}),flush=True)
finally:
    if original: close(original)
    if restricted: close(restricted)
    if sid: local_free(sid)
'''


@windows_only('Windows only')
class ClientIdentity(unittest.TestCase):
    def setUp(self):
        self.name = pipe_name()
        self.listener = PrivateListener(self.name)
        self.addCleanup(self.listener.close)

    def child(self, code, *args):
        child = Child(code, *args)
        self.addCleanup(child.close)
        return child

    def test_connected_child_identity_is_checked_before_reading_untrusted_labels(self):
        child = self.child(CONNECTED, self.name, os.getpid(), own_ctime())
        self.assertEqual(child.line(), {'ready': True})
        self.listener.accept()
        for pid, creation in ((child.process.pid, child.creation - 1), (os.getpid(), own_ctime())):
            with self.assertRaisesRegex(ProcError, 'pipe-client-process-mismatch'):
                self.listener.verify_client(pid, creation)
        verified = self.listener.verify_client(child.process.pid, child.creation)
        self.assertEqual(verified, {'client_process_verified': True, 'client_user_verified': True})
        self.assertNotIn('role_verified', verified)
        child.close()
        frames, closed = frames_until_close(self.listener, 3)
        self.assertTrue(closed)
        self.assertEqual(frames, [{'claimed_role': 'owner'}])

    def test_missing_connected_client_and_boolean_identity_fail_closed(self):
        with self.assertRaisesRegex(ProcError, 'pipe-client-process-mismatch'):
            self.listener.verify_client(os.getpid(), own_ctime())
        with self.assertRaisesRegex(ProcError, 'invalid-client-identity'):
            self.listener.verify_client(True, own_ctime())

    def test_anonymous_token_cannot_open_current_user_private_reply_pipe(self):
        # Impersonation happens only inside the helper, never the test runner.
        self.assertEqual(self.child(ANONYMOUS, self.name).line(), {'anonymous_open_errors': [5, 5]})

    def test_restricting_sid_denies_a_protected_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.key'
            path.write_bytes(b'public-fixture-content-not-a-credential')
            protect_file(path)
            child = self.child(RESTRICTED, path)
            self.assertEqual(child.line(), {'normal_open_verified': True,
                                            'restricted_token_verified': True, 'restricted_open_error': 5})
            child.close()


class Errors(unittest.TestCase):
    def test_the_error_carries_its_code_and_no_caller_data(self):
        error = ProcError('pipe-owner-mismatch')
        self.assertEqual(error.code, 'pipe-owner-mismatch')
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'process call refused: pipe-owner-mismatch')


if __name__ == '__main__':
    unittest.main()
