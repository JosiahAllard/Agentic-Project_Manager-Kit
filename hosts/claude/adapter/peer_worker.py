"""The hidden helper that writes one hub push to a session's peer pipe (hosts.claude.adapter.peer).

Run only by peer.write_once, as `python -I -B peer_worker.py` inside a kill-on-close job, so a
WriteFile that blocks never blocks the hub. The request comes on stdin, never on the command line:
one JSON line {pipe, pid, creation, length} (at most 2048 bytes), then exactly `length` bytes of
packet. The result is one JSON line on stdout, content-free:

  {"state": "not-written"}                       nothing was sent: a bad request, or connect()
                                                 refused (no such pipe, or its server is not that
                                                 pid with that creation time and our user)
  {"state": "written", "bytes_written": n}       WriteFile took all n bytes (a transfer, never a
                                                 proof the session read it)
  {"state": "uncertain"}                         a write was tried and did not plainly finish

It mirrors optional/bridge/win/worker.py pipe_write with its own JSON-line parsing, so the hub
never imports optional/.
"""
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import re
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import kitlib  # noqa: E402,F401  (installs the CreateProcess hook before anything starts)

MAX_PACKET_BYTES = 256 * 1024
MAX_HEADER_BYTES = 2048
LOCAL_PIPE = re.compile(r'\\\\\.\\pipe\\(?:LOCAL\\)?[A-Za-z0-9_-]{1,160}\Z', re.I)
FIELDS = {'pipe', 'pid', 'creation', 'length'}


def valid_target(pipe, pid, creation):
    """A local pipe name, a pid and a creation time of the right types and ranges."""
    return (type(pipe) is str and LOCAL_PIPE.fullmatch(pipe) is not None
            and type(pid) is int and 0 < pid <= 0xffffffff
            and type(creation) is int and 0 < creation < 2 ** 64)


def encode_line(value):
    """One compact JSON line, UTF-8."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'


def read_request(stream):
    """(pipe, pid, creation, packet) from stream, or ValueError."""
    header = stream.readline(MAX_HEADER_BYTES + 1)
    if not header.endswith(b'\n') or len(header) > MAX_HEADER_BYTES + 1:
        raise ValueError('invalid private request')
    meta = json.loads(header.decode('utf-8'))
    if not isinstance(meta, dict) or set(meta) != FIELDS:
        raise ValueError('invalid private request')
    if not valid_target(meta['pipe'], meta['pid'], meta['creation']):
        raise ValueError('invalid pipe target')
    size = meta['length']
    if type(size) is not int or not 0 < size <= MAX_PACKET_BYTES:
        raise ValueError('invalid packet size')
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ValueError('truncated input')
        data.extend(chunk)
    return meta['pipe'], meta['pid'], meta['creation'], bytes(data)


def pipe_write(stream):
    """Read one request from stream and write its packet once; the result dict (the module
    docstring). May block in WriteFile: it runs only inside write_once's job."""
    from kitlib.proc.pipe import connect, write
    pipe = None
    try:
        try:
            name, pid, creation, payload = read_request(stream)
            # connect checks the kernel pipe-server pid, creation time and SID, not the name alone
            pipe = connect(name, pid, creation)
        except Exception:
            return {'state': 'not-written'}
        count = w.DWORD()
        if not write(pipe.handle, payload, len(payload), ctypes.byref(count), None) or count.value != len(payload):
            return {'state': 'uncertain'}
        return {'state': 'written', 'bytes_written': count.value}
    except Exception:
        return {'state': 'uncertain'}
    finally:
        if pipe is not None:
            pipe.close()


def main():
    result = pipe_write(sys.stdin.buffer)
    sys.stdout.buffer.write(encode_line(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
