"""Bridge-only native hook producer. Private publish port, no permission decision.

The controller's hook command calls produce(stdin, publish) with its authenticated
IPC publisher. The receiver independently verifies kernel client identity, exact
producer command and native parent. Neither pid nor sender is added to the JSON.
This module does not install a hook or change the existing Claude workflow.
"""
import json
from pathlib import Path
import sys

# Exec form uses isolated Python, so only this immutable installed kit is added.
ROOT=Path(__file__).resolve().parents[4]  # optional/bridge/hosts/claude/hook.py
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
import kitlib  # noqa: E402,F401  (installs the CreateProcess hook before anything starts)

from optional.bridge.hosts.claude.terminal import MAX_HOOK_BYTES, TerminalError
from optional.bridge.win.evidence import document
from optional.bridge.win.launch import PYTHON_FLAGS


def produce(stdin,publish):
    """Read native hook stdin once and publish only a bounded canonical envelope.

    Native command hooks provide one JSON document terminated by EOF, including
    pretty-printed documents. The private transport supplies producer attribution.
    """
    if not callable(publish): raise TerminalError('private-hook-publisher-required')
    raw=stdin.read(MAX_HOOK_BYTES+1)
    if type(raw) is not bytes or not 0<len(raw)<=MAX_HOOK_BYTES:
        raise TerminalError('invalid-hook-envelope')
    try:
        event=document(raw)
        payload=json.dumps(event,sort_keys=True,ensure_ascii=False,separators=(',',':'),
            allow_nan=False).encode('utf-8')
        if len(payload)>MAX_HOOK_BYTES: raise TerminalError('invalid-hook-envelope')
    except Exception: raise TerminalError('invalid-hook-envelope') from None
    publish(payload)
    return 0


def entry_command(marker,executable=None):
    """Controller hook argv descriptor; the marker is the only positional arg."""
    path=Path(marker)
    interpreter=Path(sys.executable if executable is None else executable)
    if not path.is_absolute() or not interpreter.is_absolute():
        raise TerminalError('absolute-hook-marker-and-interpreter-required')
    return [str(interpreter.resolve(strict=True)),*PYTHON_FLAGS,str(Path(__file__).resolve()),str(path)]


def main(argv=None,stdin=None):
    """Observe only. Missing service evidence denies the later bridge call.

    Returning zero and emitting no permission decision preserves Claude's native
    permission pipeline. The shared service receives producer identity from the
    kernel, not from this envelope; refusal never manufactures a context ticket.
    """
    try:
        args=sys.argv[1:] if argv is None else argv
        if len(args)!=1 or not Path(args[0]).is_absolute(): return 0
        from optional.bridge.win.frontend import service_target
        from optional.bridge.win.rpc import request
        target=service_target(args[0])
        def publish(payload):
            return request(target,{'protocol':1,'hook':document(payload)},timeout=5)
        produce(sys.stdin.buffer if stdin is None else stdin,publish)
    except Exception:
        # No stdout/stderr identity leaks or native permission allow/bypass.
        return 0
    return 0


if __name__=='__main__':
    raise SystemExit(main())
