"""The autonomous pipeline: brief -> plan -> stills -> Omni pick -> clips -> QA/review -> voice -> music -> sfx -> export.

Every step writes ``state.json`` in the run folder so a crash or a restart can resume, and
``director_status`` can report. The studio does the rendering; this only orchestrates.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..omni_review import OmniReviewer
from ..studio_client import StudioClient, StudioError
from .planner import Plan, Planner, fallback_plan

logger = logging.getLogger(__name__)


@dataclass
class FilmSpec:
    brief: str
    seconds: int = 30
    aspect: str = "16:9"
    voice_over: bool = True
    music: bool = True
    sfx: bool = True
    style: str = ""
    max_clip_seconds: int = 10
    min_clip_seconds: int = 4
    scout_count: int = 4
    review_min_score: int = 6
    max_retakes: int = 1
    preset: str = ""          # studio preset for clips ("" = studio default)
    quality: str = ""


@dataclass
class ShotState:
    id: str
    status: str = "pending"      # pending|scouting|animating|voicing|done|failed
    still: str = ""
    still_review: dict = field(default_factory=dict)
    clip: str = ""
    clip_review: dict = field(default_factory=dict)
    voice: str = ""
    sfx_clip: str = ""
    jobs: list[str] = field(default_factory=list)
    error: str = ""
    retakes: int = 0


class Director:
    def __init__(self, spec: FilmSpec, *, studio: StudioClient, omni: OmniReviewer, brain_url: str, home: Path,
                 run_id: str | None = None, sleep=time.sleep):
        self.spec = spec
        self.studio, self.omni = studio, omni
        self.planner = Planner(brain_url)
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.folder = Path(home) / self.run_id
        self.folder.mkdir(parents=True, exist_ok=True)
        self._sleep = sleep
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"run_id": self.run_id, "status": "created", "step": "", "brief": spec.brief,
                                      "spec": asdict(spec), "plan": None, "shots": [], "music": {}, "export": "",
                                      "log": [], "started": time.time(), "finished": None, "error": ""}
        self._plan: Optional[Plan] = None
        self._shots: list[ShotState] = []
        self._save()

    # ---------- bookkeeping ----------
    def _log(self, msg: str) -> None:
        logger.info("[%s] %s", self.run_id, msg)
        with self._lock:
            self.state["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
            self.state["log"] = self.state["log"][-200:]
        self._save()

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)
        self._save()

    def _save(self) -> None:
        with self._lock:
            self.state["shots"] = [asdict(s) for s in self._shots]
            if self._plan is not None:
                self.state["plan"] = self._plan.to_dict()
            tmp = self.folder / "state.json.tmp"
            tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str))
            tmp.replace(self.folder / "state.json")

    def status(self) -> dict:
        with self._lock:
            st = dict(self.state)
        st["log"] = st["log"][-25:]
        return st

    @staticmethod
    def load_status(home: Path, run_id: str | None = None) -> dict:
        home = Path(home)
        if run_id:
            path = home / run_id / "state.json"
        else:
            runs = sorted(p for p in home.glob("*/state.json")) if home.is_dir() else []
            if not runs:
                raise FileNotFoundError(f"no director runs in {home}")
            path = runs[-1]
        if not path.is_file():
            raise FileNotFoundError(f"no run {run_id} in {home}")
        st = json.loads(path.read_text())
        st["log"] = st.get("log", [])[-25:]
        return st

    # ---------- steps ----------
    def plan(self) -> dict:
        self._set(status="planning", step="plan")
        try:
            self._plan = self.planner.plan(self.spec.brief, self.spec.seconds, min_s=self.spec.min_clip_seconds,
                                           max_s=self.spec.max_clip_seconds, style=self.spec.style, voice_over=self.spec.voice_over)
            self._log(f"plan: {len(self._plan.shots)} shots, '{self._plan.title}'")
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            self._log(f"planner failed ({e}); using a plain plan")
            self._plan = fallback_plan(self.spec.brief, self.spec.seconds, self.spec.max_clip_seconds, self.spec.style)
        self._shots = [ShotState(id=s.id) for s in self._plan.shots]
        self._save()
        return self._plan.to_dict()

    def _wait(self, job: dict, timeout_s: float = 3600) -> dict:
        job_id = str(job.get("id") or "")
        if not job_id:
            raise StudioError(f"studio returned no job id: {job}")
        done = self.studio.wait(job_id, timeout_s=timeout_s, poll_s=5.0)
        if str(done.get("status", "")).lower() in ("failed", "error", "cancelled", "canceled") or done.get("error"):
            raise StudioError(f"job {job_id} {done.get('status')}: {done.get('error') or (done.get('log') or '')[-300:]}")
        return done

    def _scout(self, shot, st: ShotState) -> None:
        st.status = "scouting"
        self._save()
        params = {"prompt": self._styled(shot.image_prompt), "count": self.spec.scout_count, "preset": "2k", "seed": 0}
        job = self.studio.submit("scout", params, f"{self.run_id} {shot.id} stills")
        st.jobs.append(str(job.get("id")))
        done = self._wait(job, 1800)
        stills = [o for o in done.get("outputs") or [] if str(o).endswith(".png")]
        if not stills:
            raise StudioError("scout produced no stills")
        if len(stills) > 1 and self.omni.alive():
            pick = self.omni.pick([self.studio.local_path(p) for p in stills], shot.intent)
            best = pick.get("best_file")
            st.still = next((p for p in stills if str(self.studio.local_path(p)) == best), stills[0])
            st.still_review = {k: pick.get(k) for k in ("reason", "scores") if k in pick}
            self._log(f"{shot.id}: picked {Path(st.still).name} ({pick.get('reason', '')[:80]})")
        else:
            st.still = stills[0]
            self._log(f"{shot.id}: {len(stills)} stills, took the first (omni {'off' if not self.omni.alive() else 'n/a'})")

    def _animate(self, shot, st: ShotState) -> None:
        st.status = "animating"
        self._save()
        params: dict[str, Any] = {"prompt": shot.video_prompt, "seconds": shot.seconds, "image": st.still}
        if self.spec.preset:
            params["preset"] = self.spec.preset
        if self.spec.quality:
            params["quality"] = self.spec.quality
        check = self.studio.prompt_check(shot.video_prompt)
        if check.get("negations") or check.get("forbids"):
            self._log(f"{shot.id}: prompt forbids {check.get('negations') or check.get('forbids')} (the model ignores negations)")
        for attempt in range(self.spec.max_retakes + 1):
            job = self.studio.submit("video", params, f"{self.run_id} {shot.id} clip")
            st.jobs.append(str(job.get("id")))
            done = self._wait(job, 5400)
            clips = [o for o in done.get("outputs") or [] if str(o).endswith((".mp4", ".mov"))]
            if not clips:
                raise StudioError("video job produced no clip")
            st.clip = clips[0]
            if done.get("qa"):
                st.clip_review["studio_qa"] = done["qa"]
            if self.omni.alive():
                review = self.omni.review(self.studio.local_path(st.clip), shot.intent)
                st.clip_review["omni"] = review
                score = review.get("score")
                try:
                    score = int(score)
                except (TypeError, ValueError):
                    score = None
                self._log(f"{shot.id}: clip {Path(st.clip).name} omni score {score} {review.get('notes', '')[:80]}")
                if score is not None and score < self.spec.review_min_score and attempt < self.spec.max_retakes:
                    st.retakes += 1
                    params["seed"] = int(params.get("seed") or 0) + 1
                    problems = "; ".join(str(p) for p in (review.get("problems") or [])[:3])
                    if problems:
                        params["prompt"] = shot.video_prompt + " " + self._fix_hint(problems)
                    self._log(f"{shot.id}: retake {st.retakes} ({problems[:100]})")
                    continue
            break

    def _voice(self, shot, st: ShotState) -> None:
        if not (self.spec.voice_over and shot.voice_over.strip()):
            return
        st.status = "voicing"
        self._save()
        take = self.studio.voice_take(shot.voice_over.strip(), title=f"{self.run_id} {shot.id} VO")
        if take.get("id"):
            take = self.studio.wait_take("voice", str(take["id"]))
        st.voice = str(take.get("path") or take.get("audio") or take.get("id") or "")
        self._log(f"{shot.id}: voice take {st.voice}")

    def _sfx(self, shot, st: ShotState) -> None:
        if not (self.spec.sfx and shot.sfx.strip() and st.clip):
            return
        try:
            job = self.studio.submit("foley", {"video": st.clip, "prompt": shot.sfx.strip(), "keep_sound": True}, f"{self.run_id} {shot.id} foley")
            st.jobs.append(str(job.get("id")))
            done = self._wait(job, 1800)
            outs = [o for o in done.get("outputs") or [] if str(o).endswith((".mp4", ".mov"))]
            if outs:
                st.sfx_clip = outs[0]
                self._log(f"{shot.id}: foley -> {Path(st.sfx_clip).name}")
        except StudioError as e:
            self._log(f"{shot.id}: foley skipped ({str(e)[:120]})")

    def _music(self) -> None:
        if not self.spec.music:
            return
        music = (self._plan.music if self._plan else {}) or {}
        caption = str(music.get("caption") or f"cinematic score for: {self.spec.brief}")
        self._set(step="music")
        try:
            take = self.studio.music_take(caption, seconds=max(10, min(300, self.spec.seconds + 2)), lyrics=str(music.get("lyrics") or ""),
                                          title=f"{self.run_id} score")
            if take.get("id"):
                take = self.studio.wait_take("music", str(take["id"]), timeout_s=2400)
            self.state["music"] = {"take": take.get("id"), "path": take.get("path") or take.get("audio"), "caption": caption}
            self._log(f"music take {take.get('id')}")
        except StudioError as e:
            self._log(f"music skipped ({str(e)[:120]})")
        self._save()

    def _export(self) -> None:
        clips = [{"path": (s.sfx_clip or s.clip)} for s in self._shots if (s.sfx_clip or s.clip)]
        if not clips:
            raise StudioError("nothing to export")
        for s, clip in zip([x for x in self._shots if (x.sfx_clip or x.clip)], clips):
            if s.voice:
                clip["voice"] = s.voice
        params: dict[str, Any] = {"clips": clips}
        if self.state.get("music", {}).get("take"):
            params["soundtrack"] = {"take": self.state["music"]["take"], "music_db": -14}
        self._set(step="export")
        job = self.studio.submit("export", params, f"{self.run_id} export")
        done = self._wait(job, 1800)
        outs = [o for o in done.get("outputs") or [] if str(o).endswith((".mp4", ".mov"))]
        self._set(export=outs[0] if outs else "", export_job=str(job.get("id")))
        self._log(f"export -> {outs[0] if outs else done.get('outputs')}")

    # ---------- run ----------
    def run(self) -> dict:
        try:
            if self._plan is None:
                self.plan()
            (self.folder / "plan.json").write_text(json.dumps(self._plan.to_dict(), indent=2, ensure_ascii=False))
            self._set(status="running")
            for shot, st in zip(self._plan.shots, self._shots):
                if st.status == "done":
                    continue
                self._set(step=f"shot {shot.id}")
                try:
                    if not st.still:
                        self._scout(shot, st)
                    if not st.clip:
                        self._animate(shot, st)
                    self._voice(shot, st)
                    self._sfx(shot, st)
                    st.status = "done"
                except StudioError as e:
                    st.status, st.error = "failed", str(e)
                    self._log(f"{shot.id} failed: {e}")
                self._save()
            if not any(s.clip for s in self._shots):
                raise StudioError("no shot rendered: " + "; ".join(f"{s.id}: {s.error}" for s in self._shots if s.error)[:400])
            self._music()
            self._export()
            failed = [s.id for s in self._shots if s.status == "failed"]
            self._set(status="done" if not failed else "done_with_failures", step="", finished=time.time())
            self._log(f"finished{' with failures: ' + ', '.join(failed) if failed else ''}")
        except Exception as e:  # noqa: BLE001 - the run must end with a state, whatever happened
            logger.exception("director run failed")
            self._set(status="failed", error=str(e), finished=time.time())
            self._log(f"run failed: {e}")
        return self.status()

    # ---------- helpers ----------
    def _styled(self, prompt: str) -> str:
        style = (self._plan.style if self._plan and self._plan.style else self.spec.style).strip()
        aspect = {"16:9": "", "9:16": " vertical portrait framing", "1:1": " square framing"}.get(self.spec.aspect, "")
        return f"{prompt.rstrip('.')}. {style}{aspect}".strip()

    @staticmethod
    def _fix_hint(problems: str) -> str:
        return f"(Keep the subject clear and steady; avoid: {problems}.)"
