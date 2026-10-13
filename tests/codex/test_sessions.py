import io
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest import mock

from hosts.codex.adapter.sessions import ReadOnlyAppServer, SessionHost


class RecordedInput(io.StringIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class Process:
    def __init__(self, messages):
        self.stdin = RecordedInput()
        self.stdout = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        self.terminate = mock.Mock()
        self.kill = mock.Mock()
        self.wait = mock.Mock(return_value=0)


class SessionHostTests(unittest.TestCase):
    def setUp(self):
        native = mock.patch("hosts.codex.adapter.sessions.native_codex", return_value=Path("codex.exe").resolve())
        native.start()
        self.addCleanup(native.stop)

    def test_pagination_is_bounded_and_status_is_not_guessed(self):
        request = mock.Mock(side_effect=[{"data": [{"id": "a", "status": {"type": "notLoaded"}, "preview": "private text"}], "nextCursor": "next"},
                                         {"data": [{"id": "b"}], "nextCursor": None}])
        result = SessionHost(request).inventory(cwd="repo")
        self.assertTrue(result["complete"])
        self.assertEqual(result["sessions"][0]["status"], {"type": "notLoaded"})
        self.assertEqual(result['sessions'][0]['liveness'], 'unknown')
        self.assertNotIn("preview", result["sessions"][0])
        self.assertEqual(request.call_args_list[1].args[1]["cursor"], "next")

    def test_incomplete_pagination_is_reported(self):
        request = mock.Mock(return_value={"data": [], "nextCursor": "next"})
        self.assertFalse(SessionHost(request).inventory(max_pages=1)["complete"])
        with self.assertRaisesRegex(ValueError, "past 1 pages"):
            SessionHost(request).sessions(max_pages=1)

    def test_invalid_pagination_bounds_are_refused_before_transport(self):
        request = mock.Mock()
        for bound in (True, 1.5, 0, 101):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                SessionHost(request).sessions(max_pages=bound)
        request.assert_not_called()

    def test_repeated_cursor_is_refused(self):
        request = mock.Mock(return_value={"data": [], "nextCursor": "next"})
        with self.assertRaises(ValueError):
            SessionHost(request).sessions()

    def test_usage_and_models_use_supported_read_methods_only(self):
        request = mock.Mock(return_value={})
        host = SessionHost(request)
        host.loaded()
        host.usage()
        host.models()
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["thread/loaded/list", "account/rateLimits/read", "model/list"])

    def test_native_proxy_handshake_and_metadata_request_do_not_send_turns(self):
        process = Process([{"id": 1, "result": {}}, {"method": "peer/notification", "params": {"text": "ignored"}},
                           {"id": 2, "result": {"data": []}}])
        factory = mock.Mock(return_value=process)
        with ReadOnlyAppServer(popen=factory) as client:
            self.assertEqual(client.request("thread/list", {}), {"data": []})
            with self.assertRaises(ValueError):
                client.request("turn/start", {})
        sent = [json.loads(line) for line in process.stdin.sent.splitlines()]
        self.assertEqual([m["method"] for m in sent], ["initialize", "initialized", "thread/list"])
        self.assertTrue(factory.call_args.args[0][0].endswith("codex.exe"))
        process.terminate.assert_not_called()

    def test_unexpected_execution_request_is_denied_even_with_matching_id(self):
        process = Process([{"id": 1, "result": {}}, {"id": 2, "method": "exec/request", "params": {}},
                           {"id": 2, "result": {"data": []}}])
        with ReadOnlyAppServer(popen=lambda *a, **k: process) as client:
            self.assertEqual(client.request("thread/list", {}), {"data": []})
        messages = [json.loads(line) for line in process.stdin.sent.splitlines()]
        self.assertTrue(any(m.get("error", {}).get("code") == -32601 for m in messages))

    def test_proxy_error_is_not_an_empty_inventory(self):
        process = Process([{"id": 1, "result": {}}, {"id": 2, "error": {"message": "unavailable"}}])
        with ReadOnlyAppServer(popen=lambda *a, **k: process) as client, self.assertRaises(RuntimeError):
            client.request("thread/list", {})

    def test_eof_and_bad_json_are_errors(self):
        for data in ("", "not json\n"):
            process = Process([])
            process.stdout = io.StringIO(data)
            with self.subTest(data=data), self.assertRaises((EOFError, RuntimeError)):
                with ReadOnlyAppServer(popen=lambda *a, **k: process):
                    pass

    def test_transcript_read_is_refused_before_transport(self):
        process = Process([{"id": 1, "result": {}}])
        with ReadOnlyAppServer(popen=lambda *a, **k: process) as client:
            for params in ({}, {"includeTurns": True}, {"includeTurns": False}):
                with self.subTest(params=params), self.assertRaises(ValueError):
                    client.request("thread/read", params)
        self.assertNotIn('"method": "thread/read"', process.stdin.sent)

    def test_stubborn_proxy_is_killed_and_primary_error_survives(self):
        process = Process([{"id": 1, "result": {}}])
        process.wait.side_effect = [subprocess.TimeoutExpired("proxy", 2)] * 3
        client = ReadOnlyAppServer(popen=lambda *a, **k: process)
        with self.assertRaisesRegex(ValueError, "primary"):
            with client:
                raise ValueError("primary")
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertTrue(client.cleanup_errors)
        self.assertIsNone(client.process)

    def test_normal_cleanup_failure_is_reported(self):
        process = Process([{"id": 1, "result": {}}])
        process.wait.side_effect = [subprocess.TimeoutExpired("proxy", 2)] * 3
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            with ReadOnlyAppServer(popen=lambda *a, **k: process):
                pass

    def test_blocked_stdout_is_closed_only_by_reader_after_kill(self):
        stopped = threading.Event()
        owners = []
        class Stream:
            first = True
            def readline(self, limit):
                if self.first:
                    self.first = False
                    return json.dumps({"id": 1, "result": {}}) + '\n'
                stopped.wait(5)
                return ''
            def close(self):
                owners.append(threading.current_thread())
        process = Process([])
        process.stdout = Stream()
        process.wait.side_effect = [subprocess.TimeoutExpired("proxy", 2)] * 2 + [0]
        process.kill.side_effect = stopped.set
        client = ReadOnlyAppServer(popen=lambda *a, **k: process)
        with client:
            pass
        self.assertEqual(owners, [client.reader])
        self.assertFalse(client.cleanup_errors)

    def test_proxy_does_not_inherit_session_lock_or_identity(self):
        parent = {"PATH": "keep", "PM_KIT_ROLE": "pm", "MACHINE_LOCK_RUN": "parent",
                  "MACHINE_LOCK_CPUMASK": "15", "CLAUDE_AGENT_MODEL": "model"}
        factory = mock.Mock(return_value=Process([{"id": 1, "result": {}}]))
        with mock.patch.dict(os.environ, parent, clear=True):
            with ReadOnlyAppServer(popen=factory):
                pass
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(factory.call_args.kwargs["env"], {"PATH": "keep"})
