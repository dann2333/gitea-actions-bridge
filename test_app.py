import hashlib
import hmac
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app import make_handler


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            "test-secret", "test-token", "owner/bridge", "runner.yml"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
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
