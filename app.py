"""Gitea workflow_job -> GitHub workflow_dispatch. Python standard library only."""
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def make_handler(secret, token, repository, workflow, ref="main", label="github-hosted",
                 db_path="bridge.db"):
    owner, repo = repository.split("/")
    url = "https://api.github.com/repos/{}/{}/actions/workflows/{}/dispatches".format(
        quote(owner, safe=""), quote(repo, safe=""), quote(workflow, safe="")
    )
    target = url + "#" + ref
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS dispatches (
            target TEXT, repo INTEGER, job INTEGER, attempt INTEGER,
            state TEXT NOT NULL, PRIMARY KEY (target, repo, job, attempt)
        )""")

    def claim(key):
        with closing(sqlite3.connect(db_path, timeout=5)) as db, db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO dispatches VALUES (?, ?, ?, ?, 'pending')", key
            ).rowcount
            state = db.execute(
                "SELECT state FROM dispatches WHERE target=? AND repo=? AND job=? AND attempt=?",
                key,
            ).fetchone()[0]
        return inserted, state

    def finish(key, accepted):
        with closing(sqlite3.connect(db_path, timeout=5)) as db, db:
            if accepted:
                db.execute("UPDATE dispatches SET state='sent' "
                           "WHERE target=? AND repo=? AND job=? AND attempt=?", key)
            else:
                db.execute("DELETE FROM dispatches "
                           "WHERE target=? AND repo=? AND job=? AND attempt=?", key)

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
                key = (target, int(payload["repository"]["id"]), int(job["id"]),
                       int(job.get("run_attempt", 1)))
            except (ValueError, TypeError, KeyError, AttributeError):
                return self.reply(400, "invalid payload")
            try:
                inserted, state = claim(key)
            except sqlite3.Error:
                return self.reply(503, "database unavailable")
            if not inserted:
                if state == "sent":
                    return self.reply(200, "duplicate")
                return self.reply(409, "dispatch pending or uncertain; check GitHub before clearing record")
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
                        return self.reply(502, "dispatch uncertain; check GitHub before clearing record")
            except HTTPError as error:
                # Explicit client rejection can be retried after fixing the request.
                # 408/5xx and transport failures may follow an accepted dispatch.
                if 400 <= error.code < 500 and error.code != 408:
                    try:
                        finish(key, False)
                    except sqlite3.Error:
                        return self.reply(503, "database unavailable; check dispatch record")
                    return self.reply(502, "GitHub rejected dispatch; fix configuration before retrying")
                return self.reply(502, "dispatch uncertain; check GitHub before clearing record")
            except (URLError, TimeoutError, OSError):
                return self.reply(502, "dispatch uncertain; check GitHub before clearing record")
            try:
                finish(key, True)
            except sqlite3.Error:
                return self.reply(503, "GitHub accepted dispatch but database update failed; do not retry blindly")
            self.reply(202, "dispatched")

    return Handler


if __name__ == "__main__":
    required = ("WEBHOOK_SECRET", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    handler = make_handler(*(os.environ[name] for name in required),
                           ref=os.getenv("GITHUB_REF", "main"),
                           label=os.getenv("RUNNER_LABEL", "github-hosted"),
                           db_path=os.getenv("SQLITE_PATH", "bridge.db"))
    print("Listening on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), handler).serve_forever()
