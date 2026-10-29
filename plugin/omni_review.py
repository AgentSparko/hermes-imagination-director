"""LFM3-Omni-2B review of stills and clips through llama-server (image_url / input_video parts)."""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REVIEW_PROMPT = (
    "You are a film director's assistant reviewing generated footage. The shot was meant to show: \"{intent}\". "
    "Answer with JSON only: {{\"score\": 1-10, \"matches_intent\": true/false, \"problems\": [\"...\"], "
    "\"notes\": \"one sentence\"}}. Problems to look for: wrong subject, extra limbs or faces, text artifacts, "
    "frozen or jittery motion, wrong framing, wrong time of day, unreadable action."
)
PICK_PROMPT = (
    "You see {n} candidate stills for the shot: \"{intent}\". Answer with JSON only: "
    "{{\"best\": <1-based index>, \"reason\": \"one sentence\", \"scores\": [..]}}."
)
_JSON = re.compile(r"\{.*\}", re.S)


def _b64_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _part(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".mov", ".webm", ".mkv"):
        return {"type": "input_video", "input_video": {"data": base64.b64encode(path.read_bytes()).decode("ascii")}}
    return {"type": "image_url", "image_url": {"url": _b64_data_url(path)}}


def _parse_json(text: str) -> dict:
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    m = _JSON.search(text)
    if not m:
        return {"raw": text.strip()}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"raw": text.strip()}


class OmniReviewer:
    def __init__(self, url: str = "http://127.0.0.1:8182", timeout: int = 300):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _chat(self, parts: list[dict], max_tokens: int = 300) -> str:
        payload = {"model": "LFM3-Omni-2B", "temperature": 0.1, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": parts}]}
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - loopback
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"LFM3-Omni not reachable at {self.url}: {e}") from None
        return ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as resp:  # noqa: S310
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def review(self, path: str | Path, intent: str, question: str = "") -> dict[str, Any]:
        path = Path(path)
        if not path.is_file():
            return {"error": f"no such file: {path}"}
        prompt = question or REVIEW_PROMPT.format(intent=intent.replace('"', "'"))
        out = _parse_json(self._chat([_part(path), {"type": "text", "text": prompt}]))
        out["file"] = str(path)
        return out

    def pick(self, paths: list[str | Path], intent: str) -> dict[str, Any]:
        files = [Path(p) for p in paths if Path(p).is_file()]
        if not files:
            return {"error": "no candidate files"}
        parts = [_part(p) for p in files] + [{"type": "text", "text": PICK_PROMPT.format(n=len(files), intent=intent.replace('"', "'"))}]
        out = _parse_json(self._chat(parts, 200))
        try:
            idx = int(out.get("best", 1)) - 1
        except (TypeError, ValueError):
            idx = 0
        out["best_file"] = str(files[max(0, min(idx, len(files) - 1))])
        out["candidates"] = [str(p) for p in files]
        return out
