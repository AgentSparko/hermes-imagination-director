import json
from pathlib import Path

import pytest

from plugin import tools
from plugin.director.pipeline import Director, FilmSpec
from plugin.director.planner import Plan, Shot, fallback_plan, normalize
from plugin.omni_review import OmniReviewer
from plugin.studio_client import StudioClient, StudioError


def client(studio):
    return StudioClient(studio.url, "", str(studio.home), timeout=5)


class TestClient:
    def test_key_header_from_folder(self, studio):
        c = client(studio)
        assert c._key == "secret-key"
        c.state()
        assert studio.calls[-1][1] == "/api/state"

    def test_submit_wait_and_errors(self, studio):
        c = client(studio)
        job = c.submit("scout", {"prompt": "a fox", "count": 2}, "t")
        done = c.wait(job["id"], timeout_s=10, poll_s=0.01)
        assert done["status"] == "done" and len(done["outputs"]) == 3
        assert (studio.home / done["outputs"][0]).is_file()
        with pytest.raises(StudioError, match="unknown job kind"):
            c.submit("nope", {})
        with pytest.raises(StudioError, match="prompt is needed"):
            c.submit("video", {})
        with pytest.raises(StudioError, match="not reachable"):
            StudioClient("http://127.0.0.1:1", timeout=1).state()

    def test_takes(self, studio):
        c = client(studio)
        t = c.voice_take("hello", seed=3)
        assert c.wait_take("voice", t["id"], poll_s=0.01)["path"].endswith(".wav")
        m = c.music_take("calm piano", 20, lyrics="la")
        assert c.wait_take("music", m["id"], poll_s=0.01)["caption"] == "calm piano"
        assert [b for m, p, b in studio.calls if p == "/api/music/takes"][0]["lyrics"] == "la"


class FakeOmni(OmniReviewer):
    def __init__(self, scores=None, up=True):
        super().__init__("http://127.0.0.1:1")
        self.scores = list(scores or [])
        self.up = up
        self.reviewed = []

    def alive(self):
        return self.up

    def pick(self, paths, intent):
        return {"best": 2, "reason": "sharper", "best_file": str(paths[1]), "candidates": [str(p) for p in paths]}

    def review(self, path, intent, question=""):
        self.reviewed.append(str(path))
        score = self.scores.pop(0) if self.scores else 8
        return {"score": score, "matches_intent": score >= 6, "problems": ["blurry"] if score < 6 else [], "notes": "ok", "file": str(path)}


def settings(studio, tmp_path):
    cfg = {"studio_url": studio.url, "studio_home": str(studio.home), "omni_url": "http://127.0.0.1:1",
           "brain_url": "http://127.0.0.1:1", "director_home": str(tmp_path / "films")}
    return tools.Settings(lambda k, d=None: cfg.get(k, d))


class TestTools:
    def test_state_submit_wait_review(self, studio, tmp_path):
        h = tools.make_handlers(settings(studio, tmp_path), omni=FakeOmni())
        st = json.loads(h["studio_state"]({}))
        assert st["platform"] == "spark" and st["queue"] == []
        job = json.loads(h["studio_submit"]({"kind": "video", "params": {"prompt": "x", "seconds": 5}}))
        done = json.loads(h["studio_wait"]({"job_id": job["id"], "timeout_s": 10}))
        assert done["status"] == "done" and done["outputs"][0].endswith(".mp4") and done["qa"] == {"pass": True}
        bad = json.loads(h["studio_submit"]({"kind": "video", "params": {}}))
        assert bad["ok"] is False and "prompt" in bad["error"]
        rev = json.loads(h["studio_review"]({"paths": [done["outputs"][0]], "intent": "a fox"}))
        assert rev["score"] == 8 and rev["file"].startswith(str(studio.home))
        assert json.loads(h["studio_review"]({"paths": [], "intent": ""}))["ok"] is False
        lib = json.loads(h["studio_library"]({"kind": "video"}))
        assert lib == [{"path": "renders/a.mp4", "kind": "video"}]
        assert json.loads(h["studio_prompt_check"]({"prompt": "no rain"}))["negations"] == ["rain"]
        qa = json.loads(h["studio_qa"]({"path": "renders/a.mp4"}))
        assert qa["qa"]["pass"] is True

    def test_schemas_cover_handlers(self, studio, tmp_path):
        h = tools.make_handlers(settings(studio, tmp_path), omni=FakeOmni())
        assert set(tools.SCHEMAS) == set(h)
        for name, s in tools.SCHEMAS.items():
            assert s["name"] == name and s["parameters"]["type"] == "object"


