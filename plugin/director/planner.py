"""Shot planning with LFM3-1.7B (llama-server, JSON output) - falls back to a one-shot plan without a brain."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

PLAN_SYSTEM = (
    "You are a film director planning a short generated video. Reply with JSON only, no prose, shaped exactly as:\n"
    "{{\"title\": str, \"logline\": str, \"style\": str, \"music\": {{\"caption\": str, \"lyrics\": str}},\n"
    " \"shots\": [{{\"id\": \"s1\", \"seconds\": int, \"intent\": str, \"image_prompt\": str, \"video_prompt\": str,\n"
    "             \"voice_over\": str, \"sfx\": str}}]}}\n"
    "Rules: shots are {min_s}-{max_s} seconds each and add up to about {total} seconds. image_prompt describes one "
    "still frame (subject, setting, light, lens, composition) in 25-60 words. video_prompt describes what happens "
    "during the shot starting from that frame (action, camera move, sound), 25-60 words, present tense, no "
    "negations ('no X' does nothing). voice_over is the narration spoken over the shot ('' when none), at most "
    "{wps} words per second of the shot. sfx is a short description of the ambient sound ('' when the picture's own "
    "sound is enough). Keep one visual style across shots."
)
_JSON = re.compile(r"\{.*\}", re.S)


@dataclass
class Shot:
    id: str
    seconds: int
    intent: str
    image_prompt: str
    video_prompt: str
    voice_over: str = ""
    sfx: str = ""


@dataclass
class Plan:
    title: str
    logline: str
    style: str
    shots: list[Shot]
    music: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        shots = [Shot(**{k: s.get(k, "") for k in Shot.__dataclass_fields__}) for s in data.get("shots") or []]
        return cls(title=str(data.get("title") or "Untitled"), logline=str(data.get("logline") or ""),
                   style=str(data.get("style") or ""), shots=shots, music=dict(data.get("music") or {}))


class Planner:
    def __init__(self, brain_url: str = "http://127.0.0.1:8183", timeout: int = 180):
        self.url = brain_url.rstrip("/")
        self.timeout = timeout

    def _chat(self, system: str, user: str) -> str:
        payload = {"model": "LFM3-1.7B", "temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1, "max_tokens": 2000,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - loopback
            body = json.loads(resp.read().decode())
        return ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def plan(self, brief: str, total_seconds: int, *, min_s: int = 4, max_s: int = 10, style: str = "", voice_over: bool = True) -> Plan:
        user = f"Brief: {brief}\nTarget length: {total_seconds} s." + (f"\nStyle: {style}" if style else "") + \
               ("" if voice_over else "\nNo narration: every voice_over is \"\".")
        try:
            text = self._chat(PLAN_SYSTEM.format(min_s=min_s, max_s=max_s, total=total_seconds, wps=2.3), user)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"planner brain not reachable at {self.url}: {e}") from None
        if "</think>" in text:
            text = text.split("</think>", 1)[1]
        m = _JSON.search(text)
        if not m:
            raise RuntimeError("planner returned no JSON")
        plan = Plan.from_dict(json.loads(m.group(0)))
        return normalize(plan, total_seconds, min_s, max_s, brief)


def normalize(plan: Plan, total_seconds: int, min_s: int, max_s: int, brief: str) -> Plan:
    """Clamp shot lengths to the model's range and make sure there is at least one usable shot."""
    shots = [s for s in plan.shots if s.video_prompt.strip() or s.image_prompt.strip()]
    if not shots:
        shots = [Shot("s1", min(max_s, max(min_s, total_seconds)), brief, brief, brief)]
    for i, s in enumerate(shots, 1):
        s.id = s.id or f"s{i}"
        s.seconds = int(max(min_s, min(max_s, int(s.seconds or min_s))))
        s.image_prompt = s.image_prompt.strip() or s.video_prompt.strip()
        s.video_prompt = s.video_prompt.strip() or s.image_prompt.strip()
    while sum(s.seconds for s in shots) > total_seconds * 1.3 and len(shots) > 1:
        shots.pop()
    plan.shots = shots
    return plan


def fallback_plan(brief: str, total_seconds: int, max_s: int, style: str = "") -> Plan:
    n = max(1, round(total_seconds / max_s))
    per = max(4, min(max_s, total_seconds // n))
    shots = [Shot(f"s{i + 1}", per, brief, f"{brief}. {style}".strip(". "), f"{brief}, shot {i + 1} of {n}, slow camera move") for i in range(n)]
    return Plan(title=brief[:60], logline=brief, style=style, shots=shots, music={"caption": f"score for: {brief}", "lyrics": ""})
