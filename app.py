"""Gitea workflow_job -> GitHub workflow_dispatch. Python standard library only."""
import hashlib
import hmac
import json
import os
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def make_handler(secret, token, repository, workflow, ref="main", label="github-hosted"):
    owner, repo = repository.split("/")
    url = "https://api.github.com/repos/{}/{}/actions/workflows/{}/dispatches".format(
        quote(owner, safe=""), quote(repo, safe=""), quote(workflow, safe="")
    )
    seen = OrderedDict()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass  # Never log webhook payloads, request paths or credentials.

        def reply(self, status, message):
            body = (message + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404,
                       "ok" if self.path == "/health" else "not found")

        def do_POST(self):
            if self.path != "/webhook":
                return self.reply(404, "not found")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, "invalid content length")
            if not 0 < length <= 1024 * 1024:
                return self.reply(413, "invalid body size")
            body = self.rfile.read(length)
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            signature = self.headers.get("X-Gitea-Signature", "")
            if not hmac.compare_digest(expected.encode(), signature.encode()):
                return self.reply(401, "invalid signature")
            if self.headers.get("X-Gitea-Event") != "workflow_job":
                return self.reply(200, "ignored")
            try:
                payload = json.loads(body)
                job = payload["workflow_job"]
                labels = job.get("labels", [])
                if not isinstance(labels, list):
                    raise ValueError()
                if payload.get("action") != "queued" or label not in labels:
                    return self.reply(200, "ignored")
                # Job identity catches repeated deliveries with new delivery IDs.
                key = (int(payload["repository"]["id"]), int(job["id"]),
                       int(job.get("run_attempt", 1)))
            except (ValueError, TypeError, KeyError, AttributeError):
                return self.reply(400, "invalid payload")
            with lock:
                if key in seen:
                    return self.reply(200, "duplicate")
                request = Request(url, data=json.dumps({"ref": ref}).encode(), headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "gitea-gh-bridge",
                    "X-GitHub-Api-Version": "2022-11-28",
                })
                try:
                    with urlopen(request, timeout=10) as response:
                        if not 200 <= response.status < 300:
                            return self.reply(502, "GitHub dispatch failed")
                except (HTTPError, URLError, TimeoutError, OSError):
                    # Do not echo upstream bodies: these may contain private data.
                    return self.reply(502, "GitHub dispatch failed; check delivery before retrying")
                seen[key] = None
                if len(seen) > 4096:
                    seen.popitem(last=False)
            self.reply(202, "dispatched")

    return Handler


if __name__ == "__main__":
    required = ("WEBHOOK_SECRET", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    handler = make_handler(*(os.environ[name] for name in required),
                           ref=os.getenv("GITHUB_REF", "main"),
                           label=os.getenv("RUNNER_LABEL", "github-hosted"))
    print("Listening on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), handler).serve_forever()
