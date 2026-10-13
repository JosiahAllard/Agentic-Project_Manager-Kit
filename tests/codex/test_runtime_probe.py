import json
import os
from pathlib import Path
import socket
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

from hosts.codex.adapter.runtime_probe import probe, tool_names, request_tool_names, nested_tool_names, canary_result, CANARY_SCRIPT


class RuntimeProbe(unittest.TestCase):
    def setUp(self):
        native = mock.patch("hosts.codex.adapter.runtime_probe.native_codex", return_value=str(Path("codex.exe").resolve()))
        native.start()
        self.addCleanup(native.stop)
        version = mock.patch("hosts.codex.adapter.runtime_probe.cli_version", return_value="codex-cli test")
        version.start()
        self.addCleanup(version.stop)
        self.calls = []

    def send(self, argv, specs, kwargs, exit_code=1):
        overrides = {}
        for index, arg in enumerate(argv[:-1]):
            if arg == "--config":
                key, value = argv[index + 1].split("=", 1)
                overrides[key] = json.loads(value)
        self.calls.append((argv, overrides, kwargs))
        endpoint = overrides["model_providers.pmkit_offline_probe.base_url"] + "/responses"
        request = urllib.request.Request(endpoint, data=json.dumps({"tools": specs, "input": "private prompt"}).encode(),
                                         headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as stopped:
            opener.open(request, timeout=5)
        self.assertEqual(stopped.exception.code, 400)
        body = stopped.exception.read()
        self.assertNotIn(b"tool_call", body)
        self.assertNotIn(b"private prompt", body)
        stopped.exception.close()
        return SimpleNamespace(returncode=exit_code)

    def runner(self, argv, **kwargs):
        disabled = "features.shell_tool=false" in argv
        names = ["view_image"] if disabled else ["exec_command", "write_stdin", "view_image"]
        return self.send(argv, [{"type": "function", "name": name} for name in names], kwargs)

    def test_loopback_capture_compares_control_and_effective_disable_without_inference(self):
        result = probe(runner=self.runner)
        self.assertEqual(result["unified_only"]["command_tools"], ["exec_command", "write_stdin"])
        self.assertEqual(result["shell_disabled"]["command_tools"], [])
        self.assertFalse(result["inference"] or result["tool_execution"])
        self.assertNotIn("private prompt", json.dumps(result))
        for argv, settings, kwargs in self.calls:
            self.assertIn("--ephemeral", argv)
            self.assertIn("--ignore-user-config", argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertFalse(kwargs["shell"])
            self.assertFalse(settings["model_providers.pmkit_offline_probe.requires_openai_auth"])
            self.assertFalse(settings["features.enable_request_compression"])
            self.assertFalse(Path(kwargs["cwd"]).exists())   # only owned diagnostic homes are removed

    def test_probe_does_not_borrow_credentials_lock_or_parent_identity(self):
        parent = {"PATH": "keep", "OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret",
                  "CLAUDE_AGENT_MODEL": "model", "MACHINE_LOCK_RUN": "run", "PM_KIT_ROLE": "pm",
                  "HTTPS_PROXY": "proxy"}
        with mock.patch.dict(os.environ, parent, clear=True):
            probe(runner=self.runner)
            self.assertEqual(dict(os.environ), parent)
        for _, _, kwargs in self.calls:
            env = kwargs["env"]
            self.assertFalse(set(parent) - {"PATH", "PM_KIT_ROLE"} & set(env))
            self.assertEqual(env["PM_KIT_ROLE"], "worker")

    def test_tool_names_cover_namespaces_without_echoing_schemas(self):
        specs = [{"type": "namespace", "name": "functions", "tools": [
            {"type": "function", "name": "write_stdin", "description": "private"}]},
                 {"type": "local_shell"}]
        self.assertEqual(tool_names(specs), ("local_shell", "write_stdin"))
        for invalid in (None, {}, ["invalid"], [{"type": "namespace"}]):
            with self.subTest(specs=invalid), self.assertRaises(ValueError):
                tool_names(invalid)

    def test_native_additional_tools_are_read_without_scanning_prompt_text(self):
        request = {"input": [{"type": "additional_tools", "role": "developer", "tools": [
            {"type": "function", "name": "write_stdin"}]},
            {"type": "message", "content": [{"type": "input_text", "text": "do not run exec_command"}]}]}
        self.assertEqual(request_tool_names(request), ("write_stdin",))
        with self.assertRaises(ValueError):
            request_tool_names({"input": [{"type": "message", "content": "tools are unavailable"}]})

    def test_nested_code_mode_declarations_are_distinct_from_examples_and_user_text(self):
        request = {"input": [
            {"type": "message", "role": "developer", "content": [{"text":
                "example: await tools.exec_command(...)\n"
                "declare const tools: { write_stdin(args: {\n"
                "declare const tools: { view_image(args: {\n"}]},
            {"type": "message", "role": "user", "content": [{"text":
                "declare const tools: { exec_command(args: {"}]}]}
        self.assertEqual(nested_tool_names(request), ("view_image", "write_stdin"))

    def test_native_code_mode_spec_description_contains_the_nested_inventory(self):
        request = {"input": [{"type": "additional_tools", "role": "developer", "tools": [
            {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec",
             "description": "example: tools.exec_command(...)\n"
                            "  declare const tools: { write_stdin(args: {\n"
                            "  declare const tools: { view_image(args: {\n"}]}]}]}
        self.assertEqual(nested_tool_names(request), ("view_image", "write_stdin"))

    def test_opaque_code_mode_is_not_proof_of_command_absence(self):
        def opaque(argv, **kwargs):
            return self.send(argv, [{"type": "custom", "name": "exec"}], kwargs)
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            probe(runner=opaque)

    def test_canary_result_rejects_callable_methods_or_missing_marker(self):
        def payload(value):
            return {"input": [{"type": "custom_tool_call_output", "call_id": "pmkit_canary_call",
                               "output": [{"type": "input_text", "text": "PM_KIT_TOOL_CANARY " + json.dumps(value)}]}]}
        absent = {"exec_command": "undefined", "write_stdin": "undefined"}
        self.assertEqual(canary_result(payload(absent)), absent)
        for value in ({"exec_command": "function", "write_stdin": "undefined"}, {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canary_result(payload(value))
        with self.assertRaises(ValueError):
            canary_result({"input": []})

    def test_stub_only_emits_fixed_typeof_canary_and_requires_the_native_result(self):
        base = self.runner
        def run(argv, **kwargs):
            if "Local method-existence canary only. No commands or filesystem operations." not in argv:
                return base(argv, **kwargs)
            settings = {argv[index + 1].split("=", 1)[0]: json.loads(argv[index + 1].split("=", 1)[1])
                        for index, arg in enumerate(argv[:-1]) if arg == "--config"}
            endpoint = settings["model_providers.pmkit_offline_probe.base_url"] + "/responses"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(payload):
                request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                                 headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=5) as response:
                    return [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
            events = post({"tools": [{"type": "custom", "name": "exec", "description":
                                     "declare const tools: { view_image(args: {\n"}]})
            self.assertEqual(events[0]["item"]["input"], CANARY_SCRIPT)
            self.assertNotIn("await", CANARY_SCRIPT)
            post({"input": [{"type": "custom_tool_call_output", "call_id": "pmkit_canary_call", "output":
                            [{"type": "input_text", "text": 'PM_KIT_TOOL_CANARY {"exec_command":"undefined","write_stdin":"undefined"}'}]}]})
            return SimpleNamespace(returncode=0)
        result = probe(runner=run, canary=True)
        self.assertEqual(result["code_mode_canary"]["method_availability"],
                         {"exec_command": "undefined", "write_stdin": "undefined"})
        self.assertFalse(result["inference"])
        self.assertEqual((result["shell_commands"], result["filesystem_tool_operations"]), (0, 0))

    def test_remaining_command_exposure_refuses_verification(self):
        def bad(argv, **kwargs):
            return self.send(argv, [{"type": "function", "name": "functions.write_stdin"}], kwargs)
        with self.assertRaisesRegex(RuntimeError, "remain exposed"):
            probe(runner=bad)

    def test_no_capture_or_unexpected_native_success_is_not_verification(self):
        with self.assertRaisesRegex(RuntimeError, "not captured"):
            probe(runner=lambda *a, **k: SimpleNamespace(returncode=1))
        with self.assertRaisesRegex(RuntimeError, "not captured"):
            probe(runner=lambda argv, **kwargs: self.send(argv, [{"type": "function", "name": "view_image"}], kwargs, 0))
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            probe(runner=mock.Mock(side_effect=subprocess.TimeoutExpired("private arguments", 25)))

    def test_idle_connection_cannot_block_failed_probe_cleanup(self):
        connections = []
        self.addCleanup(lambda: [connection.close() for connection in connections])
        def idle(argv, **kwargs):
            settings = {argv[index + 1].split("=", 1)[0]: json.loads(argv[index + 1].split("=", 1)[1])
                        for index, arg in enumerate(argv[:-1]) if arg == "--config"}
            endpoint = settings["model_providers.pmkit_offline_probe.base_url"]
            port = int(endpoint.split(":")[-1].split("/", 1)[0])
            connections.append(socket.create_connection(("127.0.0.1", port), timeout=2))
            return SimpleNamespace(returncode=1)
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            probe(runner=idle)
        self.assertLess(time.monotonic() - started, 5)
