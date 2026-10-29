"""Hermes tools over the Imagination Studio API, LFM3-Omni review, and the director pipeline."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .omni_review import OmniReviewer
from .studio_client import JOB_KINDS, StudioClient, StudioError, job_summary


def _s(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required or []}}


SCHEMAS = {
    "studio_state": _s("studio_state", "Imagination Studio status: presets, durations, platform, what is loaded, queue. Call first.", {}),
    "studio_jobs": _s("studio_jobs", "List recent studio jobs (id, kind, status, outputs) or one job in detail.",
                      {"job_id": {"type": "string", "description": "Optional job id for details (log tail, qa)."}}),
    "studio_submit": _s("studio_submit",
                        "Queue a studio job. kinds: scout (stills: prompt, count<=12, preset 2k|hd, width/height, seed); "
                        "video (clip: prompt, seconds<=15, image=<studio path of first frame>, last_image, preset, quality, loras, "
                        "references=[paths]); studio (image_prompt + prompt + seconds: still then clip in one go); long "
                        "(prompt, total_seconds 16-1800, segment_seconds); export (clips=[{path}], soundtrack={take, music_db}); "
                        "sfx (prompt, seconds); foley (video=<path>, prompt); edit/upscale/mask/remove/turnaround (see studio_state). "
                        "Returns the job record; poll with studio_wait.",
                        {"kind": {"type": "string", "enum": list(JOB_KINDS)},
                         "params": {"type": "object", "description": "The job's settings as the studio expects them."},
                         "title": {"type": "string"}}, ["kind", "params"]),
    "studio_wait": _s("studio_wait", "Block until a job finishes (or timeout) and return its outputs and QA.",
                      {"job_id": {"type": "string"}, "timeout_s": {"type": "integer", "description": "Default 1800."}}, ["job_id"]),
    "studio_estimate": _s("studio_estimate", "Expected time for a clip and, on a Spark, whether it fits memory. Use before long or 2K jobs.",
                          {"kind": {"type": "string"}, "params": {"type": "object"}}, ["kind", "params"]),
    "studio_prompt_check": _s("studio_prompt_check", "What a video prompt forbids by name (negations the model ignores). Fix the prompt instead of adding 'no X'.",
                              {"prompt": {"type": "string"}}, ["prompt"]),
    "studio_compose": _s("studio_compose", "Write a video prompt from structured fields (subject, action, setting, camera, light, sound...) the studio's way.",
                         {"fields": {"type": "object"}, "seconds": {"type": "number"}, "first_frame": {"type": "boolean"}}, ["fields"]),
    "studio_library": _s("studio_library", "Finished renders, scouts, exports and sound files in the studio folder (paths usable as image/video params).",
                         {"kind": {"type": "string", "description": "Filter: video|image|audio"}, "limit": {"type": "integer"}}),
    "studio_voice_take": _s("studio_voice_take", "Record a line with the studio's voice engine (ref_audio = studio path of a reference wav for a cloned voice). Returns take id + audio path when done.",
                            {"text": {"type": "string"}, "ref_audio": {"type": "string"}, "ref_text": {"type": "string"},
                             "seed": {"type": "integer"}, "wait": {"type": "boolean", "description": "Default true."}}, ["text"]),
    "studio_music_take": _s("studio_music_take", "Generate a song/score: caption (style, mood, instruments), optional lyrics, seconds. Returns take id + audio path.",
                            {"caption": {"type": "string"}, "lyrics": {"type": "string"}, "seconds": {"type": "number"},
                             "seed": {"type": "integer"}, "wait": {"type": "boolean"}}, ["caption"]),
    "studio_qa": _s("studio_qa", "Run the studio's own QA gate on a clip (studio path) and return the report.",
                    {"path": {"type": "string"}, "prompt": {"type": "string"}}, ["path"]),
    "studio_review": _s("studio_review", "Look at a still or clip with LFM3-Omni: score 1-10, problems, whether it matches the intent. Or pick the best of several stills.",
                        {"paths": {"type": "array", "items": {"type": "string"}, "description": "Studio-relative or absolute paths; 2+ paths = pick the best."},
                         "intent": {"type": "string", "description": "What the shot should show."},
                         "question": {"type": "string", "description": "Optional free question instead of the standard review."}}, ["paths", "intent"]),
    "director_run": _s("director_run", "Start the autonomous film pipeline from a brief: plan shots with LFM3-1.7B, scout stills, pick with LFM3-Omni, "
                       "animate, voice-over, music, sound, QA, export. Runs in the background; follow with director_status.",
                       {"brief": {"type": "string"}, "seconds": {"type": "integer", "description": "Target length (default 30)."},
                        "aspect": {"type": "string", "description": "16:9 (default), 9:16, 1:1"},
                        "voice_over": {"type": "boolean"}, "music": {"type": "boolean"}, "style": {"type": "string"},
                        "dry_run": {"type": "boolean", "description": "Plan only, submit nothing."}}, ["brief"]),
    "director_status": _s("director_status", "Progress and results of director runs (latest or by id).",
                          {"run_id": {"type": "string"}}),
}


class Settings:
    def __init__(self, get_config):
        def pick(k, d):
            env = os.environ.get("IMAGINATION_" + k.upper())
            if env not in (None, ""):
                return type(d)(env) if not isinstance(d, str) else env
            v = get_config(k, d)
            return d if v in (None, "") else v
        self.studio_url = str(pick("studio_url", "http://127.0.0.1:7870")).rstrip("/")
        self.studio_key = str(pick("studio_key", ""))
        self.studio_home = os.path.expanduser(str(pick("studio_home", "~/ImaginationStudio")))
        self.omni_url = str(pick("omni_url", "http://127.0.0.1:8182")).rstrip("/")
        self.brain_url = str(pick("brain_url", "http://127.0.0.1:8183")).rstrip("/")
        self.director_home = os.path.expanduser(str(pick("director_home", "~/.hermes-imagination-director/films")))
        self.max_clip_seconds = int(pick("max_clip_seconds", 10))
        self.max_parallel_jobs = int(pick("max_parallel_jobs", 1))


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _err(e: Exception | str) -> str:
    return json.dumps({"ok": False, "error": str(e)})


def make_handlers(settings: Settings, *, studio: StudioClient | None = None, omni: OmniReviewer | None = None) -> dict[str, Any]:
    studio = studio or StudioClient(settings.studio_url, settings.studio_key, settings.studio_home)
    omni = omni or OmniReviewer(settings.omni_url)
    runs: dict[str, Any] = {}

    def resolve(path: str) -> Path:
        p = Path(os.path.expanduser(path))
        return p if p.is_absolute() else studio.local_path(path)

    def state(args, **_):
        try:
            st = studio.state()
            keep = {k: st.get(k) for k in ("platform", "platform_label", "presets", "durations", "long", "qualities", "home", "version", "turbo", "image_models", "last_frame") if k in st}
            keep["queue"] = [job_summary(j) | {"log_tail": None} for j in studio.jobs()[:8]]
            return _ok(keep)
        except StudioError as e:
            return _err(e)

    def jobs(args, **_):
        try:
            if args.get("job_id"):
                return _ok(job_summary(studio.job(str(args["job_id"]))))
            return _ok([{k: v for k, v in job_summary(j).items() if k != "log_tail"} for j in studio.jobs()[:25]])
        except StudioError as e:
            return _err(e)

    def submit(args, **_):
        try:
            params = dict(args.get("params") or {})
            return _ok(job_summary(studio.submit(str(args.get("kind")), params, str(args.get("title") or ""))))
        except StudioError as e:
            return _err(e)

    def wait(args, **_):
        try:
            job = studio.wait(str(args["job_id"]), timeout_s=float(args.get("timeout_s") or 1800))
            return _ok(job_summary(job))
        except (StudioError, KeyError) as e:
            return _err(e)

    def estimate(args, **_):
        try:
            return _ok(studio.estimate(str(args.get("kind") or "video"), dict(args.get("params") or {})))
        except StudioError as e:
            return _err(e)

    def prompt_check(args, **_):
        try:
            return _ok(studio.prompt_check(str(args.get("prompt") or "")))
        except StudioError as e:
            return _err(e)

    def compose(args, **_):
        try:
            return _ok(studio.compose(dict(args.get("fields") or {}), args.get("seconds"), bool(args.get("first_frame"))))
        except StudioError as e:
            return _err(e)

    def library(args, **_):
        try:
            items = studio.library()
            kind = args.get("kind")
            if kind:
                items = [i for i in items if i.get("kind") == kind]
            return _ok(items[: int(args.get("limit") or 40)])
        except StudioError as e:
            return _err(e)

    def voice_take(args, **_):
        try:
            extra = {k: args[k] for k in ("ref_audio", "ref_text", "seed") if args.get(k) not in (None, "")}
            take = studio.voice_take(str(args.get("text") or ""), **extra)
            if args.get("wait", True) and take.get("id"):
                take = studio.wait_take("voice", str(take["id"]))
            return _ok(take)
        except StudioError as e:
            return _err(e)

    def music_take(args, **_):
        try:
            extra = {k: args[k] for k in ("seed",) if args.get(k) not in (None, "")}
            take = studio.music_take(str(args.get("caption") or ""), float(args.get("seconds") or 30), str(args.get("lyrics") or ""), **extra)
            if args.get("wait", True) and take.get("id"):
                take = studio.wait_take("music", str(take["id"]))
            return _ok(take)
        except StudioError as e:
            return _err(e)

    def qa(args, **_):
        try:
            job = studio.qa(str(args.get("path") or ""), str(args.get("prompt") or ""))
            if job.get("id"):
                job = studio.wait(str(job["id"]), timeout_s=900)
            return _ok(job_summary(job))
        except StudioError as e:
            return _err(e)

    def review(args, **_):
        paths = [str(p) for p in (args.get("paths") or []) if p]
        if not paths:
            return _err("paths is required")
        try:
            files = [resolve(p) for p in paths]
            if len(files) == 1:
                return _ok(omni.review(files[0], str(args.get("intent") or ""), str(args.get("question") or "")))
            return _ok(omni.pick(files, str(args.get("intent") or "")))
        except RuntimeError as e:
            return _err(e)

    def director_run(args, **_):
        from .director.pipeline import Director, FilmSpec
        brief = str(args.get("brief") or "").strip()
        if not brief:
            return _err("brief is required")
        spec = FilmSpec(brief=brief, seconds=int(args.get("seconds") or 30), aspect=str(args.get("aspect") or "16:9"),
                        voice_over=bool(args.get("voice_over", True)), music=bool(args.get("music", True)),
                        style=str(args.get("style") or ""), max_clip_seconds=settings.max_clip_seconds)
        d = Director(spec, studio=studio, omni=omni, brain_url=settings.brain_url, home=Path(settings.director_home))
        runs[d.run_id] = d
        if args.get("dry_run"):
            try:
                return _ok({"run_id": d.run_id, "plan": d.plan()})
            except Exception as e:  # noqa: BLE001
                return _err(e)
        threading.Thread(target=d.run, name=f"director-{d.run_id}", daemon=True).start()
        return _ok({"run_id": d.run_id, "folder": str(d.folder), "note": "running in the background; call director_status"})

    def director_status(args, **_):
        from .director.pipeline import Director
        run_id = args.get("run_id")
        if run_id and run_id in runs:
            return _ok(runs[run_id].status())
        try:
            return _ok(Director.load_status(Path(settings.director_home), run_id))
        except FileNotFoundError as e:
            return _err(e)

    return {"studio_state": state, "studio_jobs": jobs, "studio_submit": submit, "studio_wait": wait,
            "studio_estimate": estimate, "studio_prompt_check": prompt_check, "studio_compose": compose,
            "studio_library": library, "studio_voice_take": voice_take, "studio_music_take": music_take,
            "studio_qa": qa, "studio_review": review, "director_run": director_run, "director_status": director_status}
