"""The stdio JSON-RPC 2.0 wire both MCP servers share (kitlib.lockmcp.server, kitlib.hubmcp.server):
the response shapes and the line loop. What each server answers is its own `handle`.

serve(server, rfile, wfile) reads one JSON message per line from rfile (binary), passes each to
server.handle(message) and writes each response as one line to wfile (binary) until rfile ends.
"""
import json


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(server, rfile, wfile):
    """Reads one JSON message per line from rfile (binary) and writes each response as one line to
    wfile (binary) until rfile ends. A line that is not JSON gets a parse error (-32700)."""
    for raw in iter(rfile.readline, b""):
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            resp = err(None, -32700, "parse error")
        else:
            try:
                resp = server.handle(msg)
            except Exception as e:        # never down: the client gets an internal error
                mid = msg.get("id") if isinstance(msg, dict) else None
                resp = err(mid, -32603, "internal error: %s: %s" % (type(e).__name__, e))
        if resp is not None:
            wfile.write(json.dumps(resp, ensure_ascii=True, default=str).encode("utf-8") + b"\n")
            wfile.flush()