def two_shot_plan():
    return Plan(title="Fox", logline="a fox at dawn", style="35mm film look",
                shots=[Shot("s1", 5, "fox wakes", "a fox curled in grass at dawn", "the fox lifts its head", "The fox wakes.", "birds"),
                       Shot("s2", 6, "fox runs", "a fox running through a meadow", "the fox runs toward camera", "", "")],
                music={"caption": "gentle strings", "lyrics": ""})


class TestDirector:
    def test_full_run(self, studio, tmp_path):
        omni = FakeOmni(scores=[4, 9, 8])  # s1 first take scores 4 -> retake -> 9; s2 -> 8
        d = Director(FilmSpec(brief="a fox at dawn", seconds=11), studio=client(studio), omni=omni,
                     brain_url="http://127.0.0.1:1", home=tmp_path / "films", sleep=lambda s: None)
        d.studio.wait = lambda job_id, timeout_s=0, poll_s=0, on_progress=None: StudioClient.wait(d.studio, job_id, 10, 0.01)
        d._plan = two_shot_plan()
        d._shots = [__import__("plugin.director.pipeline", fromlist=["ShotState"]).ShotState(id=s.id) for s in d._plan.shots]
        result = d.run()
        assert result["status"] == "done", result["log"]
        shots = result["shots"]
        assert shots[0]["still"].endswith("scout_s2.png")           # omni picked #2
        assert shots[0]["retakes"] == 1 and shots[0]["clip_review"]["omni"]["score"] == 9
        assert shots[0]["voice"].endswith(".wav") and shots[1]["voice"] == ""
        assert shots[0]["sfx_clip"].endswith(".mp4") and shots[1]["sfx_clip"] == ""
        assert result["music"]["take"].startswith("music") and result["export"].endswith(".mp4")
        kinds = [b.get("kind") for m, p, b in studio.calls if p == "/api/jobs" and m == "POST"]
        assert kinds == ["scout", "video", "video", "foley", "scout", "video", "export"]
        export = [b for m, p, b in studio.calls if p == "/api/jobs" and b.get("kind") == "export"][0]
        assert export["params"]["soundtrack"]["take"] == result["music"]["take"]
        assert export["params"]["clips"][0]["voice"] == shots[0]["voice"]
        retake = [b for m, p, b in studio.calls if p == "/api/jobs" and b.get("kind") == "video"][1]
        assert retake["params"]["seed"] == 1 and "blurry" in retake["params"]["prompt"]
        assert (d.folder / "state.json").is_file() and (d.folder / "plan.json").is_file()
        assert Director.load_status(tmp_path / "films")["run_id"] == d.run_id

    def test_failed_shot_and_no_brain_fallback(self, studio, tmp_path):
        studio.fail_kinds.add("video")
        d = Director(FilmSpec(brief="a fox", seconds=8, music=False), studio=client(studio), omni=FakeOmni(up=False),
                     brain_url="http://127.0.0.1:1", home=tmp_path / "films")
        d.studio.wait = lambda job_id, timeout_s=0, poll_s=0, on_progress=None: StudioClient.wait(d.studio, job_id, 10, 0.01)
        result = d.run()
        assert result["plan"]["shots"][0]["video_prompt"].startswith("a fox")   # fallback plan (brain down)
        assert result["status"] == "failed" and "no shot rendered" in result["error"] and "boom" in result["error"]
        assert result["shots"][0]["status"] == "failed" and "boom" in result["shots"][0]["error"]

    def test_normalize_clamps(self):
        p = normalize(Plan("t", "l", "", [Shot("", 40, "i", "", "run")]), 10, 4, 10, "brief")
        assert p.shots[0].seconds == 10 and p.shots[0].id == "s1" and p.shots[0].image_prompt == "run"
        fb = fallback_plan("brief", 30, 10, "noir")
        assert len(fb.shots) == 3 and fb.shots[0].image_prompt == "brief. noir"
