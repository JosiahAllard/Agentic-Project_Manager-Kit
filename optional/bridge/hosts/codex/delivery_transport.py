"""Deadline-bounded app-owned Windows pipe RPC, with no native payload in argv.

The helper owns blocking Win32 I/O inside a kill-on-close job. Its connected
handshake contains no message; only after the host re-attests its lease is the
private body released. No packet is retried. The helper verifies server kernel
PID, retained creation identity, image, user SID and pipe owner before handshake.
Endpoint identity is necessary, not authorization; a frontend lease supplies it.
"""
from dataclasses import dataclass, field
import json
import math
import ntpath
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from optional.bridge.core.messages import BeforeWriteError, DeliveryError
from kitlib.proc.job import Job
from optional.bridge.win.launch import PYTHON_FLAGS

MAX_FRAME = 128 * 1024


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('duplicate native field')
        result[key] = value
    return result


def strict_json(data):
    def invalid(value):
        raise ValueError('nonfinite native number')
    value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=invalid)
    if type(value) is not dict:
        raise ValueError('native frame is not object')
    return value


def native_frame(request):
    try:
        if type(request) is not dict:
            raise ValueError('invalid native request')
        data = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if not 0 < len(data) <= MAX_FRAME:
            raise ValueError('native size')
        import struct
        return struct.pack('<I', len(data)) + data
    except Exception:
        raise BeforeWriteError('invalid-or-oversized-codex-native-request') from None


@dataclass(frozen=True)
class NativeEndpoint:
    name: str = field(repr=False)
    pid: int
    creation: int
    executable: str = field(repr=False)

    def __post_init__(self):
        if (type(self.name) is not str or not self.name.lower().startswith('\\\\.\\pipe\\')
                or not 10 < len(self.name) <= 256 or self.name[9] in '\\/'
                or any(ord(c) < 32 or ord(c) == 127 for c in self.name)
                or type(self.pid) is not int or not 0 < self.pid <= 0xffffffff
                or type(self.creation) is not int or self.creation <= 0
                or type(self.executable) is not str or not ntpath.isabs(self.executable)
                or any(ord(c) < 32 for c in self.executable)):
            raise BeforeWriteError('invalid-codex-native-endpoint')


def _read_output(stream, events):
    try:
        for _ in range(3):
            data = stream.readline(MAX_FRAME * 2 + 2049)
            if not data:
                events.put(('eof', None))
                return
            if not data.endswith(b'\n') or len(data) > MAX_FRAME * 2 + 2048:
                raise ValueError('private helper output limit')
            events.put(('frame', strict_json(data)))
        raise ValueError('private helper frame limit')
    except Exception:
        events.put(('fault', None))


def _write_input(stream, data, phase, events):
    try:
        offset = 0
        while offset < len(data):
            count = stream.write(data[offset:])
            if not count:
                raise ValueError('helper stdin closed')
            offset += count
        stream.flush()
        events.put((phase, None))
    except Exception:
        events.put(('fault', None))


def _cleanup(job, process, threads):
    clean = True
    # Kill-on-close is the final tree owner. Close it before anything which can
    # wait on child I/O, even if explicit termination refuses or raises.
    try:
        try:
            job.terminate(1)
        finally:
            job.close()
    except Exception:
        clean = False
    ended = process is None
    if process is not None:
        try:
            process.wait(timeout=2)
            ended = True
        except subprocess.TimeoutExpired:
            clean = False
    for thread in threads:
        if thread.ident is not None:  # start() itself may have failed.
            thread.join(timeout=.5)
    stopped = not any(thread.is_alive() for thread in threads)
    # Never close a stream beneath live blocked I/O. If OS cleanup cannot be
    # confirmed, report uncertainty without blocking the controller on close.
    if ended and stopped and process is not None:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    clean = False
    return clean and ended and stopped


def _seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 10


