"""Bounded, process-verified native pipe writes in an owned hidden helper.

A successful WriteFile only proves a byte transfer. It is never a native message
acceptance. Errors after an attempted write are uncertain and must not be retried
blindly. Private payloads travel through anonymous stdin, never command arguments.
"""
from dataclasses import dataclass, field
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from optional.bridge.core.jsonwire import JsonLineDecoder, encode_frame
from optional.bridge.core.messages import BeforeWriteError, DeliveryError
from kitlib.proc.job import Job
from .launch import PYTHON_FLAGS
from optional.bridge.paths import PIPE_WORKER_REL,WORKERS

MAX_PACKET_BYTES = 256 * 1024
MAX_RESULT_BYTES = 2048
LOCAL_PIPE = re.compile(r'\\\\\.\\pipe\\(?:LOCAL\\)?[A-Za-z0-9_-]{1,160}\Z', re.I)


@dataclass(frozen=True)
class PipeTarget:
    name: str = field(repr=False)
    pid: int
    creation: int

    def __post_init__(self):
        if (type(self.name) is not str or LOCAL_PIPE.fullmatch(self.name) is None
                or type(self.pid) is not int or not 0 < self.pid <= 0xffffffff
                or type(self.creation) is not int or not 0 < self.creation < 2**64):
            raise BeforeWriteError('invalid-native-pipe-target')


@dataclass(frozen=True)
class PipeWriteReceipt:
    bytes_written: int


def _read_result(stream, completed, limit=MAX_RESULT_BYTES):
    data = bytearray()
    try:
        while len(data) <= limit:
            chunk = stream.read(min(4096, limit+1-len(data)))
            if not chunk:
                completed.put(('read', bytes(data)))
                return
            data.extend(chunk)
        completed.put(('fault', None))
    except Exception:
        completed.put(('fault', None))


def _write_input(stream, data, completed):
    try:
        view = memoryview(data)
        while view:
            count = stream.write(view)
            if not count:
                raise OSError('closed input')
            view = view[count:]
        stream.close()
        completed.put(('write', None))
    except Exception:
        completed.put(('fault', None))


def _cleanup(job, process, threads):
    # Never close a buffered stream while its I/O thread is blocked. End the
    # owned process tree first; closing its pipe ends unblocks both readers.
    job.close()  # kill-on-close is armed, including on error paths.
    if process is not None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            raise DeliveryError('native-worker-cleanup-unconfirmed') from None
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            raise DeliveryError('native-worker-cleanup-unconfirmed')
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _kit():
    """The kit this module runs from (optional/bridge/win/transport.py, three folders down)."""
    return Path(__file__).resolve().parents[3]


def run_worker(name, data, *, limit, timeout, prefix=None):
    """Private owned helper pump; code chooses helper and limits, not RPC arguments.

    prefix, when given, is called once the helper has started and before any input is
    written; its bytes go first. A BeforeWriteError from it ends the run with nothing sent.
    """
    if (name not in ('bridge_pipe_worker.py','bridge_rpc_worker.py') or type(data) is not bytes
            or len(data)>1024*1024+2048 or type(limit) is not int or not 0<limit<=1024*1024+2048
            or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=10):
        raise BeforeWriteError('invalid-native-worker-request')
    worker=_kit().joinpath(*WORKERS[name].split('/'))
    if not worker.is_file(): raise BeforeWriteError('native-worker-unavailable')
    job,process,threads=None,None,[]
    try:
        try:
            job=Job(kill_on_close=True)
            process=job.start([sys.executable,*PYTHON_FLAGS,str(worker)],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,
                bufsize=0,creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            raise BeforeWriteError('native-worker-start-failed') from None
        if prefix is not None:
            data=prefix()+data
            if type(data) is not bytes or len(data)>1024*1024+2048:
                raise BeforeWriteError('invalid-native-worker-request')
        completed=queue.Queue(maxsize=2)
        threads=[threading.Thread(target=_write_input,args=(process.stdin,data,completed),daemon=True),
            threading.Thread(target=_read_result,args=(process.stdout,completed,limit),daemon=True)]
        for thread in threads: thread.start()
        deadline,results=time.monotonic()+timeout,{}
        while len(results)<2:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise DeliveryError('native-write-uncertain')
            try: kind,result=completed.get(timeout=remaining)
            except queue.Empty: raise DeliveryError('native-write-uncertain') from None
            if kind=='fault' or kind in results: raise DeliveryError('native-write-uncertain')
            results[kind]=result
        try: process.wait(timeout=max(.001,deadline-time.monotonic()))
        except subprocess.TimeoutExpired: raise DeliveryError('native-write-uncertain') from None
        if process.returncode: raise DeliveryError('native-write-uncertain')
        return results['read']
    finally:
        if job is not None: _cleanup(job,process,threads)


def write_peer(target, packet, *, timeout=5):
    """Write once to the exact live local server; no retry, no acceptance claim.

    A helper timeout, partial write, lost response or malformed response is an
    ordinary DeliveryError (uncertain). Only a validated before-write result is
    BeforeWriteError. The caller's durable dispatch journal decides its state.
    """
    if (type(target) is not PipeTarget or type(packet) is not bytes
            or not 0 < len(packet) <= MAX_PACKET_BYTES
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 10):
        raise BeforeWriteError('invalid-native-write-request')
    target.__post_init__()
    metadata = encode_frame({'pipe': target.name, 'pid': target.pid,
        'creation': target.creation, 'length': len(packet)}, max_frame_bytes=2048)
    worker = _kit().joinpath(*PIPE_WORKER_REL.split('/'))
    if not worker.is_file():
        raise BeforeWriteError('native-worker-unavailable')
    job, process, threads = None, None, []
    try:
        try:
            job = Job(kill_on_close=True)
            process = job.start([sys.executable, *PYTHON_FLAGS, str(worker)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            raise BeforeWriteError('native-worker-start-failed') from None
        completed = queue.Queue(maxsize=2)
        threads = [threading.Thread(target=_write_input,
            args=(process.stdin, metadata+packet, completed), name='bridge-pipe-input', daemon=True),
            threading.Thread(target=_read_result, args=(process.stdout, completed),
                name='bridge-pipe-result', daemon=True)]
        for thread in threads:
            thread.start()
        deadline, results = time.monotonic()+timeout, {}
        while len(results) < 2:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise DeliveryError('native-write-uncertain')
            try:
                kind, data = completed.get(timeout=remaining)
            except queue.Empty:
                raise DeliveryError('native-write-uncertain') from None
            if kind == 'fault' or kind in results:
                raise DeliveryError('native-write-uncertain')
            results[kind] = data
        try:
            process.wait(timeout=max(.001, deadline-time.monotonic()))
            decoder = JsonLineDecoder(max_frame_bytes=MAX_RESULT_BYTES)
            frames = decoder.feed(results['read'])
            decoder.finish()
            if process.returncode or len(frames) != 1:
                raise ValueError('invalid worker response')
            result = frames[0]
        except Exception:
            raise DeliveryError('native-write-uncertain') from None
        if result == {'state': 'not-written'}:
            raise BeforeWriteError('native-peer-unavailable-before-write')
        if (set(result) != {'state', 'bytes_written'} or result['state'] != 'written'
                or type(result['bytes_written']) is not int or result['bytes_written'] != len(packet)):
            raise DeliveryError('native-write-uncertain')
        return PipeWriteReceipt(result['bytes_written'])
    finally:
        if job is not None:
            _cleanup(job, process, threads)
