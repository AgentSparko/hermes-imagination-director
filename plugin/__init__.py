"""imagination-director: Hermes drives a local Imagination Studio with LFM3 models (standalone plugin)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .tools import SCHEMAS, Settings, make_handlers

logger = logging.getLogger(__name__)
_HERE = Path(__file__).resolve().parent
_EMOJI = {"studio_state": "🎬", "studio_jobs": "📋", "studio_submit": "🎞️", "studio_wait": "⏳", "studio_estimate": "🧮",
          "studio_prompt_check": "🔎", "studio_compose": "✍️", "studio_library": "🗂️", "studio_voice_take": "🎙️",
          "studio_music_take": "🎵", "studio_qa": "✅", "studio_review": "👁️", "director_run": "🎥", "director_status": "📈"}


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="director_cmd")
    sub.add_parser("status", help="studio, LFM3 servers and latest run")
    run = sub.add_parser("run", help="run the autonomous pipeline from a brief (foreground)")
    run.add_argument("brief")
    run.add_argument("--seconds", type=int, default=30)
    run.add_argument("--aspect", default="16:9")
    run.add_argument("--style", default="")
    run.add_argument("--no-voice", action="store_true")
    run.add_argument("--no-music", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    rev = sub.add_parser("review", help="review a still/clip with LFM3-Omni")
    rev.add_argument("path", nargs="+")
    rev.add_argument("--intent", default="")


def _cli_handler_factory(settings: Settings, handlers: dict):
    def run(args) -> int:
        cmd = getattr(args, "director_cmd", None) or "status"
        if cmd == "status":
            print(handlers["studio_state"]({}))
            try:
                print(handlers["director_status"]({}))
            except Exception:  # noqa: BLE001
                pass
            return 0
        if cmd == "review":
            print(handlers["studio_review"]({"paths": args.path, "intent": args.intent}))
            return 0
        if cmd == "run":
            from .director.pipeline import Director, FilmSpec
            from .omni_review import OmniReviewer
            from .studio_client import StudioClient
            spec = FilmSpec(brief=args.brief, seconds=args.seconds, aspect=args.aspect, style=args.style,
                            voice_over=not args.no_voice, music=not args.no_music, max_clip_seconds=settings.max_clip_seconds)
            d = Director(spec, studio=StudioClient(settings.studio_url, settings.studio_key, settings.studio_home),
                         omni=OmniReviewer(settings.omni_url), brain_url=settings.brain_url, home=Path(settings.director_home))
            if args.dry_run:
                print(json.dumps(d.plan(), indent=2, ensure_ascii=False))
                return 0
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
            result = d.run()
            print(json.dumps({k: result.get(k) for k in ("run_id", "status", "export", "error")}, indent=2))
            return 0 if result.get("status", "").startswith("done") else 1
        return 0
    return run


def register(ctx) -> None:
    settings = Settings(ctx.get_config)
    handlers = make_handlers(settings)
    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset="imagination_studio", schema=schema, handler=handlers[name], emoji=_EMOJI.get(name, ""))
    ctx.register_cli_command(name="director", help="Imagination Studio director (status, run, review)",
                             setup_fn=_cli_setup, handler_fn=_cli_handler_factory(settings, handlers))
    for skill, desc in (("autonomous-film", "Make a finished short film from a brief."),
                        ("studio-tools", "Drive Imagination Studio jobs by hand.")):
        path = _HERE / "skills" / skill / "SKILL.md"
        if path.is_file():
            ctx.register_skill(skill, path, description=desc)
    ctx.register_hook("on_session_end", lambda **_kw: None)
    logger.info("imagination-director: studio=%s omni=%s brain=%s", settings.studio_url, settings.omni_url, settings.brain_url)