class NativeToolsPipe:
    """One helper per call, in two bounded phases.

    start_timeout bounds everything before the commit: helper spawn and import, the
    'connected' handshake and the host's before_write re-attestation. A miss there is a
    BeforeWriteError, since no request byte has left the helper. timeout bounds the rest and
    counts from the commit: body write, native response and helper exit; a miss there is an
    uncertain DeliveryError. A slow helper start therefore cannot spend the response budget.

    With the production defaults (start_timeout 3 s, timeout 5 s) a call reaches its outcome
    within 8 s; before the split one 5 s budget covered both phases. Cleanup after a kill adds
    at most 2 s waiting for the helper and .5 s for each of the three I/O threads (_cleanup), so
    a call ends within 11.5 s, inside the service's 12 s worker join (win/runtime.py
    run_service). Raising either default must keep that sum under the join.
    """

    def __init__(self, *, timeout=5, start_timeout=3):
        if not _seconds(timeout) or not _seconds(start_timeout):
            raise BeforeWriteError('invalid-codex-native-timeout')
        self.timeout, self.start_timeout = timeout, start_timeout

    def call(self, endpoint, request, *, before_write=None):
        if type(endpoint) is not NativeEndpoint or not callable(before_write):
            raise BeforeWriteError('codex-native-write-context-missing')
        endpoint.__post_init__()
        packet = native_frame(request)
        metadata = json.dumps({'pipe': endpoint.name, 'pid': endpoint.pid, 'creation': endpoint.creation,
            'executable': endpoint.executable, 'length': len(packet)}, separators=(',', ':')).encode('utf-8')+b'\n'
        if len(metadata) > 2048:
            raise BeforeWriteError('codex-native-endpoint-limit')
        worker = Path(__file__).with_name('delivery_worker.py').resolve()
        if not worker.is_file():
            raise BeforeWriteError('codex-native-worker-unavailable')
        deadline, committed = time.monotonic()+self.start_timeout, False
        job, process, threads = None, None, []
        events = queue.Queue(maxsize=8)
        def event():
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise ValueError('deadline')
            return events.get(timeout=remaining)
        try:
            job = Job(kill_on_close=True)
            # This helper never needs provider credentials, native metadata or
            # payloads in its environment. Python/Windows startup keys only.
            env = {key: value for key, value in os.environ.items()
                if key.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'LOCALAPPDATA'}}
            process = job.start([sys.executable, *PYTHON_FLAGS, str(worker)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW, env=env)
            threads = [threading.Thread(target=_read_output, args=(process.stdout, events), daemon=True),
                threading.Thread(target=_write_input, args=(process.stdin, metadata, 'metadata', events), daemon=True)]
            for thread in threads:
                thread.start()
            metadata_written = False
            while True:
                kind, value = event()
                if kind == 'metadata' and not metadata_written:
                    metadata_written = True
                elif kind == 'frame' and value == {'phase': 'connected'}:
                    break
                elif kind == 'frame' and value == {'phase': 'not-written'}:
                    raise BeforeWriteError('codex-native-peer-unavailable-before-write')
                else:
                    raise ValueError('invalid helper handshake')
            if before_write() is not True:
                raise BeforeWriteError('codex-native-context-changed-before-write')
            if deadline-time.monotonic() <= 0:
                raise BeforeWriteError('codex-native-deadline-before-write')
            # The response budget starts here: the start phase never spends it.
            deadline, committed = time.monotonic()+self.timeout, True
            thread = threading.Thread(target=_write_input,
                args=(process.stdin, packet, 'body', events), daemon=True)
            threads.append(thread)
            thread.start()
            final, body_written, eof = None, False, False
            while final is None or not body_written or not metadata_written or not eof:
                kind, value = event()
                if kind == 'metadata' and not metadata_written:
                    metadata_written = True
                elif kind == 'body' and not body_written:
                    body_written = True
                elif kind == 'eof' and not eof:
                    eof = True
                elif (kind == 'frame' and final is None and set(value) == {'phase', 'result'}
                        and value['phase'] == 'result' and type(value['result']) is dict):
                    final = value['result']
                else:
                    raise ValueError('invalid helper result')
            process.wait(timeout=max(.001, deadline-time.monotonic()))
            if process.returncode:
                raise ValueError('helper failed')
            return final
        except BeforeWriteError:
            if committed:
                raise DeliveryError('codex-native-delivery-uncertain') from None
            raise
        except Exception:
            if committed:
                raise DeliveryError('codex-native-delivery-uncertain') from None
            raise BeforeWriteError('codex-native-unavailable-before-write') from None
        finally:
            if job is not None:
                if not _cleanup(job, process, threads):
                    raise DeliveryError('codex-native-cleanup-unconfirmed') from None
