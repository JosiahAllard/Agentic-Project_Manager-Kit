"""Claude's per-process registry records: one bounded JSON object in a regular file.

The one helper the Claude half kept from the Phase 0 probe (claude/bridge_phase0.py, deleted;
optional/bridge/README.md "What was deleted"). Every Claude source reads session and peer-key
records through it.
"""
import os
from pathlib import Path
import stat

from kitlib.proc.errors import ProcError
from optional.bridge.core.jsonwire import JsonLineDecoder, WireError

MAX_RECORD_BYTES = 8192


def read_record(path):
    """The one JSON object in `path`. Refuses a link, a reparse point, a file replaced while it
    was opened, more than MAX_RECORD_BYTES, and anything but exactly one object."""
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ProcError('nonregular-registry-record')
    with Path(path).open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ProcError('registry-record-replaced')
        data = stream.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise ProcError('registry-record-limit')
    try:
        decoder = JsonLineDecoder()
        frames = decoder.feed(data.rstrip(b'\r\n') + b'\n')
        decoder.finish()
    except WireError:
        raise ProcError('invalid-registry-record') from None
    if len(frames) != 1:
        raise ProcError('invalid-registry-record')
    return frames[0]
