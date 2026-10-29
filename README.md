# hermes-imagination-director

A fully autonomous, fully local video-generation pipeline: [Hermes](https://github.com/NousResearch/hermes-agent)
directs [Imagination Studio](https://github.com/wassermanproductions/imagination-studio) (Wasserman Slate AI
Studio) on a DGX Spark, with the LFM3 family as its own brains — no Qwen, no cloud.

```
curl -fsSL https://raw.githubusercontent.com/AgentSparko/hermes-imagination-director/main/install.sh | bash
```

Then, in any Hermes chat: *"make a 30 second film about a lighthouse keeper's last night"* — or
`hermes director run "..."` from a shell, or a Hermes cron job for a daily clip.

## What it does

```
 brief ──► LFM3-1.7B (planner)   title, style, music caption, 4-10 s shots:
                                  image prompt · video prompt · narration · ambience
   for each shot:
     scout 4 stills (Imagination Studio) ──► LFM3-Omni-2B picks the best for the shot's intent
     video from that still (MiniMax H3 via the studio) ──► studio QA + LFM3-Omni score 1-10
         score too low ──► one retake with a new seed and a hint about what was wrong
     narration: studio voice take · ambience: studio foley pass
   music: one studio music take for the whole film
   export: clips in order + voice + soundtrack ──► ~/ImaginationStudio/exports/<job>.mp4
```

Every step is logged in `~/.hermes-imagination-director/films/<run>/state.json`; `director_status`
reads it, so the agent (or you) can follow along or pick up after a restart.

## Models and memory

| Service | Role | Memory |
|---|---|---|
| `lfm3-brain` LFM3-1.7B Q4_K_M, llama-server :8183 | Hermes' main model **and** the shot planner (JSON) | ~2.4 GB |
| `lfm3-omni` LFM3-Omni-2B Q4_K_M, llama-server :8182 | looks at stills and clips (`studio_review`, pipeline QA) | ~2.3 GB |
| `lfm3-audio` (optional, `IMAGINATION_MODELS="brain omni audio"`) | voice notes/calls via [hermes-spark-voice](https://github.com/AgentSparko/hermes-spark-voice) | ~3.5 GB |
| Imagination Studio engines (Qwen-Image, MiniMax H3, voice, music) | rendering, loaded/unloaded by the studio's own memory guard | up to ~100 GB |

The LFM3 servers stay resident (~5 GB) while the studio swaps its heavy models in and out, which is
the point of using small models here. If `hermes-spark-voice` is already installed, its llama-server
and model files are reused.

## What the installer does

1. Clones this repo, installs Hermes if missing, builds `llama-server` (≥ b11960) and downloads the GGUFs.
2. Installs Imagination Studio into `~/imagination-studio` and runs its `oneshot-setup.sh` (the Spark
   build: `nvcc` ≥ 12.9, ~100 GB of models) unless `SKIP_STUDIO=1` or the studio already answers on
   :7870; then starts it with `STUDIO_DEFAULT_BRAIN=local` pointing at LFM3-1.7B.
3. Copies the Hermes plugin to `~/.hermes/plugins/imagination-director` and enables it.
4. Sets `model.provider: custom` → LFM3-1.7B (`BRAIN=keep` leaves your current model), routes
   `auxiliary.vision` to LFM3-Omni, enables the Hermes API server; `config.yaml` is backed up first.
5. Installs/starts the `lfm3-brain` and `lfm3-omni` systemd user units.

## Using it

**Tools** (toolset `imagination_studio`): `studio_state`, `studio_jobs`, `studio_submit`, `studio_wait`,
`studio_estimate`, `studio_prompt_check`, `studio_compose`, `studio_library`, `studio_voice_take`,
`studio_music_take`, `studio_qa`, `studio_review` (Omni), `director_run`, `director_status`.

**Skills**: `imagination-director:autonomous-film` (the pipeline) and `imagination-director:studio-tools`
(one job at a time). **CLI**: `hermes director status|run|review`.

**Cron** (Hermes' scheduler): *"every morning at 7 make a 20 second film from today's top headline and
send it to me"* — the agent calls `director_run`, polls `director_status`, and sends the export.

**Inside the Studio**: its Agent room already lists Hermes; with this plugin enabled Hermes drives the
studio through these tools, and Brains & tools can use LFM3-1.7B as the local brain for prompt help.

### Pipeline knobs

`director_run` arguments: `seconds` (default 30), `aspect` (16:9/9:16/1:1), `style`, `voice_over`, `music`,
`dry_run` (plan only). Plugin config (`plugins.entries.imagination-director.config`): `max_clip_seconds`
(10), `studio_url`, `studio_home`, `omni_url`, `brain_url`, `director_home`. `FilmSpec` in
`plugin/director/pipeline.py` has the rest (`scout_count`, `review_min_score`, `max_retakes`, `preset`, `quality`).

## Safety and limits

- The studio renders one job at a time; the director queues one thing at a time and never runs two films.
- Access key: read from `<studio_home>/access-key` only when the studio is not on loopback; never logged.
- Qwen-Image is non-commercial and MiniMax H3 has territory limits (see the studio's README) — the
  outputs inherit those terms.
- Video prompts ignore negations; the planner is told so and `studio_prompt_check` flags them.

## Development

```bash
python -m pytest -q plugin/tests          # fake studio server, full pipeline run
hermes plugins doctor plugin --ci
```

MIT.
