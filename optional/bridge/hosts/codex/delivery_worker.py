"""Private two-phase native tool pipe helper; no model or approval decisions."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import struct
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from optional.bridge.hosts.codex.delivery_transport import MAX_FRAME, NativeEndpoint, strict_json
from kitlib.proc.pipe import (K, bind, close, connect, open_process,
    process_image, process_sids, read, write)
from kitlib.proc.process import handle_ctime

wait = bind(K, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
get_pid = bind(K, 'GetProcessId', [w.HANDLE], w.DWORD)


def _exact(source, size):
    data = bytearray()
    while len(data) < size:
        chunk = source.read(size-len(data))
        if not chunk:
            raise ValueError('truncated private input')
        data.extend(chunk)
    return bytes(data)


def _native_exact(handle, size):
    data = bytearray()
    while len(data) < size:
        buffer = ctypes.create_string_buffer(min(4096, size-len(data)))
        count = w.DWORD()
        if not read(handle, buffer, len(buffer), ctypes.byref(count), None) or not count.value:
            raise ValueError('native response read failed')
        data.extend(buffer.raw[:count.value])
    return bytes(data)


def _emit(output, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')+b'\n'
    if len(data) > MAX_FRAME*2+2048:
        raise ValueError('private output size')
    output.write(data)
    output.flush()


def pipe_rpc(source, output):
    pipe, process, committed = None, None, False
    try:
        header = source.readline(2049)
        if not header.endswith(b'\n') or len(header) > 2048:
            raise ValueError('private metadata size')
        metadata = strict_json(header)
        if set(metadata) != {'pipe', 'pid', 'creation', 'executable', 'length'}:
            raise ValueError('private metadata fields')
        endpoint = NativeEndpoint(metadata['pipe'], metadata['pid'], metadata['creation'], metadata['executable'])
        length = metadata['length']
        if type(length) is not int or not 4 < length <= MAX_FRAME+4:
            raise ValueError('private packet size')
        # Retain the process handle throughout the connection; a recycled PID
        # cannot inherit the grant. Owner/image checks supplement, never issue it.
        process = open_process(0x00101000, False, endpoint.pid)
        def check():
            if (not process or get_pid(process) != endpoint.pid or handle_ctime(process) != endpoint.creation
                    or wait(process, 0) != 258 or process_sids(endpoint.pid)[0] != process_sids(os.getpid())[0]
                    or os.path.normcase(str(process_image(endpoint.pid))) != os.path.normcase(endpoint.executable)):
                raise ValueError('native process identity mismatch')
        check()
        pipe = connect(endpoint.name, endpoint.pid, endpoint.creation)
        check()
        _emit(output, {'phase': 'connected'})
        packet = _exact(source, length)
        size = struct.unpack('<I', packet[:4])[0]
        if size != length-4 or not 0 < size <= MAX_FRAME:
            raise ValueError('native request size')
        request = strict_json(packet[4:])
        if type(request.get('id')) is not str or not 0 < len(request['id']) <= 128:
            raise ValueError('native request id')
        check()
        committed = True  # WriteFile errors/partial writes are all uncertain.
        count = w.DWORD()
        if not write(pipe.handle, packet, len(packet), ctypes.byref(count), None) or count.value != len(packet):
            raise ValueError('native partial write')
        size = struct.unpack('<I', _native_exact(pipe.handle, 4))[0]
        if not 0 < size <= MAX_FRAME:
            raise ValueError('native response size')
        response = strict_json(_native_exact(pipe.handle, size))
        if (response.get('jsonrpc') != '2.0' or type(response.get('id')) is not str
                or response['id'] != request['id'] or set(response) != {'jsonrpc', 'id', 'result'}
                or type(response['result']) is not dict):
            raise ValueError('uncorrelated or refused native response')
        check()
        _emit(output, {'phase': 'result', 'result': response['result']})
    except Exception:
        _emit(output, {'phase': 'uncertain' if committed else 'not-written'})
    finally:
        if pipe is not None:
            pipe.close()
        if process:
            close(process)


if __name__ == '__main__':
    pipe_rpc(sys.stdin.buffer, sys.stdout.buffer)
