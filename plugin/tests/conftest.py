"""A fake Imagination Studio (http.server) so the client, tools and director can be tested end to end."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FakeStudio:
    def __init__(self, home: Path):
        self.home = home
        self.jobs: dict[str, dict] = {}
        self.takes: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_kinds: set[str] = set()
        self.n = 0

    def new_id(self):
        self.n += 1
        return f"j{self.n}"

    def finish(self, job):
        kind, params = job["kind"], job["params"]
        jid = job["id"]
        if kind in self.fail_kinds:
            job.update(status="failed", error="boom")
            return
        if kind == "scout":
            folder = self.home / "scouts" / jid
            folder.mkdir(parents=True)
            outs = []
            for i in range(int(params.get("count", 4))):
                p = folder / f"scout_s{i + 1}.png"
                p.write_bytes(b"\x89PNG" + bytes([i]))
                outs.append(str(p.relative_to(self.home)))
            job["outputs"] = outs + [str((folder / "sheet.jpg").relative_to(self.home))]
        elif kind in ("video", "foley", "export"):
            folder = self.home / ("exports" if kind == "export" else "renders")
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / f"{jid}.mp4"
            p.write_bytes(b"mp4")
            job["outputs"] = [str(p.relative_to(self.home))]
            if kind == "video":
                job["qa"] = {"pass": True}
        else:
            job["outputs"] = []
        job["status"] = "done"


def make_handler(studio: FakeStudio):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            path = self.path.split("?")[0]
            studio.calls.append(("GET", path, {}))
            if path == "/api/state":
                return self._send(200, {"platform": "spark", "presets": ["small", "2k"], "durations": [5, 10, 15], "home": str(studio.home)})
            if path == "/api/jobs":
                return self._send(200, list(studio.jobs.values()))
            if path.startswith("/api/jobs/"):
                job = studio.jobs.get(path.rsplit("/", 1)[1])
                if not job:
                    return self._send(404, {"detail": "no such job"})
                if job["status"] == "running":
                    job["polls"] = job.get("polls", 0) + 1
                    if job["polls"] >= 2:
                        studio.finish(job)
                return self._send(200, job)
            if path == "/api/library":
                return self._send(200, [{"path": "renders/a.mp4", "kind": "video"}, {"path": "scouts/x/s1.png", "kind": "image"}])
            if path.startswith("/api/voice/takes/") or path.startswith("/api/music/takes/"):
                take = studio.takes.get(path.rsplit("/", 1)[1])
                return self._send(200, take) if take else self._send(404, {"detail": "no take"})
            return self._send(404, {"detail": "nope"})

        def do_POST(self):
            path = self.path
            body = self._body()
            studio.calls.append(("POST", path, body))
            if path == "/api/jobs":
                if body.get("kind") not in ("scout", "video", "studio", "export", "long", "edit", "sfx", "foley", "upscale", "turnaround", "mask", "remove"):
                    return self._send(400, {"detail": "unknown job kind"})
                if body["kind"] not in ("export", "edit", "foley") and not (body.get("params") or {}).get("prompt"):
                    return self._send(400, {"detail": "a prompt is needed"})
                job = {"id": studio.new_id(), "kind": body["kind"], "params": body.get("params") or {}, "title": body.get("title"),
                       "status": "running", "outputs": [], "progress": None, "error": None, "log": "queued"}
                studio.jobs[job["id"]] = job
                return self._send(200, job)
            if path == "/api/prompt/check":
                return self._send(200, {"negations": ["rain"] if "no rain" in body.get("prompt", "") else []})
            if path == "/api/estimate":
                return self._send(200, {"fits": True, "seconds": 120})
            if path == "/api/compose":
                return self._send(200, {"prompt": "composed: " + json.dumps(body.get("fields"))})
            if path in ("/api/voice/takes", "/api/music/takes"):
                kind = "voice" if "voice" in path else "music"
                tid = f"{kind}{len(studio.takes) + 1}"
                studio.takes[tid] = {"id": tid, "status": "done", "path": f"{kind}/{tid}.wav", **{k: body.get(k) for k in ("text", "caption")}}
                return self._send(200, {"id": tid, "status": "queued"})
            if path == "/api/qa":
                job = {"id": studio.new_id(), "kind": "qa", "params": body, "status": "done", "outputs": [], "qa": {"pass": True, "path": body.get("path")}}
                studio.jobs[job["id"]] = job
                return self._send(200, job)
            return self._send(404, {"detail": "nope"})
    return H


@pytest.fixture
def studio(tmp_path):
    fake = FakeStudio(tmp_path / "ImaginationStudio")
    fake.home.mkdir()
    (fake.home / "access-key").write_text("secret-key\n")
    server = HTTPServer(("127.0.0.1", 0), make_handler(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    yield fake
    server.shutdown()
