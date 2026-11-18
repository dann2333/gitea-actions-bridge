import hashlib
import hmac
import http.client
import json
import threading
import tempfile
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app import make_handler


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "bridge.db")
        self.start_server()
        self.payload = {"action": "queued", "repository": {"id": 1},
                        "workflow_job": {"id": 2, "run_attempt": 1,
                                         "labels": ["github-hosted"]}}
        self.upstream = patch("app.urlopen").start()
        response = MagicMock(status=204)
        self.upstream.return_value.__enter__.return_value = response

    def tearDown(self):
        patch.stopall()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def start_server(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            "test-secret", "test-token", "owner/bridge", "runner.yml", db_path=self.db_path))
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(0.05), daemon=True)
        self.thread.start()

    def restart(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.start_server()

    def send(self, payload=None, signature=None, event="workflow_job"):
        body = json.dumps(self.payload if payload is None else payload).encode()
        signature = signature if signature is not None else hmac.new(
            b"test-secret", body, hashlib.sha256).hexdigest()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("POST", "/webhook", body, {
            "X-Gitea-Signature": signature, "X-Gitea-Event": event})
        response = connection.getresponse()
        result = response.status, response.read().decode()
        connection.close()
        return result

    def test_dispatch_only_fixed_ref(self):
        self.assertEqual(self.send()[0], 202)
        request = self.upstream.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"ref": "main"})
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/owner/bridge/actions/workflows/runner.yml/dispatches")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_invalid_signature(self):
        self.assertEqual(self.send(signature="bad")[0], 401)
        self.upstream.assert_not_called()

    def test_ignore_other_event(self):
        self.assertEqual(self.send(event="push")[0], 200)
        self.upstream.assert_not_called()

    def test_ignore_other_state_or_label(self):
        self.payload["action"] = "completed"
        self.assertEqual(self.send()[0], 200)
        self.payload["action"] = "queued"
        self.payload["workflow_job"]["labels"] = ["local"]
        self.assertEqual(self.send()[0], 200)
        self.upstream.assert_not_called()

    def test_dedup_and_rerun(self):
        self.assertEqual(self.send()[0], 202)
        self.assertEqual(self.send(), (200, "duplicate\n"))
        self.assertEqual(self.upstream.call_count, 1)
        self.payload["workflow_job"]["run_attempt"] = 2
        self.assertEqual(self.send()[0], 202)
        self.assertEqual(self.upstream.call_count, 2)

    def test_failure_not_cached_or_leaked(self):
        self.upstream.side_effect = HTTPError("https://api.github.com", 403,
                                              "private secret", {}, None)
        status, message = self.send()
        self.assertEqual(status, 502)
        self.assertNotIn("private secret", message)
        self.upstream.side_effect = None
        self.assertEqual(self.send()[0], 202)

    def test_success_survives_restart(self):
        self.assertEqual(self.send()[0], 202)
        self.restart()
        self.assertEqual(self.send(), (200, "duplicate\n"))
        self.assertEqual(self.upstream.call_count, 1)

    def test_ambiguous_failure_survives_restart(self):
        for error in (TimeoutError(), HTTPError("https://api.github.com", 500, "secret", {}, None)):
            self.payload["workflow_job"]["id"] += 1
            self.upstream.side_effect = error
            self.assertEqual(self.send()[0], 502)
            self.restart()
            calls = self.upstream.call_count
            self.upstream.side_effect = None
            self.assertEqual(self.send()[0], 409)
            self.assertEqual(self.upstream.call_count, calls)

    def test_crashed_pending_record_not_retried(self):
        target = "https://api.github.com/repos/owner/bridge/actions/workflows/runner.yml/dispatches#main"
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO dispatches VALUES (?, 1, 2, 1, 'pending')", (target,))
        self.restart()
        self.assertEqual(self.send()[0], 409)
        self.upstream.assert_not_called()

    def test_rejection_can_retry_after_restart(self):
        self.upstream.side_effect = HTTPError("https://api.github.com", 403, "secret", {}, None)
        self.assertEqual(self.send()[0], 502)
        self.restart()
        self.upstream.side_effect = None
        self.assertEqual(self.send()[0], 202)

    def test_same_key_concurrent_only_dispatches_once(self):
        entered, release = threading.Event(), threading.Event()
        response = self.upstream.return_value

        def wait(*args, **kwargs):
            entered.set()
            release.wait(3)
            return response

        self.upstream.side_effect = wait
        with ThreadPoolExecutor() as pool:
            first = pool.submit(self.send)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.send()[0], 409)
            finally:
                release.set()
            self.assertEqual(first.result()[0], 202)
        self.assertEqual(self.upstream.call_count, 1)

    def test_different_keys_dispatch_concurrently(self):
        barrier = threading.Barrier(2)
        response = self.upstream.return_value

        def wait(*args, **kwargs):
            barrier.wait(2)  # Fails if a global lock spans the network request.
            return response

        self.upstream.side_effect = wait
        other = json.loads(json.dumps(self.payload))
        other["workflow_job"]["id"] = 3
        with ThreadPoolExecutor() as pool:
            first = pool.submit(self.send)
            second = pool.submit(self.send, other)
            self.assertEqual(first.result()[0], 202)
            self.assertEqual(second.result()[0], 202)

    def test_database_failure_prevents_dispatch(self):
        with patch("app.sqlite3.connect", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(self.send()[0], 503)
        self.upstream.assert_not_called()

    def test_database_failure_after_acceptance_does_not_redispatch(self):
        response = self.upstream.return_value

        def accepted(*args, **kwargs):
            self.failure = patch("app.sqlite3.connect", side_effect=sqlite3.OperationalError("disk full"))
            self.failure.start()
            return response

        self.upstream.side_effect = accepted
        try:
            self.assertEqual(self.send()[0], 503)
        finally:
            self.failure.stop()
        self.upstream.side_effect = None
        self.restart()
        self.assertEqual(self.send()[0], 409)
        self.assertEqual(self.upstream.call_count, 1)

    def test_bad_payload(self):
        for payload in ({}, [], {"workflow_job": None}):
            self.assertEqual(self.send(payload=payload)[0], 400)
        self.upstream.assert_not_called()

    def test_health(self):
        connection = http.client.HTTPConnection(*self.server.server_address)
        connection.request("GET", "/health")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"ok\n"))
        connection.close()


if __name__ == "__main__":
    unittest.main()
