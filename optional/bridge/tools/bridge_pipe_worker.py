"""Private stdio worker entry point; launched only by the Windows bridge transport."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import kitlib  # noqa: E402,F401  (installs the CreateProcess hook before anything starts)

from optional.bridge.core.jsonwire import encode_frame
from optional.bridge.win.worker import pipe_write


def main():
    result = pipe_write(sys.stdin.buffer)
    sys.stdout.buffer.write(encode_frame(result, max_frame_bytes=2048))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
