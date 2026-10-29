---
name: autonomous-film
description: Make a finished short film from a brief.
version: 0.1.0
author: AgentSparko
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [video, imagination-studio, lfm3, local]
    category: creative
---

# Autonomous film from a brief

Turn one sentence into a rendered, scored, narrated video with Imagination Studio on the Spark, with
LFM3-1.7B planning the shots and LFM3-Omni judging stills and clips. Everything runs locally; one
30-second film takes roughly 20-40 minutes of rendering.

## When to Use

- "Make me a 30 second video about ...", "turn this script into a short", a scheduled daily clip.
- Not for a single still or one clip: use the `studio_submit` tool directly (see `studio-tools`).

## Prerequisites

- Imagination Studio answering on `studio_url` (`studio_state` succeeds).
- `lfm3-brain` (planner) and `lfm3-omni` (review) llama-servers up; without them the pipeline still
  runs with a plain plan and no visual review.

## How to Run

1. Call `director_run` with the brief, `seconds`, `aspect`, `style`, `voice_over`, `music`.
   Use `dry_run: true` first when the user wants to approve the shot list.
2. Report the `run_id` and tell the user it renders in the background.
3. Poll `director_status` every few minutes (or when asked); relay the log lines that matter: which shot
   is rendering, retakes and why, the export path.
4. When `status` is `done`, send the export (`export` is a studio-relative path; the file is
   `<studio_home>/<export>`; or link `studio_url/files/<export>`).

## Quick Reference

| Need | Tool |
|---|---|
| Plan only | `director_run` with `dry_run: true` |
| Progress | `director_status` |
| Judge a result yourself | `studio_review` with `paths` + `intent` |
| Re-render one shot | `studio_submit` kind `video` with `image` = the shot's still |
| Studio's own gate | `studio_qa` on the clip |

## Procedure (what the pipeline does)

1. Plan: LFM3-1.7B writes title, style, music caption and 4-10 s shots (image prompt, video prompt,
   narration, ambience) that add up to the target length.
2. Per shot: `scout` 4 stills -> LFM3-Omni picks the best for the intent -> `video` from that still ->
   Omni scores the clip; below `review_min_score` it retakes once with a new seed and a hint.
3. Narration: one `voice` take per shot with text. Ambience: a `foley` pass on the clip when the plan
   names a sound.
4. Music: one `music` take for the whole film. Export: clips in order with voice and soundtrack.

## Pitfalls

- Clips are 22-362 frames (about 1-15 s). Over 15 s the studio chains clips (`long`); the director
  keeps shots at 4-10 s instead.
- Negations do nothing in video prompts ("no rain" makes rain): describe what should be there.
- On a Spark renders are one at a time; do not queue a second film while one runs.
- The studio's access key is read from `<studio_home>/access-key`; never paste it into a chat.

## Verification

`director_status` shows `status: done` and an `export` path; the file plays; the log lists one
`picked` and one `omni score` line per shot.
