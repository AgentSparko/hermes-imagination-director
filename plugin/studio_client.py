"""Thin client for the Imagination Studio REST API (app/server.py routes). urllib only, loopback by default.

Auth: requests from this computer need no key; from elsewhere the studio wants ``X-Studio-Key`` =
``<studio folder>/access-key``. The key is read from that file when not configured and never logged.
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

JOB_KINDS = ("scout", "video", "studio", "long", "export", "edit", "turnaround", "upscale", "mask", "remove", "sfx", "foley")
TERMINAL = {"done", "finished", "failed", "error", "cancelled", "canceled"}


class StudioError(RuntimeError):
    pass


class StudioClient:
    def __init__(self, url: str = "http://127.0.0.1:7870", key: str = "", home: str = "~/ImaginationStudio", timeout: int = 60):
        self.url = url.rstrip("/")
        self.home = Path(os.path.expanduser(home))
        self._key = key or self._read_key()
        self.timeout = timeout

    def _read_key(self) -> str:
        path = self.home / "access-key"
        try:
            return path.read_text().strip() if path.is_file() else ""
        except OSError:
            return ""

    # ---------- http ----------
    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Accept": "application/json"}
        if self._key:
            h["X-Studio-Key"] = self._key
        h.update(extra or {})
        return h

    def request(self, method: str, path: str, body: Any = None, *, raw: bool = False, timeout: Optional[int] = None) -> Any:
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:  # noqa: S310 - local studio
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise StudioError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise StudioError(f"studio not reachable at {self.url}: {e}") from None
        if raw:
            return payload
        return json.loads(payload.decode() or "{}")

    def get(self, path: str, **params) -> Any:
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None, timeout: Optional[int] = None) -> Any:
        return self.request("POST", path, body or {}, timeout=timeout)

    # ---------- state and library ----------
    def alive(self) -> bool:
        try:
            self.get("/api/state")
            return True
        except StudioError:
            return False

    def state(self) -> dict:
        return self.get("/api/state")

    def library(self) -> list[dict]:
        data = self.get("/api/library")
        return data if isinstance(data, list) else data.get("items") or data.get("files") or []

    def file_url(self, rel_path: str) -> str:
        return f"{self.url}/files/{rel_path.lstrip('/')}"

    def local_path(self, rel_path: str) -> Path:
        return self.home / rel_path.lstrip("/")

    def download(self, rel_path: str, dest: Path) -> Path:
        local = self.local_path(rel_path)
        if local.is_file():
            return local
        data = self.request("GET", "/files/" + urllib.parse.quote(rel_path.lstrip("/")), raw=True, timeout=600)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def upload(self, path: str | Path) -> dict:
        """Multipart upload of a local file (``/api/upload``); returns the studio's answer ({path: ...})."""
        path = Path(path)
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/api/upload", data=body, method="POST",
                                     headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise StudioError(f"upload failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from None

    # ---------- jobs ----------
    def submit(self, kind: str, params: dict, title: str = "") -> dict:
        if kind not in JOB_KINDS:
            raise StudioError(f"unknown job kind {kind!r}; one of {', '.join(JOB_KINDS)}")
        body: dict[str, Any] = {"kind": kind, "params": params}
        if title:
            body["title"] = title
        return self.post("/api/jobs", body)

    def jobs(self) -> list[dict]:
        data = self.get("/api/jobs")
        return data if isinstance(data, list) else data.get("jobs") or []

    def job(self, job_id: str) -> dict:
        return self.get(f"/api/jobs/{job_id}")

    def cancel(self, job_id: str) -> dict:
        return self.post(f"/api/jobs/{job_id}/cancel")

    def wait(self, job_id: str, timeout_s: float = 3600, poll_s: float = 5.0, on_progress=None) -> dict:
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            job = self.job(job_id)
            status = str(job.get("status") or "").lower()
            if on_progress and (job.get("progress"), status) != last:
                last = (job.get("progress"), status)
                on_progress(job)
            if status in TERMINAL:
                return job
            time.sleep(poll_s)
        raise StudioError(f"job {job_id} still {status!r} after {int(timeout_s)} s")

    def estimate(self, kind: str, params: dict) -> dict:
        return self.post("/api/estimate", {"kind": kind, **params})

    def prompt_check(self, prompt: str) -> dict:
        return self.post("/api/prompt/check", {"prompt": prompt})

    def compose(self, fields: dict, seconds: float | None = None, first_frame: bool = False, last_frame: bool = False) -> dict:
        body: dict[str, Any] = {"fields": fields, "first_frame": first_frame, "last_frame": last_frame}
        if seconds:
            body["seconds"] = seconds
        return self.post("/api/compose", body)

    # ---------- voice / music / qa ----------
    def voice_take(self, text: str, **extra) -> dict:
        return self.post("/api/voice/takes", {"text": text, **extra})

    def voice_state(self) -> dict:
        return self.get("/api/voice/state")

    def music_take(self, caption: str, seconds: float = 30, lyrics: str = "", **extra) -> dict:
        body = {"caption": caption, "seconds": seconds, **extra}
        if lyrics:
            body["lyrics"] = lyrics
        return self.post("/api/music/takes", body)

    def take(self, kind: str, take_id: str) -> dict:
        return self.get(f"/api/{kind}/takes/{take_id}")

    def wait_take(self, kind: str, take_id: str, timeout_s: float = 1800, poll_s: float = 4.0) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            take = self.take(kind, take_id)
            status = str(take.get("status") or ("done" if take.get("path") or take.get("audio") else "running")).lower()
            if status in TERMINAL:
                return take
            time.sleep(poll_s)
        raise StudioError(f"{kind} take {take_id} not finished after {int(timeout_s)} s")

    def qa(self, path: str, prompt: str = "", **extra) -> dict:
        return self.post("/api/qa", {"path": path, **({"prompt": prompt} if prompt else {}), **extra})

    def bolt(self, action: str) -> dict:
        return self.post(f"/api/bolt/{action}")


def job_summary(job: dict) -> dict:
    """The fields an agent needs from a job record (log trimmed, params kept short)."""
    out = {k: job.get(k) for k in ("id", "kind", "status", "title", "progress", "error", "outputs", "elapsed", "estimate", "qa", "engine")}
    out["outputs"] = [o for o in (job.get("outputs") or []) if o]
    log = str(job.get("log") or "")
    if log:
        out["log_tail"] = log[-600:]
    return out
