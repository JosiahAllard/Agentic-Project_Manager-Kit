"""Inspect native tools with a local stub; optional canary only checks method existence.

From the Codex branch (pmkit_codex/runtime_probe.py): a native acceptance check run by hand against an installed codex.exe (hosts/codex/shell/scripts/runtime_probe.py); kept because it checks what the runtime broker relies on, on the real binary.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from .kit import child_environment
from .sessions import native_codex
from .kit import discover, load_kit


COMMAND_TOOLS = frozenset({"exec_command", "write_stdin", "shell", "shell_command", "local_shell"})
DECLARATION = re.compile(r"(?m)^[ \t]*declare const tools:\s*\{\s*([A-Za-z_]\w*)\s*\(")
CANARY_MARK = "PM_KIT_TOOL_CANARY "
CANARY_SCRIPT = ('text("' + CANARY_MARK + '" + JSON.stringify({exec_command: typeof tools.exec_command, '
                 'write_stdin: typeof tools.write_stdin}));')


class CaptureServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)           # bound header reads before do_POST starts
        return connection, address

    def handle_error(self, request, client_address):
        self.capture_error = "capture transport failed"   # no traceback/request data on stderr


def tool_names(specs):
    """Keep only names, including nested namespaces; discard prompts and schema content."""
    if not isinstance(specs, list):
        raise ValueError("provider request has no tool declaration list")
    result = []
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
            raise ValueError("invalid tool declaration")
        if spec["type"] == "namespace":
            result.extend(tool_names(spec.get("tools")))
        else:
            name = spec.get("name", spec["type"])
            if not isinstance(name, str) or not name:
                raise ValueError("invalid tool name")
            result.append(name)
    return tuple(sorted(set(result)))


def request_tool_names(request):
    """Support standard Responses tools and native additional_tools input items."""
    if not isinstance(request, dict):
        raise ValueError("invalid provider request")
    groups = [request["tools"]] if "tools" in request else []
    for item in request.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            groups.append(item.get("tools"))
    if not groups:
        raise ValueError("explicit tool declarations are absent")
    return tuple(sorted({name for specs in groups for name in tool_names(specs)}))


def nested_tool_names(request):
    """Read native code-mode TypeScript declarations, not examples mentioning a tool."""
    result = set()
    def declarations(specs):
        if not isinstance(specs, list):
            return
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            if spec.get("type") == "namespace":
                declarations(spec.get("tools"))
            elif spec.get("name") == "exec" and isinstance(spec.get("description"), str):
                result.update(DECLARATION.findall(spec["description"]))
    declarations(request.get("tools"))
    for item in request.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            declarations(item.get("tools"))
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "developer":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                result.update(DECLARATION.findall(content["text"]))
    return tuple(sorted(result))


def canary_result(request):
    for item in request.get("input", []):
        if not isinstance(item, dict) or item.get("type") != "custom_tool_call_output" or item.get("call_id") != "pmkit_canary_call":
            continue
        output = item.get("output", [])
        texts = [output] if isinstance(output, str) else [part.get("text", "") for part in output if isinstance(part, dict)]
        for text in texts:
            if isinstance(text, str) and text.startswith(CANARY_MARK):
                result = json.loads(text[len(CANARY_MARK):])
                if result != {"exec_command": "undefined", "write_stdin": "undefined"}:
                    raise ValueError("native command methods remain callable")
                return result
    raise ValueError("code-mode canary result is missing")


def _handler(canary=False):
    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                self.connection.settimeout(2)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("unexpected request size")
                if self.headers.get("Content-Encoding"):
                    raise ValueError("request compression was not disabled")
                if self.path != "/v1/responses" or self.headers.get("Authorization"):
                    raise ValueError("unexpected endpoint or credentials")
                request = json.loads(self.rfile.read(length))
                self.server.requests += 1
                if canary and self.server.requests == 2:
                    self.server.canary = canary_result(request)
                    self.sse([])
                    return
                if self.server.requests != 1:
                    raise ValueError("unexpected extra provider request")
                self.server.names = request_tool_names(request)
                self.server.nested = nested_tool_names(request)
                if "exec" in self.server.names and not self.server.nested:
                    raise ValueError("code-mode tool declarations are unrecognized")
                self.server.captured = True
                if canary:
                    if "exec" not in self.server.names or COMMAND_TOOLS.intersection(set(self.server.names) | set(self.server.nested)):
                        raise ValueError("canary requires code mode with native commands disabled")
                    item = {"type": "custom_tool_call", "id": "pmkit_canary_item", "call_id": "pmkit_canary_call",
                            "namespace": "functions", "name": "exec", "input": CANARY_SCRIPT}
                    self.sse([item])
                    return
            except Exception as error:
                self.server.capture_error = type(error).__name__ + ": " + str(error)[:200]
            # Stop here. Never return an assistant message, token stream or tool call.
            response = b'{"error":{"message":"offline inventory probe stopped","type":"probe_stop"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def sse(self, output):
            events = [{"type": "response.output_item.done", "output_index": index, "item": item}
                      for index, item in enumerate(output)]
            events.append({"type": "response.completed", "response": {"id": "pmkit_canary_response",
                           "status": "completed", "output": output,
                           "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Capture


def _capture(exe, model, home, shell_enabled, runner=None, canary=False):
    if canary and shell_enabled:
        raise ValueError("canary must not enable native command tools")
    load_kit(discover())
    from kitlib.proc import spawn
    server = CaptureServer(("127.0.0.1", 0), _handler(canary))
    server.names, server.nested, server.captured, server.capture_error = (), (), False, None
    server.requests, server.canary = 0, None
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.1), daemon=True)
    env = child_environment(os.environ)
    for key in tuple(env):
        if key.upper().startswith(("OPENAI_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")):
            del env[key]
    env.update(CODEX_HOME=str(home), NO_PROXY="127.0.0.1,localhost", PM_KIT_ROLE="worker", PM_KIT_HOST="codex")
    overrides = {"model_provider": "pmkit_offline_probe",
                 "model_providers.pmkit_offline_probe.name": "Credential-free offline inventory",
                 "model_providers.pmkit_offline_probe.base_url": "http://127.0.0.1:%d/v1" % server.server_port,
                 "model_providers.pmkit_offline_probe.wire_api": "responses",
                 "model_providers.pmkit_offline_probe.requires_openai_auth": False,
                 "model_providers.pmkit_offline_probe.supports_websockets": False,
                 "model_providers.pmkit_offline_probe.request_max_retries": 0,
                 "model_providers.pmkit_offline_probe.stream_max_retries": 0,
                 "features.unified_exec": False, "features.shell_tool": shell_enabled,
                 "features.hooks": False, "features.apps": False, "features.plugins": False,
                 "features.enable_request_compression": False, "web_search": "disabled"}
    argv = [str(exe), "--no-daemon", "exec", "--strict-config", "--ephemeral", "--ignore-user-config",
            "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(home), "--model", model]
    for key, value in overrides.items():
        argv += ["--config", key + "=" + json.dumps(value)]
    argv += ["Local method-existence canary only. No commands or filesystem operations." if canary else
             "Do not run any tool. This offline request is rejected before model inference."]
    thread.start()
    try:
        try:
            # kitlib.proc.spawn adds the no-window flag (plan section 6: no Windows calls here)
            result = (runner or spawn.run)(argv, cwd=home, env=env, shell=False,
                                           capture_output=True, text=True,
                                           encoding="utf-8", timeout=25, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError("private native inventory probe timed out") from None
        if not server.captured or server.capture_error or (result.returncode != 0 if canary else result.returncode == 0):
            raise RuntimeError("native inventory was not captured and rejected as required "
                               "(exit=%s, capture_error=%s)" % (result.returncode, server.capture_error))
        if canary and server.canary is None:
            raise RuntimeError("native code-mode method availability is unverified")
        exposed = [name for name in set(server.names) | set(server.nested) if name.rsplit(".", 1)[-1] in COMMAND_TOOLS]
        return {"requested_features": {"unified_exec": False, "shell_tool": shell_enabled},
                "tool_names": list(server.names), "command_tools": sorted(exposed),
                "nested_tool_names": list(server.nested), **({"method_availability": server.canary} if canary else {})}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def cli_version(exe):
    from kitlib.proc import spawn
    try:
        result = spawn.run([str(exe), "--version"], shell=False,
                           capture_output=True, text=True, encoding="utf-8", timeout=5)
    except subprocess.TimeoutExpired:
        raise RuntimeError("native version probe timed out") from None
    value = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"codex-cli [\w.+-]+", value):
        raise RuntimeError("native Codex version is unverified")
    return value


def probe(codex_exe=None, model="pmkit-offline-probe", runner=None, canary=False):
    """Compare unified-only false to the effective shell-family gate in disposable homes."""
    exe = native_codex(codex_exe)
    version = cli_version(exe)
    if not isinstance(model, str) or not model.strip() or any(c in model for c in "\r\n\x00"):
        raise ValueError("invalid model name for metadata selection")
    # Only these private homes are written; never change active config, auth or trust.
    with tempfile.TemporaryDirectory(prefix="pmkit-native-inventory-") as scratch:
        root = Path(scratch).resolve()
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("pmkit-native-inventory-"):
            raise RuntimeError("diagnostic home resolved outside its private temporary folder")
        original, fixed = root / "unified-only", root / "shell-disabled"
        original.mkdir()
        fixed.mkdir()
        control = _capture(exe, model, original, True, runner)
        protected = _capture(exe, model, fixed, False, runner)
        if canary:
            canary_home = root / "canary"
            canary_home.mkdir()
            runtime = _capture(exe, model, canary_home, False, runner, canary=True)
    if protected["command_tools"]:
        raise RuntimeError("native command tools remain exposed with shell_tool=false")
    return {"cli_version": version, "model_metadata": model, "inference": False, "tool_execution": canary,
            "shell_commands": 0, "filesystem_tool_operations": 0, "unified_only": control, "shell_disabled": protected,
            **({"code_mode_canary": runtime} if canary else {})}
